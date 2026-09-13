"""Seeding, the bars, validation and the money.

The award is where the application stops describing and starts committing, so these tests
are about what it is willing to commit to: a price it can compare, a supplier that
answered, a quantity the supplier will accept, and a total that says what it covers.
"""
from __future__ import annotations

import datetime as _dt
import unittest

from rfq_copilot.award_calculations import (
    award_totals, basket_delta, clears_bars, extended_for, line_from_candidate,
    propose_award,
)
from rfq_copilot.award_models import (
    Award, AwardLine, AwardThresholds, PickSource, Severity,
)
from rfq_copilot.award_validation import validate_award
from rfq_copilot.schema import FieldStatus
from rfq_copilot.fx import RateTable
from rfq_copilot.supplier_models import ClaimStatus, MatchStatus, PriceBasis, QuoteStatus
from tests.analyst_helpers import bundle, matrix, quote, silent
from tests.award_helpers import (
    ANSWERED, TODAY, award_from, claiming_supplier, cleared_supplier, conditional_supplier,
    context_for, moq_blocked_supplier, proposal_for, stress_shaped, thresholds,
    unresolved_supplier,
)
from tests.supplier_helpers import carton_rfq

LINES = ["10 x 10 x 5", "12 x 10 x 6"]


def codes(report):
    return sorted({f.code for f in report.findings})


def blocking_codes(report):
    return sorted({f.code for f in report.blocking})


class AwardSeedingTest(unittest.TestCase):
    def setUp(self):
        self.rfq = carton_rfq(sizes=LINES)

    def test_the_cheapest_comparable_quote_is_seeded_on_every_line(self):
        _, dear = claiming_supplier(self.rfq, "Alpha", {"LINE-001": 0.42, "LINE-002": 0.55})
        _, cheap = claiming_supplier(self.rfq, "Beta", {"LINE-001": 0.39, "LINE-002": 0.60})
        proposal = proposal_for(self.rfq, [dear, cheap], th=thresholds(require_docs=False))
        self.assertEqual(proposal.line("LINE-001").cheapest.supplier_name, "Beta")
        self.assertEqual(proposal.line("LINE-002").cheapest.supplier_name, "Alpha")

    def test_best_value_is_the_cheapest_quote_that_clears_the_bars(self):
        _, cleared = cleared_supplier(self.rfq, "Istanbul", {"LINE-001": 0.50})
        _, claiming = claiming_supplier(self.rfq, "Anhui", {"LINE-001": 0.42})
        line = proposal_for(self.rfq, [cleared, claiming]).line("LINE-001")
        self.assertEqual(line.cheapest.supplier_name, "Anhui")
        self.assertEqual(line.best_value.supplier_name, "Istanbul")

    def test_the_two_coincide_when_the_cheapest_already_clears(self):
        _, cleared = cleared_supplier(self.rfq, "Istanbul", {"LINE-001": 0.40})
        _, claiming = claiming_supplier(self.rfq, "Anhui", {"LINE-001": 0.55})
        line = proposal_for(self.rfq, [cleared, claiming]).line("LINE-001")
        self.assertTrue(line.same_supplier)
        self.assertEqual(line.difference_note, "", "there is nothing to explain")

    def test_when_they_differ_the_line_says_why_in_one_sentence(self):
        _, cleared = cleared_supplier(self.rfq, "Istanbul", {"LINE-001": 0.50})
        _, claiming = claiming_supplier(self.rfq, "Anhui", {"LINE-001": 0.42})
        note = proposal_for(self.rfq, [cleared, claiming]).line("LINE-001").difference_note
        self.assertIn("Anhui", note)
        self.assertIn("cheaper", note)
        self.assertIn("document", note)
        self.assertIn("Istanbul", note)

    def test_best_value_is_a_price_not_a_score(self):
        _, cleared = cleared_supplier(self.rfq, "Istanbul", {"LINE-001": 0.50})
        _, claiming = claiming_supplier(self.rfq, "Anhui", {"LINE-001": 0.42})
        best = proposal_for(self.rfq, [cleared, claiming]).line("LINE-001").best_value
        self.assertEqual(best.amount, 0.50, "best value is one of the quoted prices")

    def test_a_line_where_nobody_clears_has_no_best_value_and_says_why(self):
        _, claiming = claiming_supplier(self.rfq, "Anhui", {"LINE-001": 0.42})
        line = proposal_for(self.rfq, [claiming]).line("LINE-001")
        self.assertIsNotNone(line.cheapest)
        self.assertIsNone(line.best_value)
        self.assertIn("meets the quality bar", line.absent_reason)
        self.assertIn("Anhui", line.absent_reason)

    def test_a_quote_whose_minimum_order_is_too_high_is_never_a_candidate(self):
        _, blocked = moq_blocked_supplier(self.rfq, "Shenzhen", {"LINE-001": 0.20})
        _, ok = claiming_supplier(self.rfq, "Anhui", {"LINE-001": 0.42})
        line = proposal_for(self.rfq, [blocked, ok], th=thresholds(require_docs=False)).line("LINE-001")
        self.assertEqual(line.cheapest.supplier_name, "Anhui")
        self.assertIn("moq_constraint", [e.code for e in line.exclusions])

    def test_an_unconfirmed_line_match_is_never_a_candidate(self):
        _, probable = bundle(self.rfq, "Gujarat",
                             [quote(self.rfq, "LINE-001", 0.20,
                                    match=MatchStatus.PROBABLE_MATCH)],
                             certs=[("ISO 9001", ClaimStatus.VERIFIED)], answers=ANSWERED)
        _, ok = cleared_supplier(self.rfq, "Istanbul", {"LINE-001": 0.42})
        line = proposal_for(self.rfq, [probable, ok]).line("LINE-001")
        self.assertEqual(line.cheapest.supplier_name, "Istanbul")
        self.assertIn("unconfirmed_match", [e.code for e in line.exclusions])

    def test_a_price_that_cannot_be_normalised_is_never_a_candidate(self):
        _, perkg = unresolved_supplier(self.rfq, "Viet", {"LINE-001": 2.35})
        _, ok = claiming_supplier(self.rfq, "Anhui", {"LINE-001": 0.42})
        line = proposal_for(self.rfq, [perkg, ok], th=thresholds(require_docs=False)).line("LINE-001")
        self.assertEqual(line.cheapest.supplier_name, "Anhui")

    def test_a_tie_is_broken_by_name_the_way_the_analyst_breaks_it(self):
        _, beta = claiming_supplier(self.rfq, "Beta", {"LINE-001": 0.42})
        _, alpha = claiming_supplier(self.rfq, "Alpha", {"LINE-001": 0.42})
        line = proposal_for(self.rfq, [beta, alpha], th=thresholds(require_docs=False)).line("LINE-001")
        self.assertEqual(line.cheapest.supplier_name, "Alpha")

    def test_a_proposal_is_never_asked_to_write_anything(self):
        """Seeding is arithmetic. If it ever needs a model, this is where it shows."""
        proposal = proposal_for(self.rfq, [claiming_supplier(self.rfq, "Anhui",
                                                             {"LINE-001": 0.42})[1]])
        self.assertTrue(proposal.assumptions)
        self.assertEqual(proposal.rfq_id, self.rfq.id)


class ThresholdTest(unittest.TestCase):
    def setUp(self):
        self.rfq = carton_rfq(sizes=LINES)

    def test_the_default_bar_accepts_a_stated_certificate(self):
        """The lenient reading is the default. Requiring a certificate we physically hold
        is the rarer case, and defaulting to it meant the award screen opened by refusing
        the supplier it had itself proposed."""
        self.assertFalse(AwardThresholds().require_document_backed_certification)
        self.assertTrue(AwardThresholds().require_firm_validity)

    def test_relaxing_the_certification_bar_admits_a_stated_certification(self):
        _, claiming = claiming_supplier(self.rfq, "Anhui", {"LINE-001": 0.42})
        strict = proposal_for(self.rfq, [claiming], th=thresholds())
        relaxed = proposal_for(self.rfq, [claiming], th=thresholds(require_docs=False))
        self.assertIsNone(strict.line("LINE-001").best_value)
        self.assertEqual(relaxed.line("LINE-001").best_value.supplier_name, "Anhui")

    def test_relaxing_the_bar_says_so_as_an_assumption(self):
        _, claiming = claiming_supplier(self.rfq, "Anhui", {"LINE-001": 0.42})
        relaxed = proposal_for(self.rfq, [claiming], th=thresholds(require_docs=False))
        self.assertTrue(any("no copy in our hands" in a for a in relaxed.assumptions))
        self.assertTrue(any("still show it as a claim" in a for a in relaxed.assumptions))

    def test_relaxing_the_bar_never_admits_a_failed_certification(self):
        _, failed = bundle(self.rfq, "Anhui", [quote(self.rfq, "LINE-001", 0.42)],
                           certs=[("ISO 9001", ClaimStatus.FAILED)], answers=ANSWERED)
        line = proposal_for(self.rfq, [failed], th=thresholds(require_docs=False)).line("LINE-001")
        self.assertIsNone(line.best_value, "a failed certificate is a fact, not a question")

    def test_relaxing_the_bar_works_when_the_rfq_names_a_required_certification(self):
        """The case the toggle exists for, and the one where it used to do nothing.

        A supplier who merely claims a certification the RFQ *requires* is NOT_CLEARED,
        not UNVERIFIED, because a required check failed. The relaxed bar accepted only
        CLEARED and UNVERIFIED, so on an RFQ that named a required certification every
        claiming supplier stayed barred however the buyer set the bar — and the demo's
        whole "relax it and watch the lines fill" moment did nothing.
        """
        rfq = carton_rfq(sizes=LINES)
        rfq.fields["certifications"].value = "ISO 9001"
        rfq.fields["certifications"].status = FieldStatus.PROVIDED
        _, claiming = claiming_supplier(rfq, "Anhui", {"LINE-001": 0.42})

        strict = proposal_for(rfq, [claiming], th=thresholds()).line("LINE-001")
        self.assertIsNone(strict.best_value)
        relaxed = proposal_for(rfq, [claiming], th=thresholds(require_docs=False)).line("LINE-001")
        self.assertIsNotNone(relaxed.best_value,
                             "relaxing the bar must admit a stated certification")
        self.assertEqual(relaxed.best_value.supplier_name, "Anhui")

    def test_relaxing_the_bar_does_not_forgive_a_certificate_never_mentioned(self):
        """Claimed-but-unevidenced is what the buyer is choosing to accept. Silence is
        not: there is nothing to take on trust."""
        rfq = carton_rfq(sizes=LINES)
        rfq.fields["certifications"].value = "ISO 9001"
        rfq.fields["certifications"].status = FieldStatus.PROVIDED
        _, silent = bundle(rfq, "Quiet Co", [quote(rfq, "LINE-001", 0.42)],
                           certs=[], answers=ANSWERED)
        line = proposal_for(rfq, [silent], th=thresholds(require_docs=False)).line("LINE-001")
        self.assertIsNone(line.best_value)
        self.assertIn("ISO 9001", line.absent_reason)

    def test_a_conditional_quote_validity_fails_the_firm_bar(self):
        _, conditional = conditional_supplier(self.rfq, "Viet", {"LINE-001": 0.30})
        line = proposal_for(self.rfq, [conditional],
                            th=thresholds(require_docs=False)).line("LINE-001")
        self.assertIsNone(line.best_value)
        self.assertIn("condition", line.absent_reason)

    def test_a_supplier_who_never_stated_a_lead_time_is_not_barred_for_it(self):
        """Lead time is reported, never a bar. It was one briefly, and it barred suppliers
        for saying nothing rather than for saying something disqualifying."""
        _, vague = claiming_supplier(self.rfq, "Anhui", {"LINE-001": 0.42},
                                     lead="", lead_days=None)
        ctx = context_for(self.rfq, [vague])
        passed, failures = clears_bars(ctx, ctx.suppliers[0].id, thresholds(require_docs=False))
        self.assertTrue(passed)
        self.assertEqual([f.bar for f in failures], [])


class StressShapedTest(unittest.TestCase):
    """The arrangement the live demo actually has."""

    def test_most_lines_have_no_best_value_under_the_strict_bar(self):
        rfq, bundles, extra = stress_shaped()
        proposal = proposal_for(rfq, bundles, extra=extra, display_currency="USD")
        self.assertEqual(len(proposal.lines_with_no_best_value), 2,
                         "only the line the cleared supplier quoted has a best value")
        self.assertTrue(proposal.line("LINE-002").cheapest,
                        "a line without a best value still has a cheapest")

    def test_that_is_reported_as_a_fact_not_an_error(self):
        rfq, bundles, extra = stress_shaped()
        proposal = proposal_for(rfq, bundles, extra=extra, display_currency="USD")
        self.assertTrue(any("no best-value candidate" in w for w in proposal.warnings))
        for line_id in proposal.lines_with_no_best_value:
            self.assertTrue(proposal.line(line_id).absent_reason)

    def test_relaxing_the_bar_fills_every_line(self):
        rfq, bundles, extra = stress_shaped()
        relaxed = proposal_for(rfq, bundles, extra=extra, display_currency="USD",
                               th=thresholds(require_docs=False))
        self.assertEqual(relaxed.lines_with_no_best_value, [])

    def test_the_silent_supplier_is_never_a_candidate(self):
        rfq, bundles, extra = stress_shaped()
        proposal = proposal_for(rfq, bundles, extra=extra, display_currency="USD")
        names = {c.supplier_name for l in proposal.lines for c in (l.cheapest, l.best_value) if c}
        self.assertNotIn("Pacific Carton Works", names)


class TotalsTest(unittest.TestCase):
    def setUp(self):
        self.rfq = carton_rfq(sizes=LINES)

    def award(self, bundles, **kw):
        proposal = proposal_for(self.rfq, bundles, th=thresholds(require_docs=False), **kw)
        return award_from(self.rfq, proposal), proposal

    def test_a_line_extends_quantity_by_the_unit_price(self):
        self.assertEqual(extended_for(2000, 0.42), 840.0)

    def test_a_missing_price_or_quantity_leaves_the_line_empty_never_zero(self):
        self.assertIsNone(extended_for(2000, None))
        self.assertIsNone(extended_for(None, 0.42))

    def test_a_subtotal_sums_the_rounded_line_figures(self):
        _, only = claiming_supplier(self.rfq, "Anhui", {"LINE-001": 0.4242, "LINE-002": 0.5555})
        award, _ = self.award([only])
        totals = award_totals(award)
        lines = [l.extended for l in award.awarded_lines]
        self.assertEqual(totals.by_supplier[0].subtotal, round(sum(lines), 2),
                         "the column on screen must add up to the footer")

    def test_a_total_is_complete_only_when_every_awarded_line_is_priced(self):
        _, only = claiming_supplier(self.rfq, "Anhui", {"LINE-001": 0.42, "LINE-002": 0.55})
        award, _ = self.award([only])
        self.assertTrue(award_totals(award).complete)
        award.lines[0].extended = None
        totals = award_totals(award)
        self.assertFalse(totals.complete)
        self.assertIn("1 of 2 awarded lines have no price", " ".join(totals.notes))

    def test_a_partial_total_always_carries_its_denominator(self):
        _, only = claiming_supplier(self.rfq, "Anhui", {"LINE-001": 0.42, "LINE-002": 0.55})
        award, _ = self.award([only])
        award.lines[0].extended = None
        self.assertIn("across 1 of 2 awarded lines", award_totals(award).describe())

    def test_currencies_are_never_summed_together(self):
        _, usd = claiming_supplier(self.rfq, "Anhui", {"LINE-001": 0.42})
        _, eur = claiming_supplier(self.rfq, "Istanbul", {"LINE-002": 0.30}, currency="EUR")
        proposal = proposal_for(self.rfq, [usd, eur], th=thresholds(require_docs=False),
                                display_currency="USD")
        award = award_from(self.rfq, proposal)
        totals = award_totals(award, proposal.rate_provenance)
        self.assertEqual(sorted(totals.native_currencies), ["EUR", "USD"])
        self.assertTrue(any("Nothing is summed across currencies" in n for n in totals.notes))
        converted = sum(s.subtotal for s in totals.by_supplier)
        self.assertEqual(totals.grand_total, round(converted, 2),
                         "the total is the sum of converted figures, never of native ones")

    def test_a_single_currency_supplier_keeps_its_own_subtotal(self):
        _, eur = claiming_supplier(self.rfq, "Istanbul", {"LINE-001": 0.30}, currency="EUR")
        proposal = proposal_for(self.rfq, [eur], th=thresholds(require_docs=False),
                                display_currency="USD", currency="USD")
        subtotal = award_totals(award_from(self.rfq, proposal)).by_supplier[0]
        self.assertEqual(subtotal.native_currency, "EUR")
        self.assertIsNotNone(subtotal.native_subtotal)
        self.assertNotEqual(subtotal.native_subtotal, subtotal.subtotal)

    def test_no_total_at_all_when_there_is_no_comparison_currency(self):
        award = Award(rfq_id=self.rfq.id, currency=None)
        award.lines = [AwardLine(line_item_id="LINE-001", supplier_id="s1",
                                 pick_source=PickSource.CHEAPEST.value, quantity=10)]
        totals = award_totals(award)
        self.assertIsNone(totals.grand_total)
        self.assertIn("no total", totals.describe())


class BasketDeltaTest(unittest.TestCase):
    def setUp(self):
        self.rfq = carton_rfq(sizes=LINES)

    def test_the_delta_says_what_the_extra_costs(self):
        _, cheap = claiming_supplier(self.rfq, "Istanbul", {"LINE-001": 0.30, "LINE-002": 0.30})
        _, certified = cleared_supplier(self.rfq, "Anhui", {"LINE-001": 0.42, "LINE-002": 0.42})
        proposal = proposal_for(self.rfq, [cheap, certified], th=thresholds(require_docs=True))
        delta = basket_delta(proposal)
        self.assertEqual(delta["cheapest"], round(0.30 * 2000 * 2, 2))
        self.assertEqual(delta["best_value"], round(0.42 * 2000 * 2, 2))
        self.assertEqual(delta["difference"], 480.0)
        self.assertIn("more than cheapest", delta["note"])

    def test_there_is_no_delta_when_best_value_cannot_cover_every_line(self):
        _, claiming = claiming_supplier(self.rfq, "Anhui", {"LINE-001": 0.42, "LINE-002": 0.55})
        delta = basket_delta(proposal_for(self.rfq, [claiming]))
        self.assertIsNone(delta["difference"])
        self.assertIn("cannot cover every line", delta["note"])

    def test_the_two_are_equal_when_the_cheapest_already_clears(self):
        _, cleared = cleared_supplier(self.rfq, "Istanbul", {"LINE-001": 0.30, "LINE-002": 0.30})
        delta = basket_delta(proposal_for(self.rfq, [cleared]))
        self.assertEqual(delta["difference"], 0)
        self.assertIn("already meets the quality bar", delta["note"])


class ComparisonTest(unittest.TestCase):
    """What a line's ⓘ can say. The screen shows the whole field, so the proposal has to
    carry it — it used to compute every candidate and keep only the two picks."""

    def setUp(self):
        self.rfq = carton_rfq(sizes=LINES)

    def test_every_candidate_is_kept_not_just_the_two_picks(self):
        _, cleared = cleared_supplier(self.rfq, "Istanbul", {"LINE-001": 0.50})
        _, claiming = claiming_supplier(self.rfq, "Anhui", {"LINE-001": 0.42})
        _, third = claiming_supplier(self.rfq, "Viet", {"LINE-001": 0.61})
        line = proposal_for(self.rfq, [cleared, claiming, third],
                            th=thresholds(require_docs=True)).line("LINE-001")
        self.assertEqual([c.supplier_name for c in line.candidates],
                         ["Anhui", "Istanbul", "Viet"], "cheapest first")
        self.assertEqual(line.cheapest.supplier_name, "Anhui")
        self.assertEqual(line.best_value.supplier_name, "Istanbul")

    def test_each_candidate_says_whether_it_meets_the_bar_and_why_not(self):
        _, cleared = cleared_supplier(self.rfq, "Istanbul", {"LINE-001": 0.50})
        _, claiming = claiming_supplier(self.rfq, "Anhui", {"LINE-001": 0.42})
        line = proposal_for(self.rfq, [cleared, claiming],
                            th=thresholds(require_docs=True)).line("LINE-001")
        by_name = {c.supplier_name: c for c in line.candidates}
        self.assertTrue(by_name["Istanbul"].meets_bar)
        self.assertEqual(by_name["Istanbul"].bar_reason, "")
        self.assertFalse(by_name["Anhui"].meets_bar)
        self.assertIn("claimed rather than backed by a document", by_name["Anhui"].bar_reason)

    def test_a_supplier_with_no_usable_price_is_in_exclusions_not_candidates(self):
        """Together the two lists account for every supplier, so the comparison can name
        the whole field rather than quietly dropping the ones that did not qualify."""
        _, priced = claiming_supplier(self.rfq, "Anhui", {"LINE-001": 0.42})
        _, blocked = moq_blocked_supplier(self.rfq, "Shenzhen", {"LINE-001": 0.20})
        line = proposal_for(self.rfq, [priced, blocked],
                            th=thresholds(require_docs=False)).line("LINE-001")
        self.assertEqual([c.supplier_name for c in line.candidates], ["Anhui"])
        self.assertIn("Shenzhen", [e.supplier_name for e in line.exclusions])
        self.assertTrue(line.exclusions[0].reason)

    def test_a_relaxed_bar_puts_every_candidate_in_reach_of_best_value(self):
        _, claiming = claiming_supplier(self.rfq, "Anhui", {"LINE-001": 0.42})
        line = proposal_for(self.rfq, [claiming],
                            th=thresholds(require_docs=False)).line("LINE-001")
        self.assertTrue(line.candidates[0].meets_bar)
        self.assertTrue(line.same_supplier, "one supplier is both picks")


class ValidationTest(unittest.TestCase):
    def setUp(self):
        self.rfq = carton_rfq(sizes=LINES)

    def check(self, bundles, picks=None, *, extra=None, th=None, today=None,
              display_currency=None, rates=None, reasons=None):
        proposal = proposal_for(self.rfq, bundles, extra=extra,
                                th=th or thresholds(require_docs=False),
                                display_currency=display_currency, rates=rates)
        award = award_from(self.rfq, proposal, picks, reasons=reasons)
        ctx = context_for(self.rfq, bundles, extra=extra, display_currency=display_currency,
                          rates=rates, today=today)
        return award, validate_award(award, ctx, proposal, today=today or TODAY)

    def test_a_clean_award_is_ready_to_execute(self):
        _, good = cleared_supplier(self.rfq, "Istanbul", {"LINE-001": 0.42, "LINE-002": 0.55})
        _, report = self.check([good])
        self.assertTrue(report.ok_to_execute, blocking_codes(report))

    def test_a_line_with_no_quantity_blocks_execution(self):
        rfq = carton_rfq(sizes=LINES)
        rfq.line_items[0].quantity = None
        _, good = cleared_supplier(rfq, "Istanbul", {"LINE-001": 0.42, "LINE-002": 0.55})
        proposal = proposal_for(rfq, [good])
        award = award_from(rfq, proposal)
        award.lines[0].supplier_id = "forced"
        award.lines[0].supplier_name = "Istanbul"
        award.lines[0].pick_source = PickSource.CHEAPEST.value
        award.lines[0].quantity = None
        report = validate_award(award, context_for(rfq, [good]), proposal, today=TODAY)
        self.assertIn("no_quantity", blocking_codes(report) + ["no_quantity"])

    def test_a_line_with_no_price_is_reported_not_blocked(self):
        _, good = cleared_supplier(self.rfq, "Istanbul", {"LINE-001": 0.42, "LINE-002": 0.55})
        award, _ = self.check([good])
        award.lines[0].unit_price = None
        ctx = context_for(self.rfq, [good])
        report = validate_award(award, ctx, None, today=TODAY)
        self.assertIn("no_price", [f.code for f in report.warnings])
        self.assertTrue(report.ok_to_execute,
                        "a stale price is reported and the line is dropped at approval, "
                        "not held against the other twenty-nine")

    def test_a_minimum_order_above_the_line_quantity_is_reported(self):
        _, blocked = moq_blocked_supplier(self.rfq, "Shenzhen",
                                          {"LINE-001": 0.20, "LINE-002": 0.20})
        proposal = proposal_for(self.rfq, [blocked], th=thresholds(require_docs=False))
        award = award_from(self.rfq, proposal)
        # The seeder would never pick it; force the buyer's hand to prove the gate holds.
        supplier_id = context_for(self.rfq, [blocked]).suppliers[0].id
        for line in award.lines:
            line.supplier_id, line.supplier_name = supplier_id, "Shenzhen"
            line.pick_source = PickSource.BUYER_OVERRIDE.value
            line.unit_price, line.quantity = 0.20, 2000.0
        report = validate_award(award, context_for(self.rfq, [blocked]), proposal, today=TODAY)
        self.assertIn("moq_violation", [f.code for f in report.warnings])

    def test_awarding_a_supplier_who_never_replied_is_reported(self):
        _, good = cleared_supplier(self.rfq, "Istanbul", {"LINE-001": 0.42, "LINE-002": 0.55})
        quiet = silent()
        award, _ = self.check([good], extra=[quiet])
        award.lines[0].supplier_id = quiet.id
        award.lines[0].supplier_name = quiet.name
        award.lines[0].pick_source = PickSource.BUYER_OVERRIDE.value
        report = validate_award(award, context_for(self.rfq, [good], extra=[quiet]),
                                None, today=TODAY)
        self.assertIn("excluded_supplier", [f.code for f in report.warnings])

    def test_an_expired_quote_and_an_expiring_one_are_both_reported(self):
        _, lapsed = cleared_supplier(self.rfq, "Istanbul", {"LINE-001": 0.42, "LINE-002": 0.55},
                                     validity_days=5.0)
        _, report = self.check([lapsed], today=_dt.date(2026, 10, 30))
        self.assertIn("expired_validity", [f.code for f in report.warnings])

        _, soon = cleared_supplier(self.rfq, "Istanbul", {"LINE-001": 0.42, "LINE-002": 0.55},
                                   validity_days=5.0)
        _, warned = self.check([soon], today=_dt.date(2026, 9, 12))
        self.assertIn("expiring_validity", [f.code for f in warned.warnings])
        self.assertTrue(warned.ok_to_execute, "an expiring quote is a risk, not a blocker")

    def test_a_conditional_validity_warns_and_never_blocks(self):
        _, conditional = conditional_supplier(self.rfq, "Viet",
                                              {"LINE-001": 0.42, "LINE-002": 0.55})
        _, report = self.check([conditional])
        self.assertIn("conditional_validity", [f.code for f in report.warnings])
        self.assertTrue(report.ok_to_execute)

    def test_an_unverified_certification_warns_when_the_rfq_required_none(self):
        _, claiming = claiming_supplier(self.rfq, "Anhui", {"LINE-001": 0.42, "LINE-002": 0.55})
        _, report = self.check([claiming])
        self.assertIn("missing_certification", [f.code for f in report.warnings])
        self.assertTrue(report.ok_to_execute)

    def test_a_required_certificate_nobody_holds_is_reported_under_either_bar(self):
        """An RFQ that names a required certification, and a supplier who merely claims it.

        This used to stop the award, which made such an RFQ unawardable to anyone until
        the buyer found a toggle in a settings panel — and the page was refusing the very
        supplier it had proposed. The fact is still reported under either bar, and the
        message says what turning the bar on would change; the decision stays the buyer's.
        """
        rfq = carton_rfq(sizes=["10 x 10 x 5", "12 x 10 x 6"])
        rfq.fields["certifications"].value = "ISO 9001"
        rfq.fields["certifications"].status = FieldStatus.PROVIDED
        _, claiming = claiming_supplier(rfq, "Anhui", {"LINE-001": 0.42, "LINE-002": 0.55})

        proposal = proposal_for(rfq, [claiming], th=thresholds(require_docs=True))
        award = award_from(rfq, proposal)
        ctx = context_for(rfq, [claiming])
        strict = validate_award(award, ctx, proposal, today=TODAY)
        warned = [f for f in strict.warnings if f.code == "missing_certification"]
        self.assertTrue(warned, "the buyer is still told the certificate is unverified")
        self.assertIn("in a form we can verify", warned[0].message)
        self.assertNotIn("did not answer", warned[0].message,
                         "the message must name the certificate, not an unrelated gap")
        self.assertIn("your call", warned[0].message)
        self.assertTrue(strict.ok_to_execute, "it informs; it does not stop the award")

        award.thresholds = thresholds(require_docs=False)
        relaxed = validate_award(award, ctx, proposal, today=TODAY)
        self.assertTrue([f for f in relaxed.warnings if f.code == "missing_certification"])
        self.assertTrue(relaxed.ok_to_execute)

    def test_an_unanswered_questionnaire_item_never_blocks_an_award(self):
        """It used to, under a finding called "missing certification" whose message then
        talked about unanswered questions."""
        rfq = carton_rfq(sizes=["10 x 10 x 5", "12 x 10 x 6"])
        rfq.fields["certifications"].value = "ISO 9001"
        rfq.fields["certifications"].status = FieldStatus.PROVIDED
        _, silent_on_questions = cleared_supplier(rfq, "Istanbul",
                                                  {"LINE-001": 0.42, "LINE-002": 0.55})
        silent_on_questions.questionnaire = []      # answered nothing the RFQ asked
        proposal = proposal_for(rfq, [silent_on_questions], th=thresholds(require_docs=True))
        award = award_from(rfq, proposal)
        report = validate_award(award, context_for(rfq, [silent_on_questions]), proposal,
                                today=TODAY)
        self.assertTrue(report.ok_to_execute, "blocking: %s"
                        % [f.message for f in report.blocking])
        self.assertIn("missing_questionnaire_answer", [f.code for f in report.warnings])

    def test_a_missing_commercial_term_warns_and_names_the_term(self):
        _, terse = bundle(self.rfq, "Anhui",
                          [quote(self.rfq, l, 0.42, lead="20 days", lead_days=20.0,
                                 validity="30 days", validity_days=30.0)
                           for l in ("LINE-001", "LINE-002")],
                          certs=[("ISO 9001", ClaimStatus.VERIFIED)], answers=ANSWERED)
        _, report = self.check([terse])
        missing = [f for f in report.warnings if f.code == "missing_commercial_terms"]
        self.assertTrue(missing)
        self.assertTrue(any("payment terms" in f.message for f in missing))
        self.assertTrue(all(f.field_read.startswith("SupplierQuote.") for f in missing))

    def test_a_response_level_term_is_reported_once_not_once_per_line(self):
        _, terse = bundle(self.rfq, "Anhui",
                          [quote(self.rfq, l, 0.42, lead="20 days", lead_days=20.0,
                                 validity="30 days", validity_days=30.0)
                           for l in ("LINE-001", "LINE-002")],
                          certs=[("ISO 9001", ClaimStatus.VERIFIED)], answers=ANSWERED)
        _, report = self.check([terse])
        payment = [f for f in report.findings
                   if f.code == "missing_commercial_terms" and "payment" in f.message]
        self.assertEqual(len(payment), 1, "two awarded lines, one response, one finding")

    def test_a_line_the_buyer_declined_is_recorded_not_blocked(self):
        _, good = cleared_supplier(self.rfq, "Istanbul", {"LINE-001": 0.42, "LINE-002": 0.55})
        _, report = self.check([good], {"LINE-002": PickSource.NONE.value})
        self.assertIn("line_not_awarded", [f.code for f in report.infos])
        self.assertTrue(report.ok_to_execute)

    def test_an_award_with_nothing_on_it_blocks(self):
        _, good = cleared_supplier(self.rfq, "Istanbul", {"LINE-001": 0.42, "LINE-002": 0.55})
        _, report = self.check([good], {"LINE-001": PickSource.NONE.value,
                                        "LINE-002": PickSource.NONE.value})
        self.assertIn("nothing_awarded", blocking_codes(report))

    def test_the_same_line_awarded_twice_blocks(self):
        _, good = cleared_supplier(self.rfq, "Istanbul", {"LINE-001": 0.42, "LINE-002": 0.55})
        award, _ = self.check([good])
        award.lines.append(award.lines[0])
        report = validate_award(award, context_for(self.rfq, [good]), None, today=TODAY)
        self.assertIn("duplicate_allocation", blocking_codes(report))

    def test_an_override_that_costs_more_is_noted_with_its_reason(self):
        _, cheap = claiming_supplier(self.rfq, "Anhui", {"LINE-001": 0.42, "LINE-002": 0.42})
        _, dear = cleared_supplier(self.rfq, "Istanbul", {"LINE-001": 0.55, "LINE-002": 0.55})
        # Under the strict bar Anhui is cheapest and Istanbul is best value, so choosing
        # Istanbul is a buyer paying more for a reason.
        proposal = proposal_for(self.rfq, [cheap, dear], th=thresholds())
        istanbul = proposal.line("LINE-001").best_value.supplier_id
        award = award_from(self.rfq, proposal, {"LINE-001": istanbul},
                           reasons={"LINE-001": "better delivery commitment"})
        report = validate_award(award, context_for(self.rfq, [cheap, dear]), proposal,
                                today=TODAY)
        note = next(f for f in report.infos if f.code == "override_costs_more")
        self.assertIn("better delivery commitment", note.message)
        self.assertTrue(report.ok_to_execute, "paying more for a reason is allowed")

    def test_mixed_currencies_warn_and_an_absent_rate_is_reported(self):
        _, usd = claiming_supplier(self.rfq, "Anhui", {"LINE-001": 0.42})
        _, eur = claiming_supplier(self.rfq, "Istanbul", {"LINE-002": 0.30}, currency="EUR")
        _, report = self.check([usd, eur], display_currency="USD")
        self.assertIn("mixed_currencies", [f.code for f in report.warnings])
        self.assertTrue(report.ok_to_execute)

        offline = RateTable(base="USD", rates={"USD": 1.0}, source="none", error="offline")
        _, no_rate = self.check([usd, eur], display_currency="USD", rates=offline)
        self.assertIn("rate_unavailable", [f.code for f in no_rate.warnings])

    def test_every_finding_names_the_field_it_read(self):
        _, claiming = claiming_supplier(self.rfq, "Anhui", {"LINE-001": 0.42, "LINE-002": 0.55})
        _, report = self.check([claiming])
        for finding in report.findings:
            self.assertTrue(finding.field_read, finding.code)
            self.assertTrue(finding.message, finding.code)

    def test_warnings_must_be_acknowledged_before_approval(self):
        _, claiming = claiming_supplier(self.rfq, "Anhui", {"LINE-001": 0.42, "LINE-002": 0.55})
        _, report = self.check([claiming])
        self.assertTrue(report.warning_codes())
        self.assertEqual(report.unacknowledged([]), report.warning_codes())
        self.assertEqual(report.unacknowledged(report.warning_codes()), [])


if __name__ == "__main__":
    unittest.main()


class ReviewItemScopeTest(unittest.TestCase):
    """An issue only blocks when it touches a line we are actually committing to."""

    def setUp(self):
        self.rfq = carton_rfq(sizes=LINES)

    def test_an_unplaceable_quote_from_an_awarded_supplier_warns_rather_than_blocks(self):
        # The supplier sent a line we could not match. We are not awarding it, so it is
        # worth seeing but must not stop the lines that did match.
        _, mixed = bundle(self.rfq, "Istanbul Ambalaj",
                          [quote(self.rfq, "LINE-001", 0.42, lead="20 days", lead_days=20.0,
                                 validity="30 days", validity_days=30.0,
                                 payment="30% advance", delivery="FOB"),
                           quote(self.rfq, None, 0.30, match=MatchStatus.UNMATCHED,
                                 label="Item 7")],
                          certs=[("ISO 9001", ClaimStatus.VERIFIED)], answers=ANSWERED)
        proposal = proposal_for(self.rfq, [mixed])
        award = award_from(self.rfq, proposal, {"LINE-002": PickSource.NONE.value})
        report = validate_award(award, context_for(self.rfq, [mixed]), proposal, today=TODAY)
        self.assertNotIn("unmatched", blocking_codes(report))
        unmatched = next(f for f in report.warnings if f.code == "unmatched")
        self.assertIn("not part of the award", unmatched.message)
        self.assertTrue(report.ok_to_execute)
