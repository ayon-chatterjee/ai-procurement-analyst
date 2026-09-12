"""Regenerate TEST_INVENTORY.md from the test suite itself.

The inventory used to be written by hand, so it drifted: it claimed 201 tests after
the suite had grown past it, and a whole test file was missing from the listing. A
document that describes the tests is only worth reading if it cannot disagree with
them, so it is now derived from the suite rather than maintained alongside it.

Usage:  python3 scripts/make_test_inventory.py
"""
from __future__ import annotations

import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

#: Section headings, in the order they should appear. A file with no entry here still
#: appears, under its own name, so a new test file can never go unlisted.
AREAS = [
    ("tests/test_ai_service.py", "AI boundary (Claude CLI)"),
    ("tests/test_guards.py", "Trust guards (Phase 1)"),
    ("tests/test_persistence.py", "Persistence"),
    ("tests/test_playground.py", "Quotation Extraction Playground"),
    ("tests/test_testbench.py", "Supplier response test bench"),
    ("tests/test_rfq_service.py", "RFQ service flows (Phase 1)"),
    ("tests/test_schema.py", "Schema, fields & UI glue (Phase 1)"),
    ("tests/test_supplier_extraction.py", "Document reading & supplier guards (Phase 2)"),
    ("tests/test_supplier_normalization.py", "Price normalisation & line matching (Phase 2)"),
    ("tests/test_supplier_service.py", "Supplier service, comparison & FX (Phase 2)"),
    ("tests/test_analyst_calculations.py", "Analyst calculations (Phase 3)"),
    ("tests/test_analyst_guards.py", "Analyst query & explanation guards (Phase 3)"),
    ("tests/test_analyst_service.py", "Analyst service & conversation (Phase 3)"),
    ("tests/test_award_calculations.py", "Award seeding, bars & totals (Phase 5)"),
    ("tests/test_award_guards.py", "Supplier communication guards (Phase 5)"),
    ("tests/test_award_service.py", "Award lifecycle, execution & audit (Phase 5)"),
    ("tests/test_end_to_end.py", "End to end: one RFQ through every phase"),
]


def humanise(name: str) -> str:
    """`test_a_bare_claim_is_flagged` -> `A bare claim is flagged`."""
    text = re.sub(r"^test_", "", name).replace("_", " ").strip()
    return text[:1].upper() + text[1:]


def split_class(name: str) -> str:
    """`UnclaimedFigureTest` -> `Unclaimed Figure`."""
    words = re.findall(r"[A-Z][a-z0-9]*|[A-Z]+(?![a-z])", name)
    if words and words[-1] == "Test":
        words = words[:-1]
    return " ".join(words) or name


def collect():
    """{module file: {class: [test names]}}, from the loader rather than the filenames."""
    found = {}
    suite = unittest.defaultTestLoader.discover(os.path.join(ROOT, "tests"), top_level_dir=ROOT)

    def walk(s):
        for item in s:
            if isinstance(item, unittest.TestSuite):
                walk(item)
                continue
            cls = item.__class__
            path = "tests/%s.py" % cls.__module__.split(".")[-1]
            found.setdefault(path, {}).setdefault(cls.__name__, []).append(
                item._testMethodName)

    walk(suite)
    return found


def main() -> int:
    found = collect()
    ordered = [(p, t) for p, t in AREAS if p in found]
    ordered += [(p, os.path.basename(p)) for p in sorted(found) if p not in dict(AREAS)]
    total = sum(len(n) for classes in found.values() for n in classes.values())

    out = ["# Test inventory", "",
           "%d tests across %d files. Run with `python3 -m unittest discover -s tests -t .`"
           % (total, len(found)),
           "",
           "This file is generated: `python3 scripts/make_test_inventory.py`.", "",
           "| Area | Tests |", "|---|---:|"]
    for path, title in ordered:
        count = sum(len(n) for n in found[path].values())
        out.append("| %s | %d |" % (title, count))
    out += ["| **Total** | **%d** |" % total, "", ""]

    for path, title in ordered:
        classes = found[path]
        out += ["## %s" % title, "", "`%s`" % path, ""]
        for cls in sorted(classes):
            names = sorted(classes[cls])
            out += ["**%s** (%d)" % (split_class(cls), len(names)), ""]
            out += ["- %s" % humanise(n) for n in names]
            out.append("")
        out.append("")

    text = "\n".join(out).rstrip() + "\n"
    with open(os.path.join(ROOT, "TEST_INVENTORY.md"), "w", encoding="utf-8") as f:
        f.write(text)
    print("TEST_INVENTORY.md: %d tests across %d files" % (total, len(found)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
