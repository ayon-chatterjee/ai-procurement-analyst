"""Fixtures for the Phase 3 analyst tests. Test-only: the app never uses these.

These build a real `ComparisonMatrix` out of real `SupplierQuote` objects, running the
same `refresh_derived_values` the app runs, so a test exercises the actual normalisation
and MOQ rules rather than a hand-set flag that could drift from them.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from rfq_copilot.analyst_models import AnalystQuery, Hypothetical, Intent
from rfq_copilot.fx import RateTable
from rfq_copilot.quote_normalizer import refresh_derived_values
from rfq_copilot.schema import RFQ
from rfq_copilot.supplier_models import (
    Certification, ClaimStatus, Evidence, MatchStatus, PriceBasis, QuestionnaireResponse,
    QuoteStatus, ResponseBundle, ResponseType, SourceDocument, Supplier, SupplierQuestion,
    SupplierQuote, SupplierResponse, SupplierStatus, ValueSource,
)
from rfq_copilot.supplier_service import assemble_matrix

#: A fixed table, so a test never depends on a live rate.
def rate_table() -> RateTable:
    return RateTable(base="USD", rates={"USD": 1.0, "EUR": 0.86, "INR": 95.5},
                     source="test-provider", as_of="2026-09-11", fetched_at=0.0)


def quote(rfq: RFQ, line_id: Optional[str], price: Optional[float], *, currency: str = "USD",
          basis: PriceBasis = PriceBasis.PER_UNIT, status: QuoteStatus = QuoteStatus.QUOTED,
          match: MatchStatus = MatchStatus.MATCHED, moq: Optional[float] = None,
          lead: str = "", lead_days: Optional[float] = None, lead_interpreted: bool = False,
          validity: str = "", validity_days: Optional[float] = None,
          validity_conditional: bool = False, payment: str = "", delivery: str = "",
          conflicts: Optional[List[Dict[str, Any]]] = None, issues: Optional[List[str]] = None,
          evidence_ids: Optional[List[str]] = None, label: str = "",
          value_source: ValueSource = ValueSource.SUPPLIER_STATED) -> SupplierQuote:
    """One quote, with the derived values computed by the real rules."""
    line = next((li for li in rfq.line_items if li.id == line_id), None)
    q = SupplierQuote(
        rfq_id=rfq.id, line_item_id=line_id, supplier_line_label=label or (line_id or "line"),
        quoted_quantity=line.quantity if line else None, quoted_unit="pcs",
        unit_price=price, currency=currency, price_basis=basis,
        minimum_order_quantity=moq, moq_unit="pcs",
        lead_time_text=lead, lead_time_days=lead_days, lead_time_is_interpreted=lead_interpreted,
        quote_validity_text=validity, quote_validity_days=validity_days,
        quote_validity_is_conditional=validity_conditional,
        payment_terms=payment, delivery_terms=delivery,
        status=status, match_status=match, conflicts=list(conflicts or []),
        issues=list(issues or []), evidence_ids=list(evidence_ids or []),
        value_source=value_source)
    refresh_derived_values(q, rfq, line)
    q.status = status                 # the fixture decides the status, not the refresh
    return q


def bundle(rfq: RFQ, name: str, quotes: List[SupplierQuote], *, country: str = "China",
           certs: Optional[List[Tuple[str, ClaimStatus]]] = None,
           answers: Optional[List[Tuple[str, str, ClaimStatus]]] = None,
           questions: Optional[List[str]] = None,
           evidence: Optional[Dict[str, Evidence]] = None,
           received_at: str = "2026-09-10T00:00:00Z",
           response_type: ResponseType = ResponseType.QUOTE_RECEIVED
           ) -> Tuple[Supplier, ResponseBundle]:
    supplier = Supplier(name=name, country=country, status=SupplierStatus.RESPONDED)
    response = SupplierResponse(rfq_id=rfq.id, supplier_id=supplier.id, received_at=received_at,
                                response_type=response_type)
    for q in quotes:
        q.response_id = response.id
        q.supplier_id = supplier.id
    return supplier, ResponseBundle(
        response=response, supplier=supplier,
        documents=[SourceDocument(response_id=response.id, filename="%s.txt" % name.split()[0].lower(),
                                  media_type="txt", raw_text="")],
        quotes=list(quotes),
        questionnaire=[QuestionnaireResponse(response_id=response.id, field_key=key,
                                             question_text=key, answer=answer, status=status)
                       for key, answer, status in (answers or [])],
        certifications=[Certification(response_id=response.id, name=cname, raw_name=cname,
                                      status=cstatus)
                        for cname, cstatus in (certs or [])],
        questions=[SupplierQuestion(response_id=response.id, question=q) for q in (questions or [])],
        evidence=dict(evidence or {}))


def silent(name: str = "Pacific Carton Works", country: str = "Philippines") -> Supplier:
    """A supplier who was invited and never replied. Absence is data."""
    return Supplier(name=name, country=country, status=SupplierStatus.NO_RESPONSE)


def evidence_row(eid: str, text: str, location: str = "Page 1", *, verified: bool = True,
                 document: str = "quote.txt") -> Evidence:
    return Evidence(id=eid, document_name=document, location=location, quoted_text=text,
                    verified=verified, source_type="txt")


def matrix(rfq: RFQ, bundles: List[ResponseBundle], *, extra_suppliers: Optional[List[Supplier]] = None,
           display_currency: Optional[str] = None, rates: Optional[RateTable] = None):
    """A real comparison dataset, assembled the way the app assembles it."""
    suppliers = [b.supplier for b in bundles if b.supplier] + list(extra_suppliers or [])
    return assemble_matrix(rfq, suppliers, bundles,
                           rates if rates is not None else rate_table(), display_currency)


def query(intent: Intent, **kw: Any) -> AnalystQuery:
    hyp = kw.pop("hypothetical", None)
    return AnalystQuery(intent=intent.value if isinstance(intent, Intent) else intent,
                        hypothetical=hyp or Hypothetical(), **kw)


def raw_query(intent: str, **over: Any) -> Dict[str, Any]:
    """A schema-valid Stage A payload, for scripting the model in service tests."""
    payload: Dict[str, Any] = {
        "intent": intent, "subject_suppliers": [], "subject_lines": [], "filters": [],
        "hypothetical": {"exclude_suppliers": [], "treat_claimed_as_verified": False,
                         "ignore_moq_constraints": False, "include_probable_matches": False},
        "fields": [], "grain": "supplier", "sort_by": None, "descending": False,
        "group_by": None, "comparison_currency": None, "top_n": None,
        "evidence_target": {"supplier": None, "line": None, "topic": "none", "name": None},
        "refines_previous": False, "reading": "test", "unsupported_reason": None,
    }
    hyp = over.pop("hypothetical", None)
    if hyp:
        payload["hypothetical"].update(hyp)
    target = over.pop("evidence_target", None)
    if target:
        payload["evidence_target"].update(target)
    payload.update(over)
    return payload


def explanation(text: str, caveats: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"explanation": text, "caveats": list(caveats or [])}
