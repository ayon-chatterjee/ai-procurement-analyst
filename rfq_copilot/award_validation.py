"""What the buyer should know before committing, and what must stop them.

Three severities, and the difference between them is the whole point. **Blocking** means
the application will not execute: something on the award is not a fact we hold — a price
we cannot compare, a supplier who never responded, a quote that lapsed. **Warning** means
a real commercial risk the buyer may accept but must see first, so warnings never block
and are never dropped: they have to be ticked. **Info** is for the record.

Every finding names the field its predicate read, so a buyer can go and look rather than
take the application's word for it.

Pure: no database, no model, no Streamlit.
"""
from __future__ import annotations

import datetime as _dt
from typing import Dict, List, Optional

from .analyst_calculations import first_priced, price_check, required_certifications, terms_text
from .analyst_models import QualificationStatus
from .award_models import (
    Award, AwardProposal, AwardStatus, Finding, PickSource, Severity, ValidationReport,
)
from .supplier_guards import KNOWN_CURRENCIES
from .supplier_models import SupplierStatus
from .supplier_service import review_items

#: Review-queue kinds that stop a price being trusted. Phase 3 already treats these as the
#: "not ready to decide" set; an award is where that judgement finally bites.
BLOCKING_REVIEW_KINDS = {"probable_match", "unmatched", "unresolved_price", "conflict"}

#: A quote inside this window is not yet expired but will be before most orders are placed.
EXPIRING_SOON_DAYS = 7

#: The commercial terms a purchase order needs. A missing one is not fatal — the handoff
#: prints "Not provided" — but the buyer should know before the order goes out.
REQUIRED_TERMS = [
    ("payment_terms", "payment terms"),
    ("delivery_terms", "delivery terms"),
    ("lead_time_text", "lead time"),
    ("quote_validity_text", "quote validity"),
]


def _finding(code: str, severity: Severity, message: str, field_read: str, *,
             line=None, supplier_id: Optional[str] = None, supplier_name: str = "",
             evidence_ids: Optional[List[str]] = None) -> Finding:
    return Finding(code=code, severity=severity.value,
                   scope="line" if line is not None else ("supplier" if supplier_id else "award"),
                   line_item_id=line, supplier_id=supplier_id, supplier_name=supplier_name,
                   message=message, field_read=field_read,
                   evidence_ids=list(evidence_ids or []))


def validate_award(award: Award, ctx, proposal: Optional[AwardProposal] = None,
                   today: Optional[_dt.date] = None) -> ValidationReport:
    """Everything wrong, everything risky, and everything worth noting."""
    today = today or _dt.date.today()
    report = ValidationReport()
    add = report.findings.append

    seen_lines: Dict[str, int] = {}
    for line in award.lines:
        seen_lines[line.line_item_id] = seen_lines.get(line.line_item_id, 0) + 1
    for line_id, count in sorted(seen_lines.items()):
        if count > 1:
            add(_finding("duplicate_allocation", Severity.BLOCKING,
                         "%s is awarded %d times. A line can only be awarded once."
                         % (line_id, count), "AwardLine.line_item_id", line=line_id))

    rfq_lines = {li.id: li for li in ctx.rfq.line_items}
    required_certs = required_certifications(ctx.rfq)

    for line in award.lines:
        if not line.awarded:
            add(_finding("line_not_awarded", Severity.INFO,
                         "%s is not being awarded to anyone." % line.line_item_id,
                         "AwardLine.pick_source", line=line.line_item_id))
            continue

        name = line.supplier_name or line.supplier_id or ""
        supplier = next((s for s in ctx.matrix.suppliers if s.id == line.supplier_id), None)
        bundle = ctx.bundle(line.supplier_id or "")

        if supplier is None or bundle is None:
            add(_finding("excluded_supplier", Severity.BLOCKING,
                         "%s has no active response on this RFQ, so %s cannot be awarded to "
                         "them." % (name or "That supplier", line.line_item_id),
                         "ComparisonMatrix.bundles", line=line.line_item_id,
                         supplier_id=line.supplier_id, supplier_name=name))
            continue
        if supplier.status == SupplierStatus.NO_RESPONSE:
            add(_finding("excluded_supplier", Severity.BLOCKING,
                         "%s never replied to this RFQ." % name, "Supplier.status",
                         line=line.line_item_id, supplier_id=line.supplier_id,
                         supplier_name=name))
            continue

        rfq_line = rfq_lines.get(line.line_item_id)
        if rfq_line is None:
            add(_finding("unknown_line", Severity.BLOCKING,
                         "%s is not a line on this RFQ." % line.line_item_id,
                         "RFQ.line_items", line=line.line_item_id))
            continue

        if not line.quantity:
            add(_finding("no_quantity", Severity.BLOCKING,
                         "%s has no quantity, so there is nothing to order."
                         % line.line_item_id, "LineItem.quantity", line=line.line_item_id,
                         supplier_id=line.supplier_id, supplier_name=name))
        elif rfq_line.quantity and line.quantity > rfq_line.quantity:
            add(_finding("award_exceeds_rfq_quantity", Severity.BLOCKING,
                         "%s awards %s units but the RFQ asks for %s."
                         % (line.line_item_id, "{:,.0f}".format(line.quantity),
                            "{:,.0f}".format(rfq_line.quantity)),
                         "AwardLine.quantity", line=line.line_item_id,
                         supplier_id=line.supplier_id, supplier_name=name))

        if line.unit_price is None:
            add(_finding("no_price", Severity.BLOCKING,
                         "%s has no price we can commit to." % line.line_item_id,
                         "AwardLine.unit_price", line=line.line_item_id,
                         supplier_id=line.supplier_id, supplier_name=name,
                         evidence_ids=line.evidence_ids))
        if line.native_currency and line.native_currency not in KNOWN_CURRENCIES:
            add(_finding("unknown_currency", Severity.BLOCKING,
                         "%s quoted %s in '%s', which is not a currency we can read."
                         % (name, line.line_item_id, line.native_currency),
                         "SupplierQuote.currency", line=line.line_item_id,
                         supplier_id=line.supplier_id, supplier_name=name,
                         evidence_ids=line.evidence_ids))

        # The decision was taken against a quote. Re-run the price rule now: a revision or
        # a correction since then can make the figure one the app would no longer compare.
        cell = ctx.cell(line.line_item_id, line.supplier_id or "")
        check = price_check(cell, ctx)
        if check.exclusion is not None and check.exclusion.code == "moq_constraint":
            add(_finding("moq_violation", Severity.BLOCKING,
                         "%s: %s" % (name, check.exclusion.reason),
                         "SupplierQuote.moq_constraint", line=line.line_item_id,
                         supplier_id=line.supplier_id, supplier_name=name,
                         evidence_ids=check.exclusion.evidence_ids))
        elif not check.comparable and check.exclusion is not None:
            add(_finding("unresolved_basis", Severity.BLOCKING,
                         "%s's price for %s can no longer be compared: %s"
                         % (name, line.line_item_id, check.exclusion.reason),
                         "PriceCheck.exclusion", line=line.line_item_id,
                         supplier_id=line.supplier_id, supplier_name=name,
                         evidence_ids=check.exclusion.evidence_ids))
        elif check.amount is not None and line.unit_price is not None \
                and abs(check.amount - line.unit_price) > 0.0001:
            add(_finding("stale_decision", Severity.WARNING,
                         "%s's price for %s is now %s; the award was taken at %s."
                         % (name, line.line_item_id, check.amount, line.unit_price),
                         "SupplierQuote.normalized_unit_price", line=line.line_item_id,
                         supplier_id=line.supplier_id, supplier_name=name))

        if cell.quote is not None and line.quote_id and cell.quote.id != line.quote_id:
            add(_finding("stale_decision", Severity.WARNING,
                         "%s has sent a revision since %s was awarded. Re-check the price."
                         % (name, line.line_item_id), "SupplierQuote.id",
                         line=line.line_item_id, supplier_id=line.supplier_id,
                         supplier_name=name))

        if line.is_override and line.seeded_cheapest:
            seeded = line.seeded_cheapest.get("amount")
            if seeded is not None and line.unit_price is not None and line.unit_price > seeded:
                add(_finding("override_costs_more", Severity.INFO,
                             "%s costs %s more per piece than the cheapest quote on %s. "
                             "Your reason: %s"
                             % (name, round(line.unit_price - seeded, 4), line.line_item_id,
                                line.override_reason or "not given"),
                             "AwardLine.pick_source", line=line.line_item_id,
                             supplier_id=line.supplier_id, supplier_name=name))

    # -- supplier-level, said once each ------------------------------------
    for supplier_id in award.supplier_ids:
        bundle = ctx.bundle(supplier_id)
        if bundle is None:
            continue
        name = ctx.name(supplier_id)
        _validate_supplier(report, ctx, award, supplier_id, name, bundle, required_certs, today)

    # -- award-level -------------------------------------------------------
    natives = sorted({l.native_currency for l in award.awarded_lines if l.native_currency})
    if len(natives) > 1:
        add(_finding("mixed_currencies", Severity.WARNING,
                     "This award covers quotes in %s. Totals are converted at a published "
                     "reference rate, not the rate your bank will give you."
                     % " and ".join(natives), "AwardLine.native_currency"))
        rates = ctx.matrix.rates
        if rates is not None and not rates.ok:
            add(_finding("rate_unavailable", Severity.BLOCKING,
                         "Exchange rates are unavailable (%s), so quotes in more than one "
                         "currency cannot be totalled." % (rates.error or "no rate"),
                         "RateTable.ok"))
    if not award.currency and award.awarded_lines:
        add(_finding("rate_unavailable", Severity.BLOCKING,
                     "No comparison currency could be chosen, so this award has no total.",
                     "Award.currency"))
    if not award.awarded_lines:
        add(_finding("nothing_awarded", Severity.BLOCKING,
                     "No line is awarded to anyone yet.", "Award.lines"))
    return report


def _validate_supplier(report: ValidationReport, ctx, award: Award, supplier_id: str,
                       name: str, bundle, required_certs: List[str],
                       today: _dt.date) -> None:
    """Everything true of a whole response rather than one line.

    Read once per supplier: Phase 2 copies these terms onto every quote row, so asking per
    line would raise the same finding thirty times.
    """
    add = report.findings.append
    quote = first_priced(bundle)
    lines = [l.line_item_id for l in award.lines_for(supplier_id)]

    # -- quote validity ----------------------------------------------------
    if quote is not None:
        if quote.quote_validity_is_conditional:
            add(_finding("conditional_validity", Severity.WARNING,
                         "%s's quote is valid subject to a condition, not for a fixed "
                         "period: %s" % (name, (quote.quote_validity_text or "")[:160]),
                         "SupplierQuote.quote_validity_is_conditional",
                         supplier_id=supplier_id, supplier_name=name))
        elif quote.quote_validity_days is not None:
            expiry = _expiry(bundle, quote.quote_validity_days)
            if expiry is not None:
                days_left = (expiry - today).days
                if days_left < 0:
                    add(_finding("expired_validity", Severity.BLOCKING,
                                 "%s's quote lapsed on %s, %d days ago. Ask them to "
                                 "reconfirm before awarding."
                                 % (name, expiry.isoformat(), abs(days_left)),
                                 "SupplierQuote.quote_validity_days",
                                 supplier_id=supplier_id, supplier_name=name))
                elif days_left <= EXPIRING_SOON_DAYS:
                    add(_finding("expiring_validity", Severity.WARNING,
                                 "%s's quote expires on %s, in %d days."
                                 % (name, expiry.isoformat(), days_left),
                                 "SupplierQuote.quote_validity_days",
                                 supplier_id=supplier_id, supplier_name=name))
        else:
            add(_finding("missing_commercial_terms", Severity.WARNING,
                         "%s did not state how long their quote stands." % name,
                         "SupplierQuote.quote_validity_text",
                         supplier_id=supplier_id, supplier_name=name))

    # -- qualification -----------------------------------------------------
    qualification = ctx.qualifications.get(supplier_id)
    if qualification is not None and qualification.status != QualificationStatus.CLEARED.value:
        required_failed = [c for c in qualification.checks if c.required and not c.passed]
        severity = Severity.BLOCKING if (required_certs and required_failed) else Severity.WARNING
        add(_finding("missing_certification", severity,
                     "%s is %s: %s." % (name, qualification.status.replace("_", " "),
                                        "; ".join(qualification.reasons) or "no reason recorded"),
                     "Qualification.status", supplier_id=supplier_id, supplier_name=name,
                     evidence_ids=qualification.failing_evidence_ids()))
        for check in qualification.checks:
            if check.kind == "questionnaire" and check.required and not check.passed:
                add(_finding("missing_questionnaire_answer", Severity.WARNING,
                             "%s did not answer '%s', which this RFQ required."
                             % (name, check.name), "QuestionnaireResponse.status",
                             supplier_id=supplier_id, supplier_name=name))

    # -- commercial terms --------------------------------------------------
    for attr, label in REQUIRED_TERMS:
        if attr == "quote_validity_text":
            continue                      # already covered above, with its expiry
        if not terms_text(bundle, attr):
            add(_finding("missing_commercial_terms", Severity.WARNING,
                         "%s did not state %s. The order will say \"Not provided\"."
                         % (name, label), "SupplierQuote.%s" % attr,
                         supplier_id=supplier_id, supplier_name=name))

    # -- open questions and conflicts on the awarded lines -----------------
    for item in review_items([bundle]):
        kind = str(item.get("kind") or "")
        line_id = item.get("line_item_id")
        if line_id and line_id not in lines:
            continue
        if kind in BLOCKING_REVIEW_KINDS:
            # Only an issue on a line we are actually awarding can block: a quote this
            # supplier sent that we could not place is worth seeing, but it is not part of
            # the commitment and must not stop the lines that did match cleanly.
            on_awarded_line = bool(line_id) and line_id in lines
            # A contradiction about a term other than price does not change the number
            # being committed to, so it warns rather than blocks.
            topic = (item.get("label") or "").lower()
            price_topic = "price" in topic or "cost" in topic
            blocking = on_awarded_line and (kind != "conflict" or price_topic)
            code = "unresolved_critical_conflict" if kind == "conflict" else kind
            detail = (item.get("detail") or "")[:160]
            if not on_awarded_line:
                detail += " This quote is not part of the award."
            add(_finding(code, Severity.BLOCKING if blocking else Severity.WARNING,
                         "%s: %s — %s" % (name, item.get("label") or kind.replace("_", " "),
                                          detail),
                         "review_items.kind", line=line_id if on_awarded_line else None,
                         supplier_id=supplier_id, supplier_name=name))
        elif kind == "supplier_question":
            add(_finding("supplier_question_open", Severity.WARNING,
                         "%s is waiting on an answer from you: %s"
                         % (name, (item.get("label") or "")[:160]),
                         "SupplierQuestion.resolved", supplier_id=supplier_id,
                         supplier_name=name))


def _expiry(bundle, days: float) -> Optional[_dt.date]:
    """When a quote lapses, counted from the day the response arrived.

    No supplier stated a start date, so the received date is the only honest anchor — and
    the assumption is stated wherever an expiry is shown.
    """
    raw = (bundle.response.received_at or "")[:10]
    try:
        return _dt.datetime.strptime(raw, "%Y-%m-%d").date() + _dt.timedelta(days=int(days))
    except (ValueError, TypeError):
        return None


def execution_blockers(report: ValidationReport) -> List[str]:
    """The reasons execution is refused, for a button's tooltip."""
    return [f.message for f in report.blocking]


def validation_assumptions() -> List[str]:
    return [
        "A quote's expiry is counted from the date the response was received, because no "
        "supplier stated a start date.",
        "A warning never blocks execution, but it has to be acknowledged: a caveat you "
        "were never shown cannot become an excuse later.",
    ]
