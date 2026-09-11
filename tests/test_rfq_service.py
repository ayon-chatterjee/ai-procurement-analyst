from __future__ import annotations

import json
import unittest

from rfq_copilot.ai_service import AITimeout
from rfq_copilot.rfq_service import RFQStateError
from rfq_copilot.schema import FieldStatus, Importance, MessageRole, QuestionStatus, RFQStatus, Source
from tests.helpers import StubAIService, base_turn_output, fu, line, make_service, nq

SIZES = ["10x10x5", "12x10x6", "15x10x8", "18x12x10", "20x15x10", "24x18x12", "30x20x15"]
SIZE_TEXT = "We need:\n" + "\n".join(SIZES) + "\n2,000 pieces each. Ship to Mumbai."

FIRST = base_turn_output(
    applicability_updates=[{"key": "sample_requirements", "importance": "not_applicable", "reason": "Plain cartons are rarely sampled before a first order."},
                           {"key": "certifications", "importance": "optional", "reason": "Cartons for domestic shipping rarely need certificates."}],
    new_questions=[
        nq("What are the dimensions of each box?", "box_dimensions", reason="Dimensions drive material usage and unit price."),
        nq("How many boxes do you need?", "quantity", section="commercial", reason="Quantity sets price tiers and MOQ feasibility.", answer_type="number"),
        nq("What board grade or strength (e.g. ECT rating) should suppliers quote?", "board_grade", reason="Board grade drives material cost and box strength."),
        nq("Is any printing required?", "printing", importance="recommended", reason="Printing adds plate and ink costs.", answer_type="choice",
           options=["None", "1 colour", "2 colours", "Full colour"]),
        nq("Where should the boxes be delivered?", "destination", section="logistics", reason="Destination sets freight cost and lead time."),
        nq("When do you need them delivered?", "required_delivery_date", section="logistics", importance="recommended",
           reason="Deadline determines whether expedited production is needed.", answer_type="date"),
    ],
)


class ServiceFlowTest(unittest.TestCase):
    def test_first_turn_creates_questions_persists_and_audits(self):
        ai = StubAIService(outputs=[FIRST])
        svc = make_service(ai)
        rfq = svc.start_rfq("I need carton boxes.")
        self.assertEqual(rfq.product, "Corrugated Carton Boxes")
        self.assertEqual(rfq.category, "Packaging")
        self.assertEqual(rfq.status, RFQStatus.IN_PROGRESS)
        ai_keys = {"box_dimensions", "quantity", "board_grade", "printing", "destination", "required_delivery_date"}
        self.assertTrue(ai_keys.issubset({q.field_key for q in rfq.open_questions()}), "all AI questions kept")
        self.assertLessEqual(len(rfq.open_questions()), 8, "questions stay bounded")
        # required universal fields the AI never asked about get a standard question so readiness is reachable
        self.assertIn("customization_type", {q.field_key for q in rfq.open_questions()})
        self.assertEqual(rfq.fields["sample_requirements"].status, FieldStatus.NOT_APPLICABLE)
        self.assertEqual(rfq.fields["certifications"].importance, Importance.OPTIONAL)
        self.assertFalse(rfq.completeness.ready_to_send)
        # nothing was invented
        self.assertFalse(any(fv.is_filled for fv in rfq.fields.values()))
        # persisted + audited
        again = svc.get(rfq.id)
        self.assertEqual(again.to_dict(), rfq.to_dict())
        msgs = svc.transcript(rfq.id)
        self.assertEqual([m.role for m in msgs[:2]], [MessageRole.BUYER, MessageRole.ASSISTANT])
        calls = svc.ai_calls(rfq.id)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].ok and calls[0].schema_valid)
        self.assertEqual(calls[0].call_type, "first_turn")
        self.assertIn("BUYER_REQUEST", ai.calls[0]["prompt"])
        self.assertIn("Never invent", ai.calls[0]["system"])

    def test_seven_line_items_free_text_turn(self):
        ai = StubAIService(outputs=[FIRST])
        svc = make_service(ai)
        rfq = svc.start_rfq("I need carton boxes.")
        qs = {q.field_key: q for q in rfq.open_questions()}

        def responder(prompt, schema, system):
            self.assertIn("FREE_TEXT", prompt)
            self.assertIn(qs["box_dimensions"].id, prompt)  # history is in the prompt
            return base_turn_output(
                assistant_message="Got it — seven sizes, 2,000 each, delivered to Mumbai.",
                line_items={"mode": "replace", "items": [line("Corrugated carton", s, 2000.0, s) for s in SIZES]},
                field_updates=[fu("destination", "Mumbai", "Ship to Mumbai", section="logistics")],
                answered_questions=[
                    {"question_id": qs["box_dimensions"].id, "resolution": "answered", "answer_summary": "Seven sizes listed", "evidence": "We need:"},
                    {"question_id": qs["quantity"].id, "resolution": "answered", "answer_summary": "2,000 each", "evidence": "2,000 pieces each"},
                    {"question_id": qs["destination"].id, "resolution": "answered", "answer_summary": "Mumbai", "evidence": "Ship to Mumbai"},
                ],
                new_questions=[nq("What are the dimensions of each box?", "box_dimensions"),  # re-ask attempt → must be dropped
                               nq("What flute type should suppliers quote?", "flute", reason="Flute drives board cost and stacking strength.")],
                completeness={"score": 80, "ready_to_send": True, "missing_required_fields": [], "recommended_fields": [], "open_ambiguities": [],
                              "explanation": "Ready."},
            )
        ai.responder = responder
        rfq = svc.submit_turn(rfq.id, answers={}, skipped=[], free_text=SIZE_TEXT)
        self.assertEqual(len(rfq.line_items), 7)
        self.assertEqual([li.id for li in rfq.line_items], ["LINE-%03d" % i for i in range(1, 8)])
        self.assertTrue(all(li.quantity == 2000.0 and li.source == Source.BUYER_EXPLICIT for li in rfq.line_items))
        q = rfq.fields["quantity"]
        self.assertEqual(q.value, 14000.0)
        self.assertIn("per line item", q.note)
        self.assertTrue(rfq.fields["destination"].is_filled)
        self.assertEqual(rfq.fields["destination"].evidence, "Ship to Mumbai")
        self.assertTrue(rfq.fields["destination"].source_refs[0].startswith("msg:"))
        open_texts = [x.question for x in rfq.open_questions()]
        self.assertNotIn("What are the dimensions of each box?", open_texts)
        self.assertIn("What flute type should suppliers quote?", open_texts)
        self.assertEqual(qs["quantity"].id and rfq.question(qs["quantity"].id).status, QuestionStatus.ANSWERED)
        # AI said ready, but board grade (required question) is still open → not ready
        self.assertFalse(rfq.completeness.ready_to_send)
        self.assertEqual(rfq.completeness.ai_ready_claim, True)
        self.assertEqual(rfq.status, RFQStatus.IN_PROGRESS)

    def test_answers_survive_ai_failure_and_can_be_retried(self):
        ai = StubAIService(outputs=[FIRST])
        svc = make_service(ai)
        rfq = svc.start_rfq("I need carton boxes.")
        dest_q = [q for q in rfq.open_questions() if q.field_key == "destination"][0]
        skip_q = [q for q in rfq.open_questions() if q.field_key == "printing"][0]
        ai.outputs = [AITimeout("slow")]
        with self.assertRaises(AITimeout):
            svc.submit_turn(rfq.id, answers={dest_q.id: "Mumbai"}, skipped=[skip_q.id], free_text="")
        saved = svc.get(rfq.id)
        self.assertEqual(saved.question(dest_q.id).status, QuestionStatus.ANSWERED)
        self.assertEqual(saved.question(dest_q.id).answer, "Mumbai")
        self.assertEqual(saved.question(skip_q.id).status, QuestionStatus.SKIPPED)
        self.assertTrue(svc.has_pending_turn(saved))
        self.assertEqual(saved.turn, 2)
        calls = svc.ai_calls(rfq.id)
        self.assertFalse(calls[-1].ok)
        self.assertIn("AITimeout", calls[-1].error)
        # retry succeeds and applies
        ai.outputs = [base_turn_output(field_updates=[fu("destination", "Mumbai", "Mumbai", section="logistics")])]
        rfq = svc.retry_last_turn(rfq.id)
        self.assertTrue(rfq.fields["destination"].is_filled)
        self.assertFalse(svc.has_pending_turn(rfq))
        self.assertEqual(rfq.turn, 2)

    def test_invalid_output_is_retried_with_the_validation_error(self):
        from rfq_copilot.ai_service import AIInvalidOutput
        ai = StubAIService(outputs=[AIInvalidOutput("bad json"), FIRST])
        svc = make_service(ai)
        rfq = svc.start_rfq("I need carton boxes.")
        self.assertEqual(len(ai.calls), 2)
        self.assertIn("PREVIOUS ATTEMPT FAILED VALIDATION", ai.calls[1]["prompt"])
        self.assertEqual(len(svc.ai_calls(rfq.id)), 2)

    def test_dropped_connection_is_retried_with_the_same_prompt(self):
        from rfq_copilot.ai_service import AITransient
        ai = StubAIService(outputs=[AITransient("connection lost"), FIRST])
        svc = make_service(ai)
        rfq = svc.start_rfq("I need carton boxes.")
        self.assertEqual(ai.calls[0]["prompt"], ai.calls[1]["prompt"], "a dropped connection retries verbatim")
        self.assertTrue(rfq.is_classified)
        calls = svc.ai_calls(rfq.id)
        self.assertFalse(calls[0].ok)
        self.assertTrue(calls[1].ok)

    def test_manual_edit_is_buyer_fact_closes_question_and_is_audited(self):
        ai = StubAIService(outputs=[FIRST])
        svc = make_service(ai)
        rfq = svc.start_rfq("I need carton boxes.")
        rfq = svc.set_field(rfq.id, "destination", "Pune warehouse")
        fv = rfq.fields["destination"]
        self.assertTrue(fv.is_filled)
        self.assertEqual(fv.source, Source.MANUAL_EDIT)
        self.assertIn("manual", fv.source_refs)
        dest_q = [q for q in rfq.questions if q.field_key == "destination"][0]
        self.assertEqual(dest_q.status, QuestionStatus.ANSWERED)
        self.assertTrue(any(m.kind == "manual_edit" for m in svc.transcript(rfq.id)))
        # overwriting a recommendation keeps history
        svc.repo.save_rfq(rfq)
        rfq = svc.set_field(rfq.id, "destination", "Delhi")
        self.assertEqual(rfq.fields["destination"].history[-1]["value"], "Pune warehouse")

    def test_supplier_ready_gate_and_reopen(self):
        ai = StubAIService(outputs=[FIRST])
        svc = make_service(ai)
        rfq = svc.start_rfq("I need carton boxes.")
        with self.assertRaises(RFQStateError):
            svc.mark_supplier_ready(rfq.id)
        # fill everything required manually
        for q in list(rfq.open_questions()):
            svc.dismiss_question(rfq.id, q.id)
        rfq = svc.get(rfq.id)
        for key in [k for k, fv in rfq.fields.items() if fv.importance == Importance.REQUIRED]:
            rfq = svc.set_field(rfq.id, key, "provided by buyer")
        rfq = svc.get(rfq.id)
        self.assertTrue(rfq.completeness.ready_to_send, rfq.completeness.explanation)
        self.assertEqual(rfq.status, RFQStatus.READY)
        rfq = svc.mark_supplier_ready(rfq.id)
        self.assertEqual(rfq.status, RFQStatus.SUPPLIER_READY)
        with self.assertRaises(RFQStateError):
            svc.set_field(rfq.id, "destination", "x")
        rfq = svc.reopen(rfq.id)
        self.assertNotEqual(rfq.status, RFQStatus.SUPPLIER_READY)

    def test_export_and_listing(self):
        ai = StubAIService(outputs=[FIRST])
        svc = make_service(ai)
        rfq = svc.start_rfq("I need carton boxes.")
        data = json.loads(svc.export_json(rfq.id))
        self.assertEqual(data["rfq"]["id"], rfq.id)
        self.assertGreaterEqual(len(data["transcript"]), 2)
        rows = svc.list_rfqs()
        self.assertEqual(rows[0].id, rfq.id)
        self.assertEqual(rows[0].product, "Corrugated Carton Boxes")

    def test_empty_input_rejected(self):
        svc = make_service(StubAIService(outputs=[]))
        with self.assertRaises(RFQStateError):
            svc.start_rfq("   ")


if __name__ == "__main__":
    unittest.main()
