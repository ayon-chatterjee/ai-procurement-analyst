"""The deterministic half of the analyst.

These tests are about what the application is willing to assert. The recurring theme is
that a gap is not a zero, a claim is not a proof, and a number that cannot be compared
safely is reported as such rather than quietly dropped or quietly converted.
"""
from __future__ import annotations

import datetime as _dt
import json
import unittest

from rfq_copilot.analyst_calculations import (
    CALCULATIONS, assess_supplier, build_context, choose_comparison_currency, price_check,
)
from rfq_copilot.analyst_models import (
    AnalystQuery, EvidenceTarget, EvidenceTopic, Filter, Hypothetical, Intent,
    QualificationStatus,
)
from rfq_copilot.fx import RateTable
from rfq_copilot.supplier_models import (
    ClaimStatus, MatchStatus, PriceBasis, QuoteStatus, ResponseType,
)
from rfq_copilot.supplier_service import CellState
from tests.analyst_helpers import (
    bundle, evidence_row, matrix, quote, query, rate_table, silent,
)
from tests.supplier_helpers import carton_rfq


def run(intent, rfq, bundles, *, extra=None, currency=None, display=None, today=None, **kw):
    """Build the dataset, choose the currency the way the service does, and calculate."""
    m = matrix(rfq, bundles, extra_suppliers=extra or [], display_currency=display)
    q = query(intent, **kw)
    if currency:
        q.comparison_currency = currency
    ccy, reason = choose_comparison_currency(m, q, None)
    ctx = build_context(m, q, ccy, reason, today=today)
    return CALCULATIONS[q.intent](ctx)


def codes(result):
    return sorted({e.code for e in result.exclusions})


def cell_for(result, line_id, column):
    row = next(r for r in result.rows if r.get("Line") == line_id)
    return row[column]


class CheapestByLineTest(unittest.TestCase):
    def setUp(self):
        self.rfq = carton_rfq()

    def test_the_lowest_comparable_price_wins_each_line(self):
        a, ba = bundle(self.rfq, "Alpha Cartons", [quote(self.rfq, "LINE-001", 0.42)])
        b, bb = bundle(self.rfq, "Beta Boxes", [quote(self.rfq, "LINE-001", 0.39)])
        result = run(Intent.CHEAPEST_BY_LINE, self.rfq, [ba, bb])
        self.assertEqual(cell_for(result, "LINE-001", "Cheapest supplier"), "Beta Boxes")
        self.assertEqual(cell_for(result, "LINE-001", "Price"), 0.39)

    def test_an_exact_tie_names_both_and_picks_neither(self):
        _, ba = bundle(self.rfq, "Alpha Cartons", [quote(self.rfq, "LINE-001", 0.42)])
        _, bb = bundle(self.rfq, "Beta Boxes", [quote(self.rfq, "LINE-001", 0.42)])
        result = run(Intent.CHEAPEST_BY_LINE, self.rfq, [ba, bb])
        winner = cell_for(result, "LINE-001", "Cheapest supplier")
        self.assertIn("Alpha Cartons", winner)
        self.assertIn("Beta Boxes", winner)
        self.assertIn("tie", cell_for(result, "LINE-001", "Notes"))

    def test_a_missing_quote_is_never_treated_as_zero(self):
        _, ba = bundle(self.rfq, "Alpha Cartons", [quote(self.rfq, "LINE-001", 0.42)])
        _, bb = bundle(self.rfq, "Beta Boxes", [quote(self.rfq, "LINE-002", 0.10)])
        result = run(Intent.CHEAPEST_BY_LINE, self.rfq, [ba, bb])
        self.assertEqual(cell_for(result, "LINE-001", "Cheapest supplier"), "Alpha Cartons")
        self.assertEqual(cell_for(result, "LINE-001", "Price"), 0.42)
        self.assertIn("not_quoted", codes(result))

    def test_a_line_nobody_could_quote_says_so_rather_than_showing_a_number(self):
        _, ba = bundle(self.rfq, "Alpha Cartons", [quote(self.rfq, "LINE-001", 0.42)])
        result = run(Intent.CHEAPEST_BY_LINE, self.rfq, [ba])
        self.assertEqual(cell_for(result, "LINE-002", "Cheapest supplier"), "no comparable quote")
        self.assertEqual(cell_for(result, "LINE-002", "Price"), "—")

    def test_an_unresolved_price_basis_is_excluded_with_its_note(self):
        _, ba = bundle(self.rfq, "Alpha Cartons", [quote(self.rfq, "LINE-001", 0.42)])
        _, bb = bundle(self.rfq, "Beta Boxes",
                       [quote(self.rfq, "LINE-001", 2.35, basis=PriceBasis.PER_KG)])
        result = run(Intent.CHEAPEST_BY_LINE, self.rfq, [ba, bb])
        self.assertEqual(cell_for(result, "LINE-001", "Cheapest supplier"), "Alpha Cartons")
        self.assertIn("unresolved_price", codes(result))
        reason = next(e.reason for e in result.exclusions if e.code == "unresolved_price")
        self.assertTrue(reason, "an exclusion always carries a reason")

    def test_an_unnamed_currency_is_excluded_not_guessed(self):
        _, ba = bundle(self.rfq, "Alpha Cartons", [quote(self.rfq, "LINE-001", 0.42)])
        _, bb = bundle(self.rfq, "Beta Boxes",
                       [quote(self.rfq, "LINE-001", 38.0, currency="CENTS")])
        result = run(Intent.CHEAPEST_BY_LINE, self.rfq, [ba, bb])
        self.assertEqual(cell_for(result, "LINE-001", "Cheapest supplier"), "Alpha Cartons")
        self.assertTrue({"unnamed_currency", "unresolved_price"} & set(codes(result)))

    def test_an_unconfirmed_line_match_is_excluded_until_it_is_confirmed(self):
        _, ba = bundle(self.rfq, "Alpha Cartons", [quote(self.rfq, "LINE-001", 0.42)])
        _, bb = bundle(self.rfq, "Beta Boxes",
                       [quote(self.rfq, "LINE-001", 0.30, match=MatchStatus.PROBABLE_MATCH)])
        result = run(Intent.CHEAPEST_BY_LINE, self.rfq, [ba, bb])
        self.assertEqual(cell_for(result, "LINE-001", "Cheapest supplier"), "Alpha Cartons")
        self.assertIn("unconfirmed_match", codes(result))

    def test_accepting_unconfirmed_matches_is_a_labelled_what_if(self):
        _, ba = bundle(self.rfq, "Alpha Cartons", [quote(self.rfq, "LINE-001", 0.42)])
        _, bb = bundle(self.rfq, "Beta Boxes",
                       [quote(self.rfq, "LINE-001", 0.30, match=MatchStatus.PROBABLE_MATCH)])
        result = run(Intent.CHEAPEST_BY_LINE, self.rfq, [ba, bb],
                     hypothetical=Hypothetical(include_probable_matches=True))
        self.assertEqual(cell_for(result, "LINE-001", "Cheapest supplier"), "Beta Boxes")
        self.assertTrue(result.hypothetical)
        self.assertTrue(result.hypothetical_labels)
        self.assertTrue(result.summary.startswith("What-if"))

    def test_a_minimum_order_above_the_line_quantity_makes_a_quote_unusable(self):
        _, ba = bundle(self.rfq, "Alpha Cartons", [quote(self.rfq, "LINE-001", 0.42)])
        _, bb = bundle(self.rfq, "Beta Boxes", [quote(self.rfq, "LINE-001", 0.30, moq=5000)])
        result = run(Intent.CHEAPEST_BY_LINE, self.rfq, [ba, bb])
        self.assertEqual(cell_for(result, "LINE-001", "Cheapest supplier"), "Alpha Cartons")
        self.assertIn("moq_constraint", codes(result))

    def test_ignoring_minimum_orders_is_a_labelled_what_if(self):
        _, ba = bundle(self.rfq, "Alpha Cartons", [quote(self.rfq, "LINE-001", 0.42)])
        _, bb = bundle(self.rfq, "Beta Boxes", [quote(self.rfq, "LINE-001", 0.30, moq=5000)])
        result = run(Intent.CHEAPEST_BY_LINE, self.rfq, [ba, bb],
                     hypothetical=Hypothetical(ignore_moq_constraints=True))
        self.assertEqual(cell_for(result, "LINE-001", "Cheapest supplier"), "Beta Boxes")
        self.assertTrue(result.hypothetical)

    def test_a_contradiction_about_the_price_excludes_it(self):
        conflict = [{"topic": "Unit price", "description": "two prices",
                     "values": [{"value": "0.30"}, {"value": "0.45"}]}]
        _, ba = bundle(self.rfq, "Alpha Cartons", [quote(self.rfq, "LINE-001", 0.42)])
        _, bb = bundle(self.rfq, "Beta Boxes",
                       [quote(self.rfq, "LINE-001", 0.30, status=QuoteStatus.CONFLICT,
                              conflicts=conflict)])
        result = run(Intent.CHEAPEST_BY_LINE, self.rfq, [ba, bb])
        self.assertEqual(cell_for(result, "LINE-001", "Cheapest supplier"), "Alpha Cartons")
        self.assertIn("price_conflict", codes(result))

    def test_a_contradiction_about_lead_time_keeps_the_price_but_says_so(self):
        conflict = [{"topic": "Production lead time", "description": "two lead times",
                     "values": [{"value": "18 days"}, {"value": "30 days"}]}]
        _, ba = bundle(self.rfq, "Alpha Cartons", [quote(self.rfq, "LINE-001", 0.42)])
        _, bb = bundle(self.rfq, "Beta Boxes",
                       [quote(self.rfq, "LINE-001", 0.30, status=QuoteStatus.CONFLICT,
                              conflicts=conflict)])
        result = run(Intent.CHEAPEST_BY_LINE, self.rfq, [ba, bb])
        self.assertEqual(cell_for(result, "LINE-001", "Cheapest supplier"), "Beta Boxes")
        self.assertIn("contradiction", cell_for(result, "LINE-001", "Notes"))

    def test_a_basket_total_is_only_given_when_every_line_has_an_answer(self):
        rfq = carton_rfq(sizes=["10 x 10 x 5", "12 x 10 x 6"])
        _, partial = bundle(rfq, "Alpha Cartons", [quote(rfq, "LINE-001", 0.42)])
        self.assertNotIn("basket_at_cheapest",
                         run(Intent.CHEAPEST_BY_LINE, rfq, [partial]).metrics)
        _, full = bundle(rfq, "Beta Boxes",
                         [quote(rfq, "LINE-001", 0.50), quote(rfq, "LINE-002", 0.60)])
        result = run(Intent.CHEAPEST_BY_LINE, rfq, [partial, full])
        self.assertEqual(result.metrics["basket_at_cheapest"], round(0.42 * 2000 + 0.60 * 2000, 2))

    def test_the_answer_never_recommends_or_awards(self):
        _, ba = bundle(self.rfq, "Alpha Cartons", [quote(self.rfq, "LINE-001", 0.42)])
        result = run(Intent.CHEAPEST_BY_LINE, self.rfq, [ba])
        for word in ("award", "recommend", "you should"):
            self.assertNotIn(word, result.summary.lower())


class CurrencyTest(unittest.TestCase):
    def setUp(self):
        self.rfq = carton_rfq()

    def test_the_rfq_currency_is_used_and_named_as_an_assumption(self):
        self.rfq.fields["currency"].value = "USD"
        from rfq_copilot.schema import FieldStatus
        self.rfq.fields["currency"].status = FieldStatus.PROVIDED
        _, ba = bundle(self.rfq, "Alpha Cartons", [quote(self.rfq, "LINE-001", 0.42)])
        result = run(Intent.CHEAPEST_BY_LINE, self.rfq, [ba])
        self.assertEqual(result.comparison_currency, "USD")
        self.assertTrue(any("USD" in a and "RFQ" in a for a in result.assumptions))

    def test_the_commonest_quote_currency_is_used_when_the_rfq_names_none(self):
        _, ba = bundle(self.rfq, "Alpha Cartons",
                       [quote(self.rfq, "LINE-001", 0.42, currency="EUR"),
                        quote(self.rfq, "LINE-002", 0.48, currency="EUR")])
        result = run(Intent.CHEAPEST_BY_LINE, self.rfq, [ba])
        self.assertEqual(result.comparison_currency, "EUR")

    def test_a_converted_price_names_the_rate_its_provider_and_its_date(self):
        _, ba = bundle(self.rfq, "Alpha Cartons", [quote(self.rfq, "LINE-001", 0.42)])
        _, bb = bundle(self.rfq, "Beta Boxes",
                       [quote(self.rfq, "LINE-001", 0.30, currency="EUR")])
        m = matrix(self.rfq, [ba, bb], display_currency="USD")
        q = query(Intent.CHEAPEST_BY_LINE, comparison_currency="USD")
        ctx = build_context(m, q, "USD", "the currency you asked for")
        result = CALCULATIONS[q.intent](ctx)
        pairs = result.rate_provenance["pairs"]
        self.assertTrue(pairs, "a converted figure must cite its rate")
        self.assertIn("test-provider", pairs[0])
        self.assertIn("2026-09-11", pairs[0])

    def test_a_quote_with_no_rate_is_excluded_rather_than_converted(self):
        _, ba = bundle(self.rfq, "Alpha Cartons", [quote(self.rfq, "LINE-001", 0.42)])
        _, bb = bundle(self.rfq, "Beta Boxes",
                       [quote(self.rfq, "LINE-001", 0.10, currency="EUR")])
        empty = RateTable(base="USD", rates={"USD": 1.0}, source="none", error="offline")
        m = matrix(self.rfq, [ba, bb], display_currency="USD", rates=empty)
        q = query(Intent.CHEAPEST_BY_LINE, comparison_currency="USD")
        result = CALCULATIONS[q.intent](build_context(m, q, "USD", "the currency you asked for"))
        self.assertEqual(cell_for(result, "LINE-001", "Cheapest supplier"), "Alpha Cartons")
        self.assertIn("no_fx_rate", codes(result))

    def test_one_currency_needs_no_conversion_at_all(self):
        _, ba = bundle(self.rfq, "Alpha Cartons", [quote(self.rfq, "LINE-001", 0.42)])
        m = matrix(self.rfq, [ba])
        q = query(Intent.CHEAPEST_BY_LINE)
        result = CALCULATIONS[q.intent](build_context(m, q, "USD", "the currency most suppliers quoted in"))
        self.assertEqual(cell_for(result, "LINE-001", "Price"), 0.42)
        self.assertEqual(result.rate_provenance["pairs"], [])


class EligibilityTest(unittest.TestCase):
    def setUp(self):
        self.rfq = carton_rfq()

    def test_a_claimed_certification_is_not_a_verified_one(self):
        _, b = bundle(self.rfq, "Alpha Cartons", [quote(self.rfq, "LINE-001", 0.42)],
                      certs=[("ISO 9001", ClaimStatus.CLAIMED)],
                      answers=[("required_delivery_date", "18 days", ClaimStatus.CLAIMED)])
        qual = assess_supplier(b, self.rfq, Hypothetical())
        self.assertEqual(qual.status, QualificationStatus.UNVERIFIED.value)

    def test_a_document_backed_certification_clears_when_the_rfq_names_none(self):
        _, b = bundle(self.rfq, "Alpha Cartons", [quote(self.rfq, "LINE-001", 0.42)],
                      certs=[("ISO 9001", ClaimStatus.VERIFIED)],
                      answers=[("required_delivery_date", "18 days", ClaimStatus.CLAIMED)])
        self.assertEqual(assess_supplier(b, self.rfq, Hypothetical()).status,
                         QualificationStatus.CLEARED.value)

    def test_a_failed_or_expired_certification_never_clears_even_as_a_what_if(self):
        for status in (ClaimStatus.FAILED, ClaimStatus.EXPIRED):
            _, b = bundle(self.rfq, "Alpha Cartons", [quote(self.rfq, "LINE-001", 0.42)],
                          certs=[("ISO 9001", status)],
                          answers=[("required_delivery_date", "18 days", ClaimStatus.CLAIMED)])
            lenient = Hypothetical(treat_claimed_as_verified=True)
            self.assertEqual(assess_supplier(b, self.rfq, lenient).status,
                             QualificationStatus.NOT_CLEARED.value)

    def test_treating_a_claim_as_proof_is_marked_on_the_check_that_was_promoted(self):
        _, b = bundle(self.rfq, "Alpha Cartons", [quote(self.rfq, "LINE-001", 0.42)],
                      certs=[("ISO 9001", ClaimStatus.CLAIMED)],
                      answers=[("required_delivery_date", "18 days", ClaimStatus.CLAIMED)])
        qual = assess_supplier(b, self.rfq, Hypothetical(treat_claimed_as_verified=True))
        self.assertEqual(qual.status, QualificationStatus.CLEARED.value)
        promoted = [c for c in qual.checks if c.counted_by_hypothesis]
        self.assertEqual([c.name for c in promoted], ["ISO 9001"])

    def test_a_required_question_left_unanswered_blocks_clearance(self):
        _, b = bundle(self.rfq, "Alpha Cartons", [quote(self.rfq, "LINE-001", 0.42)],
                      certs=[("ISO 9001", ClaimStatus.VERIFIED)])
        qual = assess_supplier(b, self.rfq, Hypothetical())
        self.assertEqual(qual.status, QualificationStatus.NOT_CLEARED.value)
        self.assertTrue(any("required_delivery_date" in r for r in qual.reasons))

    def test_a_named_requirement_must_be_matched_exactly(self):
        from rfq_copilot.schema import FieldStatus
        self.rfq.fields["certifications"].value = ["ISO 9001"]
        self.rfq.fields["certifications"].status = FieldStatus.PROVIDED
        _, vague = bundle(self.rfq, "Alpha Cartons", [quote(self.rfq, "LINE-001", 0.42)],
                          certs=[("ISO", ClaimStatus.VERIFIED)],
                          answers=[("required_delivery_date", "18 days", ClaimStatus.CLAIMED)])
        self.assertEqual(assess_supplier(vague, self.rfq, Hypothetical()).status,
                         QualificationStatus.NOT_CLEARED.value)

    def test_a_supplier_who_never_replied_is_not_assessed_rather_than_failed(self):
        qual = assess_supplier(None, self.rfq, Hypothetical(), silent())
        self.assertEqual(qual.status, QualificationStatus.NOT_ASSESSED.value)

    def test_filtering_to_cleared_suppliers_explains_who_it_removed(self):
        _, good = bundle(self.rfq, "Alpha Cartons", [quote(self.rfq, "LINE-001", 0.42)],
                         certs=[("ISO 9001", ClaimStatus.VERIFIED)],
                         answers=[("required_delivery_date", "18 days", ClaimStatus.CLAIMED)])
        _, weak = bundle(self.rfq, "Beta Boxes", [quote(self.rfq, "LINE-001", 0.30)],
                         certs=[("ISO 9001", ClaimStatus.CLAIMED)],
                         answers=[("required_delivery_date", "20 days", ClaimStatus.CLAIMED)])
        result = run(Intent.CHEAPEST_BY_LINE, self.rfq, [good, weak],
                     filters=[Filter("eligibility", "is", ["cleared"])])
        self.assertEqual(cell_for(result, "LINE-001", "Cheapest supplier"), "Alpha Cartons")
        removed = [e for e in result.exclusions if e.supplier_name == "Beta Boxes"]
        self.assertTrue(removed, "a filtered-out supplier is named, not silently dropped")
        self.assertIn("qualification", removed[0].reason)

    def test_the_qualification_rule_is_always_stated(self):
        _, b = bundle(self.rfq, "Alpha Cartons", [quote(self.rfq, "LINE-001", 0.42)],
                      certs=[("ISO 9001", ClaimStatus.CLAIMED)])
        result = run(Intent.QUALIFICATION_STATUS, self.rfq, [b])
        joined = " ".join(result.assumptions)
        self.assertIn("document", joined)
        self.assertIn("claim", joined)


class CoverageTest(unittest.TestCase):
    def setUp(self):
        self.rfq = carton_rfq(sizes=["10 x 10 x 5", "12 x 10 x 6", "15 x 10 x 8", "18 x 12 x 10"])

    def test_coverage_is_counted_against_the_lines_in_scope(self):
        _, b = bundle(self.rfq, "Alpha Cartons",
                      [quote(self.rfq, "LINE-001", 0.42), quote(self.rfq, "LINE-002", 0.48)])
        result = run(Intent.SUPPLIER_COVERAGE, self.rfq, [b])
        row = next(r for r in result.rows if r["Supplier"] == "Alpha Cartons")
        self.assertEqual(row["Comparable lines"], 2)
        self.assertEqual(row["Coverage %"], 50.0)

    def test_a_supplier_who_never_replied_has_no_coverage_and_a_reason(self):
        _, b = bundle(self.rfq, "Alpha Cartons", [quote(self.rfq, "LINE-001", 0.42)])
        result = run(Intent.SUPPLIER_COVERAGE, self.rfq, [b], extra=[silent()])
        row = next(r for r in result.rows if r["Supplier"] == "Pacific Carton Works")
        self.assertEqual(row["Comparable lines"], 0)
        self.assertEqual(row["Status"], "no response")

    def test_line_coverage_says_who_is_missing_and_why(self):
        _, ba = bundle(self.rfq, "Alpha Cartons", [quote(self.rfq, "LINE-001", 0.42)])
        _, bb = bundle(self.rfq, "Beta Boxes", [quote(self.rfq, "LINE-002", 0.48)])
        result = run(Intent.LINE_COVERAGE, self.rfq, [ba, bb])
        missing = cell_for(result, "LINE-001", "Missing from")
        self.assertIn("Beta Boxes", missing)
        self.assertIn("did not price", missing)

    def test_gaps_are_typed_and_never_numeric(self):
        _, ba = bundle(self.rfq, "Alpha Cartons", [quote(self.rfq, "LINE-001", 0.42)])
        result = run(Intent.MISSING_QUOTES, self.rfq, [ba], extra=[silent()])
        kinds = {r["Kind"] for r in result.rows}
        self.assertIn("not quoted", kinds)
        self.assertIn("no response", kinds)
        for row in result.rows:
            self.assertNotIn(row["Kind"], ("0", 0))


class LeadTimeTest(unittest.TestCase):
    def setUp(self):
        self.rfq = carton_rfq()

    def test_a_range_is_flagged_as_an_interpretation(self):
        _, b = bundle(self.rfq, "Alpha Cartons",
                      [quote(self.rfq, "LINE-001", 0.42, lead="20-25 days", lead_days=22.5,
                             lead_interpreted=True)])
        result = run(Intent.LEAD_TIME_COMPARISON, self.rfq, [b])
        row = next(r for r in result.rows if r["Supplier"] == "Alpha Cartons")
        self.assertEqual(row["How read"], "midpoint of a range")
        self.assertEqual(result.metrics["interpreted_count"], 1)

    def test_a_contradicted_lead_time_is_shown_both_ways_and_not_ranked(self):
        conflict = [{"topic": "Production lead time", "description": "two",
                     "values": [{"value": "18 days"}, {"value": "30 days"}]}]
        _, slow = bundle(self.rfq, "Alpha Cartons",
                         [quote(self.rfq, "LINE-001", 0.42, lead="25 days", lead_days=25.0)])
        _, mixed = bundle(self.rfq, "Beta Boxes",
                          [quote(self.rfq, "LINE-001", 0.30, lead="18 days", lead_days=18.0,
                                 status=QuoteStatus.CONFLICT, conflicts=conflict)])
        result = run(Intent.LEAD_TIME_COMPARISON, self.rfq, [slow, mixed])
        self.assertEqual(result.metrics["shortest"], "Alpha Cartons")
        row = next(r for r in result.rows if r["Supplier"] == "Beta Boxes")
        self.assertIsNone(row["Days"])
        self.assertIn("18 days", row["Contradiction"])
        self.assertIn("30 days", row["Contradiction"])

    def test_different_starting_points_raise_a_comparability_warning(self):
        _, a = bundle(self.rfq, "Alpha Cartons",
                      [quote(self.rfq, "LINE-001", 0.42, lead="18 days after artwork approval",
                             lead_days=18.0)])
        _, b = bundle(self.rfq, "Beta Boxes",
                      [quote(self.rfq, "LINE-001", 0.30, lead="20 days from order confirmation",
                             lead_days=20.0)])
        result = run(Intent.LEAD_TIME_COMPARISON, self.rfq, [a, b])
        self.assertTrue(any("different starting points" in w for w in result.warnings))


class MoqTest(unittest.TestCase):
    def setUp(self):
        self.rfq = carton_rfq()

    def test_a_minimum_order_is_compared_with_the_line_quantity(self):
        _, b = bundle(self.rfq, "Alpha Cartons", [quote(self.rfq, "LINE-001", 0.42, moq=5000)])
        result = run(Intent.MOQ_CHECK, self.rfq, [b])
        row = next(r for r in result.rows if r["Line"] == "LINE-001")
        self.assertEqual(row["Fits"], "no")
        self.assertEqual(row["Shortfall"], 3000.0)

    def test_an_unstated_minimum_order_is_not_assumed_to_fit(self):
        _, b = bundle(self.rfq, "Alpha Cartons", [quote(self.rfq, "LINE-001", 0.42)])
        result = run(Intent.MOQ_CHECK, self.rfq, [b])
        self.assertEqual(next(r for r in result.rows if r["Line"] == "LINE-001")["Fits"],
                         "MOQ not stated")


class ValidityTest(unittest.TestCase):
    def setUp(self):
        self.rfq = carton_rfq()

    def test_a_fixed_period_becomes_an_expiry_counted_from_the_received_date(self):
        _, b = bundle(self.rfq, "Alpha Cartons",
                      [quote(self.rfq, "LINE-001", 0.42, validity="30 days", validity_days=30.0)],
                      received_at="2026-09-01T00:00:00Z")
        result = run(Intent.QUOTE_VALIDITY, self.rfq, [b], today=_dt.date(2026, 9, 12))
        row = result.rows[0]
        self.assertEqual(row["Expires"], "2026-10-01")
        self.assertIn("expires in 19 days", row["Status"])

    def test_a_conditional_validity_is_never_turned_into_a_date(self):
        _, b = bundle(self.rfq, "Alpha Cartons",
                      [quote(self.rfq, "LINE-001", 0.42,
                             validity="valid subject to kraft paper prices",
                             validity_conditional=True)])
        result = run(Intent.QUOTE_VALIDITY, self.rfq, [b], today=_dt.date(2026, 9, 12))
        row = result.rows[0]
        self.assertEqual(row["Expires"], "—")
        self.assertEqual(row["Conditional"], "yes")

    def test_a_lapsed_quote_is_warned_about(self):
        _, b = bundle(self.rfq, "Alpha Cartons",
                      [quote(self.rfq, "LINE-001", 0.42, validity="15 days", validity_days=15.0)],
                      received_at="2026-08-01T00:00:00Z")
        result = run(Intent.QUOTE_VALIDITY, self.rfq, [b], today=_dt.date(2026, 9, 12))
        self.assertIn("expired", result.rows[0]["Status"])
        self.assertTrue(any("lapsed" in w for w in result.warnings))


class WhyExcludedTest(unittest.TestCase):
    def setUp(self):
        self.rfq = carton_rfq(sizes=["10 x 10 x 5", "12 x 10 x 6"])

    def test_the_reason_is_given_line_by_line(self):
        _, cheap = bundle(self.rfq, "Alpha Cartons",
                          [quote(self.rfq, "LINE-001", 0.42), quote(self.rfq, "LINE-002", 0.48)])
        supplier, dear = bundle(self.rfq, "Beta Boxes", [quote(self.rfq, "LINE-001", 0.55, moq=9000)])
        result = run(Intent.WHY_EXCLUDED, self.rfq, [cheap, dear],
                     subject_supplier_ids=[supplier.id])
        first = next(r for r in result.rows if r["Line"] == "LINE-001")
        self.assertEqual(first["Status"], "excluded")
        self.assertIn("minimum order", first["Reason"])
        second = next(r for r in result.rows if r["Line"] == "LINE-002")
        self.assertEqual(second["Status"], "excluded")
        self.assertIn("did not price", second["Reason"])

    def test_a_valid_but_dearer_quote_states_the_gap_to_the_cheapest(self):
        _, cheap = bundle(self.rfq, "Alpha Cartons", [quote(self.rfq, "LINE-001", 0.42)])
        supplier, dear = bundle(self.rfq, "Beta Boxes", [quote(self.rfq, "LINE-001", 0.55)])
        result = run(Intent.WHY_EXCLUDED, self.rfq, [cheap, dear],
                     subject_supplier_ids=[supplier.id])
        row = next(r for r in result.rows if r["Line"] == "LINE-001")
        self.assertEqual(row["Status"], "valid, not cheapest")
        self.assertIn("Alpha Cartons", row["Reason"])
        self.assertEqual(row["Gap"], 0.13)

    def test_a_supplier_that_was_not_excluded_is_said_not_to_have_been(self):
        supplier, best = bundle(self.rfq, "Alpha Cartons",
                                [quote(self.rfq, "LINE-001", 0.42), quote(self.rfq, "LINE-002", 0.48)])
        result = run(Intent.WHY_EXCLUDED, self.rfq, [best], subject_supplier_ids=[supplier.id])
        self.assertIn("not excluded", result.summary)


class EvidenceLookupTest(unittest.TestCase):
    def setUp(self):
        self.rfq = carton_rfq()

    def test_a_price_lookup_returns_the_stored_evidence_record(self):
        ev = evidence_row("ev_1", "Size 1 | 2000 pcs | USD 0.42", "Sheet Quotation · row 6")
        supplier, b = bundle(self.rfq, "Alpha Cartons",
                             [quote(self.rfq, "LINE-001", 0.42, evidence_ids=["ev_1"])],
                             evidence={"ev_1": ev})
        result = run(Intent.EVIDENCE_LOOKUP, self.rfq, [b],
                     evidence_target=EvidenceTarget(supplier=supplier.id, line="LINE-001",
                                                    topic=EvidenceTopic.PRICE.value))
        row = result.rows[0]
        self.assertEqual(row["Quoted text"], "Size 1 | 2000 pcs | USD 0.42")
        self.assertIn("row 6", row["Location"])
        self.assertEqual(row["Found in document"], "yes")

    def test_a_term_with_no_recorded_span_is_reported_not_invented(self):
        supplier, b = bundle(self.rfq, "Alpha Cartons",
                             [quote(self.rfq, "LINE-001", 0.42, lead="18 days from artwork approval")])
        result = run(Intent.EVIDENCE_LOOKUP, self.rfq, [b],
                     evidence_target=EvidenceTarget(supplier=supplier.id,
                                                    topic=EvidenceTopic.LEAD_TIME.value))
        row = result.rows[0]
        self.assertEqual(row["Location"], "—")
        self.assertEqual(row["Found in document"], "no span recorded")
        self.assertTrue(any("was not recorded" in w for w in result.warnings))

    def test_a_certification_lookup_says_a_claim_is_only_a_claim(self):
        supplier, b = bundle(self.rfq, "Alpha Cartons", [quote(self.rfq, "LINE-001", 0.42)],
                             certs=[("ISO 9001", ClaimStatus.CLAIMED)])
        result = run(Intent.EVIDENCE_LOOKUP, self.rfq, [b],
                     evidence_target=EvidenceTarget(supplier=supplier.id,
                                                    topic=EvidenceTopic.CERTIFICATION.value,
                                                    name="ISO 9001"))
        self.assertIn("claimed", result.summary)
        self.assertTrue(any("claim" in w for w in result.warnings))


class LookupTest(unittest.TestCase):
    """The general reader, for questions no specialised calculation covers."""

    def setUp(self):
        self.rfq = carton_rfq()
        _, self.a = bundle(self.rfq, "Alpha Cartons",
                           [quote(self.rfq, "LINE-001", 0.42, payment="30% advance, 70% B/L",
                                  delivery="FOB Shanghai")], country="China")
        _, self.b = bundle(self.rfq, "Beta Boxes",
                           [quote(self.rfq, "LINE-001", 0.30, payment="letter of credit",
                                  delivery="CIF Mumbai")], country="India")

    def test_a_text_filter_finds_the_supplier_and_shows_their_own_wording(self):
        result = run(Intent.LOOKUP, self.rfq, [self.a, self.b],
                     fields=["supplier", "delivery_terms"], grain="supplier",
                     filters=[Filter("delivery_terms", "contains", ["FOB"])])
        self.assertEqual([r["Supplier"] for r in result.rows], ["Alpha Cartons"])
        self.assertEqual(result.rows[0]["Matched on"], "FOB Shanghai")

    def test_a_response_level_term_appears_once_per_supplier(self):
        result = run(Intent.LOOKUP, self.rfq, [self.a],
                     fields=["supplier", "payment_terms"], grain="supplier")
        self.assertEqual(len(result.rows), 1)
        self.assertEqual(result.rows[0]["Payment terms"], "30% advance, 70% B/L")

    def test_a_price_lookup_never_shows_a_figure_the_rules_refused(self):
        _, bad = bundle(self.rfq, "Gamma Pack",
                        [quote(self.rfq, "LINE-001", 2.35, basis=PriceBasis.PER_KG)])
        result = run(Intent.LOOKUP, self.rfq, [self.a, bad],
                     fields=["supplier", "line", "price"], grain="quote")
        gamma = [r for r in result.rows if r["Supplier"] == "Gamma Pack"]
        self.assertTrue(gamma)
        self.assertTrue(all(r["Price"] == "—" for r in gamma))

    def test_rows_can_be_sorted_by_a_shown_field(self):
        result = run(Intent.LOOKUP, self.rfq, [self.a, self.b],
                     fields=["supplier", "country"], grain="supplier",
                     sort_by="country", descending=True)
        self.assertEqual([r["Country"] for r in result.rows], ["India", "China"])

    def test_nothing_matching_says_so_rather_than_returning_an_empty_table(self):
        result = run(Intent.LOOKUP, self.rfq, [self.a, self.b],
                     fields=["supplier", "payment_terms"], grain="supplier",
                     filters=[Filter("payment_terms", "contains", ["bitcoin"])])
        self.assertEqual(result.rows, [])
        self.assertIn("Nothing in this RFQ matches", result.summary)


class UnresolvedIssuesTest(unittest.TestCase):
    def setUp(self):
        self.rfq = carton_rfq(sizes=["10 x 10 x 5", "12 x 10 x 6"])

    def test_it_gathers_review_items_alongside_gaps_and_constraints(self):
        _, b = bundle(self.rfq, "Alpha Cartons",
                      [quote(self.rfq, "LINE-001", 0.42, moq=9000)],
                      certs=[("ISO 9001", ClaimStatus.CLAIMED)],
                      questions=["Can you confirm the artwork deadline?"])
        result = run(Intent.UNRESOLVED_ISSUES, self.rfq, [b], extra=[silent()])
        kinds = {r["Kind"] for r in result.rows}
        self.assertIn("unverified claim", kinds)
        self.assertIn("moq constraint", kinds)
        self.assertIn("lines not quoted", kinds)
        self.assertIn("no response", kinds)

    def test_blocking_issues_come_first(self):
        _, b = bundle(self.rfq, "Alpha Cartons",
                      [quote(self.rfq, "LINE-001", 2.35, basis=PriceBasis.PER_KG,
                             status=QuoteStatus.UNRESOLVED)],
                      certs=[("ISO 9001", ClaimStatus.CLAIMED)])
        result = run(Intent.UNRESOLVED_ISSUES, self.rfq, [b])
        self.assertEqual(result.rows[0]["Blocks comparison"], "yes")
        self.assertGreaterEqual(result.metrics["blocking_total"], 1)


class RfqCompletenessTest(unittest.TestCase):
    def test_the_percentage_carries_its_numerator_denominator_and_definition(self):
        rfq = carton_rfq(sizes=["10 x 10 x 5", "12 x 10 x 6", "15 x 10 x 8", "18 x 12 x 10"])
        _, b = bundle(rfq, "Alpha Cartons",
                      [quote(rfq, "LINE-001", 0.42), quote(rfq, "LINE-002", 0.48),
                       quote(rfq, "LINE-003", 0.55)])
        result = run(Intent.RFQ_COMPLETENESS, rfq, [b])
        self.assertEqual(result.metrics["numerator"], 3)
        self.assertEqual(result.metrics["denominator"], 4)
        self.assertEqual(result.metrics["percent_with_valid_quote"], 75.0)
        self.assertIn("75.0%", result.summary)
        self.assertIn("A quote counts when", result.summary)


class ResultShapeTest(unittest.TestCase):
    def setUp(self):
        self.rfq = carton_rfq(sizes=["10 x 10 x 5", "12 x 10 x 6"])
        _, self.b = bundle(self.rfq, "Alpha Cartons", [quote(self.rfq, "LINE-001", 0.42)])

    def test_rows_survive_a_round_trip_through_json(self):
        result = run(Intent.CHEAPEST_BY_LINE, self.rfq, [self.b])
        self.assertEqual(json.loads(json.dumps(result.rows)), result.rows)

    def test_the_csv_export_carries_the_shown_columns(self):
        result = run(Intent.CHEAPEST_BY_LINE, self.rfq, [self.b])
        header = result.to_csv().splitlines()[0]
        self.assertEqual(header.split(",")[0], "Line")
        self.assertEqual(len(result.to_csv().splitlines()), len(result.rows) + 1)

    def test_every_answer_carries_its_calculation_steps(self):
        for intent in (Intent.CHEAPEST_BY_LINE, Intent.SUPPLIER_COVERAGE, Intent.MOQ_CHECK):
            result = run(intent, self.rfq, [self.b])
            self.assertTrue(result.calculation_notes, "%s has no stated method" % intent)
            self.assertTrue(any("supplier" in n for n in result.calculation_notes))

    def test_a_what_if_states_the_assumption_and_the_real_population(self):
        supplier, other = bundle(self.rfq, "Beta Boxes", [quote(self.rfq, "LINE-001", 0.30)])
        result = run(Intent.CHEAPEST_BY_LINE, self.rfq, [self.b, other],
                     hypothetical=Hypothetical(exclude_supplier_ids=[supplier.id]))
        self.assertTrue(result.hypothetical)
        self.assertIn("Beta Boxes", "; ".join(result.hypothetical_labels))
        self.assertTrue(any(e.hypothetical for e in result.exclusions))
        self.assertTrue(any("responded" in n for n in result.calculation_notes))

    def test_the_result_is_plain_data_the_session_can_hold(self):
        result = run(Intent.CHEAPEST_BY_LINE, self.rfq, [self.b])
        self.assertEqual(json.loads(json.dumps(result.to_dict()))["intent"],
                         Intent.CHEAPEST_BY_LINE.value)


if __name__ == "__main__":
    unittest.main()


class SameSourceOfTruthTest(unittest.TestCase):
    """The analyst and the comparison screen read one dataset, so they cannot disagree.

    Every price the analyst reports has to be the figure the comparison cell already
    holds. If these ever diverge, one of the two screens is lying to the buyer.
    """

    def setUp(self):
        self.rfq = carton_rfq(sizes=["10 x 10 x 5", "12 x 10 x 6", "15 x 10 x 8"])
        _, self.a = bundle(self.rfq, "Alpha Cartons",
                           [quote(self.rfq, "LINE-001", 0.42),
                            quote(self.rfq, "LINE-002", 0.55),
                            quote(self.rfq, "LINE-003", 2.35, basis=PriceBasis.PER_KG)])
        _, self.b = bundle(self.rfq, "Beta Boxes",
                           [quote(self.rfq, "LINE-001", 0.39, currency="EUR"),
                            quote(self.rfq, "LINE-002", 0.60, moq=9000)])

    def test_every_price_the_analyst_reports_is_the_comparison_cell_figure(self):
        m = matrix(self.rfq, [self.a, self.b], display_currency="USD")
        q = query(Intent.COMPARE_PRICES, comparison_currency="USD")
        result = CALCULATIONS[q.intent](build_context(m, q, "USD", "the currency you asked for"))
        for row in result.rows:
            for supplier in m.suppliers:
                shown = str(row[supplier.name]).replace(" †", "")
                cell = m.cell(row["Line"], supplier.id)
                if cell.quote is not None and cell.converted is not None:
                    self.assertEqual(shown, "USD %s" % _trimmed(cell.converted.amount),
                                     "%s on %s" % (supplier.name, row["Line"]))

    def test_a_cell_the_comparison_calls_unresolved_is_never_given_a_price(self):
        m = matrix(self.rfq, [self.a, self.b], display_currency="USD")
        q = query(Intent.CHEAPEST_BY_LINE, comparison_currency="USD")
        ctx = build_context(m, q, "USD", "the currency you asked for")
        for line in self.rfq.line_items:
            for supplier in m.suppliers:
                cell = m.cell(line.id, supplier.id)
                if cell.state in (CellState.UNRESOLVED, CellState.NOT_QUOTED,
                                  CellState.NO_RESPONSE):
                    self.assertIsNone(price_check(cell, ctx).amount,
                                      "%s %s is %s on the comparison screen"
                                      % (supplier.name, line.id, cell.state))

    def test_the_counts_agree_with_the_comparison_summary(self):
        m = matrix(self.rfq, [self.a, self.b], display_currency="USD")
        q = query(Intent.SUPPLIER_COVERAGE)
        result = CALCULATIONS[q.intent](build_context(m, q, "USD", "the currency you asked for"))
        for row in result.rows:
            supplier = next(s for s in m.suppliers if s.name == row["Supplier"])
            priced = sum(1 for line in self.rfq.line_items
                         if (m.cell(line.id, supplier.id).quote is not None
                             and m.cell(line.id, supplier.id).quote.has_price))
            self.assertEqual(row["Priced lines"], priced, row["Supplier"])


def _trimmed(value):
    text = ("%.4f" % value).rstrip("0").rstrip(".")
    return text


class QualificationIsAlwaysExplainedTest(unittest.TestCase):
    """If a quality rule removed somebody, the answer says what the rule was.

    Excluding suppliers on a test the buyer cannot see is the silent judgement this
    application exists to avoid, so the definition travels with any answer that applied
    one — not just with the answers about qualification.
    """

    def setUp(self):
        self.rfq = carton_rfq(sizes=["10 x 10 x 5"])
        _, self.good = bundle(self.rfq, "Alpha Cartons", [quote(self.rfq, "LINE-001", 0.42)],
                              certs=[("ISO 9001", ClaimStatus.VERIFIED)],
                              answers=[("required_delivery_date", "18 days", ClaimStatus.CLAIMED)])
        _, self.weak = bundle(self.rfq, "Beta Boxes", [quote(self.rfq, "LINE-001", 0.30)],
                              certs=[("ISO 9001", ClaimStatus.CLAIMED)],
                              answers=[("required_delivery_date", "20 days", ClaimStatus.CLAIMED)])

    def test_a_qa_filtered_price_answer_defines_what_cleared_means(self):
        result = run(Intent.CHEAPEST_BY_LINE, self.rfq, [self.good, self.weak],
                     filters=[Filter("eligibility", "is", ["cleared"])])
        joined = " ".join(result.assumptions)
        self.assertIn("document", joined)
        self.assertIn("claim", joined)

    def test_an_unfiltered_answer_is_not_cluttered_with_the_definition(self):
        result = run(Intent.CHEAPEST_BY_LINE, self.rfq, [self.good, self.weak])
        self.assertNotIn("claim", " ".join(result.assumptions))

    def test_the_lenient_what_if_says_what_it_promoted(self):
        result = run(Intent.CHEAPEST_BY_LINE, self.rfq, [self.good, self.weak],
                     hypothetical=Hypothetical(treat_claimed_as_verified=True))
        self.assertTrue(any("counts as if it were verified" in a for a in result.assumptions))
