"""Phase 2 data model — suppliers, their responses, and what we extracted from them.

The organising principle is that **a supplier response is evidence, not truth**. Every
value we surface carries where it came from, how sure we are, and whether it could be
normalised safely. Nothing here ever collapses "the supplier didn't say" into a number.

Conventions follow Phase 1 (`schema.py`): plain dataclasses, ``str`` enums so JSON is
trivial, ``from_dict`` tolerant of missing/unknown keys, and stable prefixed ids so
Phase 3 can join across RFQ, line item, supplier, response, question and evidence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from .schema import _dc_to_dict, _enum, new_id, utc_now


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class SupplierStatus(str, Enum):
    INVITED = "invited"
    RESPONDED = "responded"
    DECLINED = "declined"
    NO_RESPONSE = "no_response"


class ResponseType(str, Enum):
    QUOTE_RECEIVED = "quote_received"
    PARTIAL_QUOTE = "partial_quote"
    QUESTION = "question"
    DECLINED = "declined"
    NO_RESPONSE = "no_response"
    REVISION_RECEIVED = "revision_received"
    NEEDS_REVIEW = "needs_review"


class ExtractionStatus(str, Enum):
    PENDING = "pending"          # document received, not yet read
    EXTRACTED = "extracted"      # AI extraction succeeded and passed the guards
    NEEDS_REVIEW = "needs_review"  # extracted, but something needs a human
    FAILED = "failed"            # extraction could not produce usable structure
    UNSUPPORTED = "unsupported"  # we cannot read this document format at all


class QuoteStatus(str, Enum):
    QUOTED = "quoted"
    NOT_QUOTED = "not_quoted"      # the supplier simply did not price this line
    NEEDS_REVIEW = "needs_review"
    CONFLICT = "conflict"
    UNRESOLVED = "unresolved"      # quoted, but we cannot state it comparably


class MatchStatus(str, Enum):
    MATCHED = "matched"              # unambiguous
    PROBABLE_MATCH = "probable_match"  # likely, shown for confirmation
    UNMATCHED = "unmatched"          # no RFQ line we are willing to claim
    CONFLICT = "conflict"            # two supplier lines claim the same RFQ line


class PriceBasis(str, Enum):
    PER_UNIT = "per_unit"
    PER_100 = "per_100"
    PER_1000 = "per_1000"
    PER_SET = "per_set"
    PER_KG = "per_kg"
    PER_LOT = "per_lot"
    UNKNOWN = "unknown"


#: How many priced items one quoted price covers. Only bases with a known count can be
#: divided down to a per-piece figure; the rest need information we do not have.
PRICE_BASIS_DIVISOR = {
    PriceBasis.PER_UNIT: 1.0,
    PriceBasis.PER_100: 100.0,
    PriceBasis.PER_1000: 1000.0,
}


class NormalizationStatus(str, Enum):
    NORMALIZED = "normalized"    # safely divided down to a per-piece price
    UNRESOLVED = "unresolved"    # e.g. per-kg without a weight per piece
    NOT_APPLICABLE = "not_applicable"


class ClaimStatus(str, Enum):
    """Applies to questionnaire answers and certifications alike."""
    VERIFIED = "verified"            # supporting document present
    CLAIMED = "claimed"              # the supplier says so, nothing attached
    MISSING = "missing"              # never addressed
    FAILED = "failed"
    EXPIRED = "expired"
    NOT_APPLICABLE = "not_applicable"
    CONFLICT = "conflict"


class ValueSource(str, Enum):
    SUPPLIER_STATED = "supplier_stated"    # written in the document
    SUPPLIER_REVISED = "supplier_revised"  # written in a later revision
    BUYER_CORRECTED = "buyer_corrected"    # a human fixed it
    AI_INFERRED = "ai_inferred"            # never outranks the above
    MISSING = "missing"


#: Precedence when the same fact arrives more than once (spec §27). Lower wins.
SOURCE_RANK = {
    ValueSource.BUYER_CORRECTED: 0,
    ValueSource.SUPPLIER_REVISED: 1,
    ValueSource.SUPPLIER_STATED: 2,
    ValueSource.AI_INFERRED: 3,
    ValueSource.MISSING: 4,
}


# --------------------------------------------------------------------------- #
# Evidence and documents
# --------------------------------------------------------------------------- #
@dataclass
class Evidence:
    """Where a value physically came from. Never synthesised: an Evidence row only
    exists when the quoted text was actually found in the source document."""
    id: str = field(default_factory=lambda: new_id("ev"))
    response_id: str = ""
    document_id: str = ""
    document_name: str = ""
    source_type: str = ""              # xlsx | pdf | docx | txt | image
    page: Optional[int] = None
    sheet: Optional[str] = None
    row: Optional[int] = None
    cell: Optional[str] = None
    paragraph: Optional[int] = None
    location: str = ""                 # human-readable, e.g. "Sheet Quotation · cell D7"
    quoted_text: str = ""
    verified: bool = False             # the quoted text was found in the document
    created_at: str = field(default_factory=utc_now)

    def describe(self) -> str:
        """Document and position, without repeating the filename the model already cited."""
        name, loc = (self.document_name or "").strip(), (self.location or "").strip()
        if not loc:
            return name
        if not name or name.lower() in loc.lower():
            return loc
        return "%s · %s" % (name, loc)

    def to_dict(self) -> Dict[str, Any]:
        return _dc_to_dict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Evidence":
        return cls(
            id=str(d.get("id") or new_id("ev")),
            response_id=str(d.get("response_id", "") or ""),
            document_id=str(d.get("document_id", "") or ""),
            document_name=str(d.get("document_name", "") or ""),
            source_type=str(d.get("source_type", "") or ""),
            page=d.get("page"), sheet=d.get("sheet"), row=d.get("row"),
            cell=d.get("cell"), paragraph=d.get("paragraph"),
            location=str(d.get("location", "") or ""),
            quoted_text=str(d.get("quoted_text", "") or ""),
            verified=bool(d.get("verified", False)),
            created_at=str(d.get("created_at") or utc_now()),
        )


@dataclass
class SourceDocument:
    """The supplier's actual file. Kept as the source of truth behind every extraction."""
    id: str = field(default_factory=lambda: new_id("doc"))
    response_id: str = ""
    filename: str = ""
    path: str = ""
    media_type: str = ""               # xlsx | pdf | docx | txt | image
    byte_size: int = 0
    extraction_method: str = ""        # how the raw text was obtained
    extraction_status: ExtractionStatus = ExtractionStatus.PENDING
    extraction_note: str = ""
    raw_text: str = ""                 # what the DocumentExtractor produced
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> Dict[str, Any]:
        return _dc_to_dict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SourceDocument":
        return cls(
            id=str(d.get("id") or new_id("doc")),
            response_id=str(d.get("response_id", "") or ""),
            filename=str(d.get("filename", "") or ""),
            path=str(d.get("path", "") or ""),
            media_type=str(d.get("media_type", "") or ""),
            byte_size=int(d.get("byte_size") or 0),
            extraction_method=str(d.get("extraction_method", "") or ""),
            extraction_status=_enum(ExtractionStatus, d.get("extraction_status"), ExtractionStatus.PENDING),
            extraction_note=str(d.get("extraction_note", "") or ""),
            raw_text=str(d.get("raw_text", "") or ""),
            created_at=str(d.get("created_at") or utc_now()),
        )


# --------------------------------------------------------------------------- #
# Supplier
# --------------------------------------------------------------------------- #
@dataclass
class Supplier:
    id: str = field(default_factory=lambda: new_id("sup"))
    name: str = ""
    country: str = ""
    contact_name: str = ""
    contact_email: str = ""            # fabricated demo data; nothing is ever sent
    status: SupplierStatus = SupplierStatus.INVITED
    note: str = ""
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> Dict[str, Any]:
        return _dc_to_dict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Supplier":
        return cls(
            id=str(d.get("id") or new_id("sup")),
            name=str(d.get("name", "") or ""),
            country=str(d.get("country", "") or ""),
            contact_name=str(d.get("contact_name", "") or ""),
            contact_email=str(d.get("contact_email", "") or ""),
            status=_enum(SupplierStatus, d.get("status"), SupplierStatus.INVITED),
            note=str(d.get("note", "") or ""),
            created_at=str(d.get("created_at") or utc_now()),
        )


# --------------------------------------------------------------------------- #
# Quotes
# --------------------------------------------------------------------------- #
@dataclass
class Discount:
    """Kept apart from the price, because a discount without its condition is a lie."""
    percent: Optional[float] = None
    amount: Optional[float] = None
    condition: str = ""                # e.g. "total order quantity above 10,000 pcs"
    applies: Optional[bool] = None     # None = we could not evaluate the condition
    applies_reason: str = ""
    evidence_ids: List[str] = field(default_factory=list)

    @property
    def is_present(self) -> bool:
        return self.percent is not None or self.amount is not None

    def describe(self) -> str:
        if self.percent is not None:
            head = "%g%% off" % self.percent
        elif self.amount is not None:
            head = "%g off" % self.amount
        else:
            return ""
        return "%s when %s" % (head, self.condition) if self.condition else head

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Discount":
        d = d or {}
        return cls(percent=d.get("percent"), amount=d.get("amount"),
                   condition=str(d.get("condition", "") or ""), applies=d.get("applies"),
                   applies_reason=str(d.get("applies_reason", "") or ""),
                   evidence_ids=list(d.get("evidence_ids") or []))


@dataclass
class SupplierQuote:
    """One supplier's price for one RFQ line.

    The supplier's own words are preserved in ``unit_price`` + ``quoted_unit`` +
    ``price_basis``; ``normalized_unit_price`` is only filled when dividing down is
    mathematically safe, and carries its own status when it is not.
    """
    id: str = field(default_factory=lambda: new_id("q"))
    response_id: str = ""
    rfq_id: str = ""
    supplier_id: str = ""
    line_item_id: Optional[str] = None        # the RFQ line, once matched

    # what the supplier literally said
    supplier_line_label: str = ""             # how the supplier referred to it
    quoted_quantity: Optional[float] = None
    quoted_unit: str = ""
    unit_price: Optional[float] = None
    currency: str = ""
    price_basis: PriceBasis = PriceBasis.UNKNOWN
    price_is_indicative: bool = False         # "approximately", "around", "ballpark"

    # derived, never destructive
    discount: Discount = field(default_factory=Discount)
    effective_unit_price: Optional[float] = None      # after an applicable discount
    normalized_unit_price: Optional[float] = None     # per single piece
    normalization_status: NormalizationStatus = NormalizationStatus.NOT_APPLICABLE
    normalization_note: str = ""

    # commercial terms that ride with the line
    minimum_order_quantity: Optional[float] = None
    moq_unit: str = ""
    moq_constraint: bool = False              # RFQ quantity is below the supplier's MOQ
    lead_time_text: str = ""                  # verbatim, e.g. "20-25 working days after artwork approval"
    lead_time_days: Optional[float] = None    # only when unambiguous
    lead_time_is_interpreted: bool = False    # True when days were derived from a range
    delivery_terms: str = ""
    payment_terms: str = ""
    quote_validity_text: str = ""
    quote_validity_days: Optional[float] = None
    quote_validity_is_conditional: bool = False   # "subject to material prices"

    # trust
    status: QuoteStatus = QuoteStatus.QUOTED
    match_status: MatchStatus = MatchStatus.UNMATCHED
    match_reason: str = ""
    match_candidates: List[str] = field(default_factory=list)   # rival line ids
    value_source: ValueSource = ValueSource.SUPPLIER_STATED
    confidence: Optional[float] = None
    evidence_ids: List[str] = field(default_factory=list)
    conflicts: List[Dict[str, Any]] = field(default_factory=list)
    issues: List[str] = field(default_factory=list)
    history: List[Dict[str, Any]] = field(default_factory=list)   # corrections and revisions
    created_at: str = field(default_factory=utc_now)

    # -- display helpers ----------------------------------------------------
    @property
    def has_price(self) -> bool:
        return self.unit_price is not None

    def original_price_text(self) -> str:
        if self.unit_price is None:
            return ""
        basis = {
            PriceBasis.PER_UNIT: "per %s" % (self.quoted_unit or "pc"),
            PriceBasis.PER_100: "per 100 %s" % (self.quoted_unit or "pcs"),
            PriceBasis.PER_1000: "per 1,000 %s" % (self.quoted_unit or "pcs"),
            PriceBasis.PER_SET: "per set",
            PriceBasis.PER_KG: "per kg",
            PriceBasis.PER_LOT: "per lot",
        }.get(self.price_basis, "")
        return ("%s %g %s" % (self.currency, self.unit_price, basis)).strip()

    def normalized_price_text(self) -> str:
        if self.normalized_unit_price is None:
            return ""
        return "%s %.4f" % (self.currency, self.normalized_unit_price)

    def to_dict(self) -> Dict[str, Any]:
        return _dc_to_dict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SupplierQuote":
        return cls(
            id=str(d.get("id") or new_id("q")),
            response_id=str(d.get("response_id", "") or ""),
            rfq_id=str(d.get("rfq_id", "") or ""),
            supplier_id=str(d.get("supplier_id", "") or ""),
            line_item_id=d.get("line_item_id"),
            supplier_line_label=str(d.get("supplier_line_label", "") or ""),
            quoted_quantity=d.get("quoted_quantity"),
            quoted_unit=str(d.get("quoted_unit", "") or ""),
            unit_price=d.get("unit_price"),
            currency=str(d.get("currency", "") or ""),
            price_basis=_enum(PriceBasis, d.get("price_basis"), PriceBasis.UNKNOWN),
            price_is_indicative=bool(d.get("price_is_indicative", False)),
            discount=Discount.from_dict(d.get("discount") or {}),
            effective_unit_price=d.get("effective_unit_price"),
            normalized_unit_price=d.get("normalized_unit_price"),
            normalization_status=_enum(NormalizationStatus, d.get("normalization_status"), NormalizationStatus.NOT_APPLICABLE),
            normalization_note=str(d.get("normalization_note", "") or ""),
            minimum_order_quantity=d.get("minimum_order_quantity"),
            moq_unit=str(d.get("moq_unit", "") or ""),
            moq_constraint=bool(d.get("moq_constraint", False)),
            lead_time_text=str(d.get("lead_time_text", "") or ""),
            lead_time_days=d.get("lead_time_days"),
            lead_time_is_interpreted=bool(d.get("lead_time_is_interpreted", False)),
            delivery_terms=str(d.get("delivery_terms", "") or ""),
            payment_terms=str(d.get("payment_terms", "") or ""),
            quote_validity_text=str(d.get("quote_validity_text", "") or ""),
            quote_validity_days=d.get("quote_validity_days"),
            quote_validity_is_conditional=bool(d.get("quote_validity_is_conditional", False)),
            status=_enum(QuoteStatus, d.get("status"), QuoteStatus.QUOTED),
            match_status=_enum(MatchStatus, d.get("match_status"), MatchStatus.UNMATCHED),
            match_reason=str(d.get("match_reason", "") or ""),
            match_candidates=list(d.get("match_candidates") or []),
            value_source=_enum(ValueSource, d.get("value_source"), ValueSource.SUPPLIER_STATED),
            confidence=d.get("confidence"),
            evidence_ids=list(d.get("evidence_ids") or []),
            conflicts=list(d.get("conflicts") or []),
            issues=list(d.get("issues") or []),
            history=list(d.get("history") or []),
            created_at=str(d.get("created_at") or utc_now()),
        )


# --------------------------------------------------------------------------- #
# Questionnaire and certifications
# --------------------------------------------------------------------------- #
@dataclass
class QuestionnaireResponse:
    id: str = field(default_factory=lambda: new_id("qa"))
    response_id: str = ""
    question_id: str = ""              # the RFQ Question.id
    field_key: str = ""                # the RFQ field it answers
    question_text: str = ""
    answer: str = ""
    status: ClaimStatus = ClaimStatus.MISSING
    value_source: ValueSource = ValueSource.SUPPLIER_STATED
    evidence_ids: List[str] = field(default_factory=list)
    confidence: Optional[float] = None
    note: str = ""
    history: List[Dict[str, Any]] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> Dict[str, Any]:
        return _dc_to_dict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "QuestionnaireResponse":
        return cls(
            id=str(d.get("id") or new_id("qa")),
            response_id=str(d.get("response_id", "") or ""),
            question_id=str(d.get("question_id", "") or ""),
            field_key=str(d.get("field_key", "") or ""),
            question_text=str(d.get("question_text", "") or ""),
            answer=str(d.get("answer", "") or ""),
            status=_enum(ClaimStatus, d.get("status"), ClaimStatus.MISSING),
            value_source=_enum(ValueSource, d.get("value_source"), ValueSource.SUPPLIER_STATED),
            evidence_ids=list(d.get("evidence_ids") or []),
            confidence=d.get("confidence"),
            note=str(d.get("note", "") or ""),
            history=list(d.get("history") or []),
            created_at=str(d.get("created_at") or utc_now()),
        )


@dataclass
class Certification:
    """A certification a supplier mentions. CLAIMED until a document backs it up."""
    id: str = field(default_factory=lambda: new_id("cert"))
    response_id: str = ""
    name: str = ""                     # canonical where possible, e.g. "ISO 9001"
    raw_name: str = ""                 # exactly what the supplier wrote
    status: ClaimStatus = ClaimStatus.CLAIMED
    certificate_number: str = ""
    issuing_body: str = ""
    expiry_date: str = ""
    document_reference: str = ""       # the attachment they referred to, if any
    document_id: Optional[str] = None  # a document we actually hold
    evidence_ids: List[str] = field(default_factory=list)
    confidence: Optional[float] = None
    note: str = ""
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> Dict[str, Any]:
        return _dc_to_dict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Certification":
        return cls(
            id=str(d.get("id") or new_id("cert")),
            response_id=str(d.get("response_id", "") or ""),
            name=str(d.get("name", "") or ""),
            raw_name=str(d.get("raw_name", "") or ""),
            status=_enum(ClaimStatus, d.get("status"), ClaimStatus.CLAIMED),
            certificate_number=str(d.get("certificate_number", "") or ""),
            issuing_body=str(d.get("issuing_body", "") or ""),
            expiry_date=str(d.get("expiry_date", "") or ""),
            document_reference=str(d.get("document_reference", "") or ""),
            document_id=d.get("document_id"),
            evidence_ids=list(d.get("evidence_ids") or []),
            confidence=d.get("confidence"),
            note=str(d.get("note", "") or ""),
            created_at=str(d.get("created_at") or utc_now()),
        )


@dataclass
class SupplierQuestion:
    """Something the supplier asked us. Stays unresolved until the buyer answers."""
    id: str = field(default_factory=lambda: new_id("sq"))
    response_id: str = ""
    question: str = ""
    related_field_key: str = ""
    related_line_item_id: Optional[str] = None
    resolved: bool = False
    buyer_answer: str = ""
    evidence_ids: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> Dict[str, Any]:
        return _dc_to_dict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SupplierQuestion":
        return cls(
            id=str(d.get("id") or new_id("sq")),
            response_id=str(d.get("response_id", "") or ""),
            question=str(d.get("question", "") or ""),
            related_field_key=str(d.get("related_field_key", "") or ""),
            related_line_item_id=d.get("related_line_item_id"),
            resolved=bool(d.get("resolved", False)),
            buyer_answer=str(d.get("buyer_answer", "") or ""),
            evidence_ids=list(d.get("evidence_ids") or []),
            created_at=str(d.get("created_at") or utc_now()),
        )


# --------------------------------------------------------------------------- #
# The response itself
# --------------------------------------------------------------------------- #
@dataclass
class SupplierResponse:
    id: str = field(default_factory=lambda: new_id("resp"))
    rfq_id: str = ""
    supplier_id: str = ""
    response_type: ResponseType = ResponseType.QUOTE_RECEIVED
    received_at: str = field(default_factory=utc_now)
    subject: str = ""
    raw_content: str = ""              # the combined text we actually fed to the model
    status: str = "received"
    extraction_status: ExtractionStatus = ExtractionStatus.PENDING
    extraction_note: str = ""
    revises_response_id: Optional[str] = None   # set when this supersedes an earlier one
    superseded_by_id: Optional[str] = None
    is_active: bool = True             # the response whose quotes are currently in force
    uncertainties: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> Dict[str, Any]:
        return _dc_to_dict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SupplierResponse":
        return cls(
            id=str(d.get("id") or new_id("resp")),
            rfq_id=str(d.get("rfq_id", "") or ""),
            supplier_id=str(d.get("supplier_id", "") or ""),
            response_type=_enum(ResponseType, d.get("response_type"), ResponseType.QUOTE_RECEIVED),
            received_at=str(d.get("received_at") or utc_now()),
            subject=str(d.get("subject", "") or ""),
            raw_content=str(d.get("raw_content", "") or ""),
            status=str(d.get("status", "received") or "received"),
            extraction_status=_enum(ExtractionStatus, d.get("extraction_status"), ExtractionStatus.PENDING),
            extraction_note=str(d.get("extraction_note", "") or ""),
            revises_response_id=d.get("revises_response_id"),
            superseded_by_id=d.get("superseded_by_id"),
            is_active=bool(d.get("is_active", True)),
            uncertainties=list(d.get("uncertainties") or []),
            created_at=str(d.get("created_at") or utc_now()),
            updated_at=str(d.get("updated_at") or utc_now()),
        )


def unresolved_conflicts(quote: "SupplierQuote") -> List[Dict[str, Any]]:
    """The contradictions on a quote that nobody has settled yet.

    A resolution records which stated value applies. It never edits or deletes the other
    side: the buyer decided, and the evidence for both readings stays queryable.
    """
    return [c for c in (quote.conflicts or []) if not (c.get("resolution") or {}).get("value")]


@dataclass
class ResponseBundle:
    """A response with everything extracted from it, for services and the UI to pass around."""
    response: SupplierResponse
    supplier: Optional[Supplier] = None
    documents: List[SourceDocument] = field(default_factory=list)
    quotes: List[SupplierQuote] = field(default_factory=list)
    questionnaire: List[QuestionnaireResponse] = field(default_factory=list)
    certifications: List[Certification] = field(default_factory=list)
    questions: List[SupplierQuestion] = field(default_factory=list)
    evidence: Dict[str, Evidence] = field(default_factory=dict)

    # -- review counters used by the response review screen -----------------
    def quoted_line_ids(self) -> List[str]:
        return [q.line_item_id for q in self.quotes if q.line_item_id and q.has_price]

    def issue_counts(self) -> Dict[str, int]:
        counts = {
            "unmatched": len([q for q in self.quotes if q.match_status in (MatchStatus.UNMATCHED, MatchStatus.CONFLICT)]),
            "probable_matches": len([q for q in self.quotes if q.match_status == MatchStatus.PROBABLE_MATCH]),
            # A contradiction the buyer has settled is no longer open. Both stated values
            # and their evidence stay on the quote; what changes is that someone decided.
            "conflicts": len([q for q in self.quotes
                              if q.status == QuoteStatus.CONFLICT and unresolved_conflicts(q)]) +
                         len([q for q in self.questionnaire if q.status == ClaimStatus.CONFLICT]),
            "unresolved_prices": len([q for q in self.quotes if q.normalization_status == NormalizationStatus.UNRESOLVED]),
            "unverified_claims": len([c for c in self.certifications if c.status == ClaimStatus.CLAIMED]),
            "questions": len([q for q in self.questions if not q.resolved]),
            "moq_constraints": len([q for q in self.quotes if q.moq_constraint]),
        }
        return {k: v for k, v in counts.items() if v}

    def needs_review(self) -> bool:
        blocking = ("unmatched", "probable_matches", "conflicts", "unresolved_prices")
        return any(k in self.issue_counts() for k in blocking)
