"""The vocabulary of an award and its execution.

Phases 1–3 read, compare and explain. This is where the buyer commits: a supplier per
line, a price they will be invoiced for, a letter that goes out, an order someone
fulfils. Everything here exists to make that commitment defensible afterwards — which is
a different bar from being merely correct.

Three properties the shapes below enforce rather than merely encourage:

* **The buyer's decision outranks the proposal.** A line the buyer chose carries
  `PickSource.BUYER_OVERRIDE` and its reason, and re-seeding steps over it.
* **Approval freezes.** The transition table is data, and `AwardStatus.APPROVED` has no
  edge back to `DRAFT`. Changing an approved award means cancelling it, which leaves the
  history whole.
* **History is appended, never rewritten.** An `ExecutionEvent` carries the state it
  replaced, so the chain replays to the award as it stands.

Nothing here reads the database, calls the model, or knows about Streamlit.
"""
from __future__ import annotations

import csv
import io
import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, FrozenSet, List, Optional

from .analyst_models import EvidenceRef, Exclusion, numbers_in
from .schema import new_id, utc_now

#: Shown wherever a commercial term was never stated. Never blank, never inferred: a
#: purchase order that says "Not provided" is correct; one that says "Net 30" because
#: Net 30 is common is a fabrication.
NOT_PROVIDED = "Not provided"

#: Shown instead of a figure that does not exist. Never 0.
NOT_AVAILABLE = "—"


class AwardStatus(str, Enum):
    DRAFT = "draft"                       # lines being chosen
    REVIEWED = "reviewed"                 # buyer has read the validation
    APPROVED = "approved"                 # lines frozen from here
    READY_TO_EXECUTE = "ready_to_execute"  # letters drafted
    SUPPLIER_NOTIFIED = "supplier_notified"
    ORDER_HANDOFF = "order_handoff"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


AWARD_STATUSES = [s.value for s in AwardStatus]

#: The legal moves, as data. Every status write goes through `AwardService._transition`,
#: which consults this table, so an illegal move is impossible rather than merely
#: discouraged. Note what is absent: APPROVED has no edge back to DRAFT.
TRANSITIONS: Dict[str, FrozenSet[str]] = {
    AwardStatus.DRAFT.value: frozenset({AwardStatus.REVIEWED.value, AwardStatus.CANCELLED.value}),
    AwardStatus.REVIEWED.value: frozenset({AwardStatus.DRAFT.value, AwardStatus.APPROVED.value,
                                           AwardStatus.CANCELLED.value}),
    AwardStatus.APPROVED.value: frozenset({AwardStatus.READY_TO_EXECUTE.value,
                                           AwardStatus.CANCELLED.value}),
    AwardStatus.READY_TO_EXECUTE.value: frozenset({AwardStatus.SUPPLIER_NOTIFIED.value,
                                                   AwardStatus.CANCELLED.value}),
    AwardStatus.SUPPLIER_NOTIFIED.value: frozenset({AwardStatus.ORDER_HANDOFF.value,
                                                    AwardStatus.CANCELLED.value}),
    AwardStatus.ORDER_HANDOFF.value: frozenset({AwardStatus.COMPLETED.value,
                                                AwardStatus.CANCELLED.value}),
    AwardStatus.COMPLETED.value: frozenset(),
    AwardStatus.CANCELLED.value: frozenset(),
}

#: Past this point the award lines, the currency and the thresholds are settled. What may
#: still change is what execution does with them.
FROZEN_FROM = AwardStatus.APPROVED.value

#: Statuses at which the decision itself is still being made.
EDITABLE = frozenset({AwardStatus.DRAFT.value, AwardStatus.REVIEWED.value})


def is_editable(status: str) -> bool:
    return status in EDITABLE


class PickSource(str, Enum):
    CHEAPEST = "cheapest"
    BEST_VALUE = "best_value"
    BUYER_OVERRIDE = "buyer_override"
    NONE = "none"                          # the buyer decided not to award this line


PICK_SOURCES = [p.value for p in PickSource]


class Severity(str, Enum):
    BLOCKING = "blocking"
    WARNING = "warning"
    INFO = "info"


# --------------------------------------------------------------------------- #
# The bars
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AwardThresholds:
    """The quality bar: what a supplier must meet before best value will consider it.

    Deliberately not weights. A score of 87.3 is a number this data cannot support and it
    hides why one supplier beat another; a bar can be stated in a sentence and argued with.

    One thing the buyer sets, and it defaults to the lenient reading. Requiring a
    certificate we physically hold is the stricter, rarer case — most suppliers state one
    and attach nothing — so making it the default meant the screen opened by refusing the
    supplier it had itself just proposed. The buyer turns it on when the certificate
    genuinely matters, and that is recorded.
    """
    require_document_backed_certification: bool = False
    require_firm_validity: bool = True

    def labels(self) -> List[str]:
        out = []
        if self.require_document_backed_certification:
            out.append("a certificate we hold a copy of")
        else:
            out.append("a certificate, held or stated")
        if self.require_firm_validity:
            out.append("a quote that stands for a fixed period rather than a condition")
        return out

    def describe(self) -> str:
        labels = self.labels()
        if len(labels) > 1:
            listed = ", ".join(labels[:-1]) + " and " + labels[-1]
        else:
            listed = labels[0] if labels else "no additional requirement"
        return ("Best value is the lowest comparable price from a supplier with %s." % listed)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "AwardThresholds":
        # Tolerant of an award stored before the lead-time bar was removed: the key is
        # simply ignored rather than failing to load a decision already on the record.
        d = d or {}
        return cls(require_document_backed_certification=bool(
                       d.get("require_document_backed_certification", False)),
                   require_firm_validity=bool(d.get("require_firm_validity", True)))


@dataclass
class BarResult:
    """One bar a supplier failed, in the buyer's terms."""
    supplier_id: str = ""
    supplier_name: str = ""
    bar: str = ""                  # qualification | validity | lead_time
    reason: str = ""
    evidence_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Candidate:
    """A supplier's quote for one line, as an award could take it."""
    supplier_id: str = ""
    supplier_name: str = ""
    amount: Optional[float] = None          # per piece, in the award currency
    native: str = ""                        # as the supplier wrote it
    native_unit_price: Optional[float] = None
    native_currency: str = ""
    rate_note: str = ""
    caveats: List[str] = field(default_factory=list)
    evidence_ids: List[str] = field(default_factory=list)
    quote_id: str = ""
    response_id: str = ""
    qualification: str = ""
    lead_time_days: Optional[float] = None
    validity_is_conditional: bool = False
    #: Whether this supplier meets the quality bar, and why not when it does not. Carried
    #: on the candidate so the comparison behind an ⓘ can explain a pick without asking
    #: the engine the same question a second time.
    meets_bar: bool = False
    bar_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> Optional["Candidate"]:
        if not d:
            return None
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class LineProposal:
    """What the application would propose for one line, and why."""
    line_item_id: str = ""
    line_label: str = ""
    quantity: Optional[float] = None
    unit: str = ""
    cheapest: Optional[Candidate] = None
    best_value: Optional[Candidate] = None
    same_supplier: bool = False
    difference_note: str = ""              # one sentence; "" when they coincide
    absent_reason: str = ""                # why best value is missing, when it is
    bars_failed: List[BarResult] = field(default_factory=list)
    valid_candidates: int = 0
    exclusions: List[Exclusion] = field(default_factory=list)
    #: Every supplier with a usable price on this line, cheapest first — not only the two
    #: picks. Keeping the list is what lets the screen answer "why this one?" with the
    #: whole field rather than an assertion; `propose_award` already computed it and used
    #: to throw it away. Together with `exclusions` it accounts for every supplier.
    candidates: List[Candidate] = field(default_factory=list)

    def candidate(self, source: str) -> Optional[Candidate]:
        if source == PickSource.CHEAPEST.value:
            return self.cheapest
        if source == PickSource.BEST_VALUE.value:
            return self.best_value
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "line_item_id": self.line_item_id, "line_label": self.line_label,
            "quantity": self.quantity, "unit": self.unit,
            "cheapest": self.cheapest.to_dict() if self.cheapest else None,
            "best_value": self.best_value.to_dict() if self.best_value else None,
            "same_supplier": self.same_supplier, "difference_note": self.difference_note,
            "absent_reason": self.absent_reason,
            "bars_failed": [b.to_dict() for b in self.bars_failed],
            "valid_candidates": self.valid_candidates,
            "exclusions": [e.to_dict() for e in self.exclusions],
            "candidates": [c.to_dict() for c in self.candidates],
        }


@dataclass
class AwardProposal:
    """What the application suggests, before the buyer has said anything.

    Never persisted: it is a pure function of the comparison and the thresholds, and a
    stored copy would go stale the moment a quote was corrected.
    """
    rfq_id: str = ""
    currency: Optional[str] = None
    currency_reason: str = ""
    thresholds: AwardThresholds = field(default_factory=AwardThresholds)
    lines: List[LineProposal] = field(default_factory=list)
    assumptions: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    rate_provenance: Dict[str, Any] = field(default_factory=dict)
    supplier_names: Dict[str, str] = field(default_factory=dict)

    def line(self, line_item_id: str) -> Optional[LineProposal]:
        return next((l for l in self.lines if l.line_item_id == line_item_id), None)

    @property
    def lines_with_no_cheapest(self) -> List[str]:
        return [l.line_item_id for l in self.lines if l.cheapest is None]

    @property
    def lines_with_no_best_value(self) -> List[str]:
        return [l.line_item_id for l in self.lines if l.best_value is None]

    def basket(self, source: str) -> Optional[float]:
        """What this proposal would cost, if every line took the named candidate.

        `None` when any line in scope has no such candidate — a total over a subset is a
        different number from a total, and quoting one as the other is how a comparison
        misleads.
        """
        total = 0.0
        for line in self.lines:
            candidate = line.candidate(source)
            if candidate is None or candidate.amount is None or not line.quantity:
                return None
            total += candidate.amount * float(line.quantity)
        return round(total, 2)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rfq_id": self.rfq_id, "currency": self.currency,
            "currency_reason": self.currency_reason,
            "thresholds": self.thresholds.to_dict(),
            "lines": [l.to_dict() for l in self.lines],
            "assumptions": list(self.assumptions), "warnings": list(self.warnings),
            "rate_provenance": dict(self.rate_provenance),
            "supplier_names": dict(self.supplier_names),
        }


# --------------------------------------------------------------------------- #
# The award
# --------------------------------------------------------------------------- #
@dataclass
class AwardLine:
    """One line of the award: who gets it, at what, and on whose say-so."""
    id: str = field(default_factory=lambda: new_id("awl"))
    award_id: str = ""
    line_item_id: str = ""
    line_label: str = ""
    supplier_id: Optional[str] = None
    supplier_name: str = ""
    quote_id: str = ""
    response_id: str = ""
    pick_source: str = PickSource.NONE.value
    unit_price: Optional[float] = None          # award currency, per piece
    native_unit_price: Optional[float] = None
    native_currency: str = ""
    native_text: str = ""                       # as the supplier wrote it
    rate_note: str = ""
    quantity: Optional[float] = None
    unit: str = ""
    extended: Optional[float] = None
    override_reason: str = ""
    caveats: List[str] = field(default_factory=list)
    evidence_ids: List[str] = field(default_factory=list)
    seeded_cheapest: Optional[Dict[str, Any]] = None      # what was proposed, for the record
    seeded_best_value: Optional[Dict[str, Any]] = None
    difference_note: str = ""
    absent_reason: str = ""
    decided_at: str = field(default_factory=utc_now)

    @property
    def awarded(self) -> bool:
        return bool(self.supplier_id) and self.pick_source != PickSource.NONE.value

    @property
    def is_override(self) -> bool:
        return self.pick_source == PickSource.BUYER_OVERRIDE.value

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AwardLine":
        return cls(**{k: v for k, v in (d or {}).items() if k in cls.__dataclass_fields__})


@dataclass
class SupplierSubtotal:
    supplier_id: str = ""
    supplier_name: str = ""
    currency: Optional[str] = None
    subtotal: Optional[float] = None
    native_currency: str = ""
    native_subtotal: Optional[float] = None
    lines: int = 0
    lines_without_extended: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AwardTotals:
    """Money, with its denominator attached.

    A partial total is shown — the buyer is committing to those lines and needs their
    value — but never without saying how many lines it covers.
    """
    currency: Optional[str] = None
    by_supplier: List[SupplierSubtotal] = field(default_factory=list)
    grand_total: Optional[float] = None
    complete: bool = False
    awarded_lines: int = 0
    priced_lines: int = 0
    incomplete_lines: List[str] = field(default_factory=list)
    native_currencies: List[str] = field(default_factory=list)
    rate_provenance: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    def describe(self) -> str:
        if self.grand_total is None:
            return "No total: " + (self.notes[0] if self.notes else "prices are not comparable.")
        text = "%s %s" % (self.currency, "{:,.2f}".format(self.grand_total))
        if not self.complete:
            return "%s across %d of %d awarded lines" % (text, self.priced_lines,
                                                         self.awarded_lines)
        return text

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["by_supplier"] = [s.to_dict() for s in self.by_supplier]
        return d


@dataclass
class Award:
    """A buyer's decision about who gets the business."""
    id: str = field(default_factory=lambda: new_id("awd"))
    rfq_id: str = ""
    rfq_title: str = ""
    status: str = AwardStatus.DRAFT.value
    currency: Optional[str] = None
    currency_reason: str = ""
    thresholds: AwardThresholds = field(default_factory=AwardThresholds)
    lines: List[AwardLine] = field(default_factory=list)
    assumptions: List[str] = field(default_factory=list)
    acknowledged_warnings: List[str] = field(default_factory=list)
    rate_provenance: Dict[str, Any] = field(default_factory=dict)
    validation_at_approval: Dict[str, Any] = field(default_factory=dict)
    cancelled_reason: str = ""
    created_at: str = field(default_factory=utc_now)
    decided_at: str = ""
    approved_at: str = ""
    notified_at: str = ""
    completed_at: str = ""

    # -- derived -----------------------------------------------------------
    @property
    def awarded_lines(self) -> List[AwardLine]:
        return [l for l in self.lines if l.awarded]

    @property
    def supplier_ids(self) -> List[str]:
        out: List[str] = []
        for line in self.awarded_lines:
            if line.supplier_id and line.supplier_id not in out:
                out.append(line.supplier_id)
        return out

    def supplier_name_for(self, supplier_id: str) -> str:
        """The name as it was when the line was awarded, for a message about that supplier."""
        return next((l.supplier_name for l in self.lines
                     if l.supplier_id == supplier_id and l.supplier_name), "")

    def lines_for(self, supplier_id: str) -> List[AwardLine]:
        return [l for l in self.awarded_lines if l.supplier_id == supplier_id]

    def line(self, line_item_id: str) -> Optional[AwardLine]:
        return next((l for l in self.lines if l.line_item_id == line_item_id), None)

    @property
    def overrides(self) -> List[AwardLine]:
        return [l for l in self.lines if l.is_override]

    @property
    def editable(self) -> bool:
        return is_editable(self.status)

    def can_move_to(self, status: str) -> bool:
        return status in TRANSITIONS.get(self.status, frozenset())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "rfq_id": self.rfq_id, "rfq_title": self.rfq_title,
            "status": self.status, "currency": self.currency,
            "currency_reason": self.currency_reason,
            "thresholds": self.thresholds.to_dict(),
            "lines": [l.to_dict() for l in self.lines],
            "assumptions": list(self.assumptions),
            "acknowledged_warnings": list(self.acknowledged_warnings),
            "rate_provenance": dict(self.rate_provenance),
            "validation_at_approval": dict(self.validation_at_approval),
            "cancelled_reason": self.cancelled_reason,
            "created_at": self.created_at, "decided_at": self.decided_at,
            "approved_at": self.approved_at, "notified_at": self.notified_at,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Award":
        d = dict(d or {})
        lines = [AwardLine.from_dict(x) for x in (d.pop("lines", None) or [])]
        thresholds = AwardThresholds.from_dict(d.pop("thresholds", None))
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        award = cls(**known)
        award.lines = lines
        award.thresholds = thresholds
        return award


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
@dataclass
class Finding:
    """One thing the buyer should know before committing.

    `field_read` names the attribute the predicate actually consulted, so a finding can be
    argued with rather than merely believed.
    """
    code: str = ""
    severity: str = Severity.INFO.value
    scope: str = "award"                    # award | line | supplier
    line_item_id: Optional[str] = None
    supplier_id: Optional[str] = None
    supplier_name: str = ""
    message: str = ""
    field_read: str = ""
    evidence_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ValidationReport:
    findings: List[Finding] = field(default_factory=list)
    checked_at: str = field(default_factory=utc_now)

    @property
    def blocking(self) -> List[Finding]:
        return [f for f in self.findings if f.severity == Severity.BLOCKING.value]

    @property
    def warnings(self) -> List[Finding]:
        return [f for f in self.findings if f.severity == Severity.WARNING.value]

    @property
    def infos(self) -> List[Finding]:
        return [f for f in self.findings if f.severity == Severity.INFO.value]

    @property
    def ok_to_execute(self) -> bool:
        return not self.blocking

    def warning_codes(self) -> List[str]:
        out: List[str] = []
        for f in self.warnings:
            if f.code not in out:
                out.append(f.code)
        return out

    def unacknowledged(self, acknowledged: List[str]) -> List[str]:
        done = set(acknowledged or [])
        return [c for c in self.warning_codes() if c not in done]

    def by_line(self) -> Dict[str, List[Finding]]:
        out: Dict[str, List[Finding]] = {}
        for f in self.findings:
            if f.line_item_id:
                out.setdefault(f.line_item_id, []).append(f)
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {"checked_at": self.checked_at,
                "findings": [f.to_dict() for f in self.findings]}


# --------------------------------------------------------------------------- #
# Supplier communication
# --------------------------------------------------------------------------- #
class CommunicationStatus(str, Enum):
    DRAFT = "draft"
    REJECTED = "rejected"          # the guard refused the model's text; fallback shown
    EDITED = "edited"
    APPROVED = "approved"
    SENT = "sent"                  # simulated; nothing leaves this machine
    FAILED = "failed"


@dataclass
class CommunicationLine:
    line_reference: str = ""
    description: str = ""
    quantity: Optional[float] = None
    unit: str = ""
    unit_price: Optional[float] = None
    extended: Optional[float] = None
    as_quoted: str = ""
    native_currency: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CommunicationFacts:
    """Everything the model is allowed to know when drafting one supplier's letter.

    The isolation property lives here, in the shape: there is one `supplier_name`, one
    contact and one list of that supplier's lines. There is no field a rival's price or
    name could occupy, so a leak needs a code change that shows up in review rather than a
    prompt that drifts.
    """
    award_id: str = ""
    supplier_id: str = ""
    supplier_name: str = ""
    supplier_contact_name: str = ""
    supplier_country: str = ""
    buyer_organisation: str = ""
    rfq_reference: str = ""
    rfq_title: str = ""
    currency: Optional[str] = None
    lines: List[CommunicationLine] = field(default_factory=list)
    subtotal: Optional[float] = None
    lead_time_stated: str = NOT_PROVIDED
    payment_terms_stated: str = NOT_PROVIDED
    delivery_terms_stated: str = NOT_PROVIDED
    validity_stated: str = NOT_PROVIDED
    minimum_order_stated: str = NOT_PROVIDED
    open_points: List[str] = field(default_factory=list)
    next_step: str = ""
    suppressed_fields: List[str] = field(default_factory=list)

    def numbers(self) -> List[float]:
        """Every figure a faithful letter may contain.

        Built only from this supplier's pack, which is what makes a rival's price
        detectable: it is caught by the same check that catches an invented one.
        """
        # Every string we hand the model is scanned too, not only the priced fields. The
        # RFQ title of the stress set is "Corrugated Carton Boxes — 30 sizes", and a letter
        # that repeats the title back is quoting us, not inventing a figure.
        return numbers_in([
            [l.to_dict() for l in self.lines], self.subtotal, len(self.lines),
        ] + self.strings())

    def strings(self) -> List[str]:
        out = [self.supplier_name, self.supplier_contact_name, self.buyer_organisation,
               self.rfq_reference, self.rfq_title, self.lead_time_stated,
               self.payment_terms_stated, self.delivery_terms_stated,
               self.validity_stated, self.minimum_order_stated, self.next_step]
        out += [l.description for l in self.lines]
        out += [l.as_quoted for l in self.lines]
        out += list(self.open_points)
        return [s for s in out if s]

    def to_prompt_dict(self) -> Dict[str, Any]:
        return {
            "supplier": {"name": self.supplier_name, "contact": self.supplier_contact_name},
            "buyer": self.buyer_organisation,
            "rfq": {"reference": self.rfq_reference, "title": self.rfq_title},
            "currency": self.currency,
            "awarded_lines": [l.to_dict() for l in self.lines],
            "subtotal": self.subtotal,
            "terms_as_the_supplier_stated_them": {
                "lead_time": self.lead_time_stated,
                "payment": self.payment_terms_stated,
                "delivery": self.delivery_terms_stated,
                "quote_validity": self.validity_stated,
                "minimum_order": self.minimum_order_stated,
            },
            "still_open": list(self.open_points),
            "next_step": self.next_step,
        }

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["lines"] = [l.to_dict() for l in self.lines]
        return d


@dataclass
class SupplierCommunication:
    id: str = field(default_factory=lambda: new_id("acm"))
    award_id: str = ""
    supplier_id: str = ""
    supplier_name: str = ""
    recipient: str = ""                     # the stored contact; nothing is sent to it
    status: str = CommunicationStatus.DRAFT.value
    subject: str = ""
    body: str = ""                          # the model's draft, always kept
    edited_body: str = ""                   # the buyer's, when they changed it
    guard_status: str = "skipped"           # ok | rejected: … | fallback
    rejected_body: str = ""
    #: The same verdict on the buyer's own wording. A buyer may write whatever they like,
    #: but the thing that actually goes out is checked by the check that guards the model,
    #: so hand-pasting a rival's price is caught exactly as inventing one would be.
    edit_status: str = ""
    edit_leaks: List[str] = field(default_factory=list)
    generated_by: str = "deterministic"     # deterministic | claude
    model: str = ""
    prompt_version: str = ""
    duration_ms: int = 0
    facts: Dict[str, Any] = field(default_factory=dict)
    omitted: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)
    edited_at: str = ""
    approved_at: str = ""
    sent_at: str = ""

    @property
    def text(self) -> str:
        """What the buyer would send: their edit if they made one, else the draft."""
        return self.edited_body or self.body

    @property
    def was_edited(self) -> bool:
        return bool(self.edited_body)

    @property
    def sendable(self) -> bool:
        """Whether this is fit to leave the building.

        A model draft that failed is already replaced by the deterministic letter, so the
        only way to hold an unverified message here is for the buyer to have written one.
        """
        return not (self.edit_status or "").startswith("rejected")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SupplierCommunication":
        return cls(**{k: v for k, v in (d or {}).items() if k in cls.__dataclass_fields__})


# --------------------------------------------------------------------------- #
# Order handoff
# --------------------------------------------------------------------------- #
@dataclass
class HandoffLine:
    line_reference: str = ""
    description: str = ""
    quantity: Optional[float] = None
    unit: str = ""
    unit_price: Optional[float] = None
    currency: str = ""
    extended: Optional[float] = None
    as_quoted: str = ""
    evidence_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class OrderHandoff:
    """The structured order, generated from the award with no model involved.

    A document of record has to be reproducible, so nothing here is written by Claude.
    Supplier name and contact are snapshotted rather than joined: this document must not
    silently re-render if a contact changes next month.
    """
    id: str = field(default_factory=lambda: new_id("aoh"))
    award_id: str = ""
    supplier_id: str = ""
    reference: str = ""
    rfq_reference: str = ""
    rfq_title: str = ""
    buyer_organisation: str = ""
    supplier_name: str = ""
    supplier_contact_name: str = ""
    supplier_contact_email: str = ""
    supplier_country: str = ""
    currency: str = ""
    lines: List[HandoffLine] = field(default_factory=list)
    subtotal: Optional[float] = None
    payment_terms: str = NOT_PROVIDED
    delivery_terms: str = NOT_PROVIDED
    lead_time: str = NOT_PROVIDED
    quote_validity: str = NOT_PROVIDED
    minimum_order: str = NOT_PROVIDED
    certifications: List[str] = field(default_factory=list)
    assumptions: List[str] = field(default_factory=list)
    open_points: List[str] = field(default_factory=list)
    source_note: str = ""
    generated_at: str = field(default_factory=utc_now)

    COLUMNS = ["Line", "Description", "Qty", "Unit", "Unit price", "Currency", "Total",
               "As quoted"]

    def rows(self) -> List[Dict[str, Any]]:
        out = []
        for l in self.lines:
            out.append({
                "Line": l.line_reference, "Description": l.description,
                "Qty": l.quantity if l.quantity is not None else NOT_AVAILABLE,
                "Unit": l.unit or NOT_AVAILABLE,
                "Unit price": l.unit_price if l.unit_price is not None else NOT_AVAILABLE,
                "Currency": l.currency or NOT_AVAILABLE,
                "Total": l.extended if l.extended is not None else NOT_AVAILABLE,
                "As quoted": l.as_quoted or NOT_AVAILABLE,
            })
        return out

    def to_csv(self) -> str:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=self.COLUMNS, extrasaction="ignore",
                                lineterminator="\n")
        writer.writeheader()
        for row in self.rows():
            writer.writerow(row)
        return buf.getvalue()

    def to_markdown(self) -> str:
        lines = [
            "# Order handoff %s" % self.reference,
            "",
            "**Supplier**: %s" % self.supplier_name,
            "**Contact**: %s %s" % (self.supplier_contact_name or NOT_PROVIDED,
                                    ("<%s>" % self.supplier_contact_email)
                                    if self.supplier_contact_email else ""),
            "**RFQ**: %s — %s" % (self.rfq_reference, self.rfq_title),
            "**Currency**: %s" % (self.currency or NOT_PROVIDED),
            "**Generated**: %s" % self.generated_at,
            "",
            "| " + " | ".join(self.COLUMNS) + " |",
            "|" + "---|" * len(self.COLUMNS),
        ]
        for row in self.rows():
            lines.append("| " + " | ".join(str(row[c]) for c in self.COLUMNS) + " |")
        lines += [
            "",
            "**Subtotal**: %s" % (("%s %s" % (self.currency, "{:,.2f}".format(self.subtotal)))
                                  if self.subtotal is not None else NOT_AVAILABLE),
            "",
            "## Commercial terms",
            "",
            "- Payment: %s" % self.payment_terms,
            "- Delivery: %s" % self.delivery_terms,
            "- Lead time: %s" % self.lead_time,
            "- Quote validity: %s" % self.quote_validity,
            "- Minimum order: %s" % self.minimum_order,
        ]
        if self.certifications:
            lines += ["", "## Certifications", ""] + ["- %s" % c for c in self.certifications]
        if self.open_points:
            lines += ["", "## Still open", ""] + ["- %s" % p for p in self.open_points]
        if self.assumptions:
            lines += ["", "## Assumptions", ""] + ["- %s" % a for a in self.assumptions]
        lines += ["", "_%s_" % self.source_note]
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["lines"] = [l.to_dict() for l in self.lines]
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "OrderHandoff":
        d = dict(d or {})
        lines = [HandoffLine(**{k: v for k, v in x.items()
                                if k in HandoffLine.__dataclass_fields__})
                 for x in (d.pop("lines", None) or [])]
        handoff = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        handoff.lines = lines
        return handoff


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #
class ExecutionEventType(str, Enum):
    AWARD_CREATED = "award_created"
    THRESHOLDS_SET = "thresholds_set"
    PROPOSAL_SEEDED = "proposal_seeded"
    LINE_PICKED = "line_picked"
    LINE_OVERRIDDEN = "line_overridden"
    LINE_CLEARED = "line_cleared"
    LINE_RESEEDED = "line_reseeded"
    AWARD_REVIEWED = "award_reviewed"
    AWARD_APPROVED = "award_approved"
    AWARD_ARMED = "award_armed"
    COMMUNICATION_DRAFTED = "communication_drafted"
    COMMUNICATION_REJECTED = "communication_rejected"
    COMMUNICATION_EDITED = "communication_edited"
    COMMUNICATION_APPROVED = "communication_approved"
    NOTIFICATION_RECORDED = "notification_recorded"
    HANDOFF_GENERATED = "handoff_generated"
    AWARD_COMPLETED = "award_completed"
    AWARD_CANCELLED = "award_cancelled"


@dataclass
class ExecutionEvent:
    """One thing that happened, with the state it replaced.

    `previous` is what makes this an audit trail rather than a log: the chain replays to
    the award as it stands, so "what did this look like before the buyer changed it" is
    answerable without versioning every row.
    """
    id: str = field(default_factory=lambda: new_id("aev"))
    award_id: str = ""
    seq: int = 0
    event_type: str = ""
    at: str = field(default_factory=utc_now)
    actor: str = "buyer"                    # buyer | system
    subject_type: str = "award"             # award | line | communication | handoff
    subject_id: str = ""
    from_state: str = ""
    to_state: str = ""
    summary: str = ""
    previous: Dict[str, Any] = field(default_factory=dict)
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ExecutionEvent":
        d = dict(d or {})
        for key in ("previous", "detail"):
            if isinstance(d.get(key), str):
                try:
                    d[key] = json.loads(d[key])
                except (ValueError, TypeError):
                    d[key] = {}
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
