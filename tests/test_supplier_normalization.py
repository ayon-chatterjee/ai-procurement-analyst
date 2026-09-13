"""Price normalisation, discounts, MOQ, lead time, and supplier-to-RFQ line matching."""
from __future__ import annotations

import unittest

from rfq_copilot.line_matcher import PROBABLE, STRONG, extract_dimensions, match_all, match_supplier_line
from rfq_copilot.quote_normalizer import (
    apply_discount, check_moq, comparable_across, currencies_in, infer_price_basis, normalize_price,
    parse_discount_threshold, parse_lead_time, parse_quote_validity, rfq_total_quantity,
)
from rfq_copilot.supplier_models import (
    Discount, MatchStatus, NormalizationStatus, PriceBasis, QuoteStatus, SupplierQuote,
)
from tests.supplier_helpers import carton_rfq


def quote(price=None, basis=PriceBasis.PER_UNIT, currency="USD", **kw):
    return SupplierQuote(unit_price=price, price_basis=basis, currency=currency, quoted_unit="pcs", **kw)


class PriceBasisTest(unittest.TestCase):
    def test_reads_the_basis_from_the_supplier_wording(self):
        cases = [("USD 0.42 per piece", PriceBasis.PER_UNIT), ("price per 100 pcs", PriceBasis.PER_100),
                 ("per 1,000 pieces", PriceBasis.PER_1000), ("USD 2.35 per kg", PriceBasis.PER_KG),
                 ("per set", PriceBasis.PER_SET), ("lump sum", PriceBasis.PER_LOT),
                 ("0.42", PriceBasis.UNKNOWN)]
        for text, expected in cases:
            self.assertEqual(infer_price_basis(text), expected, text)


class NormalizationTest(unittest.TestCase):
    def test_safe_divisions_are_performed(self):
        for basis, price, expected in ((PriceBasis.PER_UNIT, 0.42, 0.42),
                                       (PriceBasis.PER_100, 47.0, 0.47),
                                       (PriceBasis.PER_1000, 420.0, 0.42)):
            q = normalize_price(quote(price, basis))
            self.assertEqual(q.normalization_status, NormalizationStatus.NORMALIZED)
            self.assertAlmostEqual(q.normalized_unit_price, expected, places=6)

    def test_the_original_quote_is_never_destroyed(self):
        q = normalize_price(quote(47.0, PriceBasis.PER_100))
        self.assertEqual(q.unit_price, 47.0)
        self.assertIn("per 100", q.original_price_text())
        self.assertIn("0.47", q.normalized_price_text())

    def test_a_weight_price_cannot_become_a_piece_price(self):
        q = normalize_price(quote(2.35, PriceBasis.PER_KG))
        self.assertEqual(q.normalization_status, NormalizationStatus.UNRESOLVED)
        self.assertIsNone(q.normalized_unit_price)
        self.assertEqual(q.status, QuoteStatus.UNRESOLVED)
        self.assertIn("weight", q.normalization_note.lower())

    def test_an_unstated_basis_is_read_as_per_unit_and_says_so(self):
        """An itemised email that never writes the words "per unit" is the commonest reply
        there is. Voiding its prices showed the buyer "unresolved" against four figures the
        supplier had stated plainly, so the figure is kept and the reading is declared."""
        q = normalize_price(quote(5.0, PriceBasis.UNKNOWN))
        self.assertEqual(q.normalized_unit_price, 5.0)
        self.assertEqual(q.normalization_status, NormalizationStatus.NORMALIZED)
        self.assertTrue(q.price_basis_assumed)
        self.assertIn("assumption", q.normalization_note)

    def test_that_assumption_is_held_for_review_not_treated_as_settled(self):
        """Visible, never authoritative: a quote at NEEDS_REVIEW is excluded from every
        comparison and cannot be seeded into an award."""
        q = normalize_price(quote(5.0, PriceBasis.UNKNOWN))
        self.assertEqual(q.status, QuoteStatus.NEEDS_REVIEW)
        self.assertTrue(any("not stated" in i for i in q.issues))

    def test_a_basis_the_supplier_did_state_is_never_assumed_away(self):
        """The guard that matters stays: per-set and per-kg are still refused, because
        those the supplier actually told us, and they are not per-piece."""
        for basis in (PriceBasis.PER_SET, PriceBasis.PER_KG, PriceBasis.PER_LOT):
            q = normalize_price(quote(5.0, basis))
            self.assertIsNone(q.normalized_unit_price, basis.value)
            self.assertEqual(q.normalization_status, NormalizationStatus.UNRESOLVED)
            self.assertFalse(q.price_basis_assumed)

    def test_no_price_is_not_a_zero(self):
        q = normalize_price(quote(None))
        self.assertIsNone(q.normalized_unit_price)
        self.assertEqual(q.normalization_status, NormalizationStatus.NOT_APPLICABLE)


class DiscountTest(unittest.TestCase):
    def test_threshold_parsing(self):
        self.assertEqual(parse_discount_threshold("total order quantities above 10,000 pcs"),
                         (10000.0, False))
        self.assertEqual(parse_discount_threshold("orders over 5000 units"), (5000.0, False))
        self.assertIsNone(parse_discount_threshold("for strategic partners"))
        self.assertIsNone(parse_discount_threshold(""))

    def test_at_least_includes_the_number_it_names(self):
        """"Above 10,000" and "at least 10,000" are different conditions, and an order of
        exactly 10,000 meets only the second."""
        self.assertEqual(parse_discount_threshold("at least 10,000 pcs"), (10000.0, True))
        self.assertEqual(parse_discount_threshold("minimum of 5,000 units"), (5000.0, True))

    def test_an_order_that_exactly_meets_an_inclusive_threshold_gets_the_discount(self):
        rfq = carton_rfq(quantity=1000.0)      # 7 sizes x 1000 = 7,000
        quote = SupplierQuote(unit_price=1.0, currency="USD",
                              discount=Discount(percent=10.0, condition="at least 7,000 pcs"))
        apply_discount(quote, rfq)
        self.assertTrue(quote.discount.applies)
        self.assertIn("meets", quote.discount.applies_reason)
        self.assertEqual(quote.effective_unit_price, 0.9)

    def test_the_same_order_misses_a_strictly_greater_threshold(self):
        rfq = carton_rfq(quantity=1000.0)
        quote = SupplierQuote(unit_price=1.0, currency="USD",
                              discount=Discount(percent=10.0, condition="above 7,000 pcs"))
        apply_discount(quote, rfq)
        self.assertFalse(quote.discount.applies)
        self.assertIsNone(quote.effective_unit_price)

    def test_an_evaluable_condition_yields_an_effective_price(self):
        rfq = carton_rfq()                      # 7 lines x 2,000 = 14,000
        self.assertEqual(rfq_total_quantity(rfq), 14000.0)
        q = quote(47.0, PriceBasis.PER_100,
                  discount=Discount(percent=5, condition="total order quantities above 10,000 pcs"))
        apply_discount(q, rfq)
        normalize_price(q)
        self.assertTrue(q.discount.applies)
        self.assertAlmostEqual(q.effective_unit_price, 44.65, places=4)
        self.assertAlmostEqual(q.normalized_unit_price, 0.4465, places=6)
        self.assertEqual(q.unit_price, 47.0, "the base price is preserved")

    def test_an_unmet_condition_leaves_the_base_price(self):
        rfq = carton_rfq(quantity=100.0)        # 700 total, below the threshold
        q = quote(47.0, PriceBasis.PER_100,
                  discount=Discount(percent=5, condition="above 10,000 pcs"))
        apply_discount(q, rfq)
        self.assertFalse(q.discount.applies)
        self.assertIsNone(q.effective_unit_price)
        self.assertIn("does not reach", q.discount.applies_reason)

    def test_an_unevaluable_condition_is_not_guessed(self):
        q = quote(0.5, discount=Discount(percent=5, condition="for strategic partners"))
        apply_discount(q, carton_rfq())
        self.assertIsNone(q.discount.applies, "an unevaluable condition is neither met nor unmet")
        self.assertIsNone(q.effective_unit_price)

    def test_the_condition_is_never_lost(self):
        d = Discount(percent=5, condition="total order quantities above 10,000 pcs")
        self.assertIn("10,000", d.describe())


class MoqTest(unittest.TestCase):
    def test_a_minimum_above_the_line_quantity_is_flagged_not_rejected(self):
        rfq = carton_rfq()
        q = quote(0.41, minimum_order_quantity=5000.0)
        check_moq(q, rfq.line_items[0])
        self.assertTrue(q.moq_constraint)
        self.assertTrue(q.issues)
        self.assertEqual(q.status, QuoteStatus.QUOTED, "the supplier stays in the comparison")

    def test_a_satisfied_minimum_is_not_flagged(self):
        rfq = carton_rfq()
        q = quote(0.41, minimum_order_quantity=1000.0)
        check_moq(q, rfq.line_items[0])
        self.assertFalse(q.moq_constraint)

    def test_moq_is_separate_from_quoted_quantity(self):
        q = quote(0.41, quoted_quantity=2000.0, minimum_order_quantity=5000.0)
        self.assertNotEqual(q.quoted_quantity, q.minimum_order_quantity)


class LeadTimeAndValidityTest(unittest.TestCase):
    def test_a_single_figure_is_taken_as_stated(self):
        days, interpreted = parse_lead_time("18 days from artwork approval")
        self.assertEqual(days, 18.0)
        self.assertFalse(interpreted)

    def test_a_range_is_labelled_as_an_interpretation(self):
        days, interpreted = parse_lead_time("20-25 working days after artwork approval")
        self.assertEqual(days, 22.5)
        self.assertTrue(interpreted, "reducing a range to one number is an interpretation")

    def test_validity_distinguishes_a_period_from_a_condition(self):
        days, conditional = parse_quote_validity("Valid for 30 days")
        self.assertEqual(days, 30.0)
        self.assertFalse(conditional)
        days, conditional = parse_quote_validity("valid subject to kraft paper prices")
        self.assertTrue(conditional, "a condition is not a fixed validity period")
        self.assertIsNone(days)


class CurrencyTest(unittest.TestCase):
    def test_mixed_currencies_are_not_comparable(self):
        self.assertFalse(comparable_across([quote(1, currency="USD"), quote(1, currency="EUR")]))
        self.assertTrue(comparable_across([quote(1, currency="USD"), quote(1, currency="USD")]))
        self.assertEqual(currencies_in([quote(1, currency="usd"), quote(1, currency="EUR")]), ["USD", "EUR"])


class DimensionTest(unittest.TestCase):
    def test_dimensions_are_found_in_varied_wording(self):
        for text in ("12 x 10 x 6", "12x10x6 inch carton", "the 12 × 10 × 6 box", "12*10*6"):
            self.assertEqual(extract_dimensions(text), (12.0, 10.0, 6.0), text)
        self.assertIsNone(extract_dimensions("jumbo mailer"))


class MatchingTest(unittest.TestCase):
    def setUp(self):
        self.rfq = carton_rfq()

    def _line(self, label, size=None, qty=2000.0):
        return {"supplier_line_label": label, "described_size": size, "quoted_quantity": qty}

    def test_dimensions_match_regardless_of_row_order(self):
        lines = [self._line("Large carton 24x18x12", "24 x 18 x 12"),
                 self._line("the 12 x 10 x 6 box", "12 x 10 x 6")]
        results = match_all(self.rfq, lines)
        self.assertEqual(results[0].line_item_id, "LINE-006")
        self.assertEqual(results[1].line_item_id, "LINE-002")
        self.assertTrue(all(r.status == MatchStatus.MATCHED for r in results))

    def test_supplier_row_number_is_never_assumed_to_be_the_rfq_line(self):
        """Supplier's row 1 describes the RFQ's line 6; position must not win."""
        results = match_all(self.rfq, [self._line("1", "24 x 18 x 12")])
        self.assertEqual(results[0].line_item_id, "LINE-006")

    def test_an_unidentifiable_line_is_left_unmatched(self):
        results = match_all(self.rfq, [self._line("jumbo mailer", None, 500)])
        self.assertEqual(results[0].status, MatchStatus.UNMATCHED)
        self.assertIsNone(results[0].line_item_id)

    def test_two_lines_claiming_one_rfq_line_produce_a_conflict(self):
        results = match_all(self.rfq, [self._line("12x10x6 standard", "12 x 10 x 6"),
                                       self._line("12 x 10 x 6 premium", "12 x 10 x 6")])
        statuses = {r.status for r in results}
        self.assertIn(MatchStatus.CONFLICT, statuses)
        conflicted = [r for r in results if r.status == MatchStatus.CONFLICT][0]
        self.assertIsNone(conflicted.line_item_id, "a contested line is not assigned")

    def test_a_model_id_that_does_not_exist_is_ignored(self):
        decision = {"supplier_line_label": "mystery", "rfq_line_item_id": "LINE-999",
                    "basis": "description", "confidence": 0.99, "reason": "x",
                    "alternative_line_item_ids": []}
        r = match_supplier_line(self.rfq, self._line("mystery", None), decision)
        self.assertNotEqual(r.line_item_id, "LINE-999")
        self.assertEqual(r.status, MatchStatus.UNMATCHED)

    def test_position_based_matching_is_capped_at_probable(self):
        decision = {"supplier_line_label": "size 1", "rfq_line_item_id": "LINE-001",
                    "basis": "position", "confidence": 0.6, "reason": "list order",
                    "alternative_line_item_ids": []}
        r = match_supplier_line(self.rfq, self._line("size 1", None, None), decision)
        self.assertEqual(r.status, MatchStatus.PROBABLE_MATCH, "list order is not verifiable")

    def test_a_model_disagreeing_with_a_strong_dimension_match_forces_review(self):
        decision = {"supplier_line_label": "the 12 x 10 x 6 box", "rfq_line_item_id": "LINE-003",
                    "basis": "description", "confidence": 0.9, "reason": "x",
                    "alternative_line_item_ids": []}
        r = match_supplier_line(self.rfq, self._line("the 12 x 10 x 6 box", "12 x 10 x 6"), decision)
        self.assertEqual(r.status, MatchStatus.PROBABLE_MATCH)
        self.assertIn("LINE-003", r.alternatives)

    def test_thresholds_are_ordered(self):
        self.assertGreater(STRONG, PROBABLE)


if __name__ == "__main__":
    unittest.main()
