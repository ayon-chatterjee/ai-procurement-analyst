"""Put the database into the state the product is meant to be seen in.

One RFQ that carries the whole story: a complete Phase 1 artefact with real provenance,
six suppliers who answer in five formats, one of whom never replies, one who sends a
revision, one who contradicts themselves, one who quotes by position in an email that also
tries to give the extraction pipeline instructions, one whose certificate we actually hold
and four whose certificates we do not.

Every one of those is a case the product claims to handle. Before this script they were
spread across two half-built RFQs and four abandoned ones, so nobody opening the app could
reach a state that demonstrated anything.

    python3 scripts/seed_demo.py              # build it, register the responses
    python3 scripts/seed_demo.py --extract    # ...and read them with the real model (~3 min)
    python3 scripts/seed_demo.py --clean      # ...and remove other RFQs first

Safe to re-run: the RFQ id is fixed and responses are deleted before being re-registered.
Re-run it before a demo — response dates are relative to now, which is what keeps the
expiring-quote warning live instead of drifting into the past.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from rfq_copilot.ai_service import AIError, AIUsageLimit, get_ai_service  # noqa: E402
from rfq_copilot.config import Settings  # noqa: E402
from rfq_copilot.fields import new_field_set  # noqa: E402
from rfq_copilot.guards import apply_field_updates, compute_completeness  # noqa: E402
from rfq_copilot.persistence import RFQRepository  # noqa: E402
from rfq_copilot.schema import (  # noqa: E402
    Importance, LineItem, Message, MessageRole, Question, RFQ, RFQStatus, Section, Source,
    SpecAttr, new_id,
)
from rfq_copilot.supplier_models import Supplier, SupplierStatus  # noqa: E402
from rfq_copilot.supplier_service import SupplierService  # noqa: E402
from scripts.make_stress_fixtures import QTY, SIZES  # noqa: E402

RFQ_ID = "rfq_stress_30"
STRESS_DIR = os.path.join(ROOT, "fixtures", "suppliers", "stress")

#: What the buyer said, across three turns. The fields below are applied through the real
#: `apply_field_updates`, which refuses to mark anything "buyer stated" unless it can find
#: the words in this text — so the provenance badges on screen are earned, not set.
TURNS = [
    "We need corrugated carton boxes for our e-commerce fulfilment centre — 30 different "
    "sizes, 1,500 pieces of each, shipping to our warehouse in Mumbai, India. Budget is "
    "around USD 0.40 per piece landed.",

    "They are custom printed with our logo in two colours. Board should be 3-ply B-flute "
    "with a 125gsm kraft liner. Quote FOB at the port of loading, in US dollars. We ship "
    "by sea freight. We need them delivered within 45 days of placing the order. Suppliers "
    "must hold ISO 9001. Boxes are to be supplied flat-packed in bundles of 25.",

    "We are open to suppliers in China, Vietnam, India or Turkey. I do not know what our "
    "target landed cost should be yet — tell me what the market says. Please ask every "
    "supplier to state their minimum order quantity and quote validity clearly.",
]

#: Each entry is applied with the turn whose text supports it. `buyer_explicit` values
#: carry the exact words to look for; `ai_recommended` ones carry none, which is how the
#: guard keeps the two apart.
FIELD_UPDATES = [
    (0, [
        {"key": "quantity", "value": 45000, "unit": "pcs", "source": "buyer_explicit",
         "status": "provided", "evidence": "1,500 pieces of each"},
        {"key": "destination", "value": "Mumbai, India", "source": "buyer_explicit",
         "status": "provided", "evidence": "warehouse in Mumbai, India"},
    ]),
    (1, [
        {"key": "technical_summary",
         "value": "3-ply B-flute corrugated cartons, 125gsm kraft liner, custom printed "
                  "with a two-colour logo",
         "source": "buyer_explicit", "status": "provided",
         "evidence": "3-ply B-flute with a 125gsm kraft liner"},
        {"key": "customization_type", "value": "Custom printed", "source": "buyer_explicit",
         "status": "provided", "evidence": "custom printed with our logo in two colours"},
        {"key": "currency", "value": "USD", "source": "buyer_explicit", "status": "provided",
         "evidence": "in US dollars"},
        {"key": "price_basis", "value": "FOB", "source": "buyer_explicit", "status": "provided",
         "evidence": "Quote FOB at the port of loading"},
        {"key": "shipping_method", "value": "Sea freight", "source": "buyer_explicit",
         "status": "provided", "evidence": "We ship by sea freight"},
        {"key": "required_delivery_date", "value": "Within 45 days of order",
         "source": "buyer_explicit", "status": "provided",
         "evidence": "delivered within 45 days of placing the order"},
        {"key": "certifications", "value": "ISO 9001", "source": "buyer_explicit",
         "status": "provided", "evidence": "Suppliers must hold ISO 9001"},
        {"key": "packaging_requirements", "value": "Flat-packed in bundles of 25",
         "source": "buyer_explicit", "status": "provided",
         "evidence": "flat-packed in bundles of 25"},
    ]),
    (2, [
        {"key": "sourcing_country", "value": "China, Vietnam, India or Turkey",
         "source": "buyer_explicit", "status": "provided",
         "evidence": "suppliers in China, Vietnam, India or Turkey"},
        {"key": "additional_supplier_instructions",
         "value": "State your minimum order quantity and quote validity clearly.",
         "source": "buyer_explicit", "status": "provided",
         "evidence": "state their minimum order quantity and quote validity clearly"},
        # The buyer said outright that they do not know. An acknowledged gap is not a
        # missing field, and the readiness panel says so rather than nagging.
        {"key": "target_landed_cost", "value": None, "source": "buyer_explicit",
         "status": "unknown",
         "evidence": "I do not know what our target landed cost should be yet"},
        # No evidence: these are the assistant's suggestions, and the screen labels them
        # "AI recommended" precisely because the guard could not find them in what the
        # buyer wrote.
        {"key": "payment_terms", "value": "30% advance, 70% against B/L",
         "source": "ai_recommended", "status": "recommended",
         "note": "Common for first orders with a new overseas packaging supplier."},
        {"key": "quote_validity", "value": "30 days", "source": "ai_recommended",
         "status": "recommended",
         "note": "Long enough to compare quotes without exposing suppliers to board-price swings."},
        {"key": "quality_standards", "value": "Pre-shipment inspection on first order",
         "source": "ai_recommended", "status": "recommended",
         "note": "Usual for custom-printed packaging where print quality matters."},
    ]),
]

#: What the assistant said back each turn. Shown in the conversation history so the
#: transcript reads as a conversation rather than three orphaned buyer messages.
ASSISTANT_NOTES = [
    "Corrugated shipping cartons, 30 sizes at 1,500 pieces each to Mumbai. I still need "
    "the board specification, how they are printed, the price basis and when you need "
    "them — those four change what suppliers quote more than anything else.",
    "That settles the specification and the commercial basis. Two things left that affect "
    "pricing: which countries you will source from, and what each supplier must state back.",
    "The RFQ is ready to send. I have suggested payment terms, quote validity and an "
    "inspection step — all marked as recommendations, so change any of them before you "
    "send it.",
]

#: These are what each *supplier* must answer, which is why none of them is REQUIRED: a
#: required question is one the readiness score blocks on, and blocking the buyer's own
#: RFQ on an answer only a supplier can give would leave it permanently not-ready.
QUESTIONS = [
    ("q_cert", Section.QUALITY, "Which quality certifications do you hold, and can you "
     "attach the certificate?", "certifications", Importance.RECOMMENDED),
    ("q_pay", Section.COMMERCIAL, "What payment terms do you offer?", "payment_terms",
     Importance.RECOMMENDED),
    ("q_lead", Section.LOGISTICS, "What is your production lead time after artwork approval?",
     "required_delivery_date", Importance.RECOMMENDED),
    ("q_print", Section.TECHNICAL, "Is two-colour flexo printing included in the unit price?",
     "technical_summary", Importance.RECOMMENDED),
    ("q_moq", Section.COMMERCIAL, "What is your minimum order quantity per size?",
     "quantity", Importance.RECOMMENDED),
]

#: Who was asked, what they sent, and how long ago. Dates are relative to the run so the
#: validity arithmetic is live: Gujarat's 10-day validity on a quote received 5 days ago
#: is inside the 7-day expiring window, which is the warning the award screen should show.
SUPPLIERS = [
    ("Anhui Packaging Co", "China", "Li Wei", ["stress_a_quote.xlsx"], 5, None),
    ("Shenzhen Print & Pack", "China", "Chen Hui", ["stress_b_quote.pdf"], 5,
     (["stress_b_revision.pdf"], 1)),
    ("Viet Carton JSC", "Vietnam", "Nguyen Thi Mai", ["stress_c_response.docx"], 4, None),
    ("Gujarat Boxes Pvt Ltd", "India", "Rakesh Patel", ["stress_d_response.txt"], 5, None),
    ("Istanbul Ambalaj", "Turkey", "Emre Yilmaz",
     ["stress_e_quote.png", "stress_e_iso9001_certificate.png"], 3, None),
]
SILENT = ("Pacific Carton Works", "Philippines", "Jose Ramos")


def _key(name: str) -> str:
    return name.split()[0].lower()


def build_rfq(repo: RFQRepository) -> RFQ:
    """A Phase 1 artefact built the way Phase 1 builds one.

    The fields go through `apply_field_updates`, so an evidence claim that does not appear
    in the buyer's own words is downgraded exactly as it would be in the app. That matters
    more than it sounds: the demo's whole argument is that you can tell what the buyer said
    from what the assistant suggested, and hand-setting the statuses would make that
    argument on data that had never been tested by the rule it illustrates.
    """
    rfq = RFQ(id=RFQ_ID, title="Corrugated Carton Boxes — 30 sizes",
              product="Corrugated Carton Boxes", category="Packaging",
              product_type="Corrugated shipping cartons", fields=new_field_set(), turn=0)

    for i, size in enumerate(SIZES, start=1):
        rfq.line_items.append(LineItem(
            id="LINE-%03d" % i, product="Corrugated carton",
            specifications=[SpecAttr("Dimensions", size, "in")], quantity=QTY, unit="pcs",
            source=Source.BUYER_EXPLICIT, evidence=size))
    rfq.line_seq = len(SIZES)

    messages: list = []
    prior: list = []
    for turn_no, (text, (idx, updates)) in enumerate(zip(TURNS, FIELD_UPDATES), start=1):
        assert idx == turn_no - 1
        rfq.turn = turn_no
        ref = "turn-%d" % turn_no
        messages.append(Message(id=new_id("msg"), rfq_id=rfq.id, role=MessageRole.BUYER,
                                kind="request" if turn_no == 1 else "answers",
                                turn=turn_no, content=text))
        notes = apply_field_updates(rfq, updates, text, ref, turn_no, prior)
        if notes:
            messages.append(Message(id=new_id("msg"), rfq_id=rfq.id, role=MessageRole.SYSTEM, kind="guards",
                                    turn=turn_no, content="\n".join(notes)))
        messages.append(Message(id=new_id("msg"), rfq_id=rfq.id, role=MessageRole.ASSISTANT, kind="assistant",
                                turn=turn_no, content=ASSISTANT_NOTES[turn_no - 1]))
        prior.append((ref, text))

    rfq.questions = [Question(id=qid, category=sec, question=q, field_key=fk, importance=imp)
                     for qid, sec, q, fk, imp in QUESTIONS]
    rfq.completeness = compute_completeness(rfq, None, rfq.turn)
    rfq.status = RFQStatus.SUPPLIER_READY
    messages.append(Message(id=new_id("msg"), rfq_id=rfq.id, role=MessageRole.SYSTEM, kind="status",
                            turn=rfq.turn, content="Marked supplier-ready."))
    repo.save_rfq(rfq)
    repo.delete_messages_for(rfq.id)
    for m in messages:
        repo.add_message(m)
    return rfq


def register_responses(svc: SupplierService, rfq_id: str) -> int:
    """Register every response, plus the supplier who never answers.

    `_register` also records the invitation, which is what keeps a supplier who was asked
    to quote on this RFQ out of every other RFQ's comparison.
    """
    missing = [f for _, _, _, files, _, rev in SUPPLIERS
               for f in files + (rev[0] if rev else [])
               if not os.path.exists(os.path.join(STRESS_DIR, f))]
    if missing:
        raise SystemExit("Missing fixtures: %s\nRun python3 scripts/make_stress_fixtures.py"
                         % ", ".join(sorted(set(missing))))

    svc.store.delete_responses_for(rfq_id)
    existing = {s.name: s for s in svc.store.list_suppliers()}
    today = _dt.date.today()
    count = 0

    for name, country, contact, files, days_ago, revision in SUPPLIERS:
        supplier = existing.get(name) or Supplier(
            name=name, country=country, contact_name=contact,
            contact_email="sales@%s.example" % _key(name))
        supplier.contact_name = supplier.contact_name or contact
        supplier.status = SupplierStatus.RESPONDED
        svc.store.save_supplier(supplier)
        svc.register_response(rfq_id, supplier, files, STRESS_DIR,
                              received_at=str(today - _dt.timedelta(days=days_ago)))
        count += 1
        if revision:
            files_r, days_r = revision
            svc.register_response(rfq_id, supplier, files_r, STRESS_DIR,
                                  received_at=str(today - _dt.timedelta(days=days_r)),
                                  is_revision=True)
            count += 1

    name, country, contact = SILENT
    silent = existing.get(name) or Supplier(name=name, country=country, contact_name=contact,
                                            contact_email="sales@%s.example" % _key(name))
    silent.status = SupplierStatus.NO_RESPONSE
    svc.store.save_supplier(silent)
    svc.store.invite(rfq_id, silent.id, "no_response",
                     "Invited on %s; no reply." % (today - _dt.timedelta(days=7)))
    return count


def clean_other_rfqs(repo: RFQRepository) -> int:
    keep = {RFQ_ID, "rfq_phase2_demo"}
    removed = 0
    for summary in repo.list_rfqs():
        if summary.id not in keep:
            repo.delete_rfq(summary.id)
            removed += 1
    return removed


def report(svc: SupplierService, rfq: RFQ) -> None:
    matrix = svc.build_comparison(rfq.id)
    s = matrix.summary
    print("\n=== what the demo now holds ===")
    print("  RFQ            %s — %s, %d lines, readiness %d%%, status %s"
          % (rfq.id, rfq.title, len(rfq.line_items), rfq.completeness.score, rfq.status.value))
    print("  suppliers      %d (%d replied, %d silent)"
          % (s["suppliers_total"], s["responses_received"], s["no_response"]))
    print("  prices         %d of %d supplier-line pairs priced"
          % (s["line_responses"], s["comparable_cells"]))
    print("  needs review   %d items across %d responses"
          % (s["review_items_total"], s["need_review"]))
    print("  currencies     %s" % ", ".join(s["currencies"]))
    for b in sorted(svc.bundles_for(rfq.id, active_only=True), key=lambda x: x.supplier.name):
        certs = ", ".join("%s %s" % (c.name, c.status.value) for c in b.certifications) or "none"
        print("    %-24s %-14s %2d priced · certs: %s"
              % (b.supplier.name, b.response.extraction_status.value,
                 len([q for q in b.quotes if q.has_price]), certs))
    superseded = [r for r in svc.store.list_responses(rfq.id) if not r.is_active]
    print("  superseded     %d response(s) kept as history" % len(superseded))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--extract", action="store_true",
                    help="read the responses with the real model (a few minutes)")
    ap.add_argument("--clean", action="store_true",
                    help="delete every other RFQ from the local database first")
    args = ap.parse_args()

    settings = Settings.from_env()
    repo = RFQRepository(settings.db_path)
    svc = SupplierService(repo, get_ai_service(settings), settings)
    print("Database: %s" % settings.db_path)

    if args.clean:
        print("Removed %d other RFQ(s)." % clean_other_rfqs(repo))

    rfq = build_rfq(repo)
    print("Built %s — %d line items, readiness %d%%, %d open question(s)"
          % (rfq.id, len(rfq.line_items), rfq.completeness.score, len(rfq.open_questions())))
    n = register_responses(svc, rfq.id)
    print("Registered %d response(s) from %d supplier(s), plus %s who never replies."
          % (n, len(SUPPLIERS), SILENT[0]))

    if not args.extract:
        print("\nNothing has been read yet. Open Quotes & Comparison and press "
              "'Run extraction', or re-run this with --extract.")
        return 0

    print("\nReading the responses with the real model. This takes a few minutes.")
    started = time.time()
    try:
        result = svc.extract_all(rfq.id, on_stage=lambda n, s: print("   [%s] %s" % (n, s)),
                                 only_pending=True)
    except AIUsageLimit as e:
        print("\n  STOPPED  %s" % e.user_message)
        return 2
    except AIError as e:
        print("\n  ERROR    %s" % e.user_message)
        return 1
    print("\nRead %d response(s) in %.0fs; %d failed."
          % (len(result["succeeded"]), time.time() - started, len(result["failed"])))
    report(svc, repo.get_rfq(rfq.id))
    return 0


if __name__ == "__main__":
    sys.exit(main())
