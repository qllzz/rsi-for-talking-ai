import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


FOLLOWUP_LEASE_SECONDS = 300


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
                CREATE TABLE IF NOT EXISTS written_notices (
                    session_id TEXT PRIMARY KEY, case_id TEXT NOT NULL,
                    notice_id TEXT NOT NULL UNIQUE, reason TEXT NOT NULL,
                    action TEXT NOT NULL, source TEXT NOT NULL, created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS followup_attempts (
                    session_id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL,
                    claimed REAL NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('claimed', 'released', 'consumed'))
                );
                CREATE UNIQUE INDEX IF NOT EXISTS followup_current
                ON followup_attempts(ticket_id) WHERE status IN ('claimed', 'consumed');
                CREATE TABLE IF NOT EXISTS callback_schedules (
                    ticket_id TEXT PRIMARY KEY, scheduled_at INTEGER, version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS resolved_handoffs (
                    ticket_id TEXT PRIMARY KEY, resolved REAL NOT NULL
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

    def begin(self, session_id, case_id, mode, revision, followup_ticket=None):
        with self.connect() as db:
            if followup_ticket is not None:
                db.execute("BEGIN IMMEDIATE")
            now = time.time()
            if followup_ticket is not None:
                self._claim_followup(db, session_id, case_id, followup_ticket, now)
            db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
                       (session_id, case_id, mode, revision, now))

    @staticmethod
    def _claim_followup(db, session_id, case_id, ticket_id, now):
        ticket = db.execute("""
            SELECT t.case_id, t.kind FROM tickets t
            JOIN results r ON r.session_id=t.session_id WHERE t.ticket_id=?
        """, (ticket_id,)).fetchone()
        if not ticket or ticket["case_id"] != case_id or ticket["kind"] != "callback":
            raise ValueError("找不到该单据的待回联工单")
        db.execute("""
            UPDATE followup_attempts SET status='released'
            WHERE ticket_id=? AND status='claimed' AND claimed<=?
        """, (ticket_id, now - FOLLOWUP_LEASE_SECONDS))
        attempt = db.execute("""
            SELECT status FROM followup_attempts
            WHERE ticket_id=? AND status IN ('claimed', 'consumed')
        """, (ticket_id,)).fetchone()
        if attempt:
            if attempt["status"] == "consumed":
                raise ValueError("此回联工单已经处理")
            raise ValueError("此回联工单正在处理")
        schedule = db.execute('SELECT scheduled_at FROM callback_schedules WHERE ticket_id=?', (ticket_id,)).fetchone()
        if schedule and schedule['scheduled_at'] is not None and schedule['scheduled_at'] > now:
            raise ValueError('尚未到预约回电时间，请到期后再领取')
        db.execute("INSERT INTO followup_attempts VALUES (?, ?, ?, 'claimed')", (session_id, ticket_id, now))

    def release_followup(self, session_id):
        with self.connect() as db:
            db.execute("""
                UPDATE followup_attempts SET status='released' WHERE session_id=? AND status='claimed'
            """, (session_id,))

    def log(self, session_id, kind, **data):
        with self.connect() as db:
            db.execute("INSERT INTO events(session_id, type, created, data) VALUES (?, ?, ?, ?)",
                       (session_id, kind, time.time(), json.dumps(data, ensure_ascii=False)))

    def save_result(self, result: CallResult, *, notice=None, ticket_kind=None):
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
            now = time.time()
            attempt = db.execute("SELECT status, claimed FROM followup_attempts WHERE session_id=?",
                                 (result.session_id,)).fetchone()
            if attempt:
                valid_claim = attempt["status"] == "claimed" and attempt["claimed"] > now - FOLLOWUP_LEASE_SECONDS
                if result.disposition != "ended" and not valid_claim:
                    raise ValueError("回联工单已释放，请重新领取")
                status = "released" if result.disposition == "ended" else "consumed"
                db.execute("UPDATE followup_attempts SET status=? WHERE session_id=?",
                           (status, result.session_id))
            if notice is not None:
                reason, action, source = notice
                notice_id = "JM-" + result.session_id[:12].upper()
                db.execute("INSERT INTO written_notices VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (result.session_id, result.case_id, notice_id, reason, action, source, now))
            if ticket_kind is not None:
                self._create_ticket(db, result.session_id, result.case_id, ticket_kind, now)
            db.execute("INSERT INTO results VALUES (?, ?, ?, ?)",
                       (result.session_id, payload, receipt, now))
        return receipt

    @staticmethod
    def _create_ticket(db, session_id, case_id, kind, now):
        ticket_id = "WK-" + session_id[:12].upper()
        db.execute("INSERT INTO tickets VALUES (?, ?, ?, ?, ?) ON CONFLICT(session_id) DO NOTHING",
                   (session_id, case_id, kind, ticket_id, now))
        row = db.execute("SELECT case_id, kind, ticket_id FROM tickets WHERE session_id=?",
                         (session_id,)).fetchone()
        if row["case_id"] != case_id or row["kind"] != kind:
            raise ValueError("工单幂等键冲突")
        return row["ticket_id"]

    def create_ticket(self, session_id, case_id, kind):
        with self.connect() as db:
            return self._create_ticket(db, session_id, case_id, kind, time.time())

    def result_artifacts(self, session_id):
        with self.connect() as db:
            row = db.execute("""
                SELECT t.ticket_id AS ticket, n.notice_id AS written FROM results r
                LEFT JOIN tickets t ON t.session_id=r.session_id
                LEFT JOIN written_notices n ON n.session_id=r.session_id
                WHERE r.session_id=?
            """, (session_id,)).fetchone()
        return dict(row) if row else {"ticket": None, "written": None}

    def records(self, limit=60):
        with self.connect() as db:
            rows = db.execute("""
                SELECT s.*, r.receipt, r.payload, t.ticket_id, n.notice_id AS written_id FROM sessions s
                LEFT JOIN results r ON s.id=r.session_id
                LEFT JOIN tickets t ON t.session_id=r.session_id
                LEFT JOIN written_notices n ON n.session_id=r.session_id
                ORDER BY s.started DESC LIMIT ?
            """, (limit,)).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"]) if row["payload"] else None}
                for row in rows]

    def latest_by_case(self):
        with self.connect() as db:
            rows = db.execute("""
                WITH ranked AS (
                    SELECT s.case_id, r.payload, r.receipt, t.ticket_id, n.notice_id AS written_id,
                           ROW_NUMBER() OVER (PARTITION BY s.case_id ORDER BY r.created DESC, s.id DESC) AS rank
                    FROM results r JOIN sessions s ON s.id=r.session_id
                    LEFT JOIN tickets t ON t.session_id=r.session_id
                    LEFT JOIN written_notices n ON n.session_id=r.session_id
                )
                SELECT case_id, payload, receipt, ticket_id, written_id FROM ranked WHERE rank=1
            """).fetchall()
        return {row["case_id"]: {
            "disposition": json.loads(row["payload"])["disposition"], "receipt": row["receipt"],
            "written_id": row["written_id"], "ticket_id": row["ticket_id"],
        } for row in rows}

    def detail(self, session_id):
        with self.connect() as db:
            session = db.execute("""
                SELECT s.*, r.receipt, r.payload, t.ticket_id, f.ticket_id AS followup_ticket,
                       CASE WHEN t.kind='handoff' AND h.ticket_id IS NOT NULL THEN 'resolved'
                            WHEN t.kind='handoff' THEN 'pending' END AS handoff_status
                FROM sessions s
                LEFT JOIN results r ON s.id=r.session_id
                LEFT JOIN tickets t ON s.id=t.session_id
                LEFT JOIN resolved_handoffs h ON h.ticket_id=t.ticket_id
                LEFT JOIN followup_attempts f ON f.session_id=s.id WHERE s.id=?
            """, (session_id,)).fetchone()
            if not session:
                return None
            rows = db.execute("SELECT * FROM events WHERE session_id=? ORDER BY id",
                              (session_id,)).fetchall()
            notice = db.execute("""
                SELECT notice_id AS id, case_id, reason, action, source, created
                FROM written_notices WHERE session_id=?
            """, (session_id,)).fetchone()
        return {**dict(session), "payload": json.loads(session["payload"]) if session["payload"] else None,
                "written": dict(notice) if notice else None,
                "events": [{**dict(row), "data": json.loads(row["data"])} for row in rows]}

    def work_items(self, limit=60):
        with self.connect() as db:
            notices = db.execute("""
                SELECT notice_id AS id, session_id, case_id, source, created
                FROM written_notices ORDER BY created DESC LIMIT ?
            """, (limit,)).fetchall()
            tickets = db.execute("""
                SELECT t.ticket_id AS id, t.session_id, t.case_id, t.kind, t.created,
                       CASE WHEN f.status='claimed' AND f.claimed>?
                            THEN 'in_progress' ELSE 'pending' END AS status
                FROM tickets t JOIN results r ON r.session_id=t.session_id
                LEFT JOIN followup_attempts f
                  ON f.ticket_id=t.ticket_id AND f.status IN ('claimed', 'consumed')
                LEFT JOIN resolved_handoffs h ON h.ticket_id=t.ticket_id
                WHERE (f.status IS NULL OR f.status!='consumed') AND h.ticket_id IS NULL
                ORDER BY t.created DESC LIMIT ?
            """, (time.time() - FOLLOWUP_LEASE_SECONDS, limit)).fetchall()
        return {"notices": list(map(dict, notices)), "tickets": list(map(dict, tickets))}

    @staticmethod
    def _agenda_rows(db, now, ticket_id=None):
        query = '''SELECT t.ticket_id AS id,t.case_id,t.session_id,t.created,s.scheduled_at,
                  COALESCE(s.version,0) AS version,
                  CASE WHEN a.status='consumed' THEN 'done'
                       WHEN a.status='claimed' AND a.claimed>? THEN 'in_progress' ELSE 'pending' END AS status
                  FROM tickets t JOIN results r ON t.session_id=r.session_id
                  LEFT JOIN callback_schedules s ON s.ticket_id=t.ticket_id
                  LEFT JOIN followup_attempts a ON a.ticket_id=t.ticket_id AND a.status IN ('claimed','consumed')
                  WHERE t.kind='callback' '''
        args = [now - FOLLOWUP_LEASE_SECONDS]
        if ticket_id is not None:
            query += ' AND t.ticket_id=?'; args.append(ticket_id)
        else:
            query += " AND (a.status IS NULL OR a.status!='consumed')"
        return [dict(row) for row in db.execute(query + ' ORDER BY s.scheduled_at IS NULL,s.scheduled_at,t.created,t.ticket_id',args)]

    def callback_agenda(self):
        with self.connect() as db:
            return self._agenda_rows(db,time.time())

    def schedule_callback(self, ticket_id, scheduled_at, version):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            now = time.time()
            rows = self._agenda_rows(db,now,ticket_id)
            if not rows:
                raise KeyError('找不到回电工单')
            current = rows[0]
            if current['status'] != 'pending' or current['version'] != version:
                raise ValueError('工单已由其他坐席修改、领取或处理，请刷新')
            if scheduled_at is not None and not int(now) <= scheduled_at <= int(now)+365*86400:
                raise OverflowError('请选择当前起一年内的回电时间')
            if current['scheduled_at'] != scheduled_at:
                db.execute('INSERT INTO callback_schedules VALUES (?,?,?) ON CONFLICT(ticket_id) DO UPDATE SET scheduled_at=excluded.scheduled_at,version=excluded.version', (ticket_id,scheduled_at,version+1))
                change = {'ticket': ticket_id, 'previous': current['scheduled_at'], 'scheduled_at': scheduled_at,'version':version+1}
                db.execute('INSERT INTO events(session_id,type,created,data) VALUES (?,?,?,?)',(current['session_id'],'callback_schedule',now,json.dumps(change)))
            return self._agenda_rows(db,now,ticket_id)[0]

    def resolve_handoff(self, ticket_id):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            ticket = db.execute("""
                SELECT t.kind FROM tickets t JOIN results r ON r.session_id=t.session_id WHERE t.ticket_id=?
            """, (ticket_id,)).fetchone()
            if not ticket or ticket["kind"] != "handoff":
                raise ValueError("没有可确认处理的人工复核工单")
            db.execute("INSERT INTO resolved_handoffs VALUES (?, ?) ON CONFLICT(ticket_id) DO NOTHING",
                       (ticket_id, time.time()))
            row = db.execute("SELECT resolved FROM resolved_handoffs WHERE ticket_id=?", (ticket_id,)).fetchone()
        return {"id": ticket_id, "status": "resolved", "resolved": row["resolved"]}


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
        case = self.knowledge.cases.get(result.case_id)
        if case is None:
            raise ValueError("找不到模拟单据")
        if result.disposition == "notified" and not (result.identity_verified and result.acknowledged):
            raise ValueError("通知完成必须验证身份并确认结果")
        notice = None
        if result.disposition in {"notified", "jingmai"}:
            reason = case.reason or case.name
            action = case.action or "请在京麦查看驳回单据中的书面审核意见，有疑问可联系官方客服核实"
            notice = (reason, action, case.source)
        ticket_kind = result.disposition if result.disposition in {"callback", "handoff"} else None
        return self.store.save_result(result, notice=notice, ticket_kind=ticket_kind)

    def result_artifacts(self, session_id):
        return self.store.result_artifacts(session_id)

    def create_ticket(self, session_id, case_id, kind):
        if kind not in {"callback", "handoff"} or case_id not in self.knowledge.cases:
            raise ValueError("工单类型或单据无效")
        return self.store.create_ticket(session_id, case_id, kind)
