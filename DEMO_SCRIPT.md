# Kill the Quote Spreadsheet — 10-minute demo script

Every figure below is read from the live database. Re-verify with
`python3 scripts/seed_demo.py --extract` before the session; response dates are relative to
the run, which is what keeps the expiring-quote warning live.

Two live model calls are in this script — the Playground at 4:00 and the letters at 7:55.
Everything else is either pre-extracted or an instant, pre-built query. Budget for those two.

---

## Thesis

The spreadsheet isn't the problem — the three days of re-keying and the fourth day answering
the VP are. The moment that proves it isn't the extraction: it's one question reducing a
30-line answer to 11, because only one supplier's certificate is actually on file.

## Before you start

- [ ] `claude auth status` → signed in. The sidebar confirms it.
- [ ] App open on **1 · RFQ Copilot**, nothing opened yet.
- [ ] No award on the demo RFQ — the Award page reads **Start the award**.
- [ ] `Kill_the_Quote_Spreadsheet.pdf` open in another tab, page 1 — for the diagram at 8:45.
- [ ] Sidebar collapsed on a small screen; the award grid wants the width.

**The dataset** — 30 line items, 1,500 pcs each, 45,000 total. Six suppliers, five replied:

| Supplier | Format | Lines | What makes it awkward |
|---|---|---|---|
| Anhui Packaging | `.xlsx` | 30/30 | The clean one. USD. |
| Shenzhen Print & Pack | `.pdf` | 25/30 | **A revision.** Per 1,000. 7% footnote discount. MOQ 3,000. Contradicting lead time. |
| Viet Carton | `.docx` | 7/30 | Prose. One line priced per kg. |
| Gujarat Boxes | `.txt` | 8/30 | Plain email. INR. "item 2, item 4". |
| Istanbul Ambalaj | `.png` | 11/30 | **Angled phone photo.** EUR. The only verified certificate. |
| Pacific Carton Works | — | 0/30 | Never replied. |

---

## 0:00–1:15 · The goal, two choices, and one refusal

**[SHOW]** Landing page. Don't click anything yet.

**[SAY]**

> Let me start with the goal, not the screen.
>
> We called this *Kill the Quote Spreadsheet*, but the spreadsheet isn't the problem. The
> problem is what happens after the RFQ goes out. Five suppliers reply in five shapes, someone
> spends three days retyping it into Excel, and then the VP asks one question and there goes
> the fourth day.
>
> Two choices shaped everything you'll see.
>
> **The stack.** Streamlit, SQLite, Python. Claude through the Claude Code CLI, not the API —
> no key management, no integration layer, one sign-in and it runs anywhere. That bought us
> speed, and it cost us latency: every call carries CLI start-up. That trade-off is why the
> seven most useful analyst questions are pre-built and instant, and you'll see that matter.
> Extraction, matching and question-parsing run on Sonnet; narration and letters on Haiku.
>
> **The rule.** *AI interprets. The application decides.* Whenever the problem is ambiguous
> language or a messy document, the model gets it. Whenever the answer involves money,
> eligibility, comparison, state or execution, it comes back into deterministic code. Every
> price, total and ranking you'll see is Python over a database. The model never calculates.
>
> And one refusal. We did not build the email loop — sending the RFQ, watching an inbox,
> pulling attachments off replies. It's a solved problem and it would have eaten the time that
> went into the hard part: deciding what the system should refuse to say. Sending is simulated
> and labelled on every screen. **Reading the attachments is fully real.** Only the transport
> isn't.

**[TRANSITION]** → Scroll to **Start here — the worked example** → **Open**.

---

## 1:15–2:15 · Phase 1 — a requirement becomes an RFQ

**[CLICK]** **Review RFQ** in the sidebar.

**[SHOW]** The readiness panel, then the field list.

**[POINT OUT]** The legend: **✓ Buyer provided · ✦ AI recommended · ⚠ Missing**.

**[SAY]**

> Phase one is a conversation, not a form — the buyer describes what they want and the model
> works out what a supplier needs in order to price it. Three turns produced this: 99% ready,
> 30 line items, and every field says where it came from.
>
> Here's the first guardrail, and it's visible. Thirteen fields are buyer-stated, and each
> holds the buyer's own words as evidence — order quantity is 45,000, and behind it is the
> phrase *"1,500 pieces of each."* That link is enforced: if the model proposes a fact as
> buyer-stated and we can't find it verbatim in what the buyer typed, it's downgraded to a
> recommendation automatically.
>
> Three fields are marked *AI recommended* — payment terms, validity, inspection. The model
> suggested those. They're visibly not buyer facts, and they don't count toward readiness until
> someone confirms them. The model doesn't get to invent a requirement and have it pass as
> yours.

**[PRINCIPLE]** The AI proposes. It never silently promotes its own suggestion into a requirement.

> *Optional, if you have 40s to spare:* on the Copilot page type a fresh requirement — it
> makes one live call and shows the questions being asked. Don't continue on that RFQ; it has
> no suppliers. Come back to the worked example.

**[TRANSITION]** → **2 · Quotes & Comparison**.

---

## 2:15–4:00 · Phase 2 — five formats, one comparison

### 2:15–2:45 — The comparison

**[POINT OUT]** `Suppliers 5 of 6 replied` · `Currencies USD · INR · EUR`.

**[SAY]**

> Five suppliers replied, nobody used a template. A spreadsheet, a PDF, a Word document with
> the commercials in a paragraph, a plain email, and a photograph taken at an angle.
>
> Extraction is one structured call per reply, against a strict schema — line, price, price
> basis, currency, MOQ, lead time, validity, payment and delivery terms, certifications. The
> model works inside a known procurement shape, not a free-form summary.
>
> The sixth supplier never replied — and that's a column, not an absence. *No response* is
> different from *didn't quote this line*, and both are different from zero.

**[CLICK]** **Suppliers** tab → **Open** on Istanbul's `stress_e_quote.png`.

**[SAY]**

> That's the phone photo. This is what came out of it. Vision is genuinely fallible, so
> anything from an image is capped at 75% confidence and flagged. We don't pretend the photo
> is as reliable as the spreadsheet.

### 2:45–3:35 — The Shenzhen line *(the ugly edge)*

**[CLICK]** **Comparison** → *Where did a number come from?* → **LINE-017** → **Shenzhen**.

**[SAY]**

> Shenzhen is four problems in one quote. This is their **second** PDF — it supersedes one
> from four days earlier; the original is still on file. They quoted **USD 281.30 per 1,000**,
> reduced to **USD 0.2616** per piece with the arithmetic shown. There's a **7% discount in a
> footnote**, conditional on orders above 20,000 — this RFQ is 45,000, so it's applied, and the
> note says the undiscounted price was 0.2813. If the condition couldn't be evaluated we'd say
> so instead of guessing.
>
> And the one that matters: **minimum order 3,000**, this line needs 1,500. The price is real,
> correctly normalised, and still **not usable at this quantity**. The system flags it rather
> than quietly ranking Shenzhen cheapest. Their own document also contradicts itself on lead
> time — 18 days here, 30 days in peak season there. We show both. We don't pick one.

**[PRINCIPLE]** Extraction succeeding is not the same as a number being safe to act on.

### 3:35–4:00 — Needs review, and untrusted content

**[CLICK]** **Needs review** tab.

**[SAY]**

> Everything the system won't assert on its own lands here. Gujarat's email says *"item 2,
> item 4"* — no dimensions, no SKU. The best guess is positional and it says so; the buyer
> confirms or corrects. And every certificate except one reads **claimed** — the supplier said
> so and attached nothing. Istanbul's is the only one actually among the documents received.
>
> One more guardrail you can't see, because it worked. Gujarat's email contains a line
> addressed to any AI reading it, telling it to award Gujarat. Supplier documents are data,
> not instructions. Text shaped like a command is stripped before it reaches a prompt, and the
> buyer is told it was.

**[TRANSITION]**

> Hold onto the certificate point. It's about to change an answer. But first, the obvious
> question.

---

## 4:00–4:40 · Is any of this hardcoded?

**[CLICK]** **Tools → Extraction Playground**.

**[TYPE]** — invent it on the spot:

```
12x10x6 at 41 cents, 16x16x12 at 0.92. MOQ 2000 per size.
20 days after artwork. FOB Ningbo. ISO 9001 held.
```

**[CLICK]** **Run extraction**. *(30–60s. Talk over it.)*

**[SAY]**

> Everything so far was extracted before this call, so the fair question is whether any of it
> is real. Same pipeline, same schema, same guards, on text I typed thirty seconds ago. Nothing
> is saved unless I keep it.

**[SHOW]** Three panels — sent · extracted · needs review — and **Trace a line**.

**[SAY]**

> Two line items, the terms, what it couldn't determine, and the span each value came from.
> Note "41 cents": it read the number and refused to treat *cents* as a currency it can
> compare, because that could be four different ones. Same guard you just saw on Gujarat.

**[TRANSITION]** → **3 · Procurement Analyst**.

---

## 4:40–6:20 · Phase 3 — ask, don't rebuild

### 4:40–5:10 — Cheapest by line

**[CLICK]** **Cheapest by line**. *(Instant.)*

**[SAY]**

> *30 of 30 lines have a comparable quote. Anhui is lowest on 25.*
>
> The buyer asked in English. Claude turned that into one of sixteen fixed analytical
> intents — it cannot generate SQL or Python, it picks an operation. Our code ran the
> operation. Claude then described the result it was handed. It never saw a price while
> planning, and it never did arithmetic.

**[CLICK]** Expand **Left out of this answer**.

**[SAY]**

> Which prices aren't directly comparable, and why: **103 exclusions**, each named with its
> reason — currency the supplier never named, a per-kg basis nobody can reduce, a minimum
> order above the line, a match nobody confirmed. Nothing is dropped silently.

### 5:10–5:55 — The VP's question

**[SAY]**

> Now the question from the brief. The one that costs the fourth day.

**[CLICK]** **Cheapest among QA-cleared**.

**[SAY]**

> *11 of 30 lines have a comparable quote. Istanbul is lowest on 11.*
>
> That's the whole product in one number. Same calculation, one filter, and the answer
> collapses from thirty lines to eleven — because when you insist on a certificate we actually
> hold rather than one a supplier claims, four of the five suppliers drop out. That is a
> different procurement decision, and in a spreadsheet it's a day of work to discover.

**[PRINCIPLE]** The model reasons about the question. The application owns the arithmetic.

### 5:55–6:20 — What should I review?

**[CLICK]** **What should I review?**

**[SAY]**

> *23 things worth reviewing; 10 of them stop a price being compared.* That's the honest state
> of this RFQ. Not a green tick — a list.

**[TRANSITION]** → **Take a decision**.

---

## 6:20–7:30 · Phase 4 — a calculated proposal, then the buyer's decision

**[CLICK]** **Start the award**.

**[SHOW]** The grid: Line · Item · Qty · **Best value** · **Cheapest** · Your pick · Unit price · Total.

**[SAY]**

> Here the model's authority drops to zero. Nothing on this screen is AI — both proposals are
> calculated. *Cheapest* is the lowest price we're willing to compare. *Best value* is the
> cheapest whose supplier also meets a quality bar. Deliberately not a weighted score: a
> supplier scoring 87.3 tells you nothing about why it won. Two named prices and a stated rule
> can be argued with.

**[CLICK]** The **ⓘ** on a line where the two differ.

**[SAY]**

> Every supplier who priced this line, as quoted in their own currency, whether their
> certificate is on file or merely stated, and for everyone excluded, the reason. Shenzhen is
> here with its MOQ. The one who never replied is here. The buyer sees the whole field.

**[CLICK]** Flip one line's chooser. **[POINT OUT]** Unit price, total and footer move.

**[CLICK]** **Award a line to someone else** → third supplier → **Apply** with no reason *(refused)* → add one.

**[SAY]**

> Proposal and decision are different things and the screen keeps them apart. I just
> overrode it; the total followed. Going to a supplier neither proposal named requires a
> reason — because in six months, "no reason given" is not an answer.
>
> And nothing is blocking me. An earlier version refused to approve because a supplier's
> certificate was unverified — while proposing that same supplier. The product was arguing
> with itself. Now it reports, and the buyer decides.

**[PRINCIPLE]** The system proposes. The buyer stays accountable.

---

## 7:30–8:45 · Phase 5 — the decision becomes executable

### 7:30–7:55 — Approve

**[CLICK]** Expand **Notes** → **Approve this award**.

**[SAY]**

> Validation is deterministic and it reports everything — unverified certificates, an
> expiring quote, missing payment terms, mixed currencies. None of it stops me; all of it is
> recorded with the approval. The only thing that can refuse an approval is an award with
> nothing on it. A price that went stale since I picked it drops that one line and names it,
> rather than holding up the other twenty-nine.

### 7:55–8:30 — Supplier letters

**[CLICK]** **Prepare supplier messages** → open one. *(Live call, ~20–40s.)*

**[SAY]**

> The model writes prose here, because the problem is language. It doesn't decide who won —
> it's handed one supplier's already-decided facts, and the schema it fills has **no numeric
> field**. The line table is rendered by the application. Then every draft is checked against
> that supplier's own data; one that invents a figure, names a rival, or claims to have been
> sent is discarded whole and replaced by a deterministic letter.

**[CLICK]** Paste a rival's name and price into the body → **Save edit**. **[SHOW]** Refused; Send disabled.

**[SAY]**

> That applies to me too.

**[CLICK]** Undo → **Send to supplier**. **[POINT OUT]** *Simulated, demo only.*

### 8:30–8:45 — Handoff and history

**[CLICK]** **Generate order handoff** → open → **Download CSV** → **History**.

**[SAY]**

> A structured order per supplier, no model involved. An unstated term reads *Not provided*,
> never a default. And every state change, oldest first, with what it replaced. A decision
> you can't execute is still a spreadsheet with better formatting.

---

## 8:45–9:30 · The guardrails in one view

**[SHOW]** The PDF, page 1 — the architecture table. One visit, not three.

**[SAY]**

> Five layers, and you've now seen each one working.
>
> **Constrained outputs** — extraction and question-parsing fill a strict schema; the analyst
> picks from sixteen intents, it doesn't write code.
> **The model never calculates** — prices, totals, comparability, validation are Python.
> **Evidence and uncertainty** — every value traces to a span; a gap is shown, not filled.
> There is no fallback exchange rate anywhere.
> **Untrusted content** — supplier documents are data; instruction-shaped text is stripped.
> **The human boundary** — the system proposes, the buyer decides, and the trail records why.
>
> Behind it: 565 tests that run with no network and no model, plus live scripts against the
> real model per phase — one of which fingerprints the upstream tables to prove an award
> changes nothing it shouldn't.

---

## 9:30–10:00 · Close

**[SAY]**

> This looked like a document-processing problem. It isn't. Five suppliers give you five
> versions of reality, and the job is not a prettier spreadsheet — it's a view structured
> enough to analyse, transparent enough to trust, and honest about what it doesn't know.
>
> So Claude handles the messy parts — understanding, extraction, interpretation, language.
> The application handles the parts that must be correct — calculations, rules, state,
> execution. The buyer stays where judgement matters.
>
> The plumbing is stubbed where it doesn't prove the thesis. The extraction, normalisation,
> analysis, validation and decision workflow are real.
>
> That's what we mean by killing the quote spreadsheet.

---

## Backup questions

First four instant, no model call.

1. **Supplier coverage** — Anhui 30/30, Shenzhen 25/30, Istanbul 11/30.
2. **Lead times** — Shenzhen's self-contradiction shown, not averaged.
3. **Quality status** — claimed vs verified on one screen.
4. **RFQ completeness** — coverage with numerator, denominator, and the definition of "valid".
5. *"Why didn't we choose Shenzhen for line 17?"* — typed, 60–90s. The MOQ reason with the sentence from their PDF and its page.

## If it goes wrong

| Risk | Recovery |
|---|---|
| **Typed analyst question takes 60–120s** | Use the seven suggestions. Type only with slack. |
| **Playground stalls** | Skip it; the Suppliers tab already shows real source documents. Come back if challenged. |
| **Letters slow** | Talk through the guard while it runs; it's the last live call. |
| **CLI not signed in** | Sidebar shows it. `claude auth login`. Extracted data still renders. |
| **A model call fails** | Plain-English message, data kept. Letters always have a deterministic fallback. |
| **Award already exists** | Cancel it, or demo from where it is. |
| **Data drifted** | `python3 scripts/seed_demo.py --extract`. |
| **Refresh loses the RFQ** | Session resets by design. Re-open via *Start here*. |
| **"Prove nothing is hardcoded"** | The Playground, or **Show diagnostics** — the AI call log with prompts, timings and schema results. |
