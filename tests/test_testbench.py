"""The supplier-response test bench.

The bench forms no opinion of its own: it arranges inputs, calls the Phase 2 extractor,
and reads the trust signals Phase 2 already recorded. These tests check that it surfaces
those signals faithfully, that it never writes anything unless asked, and that a figure
present in the input but absent from the output gets noticed.
"""
from __future__ import annotations

import os
import tempfile
import unittest

from rfq_copilot.ai_service import AITimeout
from rfq_copilot.config import Settings
from rfq_copilot.persistence import RFQRepository
from rfq_copilot.supplier_ai_extractor import SupplierExtractor
from rfq_copilot.supplier_models import ClaimStatus, MatchStatus, NormalizationStatus, QuoteStatus
from rfq_copilot.supplier_service import SupplierService
from rfq_copilot.supplier_testbench import SupplierTestbench, TestbenchError
from tests.supplier_helpers import (
    ScriptedAI, carton_rfq, evidence, extraction_payload, match, match_payload, quote_line,
)

CLEAN_REPLY = """Thank you for your enquiry.

We quote USD 0.48 per piece for the 12 x 10 x 6 carton.
Minimum order quantity is 5,000 pieces. Lead time 18 days.
Payment 30% advance. FOB Shanghai.
"""


def bench(payloads):
    settings = Settings()
    settings.db_path = os.path.join(tempfile.mkdtemp(prefix="bench_"), "t.db")
    ai = ScriptedAI(payloads)
    repo = RFQRepository(settings.db_path)
    rfq = carton_rfq()
    repo.save_rfq(rfq)
    b = SupplierTestbench(ai, settings, extractor=SupplierExtractor(ai, settings))
    return b, rfq, repo, settings, ai


def clean_payload():
    return extraction_payload(
        quote_lines=[quote_line("12 x 10 x 6 carton", 0.48, "12 x 10 x 6",
                                ev="We quote USD 0.48 per piece for the 12 x 10 x 6 carton")],
        commercial_terms={"minimum_order_quantity": 5000.0, "moq_evidence": evidence("Minimum order quantity is 5,000 pieces"),
                          "lead_time_text": "18 days", "lead_time_evidence": evidence("Lead time 18 days"),
                          "payment_terms": "30% advance", "delivery_terms": "FOB Shanghai"})


class CleanRunTest(unittest.TestCase):
    def test_every_extracted_value_carries_the_words_it_came_from(self):
        b, rfq, _, _, _ = bench([clean_payload()])
        run = b.run_test(rfq, "LINE-002", "Test Supplier", "Re: enquiry", CLEAN_REPLY)
        self.assertTrue(run.rows)
        price = [r for r in run.rows if r.field.startswith("Unit price")]
        self.assertEqual(len(price), 1)
        self.assertIn("0.48", price[0].extracted)
        self.assertTrue(price[0].traced, "the price quotes a span really in the reply")
        self.assertIn("0.48", price[0].span)
        self.assertFalse(price[0].deviation)

    def test_the_email_body_is_read_as_a_document(self):
        b, rfq, _, _, _ = bench([clean_payload()])
        run = b.run_test(rfq, "LINE-002", "S", "Re: enquiry", CLEAN_REPLY)
        self.assertTrue(run.documents)
        self.assertEqual(run.documents[0]["filename"], "supplier_email.txt")
        self.assertIn("Subject: Re: enquiry", run.combined_input())

    def test_commercial_terms_appear_once_each(self):
        b, rfq, _, _, _ = bench([clean_payload()])
        run = b.run_test(rfq, "LINE-002", "S", "", CLEAN_REPLY)
        labels = [r.field for r in run.rows if r.group == "Commercial terms"]
        self.assertEqual(len(labels), len(set(labels)), "no duplicated term rows")
        self.assertIn("Minimum order quantity", labels)
        self.assertIn("Lead time", labels)

    def test_a_run_writes_nothing_to_the_database(self):
        b, rfq, repo, settings, _ = bench([clean_payload()])
        b.run_test(rfq, "LINE-002", "S", "", CLEAN_REPLY)
        store = SupplierService(repo, ScriptedAI([]), settings).store
        self.assertEqual(store.list_responses(rfq.id), [], "a test run is throwaway")
        self.assertEqual(store.list_suppliers(), [])


class DeviationTest(unittest.TestCase):
    def test_a_price_with_no_supporting_span_is_flagged(self):
        payload = extraction_payload(
            quote_lines=[quote_line("12 x 10 x 6 carton", 0.48, "12 x 10 x 6",
                                    ev="our rock bottom price of USD 0.48")])   # not in the reply
        b, rfq, _, _, _ = bench([payload])
        run = b.run_test(rfq, "LINE-002", "S", "", CLEAN_REPLY)
        price = [r for r in run.rows if r.field.startswith("Unit price")][0]
        self.assertFalse(price.traced)
        self.assertTrue(price.deviation)
        self.assertTrue(run.deviations)

    def test_a_quote_for_a_size_the_rfq_does_not_have_is_not_matched(self):
        payload = extraction_payload(
            quote_lines=[quote_line("40 x 40 x 40 mega carton", 2.10, "40 x 40 x 40",
                                    ev="USD 2.10 for the 40 x 40 x 40")])
        b, rfq, _, _, _ = bench([payload, match_payload([match("40 x 40 x 40 mega carton", None,
                                                               basis="none", confidence=0.1)])])
        run = b.run_test(rfq, "LINE-002", "S", "", "USD 2.10 for the 40 x 40 x 40 mega carton")
        match_rows = [r for r in run.rows if r.group == "Line matching"]
        self.assertTrue(match_rows)
        self.assertEqual(match_rows[0].extracted, "not matched")
        self.assertTrue(match_rows[0].deviation)

    def test_a_per_kilogram_price_is_flagged_as_not_comparable(self):
        payload = extraction_payload(
            quote_lines=[quote_line("12 x 10 x 6 carton", 2.35, "12 x 10 x 6", basis="per_kg",
                                    ev="USD 2.35 per kg of finished carton")])
        b, rfq, _, _, _ = bench([payload])
        run = b.run_test(rfq, "LINE-002", "S", "", "We price by weight: USD 2.35 per kg of finished carton.")
        norm = [r for r in run.rows if r.field == "Normalized per piece"]
        self.assertEqual(len(norm), 1)
        self.assertEqual(norm[0].extracted, "not comparable")
        self.assertTrue(norm[0].deviation)
        self.assertIn("weight", norm[0].note.lower())

    def test_a_bare_certification_claim_is_flagged(self):
        payload = extraction_payload(
            quote_lines=[quote_line("12 x 10 x 6 carton", 0.48, "12 x 10 x 6",
                                    ev="We quote USD 0.48 per piece for the 12 x 10 x 6 carton")],
            certifications=[{"name": "ISO 9001", "raw_name": "ISO 9001", "certificate_number": None,
                             "issuing_body": None, "expiry_date": None, "document_attached": False,
                             "document_reference": None,
                             "evidence": evidence("We are ISO 9001 certified"), "confidence": 0.9}])
        b, rfq, _, _, _ = bench([payload])
        run = b.run_test(rfq, "LINE-002", "S", "", CLEAN_REPLY + "\nWe are ISO 9001 certified.")
        cert = [r for r in run.rows if r.field.startswith("Certification")][0]
        self.assertEqual(cert.extracted, "claimed")
        self.assertTrue(cert.deviation, "a claim with no certificate is worth flagging")

    def test_a_contradiction_is_shown_with_both_sides(self):
        conflict = {"topic": "lead time", "description": "Two lead times given",
                    "values": [{"value": "18 days", "evidence": evidence("Lead time 18 days")},
                               {"value": "30 days", "evidence": evidence("allow 30 days in peak season")}]}
        payload = extraction_payload(
            quote_lines=[quote_line("12 x 10 x 6 carton", 0.48, "12 x 10 x 6",
                                    ev="We quote USD 0.48 per piece for the 12 x 10 x 6 carton")],
            conflicts=[conflict])
        b, rfq, _, _, _ = bench([payload])
        run = b.run_test(rfq, "LINE-002", "S", "",
                         CLEAN_REPLY + "\nPlease allow 30 days in peak season.")
        rows = [r for r in run.rows if r.field.startswith("Contradiction")]
        self.assertEqual(len(rows), 1, "one row, not one per affected line")
        self.assertIn("18 days", rows[0].extracted)
        self.assertIn("30 days", rows[0].extracted)
        self.assertTrue(rows[0].deviation)


class UnclaimedFigureTest(unittest.TestCase):
    def test_a_figure_never_extracted_is_listed(self):
        b, rfq, _, _, _ = bench([clean_payload()])
        run = b.run_test(rfq, "LINE-002", "S", "",
                         CLEAN_REPLY + "\nA one-off tooling charge of USD 250 applies.")
        joined = " ".join(run.unclaimed_figures)
        self.assertIn("250", joined, "a charge the extractor ignored is surfaced")

    def test_figures_that_were_extracted_are_not_listed(self):
        b, rfq, _, _, _ = bench([clean_payload()])
        run = b.run_test(rfq, "LINE-002", "S", "", CLEAN_REPLY)
        joined = " ".join(run.unclaimed_figures)
        self.assertNotIn("0.48", joined)
        self.assertNotIn("5,000", joined)


class ErrorTest(unittest.TestCase):
    def test_an_empty_reply_is_refused_before_any_model_call(self):
        b, rfq, _, _, ai = bench([])
        with self.assertRaises(TestbenchError):
            b.run_test(rfq, "LINE-002", "S", "", "   ")
        self.assertEqual(ai.calls, [])

    def test_an_rfq_with_no_lines_is_refused(self):
        b, rfq, _, _, _ = bench([])
        rfq.line_items = []
        with self.assertRaises(TestbenchError):
            b.run_test(rfq, None, "S", "", CLEAN_REPLY)

    def test_an_extraction_failure_becomes_a_readable_message(self):
        b, rfq, _, _, _ = bench([AITimeout("slow")])
        with self.assertRaises(TestbenchError) as ctx:
            b.run_test(rfq, "LINE-002", "S", "", CLEAN_REPLY)
        self.assertIn("took too long", str(ctx.exception))

    def test_an_unreadable_attachment_alone_is_refused(self):
        b, rfq, _, _, ai = bench([])
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "legacy.xls")
        with open(path, "wb") as f:
            f.write(b"\xd0\xcf\x11\xe0 old binary")
        with self.assertRaises(TestbenchError) as ctx:
            b.run_test(rfq, "LINE-002", "S", "", "", [path])
        self.assertIn("Nothing in the supplier reply could be read", str(ctx.exception))
        self.assertIn("Re-save it as .xlsx or .csv", str(ctx.exception))


class PromoteTest(unittest.TestCase):
    def test_keeping_a_run_makes_it_a_real_supplier_response(self):
        b, rfq, repo, settings, _ = bench([clean_payload()])
        run = b.run_test(rfq, "LINE-002", "Keeper Supplier", "", CLEAN_REPLY)
        sup = SupplierService(repo, ScriptedAI([]), settings)
        self.assertFalse(sup.has_responses(rfq.id))

        response_id = b.promote(run, sup)
        self.assertTrue(run.promoted)
        self.assertTrue(sup.has_responses(rfq.id))
        stored = sup.bundle(response_id)
        self.assertIsNotNone(stored)
        self.assertEqual(stored.supplier.name, "Keeper Supplier")
        matrix = sup.build_comparison(rfq.id)
        self.assertIn("Keeper Supplier", [s.name for s in matrix.suppliers])
        self.assertEqual(matrix.cell("LINE-002", stored.response.supplier_id).state, "quoted")

    def test_promoting_twice_does_not_duplicate(self):
        b, rfq, repo, settings, _ = bench([clean_payload()])
        run = b.run_test(rfq, "LINE-002", "S", "", CLEAN_REPLY)
        sup = SupplierService(repo, ScriptedAI([]), settings)
        first = b.promote(run, sup)
        second = b.promote(run, sup)
        self.assertEqual(first, second)
        self.assertEqual(len(sup.responses_for(rfq.id)), 1)


class Phase2UntouchedTest(unittest.TestCase):
    def test_the_bench_uses_the_real_extractor(self):
        """If Phase 2's extractor changes, the bench changes with it; there is no copy."""
        import inspect
        from rfq_copilot import supplier_testbench
        source = inspect.getsource(supplier_testbench)
        self.assertIn("SupplierExtractor", source)
        for forbidden in ("SUPPLIER_EXTRACTION_SCHEMA", "EXTRACTION_SYSTEM_PROMPT", "def _build_quotes"):
            self.assertNotIn(forbidden, source,
                             "the bench must not carry its own copy of the extraction logic")


if __name__ == "__main__":
    unittest.main()
