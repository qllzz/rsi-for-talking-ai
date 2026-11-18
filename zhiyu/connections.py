import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .knowledge import ROOT


def config_path(role):
    env = "ZHIYU_AGENT_CONFIG" if role == "outbound" else "ZHIYU_JUDGE_CONFIG"
    filename = ".env.agent.json" if role == "outbound" else ".env.judge.json"
    return Path(os.environ.get(env) or ROOT / filename)


def profile_prefix(role):
    return "ZHIYU_AGENT" if role == "outbound" else "ZHIYU_JUDGE"


def normalize_url(value):
    value = value.strip().rstrip("/")
    if not value:
        return ""
    try:
        url = urlsplit(value)
        loopback = url.hostname in {"127.0.0.1", "::1", "localhost"}
        if (any(char.isspace() for char in value) or "\\" in value or not url.hostname
                or not (url.scheme == "https" or loopback and url.scheme == "http")
                or url.username is not None or url.query or url.fragment):
            raise ValueError
        base = str(httpx.URL(value)).rstrip("/")
    except (ValueError, httpx.InvalidURL):
        raise ValueError("请输入 HTTPS Chat Completions 基础地址，本机服务可以使用 HTTP") from None
    return base.removesuffix("/chat/completions").rstrip("/")


@dataclass(frozen=True)
class ModelConnection:
    url: str = ""
    model: str = ""
    token: str = field(default="", repr=False)
    enabled: bool = False

    @classmethod
    def parse(cls, data):
        keys = ("url", "model", "token")
        if not isinstance(data, dict) or any(not isinstance(data.get(key, ""), str) for key in keys):
            raise ValueError("接口地址、模型和 API Key 应填写为文本")
        enabled = data.get("enabled", bool(data.get("url", "").strip()))
        url, model, token = (data.get(key, "").strip() for key in keys)
        url = normalize_url(url)
        if type(enabled) is not bool or enabled and not (url and model):
            raise ValueError("启用接口前，请填写基础地址和模型名称")
        if any(ord(char) < 33 or ord(char) > 126 for char in token):
            raise ValueError("API Key 格式不正确")
        return cls(url, model, token, enabled)

    def client(self, **options):
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        return httpx.Client(headers=headers, trust_env=False, follow_redirects=False, **options)

    def request_options(self):
        return {"thinking": {"type": "disabled"}} if urlsplit(self.url).hostname == "api.deepseek.com" else {}


def load_connection(role):
    prefix = profile_prefix(role)
    if f"{prefix}_URL" in os.environ:
        return ModelConnection.parse({key: os.environ.get(f"{prefix}_{key.upper()}", "")
                                      for key in ("url", "model", "token")})
    path = config_path(role)
    return ModelConnection.parse(json.loads(path.read_text(encoding="utf-8-sig"))) if path.is_file() else None


def restrict_windows_profile(path):
    system32 = Path(os.environ["SystemRoot"]) / "System32"
    options = {"check": True, "capture_output": True, "creationflags": subprocess.CREATE_NO_WINDOW}
    try:
        user = subprocess.run([system32 / "whoami.exe", "/user", "/fo", "csv", "/nh"], **options)
        sid = re.search(rb'"(S-1-[0-9-]+)"', user.stdout)
        if not sid:
            raise OSError("无法识别当前 Windows 用户")
        icacls = system32 / "icacls.exe"
        grant = f"*{sid[1].decode('ascii')}:F"
        subprocess.run([icacls, path, "/grant:r", grant], **options)
        subprocess.run([icacls, path, "/inheritance:r"], **options)
    except subprocess.CalledProcessError as exc:
        raise OSError("无法限制 Windows 密钥文件的访问权限") from exc


def store_connection(role, connection):
    path = config_path(role)
    temp = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=".env.zhiyu-", delete=False) as file:
            temp = Path(file.name)
            if os.name == "nt":
                restrict_windows_profile(temp)
            json.dump({"url": connection.url, "model": connection.model, "token": connection.token,
                       "enabled": connection.enabled}, file, ensure_ascii=False, indent=2)
            file.write("\n")
        os.replace(temp, path)
    finally:
        if temp:
            temp.unlink(missing_ok=True)


class ProbeFailure(ValueError):
    pass


def probe_connection(connection, *, transport=None):
    if not connection.url or not connection.model:
        raise ProbeFailure("请填写基础地址和模型名称")
    start = time.monotonic()
    try:
        with connection.client(timeout=httpx.Timeout(12, connect=5), transport=transport) as client:
            response = client.post(f"{connection.url}/chat/completions", json={
                "model": connection.model, "max_tokens": 32,
                "messages": [{"role": "system", "content": "你正在做接口连通性测试，只返回 JSON。"},
                             {"role": "user", "content": '请返回 {"ready":true}。'}],
                "response_format": {"type": "json_object"},
                **connection.request_options(),
            })
            response.raise_for_status()
            output = json.loads(response.json()["choices"][0]["message"]["content"])
            if output != {"ready": True}:
                raise ValueError
    except httpx.TimeoutException:
        raise ProbeFailure("接口响应超时，请检查网络或模型服务") from None
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        message = {401: "鉴权失败，请检查 API Key", 403: "当前密钥没有接口权限", 404: "找不到接口或模型，请检查地址和模型名称",
                   429: "接口限流或额度不足，请稍后重试"}.get(code, f"测试请求未通过（HTTP {code}）")
        raise ProbeFailure(message) from None
    except httpx.HTTPError:
        raise ProbeFailure("无法连接接口，请检查地址和网络") from None
    except (ValueError, KeyError, TypeError, IndexError):
        raise ProbeFailure("模型未按要求返回 JSON，请确认接口兼容 Chat Completions 的 JSON 模式") from None
    return round((time.monotonic() - start) * 1000)
