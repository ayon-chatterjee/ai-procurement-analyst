"""End-to-end smoke test against the REAL Claude Code CLI.

Proves the intelligence claims with live model reasoning (no fixtures):
  1. "I need corrugated carton boxes."  vs  "I need steel construction brackets."
     -> different categories, materially different question sets, no invented facts
  2. Carton RFQ + free text with 7 sizes, 2,000 each, Mumbai
     -> 7 line items, per-line quantities, destination with evidence, nothing re-asked

Usage:  python3 scripts/smoke.py [--keep-db] [--save-fixtures]
Exit code 1 on any failed assertion. Takes a few minutes (3 Sonnet calls).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from rfq_copilot.ai_service import get_ai_service  # noqa: E402
from rfq_copilot.config import Settings  # noqa: E402
from rfq_copilot.guards import content_tokens, similarity  # noqa: E402
from rfq_copilot.persistence import RFQRepository  # noqa: E402
from rfq_copilot.rfq_service import RFQService  # noqa: E402
from rfq_copilot.schema import FieldStatus, Source  # noqa: E402

SIZES = ["10 × 10 × 5", "12 × 10 × 6", "15 × 10 × 8", "18 × 12 × 10", "20 × 15 × 10", "24 × 18 × 12", "30 × 20 × 15"]
FREE_TEXT = "We need:\n" + "\n".join(SIZES) + "\ninches. 2,000 pieces each. Ship to Mumbai."

failures = []


def check(cond: bool, msg: str) -> None:
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        failures.append(msg)


def show(rfq, svc) -> None:
    print("  product=%s | category=%s | type=%s" % (rfq.product, rfq.category, rfq.product_type))
    print("  title=%s" % rfq.title)
    print("  completeness=%d%% ready=%s status=%s" % (rfq.completeness.score, rfq.completeness.ready_to_send, rfq.status.value))
    print("  explanation: %s" % rfq.completeness.explanation)
    facts = [(k, v) for k, v in rfq.fields.items() if v.status not in (FieldStatus.MISSING,)]
    for k, v in facts:
        print("  field %-28s %-14s %-16s %s%s" % (k, v.status.value, v.source.value, v.display_value() or (v.note or "")[:60],
                                                   (" | evidence=%r" % v.evidence) if v.evidence else ""))
    for li in rfq.line_items:
        print("  %s %s | %s | qty=%s %s | %s" % (li.id, li.product, li.spec_summary(), li.quantity, li.unit, li.source.value))
    for q in rfq.open_questions():
        print("  Q[%s] (%s, %s) %s\n        why: %s" % (q.id, q.importance.value, q.field_key, q.question, q.reason))
    msgs = svc.transcript(rfq.id)
    ai_msgs = [m for m in msgs if m.kind == "assistant"]
    if ai_msgs:
        print("  assistant: %s" % ai_msgs[-1].content)
    guard_notes = [m for m in msgs if m.kind == "guards"]
    if guard_notes:
        print("  guards: %s" % guard_notes[-1].content.replace("\n", "\n          "))


def main() -> int:
    keep = "--keep-db" in sys.argv
    save = "--save-fixtures" in sys.argv
    settings = Settings.from_env()
    tmp = tempfile.mkdtemp(prefix="rfq_smoke_")
    settings.db_path = os.path.join(ROOT, "data", "smoke.db") if keep else os.path.join(tmp, "smoke.db")
    svc = RFQService(RFQRepository(settings.db_path), get_ai_service(settings), settings)
    h = svc.health()
    print("Claude CLI: available=%s authenticated=%s %s | model=%s" % (h.get("available"), h.get("authenticated"), h.get("detail", ""), settings.model_quality))
    if not h.get("authenticated"):
        print("Not authenticated — run `claude` and sign in first.")
        return 1

    # ---------------------------------------------------------------- carton
    t = time.time()
    print("\n[1] I need corrugated carton boxes.")
    carton = svc.start_rfq("I need corrugated carton boxes.")
    print("  (%.1fs)" % (time.time() - t))
    show(carton, svc)
    check("carton" in carton.product.lower() or "box" in carton.product.lower(), "carton product recognised")
    check("packag" in carton.category.lower() or "carton" in carton.category.lower() or "box" in carton.category.lower(), "category is packaging-like")
    check(len(carton.open_questions()) >= 5, "at least 5 questions asked (%d)" % len(carton.open_questions()))
    check(len(carton.open_questions()) <= 8, "no more than 8 questions on the first turn")
    check(not any(fv.is_filled for fv in carton.fields.values()), "no buyer facts invented from a one-line request")
    check(all(q.reason and len(content_tokens(q.reason)) >= 3 for q in carton.open_questions()), "every question has a substantive reason")
    carton_text = " ".join(q.question for q in carton.open_questions()).lower()
    check(any(w in carton_text for w in ("dimension", "size")), "carton questions cover dimensions")
    check(any(w in carton_text for w in ("flute", "ply", "board", "ect", "gsm", "grade", "material", "wall")), "carton questions cover board construction")

    # --------------------------------------------------------------- bracket
    t = time.time()
    print("\n[2] I need steel construction brackets.")
    bracket = svc.start_rfq("I need steel construction brackets.")
    print("  (%.1fs)" % (time.time() - t))
    show(bracket, svc)
    check(bracket.category.lower() != carton.category.lower(), "categories differ (%s vs %s)" % (carton.category, bracket.category))
    bracket_text = " ".join(q.question for q in bracket.open_questions()).lower()
    check(any(w in bracket_text for w in ("grade", "material", "steel", "thickness", "gauge")), "bracket questions cover material grade / thickness")
    check(any(w in bracket_text for w in ("load", "weight", "capacity", "tolerance", "finish", "coating", "galvan", "treatment", "drawing")),
          "bracket questions cover load / finish / tolerance / drawing")
    check(not any(w in bracket_text for w in ("flute", "gsm", "ply", "corrugat")), "bracket questions contain no carton vocabulary")
    ca, cb = set(), set()
    for q in carton.open_questions():
        ca |= content_tokens(q.question)
    for q in bracket.open_questions():
        cb |= content_tokens(q.question)
    jacc = len(ca & cb) / float(len(ca | cb) or 1)
    check(jacc < 0.4, "question vocab Jaccard between categories is low (%.2f)" % jacc)

    # ------------------------------------------------------ seven line items
    t = time.time()
    print("\n[3] Carton follow-up:\n%s" % FREE_TEXT.replace("\n", "\n      "))
    before_open = {q.id: q for q in carton.open_questions()}
    carton = svc.submit_turn(carton.id, answers={}, skipped=[], free_text=FREE_TEXT)
    print("  (%.1fs)" % (time.time() - t))
    show(carton, svc)
    check(len(carton.line_items) == 7, "seven line items created (%d)" % len(carton.line_items))
    check(all(li.quantity == 2000 for li in carton.line_items), "each line has quantity 2,000")
    check(all(li.source == Source.BUYER_EXPLICIT for li in carton.line_items), "every line item has verified buyer evidence")
    check(all(li.spec_summary() for li in carton.line_items), "every line carries its dimensions")
    q = carton.fields["quantity"]
    check(q.is_filled and q.value == 14000 and "per line" in (q.note or "").lower(), "RFQ quantity reflects per-line semantics (total 14,000 for reference)")
    d = carton.fields["destination"]
    check(d.is_filled and "mumbai" in str(d.value).lower(), "destination captured")
    check(d.source == Source.BUYER_EXPLICIT and bool(d.evidence) and bool(d.source_refs), "destination has buyer evidence + source ref (%r)" % d.evidence)
    # The real rule: nothing the buyer just answered may still be open, by field or by rephrasing.
    resolved_keys = {"quantity", "destination"}
    open_qs = carton.open_questions()
    dim_like = [q for q in open_qs if (q.field_key or "") in resolved_keys
                or any(w in (q.field_key or "") for w in ("dimension", "box_size", "size_"))]
    check(not dim_like, "no open question targets an answered field (sizes / quantity / destination)%s"
          % ("" if not dim_like else ": " + ", ".join("%s→%s" % (q.field_key, q.question[:50]) for q in dim_like)))
    answered = [q for q in carton.questions if q.status.value != "open"]
    rephrasings = [(q, a) for q in open_qs for a in answered if similarity(q.question, a.question) >= 0.5]
    check(not rephrasings, "no open question rephrases an answered one%s"
          % ("" if not rephrasings else ": " + "; ".join("%r ~ %r" % (q.question[:40], a.question[:40]) for q, a in rephrasings[:3])))
    for qid, oq in before_open.items():
        if oq.field_key in ("quantity", "destination") or (oq.field_key and "dimension" in oq.field_key):
            check(carton.question(qid).status.value != "open", "question %s (%s) closed" % (qid, oq.field_key))
    check(len(carton.open_questions()) <= 8, "open questions bounded")
    check(not carton.completeness.ready_to_send or not carton.completeness.missing_required_fields, "readiness is consistent with missing list")

    # ---------------------------------------------- ambiguity is asked, not guessed
    t = time.time()
    print("\n[4] I need large carton boxes, about 5,000 of them, delivered to India.")
    vague = svc.start_rfq("I need large carton boxes, about 5,000 of them, delivered to India.")
    print("  (%.1fs)" % (time.time() - t))
    show(vague, svc)
    all_values = " ".join(str(fv.value) for fv in vague.fields.values() if fv.value not in (None, "", []))
    all_specs = " ".join(li.spec_summary() for li in vague.line_items)
    invented_cities = [c for c in ("mumbai", "delhi", "gurugram", "chennai", "bangalore", "bengaluru", "kolkata", "pune")
                       if c in (all_values + all_specs).lower()]
    check(not invented_cities, "no city invented from \"delivered to India\"%s" % ("" if not invented_cities else ": " + ", ".join(invented_cities)))
    dim_values = [fv.key for fv in vague.fields.values()
                  if fv.is_filled and any(w in fv.key for w in ("dimension", "size", "length", "width", "height"))]
    check(not dim_values, "no dimensions invented from \"large\"%s" % ("" if not dim_values else ": " + ", ".join(dim_values)))
    check(not any(any(ch.isdigit() for ch in s.value) for li in vague.line_items for s in li.specifications
                  if "dimension" in s.name.lower() or "size" in s.name.lower()),
          "no numeric dimensions invented on any line item")
    vague_text = " ".join(q.question for q in vague.open_questions()).lower()
    check(any(w in vague_text for w in ("dimension", "size", "how big", "l x w", "measurements")), "asks what \"large\" means")
    q_field = vague.fields.get("quantity")
    check(q_field is not None and q_field.is_filled and q_field.value == 5000, "stated quantity captured (5,000)")

    # --------------------------------------------------- explicit buyer correction
    t = time.time()
    print("\n[5] Correction: \"Actually, make that 8,000 pieces.\"")
    vague = svc.submit_turn(vague.id, answers={}, skipped=[], free_text="Actually, make that 8,000 pieces.")
    print("  (%.1fs)" % (time.time() - t))
    q_field = vague.fields["quantity"]
    print("  quantity: %s %s | status=%s | history=%s" % (q_field.value, q_field.unit or "", q_field.status.value,
                                                          [h.get("value") for h in q_field.history]))
    check(q_field.value == 8000, "latest buyer figure is active (8,000)")
    check(q_field.status.value == "provided", "an explicit correction is not treated as a conflict")
    check(any(h.get("value") == 5000 for h in q_field.history), "the superseded 5,000 is preserved in history")

    if save:
        fx = os.path.join(ROOT, "tests", "fixtures")
        for rec in svc.ai_calls(carton.id)[:1]:
            with open(os.path.join(fx, "cli_envelope_ok.json"), "w") as f:
                f.write(rec.raw_response)
        print("\nfixtures saved to tests/fixtures/")

    print("\n%s: %d check(s) failed" % ("FAILED" if failures else "OK", len(failures)))
    for f in failures:
        print("  - " + f)
    if keep:
        print("DB kept at %s" % settings.db_path)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
