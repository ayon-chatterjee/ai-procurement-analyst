# Handover

Everything a new owner needs to run, understand and change this project.

---

## 1. What it is

A working prototype that takes a buyer from *"I need carton boxes"* to an awarded order:

| Phase | Screen | What it does |
|---|---|---|
| 1 | RFQ Copilot → Review RFQ | A vague requirement becomes a structured, supplier-ready RFQ. The AI asks only the questions that materially affect price. |
| 2 | Quotes & Comparison | Supplier replies in any format — spreadsheet, PDF, Word, email, a photograph of a printed quote — become one comparison, with every figure traceable to the words it came from. |
| 3 | Procurement Analyst | Plain-English questions about those quotes, answered by deterministic Python and narrated by the model. |
| 4–5 | Award & Execution | Cheapest vs best-value proposals per line, buyer decision and override, validation, one letter per supplier, order handoff, audit trail. |

Phase 4 as originally briefed (a weighted best-value score) was deliberately **not** built; see
*Phase 4 and the minimal award layer* in `README.md`.

---

## 2. Before anything else: authentication

**This is the only thing that does not travel with the repository.**

The application calls Claude through the **Claude Code CLI**, using a Claude subscription.
There is no `ANTHROPIC_API_KEY`, no Anthropic Console account, no second provider, and
nothing to copy from the previous owner.

```bash
claude auth login          # on the new owner's own Claude account
```

Until this is done, the app loads and every AI feature reports *"Claude Code isn't
authenticated"*. A working clone will look broken. This is the single most common cause of
a confusing first five minutes.

Check it at any time with `claude auth status`, or look at the sidebar in the app, which
says `Claude Code · signed in` when it is ready.

---

## 3. Getting it running

Requires **Python 3.9+** and the Claude Code CLI, signed in.

```bash
pip3 install -r requirements.txt
python3 scripts/seed_demo.py --extract
python3 -m streamlit run app.py
```

Then open <http://localhost:8501> and press **Open** on *Start here — the worked example*.

The middle command builds the demo: one RFQ with 30 line items, five suppliers replying in
five different formats, one who never replies, one revision, one self-contradiction, one
verified certificate and four claimed ones. It calls the real model and takes about three
minutes. Without `--extract` it registers the responses unread, so you can press **Run
extraction** in the UI and watch it happen.

Re-run it before any demo — response dates are relative to the run, which keeps the
expiring-quote warning live instead of drifting into the past. `--clean` removes every other
RFQ from the local database first.

**Verified on a fresh clone:** all 562 tests pass, and the app creates its own SQLite
database from nothing. There is no hidden setup step.

---

## 4. What is deliberately *not* in the repository

`data/` is gitignored. It holds:

- `rfq_copilot.db` — every RFQ, supplier response, extracted quote, analyst query and award
- `uploads/` — supplier documents added through the UI
- `fx_cache.json` — cached exchange rates, regenerated on demand

None of it transfers, and none of it should: it is the previous owner's sourcing data.
`scripts/seed_demo.py` rebuilds a complete, compelling demo from the committed fixtures in
`fixtures/`, so nothing of value is lost.

If you ever *do* move a database between machines, note that `supplier_documents.path`
points at absolute paths under `data/uploads/`. The extracted text is stored in the database
and still displays, but the original file will not open and re-running extraction on that
response will fail.

---

## 5. The rules this codebase is built on

These are not style preferences. Each one is enforced by tests, and breaking one silently
turns a trustworthy answer into a plausible-looking guess.

**The model is not the database.** Claude turns a question into a constrained query and
later puts a calculated result into a sentence. Every figure between those two points is
computed in Python over the same dataset the Quotes screen renders. Never let a prompt
return a price, a total or a ranking.

**Absence is never zero.** A supplier who did not quote a line reads *not quoted*. A missing
quantity is `None`, not `0`. A certificate nobody sent is *claimed*, never *verified*. An
unanswered questionnaire item is *not addressed*, never *no*.

**Never invent a currency or a rate.** `$` is deliberately unmapped — it could be USD, AUD,
CAD or SGD. A price whose currency the supplier never named is held out of the comparison
rather than guessed at. There is no fallback FX rate.

**Evidence or it did not happen.** A value the model reports must be traceable to a verbatim
span in the source document, or it is downgraded and flagged for review.

**Supplier text is untrusted input.** Documents may contain text shaped like instructions.
It is neutralised before reaching a prompt, never followed. See `award_guards.neutralise`.

**One supplier's letter contains only their own lines.** The fact pack handed to the model
holds exactly one supplier — there is no field a rival's price could occupy. A leak requires
a code change that shows up in review, not a prompt that drifts.

**Nothing is sent anywhere.** "Sending" a supplier message is simulated and labelled as such
on every screen. There is no mail connection, no ERP, no supplier portal.

---

## 6. Layout

```
app.py                     Streamlit entry point and navigation
ui/                        One module per screen, plus shared components
  page_copilot.py            Phase 1 — the conversation
  page_review.py             Phase 1 — the structured RFQ
  page_quotes.py             Phase 2 — comparison, evidence, review queue
  page_analyst.py            Phase 3 — questions and answers
  page_award.py              Phases 4–5 — decision and execution
  page_playground.py         Extraction bench (a developer tool, not part of the flow)
  components.py, theme.py, state.py, errors.py, previews.py
rfq_copilot/               All logic. No Streamlit imports anywhere in here.
  ai_service.py              The only place the Claude CLI is invoked
  guards.py                  Phase 1 trust rules (evidence, conflicts, provenance)
  supplier_*.py              Phase 2 extraction, matching, normalisation
  analyst_*.py               Phase 3 — calculations are in analyst_calculations.py
  award_*.py                 Phases 4–5 — seeding, validation, letters, handoff
  persistence.py             SQLite: every table, every migration
scripts/                   Seeding, fixtures, and live smoke tests
tests/                     562 tests, no network, no model calls
fixtures/                  The committed supplier documents
```

The dependency rule: `ui/` may import `rfq_copilot/`, never the reverse.

---

## 7. Testing

```bash
python3 -m unittest discover -s tests -t .        # 562 tests, ~30s, no network
```

`TEST_INVENTORY.md` lists every test by area and is generated —
regenerate with `python3 scripts/make_test_inventory.py` after adding tests.

Live checks that use the real model and cost a few minutes each:

```bash
python3 scripts/smoke.py             # Phase 1
python3 scripts/supplier_smoke.py --keep-db   # Phase 2 (note the flag)
python3 scripts/analyst_smoke.py     # Phase 3
python3 scripts/award_smoke.py       # Phases 4–5, incl. a deliberate data-leak test
python3 scripts/playground_smoke.py  # the extraction bench
```

`award_smoke.py` fingerprints the Phase 1–3 tables before and after to prove an award
changes nothing upstream. Run it after touching anything in `award_*.py`.

---

## 8. Configuration

Everything is an environment variable, read in `rfq_copilot/config.py`. Nothing is required.

| Variable | Default | Notes |
|---|---|---|
| `RFQ_AI_MODEL` | `sonnet` | Model for RFQ turns and extraction |
| `RFQ_AI_FAST_MODEL` | `haiku` | Narration and letters |
| `RFQ_AI_TIMEOUT` | `180` | Floor for one call, in seconds |
| `RFQ_AI_TIMEOUT_PER_KCHAR` | `25` | Added per 1,000 prompt characters |
| `RFQ_AI_TIMEOUT_MAX` | `600` | Ceiling, so nothing hangs forever |
| `RFQ_DB_PATH` | `data/rfq_copilot.db` | Point elsewhere to work on a copy |
| `RFQ_EXTRACTION_WORKERS` | `4` | Supplier responses read in parallel |
| `RFQ_ANALYST_EXPLAIN` | on | `0` skips narration, saving one call per question |
| `RFQ_AI_LOG_PROMPTS` | off | `1` stores full prompt text in `ai_calls` |

The call deadline scales with prompt size because a turn carrying a thirty-row variant table
legitimately needs about four minutes, while a flat 180 seconds failed exactly the turn a
buyer had put the most work into.

---

## 9. Platform notes and known limits

- **Document previews use macOS Quick Look.** On Linux or Windows the thumbnail is skipped
  and the extracted text is shown instead — no crash; the failure is handled.
- **Scanned PDFs** with no text layer are reported unsupported rather than guessed at.
- **Photographed quotes** are genuine vision transcription and fallible; values are capped
  at 75% confidence and should be spot-checked.
- **Exchange rates** come from a free public endpoint, cached six hours. Reference rates,
  not the rate a bank gives, and fees are excluded.
- **Timing.** Extraction is 30–90s per supplier response, four at a time. An analyst
  question is two model calls, 60–120s. A supplier letter is one call per supplier.
- **One supplier per line.** Splitting a line's quantity across two suppliers is
  unrepresentable by design — a `UNIQUE(award_id, line_item_id)` constraint holds that line.
- **Term evidence** is stored for prices and minimum order quantities but not for lead time,
  validity, payment or delivery terms. Asking where one of those came from returns the
  wording and says plainly that no location was recorded.

`README.md` carries the full *Known limitations* section.

---

## 10. First hour, suggested

1. `claude auth login`, then the three commands in §3.
2. Walk the demo end to end: Copilot → Review → Quotes → Analyst → Award. About ten minutes.
3. On the Quotes screen, open a supplier document and follow one price back to the sentence
   it came from. That drill-down is the point of the product.
4. Read §5 above, then `rfq_copilot/guards.py` and `rfq_copilot/supplier_guards.py`. Those
   two files are where the trustworthiness actually lives.
5. Run the test suite and skim `TEST_INVENTORY.md` to see what is already guaranteed.
