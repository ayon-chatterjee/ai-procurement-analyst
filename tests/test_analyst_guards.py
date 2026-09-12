"""What the analyst accepts from the model, and what it refuses.

The model proposes; these rules dispose. A proposal naming something this RFQ does not
contain is refused rather than quietly trimmed, because a silently dropped exclusion
changes the answer without telling the buyer. A narration containing a figure the result
does not support is dropped entirely, because a sentence we cannot stand behind is worse
than no sentence.
"""
from __future__ import annotations

import unittest

from rfq_copilot.analyst_guards import (
    Ambiguous, Resolved, Unknown, guard_explanation, resolve_line, resolve_supplier,
    validate_query,
)
from rfq_copilot.analyst_models import (
    AnalystQuery, AnalystResult, Exclusion, Intent, RawQuery, Refusal,
)
from rfq_copilot.supplier_models import Supplier
from tests.analyst_helpers import raw_query
from tests.supplier_helpers import carton_rfq

SUPPLIERS = [
    Supplier(id="sup_a", name="Anhui Packaging Co", country="China"),
    Supplier(id="sup_b", name="Shenzhen Print & Pack", country="China"),
    Supplier(id="sup_c", name="Viet Carton JSC", country="Vietnam"),
    Supplier(id="sup_d", name="Istanbul Ambalaj", country="Turkey"),
]


def check(payload):
    return validate_query(RawQuery.from_dict(payload), carton_rfq(), SUPPLIERS)


class SupplierResolutionTest(unittest.TestCase):
    def test_an_id_a_full_name_and_a_distinctive_word_all_resolve(self):
        for text, want in (("sup_a", "sup_a"), ("Anhui Packaging Co", "sup_a"),
                           ("anhui packaging co", "sup_a"), ("Anhui", "sup_a"),
                           ("Viet Carton", "sup_c")):
            got = resolve_supplier(text, SUPPLIERS)
            self.assertIsInstance(got, Resolved, text)
            self.assertEqual(got.id, want, text)

    def test_a_resolution_is_recorded_so_the_buyer_can_see_how_it_was_read(self):
        got = resolve_supplier("Anhui", SUPPLIERS)
        self.assertIn("Anhui Packaging Co", got.how)

    def test_a_letter_label_is_refused_with_the_real_names(self):
        got = resolve_supplier("Supplier B", SUPPLIERS)
        self.assertIsInstance(got, Unknown)
        self.assertIn("Shenzhen Print & Pack", got.candidates)

    def test_an_unknown_supplier_is_refused_and_the_real_names_offered(self):
        result = check(raw_query("why_excluded", subject_suppliers=["Shenzen Printing"]))
        self.assertIsInstance(result, Refusal)
        self.assertIn("No supplier called", result.reason)
        self.assertIn("Anhui Packaging Co", result.candidates)

    def test_an_unknown_what_if_exclusion_is_a_refusal_not_a_silent_drop(self):
        result = check(raw_query("cheapest_by_line",
                                 hypothetical={"exclude_suppliers": ["Nobody Ltd"]}))
        self.assertIsInstance(result, Refusal)

    def test_an_ambiguous_name_is_refused_with_the_candidates(self):
        pair = [Supplier(id="s1", name="Pacific Carton Works"),
                Supplier(id="s2", name="Pacific Carton Supply")]
        got = resolve_supplier("Pacific Carton", pair)
        self.assertIsInstance(got, Ambiguous)
        self.assertEqual(len(got.candidates), 2)


class LineResolutionTest(unittest.TestCase):
    def setUp(self):
        self.lines = carton_rfq().line_items

    def test_a_line_is_found_however_the_buyer_spells_it(self):
        for text in ("LINE-003", "line 3", "Line-3", "#3", "3"):
            got = resolve_line(text, self.lines)
            self.assertIsInstance(got, Resolved, text)
            self.assertEqual(got.id, "LINE-003", text)

    def test_dimensions_find_the_line_they_describe(self):
        got = resolve_line("12 x 10 x 6", self.lines)
        self.assertIsInstance(got, Resolved)
        self.assertEqual(got.id, "LINE-002")

    def test_a_line_this_rfq_does_not_have_is_refused(self):
        got = resolve_line("LINE-099", self.lines)
        self.assertIsInstance(got, Unknown)
        self.assertIn("LINE-001", got.candidates)


class QueryValidationTest(unittest.TestCase):
    def test_an_unknown_intent_is_refused(self):
        self.assertIsInstance(check(raw_query("do_my_taxes")), Refusal)

    def test_a_quality_filter_is_accepted_however_the_model_phrases_it(self):
        """Found on a live run: "only among suppliers who cleared QA" — the question the
        demo is built around — was refused whenever the model wrote `in` rather than `is`,
        even though the calculation asks the same set-membership question either way."""
        for op in ("is", "in", "not_in"):
            query = check(raw_query("cheapest_by_line",
                                    filters=[{"field": "eligibility", "op": op,
                                              "values": ["cleared"]}]))
            self.assertNotIsInstance(query, Refusal,
                                     "eligibility %s was refused: %r" % (op, query))
            self.assertEqual(query.filters[0].op, op)

    def test_a_filter_operator_the_field_cannot_support_is_still_refused(self):
        refusal = check(raw_query("cheapest_by_line",
                                  filters=[{"field": "eligibility", "op": "lte",
                                            "values": ["cleared"]}]))
        self.assertIsInstance(refusal, Refusal)

    def test_unsupported_carries_the_models_own_reason(self):
        result = check(raw_query("unsupported",
                                 unsupported_reason="that needs market data we do not hold"))
        self.assertIsInstance(result, Refusal)
        self.assertIn("market data", result.reason)

    def test_a_filter_the_intent_does_not_honour_is_refused(self):
        result = check(raw_query("quote_validity",
                                 filters=[{"field": "unit_price", "op": "lte", "values": ["1"]}]))
        self.assertIsInstance(result, Refusal)
        self.assertIn("does not apply", result.reason)

    def test_an_operator_that_makes_no_sense_for_a_field_is_refused(self):
        result = check(raw_query("cheapest_by_line",
                                 filters=[{"field": "currency", "op": "lte", "values": ["USD"]}]))
        self.assertIsInstance(result, Refusal)

    def test_a_value_that_should_be_a_number_is_checked(self):
        result = check(raw_query("lead_time_comparison",
                                 filters=[{"field": "lead_time_days", "op": "lte",
                                           "values": ["soon"]}]))
        self.assertIsInstance(result, Refusal)
        self.assertIn("not a number", result.reason)

    def test_an_unrecognised_currency_is_refused(self):
        self.assertIsInstance(
            check(raw_query("cheapest_by_line", comparison_currency="CENTS")), Refusal)

    def test_a_questionnaire_key_this_rfq_never_asked_is_refused(self):
        result = check(raw_query("qualification_status",
                                 filters=[{"field": "questionnaire", "op": "answered",
                                           "values": ["warranty_years"]}]))
        self.assertIsInstance(result, Refusal)
        self.assertIn("did not ask", result.reason)

    def test_a_lookup_field_we_do_not_hold_is_refused_rather_than_guessed(self):
        result = check(raw_query("lookup", fields=["profit_margin"], grain="supplier"))
        self.assertIsInstance(result, Refusal)
        self.assertIn("don't hold a field", result.reason)

    def test_a_field_cannot_be_listed_at_a_grain_it_has_no_meaning_at(self):
        result = check(raw_query("lookup", fields=["price"], grain="supplier"))
        self.assertIsInstance(result, Refusal)

    def test_why_excluded_needs_exactly_one_supplier(self):
        self.assertIsInstance(check(raw_query("why_excluded")), Refusal)
        self.assertIsInstance(
            check(raw_query("why_excluded", subject_suppliers=["Anhui", "Viet Carton"])), Refusal)
        self.assertIsInstance(
            check(raw_query("why_excluded", subject_suppliers=["Anhui"])), AnalystQuery)

    def test_a_price_comparison_needs_one_or_two_suppliers(self):
        self.assertIsInstance(check(raw_query("price_difference")), Refusal)
        self.assertIsInstance(
            check(raw_query("price_difference",
                            subject_suppliers=["Anhui", "Viet Carton", "Istanbul Ambalaj"])),
            Refusal)

    def test_an_evidence_lookup_must_say_what_it_wants_the_source_of(self):
        self.assertIsInstance(check(raw_query("evidence_lookup")), Refusal)
        result = check(raw_query("evidence_lookup",
                                 evidence_target={"supplier": "Anhui", "topic": "price"}))
        self.assertIsInstance(result, Refusal)
        self.assertIn("belongs to one line", result.reason)

    def test_a_valid_query_records_how_each_name_was_read(self):
        result = check(raw_query("why_excluded", subject_suppliers=["Anhui"],
                                 subject_lines=["line 3"]))
        self.assertIsInstance(result, AnalystQuery)
        self.assertEqual(result.subject_supplier_ids, ["sup_a"])
        self.assertEqual(result.subject_line_ids, ["LINE-003"])
        self.assertEqual(len(result.resolution_notes), 2)

    def test_a_filter_value_naming_a_supplier_is_resolved_to_its_id(self):
        result = check(raw_query("cheapest_by_line",
                                 filters=[{"field": "supplier", "op": "in", "values": ["Anhui"]}]))
        self.assertIsInstance(result, AnalystQuery)
        self.assertEqual(result.filters[0].values, ["sup_a"])

    def test_a_hypothetical_is_dropped_for_an_intent_that_cannot_use_one(self):
        result = check(raw_query("quote_validity",
                                 hypothetical={"ignore_moq_constraints": True}))
        self.assertIsInstance(result, AnalystQuery)
        self.assertFalse(result.hypothetical.is_active())


class ExplanationGuardTest(unittest.TestCase):
    def setUp(self):
        self.result = AnalystResult(
            intent=Intent.CHEAPEST_BY_LINE.value,
            summary="7 of 7 lines have a comparable quote.",
            columns=["Line", "Cheapest supplier", "Price"],
            rows=[{"Line": "LINE-001", "Cheapest supplier": "Anhui Packaging Co", "Price": 0.42}],
            metrics={"lines_answered": 7},
            exclusions=[Exclusion(supplier_id="sup_b", supplier_name="Shenzhen Print & Pack",
                                  code="moq_constraint", reason="minimum order 3,000 exceeds it")])

    def test_a_faithful_sentence_is_kept(self):
        text, status = guard_explanation(
            "Anhui Packaging Co is lowest on LINE-001 at 0.42 across 7 lines.", self.result)
        self.assertEqual(status, "ok")
        self.assertTrue(text)

    def test_a_line_number_written_plainly_is_still_recognised(self):
        text, status = guard_explanation("Anhui Packaging Co is lowest on Line 1.", self.result)
        self.assertEqual(status, "ok")

    def test_a_figure_the_result_does_not_contain_is_rejected(self):
        text, status = guard_explanation("Anhui Packaging Co is lowest at 0.39.", self.result)
        self.assertIsNone(text)
        self.assertIn("0.39", status)

    def test_award_language_is_rejected_even_when_the_numbers_are_right(self):
        for phrase in ("I recommend Anhui Packaging Co.",
                       "You should choose Anhui Packaging Co.",
                       "Award the business to Anhui Packaging Co."):
            text, status = guard_explanation(phrase, self.result)
            self.assertIsNone(text, phrase)
            self.assertIn("award language", status)

    def test_an_empty_or_overlong_narration_is_rejected(self):
        self.assertIsNone(guard_explanation("", self.result)[0])
        self.assertIsNone(guard_explanation("word " * 400, self.result)[0])


if __name__ == "__main__":
    unittest.main()
