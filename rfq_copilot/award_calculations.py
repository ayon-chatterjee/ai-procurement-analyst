"""Who would get each line, and what it would cost.

Two proposals per line. **Cheapest** is the lowest price the application is willing to
compare. **Best value** is the lowest price from a supplier that also clears the bars the
buyer set — a certification they can evidence, a quote validity that is a period rather
than a condition, and a lead time within a stated limit.

Best value is deliberately not a weighted score. A score of 87.3 is a number this data
cannot support, and it hides the thing the buyer actually needs: *why* one supplier beat
another. A bar produces a sentence instead — "Anhui is USD 0.045 cheaper, but its ISO 9001
is claimed rather than document-backed" — which a buyer can disagree with.

Everything here reuses Phase 3's engine rather than reimplementing it: the same
`price_check`, the same comparison currency, the same exclusion vocabulary. Two answers to
"is this price comparable" would eventually disagree, and the disagreement would surface as
a number on a purchase order.

Pure: no database, no model, no Streamlit.
"""
from __future__ import annotations

import datetime as _dt
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from .analyst_calculations import (
    build_context, choose_comparison_currency, first_priced, lead_time_days, line_label,
    price_check, scan_prices, terms_text,
)
from .analyst_models import AnalystQuery, Hypothetical, Intent, QualificationStatus
from .award_models import (
    Award, AwardLine, AwardProposal, AwardThresholds, AwardTotals, BarResult, Candidate,
    LineProposal, PickSource, SupplierSubtotal,
)
from .supplier_service import ComparisonMatrix


def _money(value: Optional[float], currency: Optional[str]) -> str:
    if value is None:
        return "—"
    text = ("%.4f" % value).rstrip("0").rstrip(".")
    return "%s %s" % (currency, text) if currency else text


# --------------------------------------------------------------------------- #
# The bars
# --------------------------------------------------------------------------- #
def clears_bars(ctx, supplier_id: str, thresholds: AwardThresholds
                ) -> Tuple[bool, List[BarResult]]:
    """Whether a supplier meets the buyer's requirements, and what it failed if not.

    The bars are read once per supplier, not once per quote: Phase 2 copies lead time,
    validity and the rest onto every quote row, so asking thirty quotes the same question
    would answer it thirty times.
    """
    name = ctx.name(supplier_id)
    bundle = ctx.bundle(supplier_id)
    failures: List[BarResult] = []

    if bundle is None:
        return False, [BarResult(supplier_id, name, "qualification",
                                 "did not respond to this RFQ")]

    qualification = ctx.qualifications.get(supplier_id)
    status = qualification.status if qualification else QualificationStatus.NOT_ASSESSED.value
    if thresholds.require_document_backed_certification:
        passed = status == QualificationStatus.CLEARED.value
        reason = {
            QualificationStatus.UNVERIFIED.value:
                "its certifications are claimed rather than backed by a document we hold",
            QualificationStatus.NOT_CLEARED.value: "it did not clear the quality checks",
            QualificationStatus.NOT_ASSESSED.value: "it has no certification on file",
        }.get(status, "its qualification is %s" % status.replace("_", " "))
    else:
        # Relaxing the bar is applied *here*, not by setting the analyst's
        # `treat_claimed_as_verified` hypothetical: that would rewrite the Qualification
        # object and mark every check as promoted, which would misdescribe a supplier who
        # genuinely holds a document.
        passed = status in (QualificationStatus.CLEARED.value,
                            QualificationStatus.UNVERIFIED.value)
        reason = ("it did not clear the quality checks"
                  if status == QualificationStatus.NOT_CLEARED.value
                  else "it has no certification on file")
    if not passed:
        failures.append(BarResult(supplier_id, name, "qualification", reason,
                                  qualification.failing_evidence_ids() if qualification else []))

    quote = first_priced(bundle)
    if thresholds.require_firm_validity and quote is not None and quote.quote_validity_is_conditional:
        failures.append(BarResult(
            supplier_id, name, "validity",
            "its quote validity is a condition rather than a fixed period (%s)"
            % (quote.quote_validity_text or "not stated")[:120]))

    if thresholds.max_lead_time_days is not None:
        contradiction = _lead_time_contradiction(bundle)
        days = lead_time_days(bundle)
        if contradiction:
            failures.append(BarResult(supplier_id, name, "lead_time",
                                      "it gave more than one lead time (%s)" % contradiction))
        elif days is None:
            # A supplier who did not state a lead time is not assumed to meet one — the
            # same stance the analyst takes for an unstated minimum order.
            failures.append(BarResult(supplier_id, name, "lead_time",
                                      "it did not state a lead time we could read as days"))
        elif days > thresholds.max_lead_time_days:
            failures.append(BarResult(
                supplier_id, name, "lead_time",
                "its lead time of %g days is above the %g you set"
                % (days, thresholds.max_lead_time_days)))

    return (not failures), failures


def _lead_time_contradiction(bundle) -> str:
    for q in bundle.quotes:
        for c in (q.conflicts or []):
            topic = str(c.get("topic") or "").lower()
            if "lead" in topic or "deliver" in topic:
                values = [str(v.get("value")) for v in (c.get("values") or []) if v.get("value")]
                if values:
                    return " vs ".join(values)
    return ""


# --------------------------------------------------------------------------- #
# Seeding
# --------------------------------------------------------------------------- #
def propose_award(matrix: ComparisonMatrix, thresholds: AwardThresholds,
                  currency: Optional[str] = None, session_currency: Optional[str] = None,
                  today: Optional[_dt.date] = None) -> AwardProposal:
    """What the application would suggest, before the buyer has said anything.

    Never persisted. It is a pure function of the comparison and the thresholds, and a
    stored copy would go stale the moment a quote was corrected.
    """
    query = AnalystQuery(intent=Intent.CHEAPEST_BY_LINE.value, hypothetical=Hypothetical(),
                         comparison_currency=currency)
    chosen, reason = choose_comparison_currency(matrix, query, session_currency)
    ctx = build_context(matrix, query, chosen, reason, today=today,
                        session_currency=session_currency)
    checks, _ = scan_prices(ctx)

    bars: Dict[str, Tuple[bool, List[BarResult]]] = {
        s.id: clears_bars(ctx, s.id, thresholds) for s in ctx.suppliers}

    proposal = AwardProposal(
        rfq_id=matrix.rfq.id, currency=chosen, currency_reason=reason,
        thresholds=thresholds, rate_provenance=ctx.rate_provenance(),
        supplier_names=dict(ctx.names))

    for line in ctx.lines:
        entry = LineProposal(line_item_id=line.id, line_label=line_label(line),
                             quantity=line.quantity, unit=line.unit)
        candidates: List[Candidate] = []
        for supplier in ctx.suppliers:
            check = checks.get((line.id, supplier.id))
            if check is None:
                continue
            if check.exclusion is not None and not check.valid:
                entry.exclusions.append(check.exclusion)
            if not check.valid:
                continue
            cell = ctx.cell(line.id, supplier.id)
            quote = cell.quote
            bundle = ctx.bundle(supplier.id)
            qualification = ctx.qualifications.get(supplier.id)
            candidates.append(Candidate(
                supplier_id=supplier.id, supplier_name=supplier.name, amount=check.amount,
                native=check.native, native_unit_price=quote.normalized_unit_price if quote else None,
                native_currency=quote.currency if quote else "", rate_note=check.rate_note,
                caveats=list(check.caveats), evidence_ids=list(check.evidence_ids),
                quote_id=quote.id if quote else "",
                response_id=quote.response_id if quote else "",
                qualification=qualification.status if qualification else "",
                lead_time_days=lead_time_days(bundle) if bundle else None,
                validity_is_conditional=bool(
                    first_priced(bundle).quote_validity_is_conditional) if bundle and first_priced(bundle) else False))

        candidates.sort(key=lambda c: (c.amount, c.supplier_name.casefold()))
        entry.valid_candidates = len(candidates)
        entry.cheapest = candidates[0] if candidates else None

        clearing = [c for c in candidates if bars.get(c.supplier_id, (False, []))[0]]
        entry.best_value = clearing[0] if clearing else None

        if entry.cheapest is None:
            entry.absent_reason = ("No supplier has a price for this line that can be "
                                   "compared." if not entry.exclusions else
                                   "No comparable price: " + entry.exclusions[0].reason)
        elif entry.best_value is None:
            failed = [c for c in candidates if not bars.get(c.supplier_id, (False, []))[0]]
            reasons = []
            for c in failed[:3]:
                for bar in bars.get(c.supplier_id, (False, []))[1][:1]:
                    reasons.append("%s — %s" % (c.supplier_name, bar.reason))
                    entry.bars_failed.append(bar)
            entry.absent_reason = ("No supplier that quoted this line clears the bars: "
                                   + "; ".join(reasons) + ".")

        if entry.cheapest and entry.best_value:
            entry.same_supplier = entry.cheapest.supplier_id == entry.best_value.supplier_id
            if not entry.same_supplier:
                entry.difference_note = _difference_note(entry, bars, chosen)

        proposal.lines.append(entry)

    proposal.assumptions = threshold_assumptions(thresholds, chosen, reason)
    proposal.warnings = list(ctx.warnings)
    empty = proposal.lines_with_no_best_value
    if empty and len(empty) < len(proposal.lines):
        proposal.warnings.append(
            "%d of %d lines have no best-value candidate, so only the cheapest quote is "
            "offered there." % (len(empty), len(proposal.lines)))
    elif empty and len(empty) == len(proposal.lines) and proposal.lines:
        proposal.warnings.append(
            "No supplier on this RFQ clears the bars you set, so best value is empty on "
            "every line. Relaxing the certification bar is a recorded decision.")
    return proposal


def _difference_note(entry: LineProposal, bars: Dict[str, Tuple[bool, List[BarResult]]],
                     currency: Optional[str]) -> str:
    """One sentence saying why the two proposals differ. Composed here, never by a model."""
    cheap, best = entry.cheapest, entry.best_value
    if cheap is None or best is None or cheap.amount is None or best.amount is None:
        return ""
    gap = best.amount - cheap.amount
    failed = bars.get(cheap.supplier_id, (False, []))[1]
    why = failed[0].reason if failed else "it does not clear the bars"
    return ("%s is %s cheaper, but %s. %s is the lowest price that clears the bars."
            % (cheap.supplier_name, _money(round(gap, 4), currency), why, best.supplier_name))


def threshold_assumptions(thresholds: AwardThresholds, currency: Optional[str],
                          currency_reason: str) -> List[str]:
    out: List[str] = []
    if currency:
        out.append("Prices are compared in %s, taken from %s." % (currency, currency_reason))
    out.append(thresholds.describe())
    if not thresholds.require_document_backed_certification:
        out.append("You relaxed the quality bar: a certification the supplier states, with "
                   "no document we hold, counts towards best value. The records still show "
                   "it as a claim.")
    out.append("Cheapest is the lowest price the application is willing to compare: its "
               "price basis is normalised, its currency is readable, its line match is "
               "confirmed and its minimum order fits this line's quantity.")
    return out


# --------------------------------------------------------------------------- #
# Turning a proposal into award lines
# --------------------------------------------------------------------------- #
def line_from_candidate(line: LineProposal, candidate: Optional[Candidate],
                        pick_source: str, reason: str = "") -> AwardLine:
    """One award line. The arithmetic happens here, never in a model."""
    entry = AwardLine(
        line_item_id=line.line_item_id, line_label=line.line_label,
        quantity=line.quantity, unit=line.unit, pick_source=pick_source,
        override_reason=reason,
        seeded_cheapest=line.cheapest.to_dict() if line.cheapest else None,
        seeded_best_value=line.best_value.to_dict() if line.best_value else None,
        difference_note=line.difference_note, absent_reason=line.absent_reason)
    if candidate is None:
        return entry
    entry.supplier_id = candidate.supplier_id
    entry.supplier_name = candidate.supplier_name
    entry.quote_id = candidate.quote_id
    entry.response_id = candidate.response_id
    entry.unit_price = candidate.amount
    entry.native_unit_price = candidate.native_unit_price
    entry.native_currency = candidate.native_currency
    entry.native_text = candidate.native
    entry.rate_note = candidate.rate_note
    entry.caveats = list(candidate.caveats)
    entry.evidence_ids = list(candidate.evidence_ids)
    entry.extended = extended_for(line.quantity, candidate.amount)
    return entry


def extended_for(quantity: Optional[float], unit_price: Optional[float]) -> Optional[float]:
    """A line's money. `None` when either half is missing — never 0, which would read as free."""
    if quantity is None or unit_price is None:
        return None
    return round(float(quantity) * float(unit_price), 2)


# --------------------------------------------------------------------------- #
# Totals
# --------------------------------------------------------------------------- #
def award_totals(award: Award, rate_provenance: Optional[Dict[str, Any]] = None) -> AwardTotals:
    """What the award costs, with its denominator attached.

    Rounding happens once, at the line, and subtotals sum those rounded figures — so the
    column on screen adds up to the footer beneath it. Accumulating unrounded values and
    rounding at the end makes the two disagree by a cent, which costs more trust than the
    cent is worth.

    Currencies are never silently combined: this only ever adds `AwardLine.unit_price`,
    which is by construction already in the award currency because `price_check` returns
    nothing at all when it cannot convert. There is no code path here that touches
    `native_unit_price`.
    """
    totals = AwardTotals(currency=award.currency,
                         rate_provenance=dict(rate_provenance or award.rate_provenance))
    awarded = award.awarded_lines
    totals.awarded_lines = len(awarded)

    by_supplier: "OrderedDict[str, SupplierSubtotal]" = OrderedDict()
    natives: List[str] = []
    for line in sorted(awarded, key=lambda l: (l.supplier_name.casefold(), l.line_item_id)):
        entry = by_supplier.get(line.supplier_id or "")
        if entry is None:
            entry = SupplierSubtotal(supplier_id=line.supplier_id or "",
                                     supplier_name=line.supplier_name,
                                     currency=award.currency,
                                     native_currency=line.native_currency)
            by_supplier[line.supplier_id or ""] = entry
        entry.lines += 1
        if line.native_currency and line.native_currency not in natives:
            natives.append(line.native_currency)
        if line.native_currency and entry.native_currency != line.native_currency:
            # One supplier quoting two currencies makes a native subtotal meaningless.
            entry.native_currency = ""
            entry.native_subtotal = None

        if line.extended is None:
            entry.lines_without_extended += 1
            totals.incomplete_lines.append(line.line_item_id)
            continue
        totals.priced_lines += 1
        entry.subtotal = round((entry.subtotal or 0.0) + line.extended, 2)
        if entry.native_currency:
            native = extended_for(line.quantity, line.native_unit_price)
            if native is not None:
                entry.native_subtotal = round((entry.native_subtotal or 0.0) + native, 2)

    totals.by_supplier = list(by_supplier.values())
    totals.native_currencies = sorted(natives)

    if award.currency and totals.priced_lines:
        totals.grand_total = round(sum(s.subtotal or 0.0 for s in totals.by_supplier), 2)
    totals.complete = bool(awarded) and totals.priced_lines == totals.awarded_lines

    if not award.currency:
        totals.notes.append("No comparison currency could be chosen, so there is no total.")
    if not totals.complete and totals.awarded_lines:
        totals.notes.append(
            "%d of %d awarded lines have no price and are not in this figure: %s."
            % (totals.awarded_lines - totals.priced_lines, totals.awarded_lines,
               ", ".join(totals.incomplete_lines[:6])))
    if len(totals.native_currencies) > 1:
        totals.notes.append(
            "This award covers quotes in %s. Totals are stated in %s at a published "
            "reference rate, not the rate your bank will give you, and each supplier's own "
            "figure is shown beside the converted one. Nothing is summed across currencies."
            % (" and ".join(totals.native_currencies), award.currency))
    return totals


def basket_delta(proposal: AwardProposal) -> Dict[str, Any]:
    """What best value costs against cheapest, and what the difference buys.

    `None` on either side when that proposal cannot cover every line: a total over a
    subset is a different number from a total, and presenting one as the other is how a
    comparison misleads.
    """
    cheapest = proposal.basket(PickSource.CHEAPEST.value)
    best = proposal.basket(PickSource.BEST_VALUE.value)
    out: Dict[str, Any] = {
        "cheapest": cheapest, "best_value": best, "currency": proposal.currency,
        "difference": None, "percent": None, "note": "",
    }
    if cheapest is None or best is None:
        missing = len(proposal.lines_with_no_best_value)
        out["note"] = ("Best value cannot cover every line — %d of %d have no candidate that "
                       "clears the bars — so the two cannot be compared as baskets."
                       % (missing, len(proposal.lines))) if missing else \
                      "Not every line has a comparable price, so the baskets cannot be compared."
        return out
    difference = round(best - cheapest, 2)
    out["difference"] = difference
    out["percent"] = round(100.0 * difference / cheapest, 2) if cheapest else None
    if difference == 0:
        out["note"] = "Best value costs the same as cheapest: the cheapest quote already " \
                      "clears every bar you set."
    else:
        direction = "more" if difference > 0 else "less"
        out["note"] = ("Best value costs %s %s (%s%%) %s than cheapest."
                       % (proposal.currency, "{:,.2f}".format(abs(difference)),
                          "{:,.2f}".format(abs(out["percent"] or 0)), direction))
    return out


def supplier_lead_times(award: Award, ctx) -> Dict[str, str]:
    """The lead time each awarded supplier stated, read once per supplier."""
    out: Dict[str, str] = {}
    for supplier_id in award.supplier_ids:
        bundle = ctx.bundle(supplier_id)
        out[supplier_id] = terms_text(bundle, "lead_time_text") if bundle else ""
    return out
