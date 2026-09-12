# AI Procurement Analyst

A working prototype that takes a buyer from *"I need carton boxes"* to a supplier-ready
RFQ, reads messy supplier replies into one comparison you can trust, lets the buyer
interrogate that comparison in plain English, and carries the decision through to an award
with a letter for each supplier and a structured order handoff.

Four phases are built:

| Phase | What it does |
|---|---|
| **1 · RFQ Copilot** | The buyer describes a need in their own words. The AI identifies the product and category, asks only the questions that materially affect supplier pricing, and produces a structured RFQ with line items, provenance and a readiness score. |
| **2 · Supplier Response Intelligence** | Five suppliers reply in five different formats. The system reads each document, extracts what was actually written, matches supplier lines to RFQ lines, normalises prices where that is safe, and presents a side-by-side comparison with the evidence still attached. |
| **3 · Procurement Analyst** | The buyer asks questions — *"who is cheapest for each line?"*, *"only among suppliers who cleared QA"*, *"why was Shenzhen excluded?"* — and gets answers calculated from those same quotes, with the method, the assumptions, what was left out, and the supplier's own words behind every figure. |
| **5 · Award & Execution** | Every line is seeded with two proposals — the cheapest quote and the cheapest that clears the buyer's quality bars — and the buyer picks, or overrides with a reason. The award is validated, approved, and turned into one letter per supplier and a structured order handoff, with an append-only audit trail behind it. |

Phase 4 as briefed — a weighted best-value score — is **not** built. Phase 5 needed an
award decision to execute, so it has a deliberately minimal one: *best value* is the
cheapest quote that clears explicit, buyer-set bars, never a weighted score. See
[Phase 4 and the minimal award layer](#phase-4-and-the-minimal-award-layer).

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
- Suppliers quote in different currencies. **Show prices in** converts them at a live
  published rate, and every converted figure names the rate, the provider and the date
  it was published, with the supplier's original figure kept beside it.
- Every certification reads **claimed**, not verified, because no certificate file arrived.
- Shenzhen's first quote contradicts itself on lead time (15 days on page 1, 25 on page 3).
  Both are kept. Their revision supersedes it without deleting it.

### Phase 3 — ask the analyst

1. From the comparison, press **Ask the analyst**, or open **Procurement Analyst**.
2. The six suggested questions answer instantly: they are pre-built queries and make no
   model call. Typing a question of your own costs two calls and about a minute.
3. Every answer carries **How this was calculated** (the steps, the assumptions, the
   exchange rates and where they came from), **Left out of this answer** (every supplier
   and line that was excluded, with the reason), and **Evidence** popovers showing the
   supplier's own sentence and where in their document it appears.

The sequence worth walking through on the 30-line RFQ:

| Ask | What it shows |
|---|---|
| *Who is cheapest for each line?* | A price per line with a runner-up and a gap, and 104 cells left out — each one named and explained. |
| *Now only among suppliers who cleared QA.* | The same calculation over one supplier, because only Istanbul holds a certificate we actually have. The assumption is printed under the answer. |
| *Why didn't we choose Shenzhen for line 17?* | Their MOQ of 4,000 exceeds the line's 1,500, and their certifications are claimed rather than document-backed — with the sentence from their PDF and its page number. |
| *What percentage of the RFQ has valid quotes?* | The percentage with its numerator, its denominator and the definition of "valid". |
| *Who quoted the most lines?* | Coverage per supplier, with declines, silences and unresolved prices counted separately. |
| *What should I review before deciding?* | Every open issue, blocking ones first. |
| *Which suppliers quote FOB terms?* | An open-ended read of the stored data, quoting each supplier's own wording. |
| *What is the best supplier in China?* | The analyst compares the suppliers who responded and says plainly that it has no data on suppliers outside this RFQ. |

A what-if — *"what if we ignore minimum order quantities?"* — is labelled as one and
changes only what is counted. `sqlite3 data/rfq_copilot.db .dump` is byte-identical before
and after.

### Phase 5 — award it and tell the suppliers

1. From the comparison or the analyst, press **Take a decision**, or open
   **Award & Execution**. **Start the award** seeds every line.
2. Each line carries two proposals. *Cheapest* is the lowest valid quote. *Best value* is
   the cheapest quote that clears the bars you set at the top of the page: a certificate
   we actually hold, a firm (non-conditional) quote validity, and an optional lead-time
   limit.
3. On the strict default, **19 of the 30 lines have no best-value candidate at all** —
   only one supplier in this dataset holds a document-backed certificate, and it quoted 11
   lines at 26 days. The screen says so rather than showing an empty column, and offers the
   relaxation as one click, recorded as an assumption.
4. Relaxed, with a 22-day lead-time limit, the two baskets are comparable and the trade-off
   is real: **cheapest USD 41,919.60 against best value USD 42,000.00 — USD 80.40 (0.19%)
   more, for a 21-day lead time instead of 26.** Both figures are computed in Python.
5. Override any line to any supplier with a **valid** price on it. A reason is required, and
   an override survives a re-seed; a line still on its seed is re-seeded and the screen says
   what it moved from.
6. **Approve** runs validation. Blocking findings disable the button; warnings must each be
   ticked, and the list of what you acknowledged is stored on the approval event.
7. **Prepare supplier messages** makes one call per supplier. Claude writes prose only — it
   is never given a rival's name, price or ranking, and the schema it fills has no numeric
   field. The line table under each letter is rendered by the application.
8. Every draft is checked against that supplier's own facts before you see it. A draft that
   invents a figure, names another supplier, composes a term or claims to have been sent is
   **discarded whole** and replaced by a deterministic letter that says exactly as much as
   the award supports. Your own edits face the same check, and a message that fails it
   cannot be recorded as sent.
9. Sending is **simulated and labelled as such** on every screen that mentions it. Nothing
   leaves the machine; there is no mail connection.
10. **Generate order handoff** re-validates and produces a structured order per supplier,
    with CSV and Markdown downloads. No model is involved. A term the supplier never stated
    reads *Not provided* — never a default.
11. The **History** popover in the header replays every state change with the state it
    replaced.

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
  └─ AnalystService ─────────► Phase 3: questions about that comparison
        ├─ analyst_prompts        question → structured query; result → one sentence
        ├─ analyst_guards         resolve names, validate the query, check the sentence
        └─ analyst_calculations   every figure, computed in Python
  └─ AwardService ───────────► Phase 5: award, communication and handoff
        ├─ award_calculations     seeding, the bars, totals, the basket delta
        ├─ award_validation       blocking / warning / info findings, per field
        ├─ award_guards           neutralise supplier text; verify every draft letter
        └─ award_prompts          prose-only schema, plus the deterministic letter
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
- a currency is converted only at a real published rate that is shown alongside it, and
  a price whose currency the supplier never named is not converted at all;
- a certification stays **claimed** until the certificate itself is among the documents;
- a line the supplier did not price is **not quoted**, never `0`;
- an ambiguous line match asks for confirmation instead of picking;
- two contradictory statements are both kept, with their own evidence;
- no exchange rate is ever invented: if rates cannot be fetched, prices stay in the
  currency each supplier used rather than being converted on a guess.

### The rule the whole of Phase 3 rests on

> **The model is not the database.**

Claude does two narrow jobs: it turns a question into a constrained `AnalystQuery`, and it
later puts a calculated result into a sentence. It is never shown a price while planning,
and never asked to work one out. Everything between — retrieval, filtering, ranking,
percentages, eligibility, evidence — is deterministic Python over the *same*
`build_comparison()` dataset the Quotes screen renders, so an answer can always be
reconciled with what is on that screen.

What falls out of that split:

- a question the data cannot settle is **refused** in fixed words, not answered plausibly;
- a supplier or line the RFQ does not contain is a refusal, never a silent drop;
- "cleared QA" means a certificate we hold, and the answer says so every time;
- a what-if ("ignore MOQ", "treat claims as verified") is **labelled** and changes only
  what is counted — never a stored record;
- every exclusion is listed with its reason and, where one exists, its evidence span;
- a narration containing a figure the result does not support is discarded, and the
  deterministic summary stands alone;
- the analyst describes which quote is lowest; it never recommends or awards. That belongs
  to the award screen, and `analyst_guards._AWARD_LANGUAGE` discards any narration that
  strays into it.

### The rule the whole of Phase 5 rests on

> **Nothing leaves the building that the award does not already support.**

Claude's entire job in Phase 5 is one call per supplier that writes prose a human reads
before sending. Seeding, picking, validating, totalling and the handoff are pure Python.

- The fact pack handed to the model holds **one** supplier. There is no field a rival's
  name or price could occupy, so a leak needs a code change that shows up in review rather
  than a prompt that drifts.
- The schema the model fills has **no numeric field**. The line table under the letter is
  rendered by the application from the award.
- Every figure in a draft must match one in that supplier's own pack. A rival's price is
  caught by the same check that catches an invented one — and so is a buyer who pastes one
  in by hand, which is why an unverified edit cannot be recorded as sent.
- A date must be one the supplier wrote; a commercial term must be quoted, not composed.
- A failed draft is **discarded whole, never repaired**. The deterministic letter is
  already complete and correct, so there is nothing to gain from showing one we cannot
  stand behind.
- Supplier-written text is neutralised before it reaches a prompt. A sentence shaped like
  an instruction is dropped and the buyer is told, rather than quietly sanitised into
  something plausible.
- `award_lines` carries `UNIQUE(award_id, line_item_id)`, so awarding one line to two
  suppliers is unrepresentable rather than merely validated against.
- `add_event` is deliberately **not** best-effort, unlike the analyst's audit writes:
  losing an analyst row costs an answer's provenance, losing an award row costs the
  decision's defensibility, so the exception propagates and rolls back the change it
  described.

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
python3 scripts/award_smoke.py      # Phase 5: seed, approve, draft, handoff, leak test
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
API-key management. Recording a message as sent is a simulation, labelled as one wherever
it appears, and the order handoff is a document you download rather than a purchase order
submitted anywhere. No payment, invoice matching, goods receipt or logistics tracking. No
weighted best-value score, no negotiation rounds, no multi-buyer approval chain, and no
splitting one line's quantity across suppliers.

## Known limitations

- **PDF reading** handles text-layer PDFs, including compressed streams. A scanned PDF
  with no text layer is reported as unsupported rather than guessed at.
- **Image reading** is genuine vision transcription and genuinely fallible. Values from a
  photograph are capped at 75% confidence and should be spot-checked.
- **Cross-currency** comparison uses live mid-market rates from a free public endpoint
  (`open.er-api.com`, with `frankfurter.app` as a fallback), cached for six hours. These
  are reference rates, not the rate your bank will give you, and they exclude any fees.
  A price whose currency the supplier never named is never converted.
- **Extraction takes time**: roughly 30–90 seconds per supplier response. Responses are
  processed four at a time (`RFQ_EXTRACTION_WORKERS`), so a five-supplier RFQ takes
  around 90 seconds rather than six minutes.
- The demo dataset is fabricated. Supplier names, contacts and prices are invented.

- **An analyst question takes time**: two model calls, roughly 60–120 seconds. The six
  suggested questions on the Analyst page are pre-built queries and answer instantly with
  no model call at all. Turning the narration off (`RFQ_ANALYST_EXPLAIN=0`) costs one call.
- **Term evidence**: Phase 2 stores the source span for prices and minimum order
  quantities, but not for lead time, validity, payment or delivery terms. Asking where one
  of those came from returns the extracted wording and says plainly that no location was
  recorded, rather than implying one.

- **A supplier letter takes time**: one model call per supplier, roughly 30–60 seconds
  each. There is always a deterministic letter, so a slow or failed call is never a dead
  end.
- **One supplier per line.** Splitting a line's quantity across two suppliers is
  unrepresentable by design — the `UNIQUE(award_id, line_item_id)` constraint is what holds
  that line.

## Phase 4 and the minimal award layer

Phase 4 as briefed is a weighted best-value score. It is not built, and Phase 5 never reads
one. What Phase 5 genuinely needed was an award *decision* to execute, so it has the
smallest honest one: **best value is the cheapest quote that clears explicit bars**, each
bar set by the buyer on the screen and each failure named. No weights, no composite score,
nothing to tune until it produces the answer someone already wanted.

The engine a weighted Phase 4 would need is in place either way:
`rfq_copilot/analyst_calculations.py` is pure over the comparison dataset — cheapest by
line, coverage, MOQ, lead time, qualification — and `award_calculations.propose_award`
reuses those same functions rather than reimplementing the price rule, so a scoring layer
would slot in beside `clears_bars` without a second source of truth.
