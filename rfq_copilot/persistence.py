"""SQLite persistence for RFQs, conversation messages and AI call audit.

* One short-lived connection per operation (no thread-affinity problems under
  Streamlit), WAL journal, foreign keys on.
* The RFQ itself is stored as a JSON payload plus indexed summary columns; the
  dataclass ``from_dict`` is the single source of truth for shape.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Optional

from .schema import AICallRecord, Message, RFQ, RFQStatus, RFQSummary, rfq_from_json, rfq_to_json, utc_now

SCHEMA_VERSION = "1"

DDL = [
    "CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS rfqs (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL DEFAULT '',
        product TEXT NOT NULL DEFAULT '',
        category TEXT NOT NULL DEFAULT '',
        product_type TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL,
        readiness_score INTEGER NOT NULL DEFAULT 0,
        ready_to_send INTEGER NOT NULL DEFAULT 0,
        line_item_count INTEGER NOT NULL DEFAULT 0,
        turn INTEGER NOT NULL DEFAULT 0,
        updated_seq INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        payload TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS ix_rfqs_updated ON rfqs(updated_seq DESC)",
    "CREATE INDEX IF NOT EXISTS ix_rfqs_status ON rfqs(status)",
    """CREATE TABLE IF NOT EXISTS messages (
        id TEXT PRIMARY KEY,
        rfq_id TEXT NOT NULL REFERENCES rfqs(id) ON DELETE CASCADE,
        turn INTEGER NOT NULL,
        role TEXT NOT NULL,
        kind TEXT NOT NULL,
        content TEXT NOT NULL,
        payload TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS ix_messages_rfq ON messages(rfq_id, turn, created_at)",
    """CREATE TABLE IF NOT EXISTS ai_calls (
        id TEXT PRIMARY KEY,
        rfq_id TEXT REFERENCES rfqs(id) ON DELETE CASCADE,
        turn INTEGER NOT NULL,
        call_type TEXT NOT NULL,
        provider TEXT NOT NULL,
        model TEXT NOT NULL,
        prompt_version TEXT NOT NULL,
        prompt_hash TEXT NOT NULL,
        prompt_chars INTEGER NOT NULL,
        prompt_text TEXT,
        duration_ms INTEGER NOT NULL,
        ok INTEGER NOT NULL,
        schema_valid INTEGER NOT NULL,
        error TEXT,
        raw_response TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS ix_ai_calls_rfq ON ai_calls(rfq_id, turn, created_at)",
]


class RFQRepository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    # -- connection -----------------------------------------------------------
    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self._conn() as c:
            c.execute("PRAGMA journal_mode = WAL")
            for stmt in DDL:
                c.execute(stmt)
            c.execute(
                "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (SCHEMA_VERSION,),
            )

    # -- RFQs -----------------------------------------------------------------
    def save_rfq(self, rfq: RFQ) -> None:
        rfq.updated_at = utc_now()
        with self._conn() as c:
            c.execute(
                """INSERT INTO rfqs(id, title, product, category, product_type, status, readiness_score,
                                    ready_to_send, line_item_count, turn, updated_seq, created_at, updated_at, payload)
                   VALUES(?,?,?,?,?,?,?,?,?,?,(SELECT COALESCE(MAX(updated_seq), 0) + 1 FROM rfqs),?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     title = excluded.title, product = excluded.product, category = excluded.category,
                     product_type = excluded.product_type, status = excluded.status,
                     readiness_score = excluded.readiness_score, ready_to_send = excluded.ready_to_send,
                     line_item_count = excluded.line_item_count, turn = excluded.turn,
                     updated_seq = excluded.updated_seq, updated_at = excluded.updated_at, payload = excluded.payload""",
                (
                    rfq.id, rfq.title, rfq.product, rfq.category, rfq.product_type, rfq.status.value,
                    int(rfq.completeness.score), 1 if rfq.completeness.ready_to_send else 0,
                    len(rfq.line_items), rfq.turn, rfq.created_at, rfq.updated_at, rfq_to_json(rfq),
                ),
            )

    def get_rfq(self, rfq_id: str) -> Optional[RFQ]:
        with self._conn() as c:
            row = c.execute("SELECT payload FROM rfqs WHERE id = ?", (rfq_id,)).fetchone()
        return rfq_from_json(row["payload"]) if row else None

    def list_rfqs(self) -> List[RFQSummary]:
        with self._conn() as c:
            rows = c.execute(
                """SELECT id, title, product, category, status, readiness_score, ready_to_send,
                          line_item_count, turn, created_at, updated_at
                   FROM rfqs ORDER BY updated_seq DESC, updated_at DESC"""
            ).fetchall()
        out: List[RFQSummary] = []
        for r in rows:
            try:
                status = RFQStatus(r["status"])
            except ValueError:
                status = RFQStatus.DRAFT
            out.append(RFQSummary(
                id=r["id"], title=r["title"], product=r["product"], category=r["category"], status=status,
                readiness_score=int(r["readiness_score"]), ready_to_send=bool(r["ready_to_send"]),
                line_item_count=int(r["line_item_count"]), turn=int(r["turn"]),
                created_at=r["created_at"], updated_at=r["updated_at"],
            ))
        return out

    def delete_rfq(self, rfq_id: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM rfqs WHERE id = ?", (rfq_id,))

    # -- messages -------------------------------------------------------------
    def add_message(self, m: Message) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO messages(id, rfq_id, turn, role, kind, content, payload, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (m.id, m.rfq_id, m.turn, m.role.value, m.kind, m.content, json.dumps(m.payload, ensure_ascii=False), m.created_at),
            )

    def list_messages(self, rfq_id: str) -> List[Message]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM messages WHERE rfq_id = ? ORDER BY turn, created_at, rowid", (rfq_id,)
            ).fetchall()
        out: List[Message] = []
        for r in rows:
            d = dict(r)
            try:
                d["payload"] = json.loads(d.get("payload") or "{}")
            except ValueError:
                d["payload"] = {}
            out.append(Message.from_dict(d))
        return out

    def get_message(self, message_id: str) -> Optional[Message]:
        with self._conn() as c:
            r = c.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        try:
            d["payload"] = json.loads(d.get("payload") or "{}")
        except ValueError:
            d["payload"] = {}
        return Message.from_dict(d)

    # -- AI call audit --------------------------------------------------------
    def add_ai_call(self, rec: AICallRecord) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO ai_calls(id, rfq_id, turn, call_type, provider, model, prompt_version, prompt_hash,
                                        prompt_chars, prompt_text, duration_ms, ok, schema_valid, error, raw_response, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    rec.id, rec.rfq_id, rec.turn, rec.call_type, rec.provider, rec.model, rec.prompt_version,
                    rec.prompt_hash, rec.prompt_chars, rec.prompt_text, rec.duration_ms, 1 if rec.ok else 0,
                    1 if rec.schema_valid else 0, rec.error, rec.raw_response, rec.created_at,
                ),
            )

    def list_ai_calls(self, rfq_id: str) -> List[AICallRecord]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM ai_calls WHERE rfq_id = ? ORDER BY turn, created_at, rowid", (rfq_id,)
            ).fetchall()
        return [AICallRecord.from_dict(dict(r)) for r in rows]
