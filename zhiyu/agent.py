import json
import math
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from weakref import finalize

import httpx

from .answer import IDENTIFIERS
from .connections import ModelConnection, load_connection
from .knowledge import ROOT

SLOT = re.compile(r"\{\{([a-z0-9_]+)\}\}")
CASE_SLOTS = {"case_reason", "case_action"}


@dataclass(frozen=True)
class Fact:
    topic: str
    text: str
    source: str


@dataclass(frozen=True)
class AgentReply:
    status: str
    action: str = ""
    text: str = ""
    slots: tuple[str, ...] = ()


class DialogueAgent:
    def __init__(self, *, connection=None, transport=None):
        self.connection = connection or load_connection("outbound") or ModelConnection()
        self.url, self.model = self.connection.url, self.connection.model
        self.client = None
        self._closer = None
        if not self.connection.enabled:
            return
        self.voice_timeout = float(os.environ.get("ZHIYU_AGENT_TIMEOUT", "12"))
        self.text_timeout = float(os.environ.get("ZHIYU_AGENT_TEXT_TIMEOUT", "20"))
        if not all(math.isfinite(value) and value > 0 for value in (self.voice_timeout, self.text_timeout)):
            raise ValueError("Agent 等待预算需要是正数（秒）")
        prompt_path = Path(os.environ.get("ZHIYU_AGENT_PROMPT") or ROOT / "data/dialogue_prompt.md")
        self.prompt = prompt_path.read_text(encoding="utf-8-sig").strip()
        self.request_options = self.connection.request_options()
        self.client = self.connection.client(limits=httpx.Limits(max_connections=4), transport=transport)
        # Each open dialogue holds its own Agent. Close the client when the last reference is released.
        self._closer = finalize(self, self.client.close)

    @property
    def enabled(self):
        return self.client is not None

    def close(self):
        if self._closer:
            self._closer()

    def plan(self, text, *, stage, offered, case, history, actions, facts, input_mode):
        def mask(value):
            value = IDENTIFIERS.sub("【已省略】", value)
            for identifier in (case.merchant, case.brand, case.id):
                if identifier:
                    value = re.sub(re.escape(identifier), "【已省略】", value, flags=re.IGNORECASE)
            return value

        payload = {
            "stage": stage,
            "explanation_delivered": offered,
            "actions": actions,
            "facts": [{"slot": "{{" + key + "}}", "topic": fact.topic}
                      for key, fact in facts.items()],
            "history": [{"role": role, "text": mask(value)[:220]} for role, value in history],
            "utterance": mask(text),
        }
        timeout = self.text_timeout if input_mode == "text" else self.voice_timeout
        deadline = time.monotonic() + timeout
        try:
            # Bound the turn even if a server keeps sending partial output.
            with self.client.stream("POST", f"{self.url}/chat/completions",
                                    timeout=httpx.Timeout(timeout, pool=min(timeout, .2)), json={
                "model": self.model, "max_tokens": 220, "temperature": 0.15,
                "messages": [{"role": "system", "content": self.prompt},
                             {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                "response_format": {"type": "json_object"},
                **self.request_options,
            }) as response:
                response.raise_for_status()
                content = bytearray()
                for chunk in response.iter_bytes():
                    if time.monotonic() > deadline:
                        return AgentReply("timeout")
                    content.extend(chunk)
                    if len(content) > 64_000:
                        return AgentReply("invalid")
            data = json.loads(json.loads(content)["choices"][0]["message"]["content"])
        except httpx.TimeoutException:
            return AgentReply("timeout")
        except httpx.HTTPError:
            return AgentReply("request_failed")
        except (ValueError, KeyError, TypeError, IndexError):
            return AgentReply("invalid")
        return validate_reply(data, actions, facts)


def validate_reply(data, actions, facts):
    if not isinstance(data, dict) or set(data) != {"action", "reply"}:
        return AgentReply("invalid")
    action, text = data["action"], data["reply"]
    if (not isinstance(action, str) or action not in actions or not isinstance(text, str)
            or not text.strip() or len(text) > 220):
        return AgentReply("invalid")
    slots = tuple(SLOT.findall(text))
    if (len(slots) != len(set(slots)) or any(slot not in facts for slot in slots)
            or any(char in SLOT.sub("", text) for char in "{}")):
        return AgentReply("unsupported_reference")
    if action == "explain" and not CASE_SLOTS.issubset(slots):
        return AgentReply("missing_reference")
    if action == "answer" and not slots:
        return AgentReply("missing_reference")
    if slots and action not in {"explain", "answer"}:
        return AgentReply("invalid")
    return AgentReply("used", action, text.strip(), slots)
