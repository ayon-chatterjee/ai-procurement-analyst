"""The vocabulary of the procurement analyst.

Phase 3's governing rule is that the model is not the database. Claude turns a buyer's
question into one of these `AnalystQuery` objects and later narrates a result the
application has already calculated; it never produces a figure. Everything here exists to
make that boundary enforceable: a controlled set of intents, a controlled set of filter
fields and operators, and a result shape that carries its own assumptions, exclusions and
evidence so an answer can always be audited.

Nothing in this module reads the database, calls the model, or knows about Streamlit.
"""
from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, FrozenSet, List, Optional

from .schema import new_id, utc_now


class Intent(str, Enum):
    """What kind of question this is. Anything outside the list is refused rather than
    guessed at, so the analyst can never answer confidently from a misread question."""

    #: The general reader: project any whitelisted field over suppliers, lines or quotes.
    #: Most open-ended questions ("who quotes FOB Shanghai?") land here.
    LOOKUP = "lookup"

    COMPARE_PRICES = "compare_prices"
    CHEAPEST_BY_LINE = "cheapest_by_line"
    PRICE_DIFFERENCE = "price_difference"
    SUPPLIER_COVERAGE = "supplier_coverage"
    LINE_COVERAGE = "line_coverage"
    MISSING_QUOTES = "missing_quotes"
    LEAD_TIME_COMPARISON = "lead_time_comparison"
    MOQ_CHECK = "moq_check"
    QUOTE_VALIDITY = "quote_validity"
    UNRESOLVED_ISSUES = "unresolved_issues"
    QUALIFICATION_STATUS = "qualification_status"
    SUPPLIER_SUMMARY = "supplier_summary"
    WHY_EXCLUDED = "why_excluded"
    EVIDENCE_LOOKUP = "evidence_lookup"
    RFQ_COMPLETENESS = "rfq_completeness"

    #: The question cannot be answered from this dataset at all.
    UNSUPPORTED = "unsupported"


INTENTS = [i.value for i in Intent]


class Grain(str, Enum):
    """What one row of a lookup stands for."""
    SUPPLIER = "supplier"        # one row per supplier
    LINE = "line"                # one row per RFQ line
    QUOTE = "quote"              # one row per (line, supplier) cell


GRAINS = [g.value for g in Grain]


# --------------------------------------------------------------------------- #
# Lookup fields
#
# A lookup projects fields the dataset actually holds. The whitelist is the safety
# property: the model picks from it, so it can never name a column we would have to
# invent, and every field below maps to a stored value plus, where one exists, the
# evidence id that supports it.
# --------------------------------------------------------------------------- #

#: Fields describing a supplier and its response as a whole.
SUPPLIER_FIELDS = [
    "supplier", "country", "response_status", "response_type", "received_at",
    "currencies", "lead_time", "lead_time_days", "moq", "validity", "payment_terms",
    "delivery_terms", "certifications", "qualification", "lines_quoted",
    "open_questions", "issues", "documents",
]

#: Fields describing one quoted line.
QUOTE_FIELDS = [
    "line", "item", "qty", "unit", "price", "as_quoted", "rate", "state", "match_status",
    "normalization_note", "moq_fits", "discount", "evidence", "supplier_line_label",
]

#: `questionnaire.<field_key>` is resolved against the RFQ's own questions at validation
#: time, so a lookup can ask about whatever this RFQ happened to ask suppliers.
QUESTIONNAIRE_PREFIX = "questionnaire."

LOOKUP_FIELDS = SUPPLIER_FIELDS + QUOTE_FIELDS

#: Which grains a field can be read at. A price only means something for one line;
#: a payment term is stated once for the whole response.
FIELD_GRAINS: Dict[str, FrozenSet[str]] = {}
for _f in SUPPLIER_FIELDS:
    FIELD_GRAINS[_f] = frozenset({Grain.SUPPLIER.value, Grain.QUOTE.value})
for _f in QUOTE_FIELDS:
    FIELD_GRAINS[_f] = frozenset({Grain.LINE.value, Grain.QUOTE.value})
FIELD_GRAINS["line"] = frozenset({Grain.LINE.value, Grain.QUOTE.value})
FIELD_GRAINS["supplier"] = frozenset({Grain.SUPPLIER.value, Grain.QUOTE.value})


class FilterField(str, Enum):
    SUPPLIER = "supplier"
    LINE = "line"
    CURRENCY = "currency"
    COUNTRY = "country"
    CERTIFICATION = "certification"
    QUESTIONNAIRE = "questionnaire"
    ELIGIBILITY = "eligibility"
    LEAD_TIME_DAYS = "lead_time_days"
    UNIT_PRICE = "unit_price"
    MOQ = "moq"
    VALIDITY = "validity"
    QUOTE_STATE = "quote_state"
    PAYMENT_TERMS = "payment_terms"
    DELIVERY_TERMS = "delivery_terms"
    LEAD_TIME_TEXT = "lead_time_text"
    VALIDITY_TEXT = "validity_text"


FILTER_FIELDS = [f.value for f in FilterField]


class FilterOp(str, Enum):
    IN = "in"
    NOT_IN = "not_in"
    LTE = "lte"
    GTE = "gte"
    CONTAINS = "contains"
    NOT_CONTAINS = "not_contains"
    HAS_VERIFIED = "has_verified"
    HAS_CLAIMED = "has_claimed"
    LACKS = "lacks"
    ANSWERED = "answered"
    MISSING = "missing"
    IS = "is"
    FITS = "fits"
    EXCEEDS = "exceeds"
    FIRM = "firm"
    CONDITIONAL = "conditional"


FILTER_OPS = [o.value for o in FilterOp]

#: Where a filter bites. A supplier-scope filter removes a whole supplier from the
#: answer; a cell-scope filter removes individual quotes. The distinction matters for
#: how an exclusion is explained.
SUPPLIER_SCOPE = "supplier"
CELL_SCOPE = "cell"
LINE_SCOPE = "line"


@dataclass(frozen=True)
class FilterRule:
    ops: FrozenSet[str]
    scope: str
    value_kind: str          # "text" | "number" | "none" | "enum"
    min_values: int
    max_values: int
    choices: FrozenSet[str] = frozenset()


def _rule(ops, scope, kind, lo, hi, choices=()) -> FilterRule:
    return FilterRule(frozenset(ops), scope, kind, lo, hi, frozenset(choices))


#: Eligibility values the analyst understands. See `analyst_calculations.assess_supplier`.
ELIGIBILITY_VALUES = ["cleared", "not_cleared", "unverified", "not_assessed"]

FILTER_RULES: Dict[str, FilterRule] = {
    FilterField.SUPPLIER.value: _rule(["in", "not_in"], SUPPLIER_SCOPE, "text", 1, 20),
    FilterField.LINE.value: _rule(["in", "not_in"], LINE_SCOPE, "text", 1, 40),
    FilterField.CURRENCY.value: _rule(["in", "not_in"], CELL_SCOPE, "text", 1, 10),
    FilterField.COUNTRY.value: _rule(["in", "not_in"], SUPPLIER_SCOPE, "text", 1, 10),
    FilterField.CERTIFICATION.value: _rule(
        ["has_verified", "has_claimed", "lacks"], SUPPLIER_SCOPE, "text", 1, 10),
    FilterField.QUESTIONNAIRE.value: _rule(
        ["answered", "missing"], SUPPLIER_SCOPE, "text", 1, 10),
    FilterField.ELIGIBILITY.value: _rule(
        ["is"], SUPPLIER_SCOPE, "enum", 1, 4, ELIGIBILITY_VALUES),
    FilterField.LEAD_TIME_DAYS.value: _rule(["lte", "gte"], SUPPLIER_SCOPE, "number", 1, 1),
    FilterField.UNIT_PRICE.value: _rule(["lte", "gte"], CELL_SCOPE, "number", 1, 1),
    FilterField.MOQ.value: _rule(["fits", "exceeds"], CELL_SCOPE, "none", 0, 0),
    FilterField.VALIDITY.value: _rule(["firm", "conditional"], SUPPLIER_SCOPE, "none", 0, 0),
    FilterField.QUOTE_STATE.value: _rule(["in", "not_in"], CELL_SCOPE, "text", 1, 8),
    FilterField.PAYMENT_TERMS.value: _rule(
        ["contains", "not_contains"], SUPPLIER_SCOPE, "text", 1, 5),
    FilterField.DELIVERY_TERMS.value: _rule(
        ["contains", "not_contains"], SUPPLIER_SCOPE, "text", 1, 5),
    FilterField.LEAD_TIME_TEXT.value: _rule(
        ["contains", "not_contains"], SUPPLIER_SCOPE, "text", 1, 5),
    FilterField.VALIDITY_TEXT.value: _rule(
        ["contains", "not_contains"], SUPPLIER_SCOPE, "text", 1, 5),
}


@dataclass
class Filter:
    field: str
    op: str
    values: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"field": self.field, "op": self.op, "values": list(self.values)}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Filter":
        return cls(field=str(d.get("field") or ""), op=str(d.get("op") or ""),
                   values=[str(v) for v in (d.get("values") or [])])

    def describe(self) -> str:
        """How the filter is shown to the buyer, in their terms."""
        vals = ", ".join(self.values)
        phrases = {
            "in": "%s is one of %s", "not_in": "%s is not %s",
            "lte": "%s at most %s", "gte": "%s at least %s",
            "contains": "%s mentions %s", "not_contains": "%s does not mention %s",
            "has_verified": "%s verified: %s", "has_claimed": "%s claimed or verified: %s",
            "lacks": "%s does not hold %s", "answered": "%s answered: %s",
            "missing": "%s unanswered: %s", "is": "%s is %s",
            "fits": "%s within the line quantity", "exceeds": "%s above the line quantity",
            "firm": "%s is a fixed period", "conditional": "%s is conditional",
        }
        label = self.field.replace("_", " ")
        template = phrases.get(self.op, "%s %s")
        try:
            return template % (label, vals) if "%s" in template[2:] else template % label
        except TypeError:
            return "%s %s %s" % (label, self.op, vals)


class EvidenceTopic(str, Enum):
    NONE = "none"
    PRICE = "price"
    MOQ = "moq"
    LEAD_TIME = "lead_time"
    VALIDITY = "validity"
    PAYMENT_TERMS = "payment_terms"
    DELIVERY_TERMS = "delivery_terms"
    DISCOUNT = "discount"
    CERTIFICATION = "certification"
    QUESTIONNAIRE = "questionnaire"
    CONFLICT = "conflict"


EVIDENCE_TOPICS = [t.value for t in EvidenceTopic]


@dataclass
class EvidenceTarget:
    supplier: Optional[str] = None
    line: Optional[str] = None
    topic: str = EvidenceTopic.NONE.value
    name: Optional[str] = None        # cert name, questionnaire field_key or conflict topic

    def is_set(self) -> bool:
        return self.topic != EvidenceTopic.NONE.value

    def to_dict(self) -> Dict[str, Any]:
        return {"supplier": self.supplier, "line": self.line, "topic": self.topic, "name": self.name}

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "EvidenceTarget":
        d = d or {}
        return cls(supplier=d.get("supplier") or None, line=d.get("line") or None,
                   topic=str(d.get("topic") or EvidenceTopic.NONE.value),
                   name=d.get("name") or None)


@dataclass
class Hypothetical:
    """Trust decisions the buyer may suspend while exploring.

    Each one corresponds to something Phase 2 refused to assert and that the real world
    could still settle — a certificate could be produced, a match confirmed, an MOQ
    negotiated. None of them can invent a price, a rate or a supplier, and none of them
    writes anything: a what-if only changes which stored rows are counted.
    """
    exclude_supplier_ids: List[str] = field(default_factory=list)
    treat_claimed_as_verified: bool = False
    ignore_moq_constraints: bool = False
    include_probable_matches: bool = False

    def is_active(self) -> bool:
        return bool(self.exclude_supplier_ids) or self.treat_claimed_as_verified \
            or self.ignore_moq_constraints or self.include_probable_matches

    def labels(self, names: Optional[Dict[str, str]] = None) -> List[str]:
        names = names or {}
        out = []
        if self.exclude_supplier_ids:
            out.append("excluding " + ", ".join(names.get(i, i) for i in self.exclude_supplier_ids))
        if self.treat_claimed_as_verified:
            out.append("treating claimed certifications as if they were verified")
        if self.ignore_moq_constraints:
            out.append("ignoring minimum order quantities")
        if self.include_probable_matches:
            out.append("accepting unconfirmed line matches")
        return out

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "Hypothetical":
        d = d or {}
        ids = d.get("exclude_supplier_ids")
        if ids is None:
            ids = d.get("exclude_suppliers") or []      # the model's spelling
        return cls(exclude_supplier_ids=[str(i) for i in ids],
                   treat_claimed_as_verified=bool(d.get("treat_claimed_as_verified")),
                   ignore_moq_constraints=bool(d.get("ignore_moq_constraints")),
                   include_probable_matches=bool(d.get("include_probable_matches")))

    def union(self, other: "Hypothetical") -> "Hypothetical":
        """Carry a previous what-if forward when the buyer refines it."""
        ids = list(self.exclude_supplier_ids)
        for i in other.exclude_supplier_ids:
            if i not in ids:
                ids.append(i)
        return Hypothetical(
            exclude_supplier_ids=ids,
            treat_claimed_as_verified=self.treat_claimed_as_verified or other.treat_claimed_as_verified,
            ignore_moq_constraints=self.ignore_moq_constraints or other.ignore_moq_constraints,
            include_probable_matches=self.include_probable_matches or other.include_probable_matches)


# --------------------------------------------------------------------------- #
# What each intent is allowed to carry
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class IntentRule:
    filters: FrozenSet[str]
    subjects: str = "none"        # none | optional | one | one_or_two
    lines: str = "none"           # none | optional | one
    hypothetical: bool = False
    currency: bool = False
    top_n: bool = False
    evidence_target: bool = False
    fields: bool = False          # honours query.fields / grain / sort (lookup only)


_PRICE_FILTERS = frozenset([
    "supplier", "line", "currency", "country", "certification", "questionnaire",
    "eligibility", "moq", "quote_state", "unit_price", "payment_terms", "delivery_terms",
])
_ALL_FILTERS = frozenset(FILTER_FIELDS)

INTENT_RULES: Dict[str, IntentRule] = {
    Intent.LOOKUP.value: IntentRule(
        _ALL_FILTERS, subjects="optional", lines="optional", hypothetical=True,
        currency=True, top_n=True, fields=True),
    Intent.COMPARE_PRICES.value: IntentRule(
        _PRICE_FILTERS, lines="optional", hypothetical=True, currency=True),
    Intent.CHEAPEST_BY_LINE.value: IntentRule(
        _PRICE_FILTERS | frozenset(["lead_time_days", "validity"]),
        lines="optional", hypothetical=True, currency=True, top_n=True),
    Intent.PRICE_DIFFERENCE.value: IntentRule(
        frozenset(["supplier", "line", "eligibility"]), subjects="one_or_two",
        lines="optional", hypothetical=True, currency=True),
    Intent.SUPPLIER_COVERAGE.value: IntentRule(
        frozenset(["supplier", "line", "eligibility", "certification", "country"]),
        subjects="optional", lines="optional", hypothetical=True, top_n=True),
    Intent.LINE_COVERAGE.value: IntentRule(
        frozenset(["supplier", "line"]), lines="optional", hypothetical=True, top_n=True),
    Intent.MISSING_QUOTES.value: IntentRule(
        frozenset(["supplier", "line", "quote_state"]), subjects="optional",
        lines="optional", hypothetical=True, top_n=True),
    Intent.LEAD_TIME_COMPARISON.value: IntentRule(
        frozenset(["supplier", "line", "lead_time_days", "eligibility", "lead_time_text"]),
        subjects="optional", lines="optional", hypothetical=True, top_n=True),
    Intent.MOQ_CHECK.value: IntentRule(
        frozenset(["supplier", "line", "moq"]), subjects="optional", lines="optional"),
    Intent.QUOTE_VALIDITY.value: IntentRule(
        frozenset(["supplier", "validity", "validity_text"]), subjects="optional"),
    Intent.UNRESOLVED_ISSUES.value: IntentRule(
        frozenset(["supplier", "line"]), subjects="optional", lines="optional", top_n=True),
    Intent.QUALIFICATION_STATUS.value: IntentRule(
        frozenset(["supplier", "certification", "questionnaire", "eligibility", "country"]),
        subjects="optional", hypothetical=True),
    Intent.SUPPLIER_SUMMARY.value: IntentRule(
        frozenset(["supplier", "eligibility", "country"]), subjects="optional",
        lines="optional", currency=True),
    Intent.WHY_EXCLUDED.value: IntentRule(
        frozenset(["line"]), subjects="one", lines="optional", hypothetical=True, currency=True),
    Intent.EVIDENCE_LOOKUP.value: IntentRule(frozenset(), evidence_target=True),
    Intent.RFQ_COMPLETENESS.value: IntentRule(frozenset()),
    Intent.UNSUPPORTED.value: IntentRule(frozenset()),
}


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #
@dataclass
class RawQuery:
    """Exactly what the model returns: text, unresolved, untrusted."""
    intent: str = Intent.UNSUPPORTED.value
    subject_suppliers: List[str] = field(default_factory=list)
    subject_lines: List[str] = field(default_factory=list)
    filters: List[Filter] = field(default_factory=list)
    hypothetical: Hypothetical = field(default_factory=Hypothetical)
    fields: List[str] = field(default_factory=list)
    grain: str = Grain.SUPPLIER.value
    sort_by: Optional[str] = None
    descending: bool = False
    group_by: Optional[str] = None
    comparison_currency: Optional[str] = None
    top_n: Optional[int] = None
    evidence_target: EvidenceTarget = field(default_factory=EvidenceTarget)
    refines_previous: bool = False
    reading: str = ""
    unsupported_reason: Optional[str] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "RawQuery":
        d = d or {}
        top = d.get("top_n")
        return cls(
            intent=str(d.get("intent") or Intent.UNSUPPORTED.value),
            subject_suppliers=[str(x) for x in (d.get("subject_suppliers") or [])],
            subject_lines=[str(x) for x in (d.get("subject_lines") or [])],
            filters=[Filter.from_dict(f) for f in (d.get("filters") or [])],
            hypothetical=Hypothetical.from_dict(d.get("hypothetical")),
            fields=[str(x) for x in (d.get("fields") or [])],
            grain=str(d.get("grain") or Grain.SUPPLIER.value),
            sort_by=d.get("sort_by") or None,
            descending=bool(d.get("descending")),
            group_by=d.get("group_by") or None,
            comparison_currency=(d.get("comparison_currency") or None),
            top_n=int(top) if isinstance(top, (int, float)) and not isinstance(top, bool) else None,
            evidence_target=EvidenceTarget.from_dict(d.get("evidence_target")),
            refines_previous=bool(d.get("refines_previous")),
            reading=str(d.get("reading") or ""),
            unsupported_reason=d.get("unsupported_reason") or None)


@dataclass
class AnalystQuery:
    """A validated query. Every id in here exists in the dataset it was checked against."""
    intent: str = Intent.UNSUPPORTED.value
    subject_supplier_ids: List[str] = field(default_factory=list)
    subject_line_ids: List[str] = field(default_factory=list)
    filters: List[Filter] = field(default_factory=list)
    hypothetical: Hypothetical = field(default_factory=Hypothetical)
    fields: List[str] = field(default_factory=list)
    grain: str = Grain.SUPPLIER.value
    sort_by: Optional[str] = None
    descending: bool = False
    group_by: Optional[str] = None
    comparison_currency: Optional[str] = None
    top_n: Optional[int] = None
    evidence_target: Optional[EvidenceTarget] = None
    reading: str = ""
    resolution_notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "intent": self.intent,
            "subject_supplier_ids": list(self.subject_supplier_ids),
            "subject_line_ids": list(self.subject_line_ids),
            "filters": [f.to_dict() for f in self.filters],
            "hypothetical": self.hypothetical.to_dict(),
            "fields": list(self.fields),
            "grain": self.grain,
            "sort_by": self.sort_by,
            "descending": self.descending,
            "group_by": self.group_by,
            "comparison_currency": self.comparison_currency,
            "top_n": self.top_n,
            "evidence_target": self.evidence_target.to_dict() if self.evidence_target else None,
            "reading": self.reading,
            "resolution_notes": list(self.resolution_notes),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AnalystQuery":
        d = d or {}
        et = d.get("evidence_target")
        return cls(
            intent=str(d.get("intent") or Intent.UNSUPPORTED.value),
            subject_supplier_ids=[str(x) for x in (d.get("subject_supplier_ids") or [])],
            subject_line_ids=[str(x) for x in (d.get("subject_line_ids") or [])],
            filters=[Filter.from_dict(f) for f in (d.get("filters") or [])],
            hypothetical=Hypothetical.from_dict(d.get("hypothetical")),
            fields=[str(x) for x in (d.get("fields") or [])],
            grain=str(d.get("grain") or Grain.SUPPLIER.value),
            sort_by=d.get("sort_by") or None,
            descending=bool(d.get("descending")),
            group_by=d.get("group_by") or None,
            comparison_currency=d.get("comparison_currency") or None,
            top_n=d.get("top_n"),
            evidence_target=EvidenceTarget.from_dict(et) if et else None,
            reading=str(d.get("reading") or ""),
            resolution_notes=[str(x) for x in (d.get("resolution_notes") or [])])


@dataclass
class Refusal:
    """The analyst declining to answer, with what it would have needed."""
    reason: str
    candidates: List[str] = field(default_factory=list)
    attempted_intent: str = Intent.UNSUPPORTED.value


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #
@dataclass
class Exclusion:
    """Something left out of an answer, and why. Never silent."""
    supplier_id: str = ""
    supplier_name: str = ""
    line_id: Optional[str] = None
    code: str = ""
    reason: str = ""
    evidence_ids: List[str] = field(default_factory=list)
    hypothetical: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class EvidenceRef:
    """A pointer into the Phase 2 evidence store, resolved for display."""
    evidence_id: str = ""
    supplier_name: str = ""
    line_id: Optional[str] = None
    topic: str = ""
    document_name: str = ""
    location: str = ""
    quoted_text: str = ""
    verified: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class QualificationCheck:
    kind: str = ""               # "certification" | "questionnaire"
    name: str = ""
    required: bool = False
    status: str = ""             # a ClaimStatus value
    passed: bool = False
    counted_by_hypothesis: bool = False
    evidence_ids: List[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class QualificationStatus(str, Enum):
    CLEARED = "cleared"
    UNVERIFIED = "unverified"
    NOT_CLEARED = "not_cleared"
    NOT_ASSESSED = "not_assessed"


@dataclass
class Qualification:
    supplier_id: str = ""
    supplier_name: str = ""
    status: str = QualificationStatus.NOT_ASSESSED.value
    checks: List[QualificationCheck] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    policy: str = "verified"     # "verified" | "claimed_ok"

    def failing_evidence_ids(self) -> List[str]:
        out: List[str] = []
        for c in self.checks:
            if not c.passed:
                out.extend(c.evidence_ids)
        return out

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["checks"] = [c.to_dict() for c in self.checks]
        return d


@dataclass
class AnalystResult:
    """One answer, with everything needed to audit it.

    `summary` is always computed by the application. `explanation` is the model's
    narration of these same numbers and may be absent; the answer never depends on it.
    """
    intent: str = ""
    question: str = ""
    summary: str = ""
    columns: List[str] = field(default_factory=list)
    rows: List[Dict[str, Any]] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    assumptions: List[str] = field(default_factory=list)
    exclusions: List[Exclusion] = field(default_factory=list)
    evidence_refs: List[EvidenceRef] = field(default_factory=list)
    calculation_notes: List[str] = field(default_factory=list)
    hypothetical: bool = False
    hypothetical_labels: List[str] = field(default_factory=list)
    comparison_currency: Optional[str] = None
    rate_provenance: Dict[str, Any] = field(default_factory=dict)
    query: Optional[AnalystQuery] = None
    refused: bool = False
    refusal_reason: str = ""
    explanation: Optional[str] = None
    explanation_status: str = "skipped"     # ok | skipped | rejected: … | failed: …
    duration_ms: int = 0

    #: The sentence the analyst uses when the data cannot answer the question. Fixed
    #: wording so a refusal is always recognisable as one.
    REFUSAL_OPENER = "I can't answer that reliably from the available RFQ data."

    @classmethod
    def refusal(cls, question: str, reason: str, intent: str = Intent.UNSUPPORTED.value,
                candidates: Optional[List[str]] = None) -> "AnalystResult":
        summary = cls.REFUSAL_OPENER
        if reason:
            text = reason.strip().rstrip(".")
            summary += " " + text[:1].upper() + text[1:] + "."
        if candidates:
            summary += " " + "Available: " + ", ".join(candidates[:12])
            if len(candidates) > 12:
                summary += " and %d more" % (len(candidates) - 12)
        return cls(intent=intent, question=question, summary=summary,
                   refused=True, refusal_reason=reason)

    def answer_text(self) -> str:
        """What to show as the direct answer."""
        return self.explanation or self.summary

    def to_dict(self) -> Dict[str, Any]:
        """A plain-data form. Streamlit cannot pickle live dataclasses across a reload,
        so the session holds this rather than the object."""
        return {
            "intent": self.intent, "question": self.question, "summary": self.summary,
            "columns": list(self.columns), "rows": list(self.rows), "metrics": dict(self.metrics),
            "warnings": list(self.warnings), "assumptions": list(self.assumptions),
            "exclusions": [e.to_dict() for e in self.exclusions],
            "evidence_refs": [e.to_dict() for e in self.evidence_refs],
            "calculation_notes": list(self.calculation_notes),
            "hypothetical": self.hypothetical,
            "hypothetical_labels": list(self.hypothetical_labels),
            "comparison_currency": self.comparison_currency,
            "rate_provenance": dict(self.rate_provenance),
            "query": self.query.to_dict() if self.query else None,
            "refused": self.refused, "refusal_reason": self.refusal_reason,
            "explanation": self.explanation, "explanation_status": self.explanation_status,
            "duration_ms": self.duration_ms,
        }

    def to_csv(self) -> str:
        """The calculated rows, exactly as shown."""
        buf = io.StringIO()
        cols = self.columns or (list(self.rows[0].keys()) if self.rows else [])
        writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in self.rows:
            writer.writerow({c: row.get(c, "") for c in cols})
        return buf.getvalue()

    def numbers_shown(self) -> List[float]:
        """Every figure this result puts on screen. The explanation guard checks the
        model's narration against exactly this set."""
        return numbers_in([
            self.rows, self.metrics, self.summary, self.rate_provenance,
            [e.reason for e in self.exclusions], self.warnings, self.assumptions,
            self.calculation_notes,
        ])


#: A "-" only starts a number when nothing word-like precedes it: "LINE-017" holds the
#: number 17, not minus 17. Getting this wrong made a correct explanation look invented.
_NUMBER = re.compile(r"(?<![\w.])-?\d[\d,]*\.?\d*")


def numbers_in(value: Any) -> List[float]:
    """Every number reachable inside a value, walking dicts, lists and strings.

    Shared so that a guard checking model prose against data reads figures exactly the
    way the data does; two implementations would eventually disagree and the disagreement
    would look like the model inventing something.
    """
    found: List[float] = []

    def take(v: Any) -> None:
        if isinstance(v, bool) or v is None:
            return
        if isinstance(v, (int, float)):
            found.append(float(v))
        elif isinstance(v, str):
            for m in _NUMBER.findall(v):
                try:
                    found.append(float(m.replace(",", "")))
                except ValueError:
                    pass
        elif isinstance(v, dict):
            for x in v.values():
                take(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                take(x)

    take(value)
    return found

# --------------------------------------------------------------------------- #
# Conversation and audit
# --------------------------------------------------------------------------- #
@dataclass
class AnalystTurn:
    """One past exchange, used to let a follow-up refine the question before it.

    It carries the *query*, never the result: context must not become a second, stale
    copy of the data.
    """
    question: str = ""
    query: Optional[AnalystQuery] = None
    summary: str = ""
    refused: bool = False
    created_at: str = field(default_factory=utc_now)


@dataclass
class AnalystQueryRecord:
    """The audit row for one question."""
    id: str = field(default_factory=lambda: new_id("aq"))
    rfq_id: str = ""
    question: str = ""
    intent: str = ""
    refused: bool = False
    hypothetical: bool = False
    structured_query: str = "{}"      # JSON
    result_summary: str = ""
    warnings: str = "[]"              # JSON
    assumptions: str = "[]"           # JSON
    comparison_currency: Optional[str] = None
    model: str = ""
    prompt_version: str = ""
    duration_ms: int = 0
    created_at: str = field(default_factory=utc_now)

    @classmethod
    def build(cls, rfq_id: str, result: AnalystResult, model: str = "",
              prompt_version: str = "") -> "AnalystQueryRecord":
        query = result.query.to_dict() if result.query else {}
        return cls(rfq_id=rfq_id, question=result.question, intent=result.intent,
                   refused=result.refused, hypothetical=result.hypothetical,
                   structured_query=json.dumps(query, sort_keys=True),
                   result_summary=result.summary[:2000],
                   warnings=json.dumps(result.warnings),
                   assumptions=json.dumps(result.assumptions),
                   comparison_currency=result.comparison_currency,
                   model=model, prompt_version=prompt_version,
                   duration_ms=result.duration_ms)

    def query_dict(self) -> Dict[str, Any]:
        try:
            return json.loads(self.structured_query)
        except (ValueError, TypeError):
            return {}
