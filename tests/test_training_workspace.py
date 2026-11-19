import json

import httpx
import pytest
from fastapi.testclient import TestClient

from zhiyu import app, evaluation, settings
from zhiyu.agent import DialogueAgent
from zhiyu.connections import probe_connection
from zhiyu.llm import LanguageModel


MATERIAL = {
    "question": "店铺联系人可以用公共邮箱吗？",
    "answer": "请使用能接收业务通知的邮箱，以京麦单据要求为准。",
    "source": "运营复核纪要",
    "scope": "shop",
    "aliases": ["店铺公共联络邮箱"],
    "enabled": True,
}
ALTERNATE = "门店通知地址能用团队共用的信箱吗？"
AGENT_TOKEN = "fixture-agent-key-training"
JUDGE_TOKEN = "fixture-judge-key-training"


@pytest.fixture
def training_client(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "RUNTIME", tmp_path)
    monkeypatch.setattr(app, "configured_catalog", lambda: None)
    monkeypatch.setattr(app, "missing_models", lambda: ["speech-fixture"])
    for name in ("ZHIYU_AGENT_URL", "ZHIYU_JUDGE_URL", "ZHIYU_LLM_URL", "ZHIYU_ANSWER_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("ZHIYU_AGENT_PROMPT", raising=False)

    def smoke_cases(split, **_options):
        # The full fixed-seed suite is already tested in test_business.py.
        return [{
            "id": f"{split}-qualification-smoke",
            "case_id": "POP-001",
            "disposition": "notified",
            "turns": ["我是负责人", "可以，请说明驳回原因", "我明白了"],
        }]

    monkeypatch.setattr(evaluation, "generate", smoke_cases)

    def setup(*, fail_role=None, enable_agent=True, enable_judge=True, reject_candidate=False, shared_model=False):
        for role, env, token, enabled in (
            ("agent", "ZHIYU_AGENT_CONFIG", AGENT_TOKEN, enable_agent),
            ("judge", "ZHIYU_JUDGE_CONFIG", JUDGE_TOKEN, enable_judge),
        ):
            profile = tmp_path / f"{role}-profile.json"
            if enabled:
                profile.write_text(json.dumps({
                    "url": "https://shared.example/v1" if shared_model else f"https://{role}.example/v1",
                    "model": "fixture-shared" if shared_model else f"fixture-{role}",
                    "token": AGENT_TOKEN if shared_model else token,
                }), encoding="utf-8")
            monkeypatch.setenv(env, str(profile))

        requests = {"agent": [], "judge": []}

        def provider(request):
            body = json.loads(request.content)
            raw = body["messages"][-1]["content"]
            if shared_model:
                assert request.url.host == "shared.example" and body["model"] == "fixture-shared"
                role = "agent" if '"utterance"' in raw else "judge"
                token = AGENT_TOKEN
            else:
                role = request.url.host.removesuffix(".example")
                token = {"agent": AGENT_TOKEN, "judge": JUDGE_TOKEN}[role]
            assert request.url.path == "/v1/chat/completions"
            assert request.headers["authorization"] == "Bearer " + token
            assert AGENT_TOKEN not in raw and JUDGE_TOKEN not in raw
            requests[role].append(raw)
            if raw == '请返回 {"ready":true}。':
                result = {"ready": True}
            elif role == fail_role:
                return httpx.Response(401, text="provider message " + AGENT_TOKEN + " " + JUDGE_TOKEN)
            elif role == "judge":
                result = judge_response(body, reject_candidate)
            else:
                result = agent_response(json.loads(raw))
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(result)}}]})

        transport = httpx.MockTransport(provider)

        def agent(**options):
            return DialogueAgent(transport=transport, **options)

        def judge(**options):
            return LanguageModel(transport=transport, **options)

        monkeypatch.setattr(app, "DialogueAgent", agent)
        monkeypatch.setattr(app, "LanguageModel", judge)
        monkeypatch.setattr(settings, "DialogueAgent", agent)
        monkeypatch.setattr(settings, "LanguageModel", judge)
        monkeypatch.setattr(settings, "probe_connection", lambda profile: probe_connection(profile, transport=transport))
        origin = "http://127.0.0.1:8765"
        return TestClient(app.api, base_url=origin, headers={"Origin": origin}, client=("127.0.0.1", 50000)), requests

    return setup


def agent_response(payload):
    stage = payload["stage"]
    if stage == "identity":
        return {"action": "verify", "reply": "谢谢确认，方便听一下本次审核说明吗？"}
    if stage == "needs":
        return {"action": "explain", "reply": "{{case_reason}}。请您{{case_action}}。还有哪里需要我说明吗？"}
    if payload["utterance"] == "我明白了":
        return {"action": "confirm", "reply": "谢谢您的确认。"}
    slots = {item["slot"] for item in payload["facts"]}
    material = next((slot for slot in sorted(slots) if slot.startswith("{{training_")), None)
    if material and any(word in payload["utterance"] for word in ("邮箱", "信箱")):
        return {"action": "answer", "reply": material}
    if "{{upload}}" in slots and "上传" in payload["utterance"]:
        return {"action": "answer", "reply": "{{upload}}"}
    return {"action": "clarify", "reply": "我暂时无法确认这项要求，您能再说明一下吗？"}


def judge_response(body, reject_candidate):
    system = body["messages"][0]["content"]
    if "same_intent" in system:
        return {"same_intent": True}
    if "仅返回 question" in system:
        return {"question": ALTERNATE}
    if reject_candidate:
        payload = json.loads(body["messages"][1]["content"])
        if MATERIAL["answer"] in payload.get("answer", ""):
            return {"grade": "incorrect"}
    return {"grade": "correct"}


def api_json(client, method, path, **options):
    response = client.request(method, path, **options)
    response.raise_for_status()
    return response.json()


def add_material(client, values=MATERIAL):
    response = client.post("/api/training/materials", json=values)
    assert response.status_code == 201
    return response.json()


def prepare(client, identifier=None):
    if identifier is None:
        return api_json(client, "POST", "/api/rsi/run")
    payload = {"options": {identifier: {"alternate": ALTERNATE, "contrast_case_id": "POP-001"}}}
    return api_json(client, "POST", "/api/rsi/run", json=payload)


def publish(client, revision):
    return api_json(client, "POST", "/api/rsi/publish", json={"revision": revision})


def say(client, session, text):
    return api_json(client, "POST", f"/api/text/{session}/turn", json={"text": text})


def open_call(client, case):
    session = api_json(client, "POST", "/api/text", json={"case_id": case})["session_id"]
    assert say(client, session, "我是负责人")["verified"]
    assert say(client, session, "可以，请说明驳回原因")["intent"] == "offer"
    return session


def active(client):
    return api_json(client, "GET", "/api/rsi")["active"]


def test_shared_cloud_model_uses_separate_outbound_and_judge_requests(training_client):
    client, requests = training_client(shared_model=True)
    with client:
        draft = add_material(client)
        result = prepare(client, draft["id"])
        trial = result["trial"]
        assert trial["judge_model"] == trial["outbound_model"] == "fixture-shared"
        assert result["ready_to_publish"] and all(check["passed"] for check in trial["checks"])
        assert requests["agent"] and requests["judge"]
        assert publish(client, result["id"])["active"] == result["id"]


def test_material_lifecycle_and_version_rollback(training_client):
    configured, requests = training_client()
    with configured as client:
        identifier = add_material(client)["id"]

        def workspace():
            return api_json(client, "GET", "/api/training")

        def lookup():
            return next(row for row in workspace()["materials"] if row["id"] == identifier)

        assert any(row["status"] == "reviewed" for row in workspace()["materials"])
        old_call = open_call(client, "POP-003")
        assert lookup()["status"] == "pending"
        assert not any(row["id"] == f"faq-{identifier}" for row in workspace()["scripts"])

        result = prepare(client, identifier)
        assert result["ready_to_publish"] and result["trial"]["passed"], result["trial"]
        assert not result["activated"] and active(client) == "baseline"
        assert lookup()["status"] == "pending"
        checks = {check["kind"]: check for check in result["trial"]["checks"] if check["material_id"] == identifier}
        assert {"primary", "alternate", "contrast"} <= checks.keys()
        assert all(checks[kind]["passed"] for kind in ("primary", "alternate", "contrast"))
        assert checks["primary"]["query"] == MATERIAL["question"]
        assert checks["alternate"]["query"] == ALTERNATE
        assert checks["contrast"]["case_id"] == "POP-001"
        assert MATERIAL["answer"] in checks["alternate"]["candidate"]["reply"]
        assert MATERIAL["answer"] not in checks["contrast"]["candidate"]["reply"]
        assert requests["agent"] and requests["judge"]

        assert publish(client, result["id"]) == {"active": result["id"], "previous": "baseline"}
        assert lookup()["status"] == "active"
        assert any(row["id"] == f"faq-{identifier}" for row in workspace()["scripts"])
        assert say(client, old_call, MATERIAL["question"])["source"] != MATERIAL["source"]
        assert say(client, open_call(client, "POP-001"), MATERIAL["question"])["source"] != MATERIAL["source"]
        assert say(client, open_call(client, "POP-003"), ALTERNATE)["source"] == MATERIAL["source"]

        record_path = f"/api/training/materials/{identifier}"
        revised = {**MATERIAL, "answer": "建议填写日常有人查收的地址，以京麦系统提示为准。", "source": "运营二次复核纪要"}
        existing_call = open_call(client, "POP-003")
        api_json(client, "PUT", record_path, json={**revised, "edit_version": 1})
        assert lookup()["status"] == "pending"
        revision = prepare(client, identifier)
        assert revision["ready_to_publish"] and revision["trial"]["passed"]
        assert active(client) == result["id"]
        before_edit = say(client, open_call(client, "POP-003"), ALTERNATE)
        assert before_edit["source"] == MATERIAL["source"] and MATERIAL["answer"] in before_edit["text"]
        assert publish(client, revision["id"]) == {"active": revision["id"], "previous": result["id"]}
        assert lookup()["status"] == "active"
        after_edit = say(client, open_call(client, "POP-003"), ALTERNATE)
        assert after_edit["source"] == revised["source"] and revised["answer"] in after_edit["text"]
        assert say(client, existing_call, ALTERNATE)["source"] == MATERIAL["source"]

        updated = client.put(record_path, json={**revised, "enabled": False, "edit_version": 2})
        assert updated.status_code == 200 and lookup()["status"] == "pending_pause"
        assert client.put(record_path, json={**revised, "edit_version": 2}).status_code == 409
        removal = prepare(client, identifier)
        assert removal["ready_to_publish"] and not removal["activated"], removal
        assert lookup()["status"] == "pending_pause" and active(client) == revision["id"]
        publish(client, removal["id"])
        assert lookup()["status"] == "paused"
        assert say(client, open_call(client, "POP-003"), MATERIAL["question"])["source"] != revised["source"]
        api_json(client, "POST", "/api/rsi/rollback", json={"revision": revision["id"]})
        assert lookup()["status"] == "pending_pause"
        assert say(client, open_call(client, "POP-003"), MATERIAL["question"])["source"] == revised["source"]
        api_json(client, "POST", "/api/rsi/rollback", json={"revision": "baseline"})
        assert lookup()["status"] == "paused"


def test_material_that_changes_a_call_action_is_rejected(training_client):
    configured, _ = training_client()
    with configured as client:
        draft = add_material(client, {"question": "请帮我转人工客服。", "answer": "请先打开原来的审核单。",
                                      "source": "流程复核记录"})
        result = prepare(client)
        assert not result["ready_to_publish"] and result["materials"]["issues"][0]["id"] == draft["id"]
        assert active(client) == "baseline"
        trace = api_json(client, "GET", f"/api/rsi/{result['id']}/trace")
        assert trace["material_checks"][0]["observed"] == "handoff"
        assert client.post("/api/rsi/publish", json={"revision": result["id"]}).status_code == 409


def test_candidate_becomes_stale_when_drafts_providers_or_base_change(training_client):
    configured, _ = training_client()
    with configured as client:
        identifier = add_material(client)["id"]
        original = prepare(client, identifier)
        assert original["ready_to_publish"]
        edited = {**MATERIAL, "answer": "建议填写日常有人查收的地址，以京麦系统提示为准。", "edit_version": 1}
        api_json(client, "PUT", f"/api/training/materials/{identifier}", json=edited)
        assert client.post("/api/rsi/publish", json={"revision": original["id"]}).status_code == 409
        history = api_json(client, "GET", "/api/rsi")["history"]
        stale = next(run for run in history if run["id"] == original["id"])
        assert stale["stale"] and not stale["ready_to_publish"]

        before_profile_change = prepare(client, identifier)
        assert before_profile_change["ready_to_publish"]
        route = "/api/ai-settings/outbound"
        profile = api_json(client, "GET", route)
        update = {key: profile[key] for key in ("url", "version", "enabled")}
        api_json(client, "PUT", route, json={**update, "model": "fixture-agent-v2"})
        assert client.post("/api/rsi/publish", json={"revision": before_profile_change["id"]}).status_code == 409

        first = prepare(client, identifier)
        second = prepare(client, identifier)
        assert first["ready_to_publish"] and second["ready_to_publish"] and active(client) == "baseline"
        assert client.post("/api/rsi/publish", json={"revision": first["id"]}).status_code == 409
        released = publish(client, second["id"])
        assert publish(client, second["id"]) == released
        assert active(client) == second["id"]

        api_json(client, "PUT", f"/api/training/materials/{identifier}", json={**edited, "enabled": False, "edit_version": 2})
        removal = prepare(client, identifier)
        assert removal["ready_to_publish"]
        unreleased = client.post("/api/rsi/rollback", json={"revision": removal["id"]})
        assert unreleased.status_code == 409
        api_json(client, "POST", "/api/rsi/rollback", json={"revision": "baseline"})
        assert client.post("/api/rsi/publish", json={"revision": removal["id"]}).status_code == 409
        assert active(client) == "baseline"


@pytest.mark.parametrize("role", ["agent", "judge"])
def test_trial_provider_failure_cannot_publish_or_expose_keys(training_client, role):
    configured, requests = training_client(fail_role=role)
    with configured as client:
        identifier = add_material(client)["id"]
        result = prepare(client, identifier)
        assert requests[role], f"trial did not attempt to contact {role}"
        assert not result["trial"]["passed"] and not result["ready_to_publish"]
        if role == "agent":
            primary = next(check for check in result["trial"]["checks"] if check["kind"] == "primary")
            assert primary["candidate"]["status"] != "used" and not primary["passed"]
        else:
            alternate = next(check for check in result["trial"]["checks"] if check["kind"] == "alternate")
            assert not alternate["passed"]
        assert active(client) == "baseline"
        denied = client.post("/api/rsi/publish", json={"revision": result["id"]})
        assert denied.status_code == 409
        trace = client.get(f"/api/rsi/{result['id']}/trace")
        assert trace.is_success
        rendered = json.dumps(result) + trace.text + denied.text
        assert AGENT_TOKEN not in rendered and JUDGE_TOKEN not in rendered


@pytest.mark.parametrize("missing", ["agent", "judge"])
def test_missing_provider_cannot_substitute_offline_trial(training_client, missing):
    configured, requests = training_client(enable_agent=missing != "agent", enable_judge=missing != "judge")
    with configured as client:
        identifier = add_material(client)["id"]
        result = prepare(client, identifier)
        assert not requests[missing]
        assert not result["trial"]["passed"] and not result["ready_to_publish"]
        assert result["trial"]["reason"]
        assert active(client) == "baseline"


def test_independent_judge_can_reject_a_reply_that_cites_the_draft(training_client):
    configured, _ = training_client(reject_candidate=True)
    with configured as client:
        identifier = add_material(client)["id"]
        result = prepare(client, identifier)
        primary = next(check for check in result["trial"]["checks"] if check["kind"] == "primary")
        assert primary["candidate"]["status"] == "used"
        assert identifier in primary["candidate"]["references"]
        assert primary["baseline"]["grade"] == "correct"
        assert primary["candidate"]["grade"] == "incorrect" and not primary["passed"] and primary["reason"]
        assert not result["ready_to_publish"] and active(client) == "baseline"
        assert client.post("/api/rsi/publish", json={"revision": result["id"]}).status_code == 409

        visible = next(run for run in api_json(client, "GET", "/api/rsi")["history"] if run["id"] == result["id"])
        assert not visible["ready_to_publish"] and not visible["trial"]["passed"]
        assert primary in visible["trial"]["checks"]
