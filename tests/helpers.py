"""Shared test helpers: a stub AI service (tests only — never used by the app)."""
from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Callable, Dict, List, Optional

from rfq_copilot.ai_schemas import TURN_OUTPUT_SCHEMA
from rfq_copilot.ai_service import AIResult, AIService, validate_against
from rfq_copilot.config import Settings
from rfq_copilot.persistence import RFQRepository
from rfq_copilot.rfq_service import RFQService

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def load_fixture(name: str) -> Any:
    with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as f:
        return json.load(f)


def base_turn_output(**over: Any) -> Dict[str, Any]:
    """A minimal, schema-valid TurnOutput that tests extend."""
    out: Dict[str, Any] = {
        "classification": {"product": "Corrugated Carton Boxes", "category": "Packaging", "product_type": "Corrugated shipping cartons",
                           "confidence": 0.9, "note": ""},
        "title": "RFQ: Corrugated Carton Boxes",
        "assistant_message": "I can help prepare this RFQ. A few details will drive supplier pricing.",
        "field_updates": [],
        "applicability_updates": [],
        "line_items": {"mode": "none", "items": []},
        "answered_questions": [],
        "new_questions": [],
        "completeness": {"score": 20, "ready_to_send": False, "missing_required_fields": [], "recommended_fields": [],
                         "open_ambiguities": [], "explanation": "Several details are still missing."},
    }
    out.update(over)
    err = validate_against(TURN_OUTPUT_SCHEMA, out)
    assert err is None, "test fixture is not schema-valid: %s" % err
    return out


def fu(key: str, value: str, evidence: Optional[str], section: str = "technical", label: Optional[str] = None,
       value_kind: str = "text", unit: Optional[str] = None, source: str = "buyer_explicit", status: str = "provided",
       revision: str = "none", importance: str = "required", note: Optional[str] = None) -> Dict[str, Any]:
    return {"key": key, "section": section, "label": label or key.replace("_", " ").title(), "value": value,
            "value_kind": value_kind, "unit": unit, "source": source, "status": status, "revision": revision,
            "evidence": evidence, "importance": importance, "note": note}


def nq(question: str, field_key: Optional[str], section: str = "technical", importance: str = "required",
       reason: str = "Directly affects supplier pricing.", answer_type: str = "text", options: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"section": section, "field_key": field_key, "question": question, "reason": reason, "importance": importance,
            "answer_type": answer_type, "suggested_options": options or []}


def line(product: str, dims: str, qty: Optional[float], evidence: Optional[str], unit: str = "pcs", line_id: Optional[str] = None) -> Dict[str, Any]:
    return {"line_id": line_id, "product": product, "description": "", "specifications": [{"name": "Dimensions", "value": dims, "unit": "in"}],
            "quantity": qty, "unit": unit, "target_price": None, "required_date": None, "evidence": evidence}


class StubAIService(AIService):
    """Replays scripted TurnOutputs (or calls a responder). TESTS ONLY."""

    name = "stub"

    def __init__(self, responder: Optional[Callable[[str, Dict[str, Any], str], Dict[str, Any]]] = None,
                 outputs: Optional[List[Any]] = None):
        self.responder = responder
        self.outputs = list(outputs or [])
        self.calls: List[Dict[str, Any]] = []

    def model_for(self, tier: str) -> str:
        return "stub-model"

    def complete_json(self, prompt: str, schema: Dict[str, Any], system_prompt: str, tier: str = "quality") -> AIResult:
        self.calls.append({"prompt": prompt, "schema": schema, "system": system_prompt, "tier": tier})
        if self.responder is not None:
            data = self.responder(prompt, schema, system_prompt)
        else:
            if not self.outputs:
                raise AssertionError("StubAIService has no more scripted outputs")
            data = self.outputs.pop(0)
        if isinstance(data, Exception):
            raise data
        err = validate_against(schema, data)
        assert err is None, "stub output invalid: %s" % err
        return AIResult(data=data, raw=json.dumps({"structured_output": data}), provider=self.name, model="stub-model", duration_ms=1)

    def health(self) -> Dict[str, Any]:
        return {"available": True, "authenticated": True, "detail": "stub"}


def make_service(ai: AIService, tmpdir: Optional[str] = None) -> RFQService:
    tmpdir = tmpdir or tempfile.mkdtemp(prefix="rfq_test_")
    settings = Settings()
    settings.db_path = os.path.join(tmpdir, "test.db")
    repo = RFQRepository(settings.db_path)
    return RFQService(repo, ai, settings)
