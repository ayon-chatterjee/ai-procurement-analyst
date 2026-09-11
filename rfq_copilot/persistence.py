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
        response_id TEXT,
        duration_ms INTEGER NOT NULL,
        ok INTEGER NOT NULL,
        schema_valid INTEGER NOT NULL,
        error TEXT,
        raw_response TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS ix_ai_calls_rfq ON ai_calls(rfq_id, turn, created_at)",
    """CREATE TABLE IF NOT EXISTS suppliers (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL DEFAULT '',
        country TEXT NOT NULL DEFAULT '',
        contact_name TEXT NOT NULL DEFAULT '',
        contact_email TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'invited',
        note TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS supplier_responses (
        id TEXT PRIMARY KEY,
        rfq_id TEXT NOT NULL REFERENCES rfqs(id) ON DELETE CASCADE,
        supplier_id TEXT NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
        response_type TEXT NOT NULL,
        received_at TEXT NOT NULL,
        subject TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'received',
        extraction_status TEXT NOT NULL DEFAULT 'pending',
        extraction_note TEXT NOT NULL DEFAULT '',
        revises_response_id TEXT,
        superseded_by_id TEXT,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        payload TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS ix_responses_rfq ON supplier_responses(rfq_id, received_at)",
    "CREATE INDEX IF NOT EXISTS ix_responses_supplier ON supplier_responses(supplier_id)",
    """CREATE TABLE IF NOT EXISTS supplier_documents (
        id TEXT PRIMARY KEY,
        response_id TEXT NOT NULL REFERENCES supplier_responses(id) ON DELETE CASCADE,
        filename TEXT NOT NULL,
        path TEXT NOT NULL DEFAULT '',
        media_type TEXT NOT NULL DEFAULT '',
        byte_size INTEGER NOT NULL DEFAULT 0,
        extraction_method TEXT NOT NULL DEFAULT '',
        extraction_status TEXT NOT NULL DEFAULT 'pending',
        extraction_note TEXT NOT NULL DEFAULT '',
        raw_text TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS ix_documents_response ON supplier_documents(response_id)",
    """CREATE TABLE IF NOT EXISTS evidence (
        id TEXT PRIMARY KEY,
        response_id TEXT NOT NULL REFERENCES supplier_responses(id) ON DELETE CASCADE,
        document_id TEXT NOT NULL DEFAULT '',
        document_name TEXT NOT NULL DEFAULT '',
        source_type TEXT NOT NULL DEFAULT '',
        page INTEGER, sheet TEXT, row INTEGER, cell TEXT, paragraph INTEGER,
        location TEXT NOT NULL DEFAULT '',
        quoted_text TEXT NOT NULL DEFAULT '',
        verified INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS ix_evidence_response ON evidence(response_id)",
    """CREATE TABLE IF NOT EXISTS supplier_quotes (
        id TEXT PRIMARY KEY,
        response_id TEXT NOT NULL REFERENCES supplier_responses(id) ON DELETE CASCADE,
        rfq_id TEXT NOT NULL DEFAULT '',
        supplier_id TEXT NOT NULL DEFAULT '',
        line_item_id TEXT,
        unit_price REAL,
        currency TEXT NOT NULL DEFAULT '',
        normalized_unit_price REAL,
        status TEXT NOT NULL DEFAULT 'quoted',
        match_status TEXT NOT NULL DEFAULT 'unmatched',
        created_at TEXT NOT NULL,
        payload TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS ix_quotes_response ON supplier_quotes(response_id)",
    "CREATE INDEX IF NOT EXISTS ix_quotes_line ON supplier_quotes(rfq_id, line_item_id)",
    """CREATE TABLE IF NOT EXISTS questionnaire_responses (
        id TEXT PRIMARY KEY,
        response_id TEXT NOT NULL REFERENCES supplier_responses(id) ON DELETE CASCADE,
        question_id TEXT NOT NULL DEFAULT '',
        field_key TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'missing',
        created_at TEXT NOT NULL,
        payload TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS ix_questionnaire_response ON questionnaire_responses(response_id)",
    """CREATE TABLE IF NOT EXISTS certifications (
        id TEXT PRIMARY KEY,
        response_id TEXT NOT NULL REFERENCES supplier_responses(id) ON DELETE CASCADE,
        name TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'claimed',
        created_at TEXT NOT NULL,
        payload TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS ix_certifications_response ON certifications(response_id)",
    """CREATE TABLE IF NOT EXISTS supplier_questions (
        id TEXT PRIMARY KEY,
        response_id TEXT NOT NULL REFERENCES supplier_responses(id) ON DELETE CASCADE,
        question TEXT NOT NULL DEFAULT '',
        related_field_key TEXT NOT NULL DEFAULT '',
        resolved INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        payload TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS ix_supplier_questions_response ON supplier_questions(response_id)",
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

    #: Columns added after a table first shipped. CREATE TABLE IF NOT EXISTS silently
    #: skips an existing table, so new columns must be added explicitly.
    MIGRATIONS = [
        ("rfqs", "updated_seq", "INTEGER NOT NULL DEFAULT 0"),
        ("ai_calls", "response_id", "TEXT"),
    ]

    def init_schema(self) -> None:
        with self._conn() as c:
            c.execute("PRAGMA journal_mode = WAL")
            for stmt in DDL:
                c.execute(stmt)
            for table, column, decl in self.MIGRATIONS:
                if not self._has_table(c, table):
                    continue
                existing = {r["name"] for r in c.execute("PRAGMA table_info(%s)" % table)}
                if column not in existing:
                    c.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, decl))
            c.execute(
                "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (SCHEMA_VERSION,),
            )

    @staticmethod
    def _has_table(conn, name: str) -> bool:
        row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
        return row is not None

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


# --------------------------------------------------------------------------- #
# Phase 2 — suppliers and their responses
# --------------------------------------------------------------------------- #
class SupplierRepository:
    """Persistence for supplier responses and everything extracted from them.

    Same approach as the RFQ store: indexed columns for the things we query across
    (line item, status, supplier) plus a JSON payload holding the full record, so the
    dataclass stays the single definition of shape.
    """

    def __init__(self, repo: "RFQRepository"):
        self.repo = repo

    @property
    def _conn(self):
        return self.repo._conn

    # -- suppliers ------------------------------------------------------------
    def save_supplier(self, s) -> None:
        with self._conn() as c:
            c.execute("""INSERT INTO suppliers(id, name, country, contact_name, contact_email, status, note, created_at)
                         VALUES(?,?,?,?,?,?,?,?)
                         ON CONFLICT(id) DO UPDATE SET name=excluded.name, country=excluded.country,
                           contact_name=excluded.contact_name, contact_email=excluded.contact_email,
                           status=excluded.status, note=excluded.note""",
                      (s.id, s.name, s.country, s.contact_name, s.contact_email, s.status.value, s.note, s.created_at))

    def list_suppliers(self) -> List[Any]:
        from .supplier_models import Supplier
        with self._conn() as c:
            rows = c.execute("SELECT * FROM suppliers ORDER BY name").fetchall()
        return [Supplier.from_dict(dict(r)) for r in rows]

    def get_supplier(self, supplier_id: str):
        from .supplier_models import Supplier
        with self._conn() as c:
            r = c.execute("SELECT * FROM suppliers WHERE id = ?", (supplier_id,)).fetchone()
        return Supplier.from_dict(dict(r)) if r else None

    # -- responses ------------------------------------------------------------
    def save_bundle(self, bundle) -> None:
        """Write a response and every record extracted from it, replacing any prior rows."""
        r = bundle.response
        with self._conn() as c:
            c.execute("""INSERT INTO supplier_responses(id, rfq_id, supplier_id, response_type, received_at, subject,
                             status, extraction_status, extraction_note, revises_response_id, superseded_by_id,
                             is_active, created_at, updated_at, payload)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                         ON CONFLICT(id) DO UPDATE SET response_type=excluded.response_type,
                           subject=excluded.subject, status=excluded.status,
                           extraction_status=excluded.extraction_status, extraction_note=excluded.extraction_note,
                           revises_response_id=excluded.revises_response_id,
                           superseded_by_id=excluded.superseded_by_id, is_active=excluded.is_active,
                           updated_at=excluded.updated_at, payload=excluded.payload""",
                      (r.id, r.rfq_id, r.supplier_id, r.response_type.value, r.received_at, r.subject, r.status,
                       r.extraction_status.value, r.extraction_note, r.revises_response_id, r.superseded_by_id,
                       1 if r.is_active else 0, r.created_at, r.updated_at,
                       json.dumps(r.to_dict(), ensure_ascii=False)))

            for table in ("supplier_documents", "evidence", "supplier_quotes",
                          "questionnaire_responses", "certifications", "supplier_questions"):
                c.execute("DELETE FROM %s WHERE response_id = ?" % table, (r.id,))

            for d in bundle.documents:
                c.execute("""INSERT INTO supplier_documents(id, response_id, filename, path, media_type, byte_size,
                                 extraction_method, extraction_status, extraction_note, raw_text, created_at)
                             VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                          (d.id, r.id, d.filename, d.path, d.media_type, d.byte_size, d.extraction_method,
                           d.extraction_status.value, d.extraction_note, d.raw_text, d.created_at))
            for e in bundle.evidence.values():
                c.execute("""INSERT INTO evidence(id, response_id, document_id, document_name, source_type,
                                 page, sheet, row, cell, paragraph, location, quoted_text, verified, created_at)
                             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (e.id, r.id, e.document_id, e.document_name, e.source_type, e.page, e.sheet, e.row,
                           e.cell, e.paragraph, e.location, e.quoted_text, 1 if e.verified else 0, e.created_at))
            for q in bundle.quotes:
                c.execute("""INSERT INTO supplier_quotes(id, response_id, rfq_id, supplier_id, line_item_id,
                                 unit_price, currency, normalized_unit_price, status, match_status, created_at, payload)
                             VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (q.id, r.id, q.rfq_id, q.supplier_id, q.line_item_id, q.unit_price, q.currency,
                           q.normalized_unit_price, q.status.value, q.match_status.value, q.created_at,
                           json.dumps(q.to_dict(), ensure_ascii=False)))
            for a in bundle.questionnaire:
                c.execute("""INSERT INTO questionnaire_responses(id, response_id, question_id, field_key, status, created_at, payload)
                             VALUES(?,?,?,?,?,?,?)""",
                          (a.id, r.id, a.question_id, a.field_key, a.status.value, a.created_at,
                           json.dumps(a.to_dict(), ensure_ascii=False)))
            for cert in bundle.certifications:
                c.execute("""INSERT INTO certifications(id, response_id, name, status, created_at, payload)
                             VALUES(?,?,?,?,?,?)""",
                          (cert.id, r.id, cert.name, cert.status.value, cert.created_at,
                           json.dumps(cert.to_dict(), ensure_ascii=False)))
            for sq in bundle.questions:
                c.execute("""INSERT INTO supplier_questions(id, response_id, question, related_field_key, resolved, created_at, payload)
                             VALUES(?,?,?,?,?,?,?)""",
                          (sq.id, r.id, sq.question, sq.related_field_key, 1 if sq.resolved else 0, sq.created_at,
                           json.dumps(sq.to_dict(), ensure_ascii=False)))

    def get_bundle(self, response_id: str):
        from .supplier_models import (
            Certification, Evidence, QuestionnaireResponse, ResponseBundle, SourceDocument,
            SupplierQuestion, SupplierQuote, SupplierResponse,
        )
        with self._conn() as c:
            row = c.execute("SELECT payload FROM supplier_responses WHERE id = ?", (response_id,)).fetchone()
            if not row:
                return None
            resp = SupplierResponse.from_dict(json.loads(row["payload"]))
            docs = [SourceDocument.from_dict(dict(x)) for x in
                    c.execute("SELECT * FROM supplier_documents WHERE response_id = ?", (response_id,))]
            ev = {}
            for x in c.execute("SELECT * FROM evidence WHERE response_id = ?", (response_id,)):
                e = Evidence.from_dict(dict(x))
                ev[e.id] = e
            quotes = [SupplierQuote.from_dict(json.loads(x["payload"])) for x in
                      c.execute("SELECT payload FROM supplier_quotes WHERE response_id = ?", (response_id,))]
            answers = [QuestionnaireResponse.from_dict(json.loads(x["payload"])) for x in
                       c.execute("SELECT payload FROM questionnaire_responses WHERE response_id = ?", (response_id,))]
            certs = [Certification.from_dict(json.loads(x["payload"])) for x in
                     c.execute("SELECT payload FROM certifications WHERE response_id = ?", (response_id,))]
            qs = [SupplierQuestion.from_dict(json.loads(x["payload"])) for x in
                  c.execute("SELECT payload FROM supplier_questions WHERE response_id = ?", (response_id,))]
        return ResponseBundle(response=resp, supplier=self.get_supplier(resp.supplier_id), documents=docs,
                              quotes=quotes, questionnaire=answers, certifications=certs, questions=qs, evidence=ev)

    def list_responses(self, rfq_id: str) -> List[Any]:
        from .supplier_models import SupplierResponse
        with self._conn() as c:
            rows = c.execute("SELECT payload FROM supplier_responses WHERE rfq_id = ? ORDER BY received_at",
                             (rfq_id,)).fetchall()
        return [SupplierResponse.from_dict(json.loads(r["payload"])) for r in rows]

    def list_bundles(self, rfq_id: str, active_only: bool = False) -> List[Any]:
        out = []
        for r in self.list_responses(rfq_id):
            if active_only and not r.is_active:
                continue
            b = self.get_bundle(r.id)
            if b:
                out.append(b)
        return out

    def delete_responses_for(self, rfq_id: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM supplier_responses WHERE rfq_id = ?", (rfq_id,))

    def get_evidence(self, evidence_id: str):
        from .supplier_models import Evidence
        with self._conn() as c:
            r = c.execute("SELECT * FROM evidence WHERE id = ?", (evidence_id,)).fetchone()
        return Evidence.from_dict(dict(r)) if r else None

    # -- audit ----------------------------------------------------------------
    def add_ai_call(self, rec, response_id: Optional[str] = None) -> None:
        """Best effort: losing an audit row must never cost us a successful extraction."""
        try:
            self._add_ai_call(rec, response_id)
        except Exception:
            pass

    def _add_ai_call(self, rec, response_id: Optional[str] = None) -> None:
        with self._conn() as c:
            c.execute("""INSERT INTO ai_calls(id, rfq_id, turn, call_type, provider, model, prompt_version,
                             prompt_hash, prompt_chars, prompt_text, response_id, duration_ms, ok, schema_valid,
                             error, raw_response, created_at)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (rec.id, rec.rfq_id, rec.turn, rec.call_type, rec.provider, rec.model, rec.prompt_version,
                       rec.prompt_hash, rec.prompt_chars, rec.prompt_text, response_id, rec.duration_ms,
                       1 if rec.ok else 0, 1 if rec.schema_valid else 0, rec.error, rec.raw_response, rec.created_at))

    def list_extraction_calls(self, response_id: str) -> List[Any]:
        from .schema import AICallRecord
        with self._conn() as c:
            rows = c.execute("SELECT * FROM ai_calls WHERE response_id = ? ORDER BY created_at", (response_id,)).fetchall()
        return [AICallRecord.from_dict(dict(r)) for r in rows]
