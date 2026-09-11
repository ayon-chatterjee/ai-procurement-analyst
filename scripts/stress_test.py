"""30-line stress test: does the comparison stay usable at scale?

Builds a 30-line RFQ, runs the five stress fixtures through the real pipeline, and
checks the properties that matter when the table gets big: coverage is reported
honestly, matching does not drift onto neighbouring sizes, gaps stay gaps, and every
price still carries its source.

Usage:  python3 scripts/stress_test.py
"""
from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from rfq_copilot.ai_service import AIError, AIUsageLimit, get_ai_service  # noqa: E402
from rfq_copilot.config import Settings  # noqa: E402
from rfq_copilot.fields import new_field_set  # noqa: E402
from rfq_copilot.line_matcher import extract_dimensions  # noqa: E402
from rfq_copilot.persistence import RFQRepository  # noqa: E402
from rfq_copilot.schema import (  # noqa: E402
    RFQ, Importance, LineItem, Question, RFQStatus, Section, Source, SpecAttr,
)
from rfq_copilot.supplier_models import (  # noqa: E402
    ExtractionStatus, MatchStatus, NormalizationStatus, QuoteStatus, Supplier, SupplierStatus,
)
from rfq_copilot.supplier_service import CellState, SupplierService  # noqa: E402
from scripts.make_stress_fixtures import OUT as STRESS_DIR, QTY, SIZES  # noqa: E402

RFQ_ID = "rfq_stress_30"
SUPPLIERS = [
    ("Anhui Packaging Co", "China", "stress_a_quote.xlsx"),
    ("Shenzhen Print & Pack", "China", "stress_b_quote.pdf"),
    ("Viet Carton JSC", "Vietnam", "stress_c_response.docx"),
    ("Gujarat Boxes Pvt Ltd", "India", "stress_d_response.txt"),
    ("Istanbul Ambalaj", "Turkey", "stress_e_quote.png"),
]

failures = []


def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        failures.append(msg)


def build_rfq(repo) -> RFQ:
    rfq = RFQ(id=RFQ_ID, title="Corrugated Carton Boxes — 30 sizes", product="Corrugated Carton Boxes",
              category="Packaging", product_type="Corrugated shipping cartons",
              fields=new_field_set(), status=RFQStatus.SUPPLIER_READY, turn=4)
    for i, size in enumerate(SIZES, start=1):
        rfq.line_items.append(LineItem(
            id="LINE-%03d" % i, product="Corrugated carton",
            specifications=[SpecAttr("Dimensions", size, "in")], quantity=QTY, unit="pcs",
            source=Source.BUYER_EXPLICIT, evidence=size))
    rfq.line_seq = len(SIZES)
    rfq.questions = [
        Question(id="q_cert", category=Section.QUALITY, question="Which certifications can you provide?",
                 field_key="certifications", importance=Importance.RECOMMENDED),
        Question(id="q_pay", category=Section.COMMERCIAL, question="What payment terms do you offer?",
                 field_key="payment_terms", importance=Importance.RECOMMENDED),
        Question(id="q_lead", category=Section.LOGISTICS, question="What is your production lead time?",
                 field_key="required_delivery_date", importance=Importance.REQUIRED),
    ]
    repo.save_rfq(rfq)
    return rfq


def main() -> int:
    settings = Settings.from_env()
    repo = RFQRepository(settings.db_path)
    svc = SupplierService(repo, get_ai_service(settings), settings)
    rfq = build_rfq(repo)
    print("RFQ %s — %d lines, %d pcs each\n" % (rfq.id, len(rfq.line_items), QTY))

    svc.store.delete_responses_for(RFQ_ID)
    existing = {s.name: s for s in svc.store.list_suppliers()}
    for name, country, filename in SUPPLIERS:
        supplier = existing.get(name) or Supplier(name=name, country=country)
        supplier.status = SupplierStatus.RESPONDED
        svc.store.save_supplier(supplier)
        svc._register(RFQ_ID, supplier, [filename], STRESS_DIR, received_at="2026-09-22")

    t = time.time()
    try:
        res = svc.extract_all(RFQ_ID, on_stage=lambda n, s: print("   [%s] %s" % (n, s)), only_pending=True)
    except AIUsageLimit as e:
        print("\n  STOPPED  %s" % e.user_message)
        return 2
    except AIError as e:
        print("\n  ERROR    %s" % e.user_message)
        return 1
    print("\nextracted in %.0fs: %d ok, %d failed\n" % (time.time() - t, len(res["succeeded"]), len(res["failed"])))

    matrix = svc.build_comparison(RFQ_ID)
    bundles = {b.supplier.name: b for b in svc.bundles_for(RFQ_ID, active_only=True) if b.supplier}
    s = matrix.summary

    print("=== coverage per supplier ===")
    for name, b in sorted(bundles.items()):
        priced = [q for q in b.quotes if q.has_price]
        matched = {q.line_item_id for q in priced if q.line_item_id}
        print("  %-24s %2d priced, %2d matched of %d  (%s)"
              % (name, len(priced), len(matched), len(rfq.line_items), b.response.extraction_status.value))
        check(len(matched) <= len(rfq.line_items), "%s: never more matches than RFQ lines" % name)

    print("\n=== 1. the table is complete and every cell has a state ===")
    expected = len(rfq.line_items) * len(matrix.suppliers)
    check(sum(s["cells"].values()) == expected, "%d cells all have a state" % expected)
    check(s["rfq_lines"] == 30, "30 RFQ lines")

    print("\n=== 2. matching did not drift onto a neighbouring size ===")
    by_id = {li.id: li for li in rfq.line_items}
    drift = []
    for b in bundles.values():
        for q in b.quotes:
            if not (q.line_item_id and q.has_price and q.match_status == MatchStatus.MATCHED):
                continue
            sup_dims = extract_dimensions(q.supplier_line_label)
            rfq_dims = extract_dimensions(by_id[q.line_item_id].spec_summary() or "")
            if sup_dims and rfq_dims and sorted(sup_dims) != sorted(rfq_dims):
                drift.append("%s: %r -> %s" % (b.supplier.name, q.supplier_line_label, q.line_item_id))
    for d in drift[:5]:
        print("      %s" % d)
    check(not drift, "no confirmed match contradicts its own stated dimensions (%d suspect)" % len(drift))

    print("\n=== 3. gaps stay gaps ===")
    not_quoted = [c for c in matrix.cells.values() if c.state == CellState.NOT_QUOTED]
    check(len(not_quoted) > 30, "sparse coverage is visible (%d not-quoted cells)" % len(not_quoted))
    check(all(c.quote is None or not c.quote.has_price for c in not_quoted), "no gap holds a price")
    check(all(c.display in ("not quoted", "no response") for c in not_quoted), "gaps read as words, not numbers")

    print("\n=== 4. every asserted price is traceable ===")
    asserted = [(b, q) for b in bundles.values() for q in b.quotes
                if q.status == QuoteStatus.QUOTED and q.has_price]
    untraceable = [(b.supplier.name, q.supplier_line_label) for b, q in asserted
                   if not any(b.evidence.get(e) and b.evidence[e].verified for e in q.evidence_ids)]
    for u in untraceable[:5]:
        print("      %s / %s" % u)
    check(not untraceable, "all %d asserted prices carry verified evidence (%d without)"
          % (len(asserted), len(untraceable)))

    print("\n=== 5. units and currencies are still separated at scale ===")
    print("      currencies: %s" % ", ".join(s["currencies"]))
    check(len(s["currencies"]) >= 2, "more than one currency present")
    per_1000 = [q for b in bundles.values() for q in b.quotes if q.price_basis.value == "per_1000"]
    check(bool(per_1000), "a per-1000 basis was recognised (%d lines)" % len(per_1000))
    if per_1000:
        q = per_1000[0]
        check(q.normalized_unit_price is not None and q.normalized_unit_price < q.unit_price,
              "per-1000 normalised: %s -> %s" % (q.original_price_text(), q.normalized_price_text()))
    unresolved = [q for b in bundles.values() for q in b.quotes
                  if q.normalization_status == NormalizationStatus.UNRESOLVED and q.has_price]
    check(bool(unresolved), "at least one price refused normalisation (%d)" % len(unresolved))

    print("\n=== 6. the review queue stays proportionate ===")
    queue = svc.review_queue(RFQ_ID)
    kinds = {}
    for it in queue:
        kinds[it["kind"]] = kinds.get(it["kind"], 0) + 1
    for k, v in sorted(kinds.items()):
        print("      %-20s %d" % (k, v))
    check(bool(queue), "the system still reports its own open questions (%d)" % len(queue))
    check(len(queue) < expected, "the queue is smaller than the table (%d < %d)" % (len(queue), expected))

    print("\n=== 7. dashboard adds up ===")
    for k in ("suppliers_total", "responses_received", "rfq_lines", "line_responses", "missing_quotes", "need_review"):
        print("      %-20s %s" % (k, s[k]))
    check(s["line_responses"] + s["missing_quotes"] == expected,
          "responses + gaps account for every cell (%d + %d = %d)"
          % (s["line_responses"], s["missing_quotes"], expected))

    print("\n%s: %d check(s) failed" % ("FAILED" if failures else "OK", len(failures)))
    for f in failures:
        print("  - " + f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
