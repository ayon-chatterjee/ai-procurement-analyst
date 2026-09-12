"""The analyst end to end, with the model scripted.

What these guarantee: the model's job stays small (it plans a query and words an answer,
and sees no prices doing either), a what-if never touches the database, a question that
cannot be answered is refused and recorded, and the deterministic answer survives the
model failing entirely.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import unittest

from rfq_copilot.ai_service import AIInvalidOutput, AITimeout
from rfq_copilot.analyst_models import AnalystQuery, Hypothetical, Intent
from rfq_copilot.analyst_service import AnalystError, AnalystService
from rfq_copilot.config import Settings
from rfq_copilot.persistence import RFQRepository
from rfq_copilot.supplier_models import ClaimStatus
from rfq_copilot.supplier_service import SupplierService
from tests.analyst_helpers import bundle, explanation, quote, raw_query, silent
from tests.supplier_helpers import ScriptedAI, carton_rfq


class AnalystHarness(unittest.TestCase):
    """A real service over a temporary database, with bundles saved directly.

    Extraction is Phase 2's business and is already covered there; saving the bundles
    keeps these tests about the analysis.
    """

    def build(self, payloads=None, *, quantity=2000.0, sizes=None):
        tmp = tempfile.mkdtemp(prefix="rfq_p3_")
        settings = Settings()
        settings.db_path = os.path.join(tmp, "t.db")
        self.settings = settings
        self.repo = RFQRepository(settings.db_path)
        self.rfq = carton_rfq(quantity=quantity, sizes=sizes or ["10 x 10 x 5", "12 x 10 x 6"])
        self.repo.save_rfq(self.rfq)
        self.ai = ScriptedAI(payloads or [])
        self.sup = SupplierService(self.repo, self.ai, settings)
        self.svc = AnalystService(self.ai, self.repo, settings, self.sup)
        return self.svc

    def seed(self, *specs):
        """Save one bundle per (name, quotes, kwargs) spec and return the suppliers."""
        out = []
        for name, quotes, kwargs in specs:
            supplier, b = bundle(self.rfq, name, quotes, **kwargs)
            self.sup.store.save_supplier(supplier)
            self.sup.store.save_bundle(b)
            out.append(supplier)
        return out

    def two_suppliers(self):
        return self.seed(
            ("Alpha Cartons", [quote(self.rfq, "LINE-001", 0.42), quote(self.rfq, "LINE-002", 0.55)],
             {"certs": [("ISO 9001", ClaimStatus.VERIFIED)],
              "answers": [("required_delivery_date", "18 days", ClaimStatus.CLAIMED)]}),
            ("Beta Boxes", [quote(self.rfq, "LINE-001", 0.39)],
             {"certs": [("ISO 9001", ClaimStatus.CLAIMED)],
              "answers": [("required_delivery_date", "20 days", ClaimStatus.CLAIMED)]}))


class StageAParsingTest(AnalystHarness):
    def test_several_phrasings_produce_the_same_answer(self):
        questions = ["Who is cheapest for each line?",
                     "Give me the lowest quote per item.",
                     "Which supplier has the cheapest price on every line?"]
        payloads = []
        for _ in questions:
            payloads += [raw_query("cheapest_by_line"), explanation("Beta Boxes is lowest.")]
        self.build(payloads)
        self.two_suppliers()
        answers = [self.svc.ask(self.rfq.id, q) for q in questions]
        self.assertEqual({a.intent for a in answers}, {Intent.CHEAPEST_BY_LINE.value})
        self.assertEqual(len({str(a.rows) for a in answers}), 1,
                         "the same question asked three ways gives one answer")

    def test_the_planner_is_never_shown_a_price(self):
        self.build([raw_query("cheapest_by_line"), explanation("Beta Boxes is lowest.")])
        self.two_suppliers()
        self.svc.ask(self.rfq.id, "Who is cheapest?")
        planning_prompt = self.ai.calls[0]["prompt"]
        for figure in ("0.42", "0.39", "0.55"):
            self.assertNotIn(figure, planning_prompt,
                             "the query planner must not be given the quotes themselves")

    def test_a_refinement_carries_the_previous_what_if_forward(self):
        self.build([raw_query("cheapest_by_line",
                              hypothetical={"exclude_suppliers": ["Beta Boxes"]}),
                    explanation("Alpha Cartons is lowest."),
                    raw_query("cheapest_by_line", refines_previous=True,
                              filters=[{"field": "eligibility", "op": "is", "values": ["cleared"]}]),
                    explanation("Alpha Cartons is lowest.")])
        self.two_suppliers()
        self.svc.ask(self.rfq.id, "Who is cheapest if we set Beta aside?")
        second = self.svc.ask(self.rfq.id, "Now only among those who cleared QA.")
        self.assertTrue(second.hypothetical)
        self.assertIn("Beta Boxes", "; ".join(second.hypothetical_labels))
        self.assertEqual([f.field for f in second.query.filters], ["eligibility"])

    def test_a_fresh_question_does_not_inherit_the_previous_what_if(self):
        self.build([raw_query("cheapest_by_line",
                              hypothetical={"exclude_suppliers": ["Beta Boxes"]}),
                    explanation("Alpha Cartons is lowest."),
                    raw_query("supplier_coverage"), explanation("Alpha Cartons quoted most.")])
        self.two_suppliers()
        self.svc.ask(self.rfq.id, "Cheapest without Beta?")
        second = self.svc.ask(self.rfq.id, "Who quoted the most lines?")
        self.assertFalse(second.hypothetical)

    def test_the_history_it_sees_carries_questions_not_results(self):
        self.build([raw_query("cheapest_by_line"), explanation("Beta Boxes is lowest."),
                    raw_query("supplier_coverage"), explanation("Alpha Cartons quoted most.")])
        self.two_suppliers()
        self.svc.ask(self.rfq.id, "Who is cheapest for each line?")
        self.svc.ask(self.rfq.id, "And who quoted the most?")
        second_plan = self.ai.calls[2]["prompt"]
        self.assertIn("Who is cheapest for each line?", second_plan)
        self.assertNotIn("0.39", second_plan, "a previous result must not leak into a new plan")

    def test_invalid_output_is_retried_once_with_the_validation_error(self):
        self.build([AIInvalidOutput("missing field intent"), raw_query("supplier_coverage"),
                    explanation("Alpha Cartons quoted most.")])
        self.two_suppliers()
        result = self.svc.ask(self.rfq.id, "Who quoted most?")
        self.assertEqual(result.intent, Intent.SUPPLIER_COVERAGE.value)
        self.assertIn("FAILED VALIDATION", self.ai.calls[1]["prompt"])


class RefusalTest(AnalystHarness):
    def test_an_unsupported_question_is_refused_in_the_standard_words(self):
        self.build([raw_query("unsupported",
                              unsupported_reason="I have no data on suppliers outside this RFQ")])
        self.two_suppliers()
        result = self.svc.ask(self.rfq.id, "Who is the best carton supplier in China?")
        self.assertTrue(result.refused)
        self.assertIn("can't answer that reliably", result.summary)
        self.assertIn("outside this RFQ", result.summary)
        self.assertEqual(result.rows, [])

    def test_a_supplier_this_rfq_never_had_is_refused_with_the_real_names(self):
        self.build([raw_query("why_excluded", subject_suppliers=["Globex Packaging"])])
        self.two_suppliers()
        result = self.svc.ask(self.rfq.id, "Why not Globex Packaging?")
        self.assertTrue(result.refused)
        self.assertIn("Alpha Cartons", result.summary)

    def test_a_refusal_costs_no_explanation_call(self):
        self.build([raw_query("unsupported", unsupported_reason="not in this data")])
        self.two_suppliers()
        self.svc.ask(self.rfq.id, "What will steel cost next year?")
        self.assertEqual(len(self.ai.calls), 1, "a refusal is not worth a second model call")

    def test_an_rfq_with_no_extracted_responses_is_refused_before_any_model_call(self):
        self.build([])
        with self.assertRaises(AnalystError):
            self.svc.ask(self.rfq.id, "Who is cheapest?")
        self.assertEqual(self.ai.calls, [])

    def test_an_empty_question_is_refused_before_any_model_call(self):
        self.build([])
        with self.assertRaises(AnalystError):
            self.svc.ask(self.rfq.id, "   ")
        self.assertEqual(self.ai.calls, [])


class ImmutabilityTest(AnalystHarness):
    """A what-if explores the data; it never edits it."""

    TABLES = ("suppliers", "supplier_responses", "supplier_quotes", "certifications",
              "questionnaire_responses", "evidence", "supplier_questions", "rfqs")

    def fingerprint(self):
        conn = sqlite3.connect(self.settings.db_path)
        digest = hashlib.sha256()
        for table in self.TABLES:
            for row in conn.execute("SELECT * FROM %s ORDER BY id" % table):
                digest.update(repr(row).encode("utf-8"))
        conn.close()
        return digest.hexdigest()

    def test_a_hypothetical_exclusion_leaves_the_database_untouched(self):
        self.build([raw_query("cheapest_by_line",
                              hypothetical={"exclude_suppliers": ["Beta Boxes"]}),
                    explanation("Alpha Cartons is lowest.")])
        self.two_suppliers()
        before = self.fingerprint()
        result = self.svc.ask(self.rfq.id, "What if we drop Beta Boxes?")
        self.assertTrue(result.hypothetical)
        self.assertEqual(before, self.fingerprint())

    def test_treating_claims_as_verified_changes_the_answer_not_the_records(self):
        self.build([raw_query("qualification_status",
                              hypothetical={"treat_claimed_as_verified": True}),
                    explanation("Both suppliers would clear.")])
        suppliers = self.two_suppliers()
        before = self.fingerprint()
        result = self.svc.ask(self.rfq.id, "What if a claimed certificate counted?")
        self.assertEqual(before, self.fingerprint())
        beta = next(s for s in suppliers if s.name == "Beta Boxes")
        stored = self.sup.bundles_for(self.rfq.id)
        cert = next(c for b in stored if b.response.supplier_id == beta.id
                    for c in b.certifications)
        self.assertEqual(cert.status, ClaimStatus.CLAIMED,
                         "the stored certification is still only a claim")

    def test_running_a_known_query_needs_no_model_at_all(self):
        self.build([])
        self.two_suppliers()
        result = self.svc.run_query(self.rfq.id, AnalystQuery(intent=Intent.SUPPLIER_COVERAGE.value))
        self.assertEqual(self.ai.calls, [])
        self.assertTrue(result.rows)


class PersistenceTest(AnalystHarness):
    def test_each_question_is_recorded_with_the_query_it_became(self):
        self.build([raw_query("supplier_coverage", reading="How much did each supplier quote?"),
                    explanation("Alpha Cartons quoted most.")])
        self.two_suppliers()
        self.svc.ask(self.rfq.id, "Who quoted the most lines?")
        records = self.svc.store.list_queries(self.rfq.id)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].question, "Who quoted the most lines?")
        self.assertEqual(records[0].intent, Intent.SUPPLIER_COVERAGE.value)
        self.assertEqual(records[0].query_dict()["intent"], Intent.SUPPLIER_COVERAGE.value)

    def test_a_refusal_is_recorded_too(self):
        self.build([raw_query("unsupported", unsupported_reason="not in this data")])
        self.two_suppliers()
        self.svc.ask(self.rfq.id, "What is the market price?")
        self.assertTrue(self.svc.store.list_queries(self.rfq.id)[0].refused)

    def test_both_model_calls_are_audited_under_their_own_names(self):
        self.build([raw_query("supplier_coverage"), explanation("Alpha Cartons quoted most.")])
        self.two_suppliers()
        self.svc.ask(self.rfq.id, "Who quoted the most lines?")
        conn = sqlite3.connect(self.settings.db_path)
        kinds = {r[0] for r in conn.execute(
            "SELECT call_type FROM ai_calls WHERE rfq_id = ?", (self.rfq.id,))}
        conn.close()
        self.assertEqual(kinds, {"analyst_parse", "analyst_explain"})

    def test_history_survives_a_new_service_instance(self):
        self.build([raw_query("supplier_coverage"), explanation("Alpha Cartons quoted most.")])
        self.two_suppliers()
        self.svc.ask(self.rfq.id, "Who quoted the most lines?")
        fresh = AnalystService(ScriptedAI([]), self.repo, self.settings, self.sup)
        turns = fresh.history(self.rfq.id)
        self.assertEqual(turns[-1].question, "Who quoted the most lines?")
        self.assertEqual(turns[-1].query.intent, Intent.SUPPLIER_COVERAGE.value)

    def test_deleting_the_rfq_takes_its_analyst_history_with_it(self):
        self.build([raw_query("supplier_coverage"), explanation("Alpha Cartons quoted most.")])
        self.two_suppliers()
        self.svc.ask(self.rfq.id, "Who quoted the most lines?")
        conn = sqlite3.connect(self.settings.db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("DELETE FROM rfqs WHERE id = ?", (self.rfq.id,))
        conn.commit()
        left = conn.execute("SELECT COUNT(*) FROM analyst_queries").fetchone()[0]
        conn.close()
        self.assertEqual(left, 0)


class ExplanationTest(AnalystHarness):
    def test_the_answer_survives_the_explanation_failing(self):
        self.build([raw_query("supplier_coverage"), AITimeout("the model timed out")])
        self.two_suppliers()
        result = self.svc.ask(self.rfq.id, "Who quoted the most lines?")
        self.assertTrue(result.rows, "the calculated answer stands on its own")
        self.assertIsNone(result.explanation)
        self.assertTrue(result.explanation_status.startswith("failed:"))
        self.assertTrue(result.summary)

    def test_a_narration_with_an_invented_figure_is_dropped(self):
        self.build([raw_query("supplier_coverage"),
                    explanation("Alpha Cartons quoted 999 lines.")])
        self.two_suppliers()
        result = self.svc.ask(self.rfq.id, "Who quoted the most lines?")
        self.assertIsNone(result.explanation)
        self.assertIn("999", result.explanation_status)
        self.assertTrue(result.answer_text(), "the deterministic summary is still shown")

    def test_a_narration_recommending_a_supplier_is_dropped(self):
        self.build([raw_query("supplier_coverage"),
                    explanation("I recommend Alpha Cartons.")])
        self.two_suppliers()
        result = self.svc.ask(self.rfq.id, "Who quoted the most lines?")
        self.assertIsNone(result.explanation)
        self.assertIn("award language", result.explanation_status)

    def test_a_faithful_narration_is_kept_alongside_the_summary(self):
        self.build([raw_query("supplier_coverage"),
                    explanation("Alpha Cartons quoted 2 of 2 lines.")])
        self.two_suppliers()
        result = self.svc.ask(self.rfq.id, "Who quoted the most lines?")
        self.assertEqual(result.explanation_status, "ok")
        self.assertEqual(result.answer_text(), result.explanation)
        self.assertTrue(result.summary)

    def test_turning_narration_off_costs_one_model_call(self):
        self.build([raw_query("supplier_coverage")])
        self.two_suppliers()
        result = self.svc.ask(self.rfq.id, "Who quoted the most lines?", explain=False)
        self.assertEqual(len(self.ai.calls), 1)
        self.assertTrue(result.rows)


class SuggestedQuestionTest(AnalystHarness):
    def test_every_suggestion_answers_without_a_model_call(self):
        self.build([])
        self.two_suppliers()
        for label, query in self.svc.suggested_queries(self.rfq.id):
            result = self.svc.run_query(self.rfq.id, query)
            self.assertFalse(result.refused, label)
            self.assertTrue(result.summary, label)
        self.assertEqual(self.ai.calls, [])


if __name__ == "__main__":
    unittest.main()


class ContextScopeTest(AnalystHarness):
    """A follow-up inherits assumptions, not the subject of the question before it."""

    def test_a_line_named_in_an_earlier_question_does_not_narrow_the_next_one(self):
        self.build([raw_query("why_excluded", subject_suppliers=["Beta Boxes"],
                              subject_lines=["LINE-001"]),
                    explanation("Beta Boxes is not lowest on LINE-001."),
                    raw_query("cheapest_by_line", refines_previous=True),
                    explanation("Alpha Cartons is lowest.")])
        self.two_suppliers()
        self.svc.ask(self.rfq.id, "Why not Beta Boxes for line 1?")
        second = self.svc.ask(self.rfq.id, "And who is cheapest overall?")
        self.assertEqual(second.query.subject_line_ids, [],
                         "a broad follow-up is not scoped to the previous question's line")
        self.assertEqual(len(second.rows), len(self.rfq.line_items))

    def test_a_supplier_named_in_an_earlier_question_does_not_narrow_the_next_one(self):
        self.build([raw_query("why_excluded", subject_suppliers=["Beta Boxes"]),
                    explanation("Beta Boxes is not lowest."),
                    raw_query("supplier_coverage", refines_previous=True),
                    explanation("Alpha Cartons quoted most.")])
        self.two_suppliers()
        self.svc.ask(self.rfq.id, "Why not Beta Boxes?")
        second = self.svc.ask(self.rfq.id, "How much did everyone quote?")
        self.assertEqual(len(second.rows), 2, "both suppliers are still in scope")
