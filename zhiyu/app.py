import asyncio
import os
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from .answer import AnswerAssistant
from .audio import SpeechModels, missing_models
from .dialog import Dialogue, voice_templates
from .evaluation import Evolution
from .knowledge import ROOT, REVISION
from .llm import LanguageModel
from .quality import inspect, percentile
from .rtc import VoiceCall
from .storage import CallResult, MockTools, Store

RUNTIME = Path(os.environ.get("ZHIYU_RUNTIME", ROOT / "runtime")).resolve()
WEB = ROOT / "web"


@dataclass
class TextSession:
    dialog: Dialogue
    born: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


async def reap(service):
    while True:
        await asyncio.sleep(12)
        now = time.monotonic()
        for call_id, call in list(service.state.voice.items()):
            if call.closed or now - call.created > 300:
                await call.close()
                service.state.voice.pop(call_id, None)
        for call_id, session in list(service.state.text.items()):
            if now - session.born > 300 and not session.lock.locked():
                async with session.lock:
                    if session.dialog.state not in {"idle", "done"}:
                        await run_in_threadpool(session.dialog.terminal, "disconnect")
                    service.state.text.pop(call_id, None)


@asynccontextmanager
async def lifetime(service):
    service.state.store = Store(RUNTIME / "calls.db")
    service.state.model = LanguageModel()
    service.state.evolution = Evolution(RUNTIME / "evolution", service.state.model)
    service.state.voice = {}
    service.state.text = {}
    service.state.speech = None
    missing = missing_models()
    if not missing:
        service.state.speech = await asyncio.to_thread(SpeechModels)
        await asyncio.to_thread(service.state.speech.warm)
        await asyncio.to_thread(service.state.speech.prewarm, voice_templates(service.state.evolution.get()))
    service.state.answer = AnswerAssistant(asyncio.get_running_loop())
    gc = asyncio.create_task(reap(service))
    try:
        yield
    finally:
        gc.cancel()
        with suppress(asyncio.CancelledError):
            await gc
        await asyncio.gather(*(call.close() for call in service.state.voice.values()), return_exceptions=True)
        await service.state.answer.close()
        service.state.model.close()


api = FastAPI(title="智语外呼 · 本地比赛沙箱", version="1.0", lifespan=lifetime)
api.mount("/assets", StaticFiles(directory=WEB), name="assets")


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Start(Payload):
    case_id: str = Field(min_length=1, max_length=48)


class Offer(Start):
    sdp: str = Field(min_length=20, max_length=18000)
    record: bool = False
    microphone: bool = True


class Say(Payload):
    text: str = Field(min_length=1, max_length=400)


class Terminal(Payload):
    command: Literal["no_answer", "hangup", "retry"]


class Rollback(Payload):
    revision: str = Field(min_length=1, max_length=64)


@api.middleware("http")
async def same_origin(request, next_handler):
    origin = request.headers.get("origin")
    if origin and request.method not in {"GET", "HEAD"}:
        host = request.url.netloc
        if origin not in {f"http://{host}", f"https://{host}"}:
            return JSONResponse(status_code=403, content={"detail": "请从同一应用页操作"})
    response = await next_handler(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def state():
    return api.state


def new_dialog(case_id, mode):
    services = state()
    knowledge = services.evolution.get()
    if case_id not in knowledge.cases:
        raise HTTPException(404, "未知模拟任务")
    return Dialogue(knowledge, MockTools(services.store, knowledge), services.store,
                    case_id, mode, services.model.resolve if services.model.enabled else None,
                    answer_assistant=services.answer if services.answer.enabled else None)


async def text_action(call_id, method, value, **options):
    active = state().text
    session = active.get(call_id)
    if session is None:
        raise HTTPException(404, "未找到文字调试会话")
    async with session.lock:
        if call_id not in active:
            raise HTTPException(404, "文字会话已结束")
        result = await run_in_threadpool(method, session.dialog, value, **options)
        if result["state"] == "done":
            active.pop(call_id, None)
        return result


def fetch_record(call_id):
    result = state().store.detail(call_id)
    if result is None:
        raise HTTPException(404, "通话不存在")
    return result


@api.get("/", include_in_schema=False)
async def index():
    return FileResponse(WEB / "index.html")


@api.get("/api/status")
def status():
    knowledge = state().evolution.get()
    return {"voice": state().speech is not None, "missing": missing_models(), "llm": state().model.enabled,
            "model": state().model.model if state().model.enabled else None,
            "answer_model": state().answer.model if state().answer.enabled else None,
            "revision": knowledge.revision, "scene": knowledge.title, "scene_version": knowledge.version}


@api.get("/api/cases")
def cases():
    return [{"id": case.id, "title": case.name, "merchant": case.merchant,
             "route": case.channel(), "attempt": case.attempt}
            for case in state().evolution.get().cases.values()]


@api.get("/api/contracts/writeback")
def contract():
    return CallResult.model_json_schema()


@api.post("/api/offer")
async def offer(payload: Offer):
    services = state()
    if not services.speech:
        raise HTTPException(503, "离线语音模型缺失。请先运行 scripts/models.py。")
    active = sum(not call.closed for call in services.voice.values())
    if active >= 8:
        raise HTTPException(429, "模拟会话已达上限")
    dialog = new_dialog(payload.case_id, "voice")
    call = VoiceCall(dialog, services.speech, RUNTIME / "recordings", payload.record, payload.microphone)
    services.voice[dialog.id] = call
    try:
        answer = await asyncio.wait_for(call.negotiate(payload.sdp), 10)
    except (ValueError, TypeError, TimeoutError, AssertionError, IndexError):
        await call.close()
        services.voice.pop(dialog.id, None)
        raise HTTPException(400, "无法建立 WebRTC 会话，请检查音频设备与浏览器") from None
    return {"sdp": answer, "type": "answer", "session_id": dialog.id}


@api.post("/api/calls/{call_id}/turn")
async def voice_text_turn(call_id: str, payload: Say):
    call = state().voice.get(call_id)
    if call is None:
        raise HTTPException(404, "通话不存在")
    text = payload.text.strip()
    if not text:
        raise HTTPException(422, "请输入回复")
    if not await call.process(text):
        raise HTTPException(409, "通话当前无法接收回复")
    return {"accepted": True}


@api.post("/api/calls/{call_id}/close")
async def close(call_id: str):
    call = state().voice.pop(call_id, None)
    if call:
        await call.close()
    return {"closed": True}


@api.post("/api/text")
async def text_start(payload: Start):
    if len(state().text) >= 32:
        raise HTTPException(429, "调试会话已达上限")
    dialog = new_dialog(payload.case_id, "text")
    state().text[dialog.id] = TextSession(dialog)
    return {"session_id": dialog.id, **dialog.start()}


@api.post("/api/text/{call_id}/turn")
async def text_turn(call_id: str, payload: Say):
    return await text_action(call_id, Dialogue.handle, payload.text, input_mode="text")


@api.post("/api/text/{call_id}/terminal")
async def text_terminal(call_id: str, payload: Terminal):
    return await text_action(call_id, Dialogue.terminal, payload.command)


@api.get("/api/calls")
def calls():
    return state().store.records()


@api.get("/api/calls/{call_id}/quality")
def quality(call_id: str):
    result = inspect(fetch_record(call_id))
    result["recording"] = (RUNTIME / "recordings" / f"{call_id}.wav").is_file()
    return result


@api.get("/api/calls/{call_id}/trace")
def trace(call_id: str):
    return JSONResponse(fetch_record(call_id), headers={
        "Content-Disposition": f'attachment; filename="{call_id}-trace.json"'})


@api.get("/api/calls/{call_id}/audio")
def audio(call_id: str):
    fetch_record(call_id)
    file = RUNTIME / "recordings" / f"{call_id}.wav"
    if not file.is_file():
        raise HTTPException(404, "本通未保存录音")
    return FileResponse(file, media_type="audio/wav", filename=file.name)


@api.get("/api/metrics")
def metrics():
    calls = state().store.records(100)
    reports = [inspect(state().store.detail(call["id"])) for call in calls]
    details = [fetch_record(call["id"]) for call in calls if call["mode"] == "voice"]
    server_ms = [event["data"]["ms"] for detail in details for event in detail["events"]
                 if event["type"] == "first_audio" and event["data"]["ms"] is not None]
    client_ms = [event["data"]["ms"] for detail in details for event in detail["events"]
                 if event["type"] == "client_audio"]
    phone = [report for report in reports if report["result"] and report["result"]["channel"] == "phone"]
    return {"calls": len(calls), "closed": sum(bool(item["result"]) for item in reports),
            "completed": sum(item["complete"] for item in reports), "phone_finished": len(phone),
            "client_p50": percentile(client_ms, .5), "client_p95": percentile(client_ms, .95),
            "server_p50": percentile(server_ms, .5), "server_p95": percentile(server_ms, .95),
            "client_samples": len(client_ms), "server_samples": len(server_ms),
            "violations": sum(item["severe_output"] for item in reports),
            "intercepts": sum(item["compliance_intercepted"] for item in reports),
            "interruptions": sum(item["audio"]["interruptions"] for item in reports)}


@api.get("/api/rsi")
def rsi():
    evolution = state().evolution
    return {"active": evolution.pointer(), "history": evolution.history()}


@api.post("/api/rsi/run")
async def rsi_run(request: Request):
    return await asyncio.to_thread(request.app.state.evolution.run)


@api.post("/api/rsi/rollback")
def rollback(payload: Rollback):
    try:
        return state().evolution.rollback(payload.revision)
    except (FileNotFoundError, ValueError):
        raise HTTPException(404, "没有该知识版本") from None


@api.get("/api/rsi/{revision}/trace")
def rsi_trace(revision: str):
    if not REVISION.fullmatch(revision):
        raise HTTPException(404, "找不到迭代记录")
    file = state().evolution.folder / f"run-{revision}.json"
    if not file.is_file():
        raise HTTPException(404, "找不到迭代记录")
    return FileResponse(file, media_type="application/json", filename=file.name)
