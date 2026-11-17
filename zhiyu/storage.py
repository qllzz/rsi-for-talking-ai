import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CallResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=8, max_length=64)
    case_id: str = Field(min_length=1, max_length=48)
    channel: Literal["phone", "jingmai"]
    disposition: Literal["notified", "wrong_party", "callback", "jingmai", "handoff", "ended"]
    identity_verified: bool
    acknowledged: bool
    turn_count: int = Field(ge=0)
    revision: str


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, case_id TEXT NOT NULL, mode TEXT NOT NULL,
                    revision TEXT NOT NULL, started REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, type TEXT NOT NULL,
                    created REAL NOT NULL, data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_call ON events(session_id, id);
                CREATE TABLE IF NOT EXISTS results (
                    session_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                    receipt TEXT NOT NULL UNIQUE, created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tickets (
                    session_id TEXT PRIMARY KEY, case_id TEXT NOT NULL,
                    kind TEXT NOT NULL, ticket_id TEXT NOT NULL UNIQUE, created REAL NOT NULL
                );
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=8)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def begin(self, session_id, case_id, mode, revision):
        with self.connect() as db:
            db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
                       (session_id, case_id, mode, revision, time.time()))

    def log(self, session_id, kind, **data):
        with self.connect() as db:
            db.execute("INSERT INTO events(session_id, type, created, data) VALUES (?, ?, ?, ?)",
                       (session_id, kind, time.time(), json.dumps(data, ensure_ascii=False)))

    def save_result(self, result: CallResult):
        payload = result.model_dump_json()
        receipt = "RC-" + result.session_id[:12].upper()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT payload, receipt FROM results WHERE session_id=?",
                                  (result.session_id,)).fetchone()
            if existing:
                if existing["payload"] != payload:
                    raise ValueError("幂等键已存在但请求不同")
                return existing["receipt"]
            db.execute("INSERT INTO results VALUES (?, ?, ?, ?)",
                       (result.session_id, payload, receipt, time.time()))
        return receipt

    def create_ticket(self, session_id, case_id, kind):
        ticket_id = "WK-" + session_id[:12].upper()
        with self.connect() as db:
            db.execute("INSERT INTO tickets VALUES (?, ?, ?, ?, ?) ON CONFLICT(session_id) DO NOTHING",
                       (session_id, case_id, kind, ticket_id, time.time()))
            row = db.execute("SELECT * FROM tickets WHERE session_id=?", (session_id,)).fetchone()
            if row["case_id"] != case_id or row["kind"] != kind:
                raise ValueError("工单幂等键冲突")
        return ticket_id

    def records(self, limit=60):
        with self.connect() as db:
            rows = db.execute("""SELECT s.*, r.receipt, r.payload FROM sessions s
                                 LEFT JOIN results r ON s.id=r.session_id ORDER BY s.started DESC LIMIT ?""",
                              (limit,)).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"]) if row["payload"] else None}
                for row in rows]

    def detail(self, session_id):
        with self.connect() as db:
            session = db.execute("""SELECT s.*, r.receipt, r.payload, t.ticket_id FROM sessions s
                                 LEFT JOIN results r ON s.id=r.session_id
                                 LEFT JOIN tickets t ON s.id=t.session_id WHERE s.id=?""",
                                 (session_id,)).fetchone()
            if not session:
                return None
            rows = db.execute("SELECT * FROM events WHERE session_id=? ORDER BY id",
                              (session_id,)).fetchall()
        return {**dict(session), "payload": json.loads(session["payload"]) if session["payload"] else None,
                "events": [{**dict(row), "data": json.loads(row["data"])} for row in rows]}


class MockTools:
    """业务工具网关。所有调用均在服务端由状态机发起。"""
    def __init__(self, store, knowledge):
        self.store = store
        self.knowledge = knowledge

    def lookup_list(self, case_id):
        case = self.knowledge.cases.get(case_id)
        if not case:
            raise KeyError(case_id)
        return {"case_id": case.id, "route": case.channel(), "attempt": case.attempt}

    def lookup_application(self, case_id, verified):
        if not verified:
            raise PermissionError("需先验证身份")
        return self.knowledge.cases[case_id]

    def write_result(self, data):
        result = CallResult.model_validate(data)
        if result.case_id not in self.knowledge.cases:
            raise ValueError("找不到模拟单据")
        if result.disposition == "notified" and not (result.identity_verified and result.acknowledged):
            raise ValueError("通知完成必须验证身份并确认结果")
        return self.store.save_result(result)

    def create_ticket(self, session_id, case_id, kind):
        if kind not in {"callback", "handoff"} or case_id not in self.knowledge.cases:
            raise ValueError("工单类型或单据无效")
        return self.store.create_ticket(session_id, case_id, kind)
