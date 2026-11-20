import asyncio
import json
import os
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from .agent import DialogueAgent
from .answer import AnswerAssistant
from .audio import SpeechModels, missing_models
from .catalog import find_source, load_catalog
from .connections import load_connection
from .dialog import Dialogue, voice_templates
from .evaluation import Evolution
from .knowledge import ROOT, REVISION, SCENE_PATH
from .llm import LanguageModel
from .quality import inspect, percentile
from .rtc import VoiceCall
from .settings import local_settings, router as settings_router
from .storage import CallResult, MockTools, Store
from .training import SCOPES, STAGES as SCRIPT_STAGES, MaterialConflict, MaterialInput, MaterialUpdate, material_catalog, quick_scripts
from .trial import run_trials

RUNTIME = Path(os.environ.get("ZHIYU_RUNTIME", ROOT / "runtime")).resolve()
WEB = ROOT / "web"


def configured_catalog():
    configured = os.environ.get("ZHIYU_RULES_FILE")
    if configured:
        path = Path(configured).expanduser()
        if not path.is_file():
            raise ValueError("ZHIYU_RULES_FILE 指定的资审工作簿不存在。")
    else:
        path = find_source(ROOT / "reference") or find_source(ROOT.parent)
    if path is None:
        return None
    fixtures = json.loads(SCENE_PATH.read_text(encoding="utf-8"))["cases"]
    return load_catalog(path, fixtures=fixtures)


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
    catalog = await asyncio.to_thread(configured_catalog)
    service.state.store = Store(RUNTIME / "calls.db")
    service.state.model = LanguageModel()
    judge_connection = load_connection("judge")
    judge = LanguageModel(connection=judge_connection) if judge_connection else service.state.model
    service.state.evolution = Evolution(RUNTIME / "evolution", judge, catalog=catalog)
    service.state.ai_settings_lock = asyncio.Lock()
    service.state.ai_versions = {role: uuid4().hex for role in ("outbound", "judge")}
    service.state.catalog = {
        "ready": catalog is not None,
        "total": len(catalog or ()),
        "phone": sum(row["route"] == "phone" for row in catalog or ()),
        "jingmai": sum(row["route"] == "jingmai" for row in catalog or ()),
        "message": None if catalog is not None else "未找到资审关单场景话术 V1.0 原表，当前仅提供内置测试任务。",
    }
    service.state.voice = {}
    service.state.text = {}
    service.state.speech = None
    missing = missing_models()
    if not missing:
        service.state.speech = await asyncio.to_thread(SpeechModels)
        await asyncio.to_thread(service.state.speech.warm)
        await asyncio.to_thread(service.state.speech.prewarm, voice_templates(service.state.evolution.get()))
    service.state.answer = AnswerAssistant(asyncio.get_running_loop())
    service.state.agent = DialogueAgent()
    gc = asyncio.create_task(reap(service))
    try:
        yield
    finally:
        gc.cancel()
        with suppress(asyncio.CancelledError):
            await gc
        await asyncio.gather(*(call.close() for call in service.state.voice.values()), return_exceptions=True)
        old_agents = {session.dialog.agent for session in service.state.text.values()}
        old_agents.update(call.dialog.agent for call in service.state.voice.values())
        for agent in old_agents - {None, service.state.agent}:
            agent.close()
        service.state.agent.close()
        await service.state.answer.close()
        if service.state.evolution.model is not service.state.model:
            service.state.evolution.model.close()
        service.state.model.close()


api = FastAPI(title="智语外呼 · 资审服务工作台", version="1.1", lifespan=lifetime)
api.mount("/assets", StaticFiles(directory=WEB), name="assets")
api.include_router(settings_router)


@api.exception_handler(RequestValidationError)
async def safe_validation_error(request: Request, exc: RequestValidationError):
    if request.url.path.startswith("/api/ai-settings/"):
        return JSONResponse(status_code=422, content={"detail": "配置格式有误，请检查地址、模型和密钥字段"})
    return await request_validation_exception_handler(request, exc)


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Start(Payload):
    case_id: str = Field(min_length=1, max_length=48)
    followup_ticket: str | None = Field(default=None, pattern=r"^WK-[A-F0-9]{12}$")


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


class TrialOptions(Payload):
    alternate: str | None = Field(default=None, min_length=3, max_length=100)
    contrast_case_id: str | None = Field(default=None, min_length=1, max_length=48)


class TrialRequest(Payload):
    options: dict[str, TrialOptions] = Field(default_factory=dict, max_length=60)


class Publish(Payload):
    revision: str = Field(pattern=r"^r-[a-f0-9]{12}$")


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


def dialogue_components(services):
    agent = services.agent if services.agent.enabled else None
    return {"agent": agent, "resolver": services.model.resolve if services.model.enabled and not agent else None,
            "answer_assistant": services.answer if services.answer.enabled and not agent else None}


def new_dialog(case_id, mode, followup_ticket=None):
    services = state()
    knowledge = services.evolution.get()
    if case_id not in knowledge.cases:
        raise HTTPException(404, "未找到该资审任务")
    if mode == "voice" and knowledge.cases[case_id].channel() != "phone" and not followup_ticket:
        raise HTTPException(409, "该单据应通过京麦书面处理。")
    try:
        return Dialogue(knowledge, MockTools(services.store, knowledge), services.store,
                        case_id, mode, followup_ticket=followup_ticket, **dialogue_components(services))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


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
    judge = state().evolution.model
    agent = state().agent
    return {"voice": state().speech is not None, "missing": missing_models(), "llm": judge.enabled,
            "tts_engine": state().speech.tts_engine if state().speech else None,
            "model": judge.model if judge.enabled else None,
            "answer_model": state().answer.model if state().answer.enabled else None,
            "dialogue_model": agent.model if agent.enabled else None,
            "dialogue_policy": knowledge.dialogue["version"],
            "revision": knowledge.revision, "scene": knowledge.title, "scene_version": knowledge.version,
            "catalog": state().catalog}


@api.get("/api/cases")
def cases():
    results = state().store.latest_by_case()
    return [{"id": case.id, "title": case.name, "merchant": case.merchant,
             "route": case.channel(), "attempt": case.attempt, "categories": case.categories,
             "source_kind": case.source_kind, "source_row": case.scene_row,
             "source_cell": case.source_cell, "source_route": case.route,
             "last_result": results.get(case.id)}
            for case in state().evolution.get().cases.values()]


class CallbackSchedule(Payload):
    scheduled_at: int | None = Field(..., strict=True)
    version: int = Field(ge=0, strict=True)


@api.get('/api/callback-agenda')
def callback_agenda():
    return {'items': state().store.callback_agenda()}


@api.post('/api/work-items/{ticket_id}/schedule')
def schedule_callback(ticket_id: str, payload: CallbackSchedule, request: Request):
    services = local_settings(request)
    try:
        return services.store.schedule_callback(ticket_id, payload.scheduled_at, payload.version)
    except KeyError:
        raise HTTPException(404, '找不到回电工单') from None
    except OverflowError as exc:
        raise HTTPException(422, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@api.get("/api/work-items")
def work_items():
    return state().store.work_items()


@api.post("/api/work-items/{ticket_id}/resolve")
def resolve_work_item(ticket_id: str):
    try:
        return state().store.resolve_handoff(ticket_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from None


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
    dialog = new_dialog(payload.case_id, "voice", payload.followup_ticket)
    try:
        call = VoiceCall(dialog, services.speech, RUNTIME / "recordings", payload.record, payload.microphone)
    except Exception:
        dialog.abandon()
        raise
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
    dialog = new_dialog(payload.case_id, "text", payload.followup_ticket)
    state().text[dialog.id] = TextSession(dialog)
    try:
        response = await run_in_threadpool(dialog.start)
    except Exception:
        state().text.pop(dialog.id, None)
        await run_in_threadpool(dialog.abandon)
        raise
    if response["state"] == "done":
        state().text.pop(dialog.id, None)
    return {"session_id": dialog.id, **response}


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
    services = state()
    evolution = services.evolution
    return {"active": evolution.pointer(), "history": evolution.history(services.ai_versions)}


@api.get("/api/training")
def training_workspace():
    evolution = state().evolution
    knowledge = evolution.get()
    trial_cases = [{"id": case.id, "title": case.name, "scopes": case.scope} for case in knowledge.cases.values()
                   if case.channel() == "phone" and case.reason and case.action]
    return {"active": knowledge.revision, "scopes": SCOPES, "stages": SCRIPT_STAGES, "trial_cases": trial_cases,
            "materials": material_catalog(knowledge, evolution.materials()), "scripts": quick_scripts(knowledge)}


@api.post("/api/training/materials", status_code=201)
def create_training_material(payload: MaterialInput):
    try:
        return state().evolution.save_material(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@api.put("/api/training/materials/{identifier}")
def update_training_material(identifier: str, payload: MaterialUpdate):
    try:
        return state().evolution.save_material(payload.model_dump(exclude={"edit_version"}), identifier, payload.edit_version)
    except KeyError:
        raise HTTPException(404, "找不到可编辑的训练资料") from None
    except MaterialConflict as exc:
        raise HTTPException(409, str(exc)) from None


@api.post("/api/rsi/run")
async def rsi_run(request: Request, payload: TrialRequest | None = None):
    services = request.app.state
    async with services.ai_settings_lock:
        components = dialogue_components(services)
        options = payload.model_dump(exclude_none=True)["options"] if payload else {}

        def trial_runner(current, candidate, changes):
            return run_trials(current, candidate, changes, judge=services.evolution.model,
                              options=options, **components)

        return await run_in_threadpool(services.evolution.run, trial_runner=trial_runner,
                                       configuration=services.ai_versions.copy())


@api.post("/api/rsi/publish")
async def rsi_publish(payload: Publish, request: Request):
    services = request.app.state
    async with services.ai_settings_lock:
        try:
            return await run_in_threadpool(services.evolution.publish, payload.revision, services.ai_versions.copy())
        except MaterialConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except FileNotFoundError:
            raise HTTPException(404, "找不到该候选版本") from None


@api.post("/api/rsi/rollback")
def rollback(payload: Rollback):
    try:
        return state().evolution.rollback(payload.revision)
    except MaterialConflict as exc:
        raise HTTPException(409, str(exc)) from None
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
