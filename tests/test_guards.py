from __future__ import annotations

import unittest

from rfq_copilot import guards
from rfq_copilot.config import Settings
from rfq_copilot.fields import new_field_set
from rfq_copilot.schema import (
    RFQ, FieldStatus, FieldValue, Importance, LineItem, Question, QuestionStatus, RFQStatus, Section, Source, SpecAttr, ValueKind,
)
from tests.helpers import fu, line, nq


def rfq_with(product="Corrugated Carton Boxes", category="Packaging") -> RFQ:
    return RFQ(id="rfq_g", product=product, category=category, product_type="cartons", fields=new_field_set(), turn=1)


class EvidenceTest(unittest.TestCase):
    def test_exact_and_normalised_matches(self):
        text = "We need 10 × 10 × 5 inch boxes, 2,000 pieces each. Ship to Mumbai."
        self.assertTrue(guards.evidence_supported("Ship to Mumbai", text))
        self.assertTrue(guards.evidence_supported("10x10x5 inch", text))
        self.assertTrue(guards.evidence_supported("2,000 pieces each", text))
        self.assertFalse(guards.evidence_supported("B flute", text))
        self.assertFalse(guards.evidence_supported("", text))
        self.assertFalse(guards.evidence_supported(None, text))

    def test_prior_turn_lookup(self):
        ref = guards.find_evidence_ref("30,000 boxes", "now Mumbai please", "msg:2", [("msg:1", "I need 30,000 boxes")])
        self.assertEqual(ref, "msg:1")
        self.assertIsNone(guards.find_evidence_ref("blue print", "now Mumbai please", "msg:2", [("msg:1", "I need 30,000 boxes")]))


class FieldUpdateGuardTest(unittest.TestCase):
    def test_buyer_explicit_without_evidence_is_downgraded_to_recommendation(self):
        rfq = rfq_with()
        audit = guards.apply_field_updates(rfq, [fu("board_grade", "32 ECT", "32 ECT", value_kind="text")], "I need carton boxes.", "msg:1", 1)
        fv = rfq.fields["board_grade"]
        self.assertEqual(fv.status, FieldStatus.RECOMMENDED)
        self.assertEqual(fv.source, Source.AI_RECOMMENDED)
        self.assertFalse(fv.is_filled)
        self.assertTrue(any("downgraded" in a for a in audit))

    def test_verified_buyer_fact_is_recorded_with_provenance(self):
        rfq = rfq_with()
        guards.apply_field_updates(rfq, [fu("destination", "Mumbai", "ship to Mumbai", section="logistics")], "Please ship to Mumbai by June.", "msg:9", 2)
        fv = rfq.fields["destination"]
        self.assertTrue(fv.is_filled)
        self.assertEqual(fv.value, "Mumbai")
        self.assertEqual(fv.source, Source.BUYER_EXPLICIT)
        self.assertEqual(fv.source_refs, ["msg:9"])
        self.assertEqual(fv.evidence, "ship to Mumbai")
        self.assertEqual(fv.updated_turn, 2)

    def test_number_parsing(self):
        rfq = rfq_with()
        guards.apply_field_updates(rfq, [fu("quantity", "30,000", "30,000 boxes", section="commercial", value_kind="number", unit="pcs")],
                                   "I need 30,000 boxes", "msg:1", 1)
        self.assertEqual(rfq.fields["quantity"].value, 30000.0)
        self.assertEqual(rfq.fields["quantity"].display_value(), "30,000 pcs")

    def test_registry_type_wins_when_the_model_sends_a_number_as_text(self):
        rfq = rfq_with()
        # the model labels a numeric universal field as text and repeats the unit in the value
        guards.apply_field_updates(rfq, [fu("quantity", "8,000 units", "make that 8,000 pieces", section="commercial",
                                            value_kind="text", unit="units")], "Actually, make that 8,000 pieces.", "msg:2", 2)
        q = rfq.fields["quantity"]
        self.assertEqual(q.value, 8000.0)
        self.assertEqual(q.value_kind, ValueKind.NUMBER)
        self.assertEqual(q.display_value(), "8,000 units", "the unit is printed once")

    def test_trailing_unit_is_not_duplicated_for_text_fields(self):
        self.assertEqual(guards._strip_trailing_unit("8,000 units", "units"), "8,000")
        self.assertEqual(guards._strip_trailing_unit("32 ECT", "ECT"), "32")
        self.assertEqual(guards._strip_trailing_unit("units", "units"), "units", "never strip to nothing")
        self.assertEqual(guards._strip_trailing_unit("BC double wall", "mm"), "BC double wall")

    def test_recommendation_cannot_overwrite_buyer_fact(self):
        rfq = rfq_with()
        guards.apply_field_updates(rfq, [fu("quantity", "30000", "30,000 boxes", section="commercial", value_kind="number")], "I need 30,000 boxes", "msg:1", 1)
        audit = guards.apply_field_updates(rfq, [fu("quantity", "50000", None, section="commercial", value_kind="number",
                                                   source="ai_recommended", status="recommended")], "anything", "msg:2", 2)
        self.assertEqual(rfq.fields["quantity"].value, 30000.0)
        self.assertEqual(rfq.fields["quantity"].source, Source.BUYER_EXPLICIT)
        self.assertTrue(any("skipped" in a for a in audit))

    def test_explicit_correction_replaces_and_keeps_history(self):
        rfq = rfq_with()
        guards.apply_field_updates(rfq, [fu("quantity", "30000", "30,000 boxes", section="commercial", value_kind="number")], "I need 30,000 boxes", "msg:1", 1)
        guards.apply_field_updates(rfq, [fu("quantity", "50000", "make that 50,000", section="commercial", value_kind="number", revision="correction")],
                                   "Actually, make that 50,000.", "msg:2", 2)
        fv = rfq.fields["quantity"]
        self.assertEqual(fv.value, 50000.0)
        self.assertEqual(fv.status, FieldStatus.PROVIDED)
        self.assertEqual(len(fv.history), 1)
        self.assertEqual(fv.history[0]["value"], 30000.0)
        self.assertEqual(fv.history[0]["reason"], "buyer correction")

    def test_contradiction_without_correction_becomes_conflict_not_a_guess(self):
        rfq = rfq_with()
        guards.apply_field_updates(rfq, [fu("box_dimensions", "12 x 10 x 6 in", "12 x 10 x 6 inches", label="Box dimensions")], "Boxes are 12 x 10 x 6 inches", "msg:1", 1)
        guards.apply_field_updates(rfq, [fu("box_dimensions", "12 x 10 x 8 in", "12 x 10 x 8 inches", label="Box dimensions")], "They are 12 x 10 x 8 inches", "msg:2", 2)
        fv = rfq.fields["box_dimensions"]
        self.assertEqual(fv.status, FieldStatus.CONFLICT)
        self.assertEqual(len(fv.conflict_values), 2)
        self.assertFalse(fv.is_filled)
        # a later explicit statement resolves it
        guards.apply_field_updates(rfq, [fu("box_dimensions", "12 x 10 x 8 in", "use 12 x 10 x 8", label="Box dimensions")], "Please use 12 x 10 x 8.", "msg:3", 3)
        self.assertEqual(fv.status, FieldStatus.PROVIDED)
        self.assertEqual(fv.value, "12 x 10 x 8 in")
        self.assertEqual(fv.conflict_values, [])
        self.assertEqual(len(fv.history), 2)

    def test_unknown_is_preserved_and_needs_evidence(self):
        rfq = rfq_with()
        guards.apply_field_updates(rfq, [fu("flute", "unknown", "I don't know the flute type", status="unknown")], "I don't know the flute type.", "msg:1", 1)
        self.assertEqual(rfq.fields["flute"].status, FieldStatus.UNKNOWN)
        self.assertIsNone(rfq.fields["flute"].value)
        rfq2 = rfq_with()
        audit = guards.apply_field_updates(rfq2, [fu("flute", "unknown", "no idea about flute", status="unknown")], "I need boxes.", "msg:1", 1)
        self.assertNotIn("flute", rfq2.fields) if "flute" not in rfq2.fields else self.assertEqual(rfq2.fields["flute"].status, FieldStatus.MISSING)
        self.assertTrue(any("ignored" in a for a in audit))

    def test_same_value_restated_adds_reference_only(self):
        rfq = rfq_with()
        guards.apply_field_updates(rfq, [fu("destination", "Mumbai", "to Mumbai", section="logistics")], "Ship to Mumbai", "msg:1", 1)
        guards.apply_field_updates(rfq, [fu("destination", "mumbai", "Mumbai warehouse", section="logistics")], "Yes, the Mumbai warehouse", "msg:2", 2)
        fv = rfq.fields["destination"]
        self.assertEqual(fv.status, FieldStatus.PROVIDED)
        self.assertEqual(fv.source_refs, ["msg:1", "msg:2"])
        self.assertEqual(fv.history, [])


class ApplicabilityGuardTest(unittest.TestCase):
    def test_not_applicable_requires_reason_and_cannot_hit_buyer_facts(self):
        rfq = rfq_with()
        guards.apply_field_updates(rfq, [fu("destination", "Mumbai", "to Mumbai", section="logistics")], "Ship to Mumbai", "msg:1", 1)
        audit = guards.apply_applicability(rfq, [
            {"key": "certifications", "importance": "not_applicable", "reason": ""},
            {"key": "destination", "importance": "not_applicable", "reason": "Plain cartons need no destination, honestly."},
            {"key": "sample_requirements", "importance": "not_applicable", "reason": "Stock cartons are not sampled before order."},
            {"key": "shipping_method", "importance": "required", "reason": "Bulky freight; mode changes landed cost."},
        ])
        self.assertEqual(rfq.fields["certifications"].status, FieldStatus.MISSING)
        self.assertEqual(rfq.fields["destination"].status, FieldStatus.PROVIDED)
        self.assertEqual(rfq.fields["sample_requirements"].status, FieldStatus.NOT_APPLICABLE)
        self.assertEqual(rfq.fields["sample_requirements"].importance, Importance.NOT_APPLICABLE)
        self.assertEqual(rfq.fields["shipping_method"].importance, Importance.REQUIRED)
        self.assertEqual(len([a for a in audit if "rejected" in a]), 2)

    def test_missing_and_unknown_are_never_silently_converted_to_na(self):
        rfq = rfq_with()
        guards.apply_field_updates(rfq, [fu("flute", "unknown", "don't know the flute", status="unknown")], "I don't know the flute", "msg:1", 1)
        guards.apply_applicability(rfq, [{"key": "flute", "importance": "not_applicable", "reason": "Buyer does not know, so skip it."}])
        self.assertEqual(rfq.fields["flute"].status, FieldStatus.UNKNOWN)


class LineItemGuardTest(unittest.TestCase):
    SIZES = ["10x10x5", "12x10x6", "15x10x8", "18x12x10", "20x15x10", "24x18x12", "30x20x15"]
    TEXT = "We need:\n" + "\n".join(SIZES) + "\n2,000 pieces each."

    def items(self, qty=2000.0):
        return [line("Corrugated carton", s, qty, s + " 2,000 pieces each") for s in self.SIZES]

    def test_seven_distinct_variants_become_seven_lines_with_stable_ids(self):
        rfq = rfq_with()
        guards.merge_line_items(rfq, "replace", self.items(), self.TEXT, "msg:1", 1)
        self.assertEqual(len(rfq.line_items), 7)
        self.assertEqual([li.id for li in rfq.line_items], ["LINE-%03d" % i for i in range(1, 8)])
        self.assertTrue(all(li.quantity == 2000.0 for li in rfq.line_items))
        self.assertTrue(all(li.source == Source.BUYER_EXPLICIT for li in rfq.line_items))

    def test_duplicates_are_not_added_twice_and_unverified_lines_are_flagged(self):
        rfq = rfq_with()
        guards.merge_line_items(rfq, "replace", self.items(), self.TEXT, "msg:1", 1)
        audit = guards.merge_line_items(rfq, "append", [line("Corrugated carton", "10x10x5", 2000.0, "10x10x5"),
                                                        line("Corrugated carton", "40x40x40", 500.0, "40x40x40 boxes")], "ok thanks", "msg:2", 2)
        self.assertEqual(len(rfq.line_items), 8)
        self.assertEqual(rfq.line_items[7].id, "LINE-008")
        self.assertEqual(rfq.line_items[7].source, Source.AI_RECOMMENDED)
        self.assertTrue(any("needs buyer confirmation" in a for a in audit))

    def test_update_by_line_id_keeps_history(self):
        rfq = rfq_with()
        guards.merge_line_items(rfq, "replace", self.items(qty=None), self.TEXT, "msg:1", 1)
        guards.merge_line_items(rfq, "append", [line("Corrugated carton", "10x10x5", 3000.0, "3,000 of the smallest", line_id="LINE-001")],
                                "3,000 of the smallest", "msg:2", 2)
        self.assertEqual(rfq.line_items[0].quantity, 3000.0)
        self.assertEqual(rfq.line_items[0].history[0]["field"], "quantity")
        self.assertEqual(len(rfq.line_items), 7)


class QuantitySemanticsTest(unittest.TestCase):
    def test_all_lines_quantified_gives_reference_total_with_note(self):
        rfq = rfq_with()
        for i in range(7):
            rfq.line_items.append(LineItem(id="LINE-%03d" % (i + 1), product="Carton", quantity=2000.0, source_refs=["msg:1"]))
        guards.apply_quantity_semantics(rfq, 1)
        q = rfq.fields["quantity"]
        self.assertEqual(q.value, 14000.0)
        self.assertTrue(q.is_filled)
        self.assertIn("per line item", q.note)
        self.assertEqual(guards.line_item_gaps(rfq), [])

    def test_partial_line_quantities_leave_gaps(self):
        rfq = rfq_with()
        rfq.line_items = [LineItem(id="LINE-001", product="Carton", quantity=2000.0), LineItem(id="LINE-002", product="Carton", quantity=None)]
        guards.apply_quantity_semantics(rfq, 1)
        self.assertFalse(rfq.fields["quantity"].is_filled)
        self.assertEqual(guards.line_item_gaps(rfq), ["LINE-002 · quantity"])

    def test_single_line_mirrors_both_ways(self):
        rfq = rfq_with()
        rfq.line_items = [LineItem(id="LINE-001", product="Carton", quantity=5000.0, source_refs=["msg:1"], evidence="5,000")]
        guards.apply_quantity_semantics(rfq, 1)
        self.assertEqual(rfq.fields["quantity"].value, 5000.0)
        rfq2 = rfq_with()
        rfq2.line_items = [LineItem(id="LINE-001", product="Carton")]
        guards.apply_field_updates(rfq2, [fu("quantity", "800", "800 pcs", section="commercial", value_kind="number")], "800 pcs", "msg:1", 1)
        guards.apply_quantity_semantics(rfq2, 1)
        self.assertEqual(rfq2.line_items[0].quantity, 800.0)

    def test_stated_total_disagreeing_with_line_sum_is_a_conflict(self):
        rfq = rfq_with()
        guards.apply_field_updates(rfq, [fu("quantity", "50000", "50,000 total", section="commercial", value_kind="number")], "50,000 total", "msg:1", 1)
        rfq.line_items = [LineItem(id="LINE-001", product="Carton", quantity=2000.0), LineItem(id="LINE-002", product="Carton", quantity=2000.0)]
        guards.apply_quantity_semantics(rfq, 2)
        self.assertEqual(rfq.fields["quantity"].status, FieldStatus.CONFLICT)

    def test_each_quantity_recorded_at_rfq_level_is_not_a_conflict(self):
        """'2,000 pieces each' captured as the RFQ quantity must resolve to per-line semantics, not a conflict."""
        rfq = rfq_with()
        guards.apply_field_updates(rfq, [fu("quantity", "2000", "2,000 pieces each", section="commercial", value_kind="number", unit="pieces")],
                                   "seven sizes, 2,000 pieces each", "msg:1", 1)
        rfq.line_items = [LineItem(id="LINE-%03d" % i, product="Carton", quantity=2000.0, unit="pieces", source_refs=["msg:1"]) for i in range(1, 8)]
        guards.apply_quantity_semantics(rfq, 1)
        q = rfq.fields["quantity"]
        self.assertEqual(q.status, FieldStatus.PROVIDED)
        self.assertEqual(q.value, 14000.0)
        self.assertIn("7 lines × 2,000 each", q.note)
        # stating the correct total is also fine
        rfq2 = rfq_with()
        guards.apply_field_updates(rfq2, [fu("quantity", "14000", "14,000 total", section="commercial", value_kind="number")], "14,000 total", "msg:1", 1)
        rfq2.line_items = list(rfq.line_items)
        guards.apply_quantity_semantics(rfq2, 1)
        self.assertEqual(rfq2.fields["quantity"].status, FieldStatus.PROVIDED)


class QuestionGuardTest(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()

    def test_first_turn_cap_and_ordering(self):
        rfq = rfq_with()
        qs = [nq("Q%d?" % i, "f%d" % i, importance=("optional" if i % 3 == 0 else "required")) for i in range(12)]
        audit = guards.reconcile_questions(rfq, [], qs, "", "msg:1", 1, True, self.settings)
        self.assertEqual(len(rfq.open_questions()), 8)
        self.assertEqual(rfq.open_questions()[0].importance, Importance.REQUIRED)
        self.assertTrue(any("cap" in a for a in audit))

    def test_later_turn_cap_is_three_for_ai_questions(self):
        rfq = rfq_with()
        guards.reconcile_questions(rfq, [], [nq("Q%d?" % i, "f%d" % i) for i in range(5)], "", "msg:2", 2, False, self.settings)
        ai_asked = [q for q in rfq.open_questions() if (q.field_key or "").startswith("f")]
        self.assertEqual(len(ai_asked), 3, "at most 3 AI questions per later turn")
        self.assertLessEqual(len(rfq.open_questions()), self.settings.max_open_questions)

    def test_never_reask_answered_skipped_or_filled_including_rephrasings(self):
        rfq = rfq_with()
        guards.reconcile_questions(rfq, [], [nq("What are the box dimensions for each size?", "box_dimensions"),
                                             nq("Where should the boxes be delivered?", "destination", section="logistics")], "", "msg:1", 1, True, self.settings)
        dims_q = [q for q in rfq.open_questions() if q.field_key == "box_dimensions"][0]
        dest_q = [q for q in rfq.open_questions() if q.field_key == "destination"][0]
        # buyer skipped destination, and dimensions got filled through free text
        dest_q.status = QuestionStatus.SKIPPED
        guards.apply_field_updates(rfq, [fu("box_dimensions", "12x10x6 in", "12x10x6", label="Box dimensions")], "They are 12x10x6", "msg:2", 2)
        audit = guards.reconcile_questions(rfq, [], [
            nq("What size should each carton be?", "box_dimensions"),                     # same field, filled → dropped
            nq("Which city should we deliver the boxes to?", "destination", section="logistics"),  # skipped → dropped
            nq("What are the box dimensions for each size, in inches?", None),             # rephrasing → dropped
            nq("What flute profile do you need?", "flute"),
        ], "They are 12x10x6", "msg:2", 2, False, self.settings)
        self.assertEqual(dims_q.status, QuestionStatus.ANSWERED)
        self.assertEqual(dims_q.resolution, "filled")
        self.assertIn("What flute profile do you need?", [q.question for q in rfq.open_questions()])
        self.assertNotIn("What size should each carton be?", [q.question for q in rfq.open_questions()])
        self.assertNotIn("Which city should we deliver the boxes to?", [q.question for q in rfq.open_questions()])
        self.assertEqual(len([a for a in audit if "dropped" in a]), 3)

    def test_ai_mapped_free_text_answers_close_questions_and_unknown_marks_field(self):
        rfq = rfq_with()
        guards.reconcile_questions(rfq, [], [nq("What flute type?", "flute"), nq("Target price per box?", "target_landed_cost", section="commercial")],
                                   "", "msg:1", 1, True, self.settings)
        q_flute = [q for q in rfq.open_questions() if q.field_key == "flute"][0]
        q_price = [q for q in rfq.open_questions() if q.field_key == "target_landed_cost"][0]
        guards.reconcile_questions(rfq, [
            {"question_id": q_flute.id, "resolution": "unknown", "answer_summary": "Buyer does not know", "evidence": "no idea what flute"},
            {"question_id": q_price.id, "resolution": "answered", "answer_summary": "Around $0.40", "evidence": "around $0.40"},
        ], [], "I have no idea what flute we need, budget is around $0.40 a box", "msg:2", 2, False, self.settings)
        self.assertEqual(q_flute.status, QuestionStatus.ANSWERED)
        self.assertEqual(q_flute.resolution, "unknown")
        self.assertEqual(rfq.fields["flute"].status, FieldStatus.UNKNOWN) if "flute" in rfq.fields else None
        self.assertEqual(q_price.answer, "Around $0.40")

    def test_required_field_never_asked_gets_a_standard_question_once(self):
        rfq = rfq_with()
        guards.reconcile_questions(rfq, [], [nq("What flute?", "flute")], "", "msg:1", 1, True, self.settings)
        keys = [q.field_key for q in rfq.open_questions()]
        for k in ("quantity", "destination", "customization_type", "technical_summary"):
            self.assertIn(k, keys)
        n = len(rfq.questions)
        guards.reconcile_questions(rfq, [], [], "", "msg:2", 2, False, self.settings)
        self.assertEqual(len(rfq.questions), n, "standard questions are added once")
        # once the field is filled the standard question closes and never comes back
        guards.apply_field_updates(rfq, [fu("destination", "Mumbai", "Mumbai", section="logistics")], "Mumbai", "msg:3", 3)
        guards.reconcile_questions(rfq, [], [], "Mumbai", "msg:3", 3, False, self.settings)
        self.assertEqual([q.status for q in rfq.questions if q.field_key == "destination"], [QuestionStatus.ANSWERED])

    def test_conflict_generates_a_required_choice_question_once(self):
        rfq = rfq_with()
        guards.apply_field_updates(rfq, [fu("quantity", "30000", "30,000", section="commercial", value_kind="number")], "30,000", "msg:1", 1)
        guards.apply_field_updates(rfq, [fu("quantity", "50000", "50,000", section="commercial", value_kind="number")], "50,000", "msg:2", 2)
        guards.reconcile_questions(rfq, [], [], "", "msg:2", 2, False, self.settings)
        guards.reconcile_questions(rfq, [], [], "", "msg:2", 2, False, self.settings)
        conflict_qs = [q for q in rfq.open_questions() if q.field_key == "quantity"]
        self.assertEqual(len(conflict_qs), 1)
        self.assertEqual(conflict_qs[0].importance, Importance.REQUIRED)
        self.assertEqual(conflict_qs[0].suggested_options, ["30,000", "50,000"])


class LabelJoinTest(unittest.TestCase):
    def test_never_claims_more_than_it_shows(self):
        self.assertEqual(guards.join_labels([]), "")
        self.assertEqual(guards.join_labels(["A"]), "A")
        self.assertEqual(guards.join_labels(["A", "B"]), "A and B")
        self.assertEqual(guards.join_labels(["A", "B", "C", "D"]), "A, B, C and D")
        self.assertEqual(guards.join_labels(["A", "B", "C", "D", "E", "F"]), "A, B, C, D and 2 more")


class CompletenessTest(unittest.TestCase):
    def test_scores_and_readiness(self):
        rfq = rfq_with()
        # make it small: only three fields count
        for k, fv in list(rfq.fields.items()):
            if k not in ("quantity", "destination", "certifications"):
                fv.importance, fv.status = Importance.NOT_APPLICABLE, FieldStatus.NOT_APPLICABLE
        c = guards.compute_completeness(rfq, {"score": 95, "ready_to_send": True, "explanation": "looks good"}, 1)
        self.assertEqual(c.score, 0)
        self.assertFalse(c.ready_to_send, "AI readiness claim must never override the rules")
        self.assertEqual(c.ai_score, 95)
        self.assertEqual(c.ai_ready_claim, True)
        self.assertIn("Order quantity", c.missing_required_fields)
        guards.apply_field_updates(rfq, [fu("quantity", "1000", "1,000 pcs", section="commercial", value_kind="number")], "1,000 pcs", "msg:1", 1)
        c = guards.compute_completeness(rfq, None, 1)
        self.assertEqual(c.score, int(round(100 * 3 / 7)))
        self.assertFalse(c.ready_to_send)
        guards.apply_field_updates(rfq, [fu("destination", "Mumbai", "Mumbai", section="logistics")], "Mumbai", "msg:1", 1)
        c = guards.compute_completeness(rfq, None, 1)
        self.assertTrue(c.ready_to_send)
        self.assertEqual(c.score, int(round(100 * 6 / 7)))
        self.assertIn("Certifications", c.recommended_fields)
        self.assertTrue(c.explanation.startswith("Ready to send"))
        # the sentence must not promise more names than it prints
        import re as _re
        m = _re.search(r"Answering (\d+) more", c.explanation)
        self.assertIsNone(m, "counts are not asserted without the matching names: %s" % c.explanation)

    def test_unknown_does_not_block_but_conflict_and_line_gaps_do(self):
        rfq = rfq_with()
        for k, fv in list(rfq.fields.items()):
            if k not in ("quantity", "destination"):
                fv.importance, fv.status = Importance.NOT_APPLICABLE, FieldStatus.NOT_APPLICABLE
        guards.apply_field_updates(rfq, [fu("destination", "unknown", "not sure where yet", section="logistics", status="unknown"),
                                         fu("quantity", "1000", "1,000", section="commercial", value_kind="number")], "not sure where yet, 1,000", "msg:1", 1)
        c = guards.compute_completeness(rfq, None, 1)
        self.assertTrue(c.ready_to_send)
        rfq.line_items.append(LineItem(id="LINE-001", product="Carton", quantity=None))
        c = guards.compute_completeness(rfq, None, 1)
        self.assertFalse(c.ready_to_send)
        self.assertIn("LINE-001 · quantity", c.missing_required_fields)

    def test_blocking_questions_are_listed_without_double_counting_fields(self):
        rfq = rfq_with()
        for k, fv in list(rfq.fields.items()):
            if k not in ("quantity", "destination"):
                fv.importance, fv.status = Importance.NOT_APPLICABLE, FieldStatus.NOT_APPLICABLE
        # one required question on a missing field (already listed) + one on a product-specific field (not listed)
        rfq.questions.append(Question(id="q_a", category=Section.COMMERCIAL, question="How many boxes?", importance=Importance.REQUIRED, field_key="quantity"))
        rfq.questions.append(Question(id="q_b", category=Section.TECHNICAL, question="What flute type?", importance=Importance.REQUIRED, field_key="flute"))
        rfq.questions.append(Question(id="q_c", category=Section.TECHNICAL, question="Any nice-to-have?", importance=Importance.OPTIONAL, field_key="x"))
        c = guards.compute_completeness(rfq, None, 1)
        self.assertEqual(c.blocking_questions, ["What flute type?"], "a question on an already-listed missing field is not counted twice")
        blockers = c.missing_required_fields + c.open_conflicts + c.blocking_questions
        self.assertEqual(len(blockers), 3)  # Order quantity, Destination, flute question
        self.assertFalse(c.ready_to_send)

    def test_unclassified_rfq_is_capped(self):
        rfq = RFQ(id="x", fields=new_field_set())
        for fv in rfq.fields.values():
            fv.status, fv.source, fv.value = FieldStatus.PROVIDED, Source.BUYER_EXPLICIT, "x"
        c = guards.compute_completeness(rfq, None, 1)
        self.assertLessEqual(c.score, 20)
        self.assertFalse(c.ready_to_send)

    def test_status_transitions(self):
        rfq = rfq_with()
        rfq.turn = 0
        self.assertEqual(guards.next_status(rfq), RFQStatus.DRAFT)
        rfq.turn = 1
        rfq.completeness.ready_to_send = False
        self.assertEqual(guards.next_status(rfq), RFQStatus.IN_PROGRESS)
        rfq.completeness.ready_to_send = True
        self.assertEqual(guards.next_status(rfq), RFQStatus.READY)
        rfq.status = RFQStatus.SUPPLIER_READY
        self.assertEqual(guards.next_status(rfq), RFQStatus.SUPPLIER_READY)


class TechnicalSummaryTest(unittest.TestCase):
    def test_derived_only_from_buyer_facts(self):
        rfq = rfq_with()
        guards.derive_technical_summary(rfq, 1)
        self.assertFalse(rfq.fields["technical_summary"].is_filled)
        guards.apply_field_updates(rfq, [fu("board_grade", "32 ECT", "32 ECT", label="Board grade"),
                                         fu("flute", "B flute", None, source="ai_recommended", status="recommended")], "32 ECT please", "msg:1", 1)
        guards.derive_technical_summary(rfq, 1)
        ts = rfq.fields["technical_summary"]
        self.assertTrue(ts.is_filled)
        self.assertIn("Board grade: 32 ECT", ts.value)
        self.assertNotIn("flute", ts.value.lower())


if __name__ == "__main__":
    unittest.main()
