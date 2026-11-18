import json

import httpx
import pytest
from fastapi.testclient import TestClient

from zhiyu import app
from zhiyu.agent import DialogueAgent


@pytest.fixture
def agent_client(monkeypatch, tmp_path):
    def setup(handler):
        monkeypatch.setenv("ZHIYU_AGENT_URL", "https://dialogue.example/v1")
        monkeypatch.setenv("ZHIYU_AGENT_MODEL", "local-agent-test")
        monkeypatch.setenv("ZHIYU_AGENT_TOKEN", "fixture-token")
        monkeypatch.setattr(app, "RUNTIME", tmp_path)
        monkeypatch.setattr(app, "missing_models", lambda: ["speech-fixture"])

        def provider(request):
            body = json.loads(request.content)
            assert request.url.path == "/v1/chat/completions"
            assert request.headers["Authorization"] == "Bearer fixture-token"
            assert body["response_format"] == {"type": "json_object"}
            result = handler(json.loads(body["messages"][1]["content"]))
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(result)}}]})

        monkeypatch.setattr(app, "DialogueAgent", lambda: DialogueAgent(transport=httpx.MockTransport(provider)))
        return TestClient(app.api)
    return setup


def start(client, case_id="POP-001"):
    response = client.post("/api/text", json={"case_id": case_id})
    response.raise_for_status()
    return response.json()["session_id"]


def say(client, call, text):
    response = client.post(f"/api/text/{call}/turn", json={"text": text})
    response.raise_for_status()
    return response.json()


def test_agent_uses_stage_history_and_scoped_facts(agent_client):
    responses = iter([
        ("identity", "verify_again", "您担心电话来源，可以在京麦核实。我想先确认您负责这家店吗？"),
        ("identity", "verify", "谢谢。您这会儿能听一下资质审核的通知吗？"),
        ("needs", "explain", "我给您捋一下，{{case_reason}}。下一步可以{{case_action}}。这里有疑问吗？"),
        ("confirm", "answer", "先解决您说的附件问题。{{upload}}至于最后的结果，{{approval}}"),
        ("confirm", "clarify", "在外地操作确实不方便。我这边无法核实平板是否支持上传，您目前能打开那条单据吗？"),
        ("confirm", "answer", "您刚说的是原来那条驳回单。{{upload}}"),
        ("confirm", "confirm", "那您就有方向了，已登记这次沟通。谢谢您。"),
    ])

    def provider(payload):
        stage, action, reply = next(responses)
        assert payload["stage"] == stage and action in payload["actions"]
        facts = {item["slot"] for item in payload["facts"]}
        if stage == "identity":
            assert not facts and "POP-001" not in str(payload) and "星河商贸" not in str(payload)
        else:
            assert {"{{case_action}}", "{{case_reason}}", "{{upload}}"} <= facts
            assert "{{shop_name}}" not in facts
            assert all(set(entry) == {"slot", "topic"} for entry in payload["facts"])
            assert "授权书对应的位置" not in str(payload) and "缺少有效的品牌授权书" not in str(payload)
        if "原来那个" in payload["utterance"]:
            assert any("平板" in entry["text"] for entry in payload["history"])
        return {"action": action, "reply": reply}

    with agent_client(provider) as client:
        call = start(client)
        opening = say(client, call, "星河商贸 POP-001，你们这电话真的假的？")
        assert opening["intent"] == "verify_again" and "京麦核实" in opening["text"] and not opening["fact"]
        assert say(client, call, "我是店主，申请这一块一直是我在打理。")["verified"]
        explanation = say(client, call, "我在听，您接着讲就成。")
        assert explanation["intent"] == "offer" and explanation["answer_model"] == "local-agent-test"
        assert "捋一下" in explanation["text"] and "场景汇总!18" in explanation["source"]
        combined = say(client, call, "懂了，不过扫描件往什么格子塞，交上就稳了吗？")
        assert combined["intent"] == "faq:upload" and combined["state"] == "confirm" and not combined["receipt"]
        assert "高频QA!2" in combined["source"] and "高频QA!12" in combined["source"]
        assert "平板" in say(client, call, "我在外地，平板上也能办吧？")["text"]
        assert "授权书对应的位置" in say(client, call, "那还是原来那个入口？")["text"]
        final = say(client, call, "这回有方向了，我会照着操作。")
        assert final["disposition"] == "notified" and final["receipt"]
        assert client.get(f"/api/calls/{call}/quality").json()["complete"]


def test_agent_cannot_verify_without_attestation_or_invent_a_reference(agent_client):
    results = iter([
        {"action": "verify", "reply": "谢谢确认，您现在方便听一下吗？"},
        {"action": "verify", "reply": "我先确认，稍后说明。"},
        {"action": "explain", "reply": "原因是{{case_reason}}，请您{{case_action}}。"},
        {"action": "answer", "reply": "{{shop_name}}"},
        {"action": "answer", "reply": "保证通过。{{approval}}"},
        {"action": "confirm", "reply": "已登记。"},
    ])
    with agent_client(lambda payload: next(results)) as client:
        call = start(client)
        assert say(client, call, "我是店主的邻居，代运营同事还没来")["verified"] is False
        assert say(client, call, "我是负责人")["verified"] is True
        say(client, call, "您继续")
        for question in ("授权书在哪里上传？", "补齐后就能通过吗？", "好，但请问多久会审核？"):
            reply = say(client, call, question)
            assert reply["state"] == "confirm" and not reply["receipt"] and "answer_model" not in reply
        trace = client.get(f"/api/calls/{call}/trace").json()["events"]
        assert [row["data"]["status"] for row in trace if row["type"] == "dialogue_agent"] == [
            "unconfirmed_identity", "used", "used", "unsupported_reference", "unsafe_reply", "unanswered_question",
        ]
