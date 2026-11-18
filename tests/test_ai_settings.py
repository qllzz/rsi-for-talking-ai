import json
import stat

import httpx
from fastapi.testclient import TestClient

from zhiyu import app, settings
from zhiyu.agent import DialogueAgent
from zhiyu.connections import load_connection, probe_connection
from zhiyu.llm import LanguageModel


def test_ai_profiles_keep_credentials_private_and_apply_to_new_work(monkeypatch, tmp_path):
    origin = "http://127.0.0.1:8765"
    filename = tmp_path / "outbound.json"
    judge_file = tmp_path / "judge.json"
    filename.write_text(json.dumps({"url": "https://outbound.example/v1", "model": "first", "token": "key-original"}))
    monkeypatch.setenv("ZHIYU_AGENT_CONFIG", str(filename))
    monkeypatch.setenv("ZHIYU_JUDGE_CONFIG", str(judge_file))
    for name in ("ZHIYU_AGENT_URL", "ZHIYU_JUDGE_URL", "ZHIYU_LLM_URL", "ZHIYU_ANSWER_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(app, "RUNTIME", tmp_path / "runtime")
    monkeypatch.setattr(app, "configured_catalog", lambda: None)
    monkeypatch.setattr(app, "missing_models", lambda: ["speech-fixture"])
    observed = []

    def provider(request):
        body = json.loads(request.content)
        assert request.url.path == "/v1/chat/completions"
        model = body["model"]
        key = request.headers.get("authorization")
        if model == "judge-model":
            assert key == "Bearer key-judge" and request.url.host == "judge.example"
        else:
            assert key == "Bearer key-original" and request.url.host == "outbound.example"
        assert body["response_format"] == {"type": "json_object"}
        data = body["messages"][1]["content"]
        observed.append((model, data))
        if model == "rejected":
            return httpx.Response(401, text="internal response containing key-original")
        if '请返回 {"ready":true}。' == data:
            output = {"ready": True}
        elif model == "judge-model":
            output = {"grade": "correct"}
        else:
            output = {"action": "verify", "reply": "多谢确认，现在方便听一下吗？"}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(output)}}]})

    def agent(**options):
        return DialogueAgent(transport=httpx.MockTransport(provider), **options)

    monkeypatch.setattr(app, "DialogueAgent", agent)
    monkeypatch.setattr(settings, "DialogueAgent", agent)
    monkeypatch.setattr(settings, "LanguageModel", lambda **options: LanguageModel(transport=httpx.MockTransport(provider), **options))
    monkeypatch.setattr(settings, "probe_connection", lambda config: probe_connection(config, transport=httpx.MockTransport(provider)))

    with TestClient(app.api, base_url=origin, headers={"Origin": origin}, client=("127.0.0.1", 50000)) as client:
        route = "/api/ai-settings/outbound"
        visible = client.get(route)
        initial = visible.json()
        assert initial["has_token"] and "key-original" not in visible.text and visible.headers["cache-control"] == "no-store"
        assert client.get(route, headers={"Host": "untrusted.example"}).status_code == 403
        edit = {key: initial[key] for key in ("url", "model", "version", "enabled")}
        assert client.put(route, json=edit, headers={"Origin": "https://untrusted.example"}).status_code == 403
        invalid = client.post(route + "/test", json={**edit, "version": "bad", "token": "secret-invalid"})
        assert invalid.status_code == 422 and "secret-invalid" not in invalid.text

        before = client.post("/api/text", json={"case_id": "POP-001"}).json()["session_id"]
        assert client.post(route + "/test", json={**edit, "url": "https://different.example/v1"}).status_code == 422
        assert observed == []
        rejected = client.put(route, json={**edit, "model": "rejected"})
        assert rejected.status_code == 422 and "key-original" not in rejected.text
        assert load_connection("outbound").model == "first" and client.get(route).json() == initial
        tested = client.post(route + "/test", json={**edit, "model": "second"})
        assert tested.is_success and "latency_ms" in tested.json() and client.get(route).json() == initial
        saved = client.put(route, json={**edit, "model": "second"})
        assert saved.is_success and saved.json()["has_token"] and "key-original" not in saved.text
        assert load_connection("outbound").model == "second"
        assert stat.S_IMODE(filename.stat().st_mode) == 0o600
        assert client.put(route, json=edit).status_code == 409

        after = client.post("/api/text", json={"case_id": "POP-001"}).json()["session_id"]
        for call, model in ((before, "first"), (after, "second")):
            response = client.post(f"/api/text/{call}/turn", json={"text": "我是负责人"})
            assert response.json()["answer_model"] == model

        judge_route = "/api/ai-settings/judge"
        judge = client.get(judge_route).json()
        assert not judge["enabled"]
        response = client.put(judge_route, json={"url": "https://judge.example/v1/chat/completions", "model": "judge-model",
                                                "token": "key-judge", "version": judge["version"], "enabled": True})
        assert response.is_success and response.json()["url"] == "https://judge.example/v1"
        status = client.get("/api/status").json()
        assert status["dialogue_model"] == "second" and status["model"] == "judge-model"
        result = client.post("/api/rsi/run", json={}).json()
        assert result["judge_model"] == "judge-model" and result["summary"]["holdout_after"]["judge_checks"]["correct"] > 0
        state = client.get(judge_route).json()
        edit = {key: state[key] for key in ("url", "model", "version")}
        assert client.put(judge_route, json={**edit, "enabled": False, "token": ""}).is_success
        assert not client.get(judge_route).json()["has_token"] and not client.get("/api/status").json()["llm"]
