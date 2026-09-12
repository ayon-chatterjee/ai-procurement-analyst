"""Every figure the analyst reports is computed here.

This is the half of Phase 3 that the model does not touch. It reads the same comparison
dataset the Quotes screen renders, applies the buyer's filters, and works out the answer
in plain Python — so an answer cannot drift from what the comparison shows, and a
question the data cannot settle produces a stated gap rather than a plausible number.

Three rules run through all of it:

* An absent quote is a state, never a zero. "Not quoted", "no response", "unresolved" and
  "needs review" are different things and are reported as different things.
* A price enters a comparison only when Phase 2 normalised it and a real published rate
  can express it in the comparison currency. Anything else is excluded *with its reason*.
* A claim is not a verification. Certifications and questionnaire answers keep the
  `ClaimStatus` Phase 2 gave them, and "cleared QA" means VERIFIED unless the buyer
  explicitly asks for the lenient reading, which is then labelled a what-if.

Pure: no database, no model, no Streamlit.
"""
from __future__ import annotations

import datetime as _dt
import re
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .analyst_models import (
    AnalystQuery, AnalystResult, EvidenceRef, EvidenceTopic, Exclusion, Filter, Grain,
    Hypothetical, INTENT_RULES, Intent, Qualification, QualificationCheck,
    QualificationStatus, QUESTIONNAIRE_PREFIX, QUOTE_FIELDS,
)
from .fx import Converted
from .guards import norm_text
from .schema import RFQ, Importance, LineItem
from .supplier_guards import KNOWN_CURRENCIES, canonical_cert_name
from .supplier_models import (
    Certification, ClaimStatus, MatchStatus, NormalizationStatus, QuoteStatus,
    ResponseBundle, Supplier, SupplierQuote,
)
from .supplier_service import CellState, ComparisonCell, ComparisonMatrix, review_items

#: Shown instead of a number when a supplier has no comparable price for a line. Never 0.
NOT_AVAILABLE = "—"


def _round(value: Optional[float], places: int = 4) -> Optional[float]:
    return None if value is None else round(float(value), places)


def _pct(part: float, whole: float) -> Optional[float]:
    return None if not whole else round(100.0 * part / whole, 1)


def _money(value: Optional[float], currency: Optional[str]) -> str:
    if value is None:
        return NOT_AVAILABLE
    text = ("%.4f" % value).rstrip("0").rstrip(".")
    return "%s %s" % (currency, text) if currency else text


# --------------------------------------------------------------------------- #
# Comparison currency
# --------------------------------------------------------------------------- #
def choose_comparison_currency(matrix: ComparisonMatrix, query: AnalystQuery,
                               session_currency: Optional[str] = None
                               ) -> Tuple[Optional[str], str]:
    """Pick the currency to compare in, and say where the choice came from.

    The buyer is always told: comparing mixed currencies without naming the rate is how
    a comparison quietly becomes wrong.
    """
    if query.comparison_currency:
        return query.comparison_currency, "the currency you asked for"
    if session_currency and session_currency.strip().upper() in KNOWN_CURRENCIES:
        return session_currency.strip().upper(), "your display setting on the comparison screen"

    fv = matrix.rfq.fields.get("currency") if matrix.rfq.fields else None
    if fv is not None and fv.is_filled:
        code = str(fv.value).strip().upper()
        if code in KNOWN_CURRENCIES:
            return code, "the currency this RFQ asked for"

    counts: Counter = Counter()
    for cell in matrix.cells.values():
        q = cell.quote
        if q is not None and q.normalized_unit_price is not None and q.currency in KNOWN_CURRENCIES:
            counts[q.currency] += 1
    if counts:
        top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        return top, "the currency most suppliers quoted in"
    return None, "no supplier quoted a price we could read"


# --------------------------------------------------------------------------- #
# Eligibility
# --------------------------------------------------------------------------- #
#: Statuses that can never pass, whatever the buyer assumes. A failed or expired
#: certificate is a fact about the document, not an outstanding question.
_DISQUALIFYING = (ClaimStatus.FAILED, ClaimStatus.EXPIRED, ClaimStatus.CONFLICT)


def required_certifications(rfq: RFQ) -> List[str]:
    """Certifications this RFQ actually demanded, canonicalised. Often empty."""
    fv = rfq.fields.get("certifications") if rfq.fields else None
    if fv is None or not fv.is_filled:
        return []
    raw = fv.value
    items = raw if isinstance(raw, list) else [raw]
    out: List[str] = []
    for item in items:
        name = canonical_cert_name(str(item))
        if name and name not in out:
            out.append(name)
    return out


def assess_supplier(bundle: Optional[ResponseBundle], rfq: RFQ, hyp: Hypothetical,
                    supplier: Optional[Supplier] = None) -> Qualification:
    """Work out whether a supplier has cleared the RFQ's quality requirements.

    The default reading of "cleared" is VERIFIED — a certificate we actually hold. A
    supplier saying they are certified is a claim, and treating it as proof is exactly
    the mistake this app exists to avoid. The buyer can ask for the lenient reading, and
    when they do every promoted check is marked so the answer stays honest about it.
    """
    sid = bundle.response.supplier_id if bundle else (supplier.id if supplier else "")
    name = (bundle.supplier.name if bundle and bundle.supplier
            else (supplier.name if supplier else sid))
    policy = "claimed_ok" if hyp.treat_claimed_as_verified else "verified"

    if bundle is None:
        return Qualification(supplier_id=sid, supplier_name=name,
                             status=QualificationStatus.NOT_ASSESSED.value,
                             reasons=["did not respond to this RFQ"], policy=policy)

    required = required_certifications(rfq)
    checks: List[QualificationCheck] = []
    reasons: List[str] = []
    seen: Dict[str, Certification] = {}          # canonical name -> cert

    for cert in bundle.certifications:
        canonical = canonical_cert_name(cert.name or cert.raw_name)
        seen[canonical] = cert
        status = cert.status.value if hasattr(cert.status, "value") else str(cert.status)
        disqualified = cert.status in _DISQUALIFYING
        promoted = (not disqualified and cert.status == ClaimStatus.CLAIMED
                    and hyp.treat_claimed_as_verified)
        passed = cert.status == ClaimStatus.VERIFIED or promoted
        note = cert.note or ""
        if cert.status == ClaimStatus.CLAIMED and not promoted:
            note = note or "Stated by the supplier; no certificate was attached."
        checks.append(QualificationCheck(
            kind="certification", name=canonical or cert.name, required=canonical in required,
            status=status, passed=passed, counted_by_hypothesis=promoted,
            evidence_ids=list(cert.evidence_ids), note=note))
        if disqualified:
            reasons.append("%s is %s" % (canonical or cert.name, status))

    for want in required:
        if want not in seen:
            checks.append(QualificationCheck(
                kind="certification", name=want, required=True,
                status=ClaimStatus.MISSING.value, passed=False,
                note="The RFQ asks for this and the supplier did not mention it."))
            reasons.append("%s was not addressed" % want)

    # The questionnaire records what a supplier answered, not what they proved: Phase 2
    # never marks an answer VERIFIED. So a required question counts as met when it was
    # answered at all, and the assumption says so rather than implying more.
    answered = {a.field_key: a for a in bundle.questionnaire if a.field_key}
    for question in rfq.questions:
        key = getattr(question, "field_key", None)
        if not key or question.importance != Importance.REQUIRED:
            continue
        a = answered.get(key)
        ok = a is not None and a.status != ClaimStatus.MISSING
        checks.append(QualificationCheck(
            kind="questionnaire", name=key, required=True,
            status=(a.status.value if a else ClaimStatus.MISSING.value), passed=ok,
            evidence_ids=list(a.evidence_ids) if a else [],
            note="" if ok else "The RFQ required an answer and none was given."))
        if not ok:
            reasons.append("did not answer '%s'" % key)

    cert_checks = [c for c in checks if c.kind == "certification"]
    required_checks = [c for c in checks if c.required]
    has_disqualifying = any(c.kind == "certification" and c.status in
                            [d.value for d in _DISQUALIFYING] for c in checks)

    if has_disqualifying or any(not c.passed for c in required_checks):
        status = QualificationStatus.NOT_CLEARED.value
    elif required and all(c.passed for c in required_checks if c.kind == "certification"):
        status = QualificationStatus.CLEARED.value
    elif not required and any(c.passed for c in cert_checks):
        status = QualificationStatus.CLEARED.value
    else:
        status = QualificationStatus.UNVERIFIED.value
        if cert_checks:
            reasons.append("certifications are claimed but not backed by a document")
        else:
            reasons.append("no certification was mentioned")

    return Qualification(supplier_id=sid, supplier_name=name, status=status,
                         checks=checks, reasons=reasons, policy=policy)


def qualification_assumptions(rfq: RFQ, hyp: Hypothetical) -> List[str]:
    required = required_certifications(rfq)
    out: List[str] = []
    if hyp.treat_claimed_as_verified:
        out.append("For this what-if a certification the supplier claims counts as if it "
                   "were verified. Nothing in the records changed.")
    else:
        out.append("\"Cleared\" means a certification backed by a document we hold. A "
                   "certification the supplier merely states is treated as a claim.")
    if required:
        out.append("This RFQ requires: %s." % ", ".join(required))
    else:
        out.append("This RFQ names no required certification, so \"cleared\" means at "
                   "least one certification is document-backed.")
    out.append("Questionnaire answers count as answered or not answered; Phase 2 never "
               "marks an answer verified.")
    return out


# --------------------------------------------------------------------------- #
# Price comparability
# --------------------------------------------------------------------------- #
@dataclass
class PriceCheck:
    """Whether one cell's price can take part in a comparison, and why not if it cannot."""
    amount: Optional[float] = None            # in the comparison currency
    native: str = ""                          # as the supplier wrote it
    rate_note: str = ""                       # "1 EUR = 1.16 USD, provider as of …"
    exclusion: Optional[Exclusion] = None
    caveats: List[str] = field(default_factory=list)
    moq_constraint: bool = False
    evidence_ids: List[str] = field(default_factory=list)

    @property
    def comparable(self) -> bool:
        return self.amount is not None

    @property
    def valid(self) -> bool:
        """Comparable *and* something the buyer could actually accept at this quantity."""
        return self.amount is not None and not self.moq_constraint


def _conflict_topics(quote: SupplierQuote) -> List[str]:
    return [str(c.get("topic") or "") for c in (quote.conflicts or [])]


def _is_price_conflict(quote: SupplierQuote) -> bool:
    return any(("price" in t.lower() or "cost" in t.lower()) for t in _conflict_topics(quote))


def _describe_conflicts(quote: SupplierQuote) -> str:
    bits = []
    for c in quote.conflicts or []:
        values = [str(v.get("value")) for v in (c.get("values") or []) if v.get("value")]
        bits.append("%s: %s" % (c.get("topic") or "contradiction", " vs ".join(values))
                    if values else (c.get("topic") or "contradiction"))
    return "; ".join(bits)


def price_check(cell: ComparisonCell, ctx: "CalcContext") -> PriceCheck:
    """The single rule for whether a price may be compared. Every intent uses it."""
    name = ctx.names.get(cell.supplier_id, cell.supplier_id)

    def out(code: str, reason: str, evidence: Optional[List[str]] = None) -> PriceCheck:
        return PriceCheck(exclusion=Exclusion(
            supplier_id=cell.supplier_id, supplier_name=name, line_id=cell.line_item_id,
            code=code, reason=reason, evidence_ids=list(evidence or [])))

    if cell.state == CellState.NO_RESPONSE:
        return out("no_response", "did not respond to this RFQ")
    quote = cell.quote
    if quote is None:
        return out("not_quoted", "did not price this line")
    if quote.status == QuoteStatus.NOT_QUOTED:
        return out("not_quoted_explicit",
                   "; ".join(quote.issues) or "stated they cannot quote this line",
                   quote.evidence_ids)
    if not quote.has_price:
        return out("no_price", "; ".join(quote.issues) or "no price was given for this line",
                   quote.evidence_ids)
    if quote.match_status == MatchStatus.PROBABLE_MATCH and not ctx.hyp.include_probable_matches:
        return out("unconfirmed_match",
                   quote.match_reason or "the line this quote belongs to is not confirmed",
                   quote.evidence_ids)
    if quote.currency and quote.currency not in KNOWN_CURRENCIES:
        return out("unnamed_currency",
                   "; ".join(quote.issues) or "priced in '%s', which is not a currency we "
                                              "can read" % quote.currency, quote.evidence_ids)
    if quote.normalized_unit_price is None or quote.normalization_status != NormalizationStatus.NORMALIZED:
        return out("unresolved_price",
                   quote.normalization_note or "the price basis could not be normalised",
                   quote.evidence_ids)
    if quote.status == QuoteStatus.NEEDS_REVIEW:
        return out("needs_review", "; ".join(quote.issues) or "this quote is held for review",
                   quote.evidence_ids)
    if quote.status == QuoteStatus.CONFLICT and _is_price_conflict(quote):
        return out("price_conflict", _describe_conflicts(quote) or "the supplier gave more "
                                                                   "than one price", quote.evidence_ids)

    # The price stands. Work out what it is in the comparison currency.
    amount: Optional[float] = None
    rate_note = ""
    converted: Optional[Converted] = cell.converted
    if ctx.currency is None or quote.currency == ctx.currency:
        amount = quote.normalized_unit_price
    elif converted is not None:
        amount = converted.amount
        rate_note = converted.describe_rate()
    else:
        return out("no_fx_rate",
                   "priced in %s and no published rate to %s was available"
                   % (quote.currency, ctx.currency), quote.evidence_ids)

    check = PriceCheck(amount=_round(amount), native=quote.original_price_text(),
                       rate_note=rate_note, moq_constraint=bool(quote.moq_constraint)
                       and not ctx.hyp.ignore_moq_constraints,
                       evidence_ids=list(quote.evidence_ids))

    # Included, but the buyer should see these next to the number.
    if quote.status == QuoteStatus.CONFLICT:
        check.caveats.append("contradiction on %s" % (_describe_conflicts(quote) or "a term"))
    if quote.moq_constraint:
        check.caveats.append("minimum order %s exceeds this line's quantity"
                             % _quantity_text(quote.minimum_order_quantity, quote.moq_unit))
    if quote.effective_unit_price is not None and quote.discount.is_present:
        check.caveats.append("after %s" % quote.discount.describe())
    if quote.price_is_indicative:
        check.caveats.append("indicative price")
    if quote.value_source is not None and getattr(quote.value_source, "value", "") == "buyer_corrected":
        check.caveats.append("buyer-corrected value")
    if ctx.hyp.include_probable_matches and quote.match_status == MatchStatus.PROBABLE_MATCH:
        check.caveats.append("line match unconfirmed")
    if check.moq_constraint:
        check.exclusion = Exclusion(
            supplier_id=cell.supplier_id, supplier_name=name, line_id=cell.line_item_id,
            code="moq_constraint",
            reason="minimum order %s exceeds this line's quantity"
                   % _quantity_text(quote.minimum_order_quantity, quote.moq_unit),
            evidence_ids=list(quote.evidence_ids))
    return check


def _plural(count: int, singular: str, plural: Optional[str] = None) -> str:
    """"1 supplier quotes" / "3 suppliers quote" - summaries are read aloud in demos."""
    return singular if count == 1 else (plural if plural is not None else singular + "s")


def _quantity_text(value: Optional[float], unit: str = "") -> str:
    if value is None:
        return "not stated"
    return "{:,.0f} {}".format(value, unit or "pcs").strip()


# --------------------------------------------------------------------------- #
# Scope
# --------------------------------------------------------------------------- #
@dataclass
class CalcContext:
    """Everything a calculation needs, with the scope already worked out."""
    matrix: ComparisonMatrix
    query: AnalystQuery
    currency: Optional[str] = None
    currency_reason: str = ""
    today: _dt.date = field(default_factory=_dt.date.today)
    qualifications: Dict[str, Qualification] = field(default_factory=dict)
    suppliers: List[Supplier] = field(default_factory=list)
    lines: List[LineItem] = field(default_factory=list)
    scope_exclusions: List[Exclusion] = field(default_factory=list)
    names: Dict[str, str] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    match_notes: Dict[str, str] = field(default_factory=dict)   # supplier_id -> matched wording

    @property
    def rfq(self) -> RFQ:
        return self.matrix.rfq

    @property
    def hyp(self) -> Hypothetical:
        return self.query.hypothetical

    def bundle(self, supplier_id: str) -> Optional[ResponseBundle]:
        return self.matrix.bundles.get(supplier_id)

    def cell(self, line_id: str, supplier_id: str) -> ComparisonCell:
        return self.matrix.cell(line_id, supplier_id)

    def name(self, supplier_id: str) -> str:
        return self.names.get(supplier_id, supplier_id)

    def rate_provenance(self) -> Dict[str, Any]:
        rates = self.matrix.rates
        pairs: List[str] = []
        for cell in self.matrix.cells.values():
            if cell.converted is not None and cell.converted.is_conversion:
                note = cell.converted.describe_rate()
                if note not in pairs:
                    pairs.append(note)
        return {"source": rates.source if rates else "", "as_of": rates.as_of if rates else "",
                "error": rates.error if rates else "", "pairs": sorted(pairs)}


def _terms_text(bundle: Optional[ResponseBundle], attr: str) -> str:
    """A response-level term, read once per supplier.

    Phase 2 copies these onto every quote line, so reading them per quote would count one
    sentence five times.
    """
    if bundle is None:
        return ""
    seen: List[str] = []
    for q in bundle.quotes:
        value = (getattr(q, attr, "") or "").strip()
        if value and value not in seen:
            seen.append(value)
    return " / ".join(seen)


def _first_priced(bundle: Optional[ResponseBundle]) -> Optional[SupplierQuote]:
    if bundle is None:
        return None
    for q in bundle.quotes:
        if q.has_price:
            return q
    return bundle.quotes[0] if bundle.quotes else None


def build_context(matrix: ComparisonMatrix, query: AnalystQuery, currency: Optional[str],
                  currency_reason: str, today: Optional[_dt.date] = None,
                  session_currency: Optional[str] = None) -> CalcContext:
    """Apply the buyer's scope, recording every supplier and line it removes."""
    ctx = CalcContext(matrix=matrix, query=query, currency=currency,
                      currency_reason=currency_reason, today=today or _dt.date.today())
    ctx.names = {s.id: s.name for s in matrix.suppliers}

    all_suppliers = list(matrix.suppliers)
    responded = [s for s in all_suppliers if s.id in matrix.bundles]
    ctx.qualifications = {
        s.id: assess_supplier(matrix.bundles.get(s.id), matrix.rfq, query.hypothetical, s)
        for s in all_suppliers}

    keep = list(all_suppliers)

    def drop(supplier: Supplier, code: str, reason: str, hypothetical: bool = False,
             evidence: Optional[List[str]] = None) -> None:
        ctx.scope_exclusions.append(Exclusion(
            supplier_id=supplier.id, supplier_name=supplier.name, code=code, reason=reason,
            evidence_ids=list(evidence or []), hypothetical=hypothetical))

    # What-if exclusions first: the buyer asked for them explicitly.
    if query.hypothetical.exclude_supplier_ids:
        excluded = set(query.hypothetical.exclude_supplier_ids)
        for s in list(keep):
            if s.id in excluded:
                drop(s, "hypothetical_exclusion", "set aside for this what-if", True)
                keep.remove(s)

    rule = INTENT_RULES.get(query.intent)
    narrows_scope = rule is None or rule.subjects in ("none", "optional")
    if query.subject_supplier_ids and narrows_scope:
        wanted = set(query.subject_supplier_ids)
        keep = [s for s in keep if s.id in wanted]

    for f in query.filters:
        keep = _apply_supplier_filter(ctx, f, keep, drop)

    ctx.suppliers = sorted(keep, key=lambda s: (s.name or "").casefold())

    lines = list(matrix.rfq.line_items)
    if query.subject_line_ids:
        wanted = set(query.subject_line_ids)
        lines = [li for li in lines if li.id in wanted]
    for f in query.filters:
        if f.field == "line":
            chosen = set(f.values)
            lines = ([li for li in lines if li.id in chosen] if f.op == "in"
                     else [li for li in lines if li.id not in chosen])
    ctx.lines = lines

    ctx.notes.append("%d supplier%s on this RFQ → %d responded → %d in this answer."
                     % (len(all_suppliers), "" if len(all_suppliers) == 1 else "s",
                        len(responded), len(ctx.suppliers)))
    ctx.notes.append("%d of %d RFQ line%s in scope."
                     % (len(ctx.lines), len(matrix.rfq.line_items),
                        "" if len(matrix.rfq.line_items) == 1 else "s"))
    if currency:
        ctx.notes.append("Prices compared in %s (%s)." % (currency, currency_reason))
    for note in query.resolution_notes:
        ctx.notes.append(note)

    if currency and len(matrix.currencies()) > 1 and matrix.display_currency != currency:
        # The caller built the matrix in a different currency, so nothing can be converted.
        # Saying so beats reporting every foreign quote as missing.
        ctx.warnings.append(
            "Prices were requested in %s but the comparison was built in %s, so quotes in "
            "another currency could not be converted." % (currency, matrix.display_currency or "each supplier's own currency"))
    rates = matrix.rates
    if matrix.display_currency and rates is not None and not rates.ok:
        ctx.warnings.append(
            "Exchange rates were unavailable (%s), so quotes in another currency are left "
            "out rather than converted at a guessed rate." % (rates.error or "no rate"))
    return ctx


def _supplier_text(ctx: CalcContext, supplier: Supplier, field_name: str) -> str:
    bundle = ctx.bundle(supplier.id)
    return {
        "payment_terms": lambda: _terms_text(bundle, "payment_terms"),
        "delivery_terms": lambda: _terms_text(bundle, "delivery_terms"),
        "lead_time_text": lambda: _terms_text(bundle, "lead_time_text"),
        "validity_text": lambda: _terms_text(bundle, "quote_validity_text"),
    }[field_name]()


def _apply_supplier_filter(ctx: CalcContext, f, keep: List[Supplier],
                           drop: Callable) -> List[Supplier]:
    """Filters that remove a whole supplier. Cell-level filters are applied later."""
    out: List[Supplier] = []
    for s in keep:
        bundle = ctx.bundle(s.id)
        verdict, reason = True, ""

        if f.field == "supplier":
            verdict = (s.id in f.values) if f.op == "in" else (s.id not in f.values)
            reason = "not among the suppliers you named" if not verdict else ""
        elif f.field == "country":
            hit = norm_text(s.country) in {norm_text(v) for v in f.values}
            verdict = hit if f.op == "in" else not hit
            reason = "based in %s" % (s.country or "an unstated country")
        elif f.field == "eligibility":
            q = ctx.qualifications.get(s.id)
            verdict = bool(q and q.status in f.values)
            reason = "qualification is %s, not %s" % (
                q.status if q else "unknown", " or ".join(f.values))
        elif f.field == "certification":
            names = {}
            for cert in (bundle.certifications if bundle else []):
                names[canonical_cert_name(cert.name or cert.raw_name)] = cert
            wanted = [canonical_cert_name(v) for v in f.values]
            if f.op == "has_verified":
                verdict = all(n in names and (
                    names[n].status == ClaimStatus.VERIFIED
                    or (ctx.hyp.treat_claimed_as_verified
                        and names[n].status == ClaimStatus.CLAIMED)) for n in wanted)
                reason = "does not hold a verified %s" % ", ".join(wanted)
            elif f.op == "has_claimed":
                verdict = all(n in names and names[n].status in
                              (ClaimStatus.VERIFIED, ClaimStatus.CLAIMED) for n in wanted)
                reason = "has not claimed %s" % ", ".join(wanted)
            else:                                    # lacks
                verdict = all(n not in names for n in wanted)
                reason = "holds %s" % ", ".join(wanted)
        elif f.field == "questionnaire":
            answered = {a.field_key: a for a in (bundle.questionnaire if bundle else [])}
            if f.op == "answered":
                verdict = all(k in answered and answered[k].status != ClaimStatus.MISSING
                              for k in f.values)
                reason = "did not answer %s" % ", ".join(f.values)
            else:
                verdict = all(k not in answered or answered[k].status == ClaimStatus.MISSING
                              for k in f.values)
                reason = "answered %s" % ", ".join(f.values)
        elif f.field == "lead_time_days":
            days = _lead_time_days(bundle)
            limit = float(f.values[0])
            if days is None:
                verdict, reason = False, "did not state a lead time we could read"
            elif f.op == "lte":
                verdict = days <= limit
                reason = "lead time %g days is above %g" % (days, limit)
            else:
                verdict = days >= limit
                reason = "lead time %g days is below %g" % (days, limit)
        elif f.field == "validity":
            quote = _first_priced(bundle)
            conditional = bool(quote and quote.quote_validity_is_conditional)
            verdict = (not conditional) if f.op == "firm" else conditional
            reason = ("quote validity is conditional" if f.op == "firm"
                      else "quote validity is a fixed period")
        elif f.field in ("payment_terms", "delivery_terms", "lead_time_text", "validity_text"):
            text = _supplier_text(ctx, s, f.field)
            blob = norm_text(text)
            hit = any(norm_text(v) and norm_text(v) in blob for v in f.values)
            verdict = hit if f.op == "contains" else not hit
            if hit and f.op == "contains":
                ctx.match_notes[s.id] = text
            reason = ("%s do not mention %s" % (f.field.replace("_", " "), ", ".join(f.values))
                      if f.op == "contains"
                      else "%s mention %s" % (f.field.replace("_", " "), ", ".join(f.values)))
            if f.op == "contains" and not text:
                reason = "did not state %s" % f.field.replace("_", " ")
        else:
            out.append(s)
            continue

        if verdict:
            out.append(s)
        else:
            drop(s, "filtered_out", reason or "excluded by your filter")
    return out


def _lead_time_days(bundle: Optional[ResponseBundle]) -> Optional[float]:
    if bundle is None:
        return None
    for q in bundle.quotes:
        if q.lead_time_days is not None:
            return q.lead_time_days
    return None


def _cell_filters_pass(ctx: CalcContext, cell: ComparisonCell, check: PriceCheck) -> bool:
    """Filters that remove individual quotes rather than whole suppliers."""
    for f in ctx.query.filters:
        if f.field == "currency":
            code = cell.quote.currency if cell.quote else ""
            hit = code in f.values
            if (f.op == "in" and not hit) or (f.op == "not_in" and hit):
                return False
        elif f.field == "quote_state":
            hit = cell.state in f.values
            if (f.op == "in" and not hit) or (f.op == "not_in" and hit):
                return False
        elif f.field == "unit_price":
            if check.amount is None:
                return False
            limit = float(f.values[0])
            if f.op == "lte" and check.amount > limit:
                return False
            if f.op == "gte" and check.amount < limit:
                return False
        elif f.field == "moq":
            constrained = bool(cell.quote and cell.quote.moq_constraint)
            if f.op == "fits" and constrained:
                return False
            if f.op == "exceeds" and not constrained:
                return False
    return True


# --------------------------------------------------------------------------- #
# Result helper
# --------------------------------------------------------------------------- #
def _result(ctx: CalcContext, columns: List[str], rows: List[Dict[str, Any]],
            summary: str, metrics: Optional[Dict[str, Any]] = None,
            exclusions: Optional[List[Exclusion]] = None,
            assumptions: Optional[List[str]] = None,
            warnings: Optional[List[str]] = None,
            evidence: Optional[List[EvidenceRef]] = None,
            notes: Optional[List[str]] = None) -> AnalystResult:
    hyp = ctx.hyp
    labels = hyp.labels(ctx.names)
    if labels:
        summary = "What-if (%s): %s" % ("; ".join(labels), summary)
    all_exclusions = _dedupe_exclusions(list(ctx.scope_exclusions) + list(exclusions or []))
    result = AnalystResult(
        intent=ctx.query.intent, columns=columns, rows=rows, summary=summary,
        metrics=metrics or {}, warnings=list(ctx.warnings) + list(warnings or []),
        assumptions=list(assumptions or []), exclusions=all_exclusions,
        evidence_refs=list(evidence or []),
        calculation_notes=list(ctx.notes) + list(notes or []),
        hypothetical=hyp.is_active(), hypothetical_labels=labels,
        comparison_currency=ctx.currency, rate_provenance=ctx.rate_provenance(),
        query=ctx.query)
    result.metrics.setdefault("supplier_names", [s.name for s in ctx.suppliers])
    result.metrics.setdefault(
        "out_of_scope_names",
        [s.name for s in ctx.matrix.suppliers
         if s.id not in {x.id for x in ctx.suppliers}
         and not any(e.supplier_id == s.id for e in all_exclusions)])
    if ctx.currency:
        result.assumptions.insert(0, "Prices are compared in %s, taken from %s."
                                  % (ctx.currency, ctx.currency_reason))
    return result


#: Exclusions that describe a whole response rather than one line. Listing them once per
#: line would bury the line-specific reasons under seven copies of the same sentence.
_RESPONSE_WIDE_CODES = {"no_response", "hypothetical_exclusion", "filtered_out",
                        "lead_time_conflict"}


def _dedupe_exclusions(items: List[Exclusion]) -> List[Exclusion]:
    out: List[Exclusion] = []
    seen: Dict[Tuple[str, str], Exclusion] = {}
    for item in items:
        if item.code not in _RESPONSE_WIDE_CODES:
            out.append(item)
            continue
        key = (item.supplier_id, item.code)
        if key in seen:
            continue
        collapsed = Exclusion(item.supplier_id, item.supplier_name, None, item.code,
                              item.reason, list(item.evidence_ids), item.hypothetical)
        seen[key] = collapsed
        out.append(collapsed)
    return out


def _limit(ctx: CalcContext, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    n = ctx.query.top_n
    if n and len(rows) > n:
        ctx.notes.append("Showing the first %d of %d rows, as you asked." % (n, len(rows)))
        return rows[:n]
    return rows


def _line_label(line: LineItem) -> str:
    return line.spec_summary() or line.description or line.product


# --------------------------------------------------------------------------- #
# The calculations
#
# One function per intent. Each returns a complete AnalystResult: the numbers, the rows
# behind them, what was left out and why, and the assumptions the buyer is entitled to
# argue with. None of them ever returns a bare figure.
# --------------------------------------------------------------------------- #
def _scan(ctx: CalcContext) -> Tuple[Dict[Tuple[str, str], PriceCheck], List[Exclusion]]:
    """Run the price rule over every (line, supplier) in scope, once."""
    checks: Dict[Tuple[str, str], PriceCheck] = {}
    excluded: List[Exclusion] = []
    for line in ctx.lines:
        for supplier in ctx.suppliers:
            cell = ctx.cell(line.id, supplier.id)
            check = price_check(cell, ctx)
            if not _cell_filters_pass(ctx, cell, check):
                continue
            checks[(line.id, supplier.id)] = check
            if check.exclusion is not None:
                excluded.append(check.exclusion)
    return checks, excluded


def cheapest_by_line(ctx: CalcContext) -> AnalystResult:
    """The lowest price we can stand behind, line by line.

    This is an analytical result, not an award: it says which quote is lowest among those
    that are comparable and acceptable at the quantity asked for, and lists everything it
    could not count.
    """
    checks, excluded = _scan(ctx)
    columns = ["Line", "Item", "Qty", "Cheapest supplier", "Price", "As quoted",
               "Runner-up", "Runner-up price", "Gap %", "Valid quotes", "Left out", "Notes"]
    rows: List[Dict[str, Any]] = []
    wins: Counter = Counter()
    answered = 0
    basket = 0.0
    basket_complete = True

    for line in ctx.lines:
        candidates = []
        left_out = 0
        for supplier in ctx.suppliers:
            check = checks.get((line.id, supplier.id))
            if check is None:
                continue
            if check.valid:
                candidates.append((check.amount, supplier.name, check))
            else:
                left_out += 1
        candidates.sort(key=lambda c: (c[0], c[1].casefold()))

        row: Dict[str, Any] = {"Line": line.id, "Item": _line_label(line),
                               "Qty": line.quantity, "Valid quotes": len(candidates),
                               "Left out": left_out}
        if not candidates:
            row.update({"Cheapest supplier": "no comparable quote", "Price": NOT_AVAILABLE,
                        "As quoted": NOT_AVAILABLE, "Runner-up": NOT_AVAILABLE,
                        "Runner-up price": NOT_AVAILABLE, "Gap %": None,
                        "Notes": "every quote for this line was left out — see below"})
            basket_complete = False
            rows.append(row)
            continue

        best_amount = candidates[0][0]
        tied = [c for c in candidates if c[0] == best_amount]
        winner = "; ".join(c[1] for c in tied)
        notes = list(tied[0][2].caveats)
        if len(tied) > 1:
            notes.insert(0, "tie — %d suppliers at the same price" % len(tied))
        for c in tied:
            wins[c[1]] += 1
        answered += 1
        if line.quantity:
            basket += best_amount * float(line.quantity)
        else:
            basket_complete = False

        rest = [c for c in candidates if c[0] != best_amount]
        runner = rest[0] if rest else None
        row.update({
            "Cheapest supplier": winner,
            "Price": best_amount,
            "As quoted": tied[0][2].native or NOT_AVAILABLE,
            "Runner-up": runner[1] if runner else NOT_AVAILABLE,
            "Runner-up price": runner[0] if runner else NOT_AVAILABLE,
            "Gap %": (round(100.0 * (runner[0] - best_amount) / best_amount, 1)
                      if runner and best_amount else None),
            "Notes": "; ".join(notes)})
        rows.append(row)

    metrics: Dict[str, Any] = {
        "lines_answered": answered,
        "lines_without_valid_quote": len(ctx.lines) - answered,
        "wins_by_supplier": dict(sorted(wins.items())),
    }
    if basket_complete and answered == len(ctx.lines) and answered:
        metrics["basket_at_cheapest"] = round(basket, 2)
    else:
        ctx.notes.append("No basket total: not every line has a valid cheapest quote and "
                         "a stated quantity.")

    if answered:
        leader = sorted(wins.items(), key=lambda kv: (-kv[1], kv[0]))
        summary = ("%s of %d line%s %s a comparable quote. %s is lowest on %d of them."
                   % (answered, len(ctx.lines), "" if len(ctx.lines) == 1 else "s",
                      "has" if answered == 1 else "have", leader[0][0], leader[0][1])) \
            if leader else ("%d of %d lines have a comparable quote." % (answered, len(ctx.lines)))
    else:
        summary = ("No line has a quote I can compare. Every price was left out for the "
                   "reasons listed below.")

    assumptions = ["A quote counts only when Phase 2 normalised its price basis and the "
                   "currency is one I can convert.",
                   "A minimum order above the line's quantity makes a quote unusable at "
                   "that quantity, so it is not counted as cheapest."
                   if not ctx.hyp.ignore_moq_constraints else
                   "For this what-if, minimum order quantities are ignored."]
    return _result(ctx, columns, _limit(ctx, rows), summary, metrics, excluded, assumptions)


def compare_prices(ctx: CalcContext) -> AnalystResult:
    """The price matrix: every line against every supplier, comparable or not."""
    checks, excluded = _scan(ctx)
    columns = ["Line", "Item", "Qty"] + [s.name for s in ctx.suppliers] + ["Comparable"]
    rows: List[Dict[str, Any]] = []
    comparable_cells = 0
    states: Counter = Counter()

    for line in ctx.lines:
        row: Dict[str, Any] = {"Line": line.id, "Item": _line_label(line), "Qty": line.quantity}
        count = 0
        for supplier in ctx.suppliers:
            cell = ctx.cell(line.id, supplier.id)
            states[cell.state] += 1
            check = checks.get((line.id, supplier.id))
            if check is None:
                row[supplier.name] = "filtered out"
            elif check.comparable:
                text = _money(check.amount, ctx.currency)
                if check.moq_constraint:
                    text += " †"
                row[supplier.name] = text
                count += 1
                comparable_cells += 1
            else:
                row[supplier.name] = check.exclusion.code.replace("_", " ") if check.exclusion \
                    else NOT_AVAILABLE
        row["Comparable"] = count
        rows.append(row)

    notes = []
    if any(c.moq_constraint for c in checks.values()):
        notes.append("† the supplier's minimum order exceeds that line's quantity.")
    summary = ("%d of %d line-by-supplier cells carry a price I can compare."
               % (comparable_cells, max(1, len(ctx.lines) * len(ctx.suppliers))))
    metrics = {"lines": len(ctx.lines), "suppliers": len(ctx.suppliers),
               "comparable_cells": comparable_cells, "cells_by_state": dict(states)}
    return _result(ctx, columns, _limit(ctx, rows), summary, metrics, excluded, notes=notes)


def price_difference(ctx: CalcContext) -> AnalystResult:
    """How much dearer one supplier is than another, or than the cheapest alternative."""
    checks, excluded = _scan(ctx)
    subjects = [s for s in ctx.suppliers if s.id in ctx.query.subject_supplier_ids]
    if not subjects:
        subjects = [s for s in ctx.matrix.suppliers if s.id in ctx.query.subject_supplier_ids]
    if not subjects:
        return AnalystResult.refusal(ctx.query.reading,
                                     "I need to know which supplier to compare",
                                     ctx.query.intent, [s.name for s in ctx.suppliers])
    a = subjects[0]
    b = subjects[1] if len(subjects) > 1 else None
    other_label = b.name if b else "Cheapest other"

    columns = ["Line", "Item", a.name, other_label, "Difference", "Difference %", "Notes"]
    rows: List[Dict[str, Any]] = []
    compared = 0
    a_total = other_total = 0.0
    a_cheaper = b_cheaper = ties = 0

    for line in ctx.lines:
        ca = checks.get((line.id, a.id))
        a_amount = ca.amount if ca and ca.comparable else None
        note = ""
        if a_amount is None and ca and ca.exclusion:
            note = "%s: %s" % (a.name, ca.exclusion.reason)

        if b is not None:
            cb = checks.get((line.id, b.id))
            other_amount = cb.amount if cb and cb.comparable else None
            other_name = b.name
            if other_amount is None and cb and cb.exclusion:
                note = (note + "; " if note else "") + "%s: %s" % (b.name, cb.exclusion.reason)
        else:
            rivals = [(checks[(line.id, s.id)].amount, s.name) for s in ctx.suppliers
                      if s.id != a.id and checks.get((line.id, s.id))
                      and checks[(line.id, s.id)].valid]
            rivals.sort(key=lambda r: (r[0], r[1].casefold()))
            other_amount, other_name = (rivals[0] if rivals else (None, NOT_AVAILABLE))
            if other_amount is None:
                note = note or "no other supplier has a comparable quote for this line"

        row: Dict[str, Any] = {"Line": line.id, "Item": _line_label(line),
                               a.name: a_amount if a_amount is not None else NOT_AVAILABLE,
                               other_label: other_amount if other_amount is not None else NOT_AVAILABLE}
        if a_amount is not None and other_amount is not None:
            diff = round(a_amount - other_amount, 4)
            row["Difference"] = diff
            row["Difference %"] = round(100.0 * diff / a_amount, 1) if a_amount else None
            compared += 1
            qty = float(line.quantity or 0)
            a_total += a_amount * qty
            other_total += other_amount * qty
            if diff < 0:
                a_cheaper += 1
            elif diff > 0:
                b_cheaper += 1
            else:
                ties += 1
            if b is None and other_name != NOT_AVAILABLE:
                note = (note + "; " if note else "") + "cheapest other: %s" % other_name
        else:
            row["Difference"] = NOT_AVAILABLE
            row["Difference %"] = None
        row["Notes"] = note
        rows.append(row)

    metrics = {"lines_compared": compared, "%s cheaper on" % a.name: a_cheaper,
               "%s cheaper on" % other_label: b_cheaper, "ties": ties,
               "%s total on compared lines" % a.name: round(a_total, 2),
               "%s total on compared lines" % other_label: round(other_total, 2)}
    if compared:
        direction = ("cheaper" if a_total < other_total else
                     "more expensive" if a_total > other_total else "level")
        summary = ("Across the %d line%s where both have a comparable price, %s is %s: "
                   "%s against %s at the quantities asked for."
                   % (compared, "" if compared == 1 else "s", a.name, direction,
                      _money(round(a_total, 2), ctx.currency),
                      _money(round(other_total, 2), ctx.currency)))
    else:
        summary = ("There is no line where both sides have a price I can compare, so I "
                   "cannot put a difference on it.")
    assumptions = ["Differences are shown relative to %s." % a.name,
                   "Totals multiply the per-piece price by each line's quantity."]
    return _result(ctx, columns, _limit(ctx, rows), summary, metrics, excluded, assumptions)


def supplier_coverage(ctx: CalcContext) -> AnalystResult:
    """How much of the RFQ each supplier actually answered."""
    checks, excluded = _scan(ctx)
    columns = ["Supplier", "Status", "Comparable lines", "Priced lines", "Not quoted",
               "Unresolved", "Coverage %", "Currencies", "Qualification"]
    rows: List[Dict[str, Any]] = []
    total = len(ctx.lines) or 1

    for supplier in ctx.suppliers:
        bundle = ctx.bundle(supplier.id)
        comparable = priced = not_quoted = unresolved = 0
        currencies: List[str] = []
        for line in ctx.lines:
            cell = ctx.cell(line.id, supplier.id)
            check = checks.get((line.id, supplier.id))
            q = cell.quote
            if q is not None and q.has_price:
                priced += 1
                if q.currency and q.currency not in currencies:
                    currencies.append(q.currency)
            if check is not None and check.comparable:
                comparable += 1
            elif cell.state in (CellState.NOT_QUOTED, CellState.NO_RESPONSE):
                not_quoted += 1
            else:
                unresolved += 1
        qual = ctx.qualifications.get(supplier.id)
        rows.append({
            "Supplier": supplier.name,
            "Status": "no response" if bundle is None else supplier.status.value.replace("_", " "),
            "Comparable lines": comparable, "Priced lines": priced,
            "Not quoted": not_quoted, "Unresolved": unresolved,
            "Coverage %": _pct(comparable, total),
            "Currencies": ", ".join(sorted(currencies)) or NOT_AVAILABLE,
            "Qualification": (qual.status.replace("_", " ") if qual else "unknown")})

    rows.sort(key=lambda r: (-(r["Comparable lines"]), r["Supplier"].casefold()))
    warnings = []
    for supplier in ctx.suppliers:
        bundle = ctx.bundle(supplier.id)
        if bundle is None:
            continue
        counts = Counter(q.line_item_id for q in bundle.quotes if q.line_item_id)
        dupes = [lid for lid, n in counts.items() if n > 1]
        if dupes:
            warnings.append("%s has more than one quote for %s; the comparison shows the "
                            "first and I have not chosen between them."
                            % (supplier.name, ", ".join(sorted(dupes))))
    best = rows[0] if rows else None
    summary = ("%s answered the most of this RFQ: %d of %d lines with a comparable price "
               "(%s%%)." % (best["Supplier"], best["Comparable lines"], len(ctx.lines),
                            best["Coverage %"])) if best else "No supplier is in scope."
    metrics = {"lines_in_scope": len(ctx.lines), "suppliers": len(rows)}
    return _result(ctx, columns, _limit(ctx, rows), summary, metrics, excluded,
                   warnings=warnings)


def line_coverage(ctx: CalcContext) -> AnalystResult:
    """How well covered each line is, and who is missing from it."""
    checks, excluded = _scan(ctx)
    columns = ["Line", "Item", "Qty", "Comparable quotes", "Coverage %", "Quoted by",
               "Missing from"]
    rows: List[Dict[str, Any]] = []
    total = len(ctx.suppliers) or 1
    uncovered = 0

    for line in ctx.lines:
        quoted: List[str] = []
        missing: List[str] = []
        for supplier in ctx.suppliers:
            check = checks.get((line.id, supplier.id))
            if check is not None and check.comparable:
                quoted.append(supplier.name)
            else:
                reason = check.exclusion.reason if (check and check.exclusion) else "filtered out"
                missing.append("%s (%s)" % (supplier.name, reason))
        if not quoted:
            uncovered += 1
        rows.append({"Line": line.id, "Item": _line_label(line), "Qty": line.quantity,
                     "Comparable quotes": len(quoted), "Coverage %": _pct(len(quoted), total),
                     "Quoted by": ", ".join(quoted) or NOT_AVAILABLE,
                     "Missing from": "; ".join(missing) or NOT_AVAILABLE})

    covered = len(ctx.lines) - uncovered
    summary = ("%d of %d line%s has at least one comparable quote; %d has none."
               % (covered, len(ctx.lines), "" if len(ctx.lines) == 1 else "s", uncovered))
    metrics = {"lines_with_no_comparable_quote": uncovered,
               "lines_fully_covered": sum(1 for r in rows if r["Comparable quotes"] == total),
               "suppliers_in_scope": len(ctx.suppliers)}
    return _result(ctx, columns, _limit(ctx, rows), summary, metrics, excluded)


def missing_quotes(ctx: CalcContext) -> AnalystResult:
    """Every gap, named by what kind of gap it is."""
    checks, _ = _scan(ctx)
    columns = ["Line", "Item", "Supplier", "Kind", "Detail"]
    rows: List[Dict[str, Any]] = []
    kinds: Counter = Counter()
    by_supplier: Counter = Counter()

    for line in ctx.lines:
        for supplier in ctx.suppliers:
            check = checks.get((line.id, supplier.id))
            if check is None or check.comparable:
                continue
            exclusion = check.exclusion
            kind = (exclusion.code if exclusion else "unknown").replace("_", " ")
            kinds[kind] += 1
            by_supplier[supplier.name] += 1
            rows.append({"Line": line.id, "Item": _line_label(line), "Supplier": supplier.name,
                         "Kind": kind, "Detail": exclusion.reason if exclusion else ""})

    summary = ("%d line-by-supplier cells have no comparable price. The commonest reason "
               "is '%s' (%d)." % (len(rows), kinds.most_common(1)[0][0],
                                  kinds.most_common(1)[0][1])) if rows else \
              "Every supplier in scope has a comparable price for every line in scope."
    metrics = {"total": len(rows), "by_kind": dict(sorted(kinds.items())),
               "by_supplier": dict(sorted(by_supplier.items()))}
    assumptions = ["\"Not quoted\", \"no response\" and \"unresolved\" are different "
                   "things and are counted separately. None of them is a price of zero."]
    return _result(ctx, columns, _limit(ctx, rows), summary, metrics, assumptions=assumptions)


def lead_time_comparison(ctx: CalcContext) -> AnalystResult:
    """Lead times side by side, with how each was read."""
    columns = ["Supplier", "Lead time (as stated)", "Days", "How read", "Contradiction", "Notes"]
    rows: List[Dict[str, Any]] = []
    exclusions: List[Exclusion] = []
    interpreted = unknown = 0
    triggers: List[str] = []

    for supplier in ctx.suppliers:
        bundle = ctx.bundle(supplier.id)
        if bundle is None:
            exclusions.append(Exclusion(supplier.id, supplier.name, None, "no_response",
                                        "did not respond to this RFQ"))
            continue
        text = _terms_text(bundle, "lead_time_text")
        quote = _first_priced(bundle)
        days = _lead_time_days(bundle)
        is_interpreted = bool(quote and quote.lead_time_is_interpreted)
        contradiction = ""
        for q in bundle.quotes:
            for c in (q.conflicts or []):
                topic = str(c.get("topic") or "").lower()
                if "lead" in topic or "deliver" in topic:
                    values = [str(v.get("value")) for v in (c.get("values") or [])]
                    contradiction = " vs ".join(v for v in values if v)
                    break
            if contradiction:
                break

        if text:
            lower = text.lower()
            for marker in ("after ", "from "):
                if marker in lower:
                    tail = text[lower.index(marker) + len(marker):].strip()
                    if tail and tail not in triggers:
                        triggers.append(tail)
                    break

        how = "not stated"
        if days is not None:
            how = "midpoint of a range" if is_interpreted else "as stated"
            if is_interpreted:
                interpreted += 1
        else:
            unknown += 1

        if contradiction:
            exclusions.append(Exclusion(
                supplier.id, supplier.name, None, "lead_time_conflict",
                "gave more than one lead time (%s), so I have not ranked it" % contradiction))
        rows.append({"Supplier": supplier.name, "Lead time (as stated)": text or NOT_AVAILABLE,
                     "Days": None if contradiction else days,
                     "How read": how, "Contradiction": contradiction or "",
                     "Notes": "not ranked while the contradiction stands" if contradiction
                              else ("read from a range" if is_interpreted else "")})

    rankable = [r for r in rows if isinstance(r["Days"], (int, float))]
    rankable.sort(key=lambda r: (r["Days"], r["Supplier"].casefold()))
    rest = sorted((r for r in rows if r not in rankable), key=lambda r: r["Supplier"].casefold())
    ordered = rankable + rest

    warnings = []
    if len(triggers) > 1:
        warnings.append("These lead times are measured from different starting points (%s), "
                        "so they are not strictly like for like."
                        % "; ".join(triggers[:4]))
    summary = ("%s quotes the shortest lead time at %g days."
               % (rankable[0]["Supplier"], rankable[0]["Days"])) if rankable else \
              "No supplier in scope stated a lead time I could read as a number of days."
    metrics = {"shortest": rankable[0]["Supplier"] if rankable else None,
               "interpreted_count": interpreted, "unknown_count": unknown}
    assumptions = ["A range like \"20–25 days\" is read as its midpoint and marked as an "
                   "interpretation.",
                   "Lead times are shown with the wording the supplier used, including "
                   "what they start from."]
    return _result(ctx, columns, _limit(ctx, ordered), summary, metrics, exclusions,
                   assumptions, warnings)


def moq_check(ctx: CalcContext) -> AnalystResult:
    """Minimum order quantities against the quantities this RFQ asks for."""
    columns = ["Supplier", "MOQ (as stated)", "Line", "Line qty", "Fits", "Shortfall"]
    rows: List[Dict[str, Any]] = []
    evidence: List[EvidenceRef] = []
    constrained_suppliers: set = set()
    affected_lines: set = set()

    for supplier in ctx.suppliers:
        bundle = ctx.bundle(supplier.id)
        if bundle is None:
            continue
        seen: set = set()
        for line in ctx.lines:
            cell = ctx.cell(line.id, supplier.id)
            q = cell.quote
            if q is None or not q.has_price:
                continue
            moq = q.minimum_order_quantity
            key = (moq, line.id)
            if key in seen:
                continue
            seen.add(key)
            fits = "MOQ not stated" if moq is None else ("no" if q.moq_constraint else "yes")
            shortfall = (round(moq - float(line.quantity), 2)
                         if moq is not None and line.quantity and q.moq_constraint else None)
            if q.moq_constraint:
                constrained_suppliers.add(supplier.name)
                affected_lines.add(line.id)
                evidence.extend(_evidence_refs(ctx, supplier.id, q.evidence_ids,
                                               EvidenceTopic.MOQ.value, line.id))
            rows.append({"Supplier": supplier.name,
                         "MOQ (as stated)": _quantity_text(moq, q.moq_unit),
                         "Line": line.id, "Line qty": line.quantity, "Fits": fits,
                         "Shortfall": shortfall})

    rows.sort(key=lambda r: (r["Supplier"].casefold(), r["Line"]))
    n = len(constrained_suppliers)
    summary = ("%d %s %s a minimum order above the quantity this RFQ asks for, "
               "affecting %d %s."
               % (n, _plural(n, "supplier"), _plural(n, "quotes", "quote"),
                  len(affected_lines), _plural(len(affected_lines), "line"))) \
        if constrained_suppliers else \
        "No supplier in scope has a minimum order above the quantities asked for."
    metrics = {"suppliers_with_constraint": sorted(constrained_suppliers),
               "lines_affected": len(affected_lines)}
    assumptions = ["A minimum order is compared with each line's own quantity.",
                   "A supplier who did not state a minimum order is not assumed to fit."]
    return _result(ctx, columns, _limit(ctx, rows), summary, metrics,
                   assumptions=assumptions, evidence=evidence)


def quote_validity(ctx: CalcContext) -> AnalystResult:
    """How long each quote stands, where the supplier committed to a period at all."""
    columns = ["Supplier", "Validity (as stated)", "Days", "Conditional", "Received",
               "Expires", "Status"]
    rows: List[Dict[str, Any]] = []
    warnings: List[str] = []
    expired = conditional = unknown = 0

    for supplier in ctx.suppliers:
        bundle = ctx.bundle(supplier.id)
        if bundle is None:
            continue
        text = _terms_text(bundle, "quote_validity_text")
        quote = _first_priced(bundle)
        days = quote.quote_validity_days if quote else None
        is_conditional = bool(quote and quote.quote_validity_is_conditional)
        received_raw = (bundle.response.received_at or "")[:10]
        expires = ""
        status = "unknown"

        if is_conditional:
            conditional += 1
            status = "conditional"
            warnings.append("%s's quote is valid subject to a condition (%s), so it has no "
                            "fixed expiry date." % (supplier.name, text))
        elif days is not None and received_raw:
            try:
                start = _dt.datetime.strptime(received_raw, "%Y-%m-%d").date()
                end = start + _dt.timedelta(days=int(days))
                expires = end.isoformat()
                delta = (end - ctx.today).days
                if delta < 0:
                    status = "expired %d days ago" % abs(delta)
                    expired += 1
                    warnings.append("%s's quote lapsed on %s." % (supplier.name, expires))
                else:
                    status = "expires in %d days" % delta
            except (ValueError, TypeError):
                status = "unknown"
                unknown += 1
        else:
            unknown += 1

        rows.append({"Supplier": supplier.name, "Validity (as stated)": text or NOT_AVAILABLE,
                     "Days": days, "Conditional": "yes" if is_conditional else "no",
                     "Received": received_raw or NOT_AVAILABLE,
                     "Expires": expires or NOT_AVAILABLE, "Status": status})

    rows.sort(key=lambda r: r["Supplier"].casefold())
    summary = ("%d %s already lapsed, %d %s conditional and %d did not state a period."
               % (expired, _plural(expired, "quote"), conditional,
                  _plural(conditional, "is", "are"), unknown)) if rows else \
              "No supplier in scope has stated quote validity."
    metrics = {"expired": expired, "conditional": conditional, "not_stated": unknown,
               "today": ctx.today.isoformat()}
    assumptions = ["Validity is counted from the date each response was received, because "
                   "no supplier stated a start date.",
                   "A validity that depends on a condition is never turned into a date."]
    return _result(ctx, columns, _limit(ctx, rows), summary, metrics,
                   assumptions=assumptions, warnings=warnings)


#: Issues that stop a price being compared at all, as opposed to ones worth knowing.
_BLOCKING = {"probable_match", "unmatched", "unresolved_price", "conflict", "needs_review"}


def unresolved_issues(ctx: CalcContext) -> AnalystResult:
    """Everything worth settling before a decision, in one list."""
    columns = ["Kind", "Supplier", "Subject", "Detail", "Blocks comparison"]
    rows: List[Dict[str, Any]] = []
    kinds: Counter = Counter()
    in_scope = {s.name for s in ctx.suppliers}

    for item in review_items([ctx.bundle(s.id) for s in ctx.suppliers if ctx.bundle(s.id)]):
        if item.get("supplier") not in in_scope:
            continue
        line_id = item.get("line_item_id")
        if ctx.query.subject_line_ids and line_id and line_id not in ctx.query.subject_line_ids:
            continue
        kind = str(item.get("kind") or "")
        kinds[kind] += 1
        rows.append({"Kind": kind.replace("_", " "), "Supplier": item.get("supplier", ""),
                     "Subject": item.get("label") or line_id or "",
                     "Detail": (item.get("detail") or "")[:240],
                     "Blocks comparison": "yes" if kind in _BLOCKING else "no"})

    # Things the review queue does not track but a buyer still has to weigh.
    for supplier in ctx.suppliers:
        bundle = ctx.bundle(supplier.id)
        if bundle is None:
            kinds["no_response"] += 1
            rows.append({"Kind": "no response", "Supplier": supplier.name, "Subject": "",
                         "Detail": "invited and never replied", "Blocks comparison": "no"})
            continue
        constrained = sorted({q.line_item_id for q in bundle.quotes
                              if q.moq_constraint and q.line_item_id})
        if constrained:
            kinds["moq_constraint"] += 1
            first = _first_priced(bundle)
            rows.append({"Kind": "moq constraint", "Supplier": supplier.name,
                         "Subject": "%d lines" % len(constrained),
                         "Detail": "minimum order %s exceeds the quantity on %s"
                                   % (_quantity_text(first.minimum_order_quantity if first else None,
                                                     first.moq_unit if first else ""),
                                      ", ".join(constrained[:6])),
                         "Blocks comparison": "no"})
        first = _first_priced(bundle)
        if first is not None and first.quote_validity_is_conditional:
            kinds["conditional_validity"] += 1
            rows.append({"Kind": "conditional validity", "Supplier": supplier.name,
                         "Subject": "quote validity",
                         "Detail": first.quote_validity_text[:240],
                         "Blocks comparison": "no"})
        gaps = sum(1 for line in ctx.lines
                   if ctx.cell(line.id, supplier.id).state == CellState.NOT_QUOTED)
        if gaps:
            kinds["not_quoted_gap"] += 1
            rows.append({"Kind": "lines not quoted", "Supplier": supplier.name,
                         "Subject": "%d of %d lines" % (gaps, len(ctx.lines)),
                         "Detail": "no price was given for these lines",
                         "Blocks comparison": "no"})

    rows.sort(key=lambda r: (r["Blocks comparison"] != "yes", r["Supplier"].casefold(),
                             r["Kind"]))
    blocking = sum(1 for r in rows if r["Blocks comparison"] == "yes")
    summary = ("%d thing%s worth reviewing before you decide; %d of them stop a price "
               "being compared." % (len(rows), "" if len(rows) == 1 else "s", blocking)) \
        if rows else "Nothing in scope is waiting on a decision from you."
    metrics = {"total": len(rows), "blocking_total": blocking,
               "by_kind": dict(sorted(kinds.items()))}
    return _result(ctx, columns, _limit(ctx, rows), summary, metrics)


def qualification_status(ctx: CalcContext) -> AnalystResult:
    """Who has actually proved what, check by check."""
    columns = ["Supplier", "Overall", "Check", "Name", "Required", "Status", "Counts", "Note"]
    rows: List[Dict[str, Any]] = []
    evidence: List[EvidenceRef] = []
    by_status: Counter = Counter()

    for supplier in ctx.suppliers:
        qual = ctx.qualifications.get(supplier.id)
        if qual is None:
            continue
        by_status[qual.status] += 1
        if not qual.checks:
            rows.append({"Supplier": supplier.name, "Overall": qual.status.replace("_", " "),
                         "Check": NOT_AVAILABLE, "Name": NOT_AVAILABLE, "Required": "",
                         "Status": "", "Counts": "", "Note": "; ".join(qual.reasons)})
        for check in qual.checks:
            rows.append({
                "Supplier": supplier.name, "Overall": qual.status.replace("_", " "),
                "Check": check.kind, "Name": check.name,
                "Required": "yes" if check.required else "no",
                "Status": check.status,
                "Counts": ("yes (what-if)" if check.counted_by_hypothesis
                           else ("yes" if check.passed else "no")),
                "Note": check.note})
            evidence.extend(_evidence_refs(ctx, supplier.id, check.evidence_ids,
                                           EvidenceTopic.CERTIFICATION.value
                                           if check.kind == "certification"
                                           else EvidenceTopic.QUESTIONNAIRE.value))

    cleared = by_status.get(QualificationStatus.CLEARED.value, 0)
    summary = ("%d of %d supplier%s in scope %s cleared: %s."
               % (cleared, len(ctx.suppliers), "" if len(ctx.suppliers) == 1 else "s",
                  "has" if cleared == 1 else "have",
                  ", ".join(sorted(q.supplier_name for q in ctx.qualifications.values()
                                   if q.status == QualificationStatus.CLEARED.value
                                   and q.supplier_id in {s.id for s in ctx.suppliers}))
                  or "none")) if ctx.suppliers else "No supplier in scope."
    if not cleared:
        summary = ("No supplier in scope has a certification backed by a document we hold. "
                   "Every certification on file is a claim the supplier made.")
    metrics = {"by_status": dict(sorted(by_status.items()))}
    return _result(ctx, columns, _limit(ctx, rows), summary, metrics,
                   assumptions=qualification_assumptions(ctx.rfq, ctx.hyp), evidence=evidence)


def supplier_summary(ctx: CalcContext) -> AnalystResult:
    """One row per supplier: what they offered, all together."""
    checks, _ = _scan(ctx)
    columns = ["Supplier", "Country", "Status", "Received", "Comparable lines", "Currencies",
               "Lead time", "MOQ", "Validity", "Payment", "Delivery", "Certifications",
               "Questionnaire", "Open questions", "Qualification"]
    rows: List[Dict[str, Any]] = []

    for supplier in ctx.suppliers:
        bundle = ctx.bundle(supplier.id)
        qual = ctx.qualifications.get(supplier.id)
        if bundle is None:
            rows.append({"Supplier": supplier.name, "Country": supplier.country or NOT_AVAILABLE,
                         "Status": "no response", "Received": NOT_AVAILABLE,
                         "Comparable lines": 0, "Currencies": NOT_AVAILABLE,
                         "Lead time": NOT_AVAILABLE, "MOQ": NOT_AVAILABLE,
                         "Validity": NOT_AVAILABLE, "Payment": NOT_AVAILABLE,
                         "Delivery": NOT_AVAILABLE, "Certifications": NOT_AVAILABLE,
                         "Questionnaire": NOT_AVAILABLE, "Open questions": 0,
                         "Qualification": "not assessed"})
            continue
        comparable = sum(1 for line in ctx.lines
                         if (checks.get((line.id, supplier.id)) or PriceCheck()).comparable)
        currencies = sorted({q.currency for q in bundle.quotes if q.has_price and q.currency})
        first = _first_priced(bundle)
        certs = "; ".join("%s: %s" % (c.name, c.status.value) for c in bundle.certifications)
        answered = sum(1 for a in bundle.questionnaire if a.status != ClaimStatus.MISSING)
        rows.append({
            "Supplier": supplier.name, "Country": supplier.country or NOT_AVAILABLE,
            "Status": bundle.response.response_type.value.replace("_", " "),
            "Received": (bundle.response.received_at or "")[:10] or NOT_AVAILABLE,
            "Comparable lines": "%d of %d" % (comparable, len(ctx.lines)),
            "Currencies": ", ".join(currencies) or NOT_AVAILABLE,
            "Lead time": _terms_text(bundle, "lead_time_text") or NOT_AVAILABLE,
            "MOQ": _quantity_text(first.minimum_order_quantity if first else None,
                                  first.moq_unit if first else ""),
            "Validity": _terms_text(bundle, "quote_validity_text") or NOT_AVAILABLE,
            "Payment": _terms_text(bundle, "payment_terms") or NOT_AVAILABLE,
            "Delivery": _terms_text(bundle, "delivery_terms") or NOT_AVAILABLE,
            "Certifications": certs or NOT_AVAILABLE,
            "Questionnaire": "%d of %d answered" % (answered, len(bundle.questionnaire)),
            "Open questions": sum(1 for q in bundle.questions if not q.resolved),
            "Qualification": (qual.status.replace("_", " ") if qual else "unknown")})

    rows.sort(key=lambda r: r["Supplier"].casefold())
    summary = ("%d supplier%s in scope." % (len(rows), "" if len(rows) == 1 else "s")) \
        if len(rows) != 1 else \
        ("%s: %s comparable, lead time %s, MOQ %s."
         % (rows[0]["Supplier"], rows[0]["Comparable lines"], rows[0]["Lead time"],
            rows[0]["MOQ"]))
    metrics = {"suppliers": len(rows)}
    assumptions = [
        "This covers only the suppliers invited to this RFQ. I have no data on suppliers "
        "outside it, and nothing here measures actual product quality or past performance.",
        "Lead time, MOQ, validity, payment and delivery are stated once per response, so "
        "they are shown once per supplier rather than per line."]
    return _result(ctx, columns, _limit(ctx, rows), summary, metrics, assumptions=assumptions)


def why_excluded(ctx: CalcContext) -> AnalystResult:
    """Why one supplier is not the answer — line by line, with the evidence."""
    subject_ids = ctx.query.subject_supplier_ids
    if not subject_ids:
        return AnalystResult.refusal(ctx.query.reading, "I need to know which supplier you mean",
                                     ctx.query.intent, [s.name for s in ctx.matrix.suppliers])
    sid = subject_ids[0]
    supplier = next((s for s in ctx.matrix.suppliers if s.id == sid), None)
    name = supplier.name if supplier else sid

    columns = ["Line", "Status", "Reason", "Their price", "Cheapest here", "Gap"]
    rows: List[Dict[str, Any]] = []
    evidence: List[EvidenceRef] = []
    excluded_lines = 0
    cheapest_lines = 0

    # Anything that removed the supplier before we even got to prices.
    scope_reasons = [e for e in ctx.scope_exclusions if e.supplier_id == sid]
    qual = ctx.qualifications.get(sid)

    rivals = [s for s in ctx.suppliers if s.id != sid]
    for line in ctx.lines:
        cell = ctx.cell(line.id, sid)
        check = price_check(cell, ctx)
        best: Optional[Tuple[float, str]] = None
        for rival in rivals:
            rc = price_check(ctx.cell(line.id, rival.id), ctx)
            if rc.valid and (best is None or rc.amount < best[0]):
                best = (rc.amount, rival.name)

        if not check.comparable:
            excluded_lines += 1
            status = "excluded"
            reason = check.exclusion.reason if check.exclusion else "no comparable price"
            evidence.extend(_evidence_refs(ctx, sid, check.exclusion.evidence_ids if
                                           check.exclusion else [], EvidenceTopic.PRICE.value,
                                           line.id))
        elif check.moq_constraint:
            excluded_lines += 1
            status = "excluded"
            reason = check.exclusion.reason if check.exclusion else "minimum order too high"
            evidence.extend(_evidence_refs(ctx, sid, check.evidence_ids,
                                           EvidenceTopic.MOQ.value, line.id))
        elif best is None or check.amount <= best[0]:
            status = "cheapest" if best is None or check.amount < best[0] else "tie"
            reason = "lowest comparable price on this line"
            cheapest_lines += 1
        else:
            status = "valid, not cheapest"
            gap = check.amount - best[0]
            reason = ("%s above %s" % (_money(round(gap, 4), ctx.currency), best[1]))

        rows.append({
            "Line": line.id, "Status": status, "Reason": reason,
            "Their price": check.amount if check.comparable else NOT_AVAILABLE,
            "Cheapest here": best[0] if best else NOT_AVAILABLE,
            "Gap": (round(check.amount - best[0], 4)
                    if check.comparable and best else NOT_AVAILABLE)})

    prefix = []
    if scope_reasons:
        prefix.append("%s was set aside before prices were compared: %s."
                      % (name, "; ".join(e.reason for e in scope_reasons)))
    if qual and qual.status != QualificationStatus.CLEARED.value:
        prefix.append("Qualification: %s (%s)."
                      % (qual.status.replace("_", " "), "; ".join(qual.reasons) or "no reason recorded"))
        evidence.extend(_evidence_refs(ctx, sid, qual.failing_evidence_ids(),
                                       EvidenceTopic.CERTIFICATION.value))

    if excluded_lines:
        body = ("%s has no usable price on %d of %d line%s in scope."
                % (name, excluded_lines, len(ctx.lines), "" if len(ctx.lines) == 1 else "s"))
    elif cheapest_lines:
        body = ("%s is not excluded anywhere: it is lowest on %d of %d lines."
                % (name, cheapest_lines, len(ctx.lines)))
    else:
        body = ("%s is comparable on every line in scope but is not lowest on any of them."
                % name)
    summary = " ".join(prefix + [body])
    metrics = {"lines_excluded": excluded_lines, "lines_cheapest": cheapest_lines,
               "lines_in_scope": len(ctx.lines)}
    return _result(ctx, columns, _limit(ctx, rows), summary, metrics,
                   assumptions=qualification_assumptions(ctx.rfq, ctx.hyp)
                   if qual and qual.status != QualificationStatus.CLEARED.value else None,
                   evidence=evidence)


#: Terms Phase 2 extracts but whose source span it does not keep. Saying so is better than
#: quietly showing the wording as if we knew where it came from.
_TERMS_WITHOUT_SPANS = {
    EvidenceTopic.LEAD_TIME.value: "lead_time_text",
    EvidenceTopic.VALIDITY.value: "quote_validity_text",
    EvidenceTopic.PAYMENT_TERMS.value: "payment_terms",
    EvidenceTopic.DELIVERY_TERMS.value: "delivery_terms",
}


def evidence_lookup(ctx: CalcContext) -> AnalystResult:
    """Where a particular value came from, in the supplier's own document."""
    target = ctx.query.evidence_target
    if target is None or not target.is_set():
        return AnalystResult.refusal(ctx.query.reading,
                                     "Tell me which value you want the source for",
                                     ctx.query.intent)
    sid = target.supplier or ""
    supplier = next((s for s in ctx.matrix.suppliers if s.id == sid), None)
    name = supplier.name if supplier else sid
    bundle = ctx.bundle(sid)
    columns = ["Topic", "Value", "Document", "Location", "Quoted text", "Found in document"]
    rows: List[Dict[str, Any]] = []
    evidence: List[EvidenceRef] = []
    warnings: List[str] = []

    if bundle is None:
        return AnalystResult.refusal(ctx.query.reading,
                                     "%s did not respond, so there is no document to cite" % name,
                                     ctx.query.intent)

    topic = target.topic
    value_text = ""
    ids: List[str] = []

    if topic == EvidenceTopic.PRICE.value:
        cell = ctx.cell(target.line or "", sid)
        quote = cell.quote
        if quote is None:
            return AnalystResult.refusal(
                ctx.query.reading, "%s did not quote %s" % (name, target.line), ctx.query.intent)
        value_text = "%s (normalised %s)" % (quote.original_price_text() or NOT_AVAILABLE,
                                             quote.normalized_price_text() or NOT_AVAILABLE)
        ids = list(quote.evidence_ids)
    elif topic == EvidenceTopic.MOQ.value:
        quote = _first_priced(bundle)
        value_text = _quantity_text(quote.minimum_order_quantity if quote else None,
                                    quote.moq_unit if quote else "")
        ids = list(quote.evidence_ids) if quote else []
    elif topic == EvidenceTopic.DISCOUNT.value:
        quote = _first_priced(bundle)
        value_text = quote.discount.describe() if quote and quote.discount.is_present else "none stated"
        ids = list(quote.discount.evidence_ids) if quote else []
    elif topic == EvidenceTopic.CERTIFICATION.value:
        want = canonical_cert_name(target.name or "")
        cert = next((c for c in bundle.certifications
                     if canonical_cert_name(c.name or c.raw_name) == want), None)
        if cert is None:
            return AnalystResult.refusal(
                ctx.query.reading, "%s did not mention %s" % (name, want), ctx.query.intent,
                [c.name for c in bundle.certifications])
        value_text = "%s — %s%s" % (cert.name, cert.status.value,
                                    (", certificate %s" % cert.certificate_number)
                                    if cert.certificate_number else "")
        ids = list(cert.evidence_ids)
        if cert.status == ClaimStatus.CLAIMED:
            warnings.append("This is a claim: no certificate we hold supports it%s."
                            % (", though the supplier refers to %s" % cert.document_reference
                               if cert.document_reference else ""))
    elif topic == EvidenceTopic.QUESTIONNAIRE.value:
        answer = next((a for a in bundle.questionnaire if a.field_key == target.name), None)
        if answer is None:
            return AnalystResult.refusal(
                ctx.query.reading, "%s did not answer '%s'" % (name, target.name),
                ctx.query.intent, [a.field_key for a in bundle.questionnaire if a.field_key])
        value_text = "%s — %s" % (answer.answer or NOT_AVAILABLE, answer.status.value)
        ids = list(answer.evidence_ids)
    elif topic == EvidenceTopic.CONFLICT.value:
        want = norm_text(target.name or "")
        for q in bundle.quotes:
            for c in (q.conflicts or []):
                if want and want not in norm_text(str(c.get("topic") or "")):
                    continue
                value_text = _describe_conflicts(q)
                ids = [v.get("evidence_id") for v in (c.get("values") or []) if v.get("evidence_id")]
                break
            if ids:
                break
        if not value_text:
            return AnalystResult.refusal(
                ctx.query.reading, "%s has no recorded contradiction about '%s'"
                % (name, target.name), ctx.query.intent)
    elif topic in _TERMS_WITHOUT_SPANS:
        value_text = _terms_text(bundle, _TERMS_WITHOUT_SPANS[topic]) or "not stated"
        warnings.append("Phase 2 did not record where this term appeared in the document, "
                        "so I can show the wording it extracted but not a location.")

    refs = _evidence_refs(ctx, sid, ids, topic, target.line)
    evidence.extend(refs)
    if refs:
        for ref in refs:
            rows.append({"Topic": topic.replace("_", " "), "Value": value_text,
                         "Document": ref.document_name or NOT_AVAILABLE,
                         "Location": ref.location or NOT_AVAILABLE,
                         "Quoted text": ref.quoted_text,
                         "Found in document": "yes" if ref.verified else "no"})
    else:
        rows.append({"Topic": topic.replace("_", " "), "Value": value_text,
                     "Document": NOT_AVAILABLE, "Location": NOT_AVAILABLE,
                     "Quoted text": NOT_AVAILABLE, "Found in document": "no span recorded"})

    where = refs[0].location if refs else "no recorded source span"
    summary = "%s — %s: %s. Source: %s." % (name, topic.replace("_", " "), value_text, where)
    return _result(ctx, columns, rows, summary, {"spans": len(refs)},
                   warnings=warnings, evidence=evidence)


def rfq_completeness(ctx: CalcContext) -> AnalystResult:
    """How much of this RFQ has a usable answer, and what is holding the rest up."""
    checks, _ = _scan(ctx)
    matrix_summary = ctx.matrix.summary or {}
    lines = len(ctx.rfq.line_items) or 1
    suppliers = len(ctx.matrix.suppliers) or 1

    comparable_cells = sum(1 for c in checks.values() if c.comparable)
    lines_with_quote = sum(1 for line in ctx.lines
                           if any((checks.get((line.id, s.id)) or PriceCheck()).comparable
                                  for s in ctx.suppliers))

    columns = ["Area", "Metric", "Value", "Note"]
    rows: List[Dict[str, Any]] = [
        {"Area": "RFQ", "Metric": "Readiness score",
         "Value": ctx.rfq.completeness.score,
         "Note": "supplier-ready" if ctx.rfq.completeness.ready_to_send else "not yet ready to send"},
        {"Area": "RFQ", "Metric": "Line items", "Value": lines, "Note": ""},
        {"Area": "Responses", "Metric": "Suppliers invited",
         "Value": matrix_summary.get("suppliers_total", suppliers), "Note": ""},
        {"Area": "Responses", "Metric": "Responses received",
         "Value": matrix_summary.get("responses_received", len(ctx.matrix.bundles)), "Note": ""},
        {"Area": "Responses", "Metric": "No response",
         "Value": matrix_summary.get("no_response", 0), "Note": "invited and never replied"},
        {"Area": "Quotes", "Metric": "Lines with a comparable quote",
         "Value": "%d of %d" % (lines_with_quote, len(ctx.lines)),
         "Note": "%s%% of the RFQ" % _pct(lines_with_quote, len(ctx.lines) or 1)},
        {"Area": "Quotes", "Metric": "Comparable line-supplier cells",
         "Value": comparable_cells,
         "Note": "out of %d" % (len(ctx.lines) * len(ctx.suppliers))},
        {"Area": "Quotes", "Metric": "Responses needing review",
         "Value": matrix_summary.get("need_review", 0), "Note": ""},
        {"Area": "Quotes", "Metric": "Currencies quoted",
         "Value": ", ".join(matrix_summary.get("currencies", [])) or NOT_AVAILABLE, "Note": ""},
    ]
    if matrix_summary.get("unnamed_currency"):
        rows.append({"Area": "Quotes", "Metric": "Prices in an unnamed currency",
                     "Value": matrix_summary["unnamed_currency"],
                     "Note": "cannot be compared until the currency is confirmed"})
    if not required_certifications(ctx.rfq):
        rows.append({"Area": "RFQ", "Metric": "Required certifications", "Value": "none named",
                     "Note": "so \"cleared\" falls back to holding any document-backed certificate"})
    no_qty = [li.id for li in ctx.rfq.line_items if not li.quantity]
    if no_qty:
        rows.append({"Area": "RFQ", "Metric": "Lines without a quantity", "Value": len(no_qty),
                     "Note": "minimum order cannot be checked on %s" % ", ".join(no_qty[:5])})

    pct = _pct(lines_with_quote, len(ctx.lines) or 1)
    summary = ("%s%% of this RFQ has a comparable quote — %d of %d lines. A quote counts "
               "when its price basis is normalised, its currency is readable and its line "
               "match is confirmed." % (pct, lines_with_quote, len(ctx.lines)))
    metrics = {"percent_with_valid_quote": pct, "numerator": lines_with_quote,
               "denominator": len(ctx.lines), "comparable_cells": comparable_cells}
    return _result(ctx, columns, rows, summary, metrics)


def lookup(ctx: CalcContext) -> AnalystResult:
    """The general reader: show whichever stored fields the question asked about.

    Everything projected here is a value Phase 2 recorded. Prices still go through the
    same comparability rule as everywhere else, so a lookup cannot put a figure on screen
    that a comparison would have refused.
    """
    query = ctx.query
    fields = query.fields or ["supplier"]
    grain = query.grain
    checks, cell_exclusions = _scan(ctx)
    # A per-line exclusion explains a missing price. When the question is about suppliers
    # rather than prices, listing one per cell buries the answer, so only carry them when
    # the rows actually show a price.
    shows_price = grain != Grain.SUPPLIER.value or any(f in QUOTE_FIELDS for f in fields)
    excluded = cell_exclusions if shows_price else []
    rows: List[Dict[str, Any]] = []

    def supplier_value(supplier: Supplier, key: str) -> Any:
        bundle = ctx.bundle(supplier.id)
        if key == "supplier":
            return supplier.name
        if key == "country":
            return supplier.country or NOT_AVAILABLE
        if key == "response_status":
            return "no response" if bundle is None else supplier.status.value.replace("_", " ")
        if bundle is None:
            return NOT_AVAILABLE
        if key == "response_type":
            return bundle.response.response_type.value.replace("_", " ")
        if key == "received_at":
            return (bundle.response.received_at or "")[:10] or NOT_AVAILABLE
        if key == "currencies":
            return ", ".join(sorted({q.currency for q in bundle.quotes
                                     if q.has_price and q.currency})) or NOT_AVAILABLE
        if key == "lead_time":
            return _terms_text(bundle, "lead_time_text") or NOT_AVAILABLE
        if key == "lead_time_days":
            return _lead_time_days(bundle)
        if key == "moq":
            first = _first_priced(bundle)
            return _quantity_text(first.minimum_order_quantity if first else None,
                                  first.moq_unit if first else "")
        if key == "validity":
            return _terms_text(bundle, "quote_validity_text") or NOT_AVAILABLE
        if key == "payment_terms":
            return _terms_text(bundle, "payment_terms") or NOT_AVAILABLE
        if key == "delivery_terms":
            return _terms_text(bundle, "delivery_terms") or NOT_AVAILABLE
        if key == "certifications":
            return "; ".join("%s: %s" % (c.name, c.status.value)
                             for c in bundle.certifications) or NOT_AVAILABLE
        if key == "qualification":
            qual = ctx.qualifications.get(supplier.id)
            return qual.status.replace("_", " ") if qual else NOT_AVAILABLE
        if key == "lines_quoted":
            return sum(1 for line in ctx.lines
                       if (checks.get((line.id, supplier.id)) or PriceCheck()).comparable)
        if key == "open_questions":
            return sum(1 for q in bundle.questions if not q.resolved)
        if key == "issues":
            seen: List[str] = []
            for q in bundle.quotes:
                for issue in q.issues:
                    if issue not in seen:
                        seen.append(issue)
            return "; ".join(seen[:6]) or NOT_AVAILABLE
        if key == "documents":
            return ", ".join(d.filename for d in bundle.documents) or NOT_AVAILABLE
        if key.startswith(QUESTIONNAIRE_PREFIX):
            fkey = key[len(QUESTIONNAIRE_PREFIX):]
            answer = next((a for a in bundle.questionnaire if a.field_key == fkey), None)
            if answer is None:
                return "not answered"
            return "%s (%s)" % (answer.answer or NOT_AVAILABLE, answer.status.value)
        return NOT_AVAILABLE

    def quote_value(line: LineItem, supplier: Optional[Supplier], key: str) -> Any:
        if key == "line":
            return line.id
        if key == "item":
            return _line_label(line)
        if key == "qty":
            return line.quantity
        if key == "unit":
            return line.unit
        if supplier is None:
            return NOT_AVAILABLE
        cell = ctx.cell(line.id, supplier.id)
        check = checks.get((line.id, supplier.id)) or PriceCheck()
        quote = cell.quote
        if key == "price":
            return check.amount if check.comparable else NOT_AVAILABLE
        if key == "as_quoted":
            return (quote.original_price_text() or NOT_AVAILABLE) if quote else NOT_AVAILABLE
        if key == "rate":
            return check.rate_note or NOT_AVAILABLE
        if key == "state":
            return cell.state.replace("_", " ")
        if key == "match_status":
            return quote.match_status.value.replace("_", " ") if quote else NOT_AVAILABLE
        if key == "normalization_note":
            return (quote.normalization_note or NOT_AVAILABLE) if quote else NOT_AVAILABLE
        if key == "moq_fits":
            if quote is None or quote.minimum_order_quantity is None:
                return "MOQ not stated"
            return "no" if quote.moq_constraint else "yes"
        if key == "discount":
            return (quote.discount.describe() if quote and quote.discount.is_present
                    else NOT_AVAILABLE)
        if key == "supplier_line_label":
            return (quote.supplier_line_label or NOT_AVAILABLE) if quote else NOT_AVAILABLE
        if key == "evidence":
            refs = _evidence_refs(ctx, supplier.id, quote.evidence_ids if quote else [],
                                  EvidenceTopic.PRICE.value, line.id)
            return refs[0].location if refs else NOT_AVAILABLE
        return NOT_AVAILABLE

    evidence: List[EvidenceRef] = []
    if grain == Grain.SUPPLIER.value:
        for supplier in ctx.suppliers:
            row = OrderedDict()
            for key in fields:
                row[_field_label(key)] = supplier_value(supplier, key)
            if supplier.id in ctx.match_notes:
                row["Matched on"] = ctx.match_notes[supplier.id]
            rows.append(dict(row))
    elif grain == Grain.LINE.value:
        for line in ctx.lines:
            row = OrderedDict()
            for key in fields:
                row[_field_label(key)] = quote_value(line, None, key)
            rows.append(dict(row))
    else:
        for line in ctx.lines:
            for supplier in ctx.suppliers:
                cell = ctx.cell(line.id, supplier.id)
                check = checks.get((line.id, supplier.id))
                if check is None:
                    continue
                row = OrderedDict()
                for key in fields:
                    row[_field_label(key)] = (supplier_value(supplier, key)
                                              if key in ("supplier", "country",
                                                         "response_status", "qualification")
                                              or key.startswith(QUESTIONNAIRE_PREFIX)
                                              else quote_value(line, supplier, key))
                if supplier.id in ctx.match_notes:
                    row["Matched on"] = ctx.match_notes[supplier.id]
                rows.append(dict(row))
                evidence.extend(_evidence_refs(ctx, supplier.id,
                                               cell.quote.evidence_ids if cell.quote else [],
                                               EvidenceTopic.PRICE.value, line.id)[:1])

    columns = [_field_label(k) for k in fields]
    if ctx.match_notes:
        columns.append("Matched on")

    sort_key = _field_label(query.sort_by) if query.sort_by else None
    if sort_key and sort_key in columns:
        def order(row: Dict[str, Any]) -> Tuple[int, Any, str]:
            value = row.get(sort_key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return (0, value, "")
            return (1, 0, str(value).casefold())
        rows.sort(key=order, reverse=bool(query.descending))
    else:
        rows.sort(key=lambda r: tuple(str(r.get(c, "")).casefold() for c in columns[:2]))

    if query.group_by:
        group_col = _field_label(query.group_by)
        if group_col in columns:
            rows.sort(key=lambda r: str(r.get(group_col, "")).casefold())

    described = [_describe_filter(ctx, f) for f in ctx.query.filters]
    if rows:
        summary = ("%d row%s: %s across %d supplier%s in scope."
                   % (len(rows), "" if len(rows) == 1 else "s",
                      ", ".join(_field_label(f) for f in fields[:4]),
                      len(ctx.suppliers), "" if len(ctx.suppliers) == 1 else "s"))
        if described:
            summary = ("%d of %d supplier%s match: %s."
                       % (len(rows) if grain == Grain.SUPPLIER.value else len(ctx.suppliers),
                          len(ctx.matrix.suppliers), "" if len(ctx.matrix.suppliers) == 1 else "s",
                          "; ".join(described)))
    else:
        summary = ("Nothing in this RFQ matches%s."
                   % ((" " + "; ".join(described)) if described else ""))

    metrics = {"rows": len(rows), "suppliers_in_scope": len(ctx.suppliers),
               "lines_in_scope": len(ctx.lines)}
    return _result(ctx, columns, _limit(ctx, rows), summary, metrics, excluded,
                   evidence=evidence[:40])


def _describe_filter(ctx: CalcContext, f) -> str:
    """A filter in the buyer's words. Supplier ids are internal; names are what they typed."""
    if f.field in ("supplier",):
        readable = Filter(f.field, f.op, [ctx.name(v) for v in f.values])
        return readable.describe()
    return f.describe()


def _field_label(key: Optional[str]) -> str:
    if not key:
        return ""
    if key.startswith(QUESTIONNAIRE_PREFIX):
        return key[len(QUESTIONNAIRE_PREFIX):].replace("_", " ").capitalize()
    return key.replace("_", " ").capitalize()


def _figure_variants(value: Optional[float]) -> List[str]:
    """The ways a number could be written in a document, for matching a span to a figure."""
    if value is None:
        return []
    out = {("%g" % value), ("%.2f" % value), ("%.4f" % value).rstrip("0").rstrip(".")}
    if float(value).is_integer():
        out.add("{:,.0f}".format(value))
        out.add("%d" % int(value))
    return [v for v in out if v]


#: Words that mark a span as being about a minimum order rather than a unit price.
_MOQ_WORDS = re.compile(r"\b(minimum|moq|min\.? order)\b", re.I)


def _prefer_spans(ctx: CalcContext, supplier_id: str, ids: List[str], topic: str,
                  line_id: Optional[str]) -> List[str]:
    """Phase 2 files a quote's price span and its MOQ span in one list, so asking for a
    price can otherwise cite the minimum-order sentence. Score each span for the figure
    actually being asked about and keep the best; on a tie, keep them all rather than
    pick arbitrarily."""
    if topic not in (EvidenceTopic.PRICE.value, EvidenceTopic.MOQ.value) or len(ids) < 2:
        return ids
    bundle = ctx.bundle(supplier_id)
    if bundle is None:
        return ids
    quote = ctx.cell(line_id, supplier_id).quote if line_id else None
    if quote is None:
        quote = _first_priced(bundle)
    if quote is None:
        return ids

    want_moq = topic == EvidenceTopic.MOQ.value
    figures = _figure_variants(quote.minimum_order_quantity if want_moq else quote.unit_price)

    scored: List[Tuple[int, str]] = []
    for eid in ids:
        ev = bundle.evidence.get(eid)
        if ev is None:
            continue
        text = ev.quoted_text or ""
        score = 0
        if figures and any(v in text for v in figures):
            score += 1
        mentions_moq = bool(_MOQ_WORDS.search(text))
        score += 2 if (mentions_moq == want_moq) else -2
        scored.append((score, eid))
    if not scored:
        return ids
    best = max(sc for sc, _ in scored)
    return [eid for sc, eid in scored if sc == best]


def _evidence_refs(ctx: CalcContext, supplier_id: str, ids: List[str], topic: str,
                   line_id: Optional[str] = None) -> List[EvidenceRef]:
    """Resolve stored evidence ids into displayable references. Never invents one."""
    bundle = ctx.bundle(supplier_id)
    if bundle is None:
        return []
    ids = _prefer_spans(ctx, supplier_id, list(ids or []), topic, line_id)
    out: List[EvidenceRef] = []
    for eid in ids or []:
        ev = bundle.evidence.get(eid)
        if ev is None:
            continue
        out.append(EvidenceRef(
            evidence_id=eid, supplier_name=ctx.name(supplier_id), line_id=line_id, topic=topic,
            document_name=ev.document_name, location=ev.describe(),
            quoted_text=ev.quoted_text, verified=bool(ev.verified)))
    return out


#: Intent -> calculation. The service looks the function up here; there is no other way in.
CALCULATIONS: Dict[str, Callable[[CalcContext], AnalystResult]] = {
    Intent.LOOKUP.value: lookup,
    Intent.COMPARE_PRICES.value: compare_prices,
    Intent.CHEAPEST_BY_LINE.value: cheapest_by_line,
    Intent.PRICE_DIFFERENCE.value: price_difference,
    Intent.SUPPLIER_COVERAGE.value: supplier_coverage,
    Intent.LINE_COVERAGE.value: line_coverage,
    Intent.MISSING_QUOTES.value: missing_quotes,
    Intent.LEAD_TIME_COMPARISON.value: lead_time_comparison,
    Intent.MOQ_CHECK.value: moq_check,
    Intent.QUOTE_VALIDITY.value: quote_validity,
    Intent.UNRESOLVED_ISSUES.value: unresolved_issues,
    Intent.QUALIFICATION_STATUS.value: qualification_status,
    Intent.SUPPLIER_SUMMARY.value: supplier_summary,
    Intent.WHY_EXCLUDED.value: why_excluded,
    Intent.EVIDENCE_LOOKUP.value: evidence_lookup,
    Intent.RFQ_COMPLETENESS.value: rfq_completeness,
}
