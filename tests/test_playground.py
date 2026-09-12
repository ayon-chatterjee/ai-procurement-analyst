"""Requirement Playground: extraction, the adapter into an RFQ, and error handling.

The adapter is what matters most here. Once requirements become an RFQ, Phase 1's
completeness rules and Phase 2's supplier engine apply unchanged, so these tests check
that the conversion is faithful rather than re-testing either engine.
"""
from __future__ import annotations

import os
import tempfile
import unittest

from rfq_copilot.config import Settings
from rfq_copilot.persistence import RFQRepository
from rfq_copilot.playground_schemas import PLAYGROUND_EXTRACTION_SCHEMA
from rfq_copilot.playground_service import PlaygroundError, PlaygroundService
from rfq_copilot.schema import FieldStatus, Importance, RFQStatus, Source
from rfq_copilot.supplier_models import ExtractionStatus
from tests.supplier_helpers import ScriptedAI

EMAIL = """Subject: Need quotation for brackets

Hi,
Please send your best quote for 500 SS304 brackets.
Thickness should be 3mm and finish should be powder coated.
Need delivery within 2 weeks.

Thanks"""


def requirement(field_key, label, value, unit=None, status="found", kind="email_text",
                document=None, location=None, quoted="", note=None, confidence=0.9):
    return {"field_key": field_key, "label": label, "value": value, "unit": unit, "status": status,
            "source_kind": kind, "source_document": document, "source_location": location,
            "quoted_text": quoted, "note": note, "confidence": confidence}


def line_item(name, qty=None, unit=None, specs=None, reqs=None, ambiguities=None,
              evidence="", description=""):
    return {"name": name, "description": description, "quantity": qty, "unit": unit,
            "specifications": specs or [], "requirements": reqs or [],
            "ambiguities": ambiguities or [], "evidence": evidence}


def payload(**over):
    base = {
        "classification": {"product": "SS304 bracket", "category": "Metal fabrication",
                           "product_type": "Sheet metal bracket", "confidence": 0.9},
        "title": "Quotation for SS304 brackets",
        "summary": "500 stainless brackets, 3mm, powder coated, needed in two weeks.",
        "line_items": [],
        "shared_requirements": [],
        "ambiguities": [],
        "nothing_found_reason": None,
    }
    base.update(over)
    from rfq_copilot.ai_service import validate_against
    err = validate_against(PLAYGROUND_EXTRACTION_SCHEMA, base)
    assert err is None, "test payload is not schema-valid: %s" % err
    return base


def build(payloads):
    tmp = tempfile.mkdtemp(prefix="pg_")
    settings = Settings()
    settings.db_path = os.path.join(tmp, "t.db")
    ai = ScriptedAI(payloads)
    return PlaygroundService(ai, RFQRepository(settings.db_path), settings), ai


BRACKET = payload(line_items=[line_item(
    "SS304 bracket", qty=500, unit="pcs", evidence="500 SS304 brackets",
    specs=[{"name": "Material", "value": "SS304", "unit": None},
           {"name": "Thickness", "value": "3", "unit": "mm"}],
    reqs=[requirement("material_grade", "Material grade", "SS304", quoted="500 SS304 brackets"),
          requirement("thickness", "Thickness", "3", unit="mm", quoted="Thickness should be 3mm"),
          requirement("surface_finish", "Finish", "powder coated", quoted="finish should be powder coated")])],
    shared_requirements=[requirement("required_delivery_date", "Required delivery date", "2 weeks",
                                     quoted="Need delivery within 2 weeks")])


class ExtractionTest(unittest.TestCase):
    def test_a_plain_email_becomes_structured_requirements(self):
        svc, ai = build([BRACKET])
        result = svc.analyze(EMAIL)
        self.assertEqual(len(result.line_items), 1)
        li = result.line_items[0]
        self.assertEqual(li.name, "SS304 bracket")
        self.assertEqual(li.quantity, 500)
        self.assertEqual({r.label for r in li.requirements},
                         {"Material grade", "Thickness", "Finish"})
        self.assertEqual(len(ai.calls), 1, "one structured call")
        self.assertIn("Need quotation for brackets", ai.calls[0]["prompt"], "the email is passed as data")

    def test_evidence_is_checked_against_what_was_actually_sent(self):
        svc, _ = build([BRACKET])
        result = svc.analyze(EMAIL)
        by_label = {r.label: r for r in result.line_items[0].requirements}
        self.assertTrue(by_label["Thickness"].verified, "a real span is verified")

        made_up = payload(line_items=[line_item(
            "SS304 bracket", qty=500, evidence="500 SS304 brackets",
            reqs=[requirement("tolerance", "Tolerance", "±0.1mm", quoted="tolerance of ±0.1mm")])])
        svc2, _ = build([made_up])
        r2 = svc2.analyze(EMAIL)
        self.assertFalse(r2.line_items[0].requirements[0].verified,
                         "a span absent from the email is flagged, not trusted")

    def test_a_source_file_that_was_never_attached_is_rejected(self):
        claimed = payload(line_items=[line_item(
            "SS304 bracket", qty=500, evidence="500 SS304 brackets",
            reqs=[requirement("thickness", "Thickness", "3", unit="mm", kind="attachment",
                              document="drawing.pdf", location="Page 2",
                              quoted="Thickness should be 3mm")])])
        svc, _ = build([claimed])
        result = svc.analyze(EMAIL)          # no attachments were supplied
        r = result.line_items[0].requirements[0]
        self.assertEqual(r.source_kind, "email_text")
        self.assertEqual(r.source_document, "", "a file the user never sent cannot be a source")

    def test_nothing_to_extract_is_reported_not_invented(self):
        empty = payload(line_items=[], nothing_found_reason="This is a meeting invitation, not a request.")
        svc, _ = build([empty])
        result = svc.analyze("Lunch at 1pm?")
        self.assertFalse(result.found_anything)
        self.assertIn("meeting invitation", result.nothing_found_reason)

    def test_empty_input_is_refused_before_any_model_call(self):
        svc, ai = build([])
        with self.assertRaises(PlaygroundError):
            svc.analyze("   ")
        self.assertEqual(ai.calls, [], "no model call is made for an empty request")

    def test_an_unreadable_attachment_with_no_text_is_reported(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "legacy.xls")
        with open(path, "wb") as f:
            f.write(b"\xd0\xcf\x11\xe0 old binary")
        svc, ai = build([])
        with self.assertRaises(PlaygroundError) as ctx:
            svc.analyze("", [path])
        self.assertIn("None of the attachments could be read", str(ctx.exception))
        self.assertIn("Re-save it as .xlsx or .csv", str(ctx.exception),
                      "the message tells the user what to do about it")
        self.assertEqual(ai.calls, [], "nothing is sent to the model when nothing could be read")

    def test_attachments_are_read_with_the_existing_reader(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "req.csv")
        with open(path, "w") as f:
            f.write("Item,Qty,Material\nBracket,500,SS304\n")
        svc, ai = build([BRACKET])
        result = svc.analyze("See attached", [path])
        self.assertEqual(len(result.sources), 1)
        self.assertTrue(result.sources[0].readable)
        self.assertEqual(result.sources[0].media_type, "csv")
        self.assertIn("SS304", ai.calls[0]["prompt"], "attachment content reaches the model")


class DisplayTest(unittest.TestCase):
    def test_a_unit_already_in_the_value_is_not_repeated(self):
        from rfq_copilot.playground_service import Requirement
        cases = [("+/- 0.15 mm", "mm", "+/- 0.15 mm"),
                 ("3", "mm", "3 mm"),
                 ("500", "pcs", "500 pcs"),
                 ("2 weeks", "weeks", "2 weeks"),
                 ("SS304", None, "SS304")]
        for value, unit, expected in cases:
            r = Requirement(field_key="k", label="L", value=value, unit=unit)
            self.assertEqual(r.display_value(), expected, "%r + %r" % (value, unit))


class AdapterTest(unittest.TestCase):
    """Extracted requirements must become an RFQ the rest of the app already understands."""

    def test_line_items_and_specifications_carry_over(self):
        svc, _ = build([BRACKET])
        rfq = svc.to_rfq(svc.analyze(EMAIL))
        self.assertEqual(len(rfq.line_items), 1)
        li = rfq.line_items[0]
        self.assertEqual(li.id, "LINE-001")
        self.assertEqual(li.quantity, 500)
        self.assertEqual(li.source, Source.BUYER_EXPLICIT)
        names = {s.name for s in li.specifications}
        self.assertIn("Thickness", names)
        self.assertIn("Material", names)
        self.assertIn("Finish", names, "spec-like requirements move onto the line for matching")

    def test_universal_fields_are_populated_with_their_provenance(self):
        svc, _ = build([BRACKET])
        rfq = svc.to_rfq(svc.analyze(EMAIL))
        fv = rfq.fields["required_delivery_date"]
        self.assertEqual(fv.status, FieldStatus.PROVIDED)
        self.assertEqual(fv.source, Source.BUYER_EXPLICIT)
        self.assertEqual(fv.value, "2 weeks", "the buyer's wording is kept, not turned into a date")
        self.assertTrue(fv.source_refs and fv.source_refs[0].startswith("playground"))
        self.assertTrue(fv.evidence)

    def test_missing_information_comes_from_the_existing_rules(self):
        svc, _ = build([BRACKET])
        rfq = svc.to_rfq(svc.analyze(EMAIL))
        missing = rfq.completeness.missing_required_fields
        self.assertTrue(missing, "an email this short cannot be complete")
        self.assertIn("Destination", missing, "a required field nobody mentioned is missing")
        self.assertFalse(rfq.completeness.ready_to_send)
        for label in missing:
            self.assertNotIn("Thickness", label, "something that was stated is never listed as missing")

    def test_an_ambiguous_requirement_does_not_satisfy_a_field(self):
        vague = payload(line_items=[line_item(
            "Rubber gasket", evidence="Some rubber gaskets",
            reqs=[requirement("quantity", "Order quantity", "a few hundred", status="ambiguous",
                              quoted="a few hundred should do",
                              note="No firm number; suppliers would quote different volumes.")])])
        svc, _ = build([vague])
        rfq = svc.to_rfq(svc.analyze("Some rubber gaskets, a few hundred should do"))
        fv = rfq.fields["quantity"]
        self.assertNotEqual(fv.status, FieldStatus.PROVIDED, "vague wording is not a settled fact")
        self.assertEqual(fv.value, "a few hundred", "but the buyer's words are kept")
        self.assertTrue([q for q in rfq.questions if q.field_key == "quantity"],
                        "an ambiguity leaves a question behind")

    def test_several_line_items_get_stable_sequential_ids(self):
        multi = payload(line_items=[
            line_item("SS304 bracket", qty=500, evidence="500 SS304 brackets"),
            line_item("Aluminium housing", qty=200, evidence="200 aluminium housings"),
            line_item("Rubber gasket", evidence="rubber gaskets")])
        svc, _ = build([multi])
        rfq = svc.to_rfq(svc.analyze("three things"))
        self.assertEqual([li.id for li in rfq.line_items], ["LINE-001", "LINE-002", "LINE-003"])
        self.assertEqual(rfq.line_seq, 3)
        gaps = rfq.completeness.missing_required_fields
        self.assertTrue(any("LINE-003" in g for g in gaps),
                        "the item with no quantity is reported as an incomplete line")

    def test_saving_produces_an_rfq_phase_2_can_load(self):
        svc, _ = build([BRACKET])
        result = svc.analyze(EMAIL)
        rfq = svc.save_as_rfq(result)
        self.assertEqual(result.rfq_id, rfq.id)
        back = svc.repo.get_rfq(rfq.id)
        self.assertIsNotNone(back)
        self.assertEqual(len(back.line_items), 1)
        self.assertEqual(back.product, "SS304 bracket")
        self.assertEqual([r.id for r in svc.repo.list_rfqs()], [rfq.id])

    def test_nothing_is_persisted_until_the_user_saves(self):
        svc, _ = build([BRACKET])
        svc.analyze(EMAIL)
        self.assertEqual(svc.repo.list_rfqs(), [], "analysis alone writes nothing to the database")

    def test_sending_an_empty_result_onward_is_refused(self):
        empty = payload(line_items=[], nothing_found_reason="Nothing to buy here.")
        svc, _ = build([empty])
        with self.assertRaises(PlaygroundError):
            svc.to_rfq(svc.analyze("hello"))


class Phase1And2UntouchedTest(unittest.TestCase):
    def test_the_playground_rfq_is_an_ordinary_rfq(self):
        """Anything the playground produces must satisfy the same invariants as a copilot RFQ."""
        from rfq_copilot.schema import rfq_from_json, rfq_to_json
        svc, _ = build([BRACKET])
        rfq = svc.to_rfq(svc.analyze(EMAIL))
        self.assertEqual(rfq_from_json(rfq_to_json(rfq)).to_dict(), rfq.to_dict())
        self.assertIn(rfq.status, (RFQStatus.DRAFT, RFQStatus.IN_PROGRESS, RFQStatus.READY))
        self.assertTrue(all(fv.key for fv in rfq.fields.values()))

    def test_supplier_service_accepts_a_playground_rfq(self):
        from rfq_copilot.supplier_service import SupplierService
        svc, ai = build([BRACKET])
        rfq = svc.save_as_rfq(svc.analyze(EMAIL))
        sup = SupplierService(svc.repo, ai, svc.settings)
        matrix = sup.build_comparison(rfq.id)
        self.assertEqual(matrix.summary["rfq_lines"], 1)
        self.assertEqual(matrix.summary["responses_received"], 0)
        self.assertFalse(sup.has_responses(rfq.id))


if __name__ == "__main__":
    unittest.main()
