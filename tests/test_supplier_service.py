"""End-to-end service behaviour with a scripted model: persistence, revisions,
corrections, the comparison dataset, and the negative guarantees Phase 2 rests on."""
from __future__ import annotations

import os
import tempfile
import unittest

from rfq_copilot.ai_service import AIInvalidOutput, AITimeout
from rfq_copilot.config import Settings
from rfq_copilot.persistence import RFQRepository
from rfq_copilot.supplier_ai_extractor import SupplierExtractor
from rfq_copilot.supplier_models import (
    ClaimStatus, ExtractionStatus, MatchStatus, NormalizationStatus, QuoteStatus, ResponseType,
    Supplier, SupplierStatus,
)
from rfq_copilot.supplier_service import CellState, SupplierService
from tests.supplier_helpers import (
    ScriptedAI, carton_rfq, evidence, extraction_payload, match, match_payload, quote_line,
)


class ServiceHarness(unittest.TestCase):
    """Builds a service whose model output is scripted, so behaviour is deterministic."""

    def build(self, payloads):
        tmp = tempfile.mkdtemp(prefix="rfq_p2_")
        settings = Settings()
        settings.db_path = os.path.join(tmp, "t.db")
        repo = RFQRepository(settings.db_path)
        self.rfq = carton_rfq()
        repo.save_rfq(self.rfq)
        ai = ScriptedAI(payloads)
        svc = SupplierService(repo, ai, settings, extractor=SupplierExtractor(ai, settings))
        return svc, ai

    def register(self, svc, name, text, filename="quote.txt"):
        """Register a response with one real document written to disk."""
        tmp = tempfile.mkdtemp(prefix="docs_")
        path = os.path.join(tmp, filename)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        supplier = Supplier(name=name, country="Testland", status=SupplierStatus.RESPONDED)
        svc.store.save_supplier(supplier)
        return svc._register(self.rfq.id, supplier, [filename], tmp, received_at="2026-09-10"), supplier


DOC_A = """QUOTATION - Test Supplier
Line 1  10 x 10 x 5  2000 pcs  0.42
Line 2  12 x 10 x 6  2000 pcs  0.48
MOQ 2000 pcs. Lead time 18 days. Payment 30% advance.
ISO 9001 certified."""


class ExtractionFlowTest(ServiceHarness):
    def test_a_clean_response_is_extracted_matched_and_persisted(self):
        payload = extraction_payload(
            quote_lines=[quote_line("Line 1", 0.42, "10 x 10 x 5", ev="Line 1  10 x 10 x 5  2000 pcs  0.42"),
                         quote_line("Line 2", 0.48, "12 x 10 x 6", ev="Line 2  12 x 10 x 6  2000 pcs  0.48")],
            commercial_terms={"minimum_order_quantity": 2000.0, "lead_time_text": "18 days",
                              "payment_terms": "30% advance"},
            certifications=[{"name": "ISO 9001", "raw_name": "ISO 9001", "certificate_number": None,
                             "issuing_body": None, "expiry_date": None, "document_attached": False,
                             "document_reference": None, "evidence": evidence("ISO 9001 certified"),
                             "confidence": 0.9}])
        svc, _ = self.build([payload, match_payload([match("Line 1", "LINE-001"), match("Line 2", "LINE-002")])])
        resp, _ = self.register(svc, "Test Supplier", DOC_A)
        bundle = svc.extract_response(resp.id)

        # The extraction itself is sound. An unbacked certification is surfaced in the
        # review queue rather than casting doubt on everything else in the response.
        self.assertEqual(bundle.response.extraction_status, ExtractionStatus.EXTRACTED)
        priced = [q for q in bundle.quotes if q.has_price]
        self.assertEqual(len(priced), 2)
        self.assertEqual({q.line_item_id for q in priced}, {"LINE-001", "LINE-002"})
        self.assertTrue(all(q.status == QuoteStatus.QUOTED for q in priced))
        self.assertAlmostEqual(priced[0].normalized_unit_price, 0.42)
        self.assertEqual(bundle.certifications[0].status, ClaimStatus.CLAIMED)

        # survives a reload
        again = svc.bundle(resp.id)
        self.assertEqual(len(again.quotes), 2)
        self.assertEqual(again.quotes[0].line_item_id, "LINE-001")
        self.assertTrue(again.evidence, "evidence rows are persisted")

    def test_an_omitted_line_is_missing_not_zero(self):
        payload = extraction_payload(
            quote_lines=[quote_line("Line 1", 0.42, "10 x 10 x 5", ev="Line 1  10 x 10 x 5  2000 pcs  0.42")])
        svc, _ = self.build([payload, match_payload([match("Line 1", "LINE-001")])])
        resp, supplier = self.register(svc, "Partial Supplier", DOC_A)
        svc.extract_response(resp.id)

        matrix = svc.build_comparison(self.rfq.id)
        quoted = matrix.cell("LINE-001", supplier.id)
        absent = matrix.cell("LINE-003", supplier.id)
        self.assertEqual(quoted.state, CellState.QUOTED)
        self.assertEqual(absent.state, CellState.NOT_QUOTED)
        self.assertIsNone(absent.quote)
        self.assertEqual(absent.display, "not quoted")
        self.assertNotIn("0", absent.display, "a missing quote must never render as a number")

    def test_response_type_reflects_partial_coverage(self):
        payload = extraction_payload(
            quote_lines=[quote_line("Line 1", 0.42, "10 x 10 x 5", ev="Line 1  10 x 10 x 5  2000 pcs  0.42")])
        svc, _ = self.build([payload, match_payload([match("Line 1", "LINE-001")])])
        resp, _ = self.register(svc, "Partial Supplier", DOC_A)
        bundle = svc.extract_response(resp.id)
        self.assertEqual(bundle.response.response_type, ResponseType.PARTIAL_QUOTE)

    def test_one_supplier_failing_does_not_stop_the_others(self):
        good = extraction_payload(
            quote_lines=[quote_line("Line 1", 0.42, "10 x 10 x 5", ev="Line 1  10 x 10 x 5  2000 pcs  0.42")])
        # first supplier's extraction times out; second succeeds
        svc, _ = self.build([AITimeout("slow"), good, match_payload([match("Line 1", "LINE-001")])])
        r1, _ = self.register(svc, "Broken Supplier", DOC_A, "a.txt")
        r2, _ = self.register(svc, "Working Supplier", DOC_A, "b.txt")
        # One worker: the scripted AI hands out payloads in order, so running the two
        # extractions in parallel would decide by a race which supplier got the timeout.
        results = svc.extract_all(self.rfq.id, max_workers=1)

        self.assertEqual(results["succeeded"], ["Working Supplier"])
        self.assertEqual(len(results["failed"]), 1)
        self.assertEqual(svc.bundle(r1.id).response.extraction_status, ExtractionStatus.FAILED)
        self.assertEqual(svc.bundle(r2.id).response.extraction_status, ExtractionStatus.EXTRACTED)
        # the RFQ itself is untouched
        self.assertEqual(len(svc.repo.get_rfq(self.rfq.id).line_items), 7)

    def test_invalid_output_is_retried_once_then_recorded_as_failed(self):
        svc, ai = self.build([AIInvalidOutput("bad"), AIInvalidOutput("still bad")])
        resp, _ = self.register(svc, "Chaotic Supplier", DOC_A)
        results = svc.extract_all(self.rfq.id)
        self.assertEqual(len(ai.calls), 2, "one corrective retry, then stop")
        self.assertEqual(len(results["failed"]), 1)
        bundle = svc.bundle(resp.id)
        self.assertEqual(bundle.response.extraction_status, ExtractionStatus.FAILED)
        self.assertEqual(bundle.quotes, [], "a failed extraction fabricates nothing")
        self.assertTrue(bundle.documents[0].raw_text, "the raw source is preserved")

    def test_extraction_calls_are_audited(self):
        payload = extraction_payload(
            quote_lines=[quote_line("Line 1", 0.42, "10 x 10 x 5", ev="Line 1  10 x 10 x 5  2000 pcs  0.42")])
        svc, _ = self.build([payload, match_payload([match("Line 1", "LINE-001")])])
        resp, _ = self.register(svc, "Test Supplier", DOC_A)
        svc.extract_response(resp.id)
        calls = svc.extraction_calls(resp.id)
        # Dimensions settle both lines here, so the line-matching call is skipped and
        # only the extraction call is made. Every call that does happen is audited.
        self.assertEqual([c.call_type for c in calls], ["supplier_extraction"])
        self.assertTrue(all(c.prompt_hash and c.prompt_version for c in calls))

    def test_the_matching_call_is_skipped_only_when_dimensions_settle_every_line(self):
        clear = extraction_payload(
            quote_lines=[quote_line("Line 1", 0.42, "10 x 10 x 5", ev="Line 1  10 x 10 x 5  2000 pcs  0.42")])
        svc, ai = self.build([clear])
        resp, _ = self.register(svc, "Clear Supplier", DOC_A)
        svc.extract_response(resp.id)
        self.assertEqual(len(ai.calls), 1, "an unambiguous line needs no second opinion")

        vague = extraction_payload(
            quote_lines=[quote_line("jumbo mailer", 0.42, None, ev="Line 1  10 x 10 x 5  2000 pcs  0.42")])
        svc2, ai2 = self.build([vague, match_payload([match("jumbo mailer", None, basis="none", confidence=0.2)])])
        resp2, _ = self.register(svc2, "Vague Supplier", DOC_A)
        svc2.extract_response(resp2.id)
        self.assertEqual(len(ai2.calls), 2, "an unidentifiable line is worth a second opinion")


class RevisionTest(ServiceHarness):
    def test_a_later_response_supersedes_the_earlier_one_without_deleting_it(self):
        first = extraction_payload(
            quote_lines=[quote_line("Line 1", 0.52, "10 x 10 x 5", ev="Line 1  10 x 10 x 5  2000 pcs  0.52")])
        second = extraction_payload(
            response={"response_type": "revision_received", "is_revision": True,
                      "supersedes_reference": "Q-1", "summary": "Revised."},
            quote_lines=[quote_line("Line 1", 0.49, "10 x 10 x 5", ev="Line 1  10 x 10 x 5  2000 pcs  0.49")])
        svc, _ = self.build([first, match_payload([match("Line 1", "LINE-001")]),
                             second, match_payload([match("Line 1", "LINE-001")])])
        supplier = Supplier(name="Revising Supplier", status=SupplierStatus.RESPONDED)
        svc.store.save_supplier(supplier)
        tmp = tempfile.mkdtemp()
        for fn, price in (("v1.txt", "0.52"), ("v2.txt", "0.49")):
            with open(os.path.join(tmp, fn), "w") as f:
                f.write("Line 1  10 x 10 x 5  2000 pcs  %s" % price)
        r1 = svc._register(self.rfq.id, supplier, ["v1.txt"], tmp, received_at="2026-09-09")
        r2 = svc._register(self.rfq.id, supplier, ["v2.txt"], tmp, received_at="2026-09-12")
        svc.extract_response(r1.id)
        svc.extract_response(r2.id)

        stored = {r.id: r for r in svc.responses_for(self.rfq.id)}
        self.assertFalse(stored[r1.id].is_active)
        self.assertTrue(stored[r2.id].is_active)
        self.assertEqual(stored[r1.id].superseded_by_id, r2.id)
        self.assertIsNotNone(svc.bundle(r1.id), "the earlier response is retained, not deleted")

        matrix = svc.build_comparison(self.rfq.id)
        cell = matrix.cell("LINE-001", supplier.id)
        self.assertAlmostEqual(cell.quote.normalized_unit_price, 0.49, msg="the active quote is the revision")
        self.assertEqual(matrix.summary["superseded_responses"], 1)


class CorrectionTest(ServiceHarness):
    def _extracted(self):
        payload = extraction_payload(
            quote_lines=[quote_line("Line 1", 0.42, "10 x 10 x 5", currency="cents",
                                    ev="Line 1  10 x 10 x 5  2000 pcs  0.42")])
        svc, _ = self.build([payload, match_payload([match("Line 1", "LINE-001")])])
        resp, supplier = self.register(svc, "Vague Supplier", DOC_A)
        return svc, svc.extract_response(resp.id), supplier

    def test_an_unnamed_currency_is_held_out_of_the_price_column(self):
        svc, bundle, supplier = self._extracted()
        q = bundle.quotes[0]
        self.assertEqual(q.status, QuoteStatus.NEEDS_REVIEW)
        self.assertIsNone(q.normalized_unit_price)
        matrix = svc.build_comparison(self.rfq.id)
        self.assertEqual(matrix.cell("LINE-001", supplier.id).state, CellState.NEEDS_REVIEW)

    def test_a_buyer_correction_becomes_active_and_keeps_the_original(self):
        svc, bundle, supplier = self._extracted()
        q = bundle.quotes[0]
        fixed = svc.correct_quote(bundle.response.id, q.id, unit_price=0.42, currency="USD")
        cq = fixed.quotes[0]
        self.assertEqual(cq.currency, "USD")
        self.assertEqual(cq.status, QuoteStatus.QUOTED)
        self.assertAlmostEqual(cq.normalized_unit_price, 0.42)
        self.assertTrue(cq.history, "the extracted value is kept")
        self.assertEqual(cq.history[0]["was"]["currency"], "CENTS", "the original wording is retained")
        self.assertEqual(cq.value_source.value, "buyer_corrected")
        self.assertTrue(cq.evidence_ids, "source evidence is not deleted by a correction")

    def test_confirming_a_match_records_the_decision(self):
        svc, bundle, _ = self._extracted()
        q = bundle.quotes[0]
        out = svc.confirm_match(bundle.response.id, q.id, "LINE-003")
        cq = out.quotes[0]
        self.assertEqual(cq.line_item_id, "LINE-003")
        self.assertEqual(cq.match_status, MatchStatus.MATCHED)
        self.assertTrue(cq.history)

    def test_a_match_to_a_nonexistent_line_is_refused(self):
        from rfq_copilot.rfq_service import RFQStateError
        svc, bundle, _ = self._extracted()
        with self.assertRaises(RFQStateError):
            svc.confirm_match(bundle.response.id, bundle.quotes[0].id, "LINE-999")


class ComparisonDatasetTest(ServiceHarness):
    def test_summary_is_counted_from_stored_data(self):
        payload = extraction_payload(
            quote_lines=[quote_line("Line 1", 0.42, "10 x 10 x 5", ev="Line 1  10 x 10 x 5  2000 pcs  0.42"),
                         quote_line("Line 2", 0.48, "12 x 10 x 6", ev="Line 2  12 x 10 x 6  2000 pcs  0.48")])
        svc, _ = self.build([payload, match_payload([match("Line 1", "LINE-001"), match("Line 2", "LINE-002")])])
        resp, _ = self.register(svc, "Test Supplier", DOC_A)
        svc.extract_response(resp.id)
        silent = Supplier(name="Silent Supplier", status=SupplierStatus.NO_RESPONSE)
        svc.store.save_supplier(silent)
        svc.store.invite(self.rfq.id, silent.id, "no_response")

        s = svc.build_comparison(self.rfq.id).summary
        self.assertEqual(s["responses_received"], 1)
        self.assertEqual(s["no_response"], 1)
        self.assertEqual(s["suppliers_total"], 2)
        self.assertEqual(s["rfq_lines"], 7)
        self.assertEqual(s["line_responses"], 2)
        self.assertEqual(s["missing_quotes"], 12, "5 unquoted lines + 7 lines from the silent supplier")

    def test_a_silent_supplier_still_appears(self):
        svc, _ = self.build([])
        silent = Supplier(name="Silent Supplier", status=SupplierStatus.NO_RESPONSE)
        svc.store.save_supplier(silent)
        svc.store.invite(self.rfq.id, silent.id, "no_response")
        matrix = svc.build_comparison(self.rfq.id)
        self.assertIn("Silent Supplier", [s.name for s in matrix.suppliers])
        self.assertEqual(matrix.cell("LINE-001", silent.id).state, CellState.NO_RESPONSE)
        self.assertEqual(matrix.cell("LINE-001", silent.id).display, "no response")

    def test_a_supplier_invited_elsewhere_is_not_in_this_comparison(self):
        """The supplier directory is global; being asked to quote is not. Reading the
        directory wholesale once put a firm that never heard of this RFQ into its table
        as a column of missing quotes."""
        svc, _ = self.build([])
        other = svc.repo.get_rfq(self.rfq.id)
        other.id = "rfq_elsewhere"
        svc.repo.save_rfq(other)
        stranger = Supplier(name="Stranger Co", status=SupplierStatus.NO_RESPONSE)
        svc.store.save_supplier(stranger)
        svc.store.invite("rfq_elsewhere", stranger.id, "no_response")

        matrix = svc.build_comparison(self.rfq.id)
        self.assertNotIn("Stranger Co", [s.name for s in matrix.suppliers])
        self.assertEqual(matrix.summary["suppliers_total"], 0)
        self.assertIn("Stranger Co",
                      [s.name for s in svc.build_comparison("rfq_elsewhere").suppliers])

    def test_mixed_currencies_are_reported_not_converted(self):
        usd = extraction_payload(quote_lines=[quote_line("Line 1", 0.42, "10 x 10 x 5", currency="USD",
                                                         ev="Line 1  10 x 10 x 5  2000 pcs  0.42")])
        eur = extraction_payload(quote_lines=[quote_line("Line 1", 0.39, "10 x 10 x 5", currency="EUR",
                                                         ev="Line 1  10 x 10 x 5  2000 pcs  0.39")])
        svc, _ = self.build([usd, match_payload([match("Line 1", "LINE-001")]),
                             eur, match_payload([match("Line 1", "LINE-001")])])
        r1, _ = self.register(svc, "USD Supplier", "Line 1  10 x 10 x 5  2000 pcs  0.42", "a.txt")
        r2, _ = self.register(svc, "EUR Supplier", "Line 1  10 x 10 x 5  2000 pcs  0.39", "b.txt")
        svc.extract_response(r1.id)
        svc.extract_response(r2.id)
        matrix = svc.build_comparison(self.rfq.id)
        self.assertFalse(matrix.single_currency())
        self.assertEqual(sorted(matrix.currencies()), ["EUR", "USD"])
        cells = [matrix.cell("LINE-001", s.id).display for s in matrix.suppliers]
        self.assertTrue(any("USD" in c for c in cells) and any("EUR" in c for c in cells),
                        "each price keeps the currency the supplier used")

    def test_an_unsettled_price_is_flagged_in_the_table_not_only_below_it(self):
        """A positional guess divides just as cleanly as a confirmed price. Printing the
        bare number made the two indistinguishable on the screen most people read."""
        payload = extraction_payload(
            quote_lines=[quote_line("item 2", 0.42, None, ev="item 2  2000 pcs  0.42")])
        svc, _ = self.build([payload, match_payload([match("item 2", "LINE-002", confidence=0.5)])])
        resp, _ = self.register(svc, "Positional Supplier", "item 2  2000 pcs  0.42", "d.txt")
        svc.extract_response(resp.id)

        matrix = svc.build_comparison(self.rfq.id)
        flagged = [c for c in matrix.cells.values() if c.flag]
        self.assertTrue(flagged, "a needs-review quote must carry a flag")
        cell = flagged[0]
        self.assertEqual(cell.flag, "review")
        self.assertIn("·", cell.display)
        self.assertIn("review", cell.display)
        self.assertTrue(cell.display.startswith("USD"), "the price is still shown")

    def test_a_settled_price_carries_no_flag(self):
        payload = extraction_payload(
            quote_lines=[quote_line("Line 1", 0.42, "10 x 10 x 5", ev="Line 1  10 x 10 x 5  2000 pcs  0.42")])
        svc, _ = self.build([payload, match_payload([match("Line 1", "LINE-001")])])
        resp, _ = self.register(svc, "Clean Supplier", DOC_A)
        svc.extract_response(resp.id)
        cell = svc.build_comparison(self.rfq.id).cell("LINE-001", resp.supplier_id)
        self.assertEqual(cell.flag, "")
        self.assertNotIn("·", cell.display)

    def test_review_queue_surfaces_what_cannot_be_asserted(self):
        payload = extraction_payload(
            quote_lines=[quote_line("mystery item", 0.42, None, ev="Line 1  10 x 10 x 5  2000 pcs  0.42")],
            certifications=[{"name": "ISO 9001", "raw_name": "ISO 9001", "certificate_number": None,
                             "issuing_body": None, "expiry_date": None, "document_attached": False,
                             "document_reference": None, "evidence": evidence("ISO 9001 certified"),
                             "confidence": 0.9}],
            supplier_questions=[{"question": "Is 5-colour printing required?",
                                 "related_field_key": "printing", "evidence": evidence("ISO 9001 certified")}])
        svc, _ = self.build([payload, match_payload([match("mystery item", None, basis="none", confidence=0.1)])])
        resp, _ = self.register(svc, "Unclear Supplier", DOC_A)
        svc.extract_response(resp.id)
        kinds = {i["kind"] for i in svc.review_queue(self.rfq.id)}
        self.assertIn("unmatched", kinds)
        self.assertIn("unverified_claim", kinds)
        self.assertIn("supplier_question", kinds)

    def test_one_contradiction_is_listed_once_not_once_per_line(self):
        """A lead time stated twice affects every quoted line, but it is one problem."""
        conflict = {"topic": "lead time", "description": "15 days on page 1, 25 days on page 3",
                    "values": [{"value": "15 days", "evidence": evidence("lead time 15 days")},
                               {"value": "25 days", "evidence": evidence("lead time 25 days")}]}
        payload = extraction_payload(
            quote_lines=[quote_line("Line 1", 0.42, "10 x 10 x 5", ev="Line 1  10 x 10 x 5  2000 pcs  0.42"),
                         quote_line("Line 2", 0.48, "12 x 10 x 6", ev="Line 2  12 x 10 x 6  2000 pcs  0.48")],
            conflicts=[conflict])
        svc, _ = self.build([payload, match_payload([match("Line 1", "LINE-001"), match("Line 2", "LINE-002")])])
        resp, _ = self.register(svc, "Contradictory Supplier",
                                DOC_A + "\nlead time 15 days\nlead time 25 days")
        bundle = svc.extract_response(resp.id)

        self.assertEqual(len([q for q in bundle.quotes if q.conflicts]), 2,
                         "both quotes still carry the contradiction")
        entries = [i for i in svc.review_queue(self.rfq.id) if i["kind"] == "conflict"]
        self.assertEqual(len(entries), 1, "the buyer sees one problem, not one per line")
        self.assertEqual(entries[0]["affected_lines"], 2)
        self.assertEqual(len(entries[0]["values"]), 2, "both sides are shown")

    def test_answering_a_supplier_question_records_it_locally(self):
        payload = extraction_payload(
            quote_lines=[quote_line("Line 1", 0.42, "10 x 10 x 5", ev="Line 1  10 x 10 x 5  2000 pcs  0.42")],
            supplier_questions=[{"question": "Is 5-colour printing required?", "related_field_key": "printing",
                                 "evidence": evidence("Line 1  10 x 10 x 5  2000 pcs  0.42")}])
        svc, _ = self.build([payload, match_payload([match("Line 1", "LINE-001")])])
        resp, _ = self.register(svc, "Asking Supplier", DOC_A)
        bundle = svc.extract_response(resp.id)
        q = bundle.questions[0]
        self.assertFalse(q.resolved)
        out = svc.answer_supplier_question(resp.id, q.id, "Two colours only.")
        self.assertTrue(out.questions[0].resolved)
        self.assertEqual(out.questions[0].buyer_answer, "Two colours only.")


class OwnSupplierResponseTest(ServiceHarness):
    """A buyer's own RFQ has to be able to receive a buyer's own supplier replies.

    Until `add_response` existed the only route to quotes was the carton fixture set, so
    an RFQ for anything else got five carton quotations, read them correctly, matched
    nothing, and looked like broken extraction.
    """

    def _svc(self):
        payload = extraction_payload(
            quote_lines=[quote_line("Line 1", 0.42, "10 x 10 x 5",
                                    ev="Line 1  10 x 10 x 5  2000 pcs  0.42")])
        svc, _ = self.build([payload, match_payload([match("Line 1", "LINE-001")])])
        return svc

    def test_an_uploaded_response_is_registered_read_and_priced(self):
        svc = self._svc()
        resp = svc.add_response(self.rfq.id, "Acme Cylinders",
                                [("acme_quote.txt", DOC_A.encode("utf-8"))],
                                contact_email="sales@acme.example")
        svc.extract_response(resp.id)
        matrix = svc.build_comparison(self.rfq.id)
        self.assertIn("Acme Cylinders", [s.name for s in matrix.suppliers])
        cell = matrix.cell("LINE-001", resp.supplier_id)
        self.assertEqual(cell.display, "USD 0.42")

    def test_the_uploaded_file_survives_so_it_can_be_read_again(self):
        """The documents behind a registered response are part of the record: re-running
        extraction next week must not depend on a temp folder that has been swept."""
        svc = self._svc()
        resp = svc.add_response(self.rfq.id, "Acme Cylinders",
                                [("acme_quote.txt", DOC_A.encode("utf-8"))])
        doc = svc.store.get_bundle(resp.id).documents[0]
        self.assertTrue(os.path.exists(doc.path), "the document must still be on disk")
        with open(doc.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), DOC_A)

    def test_a_response_needs_a_named_supplier_and_a_document(self):
        from rfq_copilot.rfq_service import RFQStateError
        svc = self._svc()
        with self.assertRaises(RFQStateError):
            svc.add_response(self.rfq.id, "   ", [("q.txt", b"x")])
        with self.assertRaises(RFQStateError):
            svc.add_response(self.rfq.id, "Acme Cylinders", [])

    def test_a_second_reply_from_one_supplier_supersedes_the_first(self):
        payload = extraction_payload(
            quote_lines=[quote_line("Line 1", 0.42, "10 x 10 x 5",
                                    ev="Line 1  10 x 10 x 5  2000 pcs  0.42")])
        svc, _ = self.build([payload, match_payload([match("Line 1", "LINE-001")]),
                             payload, match_payload([match("Line 1", "LINE-001")])])
        first = svc.add_response(self.rfq.id, "Acme Cylinders", [("q1.txt", DOC_A.encode())])
        second = svc.add_response(self.rfq.id, "Acme Cylinders", [("q2.txt", DOC_A.encode())])
        svc.extract_response(first.id)
        svc.extract_response(second.id)
        active = [r for r in svc.store.list_responses(self.rfq.id) if r.is_active]
        self.assertEqual([r.id for r in active], [second.id])
        self.assertEqual(len(svc.store.list_responses(self.rfq.id)), 2,
                         "the earlier reply is kept, not deleted")

    def test_a_pasted_email_is_a_document_like_any_other(self):
        """Most replies arrive as an email. Requiring a file first meant a buyer had to
        save it to disk before the system would look at it."""
        svc = self._svc()
        resp = svc.add_response(self.rfq.id, "Acme Cylinders",
                                [("acme_email.txt", DOC_A.encode("utf-8"))])
        svc.extract_response(resp.id)
        cell = svc.build_comparison(self.rfq.id).cell("LINE-001", resp.supplier_id)
        self.assertEqual(cell.display, "USD 0.42")

    def test_a_response_can_be_taken_back_out_again(self):
        """Loading documents onto the wrong RFQ was a one-way door."""
        svc = self._svc()
        resp = svc.add_response(self.rfq.id, "Acme Cylinders", [("q.txt", DOC_A.encode())])
        svc.extract_response(resp.id)
        self.assertTrue(svc.build_comparison(self.rfq.id).suppliers)

        name = svc.remove_response(resp.id)
        self.assertEqual(name, "Acme Cylinders")
        self.assertEqual(svc.store.list_responses(self.rfq.id), [])
        self.assertIsNone(svc.store.get_bundle(resp.id))
        self.assertFalse(svc.build_comparison(self.rfq.id).suppliers)

    def test_removing_a_revision_makes_the_earlier_reply_active_again(self):
        payload = extraction_payload(
            quote_lines=[quote_line("Line 1", 0.42, "10 x 10 x 5",
                                    ev="Line 1  10 x 10 x 5  2000 pcs  0.42")])
        svc, _ = self.build([payload, match_payload([match("Line 1", "LINE-001")]),
                             payload, match_payload([match("Line 1", "LINE-001")])])
        first = svc.add_response(self.rfq.id, "Acme Cylinders", [("q1.txt", DOC_A.encode())])
        second = svc.add_response(self.rfq.id, "Acme Cylinders", [("q2.txt", DOC_A.encode())])
        svc.extract_response(first.id)
        svc.extract_response(second.id)

        svc.remove_response(second.id)
        active = [r for r in svc.store.list_responses(self.rfq.id) if r.is_active]
        self.assertEqual([r.id for r in active], [first.id],
                         "the earlier reply must come back rather than leaving a gap")

    def test_the_supplier_is_scoped_to_this_rfq_only(self):
        svc = self._svc()
        other = svc.repo.get_rfq(self.rfq.id)
        other.id = "rfq_somewhere_else"
        svc.repo.save_rfq(other)
        svc.add_response(self.rfq.id, "Acme Cylinders", [("q.txt", DOC_A.encode())])
        self.assertNotIn("Acme Cylinders",
                         [s.name for s in svc.build_comparison("rfq_somewhere_else").suppliers])


class SimulatedResponsesTest(ServiceHarness):
    """Sample replies written for whatever the RFQ is actually about.

    The committed fixtures quote carton boxes, which is the right demo for a carton RFQ
    and useless for any other — so a buyer who had just built an RFQ for something else
    had no way to see their own requirement work.
    """

    def _payload(self, n=3):
        return {"note": "for a demonstration",
                "suppliers": [{"supplier_name": "Supplier %d" % i, "country": "India",
                               "style": "email",
                               "document_text": "Quotation\nLine 1  10 x 10 x 5  2000 pcs  0.4%d\n"
                                                "MOQ 2000. Lead time 20 days." % i}
                              for i in range(1, n + 1)]}

    def test_it_writes_one_response_per_supplier_and_registers_them(self):
        svc, ai = self.build([self._payload(3)])
        created = svc.simulate_responses(self.rfq.id, count=3)
        self.assertEqual(len(created), 3)
        names = {b.supplier.name for b in svc.bundles_for(self.rfq.id)}
        self.assertEqual(names, {"Supplier 1", "Supplier 2", "Supplier 3"})

    def test_the_prompt_describes_this_rfq_and_not_the_carton_fixtures(self):
        svc, ai = self.build([self._payload(2)])
        svc.simulate_responses(self.rfq.id, count=2)
        prompt = ai.calls[0]["prompt"]
        self.assertIn(self.rfq.product, prompt)
        self.assertIn("10 x 10 x 5", prompt, "the real line items must reach the prompt")

    def test_the_documents_are_kept_so_the_evidence_still_resolves(self):
        svc, _ = self.build([self._payload(1)])
        created = svc.simulate_responses(self.rfq.id, count=1)
        doc = svc.store.get_bundle(created[0].id).documents[0]
        self.assertTrue(os.path.exists(doc.path))

    def test_an_rfq_with_no_line_items_is_refused_with_a_reason(self):
        from rfq_copilot.rfq_service import RFQStateError
        svc, _ = self.build([])
        empty = svc.repo.get_rfq(self.rfq.id)
        empty.line_items = []
        svc.repo.save_rfq(empty)
        with self.assertRaises(RFQStateError) as caught:
            svc.simulate_responses(self.rfq.id)
        self.assertIn("nothing for a supplier to quote", str(caught.exception))


class ConflictResolutionTest(ServiceHarness):
    """A contradiction the system will not arbitrate, the buyer can settle."""

    def _conflicted(self):
        conflict = {"topic": "Production lead time",
                    "description": "18 days on page 1, 30 days in peak season on page 3",
                    "values": [{"value": "18 days", "evidence": evidence("18 days")},
                               {"value": "30 days", "evidence": evidence("30 days")}]}
        payload = extraction_payload(
            quote_lines=[quote_line("Line 1", 0.42, "10 x 10 x 5",
                                    ev="Line 1  10 x 10 x 5  2000 pcs  0.42")],
            conflicts=[conflict])
        svc, _ = self.build([payload, match_payload([match("Line 1", "LINE-001")])])
        resp, _ = self.register(svc, "Contradicting Supplier", DOC_A)
        svc.extract_response(resp.id)
        return svc, resp

    def test_an_open_contradiction_is_listed_for_review(self):
        svc, resp = self._conflicted()
        items = [i for i in svc.review_queue(self.rfq.id) if i["kind"] == "conflict"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["values"], ["18 days", "30 days"])
        self.assertFalse(items[0]["resolution"])

    def test_recording_which_value_applies_settles_it(self):
        svc, resp = self._conflicted()
        svc.resolve_conflict(resp.id, "Production lead time", "18 days",
                             note="Supplier confirmed by email")
        items = [i for i in svc.review_queue(self.rfq.id) if i["kind"] == "conflict"]
        self.assertEqual(items[0]["resolution"]["value"], "18 days")
        self.assertEqual(items[0]["resolution"]["by"], "buyer")
        bundle = svc.store.get_bundle(resp.id)
        self.assertNotIn("conflicts", bundle.issue_counts())
        self.assertFalse(bundle.needs_review())

    def test_both_stated_values_survive_the_decision(self):
        svc, resp = self._conflicted()
        svc.resolve_conflict(resp.id, "Production lead time", "18 days")
        conflict = svc.store.get_bundle(resp.id).quotes[0].conflicts[0]
        self.assertEqual([v["value"] for v in conflict["values"]], ["18 days", "30 days"],
                         "a decision records what the buyer chose; it deletes nothing")

    def test_the_headline_count_drops_when_the_buyer_settles_something(self):
        """A settled item stays in the queue, shown as settled, but it is no longer
        waiting on anyone — so the number on the dashboard has to move."""
        svc, resp = self._conflicted()
        before = svc.build_comparison(self.rfq.id).summary["review_items_total"]
        svc.resolve_conflict(resp.id, "Production lead time", "18 days")
        after = svc.build_comparison(self.rfq.id).summary["review_items_total"]
        self.assertEqual(after, before - 1)
        self.assertTrue([i for i in svc.review_queue(self.rfq.id) if i["kind"] == "conflict"],
                        "it is still listed, so the record of the decision is visible")

    def test_a_resolution_needs_a_value_and_a_real_topic(self):
        from rfq_copilot.rfq_service import RFQStateError
        svc, resp = self._conflicted()
        with self.assertRaises(RFQStateError):
            svc.resolve_conflict(resp.id, "Production lead time", "   ")
        with self.assertRaises(RFQStateError):
            svc.resolve_conflict(resp.id, "Payment terms", "30 days")


class Phase1StillWorksTest(unittest.TestCase):
    def test_phase2_tables_do_not_disturb_the_rfq_store(self):
        tmp = tempfile.mkdtemp()
        repo = RFQRepository(os.path.join(tmp, "t.db"))
        rfq = carton_rfq()
        repo.save_rfq(rfq)
        back = repo.get_rfq(rfq.id)
        self.assertEqual(len(back.line_items), 7)
        self.assertEqual([r.id for r in repo.list_rfqs()], [rfq.id])


if __name__ == "__main__":
    unittest.main()


class ExchangeRateTest(unittest.TestCase):
    """Conversion is allowed, but only with a real rate that can be cited."""

    def _table(self):
        from rfq_copilot.fx import RateTable
        import time
        return RateTable(base="USD", rates={"EUR": 0.86, "INR": 95.5}, source="test-provider",
                         as_of="2026-09-11", fetched_at=time.time())

    def test_conversion_keeps_the_original_and_names_the_rate(self):
        from rfq_copilot.fx import convert_amount
        c = convert_amount(self._table(), 0.39, "EUR", "USD")
        self.assertAlmostEqual(c.amount, 0.39 / 0.86, places=6)
        self.assertEqual(c.original_amount, 0.39)
        self.assertEqual(c.original_currency, "EUR")
        self.assertIn("test-provider", c.describe_rate())
        self.assertIn("2026-09-11", c.describe_rate())

    def test_an_unknown_currency_is_never_converted(self):
        from rfq_copilot.fx import convert_amount
        self.assertIsNone(convert_amount(self._table(), 41.0, "CENTS", "USD"),
                          "a currency we cannot name has no rate")

    def test_no_rate_table_means_no_conversion_rather_than_a_guess(self):
        from rfq_copilot.fx import convert_amount
        self.assertIsNone(convert_amount(None, 0.39, "EUR", "USD"))

    def test_a_failed_fetch_never_invents_a_rate(self):
        from rfq_copilot.fx import FxService
        def broken(req, timeout=None):
            raise OSError("network down")
        t = FxService(cache_path=None, opener=broken).rates("USD")
        self.assertFalse(t.ok)
        self.assertEqual(t.rates, {})
        self.assertIsNone(t.rate("EUR", "USD"))

    def test_same_currency_needs_no_rate(self):
        from rfq_copilot.fx import convert_amount
        c = convert_amount(None, 0.42, "USD", "USD")
        self.assertEqual(c.amount, 0.42)
        self.assertFalse(c.is_conversion)


class ComparisonCurrencyTest(ServiceHarness):
    def test_prices_convert_on_request_and_keep_their_origin(self):
        import time
        from rfq_copilot.fx import RateTable
        usd = extraction_payload(quote_lines=[quote_line("Line 1", 0.42, "10 x 10 x 5", currency="USD",
                                                         ev="Line 1  10 x 10 x 5  2000 pcs  0.42")])
        eur = extraction_payload(quote_lines=[quote_line("Line 1", 0.39, "10 x 10 x 5", currency="EUR",
                                                         ev="Line 1  10 x 10 x 5  2000 pcs  0.39")])
        svc, _ = self.build([usd, eur])
        r1, s1 = self.register(svc, "USD Supplier", "Line 1  10 x 10 x 5  2000 pcs  0.42", "a.txt")
        r2, s2 = self.register(svc, "EUR Supplier", "Line 1  10 x 10 x 5  2000 pcs  0.39", "b.txt")
        svc.extract_response(r1.id)
        svc.extract_response(r2.id)

        # a fixed table so the assertion does not depend on today's market
        svc.fx._memory["USD"] = RateTable(base="USD", rates={"EUR": 0.86}, source="test-provider",
                                          as_of="2026-09-11", fetched_at=time.time())

        native = svc.build_comparison(self.rfq.id)
        self.assertIn("EUR", native.cell("LINE-001", s2.id).display)

        converted = svc.build_comparison(self.rfq.id, display_currency="USD")
        cell = converted.cell("LINE-001", s2.id)
        self.assertTrue(cell.display.startswith("USD"), "the table shows the chosen currency")
        self.assertIn("EUR", cell.native_display, "the supplier's own figure is still available")
        self.assertEqual(cell.quote.currency, "EUR", "the stored quote is untouched")
        self.assertAlmostEqual(cell.converted.amount, 0.39 / 0.86, places=6)
        self.assertEqual(converted.summary["rate_source"], "test-provider")
        self.assertEqual(converted.summary["unconvertible"], 0)

    def test_an_unnamed_currency_is_counted_as_unconvertible_not_converted(self):
        import time
        from rfq_copilot.fx import RateTable
        payload = extraction_payload(quote_lines=[quote_line("Line 1", 41.0, "10 x 10 x 5", currency="cents",
                                                             ev="Line 1  10 x 10 x 5  2000 pcs  41.0")])
        svc, _ = self.build([payload])
        resp, supplier = self.register(svc, "Vague Supplier", "Line 1  10 x 10 x 5  2000 pcs  41.0")
        svc.extract_response(resp.id)
        svc.fx._memory["USD"] = RateTable(base="USD", rates={"EUR": 0.86}, source="test-provider",
                                          as_of="2026-09-11", fetched_at=time.time())
        m = svc.build_comparison(self.rfq.id, display_currency="USD")
        cell = m.cell("LINE-001", supplier.id)
        self.assertIsNone(cell.converted, "an unnamed currency has no rate to convert with")
        self.assertEqual(cell.display, "unresolved")
