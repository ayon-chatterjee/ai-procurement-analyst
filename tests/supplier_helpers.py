"""Shared fixtures for the Phase 2 tests. Test-only: the app never uses these."""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from rfq_copilot.fields import new_field_set
from rfq_copilot.schema import (
    RFQ, Importance, LineItem, Question, RFQStatus, Section, Source, SpecAttr,
)

FIXTURES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "suppliers")

SIZES = ["10 x 10 x 5", "12 x 10 x 6", "15 x 10 x 8", "18 x 12 x 10",
         "20 x 15 x 10", "24 x 18 x 12", "30 x 20 x 15"]


def carton_rfq(quantity: float = 2000.0, sizes: Optional[List[str]] = None) -> RFQ:
    sizes = sizes or SIZES
    rfq = RFQ(id="rfq_test", title="Corrugated Carton Boxes", product="Corrugated Carton Boxes",
              category="Packaging", product_type="Corrugated shipping cartons",
              fields=new_field_set(), status=RFQStatus.SUPPLIER_READY, turn=4)
    for i, s in enumerate(sizes, start=1):
        rfq.line_items.append(LineItem(
            id="LINE-%03d" % i, product="Corrugated carton",
            specifications=[SpecAttr("Dimensions", s, "in")],
            quantity=quantity, unit="pcs", source=Source.BUYER_EXPLICIT, evidence=s))
    rfq.line_seq = len(sizes)
    rfq.questions = [
        Question(id="q_cert", category=Section.QUALITY, question="Which certifications can you provide?",
                 field_key="certifications", importance=Importance.RECOMMENDED),
        Question(id="q_pay", category=Section.COMMERCIAL, question="What payment terms do you offer?",
                 field_key="payment_terms", importance=Importance.RECOMMENDED),
        Question(id="q_lead", category=Section.LOGISTICS, question="What is your production lead time?",
                 field_key="required_delivery_date", importance=Importance.REQUIRED),
    ]
    return rfq


# --------------------------------------------------------------------------- #
def evidence(text: str, location: str = "Page 1") -> Dict[str, Any]:
    return {"quoted_text": text, "location": location}


#: The contract always carries the evidence object; its fields are what may be null.
NO_EVIDENCE: Dict[str, Any] = {"quoted_text": None, "location": None}


def quote_line(label: str, price: Optional[float], size: Optional[str] = None, currency: str = "USD",
               basis: str = "per_unit", qty: Optional[float] = 2000.0, ev: str = "",
               indicative: bool = False, moq: Optional[float] = None,
               lead: Optional[str] = None, confidence: float = 0.95) -> Dict[str, Any]:
    return {
        "supplier_line_label": label, "supplier_line_number": None, "described_size": size,
        "product_description": None, "quoted_quantity": qty, "quoted_unit": "pcs",
        "unit_price": price, "currency": currency, "price_basis": basis,
        "price_is_indicative": indicative, "minimum_order_quantity": moq, "lead_time_text": lead,
        "evidence": evidence(ev or label), "confidence": confidence,
    }


def extraction_payload(**over: Any) -> Dict[str, Any]:
    """A schema-valid SupplierExtraction the stub can return."""
    base: Dict[str, Any] = {
        "supplier": {"stated_name": "Test Supplier", "quote_reference": "Q-1",
                     "quote_date": "2026-09-10", "country_or_place": "China"},
        "response": {"response_type": "quote_received", "is_revision": False,
                     "supersedes_reference": None, "summary": "A test quotation."},
        "quote_lines": [],
        "lines_explicitly_not_quoted": [],
        "commercial_terms": {
            "currency": "USD", "minimum_order_quantity": None, "moq_unit": None, "moq_evidence": NO_EVIDENCE,
            "lead_time_text": None, "lead_time_evidence": NO_EVIDENCE, "payment_terms": None,
            "payment_evidence": NO_EVIDENCE, "delivery_terms": None, "delivery_evidence": NO_EVIDENCE,
            "quote_validity_text": None, "quote_validity_is_conditional": False,
            "quote_validity_evidence": NO_EVIDENCE,
            "discount": {"percent": None, "amount": None, "condition": None, "evidence": NO_EVIDENCE},
        },
        "questionnaire_answers": [],
        "certifications": [],
        "supplier_questions": [],
        "conflicts": [],
        "uncertainties": [],
    }
    for k, v in over.items():
        if k == "commercial_terms" and isinstance(v, dict):
            base["commercial_terms"].update(v)
        else:
            base[k] = v
    return base


def match_payload(matches: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"matches": matches, "notes": []}


def match(label: str, line_id: Optional[str], basis: str = "dimensions",
          confidence: float = 0.95, alts: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"supplier_line_label": label, "rfq_line_item_id": line_id, "basis": basis,
            "confidence": confidence, "reason": "test", "alternative_line_item_ids": alts or []}


# --------------------------------------------------------------------------- #
class ScriptedAI:
    """Returns queued payloads in order, validating each against the schema it is given."""

    name = "scripted"

    def __init__(self, payloads: Optional[List[Any]] = None):
        self.payloads = list(payloads or [])
        self.calls: List[Dict[str, Any]] = []

    def model_for(self, tier: str) -> str:
        return "scripted-model"

    def complete_json(self, prompt, schema, system_prompt, tier="quality"):
        from rfq_copilot.ai_service import AIResult, validate_against
        self.calls.append({"prompt": prompt, "system": system_prompt, "schema": schema})
        if not self.payloads:
            raise AssertionError("ScriptedAI ran out of payloads (call %d)" % len(self.calls))
        data = self.payloads.pop(0)
        if isinstance(data, Exception):
            raise data
        err = validate_against(schema, data)
        assert err is None, "scripted payload is not schema-valid: %s" % err
        return AIResult(data=data, raw="{}", provider=self.name, model="scripted-model", duration_ms=1)

    def health(self):
        return {"available": True, "authenticated": True, "detail": "scripted"}


def make_documents(text: str, filename: str = "quote.txt", media: str = "txt"):
    from rfq_copilot.supplier_models import ExtractionStatus, SourceDocument
    return [SourceDocument(filename=filename, media_type=media, raw_text=text,
                           extraction_status=ExtractionStatus.EXTRACTED)]
