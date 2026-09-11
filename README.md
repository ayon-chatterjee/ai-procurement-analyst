# AI Procurement Analyst

A working prototype that takes a buyer from *"I need carton boxes"* to a supplier-ready
RFQ, then reads messy supplier replies and turns them into one comparison you can trust.

Two phases are built:

| Phase | What it does |
|---|---|
| **1 · RFQ Copilot** | The buyer describes a need in their own words. The AI identifies the product and category, asks only the questions that materially affect supplier pricing, and produces a structured RFQ with line items, provenance and a readiness score. |
| **2 · Supplier Response Intelligence** | Five suppliers reply in five different formats. The system reads each document, extracts what was actually written, matches supplier lines to RFQ lines, normalises prices where that is safe, and presents a side-by-side comparison with the evidence still attached. |

Phase 3 (a natural-language analyst) and Phase 4 (awarding) are **not** built.

---

## Running it

Requires Python 3.9+ and the Claude Code CLI, signed in. Everything else is already
in the standard environment; there is nothing to install.

```bash
python3 -m streamlit run app.py
```

Then open http://localhost:8501.

### Authentication — no API key

This prototype calls Claude through the **Claude Code CLI** using your Claude
subscription. There is no `ANTHROPIC_API_KEY`, no Anthropic Console account and no
second provider. If the app reports that Claude is not authenticated:

```bash
claude auth login
claude auth status
```

On a Team plan, check `auth status` reports your team subscription. A stale personal
entitlement on the CLI will be refused with a usage-limit message even when your
account has capacity.

---

## The demo

### Phase 1 — build an RFQ

1. Open **Copilot** and type something vague, e.g. *"I need corrugated carton boxes."*
2. The analyst classifies the product and asks the questions that matter for it, grouped
   by section. Answer some, skip others, or just describe everything in your own words.
3. Readiness updates as you go. **Review RFQ** shows the structured result, where every
   value came from, and what is still missing.

### Phase 2 — read the supplier replies

1. Open **Quotes & Comparison** with an RFQ open.
2. **Load supplier responses** registers five suppliers, their documents, one revision
   and one supplier who never replies. Nothing is sent or received; files are read from
   `fixtures/suppliers/`.
3. **Run extraction** reads each document and normalises what it finds. This takes a few
   minutes: five suppliers, two model calls each.
4. The comparison shows a normalised price per piece per line. Pick a line to see, for
   each supplier, what they actually wrote, what it was reduced to, and **Where from?**
   opens the exact source span.

Things worth looking at in the demo:

- **Shenzhen** quotes per 100 pieces with a 5% discount buried in a page-2 footnote. Both
  the original and the normalised price are shown, and the discount keeps its condition.
- **Gujarat** writes "41 cents" without naming a currency, so the price is held out of the
  comparison rather than guessed at.
- **Viet Carton** prices one size per kilogram, which cannot be reduced to a per-piece
  figure without a weight the supplier never gave.
- **Istanbul** sent a photograph. It is transcribed by Claude's vision and capped at 75%
  confidence, because optical reading can misread a digit.
- Every certification reads **claimed**, not verified, because no certificate file arrived.
- Shenzhen's first quote contradicts itself on lead time (15 days on page 1, 25 on page 3).
  Both are kept. Their revision supersedes it without deleting it.

---

## Architecture

```
UI (Streamlit)
  └─ RFQService ─────────────► Phase 1: RFQ construction
  └─ SupplierService ────────► Phase 2: supplier responses
        ├─ DocumentExtractor      xlsx / pdf / docx / txt / image → text + positions
        ├─ SupplierExtractor      one structured AI call per response
        ├─ supplier_guards        evidence, claims, conflicts, currency
        ├─ line_matcher           supplier line → RFQ line, with a status
        └─ quote_normalizer       price basis, discounts, MOQ, lead time
                    │
              AIService → ClaudeCLIProvider → claude -p --json-schema
                    │
              SQLite (rfq_copilot/persistence.py)
```

Business logic never lives in a Streamlit page. The pages render state and submit actions.

### The rule the whole of Phase 2 rests on

> **A supplier response is evidence, not truth.**

The model reports *observations*: what the supplier wrote, and where. Deterministic code
decides what the application is willing to assert. That split is why:

- a price whose evidence span cannot be found in the document is held for review;
- a certification stays **claimed** until the certificate itself is among the documents;
- a line the supplier did not price is **not quoted**, never `0`;
- an ambiguous line match asks for confirmation instead of picking;
- two contradictory statements are both kept, with their own evidence;
- no currency is ever converted, and no exchange rate is ever invented.

---

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

Fast, no network, no model calls. Covers the schema, guards, document extraction,
normalisation, matching, persistence and the service flows, including the negative
guarantees above.

Live checks that use the real model and the real fixtures:

```bash
python3 scripts/smoke.py            # Phase 1: category-specific questions, line items, corrections
python3 scripts/supplier_smoke.py   # Phase 2: all five formats end to end
python3 scripts/stress_test.py      # Phase 2 at 30 line items
```

Regenerate the fixtures (they are committed, so this is only needed if you change them):

```bash
python3 scripts/make_supplier_fixtures.py
python3 scripts/make_stress_fixtures.py
python3 scripts/seed_phase2_demo.py   # a 7-line carton RFQ with responses registered
```

---

## What is deliberately not here

No email of any kind, no SMTP, IMAP, Gmail or Outlook. No supplier portal, no
authentication, no cloud deployment, no ERP integration. No second AI provider and no
API-key management. No award recommendation, no "cheapest supplier", no best-value
scoring: Phase 2 shows normalised prices and stops short of telling you who to pick.

## Known limitations

- **PDF reading** handles text-layer PDFs, including compressed streams. A scanned PDF
  with no text layer is reported as unsupported rather than guessed at.
- **Image reading** is genuine vision transcription and genuinely fallible. Values from a
  photograph are capped at 75% confidence and should be spot-checked.
- **Cross-currency** comparison is not attempted. Prices stay in the currency each
  supplier used.
- **Extraction is slow**: roughly 30–90 seconds per supplier response, sequentially.
  Reliability was preferred over parallelism.
- The demo dataset is fabricated. Supplier names, contacts and prices are invented.

## Phase 3 readiness

Phase 2 produces the normalised dataset Phase 3 will query, joined on stable ids:
`rfq_id`, `line_item_id`, `supplier_id`, `response_id`, `question_id`, `field_key` and
`evidence_id`. `SupplierService.build_comparison()` returns it in one object, so questions
like *"who quoted Line 17, and where did that number come from?"* are already answerable
from stored data.
