# AI Procurement Analyst — Phase 1: RFQ Copilot

A buyer says what they need in plain language. The copilot works out what suppliers
actually need in order to quote accurately, asks only those questions, and turns the
conversation into a structured, traceable, supplier-ready RFQ.

> **This prototype uses Claude Code through your Claude subscription and does not require an Anthropic API key.**

This is a two-day prototype of one vertical slice, not a production procurement platform.

---

## What Phase 1 does

| Capability | Behaviour |
|---|---|
| Understands a vague request | "I need corrugated carton boxes." → product, category, product type |
| Decides what matters | Picks the procurement dimensions that change price, feasibility, lead time or comparability for *this* category |
| Asks good questions | Each question carries a concrete reason ("Board grade drives material cost and box strength"), capped at 8 on the first turn and 3 after |
| Never invents requirements | A value is only a buyer fact when a verbatim span of the buyer's own words backs it up; everything else stays an AI recommendation |
| Answers naturally | Question cards with inline inputs, or one free-text box that can answer several questions at once |
| Never re-asks | Answered, skipped, dismissed and already-filled questions are off-limits, including rephrasings |
| Handles unknowns and conflicts | "I don't know the flute" is recorded as *unknown*; two contradictory statements become a *conflict* with a clarifying question, never a guess |
| Multiple line items | Seven listed sizes become seven line items with stable IDs (`LINE-001`…), per-line quantities and their own specifications |
| Readiness ≠ completeness | A deterministic 0–100 completeness score, and a separate deterministic `ready_to_send` verdict that the model cannot override |
| Traceable | Every field carries its source, evidence span and the message it came from; every AI call is logged with model, duration, prompt version and raw response |
| Persistent | SQLite; restart the app and every RFQ, message and audit row is still there |

No email, no SMTP, no Gmail or Outlook, no supplier communication, no authentication,
no cloud, no API-key management, no multi-provider routing. Those are out of scope here.

---

## Requirements

* macOS or Linux with **Python 3.9+** (developed against the macOS system Python 3.9.6)
* **Claude Code CLI**, signed in: run `claude` once in a terminal and complete login
* Python packages: `streamlit>=1.49`, `pandas`, `jsonschema` (all already present on the dev machine)

```bash
python3 -m pip install -r requirements.txt
```

Check the CLI is ready:

```bash
claude auth status
```

`loggedIn: true` means the copilot can run. The app also shows sign-in state in the sidebar
and refuses to start an RFQ with a clear message if the CLI is missing or signed out.

---

## Run it

```bash
python3 -m streamlit run app.py
```

Then open http://localhost:8501.

A first analysis takes roughly 25–60 seconds on Sonnet, because the model is genuinely
reasoning about the product category. The UI narrates the wait and never fakes progress.
For faster rehearsal at lower question quality:

```bash
RFQ_AI_MODEL=haiku python3 -m streamlit run app.py
```

### Demo flow

1. Open the Copilot and enter **"I need corrugated carton boxes."**
2. Read the questions: dimensions, board grade, flute, printing, quantity, destination.
   Every one names why it affects a supplier's price.
3. Paste a real buyer answer into *Or just tell me in your own words*:

   ```
   We need:
   10 × 10 × 5
   12 × 10 × 6
   15 × 10 × 8
   18 × 12 × 10
   20 × 15 × 10
   24 × 18 × 12
   30 × 20 × 15
   inches. 2,000 pieces each. Ship to Mumbai.
   ```

   Seven line items appear, each with its own dimensions and quantity. Sizes, quantity and
   destination are not asked again. Readiness climbs.
4. Answer or skip the rest, then open **Review RFQ**: sections, line items, provenance
   badges, evidence popovers, manual editing, JSON export, and *Mark supplier-ready*.
5. Start a second RFQ with **"I need steel construction brackets."** The questions change
   to material grade, thickness, load rating, surface treatment and drawings. Nothing about
   flutes or GSM. That difference is real model reasoning, not a lookup table.

---

## Architecture

```
Streamlit UI (ui/)            renders state, submits actions, no business logic
      ↓
RFQService (rfq_service.py)   one AI call per buyer turn, state transitions, audit
      ↓
AIService (ai_service.py)     ClaudeCLIProvider → `claude -p --json-schema` subprocess
      ↓
structured JSON (TurnOutput)  one strict schema for every turn (ai_schemas.py)
      ↓
guards (guards.py)            evidence, overwrite, conflict, N/A, line-item, question, readiness
      ↓
RFQ model (schema.py)         dataclasses, semantic field states, provenance
      ↓
SQLite (persistence.py)       rfqs · messages · ai_calls
```

**The division of labour is the core design.** The model decides what is relevant; deterministic
code decides what is allowed to change.

| Claude decides | The application decides |
|---|---|
| Product, category, product type | Whether evidence for a buyer fact actually exists |
| Which fields matter and how much | Whether a value may overwrite a buyer fact |
| Which questions to ask, their wording and reason | Question deduplication and caps |
| Extraction from natural language | Line-item merge safety and per-line quantity semantics |
| Line-item interpretation | The completeness score and the readiness verdict |
| Recommendations and the conversational reply | Field status enforcement, persistence, state transitions |

### Trust rules enforced in code, not just prompted

* **Evidence guard** — a `buyer_explicit` claim whose evidence span is not in the buyer's
  text is downgraded to an AI recommendation and the downgrade is written to the audit log.
* **Overwrite guard** — an AI recommendation can never overwrite a buyer fact.
* **Conflict guard** — two contradictory explicit values become `CONFLICT` plus a required
  clarifying question. Only an explicit buyer correction (`revision=correction`) replaces a value,
  and the superseded value stays in the field's history.
* **N/A guard** — `not_applicable` needs a written justification and can never be applied to
  something the buyer stated or said they did not know.
* **Question guards** — dedupe by field and by token similarity, never re-ask a resolved or
  skipped question, hard caps of 8 then 3, plus a safety net that adds the registry's standard
  question for a required field the model never covered so readiness is always reachable.
* **Readiness guard** — `ready_to_send` is false while any required field is missing, a
  conflict is open, a line item is incomplete, or a required question is unanswered. The
  model's own `score` and `ready_to_send` are kept as `ai_score` / `ai_ready_claim` for audit only.

### Field states

`PROVIDED` · `RECOMMENDED` · `MISSING` · `UNKNOWN` (buyer said they don't know) ·
`NOT_APPLICABLE` (with a reason) · `CONFLICT`. Unknown is never silently converted to
not-applicable, and a conflict is never silently resolved.

### Files

```
app.py                      Streamlit entry: theme + 3-page navigation
rfq_copilot/
  config.py                 settings from environment variables
  schema.py                 RFQ, LineItem, Question, Completeness, FieldValue, Message, AICallRecord
  fields.py                 universal field registry + standard fallback questions + cert vocabulary
  ai_schemas.py             TURN_OUTPUT_SCHEMA (strict) and the health-check schema
  prompts.py                system prompt (16 hard rules) and per-turn prompt builders
  ai_service.py             AIService interface, error taxonomy, ClaudeCLIProvider
  guards.py                 all deterministic guards, scoring and readiness
  rfq_service.py            orchestration, manual edits, state transitions, audit
  persistence.py            SQLite repository
ui/
  state.py  theme.py  components.py  page_copilot.py  page_review.py  page_saved.py
scripts/smoke.py            real-CLI end-to-end intelligence test
tests/                      63 unit tests (stdlib unittest)
```

---

## Tests

Unit tests use the standard library only, and a stub AI service. They never touch the network.

```bash
python3 -m unittest discover -s tests -t .
```

Covered: schema JSON round-trip and enum tolerance; the field registry; SQLite save/load/list/cascade;
every guard (evidence downgrade, overwrite refusal, correction vs conflict, unknown preservation,
N/A justification, seven-line-item handling, per-line quantity semantics, question dedupe and caps,
score clamping, AI readiness override); service flow (persistence before the AI call, answers surviving
a timeout, retry behaviour, manual edits as buyer facts, supplier-ready gating); and the CLI provider
(argv shape, `CLAUDECODE` scrubbing, envelope parsing, auth / transient / timeout / schema-violation mapping).

### Real-model smoke test

```bash
python3 scripts/smoke.py
```

Runs five live Sonnet calls (a few minutes) and asserts the intelligence claims:

1. **Carton boxes** are classified as packaging and asked about dimensions and board construction, with no facts invented from a one-line request.
2. **Steel construction brackets** get a different category and materially different questions (material grade, thickness, load rating, finish), with token overlap against the carton questions under 0.4 and no carton vocabulary.
3. **Seven sizes, 2,000 each, Mumbai** becomes seven line items with per-line quantities and buyer evidence, and nothing already answered is re-asked, by field or by rephrasing.
4. **"Large carton boxes… delivered to India"** invents no dimensions and no city, and asks what "large" means.
5. **"Actually, make that 8,000 pieces"** replaces the earlier figure, is not treated as a conflict, and keeps the superseded value in history.

Exits non-zero if any check fails.

---

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `RFQ_AI_MODEL` | `sonnet` | Model for RFQ turns |
| `RFQ_AI_FAST_MODEL` | `haiku` | Model for the lightweight health call |
| `RFQ_AI_TIMEOUT` | `180` | Seconds per CLI call |
| `RFQ_AI_EFFORT` | `low` | CLI reasoning effort; `default` gives deeper but much slower turns |
| `RFQ_AI_MAX_TURNS` | `6` | Internal CLI turn budget (structured output arrives as a tool call) |
| `RFQ_CLAUDE_BIN` | `claude` | Path to the CLI |
| `RFQ_DB_PATH` | `data/rfq_copilot.db` | SQLite file, resolved against the repo root |
| `RFQ_AI_LOG_PROMPTS` | `false` | Also store full prompt text in the audit log |
| `RFQ_MAX_QUESTIONS_FIRST` / `_TURN` / `RFQ_MAX_OPEN_QUESTIONS` | `8` / `3` / `8` | Question caps |

---

## Known limitations

* One buyer, one machine, no accounts. The SQLite file is local and `data/` is git-ignored.
* A Sonnet turn takes 25–60 seconds. That is real reasoning, not a loading animation.
* If Claude drops the connection mid-response or exhausts its internal turn budget, the turn is
  retried once automatically; a further failure surfaces a retry button with the buyer's answers intact.
* The completeness score is a deterministic weighting (required 3, recommended 1, optional 0,
  not-applicable excluded). It is a progress signal, not a quality judgement.
* Line items edited in the review table have their specifications re-parsed from
  `Name: value; Name: value` text, so free-form spec text collapses into one attribute.
* Questions are capped, so a very complex product may need an extra turn or two to reach readiness.
* The AI's own readiness claim is recorded but never displayed as the verdict, by design.

---

## Phase 2 direction (not built)

The data model is already shaped for supplier-response intelligence, and deliberately stops short of it.

* `FieldValue` carries `value / unit / source / evidence / source_refs / confidence` — the same
  record an extractor will produce from a supplier email or attachment. Phase 2 adds a
  `SUPPLIER_STATED` source and refs like `email:<id>`.
* Stable join keys already exist: **RFQ id**, **line item id** (`LINE-001`), **question id**,
  **field key**. Those are what answer "did this supplier quote line 17?", "did they answer
  question 4?" and "where did their value come from?".
* `AIService.complete_json` plus a strict schema, local validation and an `ai_calls` audit row is
  the pattern extraction and comparison calls will reuse.
* `messages` and `ai_calls` are RFQ-scoped append-only logs; `supplier_quotes` and `comparisons`
  would follow the same repository pattern.
* `RFQStatus` extends naturally to `sent`, `quotes_received`, `awarded`.

Supplier outreach, quote normalization, comparison, the natural-language analyst and award
recommendation are all still to come. Email stays simulated when they arrive.
