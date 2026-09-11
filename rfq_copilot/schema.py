"""Structured RFQ model for the RFQ Copilot.

Plain dataclasses + ``str`` enums so every object serialises to JSON with no
extra dependencies (Python 3.9, no pydantic). ``from_dict`` constructors are
tolerant of missing or unknown keys so older payloads keep loading after
additive schema changes.

Design notes
------------
* ``RFQ.fields`` is a flat registry keyed by canonical field key. Each
  ``FieldValue`` carries its own ``section`` (commercial / sourcing / ...), so
  product-specific technical fields (``flute``, ``board_grade``) can be added by
  the AI without any schema or database change.
* Provenance is first-class: every value has ``source``, ``source_refs``
  (``msg:<id>``, ``ans:<qid>``, ``manual``) and a verbatim ``evidence`` span.
* Field *status* is semantic (PROVIDED / RECOMMENDED / MISSING / UNKNOWN /
  NOT_APPLICABLE / CONFLICT) — unknown information is never collapsed to null.
* The same record shape is what a future supplier-response extractor will use.
"""
from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class Section(str, Enum):
    COMMERCIAL = "commercial"
    SOURCING = "sourcing"
    LOGISTICS = "logistics"
    TECHNICAL = "technical"
    QUALITY = "quality"
    INSTRUCTIONS = "instructions"


SECTION_LABELS = {
    Section.COMMERCIAL: "Commercial requirements",
    Section.SOURCING: "Sourcing requirements",
    Section.LOGISTICS: "Logistics requirements",
    Section.TECHNICAL: "Technical requirements",
    Section.QUALITY: "Quality requirements",
    Section.INSTRUCTIONS: "Supplier instructions",
}


class Importance(str, Enum):
    REQUIRED = "required"
    RECOMMENDED = "recommended"
    OPTIONAL = "optional"
    NOT_APPLICABLE = "not_applicable"


class FieldStatus(str, Enum):
    PROVIDED = "provided"            # buyer explicitly supplied it
    RECOMMENDED = "recommended"      # AI suggests confirming/providing it (no buyer value)
    MISSING = "missing"              # relevant but nobody has provided it
    UNKNOWN = "unknown"              # buyer explicitly said they do not know
    NOT_APPLICABLE = "not_applicable"
    CONFLICT = "conflict"            # two explicit buyer values contradict each other


class Source(str, Enum):
    BUYER_EXPLICIT = "buyer_explicit"
    AI_RECOMMENDED = "ai_recommended"
    MANUAL_EDIT = "manual_edit"
    MISSING = "missing"


class ValueKind(str, Enum):
    TEXT = "text"
    NUMBER = "number"
    DATE = "date"
    LIST = "list"
    BOOLEAN = "boolean"


class QuestionStatus(str, Enum):
    OPEN = "open"
    ANSWERED = "answered"
    SKIPPED = "skipped"
    DISMISSED = "dismissed"  # made redundant (field filled another way, duplicate, N/A)


class AnswerType(str, Enum):
    TEXT = "text"
    NUMBER = "number"
    DATE = "date"
    CHOICE = "choice"          # exactly one option
    MULTI_CHOICE = "multi_choice"   # several options may apply at once (e.g. sea AND air)
    YES_NO = "yes_no"


class RFQStatus(str, Enum):
    DRAFT = "draft"
    IN_PROGRESS = "in_progress"
    READY = "ready"
    SUPPLIER_READY = "supplier_ready"


class MessageRole(str, Enum):
    BUYER = "buyer"
    ASSISTANT = "assistant"
    SYSTEM = "system"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return "%s_%s" % (prefix, secrets.token_hex(4))


def line_item_id(n: int) -> str:
    return "LINE-%03d" % n


def _enum(cls, value: Any, default):
    if isinstance(value, cls):
        return value
    try:
        return cls(value)
    except Exception:
        return default


def _to_jsonable(obj: Any) -> Any:
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    return obj


def _dc_to_dict(dc) -> Dict[str, Any]:
    return _to_jsonable(asdict(dc))


# --------------------------------------------------------------------------- #
# Field values
# --------------------------------------------------------------------------- #
@dataclass
class FieldValue:
    key: str
    label: str
    section: Section
    value: Any = None                       # str | float | bool | List[str] | None
    value_kind: ValueKind = ValueKind.TEXT
    unit: Optional[str] = None
    importance: Importance = Importance.RECOMMENDED
    status: FieldStatus = FieldStatus.MISSING
    source: Source = Source.MISSING
    evidence: Optional[str] = None          # verbatim span from the buyer's text
    source_refs: List[str] = field(default_factory=list)
    confidence: Optional[float] = None
    note: Optional[str] = None              # AI reason / recommendation text / N/A justification
    history: List[Dict[str, Any]] = field(default_factory=list)   # superseded values, auditable
    conflict_values: List[Dict[str, Any]] = field(default_factory=list)
    updated_turn: int = 0

    # -- derived -----------------------------------------------------------
    @property
    def is_filled(self) -> bool:
        """True only for an explicit buyer value (typed or manually edited)."""
        return self.status == FieldStatus.PROVIDED and self.value not in (None, "", [])

    @property
    def is_buyer_fact(self) -> bool:
        return self.source in (Source.BUYER_EXPLICIT, Source.MANUAL_EDIT)

    @property
    def counts_for_score(self) -> bool:
        return self.importance != Importance.NOT_APPLICABLE and self.status != FieldStatus.NOT_APPLICABLE

    def display_value(self) -> str:
        if self.value in (None, "", []):
            return ""
        if isinstance(self.value, list):
            text = ", ".join(str(v) for v in self.value)
        elif isinstance(self.value, bool):
            text = "Yes" if self.value else "No"
        elif isinstance(self.value, float):
            text = ("%d" % self.value) if self.value.is_integer() else ("%g" % self.value)
            if abs(self.value) >= 1000 and self.value.is_integer():
                text = "{:,}".format(int(self.value))
        else:
            text = str(self.value)
        if self.unit:
            text = "%s %s" % (text, self.unit)
        return text

    def display_status(self) -> str:
        if self.status == FieldStatus.PROVIDED:
            return "Buyer provided" if self.source != Source.MANUAL_EDIT else "Buyer edited"
        if self.status == FieldStatus.RECOMMENDED:
            return "AI recommended"
        if self.status == FieldStatus.UNKNOWN:
            return "Buyer doesn't know"
        if self.status == FieldStatus.NOT_APPLICABLE:
            return "Not applicable"
        if self.status == FieldStatus.CONFLICT:
            return "Conflict"
        return "Missing"

    def to_dict(self) -> Dict[str, Any]:
        return _dc_to_dict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "FieldValue":
        return cls(
            key=str(d.get("key", "")),
            label=str(d.get("label", d.get("key", ""))),
            section=_enum(Section, d.get("section"), Section.TECHNICAL),
            value=d.get("value"),
            value_kind=_enum(ValueKind, d.get("value_kind"), ValueKind.TEXT),
            unit=d.get("unit"),
            importance=_enum(Importance, d.get("importance"), Importance.RECOMMENDED),
            status=_enum(FieldStatus, d.get("status"), FieldStatus.MISSING),
            source=_enum(Source, d.get("source"), Source.MISSING),
            evidence=d.get("evidence"),
            source_refs=list(d.get("source_refs") or []),
            confidence=d.get("confidence"),
            note=d.get("note"),
            history=list(d.get("history") or []),
            conflict_values=list(d.get("conflict_values") or []),
            updated_turn=int(d.get("updated_turn") or 0),
        )


# --------------------------------------------------------------------------- #
# Line items
# --------------------------------------------------------------------------- #
@dataclass
class SpecAttr:
    name: str
    value: str
    unit: Optional[str] = None

    def display(self) -> str:
        return ("%s %s" % (self.value, self.unit)).strip() if self.unit else str(self.value)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SpecAttr":
        return cls(name=str(d.get("name", "")), value=str(d.get("value", "")), unit=d.get("unit"))


@dataclass
class LineItem:
    id: str
    product: str
    description: str = ""
    specifications: List[SpecAttr] = field(default_factory=list)
    quantity: Optional[float] = None
    unit: str = "pcs"
    target_price: Optional[float] = None
    required_date: Optional[str] = None
    source: Source = Source.BUYER_EXPLICIT
    source_refs: List[str] = field(default_factory=list)
    evidence: Optional[str] = None
    history: List[Dict[str, Any]] = field(default_factory=list)

    def spec_summary(self) -> str:
        return "; ".join("%s: %s" % (s.name, s.display()) for s in self.specifications if s.value)

    def to_dict(self) -> Dict[str, Any]:
        return _dc_to_dict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "LineItem":
        return cls(
            id=str(d.get("id", "")),
            product=str(d.get("product", "")),
            description=str(d.get("description", "") or ""),
            specifications=[SpecAttr.from_dict(s) for s in (d.get("specifications") or [])],
            quantity=d.get("quantity"),
            unit=str(d.get("unit") or "pcs"),
            target_price=d.get("target_price"),
            required_date=d.get("required_date"),
            source=_enum(Source, d.get("source"), Source.BUYER_EXPLICIT),
            source_refs=list(d.get("source_refs") or []),
            evidence=d.get("evidence"),
            history=list(d.get("history") or []),
        )


# --------------------------------------------------------------------------- #
# Questions
# --------------------------------------------------------------------------- #
@dataclass
class Question:
    id: str
    category: Section
    question: str
    reason: str = ""
    importance: Importance = Importance.RECOMMENDED
    field_key: Optional[str] = None
    answer_type: AnswerType = AnswerType.TEXT
    suggested_options: List[str] = field(default_factory=list)
    answer: Optional[str] = None
    status: QuestionStatus = QuestionStatus.OPEN
    resolution: Optional[str] = None      # answered | unknown | not_applicable | skipped | filled | duplicate
    asked_turn: int = 0
    answered_turn: Optional[int] = None
    answer_ref: Optional[str] = None      # msg:<id> / manual

    def to_dict(self) -> Dict[str, Any]:
        return _dc_to_dict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Question":
        return cls(
            id=str(d.get("id", "")),
            category=_enum(Section, d.get("category"), Section.TECHNICAL),
            question=str(d.get("question", "")),
            reason=str(d.get("reason", "") or ""),
            importance=_enum(Importance, d.get("importance"), Importance.RECOMMENDED),
            field_key=d.get("field_key"),
            answer_type=_enum(AnswerType, d.get("answer_type"), AnswerType.TEXT),
            suggested_options=list(d.get("suggested_options") or []),
            answer=d.get("answer"),
            status=_enum(QuestionStatus, d.get("status"), QuestionStatus.OPEN),
            resolution=d.get("resolution"),
            asked_turn=int(d.get("asked_turn") or 0),
            answered_turn=d.get("answered_turn"),
            answer_ref=d.get("answer_ref"),
        )


# --------------------------------------------------------------------------- #
# Completeness / readiness
# --------------------------------------------------------------------------- #
@dataclass
class Completeness:
    score: int = 0                                  # deterministic 0-100
    ready_to_send: bool = False                     # deterministic
    missing_required_fields: List[str] = field(default_factory=list)   # human labels
    recommended_fields: List[str] = field(default_factory=list)
    open_conflicts: List[str] = field(default_factory=list)
    blocking_questions: List[str] = field(default_factory=list)        # required open questions not already counted above
    open_ambiguities: List[str] = field(default_factory=list)
    explanation: str = ""
    ai_score: Optional[int] = None                  # audit only
    ai_ready_claim: Optional[bool] = None           # audit only
    ai_explanation: Optional[str] = None
    computed_turn: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return _dc_to_dict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Completeness":
        return cls(
            score=int(d.get("score") or 0),
            ready_to_send=bool(d.get("ready_to_send", False)),
            missing_required_fields=list(d.get("missing_required_fields") or []),
            recommended_fields=list(d.get("recommended_fields") or []),
            open_conflicts=list(d.get("open_conflicts") or []),
            blocking_questions=list(d.get("blocking_questions") or []),
            open_ambiguities=list(d.get("open_ambiguities") or []),
            explanation=str(d.get("explanation", "") or ""),
            ai_score=d.get("ai_score"),
            ai_ready_claim=d.get("ai_ready_claim"),
            ai_explanation=d.get("ai_explanation"),
            computed_turn=int(d.get("computed_turn") or 0),
        )


# --------------------------------------------------------------------------- #
# Conversation + audit
# --------------------------------------------------------------------------- #
@dataclass
class Message:
    id: str
    rfq_id: str
    turn: int
    role: MessageRole
    kind: str                     # request | answers | assistant | error | manual_edit
    content: str
    payload: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> Dict[str, Any]:
        return _dc_to_dict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Message":
        return cls(
            id=str(d.get("id", "")),
            rfq_id=str(d.get("rfq_id", "")),
            turn=int(d.get("turn") or 0),
            role=_enum(MessageRole, d.get("role"), MessageRole.SYSTEM),
            kind=str(d.get("kind", "") or ""),
            content=str(d.get("content", "") or ""),
            payload=dict(d.get("payload") or {}),
            created_at=str(d.get("created_at") or utc_now()),
        )


@dataclass
class AICallRecord:
    id: str
    rfq_id: Optional[str]
    turn: int
    call_type: str                # first_turn | turn | supplier_summary | health
    provider: str
    model: str
    prompt_version: str
    prompt_hash: str
    prompt_chars: int
    duration_ms: int
    ok: bool
    schema_valid: bool
    error: Optional[str] = None
    raw_response: str = ""
    prompt_text: Optional[str] = None
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> Dict[str, Any]:
        return _dc_to_dict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AICallRecord":
        return cls(
            id=str(d.get("id", "")),
            rfq_id=d.get("rfq_id"),
            turn=int(d.get("turn") or 0),
            call_type=str(d.get("call_type", "")),
            provider=str(d.get("provider", "")),
            model=str(d.get("model", "")),
            prompt_version=str(d.get("prompt_version", "")),
            prompt_hash=str(d.get("prompt_hash", "")),
            prompt_chars=int(d.get("prompt_chars") or 0),
            duration_ms=int(d.get("duration_ms") or 0),
            ok=bool(d.get("ok", False)),
            schema_valid=bool(d.get("schema_valid", False)),
            error=d.get("error"),
            raw_response=str(d.get("raw_response", "") or ""),
            prompt_text=d.get("prompt_text"),
            created_at=str(d.get("created_at") or utc_now()),
        )


# --------------------------------------------------------------------------- #
# RFQ
# --------------------------------------------------------------------------- #
@dataclass
class RFQ:
    id: str
    title: str = ""
    product: str = ""
    category: str = ""
    product_type: str = ""
    classification_note: str = ""
    line_items: List[LineItem] = field(default_factory=list)
    fields: Dict[str, FieldValue] = field(default_factory=dict)
    questions: List[Question] = field(default_factory=list)
    completeness: Completeness = field(default_factory=Completeness)
    status: RFQStatus = RFQStatus.DRAFT
    turn: int = 0
    line_seq: int = 0            # high-water mark: line ids are never reused
    supplier_summary: Optional[str] = None
    schema_version: int = 1
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    # -- section views (constitution names) ---------------------------------
    def section(self, s: Section) -> Dict[str, FieldValue]:
        return {k: v for k, v in self.fields.items() if v.section == s}

    @property
    def commercial_requirements(self) -> Dict[str, FieldValue]:
        return self.section(Section.COMMERCIAL)

    @property
    def sourcing_requirements(self) -> Dict[str, FieldValue]:
        return self.section(Section.SOURCING)

    @property
    def logistics_requirements(self) -> Dict[str, FieldValue]:
        return self.section(Section.LOGISTICS)

    @property
    def technical_requirements(self) -> Dict[str, FieldValue]:
        return self.section(Section.TECHNICAL)

    @property
    def quality_requirements(self) -> Dict[str, FieldValue]:
        return self.section(Section.QUALITY)

    @property
    def supplier_instructions(self) -> Dict[str, FieldValue]:
        return self.section(Section.INSTRUCTIONS)

    # -- convenience ----------------------------------------------------------
    def open_questions(self) -> List[Question]:
        return [q for q in self.questions if q.status == QuestionStatus.OPEN]

    def question(self, qid: str) -> Optional[Question]:
        for q in self.questions:
            if q.id == qid:
                return q
        return None

    def line_item(self, lid: str) -> Optional[LineItem]:
        for li in self.line_items:
            if li.id == lid:
                return li
        return None

    def next_line_item_id(self) -> str:
        """Allocate a fresh id. Never reuses a deleted line's id, because a line id is the
        join key a supplier quote will be matched against later."""
        self.line_seq = max(self.line_seq, self._max_line_number()) + 1
        return line_item_id(self.line_seq)

    def _max_line_number(self) -> int:
        n = 0
        for li in self.line_items:
            try:
                n = max(n, int(li.id.split("-")[-1]))
            except ValueError:
                continue
        return n

    @property
    def is_classified(self) -> bool:
        return bool(self.product.strip() and self.category.strip())

    # -- serialisation --------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "product": self.product,
            "category": self.category,
            "product_type": self.product_type,
            "classification_note": self.classification_note,
            "line_items": [li.to_dict() for li in self.line_items],
            "fields": {k: v.to_dict() for k, v in self.fields.items()},
            "questions": [q.to_dict() for q in self.questions],
            "completeness": self.completeness.to_dict(),
            "status": self.status.value,
            "turn": self.turn,
            "line_seq": self.line_seq,
            "supplier_summary": self.supplier_summary,
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "RFQ":
        fields_raw = d.get("fields") or {}
        fields: Dict[str, FieldValue] = {}
        for k, v in fields_raw.items():
            fv = FieldValue.from_dict(dict(v, key=v.get("key", k)))
            fields[fv.key] = fv
        return cls(
            id=str(d.get("id", "")),
            title=str(d.get("title", "") or ""),
            product=str(d.get("product", "") or ""),
            category=str(d.get("category", "") or ""),
            product_type=str(d.get("product_type", "") or ""),
            classification_note=str(d.get("classification_note", "") or ""),
            line_items=[LineItem.from_dict(x) for x in (d.get("line_items") or [])],
            fields=fields,
            questions=[Question.from_dict(x) for x in (d.get("questions") or [])],
            completeness=Completeness.from_dict(d.get("completeness") or {}),
            status=_enum(RFQStatus, d.get("status"), RFQStatus.DRAFT),
            turn=int(d.get("turn") or 0),
            line_seq=int(d.get("line_seq") or 0),   # older payloads derive it from the ids below
            supplier_summary=d.get("supplier_summary"),
            schema_version=int(d.get("schema_version") or 1),
            created_at=str(d.get("created_at") or utc_now()),
            updated_at=str(d.get("updated_at") or utc_now()),
        )


def rfq_to_json(rfq: RFQ) -> str:
    return json.dumps(rfq.to_dict(), ensure_ascii=False, indent=None)


def rfq_from_json(s: str) -> RFQ:
    return RFQ.from_dict(json.loads(s))


@dataclass
class RFQSummary:
    """Listing row built from indexed columns only (no payload parse)."""
    id: str
    title: str
    product: str
    category: str
    status: RFQStatus
    readiness_score: int
    ready_to_send: bool
    line_item_count: int
    turn: int
    created_at: str
    updated_at: str
