import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
SCENE_PATH = ROOT / "data" / "scene.json"
REVISION = re.compile(r"[a-zA-Z0-9._-]{1,64}\Z")


def compact(text: str) -> str:
    return re.sub(r"[^\w\u3400-\u9fff]+", "", text.lower())


@dataclass(frozen=True)
class Case:
    id: str
    merchant: str
    brand: str
    name: str
    route: str
    scene_row: int
    reason: str
    action: str
    scope: list[str]
    attempt: int
    callback_requested: bool = False
    complaint: bool = False

    def channel(self) -> str:
        if self.callback_requested or self.complaint:
            return "phone"
        return "jingmai" if self.attempt >= 3 else self.route

    @property
    def source(self) -> str:
        return f"资审关单场景话术V1.0.xlsx · 场景汇总!{self.scene_row}"


@dataclass(frozen=True)
class Entry:
    id: str
    scopes: list[str]
    question: str
    aliases: list[str]
    answer: str
    source: str


class Knowledge:
    def __init__(self, scene: dict, revision: str = "baseline", aliases: dict | None = None):
        self.title = scene["title"]
        self.version = scene["version"]
        self.revision = revision
        self.identity = scene["identity"]
        self.opening = scene["opening"]
        self.domain_words = scene["domain_words"]
        self.cases = {item["id"]: Case(**item) for item in scene["cases"]}
        self.entries = {}
        for entry in scene["faq"]:
            extra = (aliases or {}).get(entry["id"], [])
            self.entries[entry["id"]] = Entry(**{**entry, "aliases": [*entry["aliases"], *extra]})

    @classmethod
    def load(cls, revision="baseline", aliases=None):
        return cls(json.loads(SCENE_PATH.read_text(encoding="utf-8")), revision, aliases)

    def transcript(self, text):
        for raw, clean in self.domain_words.items():
            text = text.replace(raw, clean)
        return text.strip()

    def allowed_entries(self, case: Case) -> list[Entry]:
        return [entry for entry in self.entries.values()
                if "*" in entry.scopes or set(case.scope) & set(entry.scopes)]

    def retrieve(
        self,
        text: str,
        case: Case,
        resolve: Callable[[str, list[Entry]], str | None] | None = None,
    ) -> Entry | None:
        allowed = self.allowed_entries(case)
        query = compact(text)
        exact = [(len(alias), entry.id, entry) for entry in allowed
                 for alias in map(compact, entry.aliases) if alias and alias in query]
        if exact:
            return max(exact, key=lambda item: (item[0], item[1]))[2]
        grams = set(zip(query, query[1:]))
        scores = []
        for entry in allowed:
            reference = compact(entry.question)
            terms = set(zip(reference, reference[1:]))
            score = len(grams & terms)
            if score >= 3:
                scores.append((score / max(len(terms), len(grams)), entry))
        scores.sort(key=lambda item: item[0], reverse=True)
        if scores and scores[0][0] >= 0.40 and (len(scores) < 2 or scores[0][0] - scores[1][0] > 0.10):
            return scores[0][1]
        if resolve:
            selected = resolve(text, allowed)
            return next((e for e in allowed if e.id == selected), None)
        return None
