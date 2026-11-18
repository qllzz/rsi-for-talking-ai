import os
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from starlette.concurrency import run_in_threadpool

from .agent import DialogueAgent
from .connections import ModelConnection, ProbeFailure, normalize_url, probe_connection, profile_prefix, store_connection
from .llm import LanguageModel


router = APIRouter(prefix="/api/ai-settings", tags=["AI 接口设置"])
Role = Literal["outbound", "judge"]
LOOPBACK = {"127.0.0.1", "::1", "localhost"}


class ProfileEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    url: str = Field(max_length=512)
    model: str = Field(max_length=150)
    enabled: bool
    token: SecretStr | None = Field(default=None, max_length=4096)
    version: str = Field(pattern=r"^[0-9a-f]{32}$")


def local_settings(request):
    if not request.client or request.client.host not in LOOPBACK or request.url.hostname not in LOOPBACK:
        raise HTTPException(403, "请在本机地址打开工作台以管理 AI 接口")
    if request.method != "GET" and request.headers.get("origin") != f"{request.url.scheme}://{request.url.netloc}":
        raise HTTPException(403, "请从同一工作台页面提交设置")
    return request.app.state


def model_for(state, role):
    return state.agent if role == "outbound" else state.evolution.model


def env_managed(role):
    return f"{profile_prefix(role)}_URL" in os.environ


def public_profile(state, role):
    current = model_for(state, role).connection
    return {"url": current.url, "model": current.model if current.url else "", "enabled": current.enabled,
            "has_token": bool(current.token), "editable": not env_managed(role), "version": state.ai_versions[role]}


def edited_profile(state, role, payload):
    if env_managed(role):
        raise HTTPException(409, "此接口由启动环境管理")
    if payload.version != state.ai_versions[role]:
        raise HTTPException(409, "接口设置已被更新，请关闭并重新打开设置")
    current = model_for(state, role).connection
    try:
        url = normalize_url(payload.url)
        if payload.token is None and current.token and url != normalize_url(current.url):
            raise ValueError("接口地址已变更，请填写新密钥或选择不使用密钥")
        token = current.token if payload.token is None else payload.token.get_secret_value()
        return ModelConnection.parse({"url": url, "model": payload.model, "token": token, "enabled": payload.enabled})
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


async def measure(connection):
    try:
        return await run_in_threadpool(probe_connection, connection)
    except ProbeFailure as exc:
        raise HTTPException(422, str(exc)) from None


@router.get("/{role}")
def read_profile(role: Role, request: Request, response: Response):
    state = local_settings(request)
    response.headers["Cache-Control"] = "no-store"
    return public_profile(state, role)


@router.post("/{role}/test")
async def test_profile(role: Role, payload: ProfileEdit, request: Request):
    state = local_settings(request)
    return {"latency_ms": await measure(edited_profile(state, role, payload))}


@router.put("/{role}")
async def update_profile(role: Role, payload: ProfileEdit, request: Request, response: Response):
    state = local_settings(request)
    async with state.ai_settings_lock:
        connection = edited_profile(state, role, payload)
        current = model_for(state, role)
        response.headers["Cache-Control"] = "no-store"
        if connection == current.connection:
            return public_profile(state, role)
        if connection.enabled:
            await measure(connection)
        updated = DialogueAgent(connection=connection) if role == "outbound" else LanguageModel(connection=connection)
        try:
            await run_in_threadpool(store_connection, role, connection)
        except OSError:
            updated.close()
            raise HTTPException(500, "无法写入本机设置文件，请检查文件权限") from None
        if role == "outbound":
            state.agent = updated
        else:
            previous = await run_in_threadpool(state.evolution.replace_judge, updated)
            if previous is not state.model:
                previous.close()
        state.ai_versions[role] = uuid4().hex
        return public_profile(state, role)
