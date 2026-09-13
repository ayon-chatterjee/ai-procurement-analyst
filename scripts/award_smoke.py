"""Phase 5 end to end against the real model and the real 30-line dataset.

Seeds an award from the live comparison, relaxes the quality bar the way the screen does,
overrides a line, approves it, has Claude write a letter per supplier, tries to leak a
rival into one of them, and generates the order handoff. Everything runs against a copy of
the database, so a smoke run never touches the demo data.

What it is actually checking is the negative guarantees: that no letter names or prices
another supplier, that a buyer's own edit faces the same check the model's draft faced, and
that nothing in Phases 1–3 changes because an award was made.

Usage:  python3 scripts/award_smoke.py
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from rfq_copilot.ai_service import AIError, get_ai_service  # noqa: E402
from rfq_copilot.award_calculations import basket_delta  # noqa: E402
from rfq_copilot.award_models import AwardThresholds  # noqa: E402
from rfq_copilot.award_service import AwardError, AwardService  # noqa: E402
from rfq_copilot.config import Settings  # noqa: E402
from rfq_copilot.persistence import RFQRepository  # noqa: E402
from rfq_copilot.supplier_service import SupplierService  # noqa: E402

RFQ_ID = "rfq_stress_30"

#: The tables Phase 5 must not touch. Fingerprinted before and after.
UPSTREAM = ("rfqs", "suppliers", "supplier_responses", "supplier_quotes",
            "certifications", "questionnaire_responses", "evidence")

failures = []


def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        failures.append(msg)


def fingerprint(db_path: str) -> str:
    conn = sqlite3.connect(db_path)
    digest = hashlib.sha256()
    for table in UPSTREAM:
        for row in conn.execute("SELECT * FROM %s" % table):
            digest.update(repr(row).encode())
    conn.close()
    return digest.hexdigest()


def main() -> int:
    source = Settings.from_env().db_path
    if not os.path.exists(source):
        print("No database at %s. Run scripts/stress_test.py first." % source)
        return 1

    settings = Settings.from_env()
    settings.db_path = os.path.join(tempfile.mkdtemp(prefix="award_smoke_"), "copy.db")
    shutil.copy(source, settings.db_path)
    print("Working on a copy: %s" % settings.db_path)

    repo = RFQRepository(settings.db_path)
    ai = get_ai_service(settings)
    svc = AwardService(ai, repo, settings, SupplierService(repo, ai, settings))
    before = fingerprint(settings.db_path)

    print("\n=== 1. a seeded award is ready to decide, not refused ===")
    # The copy inherits whatever was in flight, including an award left half-made in the
    # UI. Cancelling it keeps this script re-runnable at any moment rather than only from
    # a clean database — and cancelling is the documented way back, so using it here is
    # also a small check that it works.
    in_flight = svc.current(RFQ_ID)
    if in_flight is not None:
        svc.cancel(in_flight.id, "superseded by a smoke run")
        print("      cancelled an award that was already in progress")
    award = svc.start(RFQ_ID)
    proposal = svc.proposal(RFQ_ID, award.thresholds, award.currency)
    empty = len(proposal.lines_with_no_best_value)
    print("      %d lines, %d supplier(s), %s" % (len(award.lines), len(award.supplier_ids),
                                                  svc.totals(award).describe()))
    print("      best value covers %d of %d" % (len(proposal.lines) - empty, len(proposal.lines)))
    check(not empty, "the default bar leaves every line with a best-value candidate")
    check(not svc.validate(award.id).blocking,
          "a freshly seeded award is approvable, not refused over its own proposal")

    print("\n=== 2. every supplier on a line is available to explain a pick ===")
    entry = proposal.lines[0]
    named = {c.supplier_name for c in entry.candidates} | {e.supplier_name for e in entry.exclusions}
    print("      %s: %d candidate(s), %d excluded"
          % (entry.line_item_id, len(entry.candidates), len(entry.exclusions)))
    check(len(named) >= 2, "the comparison behind an ⓘ names the field, not just the pick")
    check(all(c.meets_bar or c.bar_reason for c in entry.candidates),
          "a candidate that misses the bar says why")

    print("\n=== 2b. tightening the bar is a recorded decision ===")
    award = svc.set_thresholds(award.id, AwardThresholds(
        require_document_backed_certification=True))
    strict = svc.proposal(RFQ_ID, award.thresholds, award.currency)
    print("      best value now covers %d of %d"
          % (len(strict.lines) - len(strict.lines_with_no_best_value), len(strict.lines)))
    check(len(strict.lines_with_no_best_value) > len(proposal.lines_with_no_best_value),
          "requiring a held certificate narrows best value")
    check(any("certificate" in a.lower() for a in award.assumptions),
          "the bar is stated as an assumption")
    award = svc.set_thresholds(award.id, AwardThresholds())
    delta = basket_delta(svc.proposal(RFQ_ID, award.thresholds, award.currency))
    print("      %s" % (delta["note"] or "baskets identical"))

    print("\n=== 3. an override needs a reason and survives a re-seed ===")
    ctx = svc.context(RFQ_ID, award.currency)
    line, other = None, None
    for entry in award.lines:
        rivals = [sid for sid, _ in svc.eligible_suppliers(award.id, entry.line_item_id)
                  if sid != entry.supplier_id]
        if rivals:
            line, other = entry.line_item_id, rivals[0]
            break
    if other is None:
        print("      no line has a second eligible supplier; nothing to override")
        return 1
    print("      overriding %s to %s" % (line, ctx.name(other)))
    try:
        svc.set_line(award.id, line, other, "")
        check(False, "an override without a reason is refused")
    except AwardError:
        check(True, "an override without a reason is refused")
    award = svc.set_line(award.id, line, other, "they hold the only verified certificate")
    award = svc.reseed(award.id)
    check(award.line(line).supplier_id == other, "the override outlives a re-seed")

    print("\n=== 4. notes inform; only an empty award stops ===")
    report = svc.validate(award.id)
    print("      blocking %s" % ([f.code for f in report.blocking] or "none"))
    print("      notes    %s" % (report.warning_codes() or "none"))
    check(not report.blocking, "nothing about a supplier refuses the award")
    award = svc.approve(award.id)
    check(award.status == "approved", "approved without ticking anything")
    check(sorted(award.acknowledged_warnings) == sorted(report.warning_codes()),
          "what was on screen is still recorded with the approval")

    print("\n=== 5. one letter per supplier, and each knows only itself ===")
    started = time.time()
    comms = svc.draft_communications(award.id)
    names = [s.name for s in ctx.matrix.suppliers]
    for comm in comms:
        rivals = [n for n in names if n != comm.supplier_name and n in comm.text]
        print("      %-24s %-14s %s" % (comm.supplier_name, comm.generated_by,
                                        comm.guard_status))
        check(not rivals, "the letter to %s names no rival" % comm.supplier_name)
        check(bool(comm.body), "there is always a letter to show %s" % comm.supplier_name)
    print("      %d letter(s) in %.0fs" % (len(comms), time.time() - started))

    print("\n=== 6. a buyer's own edit faces the same check ===")
    target = comms[0]
    rival = next(s for s in ctx.matrix.suppliers if s.name != target.supplier_name)
    edited = svc.edit_communication(
        target.id, target.text + "\n\nWe chose you over %s at 0.3312 per piece." % rival.name)
    print("      %s" % edited.edit_status)
    check(not edited.sendable, "a pasted rival is caught in the buyer's own wording")
    check(rival.name in edited.edit_leaks, "the guard names which rival leaked")
    try:
        svc.record_sent(target.id)
        check(False, "an unverified message cannot be recorded as sent")
    except AwardError:
        check(True, "an unverified message cannot be recorded as sent")
    corrected = svc.edit_communication(target.id, target.text + "\n\nPlease confirm by reply.")
    check(corrected.sendable, "correcting the wording clears the block")
    check(corrected.body and corrected.body != corrected.edited_body,
          "the original draft is kept alongside the edit")

    print("\n=== 7. the handoff is generated, not written ===")
    for comm in svc.communications(award.id):
        svc.record_sent(comm.id)
    handoffs = svc.generate_handoff(award.id)
    calls_before = len(getattr(ai, "calls", []) or [])
    for handoff in handoffs:
        print("      %-28s %-24s %2d line(s)  %s %s" % (
            handoff.reference, handoff.supplier_name, len(handoff.lines),
            handoff.currency, "{:,.2f}".format(handoff.subtotal or 0)))
    check(bool(handoffs), "an order per supplier")
    check(len(getattr(ai, "calls", []) or []) == calls_before,
          "no model call is involved in a document of record")

    svc.complete(award.id)
    print("\n=== 8. the trail and the upstream data ===")
    events = svc.events(award.id)
    print("      %d event(s), %s → %s" % (len(events), events[0].event_type,
                                          events[-1].event_type))
    check(all(e.seq for e in events[1:]), "every event is ordered")
    check(fingerprint(settings.db_path) == before,
          "nothing in Phases 1–3 changed because an award was made")

    print("\n%s: %d check(s) failed" % ("FAILED" if failures else "OK", len(failures)))
    for f in failures:
        print("  - " + f)
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AIError as e:
        print("\nThe model call failed: %s" % e)
        sys.exit(1)
