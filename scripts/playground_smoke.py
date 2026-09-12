"""Quotation Extraction Playground smoke test — real model, real files, no stubs.

Covers the cases a demo will actually hit: plain text, text plus an attachment,
several line items, requirements that are genuinely missing, and a requirement split
between an email and a spreadsheet so neither source alone is enough.

Usage:  python3 scripts/playground_smoke.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from rfq_copilot.ai_service import AIError, AIUsageLimit, get_ai_service  # noqa: E402
from rfq_copilot.config import Settings  # noqa: E402
from rfq_copilot.persistence import RFQRepository  # noqa: E402
from rfq_copilot.playground_service import PlaygroundError, PlaygroundService  # noqa: E402

failures = []


def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        failures.append(msg)


def show(result):
    print("  product=%s | category=%s" % (result.product, result.category))
    print("  summary: %s" % result.summary[:130])
    for li in result.line_items:
        print("  - %s | qty=%s %s | specs=%s" % (
            li.name, li.quantity, li.unit, "; ".join("%s=%s" % (n, v) for n, v, _ in li.specifications)[:70]))
        for r in li.requirements:
            flag = "?" if r.status == "ambiguous" else ("+" if r.verified else "!")
            print("      %s %-22s %-26s  <- %s" % (flag, r.label[:22], r.display_value()[:26], r.source_label()[:38]))
    for r in result.shared_requirements:
        flag = "?" if r.status == "ambiguous" else ("+" if r.verified else "!")
        print("    %s [shared] %-16s %-26s  <- %s" % (flag, r.label[:16], r.display_value()[:26], r.source_label()[:38]))


def run(label, fn):
    try:
        return fn()
    except AIUsageLimit as e:
        print("\n  STOPPED  %s: %s" % (label, e.user_message))
        raise SystemExit(2)
    except (AIError, PlaygroundError) as e:
        print("\n  ERROR    %s: %s" % (label, e))
        failures.append("%s raised %s" % (label, type(e).__name__))
        raise SystemExit(1)


def main() -> int:
    settings = Settings.from_env()
    settings.db_path = os.path.join(tempfile.mkdtemp(prefix="pg_smoke_"), "t.db")
    svc = PlaygroundService(get_ai_service(settings), RFQRepository(settings.db_path), settings)
    work = tempfile.mkdtemp(prefix="pg_files_")

    # ---------------------------------------------------------------- 1 text
    print("\n[1] plain text requirement")
    email = ("Subject: Need quotation for brackets\n\nHi,\nPlease send your best quote for 500 SS304 "
             "brackets.\nThickness should be 3mm and finish should be powder coated.\nNeed delivery "
             "within 2 weeks.\n\nThanks")
    t = time.time()
    r1 = run("scenario 1", lambda: svc.analyze(email))
    print("  (%.0fs)" % (time.time() - t))
    show(r1)
    check(len(r1.line_items) == 1, "one line item detected (%d)" % len(r1.line_items))
    text = " ".join(x.display_value().lower() for x in r1.line_items[0].requirements) + \
           " ".join("%s %s" % (n, v) for n, v, _ in r1.line_items[0].specifications).lower()
    check("ss304" in text, "the material grade was picked up")
    check("3" in text and "mm" in text.replace(" ", ""), "the thickness was picked up")
    check(r1.line_items[0].quantity == 500, "quantity 500 (%s)" % r1.line_items[0].quantity)
    check(all(r.verified or r.status == "ambiguous" for r in r1.line_items[0].requirements),
          "every reported requirement quotes something really in the email")

    rfq1 = svc.to_rfq(r1)
    missing = rfq1.completeness.missing_required_fields
    print("  missing per the existing rules: %s" % ", ".join(missing[:6]))
    check(bool(missing), "the existing completeness rules report real gaps (%d)" % len(missing))
    check(not any("thickness" in m.lower() for m in missing), "a stated requirement is never called missing")

    # ------------------------------------------------- 2 text + attachment
    print("\n[2] requirement split between an email and a spreadsheet")
    from openpyxl import Workbook
    wb = Workbook(); ws = wb.active; ws.title = "Requirements"
    ws.append(["Item", "Qty", "Material", "Thickness (mm)", "Tolerance"])
    ws.append(["Mounting bracket", 750, "SS316", 4, "+/- 0.15 mm"])
    xlsx = os.path.join(work, "requirements.xlsx"); wb.save(xlsx)
    split_email = ("Hi,\n\nPlease quote the bracket in the attached sheet. Finish must be "
                   "electropolished and we need it delivered to our Pune plant by 30 November.\n\nThanks")
    t = time.time()
    r2 = run("scenario 2", lambda: svc.analyze(split_email, [xlsx]))
    print("  (%.0fs)" % (time.time() - t))
    show(r2)
    check(r2.sources and r2.sources[0].readable, "the spreadsheet was read")
    kinds = {r.source_kind for r in r2.all_requirements()}
    check("attachment" in kinds, "at least one requirement came from the attachment")
    check("email_text" in kinds, "at least one requirement came from the email")
    joined = " ".join(x.display_value().lower() for x in r2.all_requirements()) + " " + \
             " ".join("%s %s" % (n, v) for li in r2.line_items for n, v, _ in li.specifications).lower()
    check("ss316" in joined, "material came from the spreadsheet")
    check("electropolish" in joined, "finish came from the email")
    check(any(r.source_document for r in r2.all_requirements() if r.source_kind == "attachment"),
          "attachment-sourced requirements name the file they came from")

    # -------------------------------------------------- 3 multiple items
    print("\n[3] several line items, one deliberately vague")
    multi = ("Hi team,\n\nWe need pricing for three things for the Pune plant:\n\n"
             "1. 500 SS304 brackets, 3mm thick, powder coated\n"
             "2. 200 aluminium housings, anodised, roughly 120 x 80 x 40 mm\n"
             "3. Some rubber gaskets, a few hundred should do\n\n"
             "Delivery to Pune by the end of next month. We'll need test certificates for the "
             "stainless steel.\n\nRegards,\nPriya")
    t = time.time()
    r3 = run("scenario 3", lambda: svc.analyze(multi))
    print("  (%.0fs)" % (time.time() - t))
    show(r3)
    check(len(r3.line_items) == 3, "three line items detected (%d)" % len(r3.line_items))
    qtys = [li.quantity for li in r3.line_items]
    check(500 in qtys and 200 in qtys, "firm quantities captured (%s)" % qtys)
    vague = [li for li in r3.line_items if not li.quantity]
    ambiguous = [r for r in r3.all_requirements() if r.status == "ambiguous"]
    check(bool(vague) or bool(ambiguous),
          "'a few hundred' was not turned into a number (%d unquantified, %d ambiguous)"
          % (len(vague), len(ambiguous)))
    rfq3 = svc.to_rfq(r3)
    check(len(rfq3.line_items) == 3, "the RFQ carries all three lines")
    check([li.id for li in rfq3.line_items] == ["LINE-001", "LINE-002", "LINE-003"], "stable line ids")
    gaps = rfq3.completeness.missing_required_fields
    print("  missing: %s" % ", ".join(gaps[:8]))
    check(any("LINE-" in g for g in gaps), "the unquantified line is reported as incomplete")

    # ------------------------------------------------------ 4 nothing to find
    print("\n[4] input with no procurement requirement in it")
    r4 = run("scenario 4", lambda: svc.analyze("Hi, are we still on for the 3pm call tomorrow? Thanks"))
    check(not r4.found_anything, "no line items invented from a meeting note")
    print("  reason: %s" % (r4.nothing_found_reason or "(none given)"))

    # -------------------------------------------------------- 5 image input
    print("\n[5] requirement inside an image")
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (760, 240), "white"); d = ImageDraw.Draw(im)
    d.text((20, 24), "PURCHASE REQUISITION  PR-2026-118", fill="black")
    d.text((20, 64), "Item: Hex head bolt M10 x 50, grade 8.8", fill="black")
    d.text((20, 96), "Quantity: 1200 nos", fill="black")
    d.text((20, 128), "Finish: hot dip galvanised", fill="black")
    d.text((20, 160), "Required at Chennai plant by 15 December 2026", fill="black")
    png = os.path.join(work, "requisition.png"); im.save(png)
    t = time.time()
    r5 = run("scenario 5", lambda: svc.analyze("Please quote the attached requisition.", [png]))
    print("  (%.0fs)" % (time.time() - t))
    show(r5)
    check(r5.sources[0].readable, "the image was transcribed")
    check(bool(r5.line_items), "a line item was found in the image")
    joined5 = (" ".join(li.name.lower() for li in r5.line_items) + " " +
               " ".join(x.display_value().lower() for x in r5.all_requirements()) + " " +
               " ".join("%s %s" % (n, v) for li in r5.line_items for n, v, _ in li.specifications).lower())
    check("bolt" in joined5 or "m10" in joined5, "the item was identified from the image")
    check("1200" in joined5 or any(li.quantity == 1200 for li in r5.line_items),
          "the quantity was read from the image")
    check(any(r.source_kind == "attachment" for r in r5.all_requirements()),
          "image-sourced requirements are attributed to the file")

    print("\n%s: %d check(s) failed" % ("FAILED" if failures else "OK", len(failures)))
    for f in failures:
        print("  - " + f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
