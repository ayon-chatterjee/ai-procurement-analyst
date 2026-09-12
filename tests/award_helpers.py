"""Fixtures for the Phase 5 award tests. Test-only: the app never uses these.

Built on `tests/analyst_helpers.py` so a test exercises the real normalisation, MOQ and
qualification rules rather than hand-set flags that could drift from them. The supplier
shapes below mirror the ones in the live demo — a cleared supplier, one whose validity is
conditional, one whose minimum order is too high — so the awkward cases are first-class
fixtures rather than something only the demo has.
"""
from __future__ import annotations

import datetime as _dt
import os
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from rfq_copilot.analyst_calculations import build_context, choose_comparison_currency
from rfq_copilot.analyst_models import AnalystQuery, Hypothetical, Intent
from rfq_copilot.award_calculations import line_from_candidate, propose_award
from rfq_copilot.award_models import (
    Award, AwardProposal, AwardThresholds, PickSource,
)
from rfq_copilot.config import Settings
from rfq_copilot.persistence import RFQRepository
from rfq_copilot.supplier_models import ClaimStatus, MatchStatus, PriceBasis, QuoteStatus
from rfq_copilot.supplier_service import SupplierService
from tests.analyst_helpers import bundle, matrix, quote, rate_table, silent
from tests.supplier_helpers import ScriptedAI, carton_rfq

#: A fixed date, so an expiry test never depends on when it is run.
TODAY = _dt.date(2026, 9, 12)

#: The answer every fixture supplier gives to the one REQUIRED question on `carton_rfq`,
#: so a test about pricing is not derailed by an unrelated questionnaire warning.
ANSWERED = [("required_delivery_date", "18 days", ClaimStatus.CLAIMED)]


def thresholds(max_lead: Optional[float] = None, require_docs: bool = True,
               require_firm: bool = True) -> AwardThresholds:
    return AwardThresholds(max_lead_time_days=max_lead,
                           require_document_backed_certification=require_docs,
                           require_firm_validity=require_firm)


# --------------------------------------------------------------------------- #
# Supplier shapes taken from the live demo
# --------------------------------------------------------------------------- #
def cleared_supplier(rfq, name: str, prices: Dict[str, float], *, currency: str = "USD",
                     lead: str = "20 days", lead_days: Optional[float] = 20.0,
                     validity_days: Optional[float] = 30.0, **kw):
    """Holds a document-backed certification — the Istanbul shape."""
    return bundle(rfq, name,
                  [quote(rfq, line_id, price, currency=currency, lead=lead,
                         lead_days=lead_days, validity="%g days" % validity_days
                         if validity_days else "", validity_days=validity_days,
                         payment="30% advance", delivery="FOB")
                   for line_id, price in sorted(prices.items())],
                  certs=[("ISO 9001", ClaimStatus.VERIFIED)], answers=ANSWERED, **kw)


def claiming_supplier(rfq, name: str, prices: Dict[str, float], *, currency: str = "USD",
                      lead: str = "21 days", lead_days: Optional[float] = 21.0,
                      validity_days: Optional[float] = 30.0, **kw):
    """Says it is certified but attached nothing — the Anhui shape, and the common case."""
    return bundle(rfq, name,
                  [quote(rfq, line_id, price, currency=currency, lead=lead,
                         lead_days=lead_days, validity="%g days" % validity_days
                         if validity_days else "", validity_days=validity_days,
                         payment="30% advance, 70% against B/L", delivery="FOB Shanghai")
                   for line_id, price in sorted(prices.items())],
                  certs=[("ISO 9001", ClaimStatus.CLAIMED)], answers=ANSWERED, **kw)


def conditional_supplier(rfq, name: str, prices: Dict[str, float], **kw):
    """Quote valid subject to a condition rather than for a period — the Viet Carton shape."""
    return bundle(rfq, name,
                  [quote(rfq, line_id, price, lead="20 days", lead_days=20.0,
                         validity="valid subject to kraft paper prices",
                         validity_conditional=True)
                   for line_id, price in sorted(prices.items())],
                  certs=[("ISO 9001", ClaimStatus.CLAIMED)], answers=ANSWERED, **kw)


def moq_blocked_supplier(rfq, name: str, prices: Dict[str, float], *, moq: float = 9000.0,
                         **kw):
    """Cheapest on paper, unusable at this quantity — the Shenzhen shape."""
    return bundle(rfq, name,
                  [quote(rfq, line_id, price, moq=moq, lead="18 days", lead_days=18.0,
                         validity="21 days", validity_days=21.0)
                   for line_id, price in sorted(prices.items())],
                  certs=[("ISO 9001", ClaimStatus.CLAIMED)], answers=ANSWERED, **kw)


def unresolved_supplier(rfq, name: str, prices: Dict[str, float], **kw):
    """Priced per kilogram, so no per-piece figure exists — the Viet Carton per-kg line."""
    return bundle(rfq, name,
                  [quote(rfq, line_id, price, basis=PriceBasis.PER_KG,
                         status=QuoteStatus.UNRESOLVED)
                   for line_id, price in sorted(prices.items())],
                  certs=[("ISO 9001", ClaimStatus.CLAIMED)], answers=ANSWERED, **kw)


def stress_shaped(sizes: Optional[List[str]] = None):
    """The five-supplier arrangement the live 30-line RFQ has, in miniature.

    One cleared supplier that quoted only some lines, one cheaper claimant, one blocked by
    its minimum order, one conditional, one silent. This is the fixture that makes "no
    best-value candidate on most lines" a tested case rather than a demo surprise.
    """
    rfq = carton_rfq(sizes=sizes or ["10 x 10 x 5", "12 x 10 x 6", "15 x 10 x 8"])
    ids = [li.id for li in rfq.line_items]
    _, cleared = cleared_supplier(rfq, "Istanbul Ambalaj", {ids[0]: 0.50}, currency="EUR",
                                  lead="26 days", lead_days=26.0, validity_days=20.0)
    _, claiming = claiming_supplier(rfq, "Anhui Packaging Co",
                                    {i: 0.42 + 0.05 * n for n, i in enumerate(ids)})
    _, blocked = moq_blocked_supplier(rfq, "Shenzhen Print & Pack",
                                      {i: 0.30 + 0.05 * n for n, i in enumerate(ids)})
    _, conditional = conditional_supplier(rfq, "Viet Carton JSC", {ids[-1]: 0.40})
    return rfq, [cleared, claiming, blocked, conditional], [silent()]


# --------------------------------------------------------------------------- #
# Proposals and awards
# --------------------------------------------------------------------------- #
def context_for(rfq, bundles, *, extra=None, display_currency: Optional[str] = None,
                rates=None, today: Optional[_dt.date] = None):
    m = matrix(rfq, bundles, extra_suppliers=extra or [], display_currency=display_currency,
               rates=rates)
    query = AnalystQuery(intent=Intent.CHEAPEST_BY_LINE.value, hypothetical=Hypothetical())
    currency, reason = choose_comparison_currency(m, query, None)
    return build_context(m, query, currency, reason, today=today or TODAY)


def proposal_for(rfq, bundles, *, extra=None, th: Optional[AwardThresholds] = None,
                 display_currency: Optional[str] = None, currency: Optional[str] = None,
                 rates=None, today: Optional[_dt.date] = None) -> AwardProposal:
    m = matrix(rfq, bundles, extra_suppliers=extra or [], display_currency=display_currency,
               rates=rates)
    return propose_award(m, th or thresholds(), currency=currency, today=today or TODAY)


def award_from(rfq, proposal: AwardProposal, picks: Optional[Dict[str, Any]] = None,
               *, reasons: Optional[Dict[str, str]] = None) -> Award:
    """Build an award from a proposal.

    `picks` maps a line id to "cheapest", "best_value", "none", or a supplier id to
    override with. Anything unnamed takes best value where it exists, else cheapest —
    which is what the screen offers by default.
    """
    picks = picks or {}
    reasons = reasons or {}
    award = Award(rfq_id=rfq.id, rfq_title=rfq.title, currency=proposal.currency,
                  currency_reason=proposal.currency_reason, thresholds=proposal.thresholds,
                  assumptions=list(proposal.assumptions),
                  rate_provenance=dict(proposal.rate_provenance))
    for line in proposal.lines:
        pick = picks.get(line.line_item_id)
        if pick is None:
            candidate = line.best_value or line.cheapest
            source = (PickSource.BEST_VALUE.value if line.best_value
                      else PickSource.CHEAPEST.value) if candidate else PickSource.NONE.value
        elif pick in (PickSource.CHEAPEST.value, PickSource.BEST_VALUE.value):
            candidate, source = line.candidate(pick), pick
        elif pick == PickSource.NONE.value:
            candidate, source = None, PickSource.NONE.value
        else:
            candidate = next((c for c in (line.cheapest, line.best_value)
                              if c and c.supplier_id == pick), None)
            source = PickSource.BUYER_OVERRIDE.value
        award.lines.append(line_from_candidate(
            line, candidate, source, reasons.get(line.line_item_id, "")))
    return award


# --------------------------------------------------------------------------- #
# A real service over a temporary database
# --------------------------------------------------------------------------- #
def award_service(payloads: Optional[List[Any]] = None, *, rfq=None, bundles=None,
                  extra=None):
    """A real `AwardService`, with bundles saved straight to the store.

    Extraction is Phase 2's business and is covered there; saving the bundles keeps these
    tests about the award.
    """
    from rfq_copilot.award_service import AwardService

    tmp = tempfile.mkdtemp(prefix="rfq_p5_")
    settings = Settings()
    settings.db_path = os.path.join(tmp, "t.db")
    repo = RFQRepository(settings.db_path)
    rfq = rfq or carton_rfq(sizes=["10 x 10 x 5", "12 x 10 x 6"])
    repo.save_rfq(rfq)
    ai = ScriptedAI(payloads or [])
    sup = SupplierService(repo, ai, settings)
    for b in (bundles or []):
        if b.supplier is not None:
            sup.store.save_supplier(b.supplier)
        sup.store.save_bundle(b)
    for s in (extra or []):
        sup.store.save_supplier(s)
    # A fixed table, so no test reaches the network for a rate.
    sup.fx._memory["USD"] = rate_table()
    svc = AwardService(ai, repo, settings, sup)
    return svc, rfq, settings, ai


def comm_payload(subject: str, paragraphs: List[str], *, greeting: str = "Dear supplier,",
                 closing: str = "Kind regards", omitted: Optional[List[str]] = None
                 ) -> Dict[str, Any]:
    """A schema-valid communication payload, for scripting the model."""
    return {"subject": subject, "greeting": greeting, "body_paragraphs": list(paragraphs),
            "closing": closing, "omitted": list(omitted or [])}
