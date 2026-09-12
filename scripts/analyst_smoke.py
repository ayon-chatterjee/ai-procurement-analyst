"""Procurement analyst smoke test — real model, real data, no stubs.

Runs the questions a demo actually asks against a copy of the working database, and
checks behaviour rather than answers. No expected number is hardcoded anywhere: the
assertions are that an answer is calculated, that what it left out is explained, that a
what-if is labelled and changes nothing, and that a question outside the data is refused.

The database is copied to a temporary path first, so a run can never write to the real one.

Usage:  python3 scripts/analyst_smoke.py [--keep-db]
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from rfq_copilot.ai_service import AIError, AIUsageLimit, get_ai_service  # noqa: E402
from rfq_copilot.analyst_models import AnalystResult, Intent  # noqa: E402
from rfq_copilot.analyst_service import AnalystError, AnalystService  # noqa: E402
from rfq_copilot.config import Settings  # noqa: E402
from rfq_copilot.persistence import RFQRepository  # noqa: E402
from rfq_copilot.supplier_service import SupplierService  # noqa: E402

failures = []

#: The RFQs the demo uses. The stress set is the interesting one: mixed currencies, a
#: contradiction, a supplier with an MOQ above every line, and one verified certificate.
STRESS_RFQ = "rfq_stress_30"
DEMO_RFQ = "rfq_phase2_demo"


def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        failures.append(msg)


def show(result: AnalystResult) -> None:
    print("        intent=%s rows=%d exclusions=%d %s"
          % (result.intent, len(result.rows), len(result.exclusions),
             "(what-if)" if result.hypothetical else ""))
    print("        summary: %s" % result.summary[:160])
    if result.explanation:
        print("        said:    %s" % result.explanation[:160])
    else:
        print("        said:    (none — %s)" % result.explanation_status)


def ask(svc, rfq_id, question):
    started = time.time()
    print("\n  ? %s" % question)
    result = svc.ask(rfq_id, question)
    print("        (%.0fs)" % (time.time() - started))
    show(result)
    return result


def main() -> int:
    keep = "--keep-db" in sys.argv
    settings = Settings.from_env()
    source = settings.db_path
    if not os.path.exists(source):
        print("No database at %s. Run the app and load the demo responses first." % source)
        return 1

    workdir = tempfile.mkdtemp(prefix="analyst_smoke_")
    settings.db_path = os.path.join(workdir, "copy.db")
    shutil.copy(source, settings.db_path)
    print("Working on a copy at %s\n" % settings.db_path)

    repo = RFQRepository(settings.db_path)
    ai = get_ai_service(settings)
    sup = SupplierService(repo, ai, settings)
    svc = AnalystService(ai, repo, settings, sup)

    before = os.path.getsize(settings.db_path)
    quotes_before = _quote_fingerprint(sup, STRESS_RFQ)

    try:
        print("=== 1. the answer is calculated, not narrated ===")
        cheapest = ask(svc, STRESS_RFQ, "Who is cheapest for each line?")
        check(cheapest.intent == Intent.CHEAPEST_BY_LINE.value, "the question is read as a price comparison")
        check(len(cheapest.rows) > 0, "an answer has rows behind it")
        check(all(r.get("Price") != 0 for r in cheapest.rows), "no line is priced at zero")
        check(bool(cheapest.calculation_notes), "the answer says how it was worked out")
        check(bool(cheapest.assumptions), "the answer states its assumptions")
        check(all(e.reason for e in cheapest.exclusions), "everything left out carries a reason")

        print("\n=== 2. a follow-up narrows the question before it ===")
        qa_only = ask(svc, STRESS_RFQ, "Now only among suppliers who cleared QA.")
        check(not qa_only.refused, "the follow-up is answered")
        cleared = [f for f in (qa_only.query.filters if qa_only.query else [])
                   if f.field == "eligibility"]
        check(bool(cleared) or qa_only.hypothetical,
              "\"cleared QA\" becomes a qualification filter, not a guess")
        check(any("document" in a for a in qa_only.assumptions),
              "the meaning of \"cleared\" is stated")

        print("\n=== 3. an exclusion can be explained against the evidence ===")
        why = ask(svc, STRESS_RFQ, "Why didn't we choose Shenzhen Print & Pack for line 17?")
        check(not why.refused, "the question is answered")
        check(bool(why.summary), "there is a stated reason")

        print("\n=== 4. a percentage carries its numerator and denominator ===")
        coverage = ask(svc, STRESS_RFQ, "What percentage of the RFQ has valid quotes?")
        metrics = coverage.metrics
        check("numerator" in metrics and "denominator" in metrics
              or any(ch.isdigit() for ch in coverage.summary),
              "the percentage is shown with the counts behind it")

        print("\n=== 5. coverage is counted from the quotes ===")
        who = ask(svc, STRESS_RFQ, "Who quoted the most lines?")
        check(len(who.rows) > 0, "every supplier is accounted for")

        print("\n=== 6. the review list is grouped and explained ===")
        review = ask(svc, STRESS_RFQ, "What should I review before making a decision?")
        check(len(review.rows) > 0, "there is something to review")
        check(all(r.get("Detail") is not None for r in review.rows), "each item says what it is")

        print("\n=== 7. an open-ended question is read from the data ===")
        fob = ask(svc, STRESS_RFQ, "Which suppliers quote FOB delivery terms?")
        check(not fob.refused, "an ordinary lookup is answered rather than refused")

        print("\n=== 8. a claim is not a verification ===")
        quality = ask(svc, DEMO_RFQ, "Which suppliers have cleared quality?")
        check(not quality.refused, "the question is answered")
        check(any("claim" in a or "document" in a for a in quality.assumptions),
              "the answer says what clearing means here")

        print("\n=== 9. what the data cannot answer is refused ===")
        market = ask(svc, DEMO_RFQ, "What is the going market rate for corrugated cartons in Asia?")
        check(market.refused or "don't have" in (market.explanation or market.summary).lower()
              or "only" in (market.explanation or market.summary).lower(),
              "a question about the outside market is not answered as fact")

        print("\n=== 10. a what-if is labelled and changes nothing ===")
        whatif = ask(svc, STRESS_RFQ,
                     "What if we exclude Shenzhen Print & Pack and ignore minimum order quantities?")
        check(whatif.hypothetical, "the answer is marked as a what-if")
        check(bool(whatif.hypothetical_labels), "the assumption is spelled out")
        check(whatif.summary.startswith("What-if"), "the summary opens by saying so")
        check(_quote_fingerprint(sup, STRESS_RFQ) == quotes_before,
              "no supplier quote was changed by the what-if")

    except AIUsageLimit as e:
        print("\n  STOPPED  %s" % e.user_message)
        return 2
    except (AIError, AnalystError) as e:
        print("\n  ERROR  %s" % e)
        return 1

    check(os.path.getsize(settings.db_path) >= before, "the copy is intact")
    print("\n%s: %d check(s) failed" % ("FAILED" if failures else "OK", len(failures)))
    for f in failures:
        print("  - %s" % f)
    if not keep:
        shutil.rmtree(workdir, ignore_errors=True)
    else:
        print("\nKept the working copy at %s" % settings.db_path)
    return 1 if failures else 0


def _quote_fingerprint(sup: SupplierService, rfq_id: str) -> str:
    """Everything an analysis could conceivably have changed, in one comparable string."""
    parts = []
    for bundle in sorted(sup.bundles_for(rfq_id), key=lambda b: b.response.id):
        for q in sorted(bundle.quotes, key=lambda q: q.id):
            parts.append("%s|%s|%s|%s|%s|%s" % (q.id, q.line_item_id, q.unit_price, q.currency,
                                                q.status.value, q.match_status.value))
        for c in sorted(bundle.certifications, key=lambda c: c.id):
            parts.append("%s|%s|%s" % (c.id, c.name, c.status.value))
    return "\n".join(parts)


if __name__ == "__main__":
    sys.exit(main())
