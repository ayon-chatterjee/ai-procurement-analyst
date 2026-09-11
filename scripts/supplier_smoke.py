"""Phase 2 smoke test: the real pipeline over the real fixtures, against live Claude.

Reads the five supplier documents that were actually written to disk, extracts them,
matches them to the RFQ, normalises what is safe, and checks the trust properties that
Phase 2 exists to provide. Nothing is stubbed and no expected answer is hardcoded:
every assertion is about *behaviour*, not about a particular number.

Usage:  python3 scripts/supplier_smoke.py [--keep-db]
"""
from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from rfq_copilot.ai_service import AIError, AIUsageLimit, get_ai_service  # noqa: E402
from rfq_copilot.config import Settings  # noqa: E402
from rfq_copilot.persistence import RFQRepository  # noqa: E402
from rfq_copilot.supplier_models import (  # noqa: E402
    ClaimStatus, ExtractionStatus, MatchStatus, NormalizationStatus, PriceBasis, QuoteStatus,
)
from rfq_copilot.supplier_service import CellState, SupplierService  # noqa: E402

failures = []


def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        failures.append(msg)


def main() -> int:
    keep = "--keep-db" in sys.argv
    settings = Settings.from_env()
    if not keep:
        settings.db_path = os.path.join(tempfile.mkdtemp(prefix="p2_smoke_"), "smoke.db")
    repo = RFQRepository(settings.db_path)
    svc = SupplierService(repo, get_ai_service(settings), settings)

    rfq_id = "rfq_phase2_demo"
    rfq = repo.get_rfq(rfq_id)
    if rfq is None:
        print("Seed the demo RFQ first (scripts/seed_phase2_demo.py).")
        return 1
    print("RFQ %s — %d lines\n" % (rfq.id, len(rfq.line_items)))

    if not svc.has_responses(rfq_id):
        svc.seed_demo_responses(rfq_id)
    try:
        res = svc.extract_all(rfq_id, on_stage=lambda n, s: print("   [%s] %s" % (n, s)), only_pending=True)
    except AIUsageLimit as e:
        print("\n  STOPPED  %s" % e.user_message)
        return 2
    except AIError as e:
        print("\n  ERROR    %s" % e.user_message)
        return 1
    print("\nprocessed: %d succeeded, %d failed\n" % (len(res["succeeded"]), len(res["failed"])))

    matrix = svc.build_comparison(rfq_id)
    bundles = {b.supplier.name: b for b in svc.bundles_for(rfq_id, active_only=True) if b.supplier}
    s = matrix.summary

    print("=== 1. every format was read ===")
    for name, b in sorted(bundles.items()):
        media = ", ".join(d.media_type for d in b.documents)
        ok = all(d.extraction_status == ExtractionStatus.EXTRACTED for d in b.documents)
        print("  %-24s %-6s %s" % (name, media, b.response.extraction_status.value))
        check(ok, "%s: document read (%s)" % (name, media))
    check(len(bundles) >= 5, "at least five suppliers extracted (%d)" % len(bundles))

    print("\n=== 2. prices are traceable, not invented ===")
    priced = [q for b in bundles.values() for q in b.quotes if q.has_price]
    check(bool(priced), "prices were extracted (%d)" % len(priced))
    with_ev = [q for q in priced if any(b.evidence.get(e) and b.evidence[e].verified
                                        for b in bundles.values() for e in q.evidence_ids)]
    check(len(with_ev) >= len(priced) * 0.8,
          "at least 80%% of prices carry verified evidence (%d/%d)" % (len(with_ev), len(priced)))
    check(not any(q.unit_price == 0 for q in priced), "no price was recorded as zero")

    print("\n=== 3. a non-per-piece basis is normalised or refused, never assumed ===")
    per100 = [q for q in priced if q.price_basis == PriceBasis.PER_100]
    check(bool(per100), "a per-100 price basis was recognised (%d lines)" % len(per100))
    for q in per100[:1]:
        check(q.normalized_unit_price is not None and q.normalized_unit_price < q.unit_price,
              "per-100 price %s normalised to %s" % (q.original_price_text(), q.normalized_price_text()))
    unresolved = [q for q in priced if q.normalization_status == NormalizationStatus.UNRESOLVED]
    check(bool(unresolved), "at least one price was left unresolved rather than guessed (%d)" % len(unresolved))
    for q in unresolved[:2]:
        print("      %s: %s" % (q.original_price_text(), q.normalization_note[:80]))

    print("\n=== 4. the buried discount was found with its condition ===")
    discounted = [q for q in priced if q.discount.is_present]
    check(bool(discounted), "a discount was extracted (%d lines)" % len(discounted))
    if discounted:
        d = discounted[0].discount
        print("      %s | applies=%s | %s" % (d.describe(), d.applies, d.applies_reason))
        check(bool(d.condition), "the discount kept its qualifying condition")
        check(discounted[0].unit_price is not None, "the base price was not overwritten")

    print("\n=== 5. missing lines stay missing ===")
    not_quoted = [c for c in matrix.cells.values() if c.state == CellState.NOT_QUOTED]
    check(bool(not_quoted), "some lines were not quoted (%d cells)" % len(not_quoted))
    check(all(c.quote is None or not c.quote.has_price for c in not_quoted),
          "a not-quoted cell holds no price")
    check(all("0" != c.display for c in not_quoted), "a not-quoted cell never renders as zero")

    print("\n=== 6. certifications are claims until a certificate arrives ===")
    certs = [c for b in bundles.values() for c in b.certifications]
    check(bool(certs), "certifications were extracted (%d)" % len(certs))
    check(all(c.status != ClaimStatus.VERIFIED for c in certs),
          "no certification was marked verified without the certificate itself")
    for c in certs[:4]:
        print("      %-14s %-9s %s" % (c.name, c.status.value, c.note[:60]))

    print("\n=== 7. contradictions are preserved, not resolved ===")
    # Across every response, including superseded ones: a contradiction stays recorded on
    # the response where it occurred, rather than following a later revision around.
    all_bundles = svc.bundles_for(rfq_id)
    conflicted = [(b, q) for b in all_bundles for q in b.quotes if q.conflicts]
    check(bool(conflicted), "a contradiction inside one document was detected")
    if conflicted:
        b, q = conflicted[0]
        c = q.conflicts[0]
        vals = [v.get("value") for v in c.get("values", [])]
        where = "%s (%s)" % (b.supplier.name if b.supplier else "?",
                             "superseded" if not b.response.is_active else "active")
        print("      %s — %s: %s" % (where, c.get("topic"), " vs ".join(str(v)[:38] for v in vals)))
        check(len(vals) >= 2, "both sides of the contradiction were kept")
        check(all(v.get("evidence_id") for v in c.get("values", [])),
              "each side of the contradiction carries its own evidence")
        check(q.status == QuoteStatus.CONFLICT, "the affected quote is marked as conflicted")

    print("\n=== 8. a revision supersedes without deleting ===")
    all_responses = svc.responses_for(rfq_id)
    superseded = [r for r in all_responses if not r.is_active]
    check(bool(superseded), "an earlier response was superseded (%d)" % len(superseded))
    check(all(svc.bundle(r.id) is not None for r in superseded), "the superseded response is still readable")

    print("\n=== 9. uncertain line matches ask rather than assume ===")
    probable = [q for b in bundles.values() for q in b.quotes if q.match_status == MatchStatus.PROBABLE_MATCH]
    unmatched = [q for b in bundles.values() for q in b.quotes
                 if q.match_status == MatchStatus.UNMATCHED and q.has_price]
    print("      %d probable, %d unmatched-with-price" % (len(probable), len(unmatched)))
    check(all(q.status != QuoteStatus.QUOTED for q in probable),
          "a probable match is not presented as settled")

    print("\n=== 10. the dashboard is counted from the data ===")
    for k in ("suppliers_total", "responses_received", "no_response", "rfq_lines",
              "line_responses", "missing_quotes", "need_review"):
        print("      %-20s %s" % (k, s[k]))
    check(s["rfq_lines"] == len(rfq.line_items), "line count matches the RFQ")
    check(s["responses_received"] == len(bundles), "response count matches stored bundles")
    check(s["no_response"] >= 1, "a silent supplier is still represented")
    total_cells = len(rfq.line_items) * len(matrix.suppliers)
    check(sum(s["cells"].values()) == total_cells,
          "every line/supplier pair has a state (%d cells)" % total_cells)

    print("\n=== 11. currencies are kept apart ===")
    print("      currencies: %s" % ", ".join(s["currencies"]))
    check(len(s["currencies"]) >= 2, "suppliers quoted in more than one currency")
    check(not s["single_currency"], "the comparison knows it cannot rank across currencies")

    print("\n=== 12. the review queue names what cannot be asserted ===")
    queue = svc.review_queue(rfq_id)
    kinds = {}
    for it in queue:
        kinds[it["kind"]] = kinds.get(it["kind"], 0) + 1
    for k, v in sorted(kinds.items()):
        print("      %-20s %d" % (k, v))
    check(bool(queue), "the system surfaced its own open questions (%d)" % len(queue))

    print("\n%s: %d check(s) failed" % ("FAILED" if failures else "OK", len(failures)))
    for f in failures:
        print("  - " + f)
    if keep:
        print("DB kept at %s" % settings.db_path)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
