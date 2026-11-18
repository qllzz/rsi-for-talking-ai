import sqlite3
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from zhiyu.knowledge import Knowledge
from zhiyu.storage import MockTools, Store


@pytest.fixture()
def workflow(tmp_path):
    knowledge = Knowledge.load()
    store = Store(tmp_path / "workflow.db")
    tools = MockTools(store, knowledge)
    case_id = "POP-001"

    def begin(ticket=None):
        session_id = uuid4().hex
        store.begin(session_id, case_id, "test", knowledge.revision, followup_ticket=ticket)
        return session_id

    def write(session_id, disposition):
        notified = disposition == "notified"
        return tools.write_result({
            "session_id": session_id, "case_id": case_id, "channel": "phone",
            "disposition": disposition, "identity_verified": notified, "acknowledged": notified,
            "turn_count": 3 if notified else 0, "revision": knowledge.revision,
        })

    return store, tools, begin, write


@pytest.mark.parametrize("disposition", ["notified", "callback"])
def test_artifacts_and_result_commit_together(workflow, disposition):
    store, tools, begin, write = workflow
    session = begin()
    with store.connect() as db:
        db.execute("""CREATE TRIGGER fail_writeback BEFORE INSERT ON results
                      BEGIN SELECT RAISE(ABORT, 'simulated failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="simulated failure"):
        write(session, disposition)

    record = store.detail(session)
    assert not record["receipt"] and not record["ticket_id"] and not record["written"]
    assert store.work_items() == {"notices": [], "tickets": []}

    with store.connect() as db:
        db.execute("DROP TRIGGER fail_writeback")
    receipt = write(session, disposition)
    assert write(session, disposition) == receipt
    record = store.detail(session)
    assert record["receipt"] == receipt
    artifacts = tools.result_artifacts(session)
    if disposition == "notified":
        notice = record["written"]
        assert notice["id"] == artifacts["written"] and notice["id"].startswith("JM-")
        assert all(notice[field] for field in ("reason", "action", "source"))
        assert not record["ticket_id"]
    else:
        assert record["ticket_id"] == artifacts["ticket"] and artifacts["ticket"].startswith("WK-")
        assert not record["written"]


def test_callback_claim_is_exclusive_and_can_be_retried(workflow):
    store, tools, begin, write = workflow
    original = begin()
    write(original, "callback")
    ticket = tools.result_artifacts(original)["ticket"]

    with pytest.raises(ValueError, match="该单据"):
        store.begin(uuid4().hex, "POP-002", "test", "baseline", followup_ticket=ticket)

    def claim(_):
        try:
            return begin(ticket)
        except ValueError as exc:
            assert "正在处理" in str(exc)
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        actual = [session for session in pool.map(claim, range(4)) if session]
    assert len(actual) == 1
    first = actual[0]
    assert store.work_items()["tickets"][0]["status"] == "in_progress"
    assert store.detail(first)["followup_ticket"] == ticket

    write(first, "ended")
    assert store.work_items()["tickets"][0]["status"] == "pending"
    second = begin(ticket)
    store.release_followup(second)
    third = begin(ticket)
    with pytest.raises(ValueError, match="已释放"):
        write(second, "notified")

    write(third, "jingmai")
    assert tools.result_artifacts(third)["written"].startswith("JM-")
    assert store.work_items()["tickets"] == []
    with pytest.raises(ValueError, match="已经处理"):
        begin(ticket)


def test_expired_callback_claim_cannot_write_a_result(workflow):
    store, tools, begin, write = workflow
    original = begin()
    write(original, "callback")
    ticket = tools.result_artifacts(original)["ticket"]
    expired = begin(ticket)
    with store.connect() as db:
        db.execute("UPDATE followup_attempts SET claimed=0 WHERE session_id=?", (expired,))

    assert store.work_items()["tickets"][0]["status"] == "pending"
    with pytest.raises(ValueError, match="已释放"):
        write(expired, "jingmai")
    assert store.detail(expired)["receipt"] is None
    assert store.detail(begin(ticket))["followup_ticket"] == ticket


def test_handoff_acknowledgement_stays_in_audit_history(workflow):
    store, tools, begin, write = workflow
    callback = begin()
    write(callback, "callback")
    with pytest.raises(ValueError, match="人工复核"):
        store.resolve_handoff(tools.result_artifacts(callback)["ticket"])

    session = begin()
    write(session, "handoff")
    ticket = tools.result_artifacts(session)["ticket"]
    assert store.detail(session)["handoff_status"] == "pending"
    resolved = store.resolve_handoff(ticket)
    assert resolved["status"] == "resolved" and store.resolve_handoff(ticket) == resolved
    assert ticket not in {item["id"] for item in store.work_items()["tickets"]}
    assert store.detail(session)["handoff_status"] == "resolved"
