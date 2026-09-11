from __future__ import annotations

import unittest

from rfq_copilot.fields import FIELD_SPECS, UNIVERSAL_FIELDS, new_field_set
from rfq_copilot.schema import (
    RFQ, AnswerType, Completeness, FieldStatus, FieldValue, Importance, LineItem, Question, QuestionStatus,
    RFQStatus, Section, Source, SpecAttr, ValueKind, rfq_from_json, rfq_to_json,
)


def sample_rfq() -> RFQ:
    rfq = RFQ(id="rfq_test", title="Cartons", product="Corrugated Carton Boxes", category="Packaging", product_type="Shipping cartons",
              fields=new_field_set(), status=RFQStatus.IN_PROGRESS, turn=2)
    q = rfq.fields["quantity"]
    q.value, q.unit, q.status, q.source, q.evidence, q.source_refs = 14000.0, "pcs", FieldStatus.PROVIDED, Source.BUYER_EXPLICIT, "2,000 each", ["msg:1"]
    rfq.fields["flute"] = FieldValue(key="flute", label="Flute", section=Section.TECHNICAL, status=FieldStatus.UNKNOWN, source=Source.BUYER_EXPLICIT,
                                     evidence="I don't know the flute", importance=Importance.REQUIRED)
    for i in range(7):
        rfq.line_items.append(LineItem(id="LINE-%03d" % (i + 1), product="Carton", specifications=[SpecAttr("Dimensions", "10 x 10 x %d" % (5 + i), "in")],
                                       quantity=2000.0, unit="pcs", source_refs=["msg:1"], evidence="10x10x5"))
    rfq.questions.append(Question(id="q_1", category=Section.TECHNICAL, question="Printing?", reason="Print colours drive plate cost.",
                                  importance=Importance.RECOMMENDED, field_key="printing", answer_type=AnswerType.CHOICE,
                                  suggested_options=["None", "1 colour", "Full colour"], asked_turn=1))
    rfq.completeness = Completeness(score=72, ready_to_send=False, missing_required_fields=["Destination"], explanation="Not ready.")
    return rfq


class SchemaRoundTripTest(unittest.TestCase):
    def test_json_round_trip_preserves_everything(self):
        rfq = sample_rfq()
        back = rfq_from_json(rfq_to_json(rfq))
        self.assertEqual(back.to_dict(), rfq.to_dict())
        self.assertIsInstance(back.fields["quantity"].status, FieldStatus)
        self.assertEqual(back.fields["flute"].status, FieldStatus.UNKNOWN)
        self.assertEqual(len(back.line_items), 7)
        self.assertEqual(back.line_items[6].id, "LINE-007")
        self.assertEqual(back.questions[0].answer_type, AnswerType.CHOICE)
        self.assertEqual(back.status, RFQStatus.IN_PROGRESS)

    def test_from_dict_tolerates_unknown_and_missing_keys(self):
        d = sample_rfq().to_dict()
        d["future_field"] = {"x": 1}
        d["fields"]["quantity"]["mystery"] = True
        del d["completeness"]
        del d["fields"]["quantity"]["history"]
        rfq = RFQ.from_dict(d)
        self.assertEqual(rfq.completeness.score, 0)
        self.assertEqual(rfq.fields["quantity"].history, [])
        self.assertEqual(rfq.fields["quantity"].value, 14000.0)

    def test_bad_enum_values_fall_back_to_defaults(self):
        d = sample_rfq().to_dict()
        d["status"] = "bogus"
        d["fields"]["quantity"]["status"] = "weird"
        rfq = RFQ.from_dict(d)
        self.assertEqual(rfq.status, RFQStatus.DRAFT)
        self.assertEqual(rfq.fields["quantity"].status, FieldStatus.MISSING)


class FieldValueTest(unittest.TestCase):
    def test_display_value_formats_numbers_units_lists_bools(self):
        fv = FieldValue(key="quantity", label="Qty", section=Section.COMMERCIAL, value=14000.0, unit="pcs", value_kind=ValueKind.NUMBER)
        self.assertEqual(fv.display_value(), "14,000 pcs")
        fv2 = FieldValue(key="certs", label="Certs", section=Section.QUALITY, value=["FSC", "ISO 9001"], value_kind=ValueKind.LIST)
        self.assertEqual(fv2.display_value(), "FSC, ISO 9001")
        fv3 = FieldValue(key="x", label="x", section=Section.QUALITY, value=True, value_kind=ValueKind.BOOLEAN)
        self.assertEqual(fv3.display_value(), "Yes")

    def test_status_semantics(self):
        fv = FieldValue(key="k", label="K", section=Section.TECHNICAL)
        self.assertFalse(fv.is_filled)
        self.assertEqual(fv.display_status(), "Missing")
        fv.status, fv.source, fv.value = FieldStatus.RECOMMENDED, Source.AI_RECOMMENDED, "B flute"
        self.assertFalse(fv.is_filled, "a recommendation is never a filled buyer fact")
        self.assertEqual(fv.display_status(), "AI recommended")
        fv.status, fv.source = FieldStatus.PROVIDED, Source.MANUAL_EDIT
        self.assertTrue(fv.is_filled)
        self.assertEqual(fv.display_status(), "Buyer edited")
        fv.status = FieldStatus.CONFLICT
        self.assertEqual(fv.display_status(), "Conflict")
        fv.importance = Importance.NOT_APPLICABLE
        self.assertFalse(fv.counts_for_score)


class RFQHelpersTest(unittest.TestCase):
    def test_sections_and_ids(self):
        rfq = sample_rfq()
        self.assertIn("quantity", rfq.commercial_requirements)
        self.assertIn("flute", rfq.technical_requirements)
        self.assertNotIn("flute", rfq.commercial_requirements)
        self.assertEqual(rfq.next_line_item_id(), "LINE-008")
        self.assertEqual(len(rfq.open_questions()), 1)
        rfq.questions[0].status = QuestionStatus.ANSWERED
        self.assertEqual(len(rfq.open_questions()), 0)
        self.assertTrue(rfq.is_classified)


class FieldRegistryTest(unittest.TestCase):
    def test_registry_integrity(self):
        keys = [f.key for f in UNIVERSAL_FIELDS]
        self.assertEqual(len(keys), len(set(keys)))
        for spec in UNIVERSAL_FIELDS:
            self.assertIsInstance(spec.section, Section)
            self.assertIsInstance(spec.default_importance, Importance)
        for required in ("quantity", "destination", "customization_type", "currency", "certifications", "required_delivery_date",
                         "target_landed_cost", "sourcing_country", "shipping_method", "technical_summary", "additional_supplier_instructions"):
            self.assertIn(required, FIELD_SPECS)

    def test_new_field_set_all_missing(self):
        fs = new_field_set()
        self.assertEqual(len(fs), len(UNIVERSAL_FIELDS))
        self.assertTrue(all(v.status == FieldStatus.MISSING and v.source == Source.MISSING for v in fs.values()))


class TrustLabelTest(unittest.TestCase):
    """The UI labels are the user-facing half of the trust model: an AI recommendation
    must never be presented as something the buyer stated."""

    def _fv(self, status, source, value="B flute", **kw):
        return FieldValue(key="flute", label="Flute", section=Section.TECHNICAL, value=value, status=status, source=source, **kw)

    def test_provenance_badge_never_conflates_ai_with_buyer(self):
        from ui.components import field_value_text, provenance_badge
        cases = {
            (FieldStatus.PROVIDED, Source.BUYER_EXPLICIT): "Buyer stated",
            (FieldStatus.PROVIDED, Source.MANUAL_EDIT): "Buyer edited",
            (FieldStatus.RECOMMENDED, Source.AI_RECOMMENDED): "AI recommendation",
            (FieldStatus.UNKNOWN, Source.BUYER_EXPLICIT): "Buyer unsure",
            (FieldStatus.NOT_APPLICABLE, Source.MISSING): "Not applicable",
            (FieldStatus.CONFLICT, Source.BUYER_EXPLICIT): "Conflict",
            (FieldStatus.MISSING, Source.MISSING): "Missing",
        }
        for (status, source), expected in cases.items():
            badge = provenance_badge(self._fv(status, source))
            self.assertIn(expected, badge, "%s/%s must read %r" % (status.value, source.value, expected))
            if source == Source.AI_RECOMMENDED:
                self.assertNotIn("Buyer", badge, "a recommendation must not be labelled as buyer input")

    def test_recommended_value_is_shown_as_a_recommendation(self):
        from ui.components import field_value_text
        fv = self._fv(FieldStatus.RECOMMENDED, Source.AI_RECOMMENDED, note="Common for shipping cartons.")
        text = field_value_text(fv)
        self.assertTrue(text.startswith("Recommended:"), text)
        self.assertIn("B flute", text)
        # and a buyer fact is shown plainly, with no hedging prefix
        self.assertEqual(field_value_text(self._fv(FieldStatus.PROVIDED, Source.BUYER_EXPLICIT)), "B flute")

    def test_unknown_and_missing_read_differently(self):
        from ui.components import field_value_text
        self.assertEqual(field_value_text(self._fv(FieldStatus.MISSING, Source.MISSING, value=None)), "Missing")
        self.assertIn("doesn't know", field_value_text(self._fv(FieldStatus.UNKNOWN, Source.BUYER_EXPLICIT, value=None)))

    def test_conflict_shows_both_values(self):
        from ui.components import field_value_text
        fv = self._fv(FieldStatus.CONFLICT, Source.BUYER_EXPLICIT, value=None)
        fv.conflict_values = [{"value": 30000.0, "unit": "pcs"}, {"value": 50000.0, "unit": "pcs"}]
        self.assertEqual(field_value_text(fv), "30,000 pcs vs 50,000 pcs")


if __name__ == "__main__":
    unittest.main()


class AnswerCollectionTest(unittest.TestCase):
    """The glue between the question-card widgets and the service. A silent break here
    would drop a buyer's typed answers, so it is tested without a browser."""

    def _questions(self):
        return [
            Question(id="q_a", category=Section.TECHNICAL, question="Flute?", field_key="flute",
                     answer_type=AnswerType.CHOICE, suggested_options=["B", "C", "BC"]),
            Question(id="q_b", category=Section.COMMERCIAL, question="Quantity?", field_key="quantity", answer_type=AnswerType.NUMBER),
            Question(id="q_c", category=Section.LOGISTICS, question="Destination?", field_key="destination"),
            Question(id="q_d", category=Section.QUALITY, question="Certificates?", field_key="certifications"),
        ]

    def _collect(self, state):
        from ui import components

        class _Stub(object):
            session_state = state
        original = components.st
        components.st = _Stub()
        try:
            return components.collect_answers(self._questions(), turn=3)
        finally:
            components.st = original

    def test_typed_pill_and_skip_inputs_are_all_collected(self):
        out = self._collect({
            "opt_q_a_3": "BC",                        # chose a suggested option
            "ans_q_b_3": " 2,000 ",                   # typed, needs trimming
            "skip_q_c_3": True,                       # skipped
            "ans_q_d_3": "",                          # untouched
        })
        self.assertEqual(out["answers"], {"q_a": "BC", "q_b": "2,000"})
        self.assertEqual(out["skipped"], ["q_c"])

    def test_typed_text_wins_over_a_selected_pill(self):
        out = self._collect({"opt_q_a_3": "B", "ans_q_a_3": "E flute, single wall"})
        self.assertEqual(out["answers"], {"q_a": "E flute, single wall"})

    def test_an_answer_beats_a_stray_skip_tick(self):
        out = self._collect({"ans_q_c_3": "Mumbai", "skip_q_c_3": True})
        self.assertEqual(out["answers"], {"q_c": "Mumbai"})
        self.assertEqual(out["skipped"], [])

    def test_widget_keys_are_scoped_per_turn_so_answers_never_leak(self):
        from ui.components import question_widget_keys
        q = self._questions()[0]
        self.assertNotEqual(question_widget_keys(q, 3), question_widget_keys(q, 4))
        for key in question_widget_keys(q, 3).values():
            self.assertIn("q_a", key)
            self.assertTrue(key.endswith("_3"))

    def test_nothing_selected_yields_nothing(self):
        self.assertEqual(self._collect({}), {"answers": {}, "skipped": []})
