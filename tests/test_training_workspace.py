from fastapi.testclient import TestClient

from zhiyu import app


def test_material_lifecycle_and_version_rollback(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "RUNTIME", tmp_path)
    monkeypatch.setattr(app, "configured_catalog", lambda: None)
    monkeypatch.setattr(app, "missing_models", lambda: ["speech-fixture"])
    monkeypatch.setenv("ZHIYU_AGENT_URL", "")
    monkeypatch.delenv("ZHIYU_ANSWER_URL", raising=False)
    payload = {"question": "店铺联系人可以用公共邮箱吗？", "answer": "请使用能接收业务通知的邮箱，以京麦单据要求为准。",
               "source": "运营复核纪要", "scope": "shop", "aliases": ["店铺公共联络邮箱"], "enabled": True}

    with TestClient(app.api) as client:
        def workspace():
            return client.get("/api/training").json()

        def lookup():
            return next(row for row in workspace()["materials"] if row["id"] == identifier)

        def start(case):
            session = client.post("/api/text", json={"case_id": case}).json()["session_id"]
            say(session, "我是负责人")
            say(session, "可以，请说明驳回原因")
            return session

        def say(session, text):
            return client.post(f"/api/text/{session}/turn", json={"text": text}).json()

        assert any(row["status"] == "reviewed" for row in workspace()["materials"])
        response = client.post("/api/training/materials", json=payload)
        assert response.status_code == 201
        identifier = response.json()["id"]
        old_call = start("POP-003")
        assert lookup()["status"] == "pending"
        assert not any(row["id"] == f"faq-{identifier}" for row in workspace()["scripts"])

        result = client.post("/api/rsi/run", json={}).json()
        assert result["activated"] and result["materials"]["passed"] == 2
        assert lookup()["status"] == "active"
        assert any(row["id"] == f"faq-{identifier}" for row in workspace()["scripts"])
        assert say(old_call, payload["question"])["source"] != payload["source"]
        assert say(start("POP-001"), payload["question"])["source"] != payload["source"]
        assert say(start("POP-003"), payload["question"])["source"] == payload["source"]

        record_path = f"/api/training/materials/{identifier}"
        updated = client.put(record_path, json={**payload, "enabled": False, "edit_version": 1})
        assert updated.status_code == 200 and lookup()["status"] == "pending_pause"
        assert client.put(record_path, json={**payload, "edit_version": 1}).status_code == 409
        assert client.post("/api/rsi/run", json={}).json()["activated"]
        assert lookup()["status"] == "paused"
        assert client.post("/api/rsi/rollback", json={"revision": result["id"]}).is_success
        assert lookup()["status"] == "pending_pause"
        assert say(start("POP-003"), payload["question"])["source"] == payload["source"]
        assert client.post("/api/rsi/rollback", json={"revision": "baseline"}).is_success
        assert lookup()["status"] == "paused"


def test_material_that_changes_a_call_action_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "RUNTIME", tmp_path)
    monkeypatch.setattr(app, "configured_catalog", lambda: None)
    monkeypatch.setattr(app, "missing_models", lambda: ["speech-fixture"])
    monkeypatch.setenv("ZHIYU_AGENT_URL", "")
    with TestClient(app.api) as client:
        draft = client.post("/api/training/materials", json={"question": "请帮我转人工客服。", "answer": "请先打开原来的审核单。",
                                                                  "source": "流程复核记录"}).json()
        result = client.post("/api/rsi/run", json={}).json()
        assert not result["activated"] and result["materials"]["issues"][0]["id"] == draft["id"]
        assert client.get("/api/rsi").json()["active"] == "baseline"
        trace = client.get(f"/api/rsi/{result['id']}/trace").json()
        assert trace["material_checks"][0]["observed"] == "handoff"
