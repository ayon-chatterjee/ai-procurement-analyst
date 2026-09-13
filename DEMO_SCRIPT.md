# Kill the Quote Spreadsheet — 10-minute demo script

Every figure below is from the live database. Re-verify with
`python3 scripts/seed_demo.py --extract` before the session; response dates are relative to
the run, which is what keeps the expiring-quote warning live.

---

## Thesis

The spreadsheet isn't the problem — the three days of re-keying and the fourth day answering
the VP are. The moment that proves it isn't the extraction: it's watching one question reduce
a 30-line answer to 11, because only one supplier's certificate is actually on file.

## Before you start

- [ ] `claude auth status` → signed in. The sidebar confirms it.
- [ ] App running, landing on **1 · RFQ Copilot**, nothing opened yet.
- [ ] No award on the demo RFQ — the Award page should read **Start the award**.
- [ ] Sidebar collapsed if you're on a small screen; the award grid wants the width.

**The dataset** — 30 line items, 1,500 pcs each, 45,000 total. Six suppliers, five replied:

| Supplier | Format | Lines | What makes it awkward |
|---|---|---|---|
| Anhui Packaging | `.xlsx` | 30/30 | The clean one. USD. |
| Shenzhen Print & Pack | `.pdf` | 25/30 | **A revision.** Per 1,000. 7% footnote discount. MOQ 3,000. Self-contradicting lead time. |
| Viet Carton | `.docx` | 7/30 | Prose, not a table. One line priced per kg. |
| Gujarat Boxes | `.txt` | 8/30 | Plain email. INR. Refers to "item 2, item 4". |
| Istanbul Ambalaj | `.png` | 11/30 | **Angled phone photo.** EUR. The only verified certificate. |
| Pacific Carton Works | — | 0/30 | Never replied. |

---

## 0:00–1:10 · Goal, constraints, and what we refused to build

**[SAY]**

> Let me start with the goal, not the screen.
>
> We called this *Kill the Quote Spreadsheet*, but the spreadsheet isn't really the problem.
> The problem is what happens after the RFQ goes out. Five suppliers reply in five different
> shapes, and someone spends three days retyping it all into Excel before anyone can decide
> anything. Then the VP asks one question, and there goes the fourth day.
>
> So we built one continuous flow: requirement, RFQ, supplier responses, one comparable
> dataset, questions, decision, execution.
>
> Three constraints we imposed on ourselves, because they shaped everything you'll see.
>
> **One — the AI is real, but it is never the source of truth.** Claude reads documents and
> interprets questions. It does not calculate. Every price, total and ranking comes from
> Python over a database.
>
> **Two — suppliers reply however they like.** We never sent anyone a template. The system
> absorbs the mess instead of asking five companies to change how they work.
>
> **Three — when the system isn't sure, it says so.** A missing quote is never a zero. A
> certificate a supplier merely claims is never shown as verified.
>
> And one thing we deliberately **did not** build: the email loop. Sending the RFQ out,
> watching an inbox, pulling attachments off replies. Wiring a mailbox is a solved,
> unremarkable problem, and it would have eaten the time that went into the part that's
> actually hard — deciding what the system should refuse to say. So sending is simulated and
> labelled as such on every screen.
>
> To be clear about which half is stubbed: **reading the attachments is completely real.**
> Only the transport isn't.
>
> Let me show you what that produces.

**[TRANSITION]** → Click **1 · RFQ Copilot**.

---

## 1:10–2:20 · Phase 1 — a requirement becomes an RFQ

### 1:10–1:35 — RFQ Copilot

**[SHOW]** The landing page and the demo card *Start here — the worked example*.

**[SAY]**

> Phase one is a conversation, not a form. The buyer describes what they want; the AI works
> out what suppliers actually need in order to quote accurately. I'll open the finished one,
> because the interesting part isn't that a chatbot asks questions — it's what it refuses to
> do with the answers.

**[CLICK]** **Open** → then **Review RFQ** in the sidebar.

### 1:35–2:20 — Review RFQ

**[SHOW]** The readiness panel, then the field list.

**[POINT OUT]** The legend: **✓ Buyer provided · ✦ AI recommended · ⚠ Missing**.

**[SAY]**

> Three turns produced this. 99% ready, 30 line items, and every field says where it came
> from.
>
> Thirteen are buyer-stated, and each one holds the buyer's own words as evidence. Order
> quantity is 45,000, and behind it is the phrase *"1,500 pieces of each."* That link is
> enforced — if the model proposes a fact as buyer-stated and we can't find it verbatim in
> what the buyer typed, we downgrade it to a recommendation automatically.
>
> Three fields are marked *AI recommended* — payment terms, quote validity, inspection. The
> model suggested those. They're visibly not buyer facts, and they don't count toward
> readiness until someone confirms them.

**[WHY]** Provenance is established before a supplier is involved. Same discipline later governs supplier data.

**[PRINCIPLE]** The AI proposes. It never silently promotes its own suggestion into a requirement.

**[TRANSITION]** → Click **2 · Quotes & Comparison**.

---

## 2:20–4:15 · Phase 2 — five formats, one comparison

### 2:20–2:55 — The comparison

**[SHOW]** The four metrics, then the grid.

**[POINT OUT]** `Suppliers 5 of 6 replied` · `Currencies USD · INR · EUR`.

**[SAY]**

> Five suppliers replied. Nobody used a template. A spreadsheet, a PDF, a Word document with
> the commercials in a paragraph, a plain email, and a photograph of a printed quote taken at
> an angle.
>
> The sixth never replied — and that's a column here, not an absence. *No response* is
> different from *didn't quote this line*, and both are different from zero.

**[CLICK]** **Suppliers** tab → **Open** on Istanbul's `stress_e_quote.png`.

**[SHOW]** The angled, blurred photo beside the text the system read from it.

**[SAY]**

> That's the phone photo. This is what came out of it. Vision transcription is genuinely
> fallible, so anything from an image is capped at 75% confidence and flagged. We're not
> pretending the photo is as reliable as the spreadsheet.

### 2:55–3:50 — The Shenzhen line *(the ugly edge)*

**[CLICK]** **Comparison** → *Where did a number come from?* → **LINE-017** → expand **Shenzhen Print & Pack**.

**[SAY]**

> This is the one I'd look at hardest. Shenzhen is four problems in a single quote.
>
> First — this is their **second** PDF. It supersedes one from four days earlier. The
> comparison uses the revision; the original is still on file.
>
> Second — they didn't quote per piece. They quoted **USD 281.30 per 1,000**. The system
> reduced that to **USD 0.2616** per piece and shows the arithmetic.
>
> Third — there's a **7% discount in a footnote**, conditional on total order above 20,000
> pieces. This RFQ is 45,000, so the condition is met, the discount is applied, and the note
> says the undiscounted price would have been 0.2813. If we couldn't evaluate the condition,
> we'd say that instead of guessing.
>
> Fourth, and this is the one that matters — their **minimum order is 3,000** and this line
> needs 1,500. So the price is real, correctly normalised, and **still not usable at this
> quantity**. The system flags it rather than quietly ranking Shenzhen cheapest.
>
> And their own document contradicts itself on lead time — 18 days in one place, 30 days
> during peak season in another. We show both. We don't pick one.

**[WHY]** The brief asked what the system does when it isn't sure. This is the answer, on one supplier, on one line.

**[PRINCIPLE]** Extraction succeeding is not the same as a number being safe to act on.

### 3:50–4:15 — Needs review

**[CLICK]** **Needs review** tab.

**[SAY]**

> Everything the system won't assert on its own lands here, grouped by what kind of doubt it
> is. Gujarat's email says *"item 2, item 4"* — no dimensions, no SKU. The system's best guess
> is positional and it says exactly that, rather than matching silently.
>
> And every certification here except one reads **claimed**, not verified — the supplier said
> so and attached nothing. Istanbul is the only one where the certificate is actually among
> the documents we received.

**[TRANSITION]**

> Hold onto that. It's about to change an answer. But first — the obvious question.

---

## 4:15–5:00 · Is any of this hardcoded?

**[CLICK]** **Tools → Extraction Playground**.

**[SAY]**

> Everything I've shown you was extracted before this call. So the fair question is whether
> any of it is real. This is the bench we built to answer that.

**[TYPE]** into the supplier reply box — make it up on the spot:

```
12x10x6 at 41 cents, 16x16x12 at 0.92. MOQ 2000 per size.
20 days after artwork. FOB Ningbo. ISO 9001 held.
```

**[CLICK]** **Run extraction**. *(Expect 30–60s — talk over it.)*

**[SAY, while it runs]**

> Same pipeline, same guards, on text I invented thirty seconds ago. Nothing here is saved
> unless I choose to keep it.

**[SHOW]** The three panels: what was sent · what was extracted · what needs review.

**[POINT OUT]** The **Trace a line** selector — the span each value came from.

**[SAY]**

> Two line items, the terms, and what it couldn't determine. Note "41 cents" — it read the
> number, and it won't treat cents as a currency it can compare, because "cents" could be
> four different ones. That's the same guard you saw on Gujarat's email.

**[WHY]** Answers "did you type all this in?" at the exact moment the audience is thinking it.

**[PRINCIPLE]** The extraction is real and inspectable on demand, not a fixture.

**[TRANSITION]** → Click **3 · Procurement Analyst**.

---

## 5:00–6:45 · Phase 3 — ask, don't rebuild

### 5:00–5:35 — Cheapest by line

**[CLICK]** The **Cheapest by line** suggestion. *(Instant — no model call.)*

**[SHOW]** The answer, then expand **How this was calculated** and **Left out of this answer**.

**[SAY]**

> *30 of 30 lines have a comparable quote. Anhui Packaging is lowest on 25 of them.*
>
> Two things under that answer matter more than the answer itself.
>
> The assumptions are stated: prices compared in USD; a quote only counts if its basis reduced
> to a single piece and its currency is one we can convert; and a minimum order above the line
> quantity makes a quote unusable — which is why Shenzhen isn't cheapest anywhere.
>
> And **103 exclusions**, each one named with a reason. Nothing is dropped silently.

### 5:35–6:25 — The VP's question

**[SAY]**

> Now the question from the brief. The one that costs the fourth day.

**[CLICK]** **Cheapest among QA-cleared**.

**[SAY]**

> *11 of 30 lines have a comparable quote. Istanbul Ambalaj is lowest on 11 of them.*
>
> That's the whole product in one number. Same calculation, one filter, and the answer
> collapses from thirty lines to eleven — because when you insist on a certificate we actually
> hold rather than one a supplier claims, four of the five suppliers drop out.
>
> That's a genuinely different procurement decision. In a spreadsheet it's a day of work to
> discover.
>
> Nothing about that is stored or pre-computed. Claude turned the question into a structured
> query, our code ran it, and Claude described the result it was handed. It never saw a price
> while planning, and was never asked to work one out.

**[WHY]** The brief's exact example question, answered live, with a materially different result.

**[PRINCIPLE]** The model reasons about the question. The application owns the arithmetic.

### 6:25–6:45 — What should I review?

**[CLICK]** **What should I review?**

**[SAY]**

> *23 things worth reviewing before you decide; 10 of them stop a price being compared.*
> That's the honest state of this RFQ. Not a green tick — a list.

**[TRANSITION]** → Click **Take a decision**.

---

## 6:45–8:00 · Phase 4 — recommendation, then decision

### 6:45–7:20 — The award grid

**[CLICK]** **Start the award**.

**[SHOW]** The per-line grid: Line · Item · Qty · **Best value** · **Cheapest** · Your pick · Unit price · Total.

**[SAY]**

> Every line offers both proposals side by side. *Cheapest* is the lowest price we're willing
> to compare. *Best value* is the cheapest one whose supplier also meets a quality bar. The
> buyer picks, per line.
>
> Deliberately not a weighted score. A supplier scoring 87.3 tells you nothing about why they
> won, and this data can't support that number honestly. Two named prices and a stated rule
> can be argued with.

**[CLICK]** The **ⓘ** beside Best value on a line where the two differ.

**[SAY]**

> Every supplier who priced this line, their price as quoted in their own currency, whether
> their certificate is on file or merely stated, their validity and lead time — and for
> everyone excluded, the reason. Shenzhen appears with its MOQ. The supplier who never replied
> appears too. The buyer sees the whole field, not just the winner.

### 7:20–8:00 — The buyer overrides

**[CLICK]** Flip one line's chooser from **Best value** to **Cheapest**.

**[POINT OUT]** Unit price, line total and the footer total all move.

**[SAY]**

> Recommendation and decision are different things, and the screen keeps them apart. The
> system proposed; I just decided otherwise; the total changed to match.

**[CLICK]** **Award a line to someone else** → pick a third supplier → **Apply** with no reason *(refused)* → add a reason → apply.

**[SAY]**

> An override to a supplier neither proposal named requires a reason. Not to slow the buyer
> down — because in six months, when someone asks why this line went where it went, "no reason
> given" is not an answer.
>
> And notice nothing is blocking me. An earlier version of this screen refused to let me
> approve because a supplier's certificate was unverified — while recommending that same
> supplier. The product was arguing with itself. Now it reports, and the buyer decides.

**[PRINCIPLE]** The system recommends. The buyer stays accountable.

---

## 8:00–9:20 · Phase 5 — the decision becomes executable

### 8:00–8:25 — Approve

**[CLICK]** Expand **Notes** → **Approve this award**.

**[SAY]**

> Everything validation found is here — unverified certificates, an expiring quote, missing
> payment terms, mixed currencies. None of it stops me, and all of it is recorded with the
> approval, so what I was looking at when I committed is on the record. The only thing that
> can actually stop an approval is an award with nothing on it.

### 8:25–9:05 — Supplier messages

**[CLICK]** **Prepare supplier messages** → open one letter.

**[SAY]**

> One letter per supplier. Claude writes the prose. It's handed exactly one supplier's facts —
> there's no field in that structure where a rival's price could sit — and the schema it fills
> has **no numeric field at all**. The line table underneath is rendered by the application.
>
> Then every draft is checked against that supplier's own data before I see it. A draft that
> invents a figure, names another supplier, composes a commercial term, or claims to have been
> sent is discarded whole and replaced with a deterministic letter.

**[CLICK]** Paste a rival's name and price into the body → **Save edit**.

**[SHOW]** The refusal, and the disabled Send.

**[SAY]**

> That applies to me too, not just the model. I can't send that.

**[CLICK]** Undo → **Send to supplier**.

**[POINT OUT]** The label: *simulated, demo only*.

**[SAY]**

> Nothing left this machine. No mail connection exists in this prototype, and the screen says
> so everywhere it matters.

### 9:05–9:20 — Handoff

**[CLICK]** **Generate order handoff** → open one → **Download CSV** → then **History**.

**[SAY]**

> A structured order per supplier, generated with no model involved at all. A term the supplier
> never stated reads *Not provided* — never a default. And every state change is in the history,
> oldest first, with what it replaced.
>
> We didn't want this to end at "Anhui wins." A decision you can't execute is still just a
> spreadsheet with better formatting.

---

## 9:20–10:00 · Trust and close

**[SAY]**

> The question behind all of this is whether a buyer with four crore on the line would act on
> what's on this screen.
>
> We didn't try to make the AI look certain. We tried to make uncertainty visible and cheap to
> inspect.
>
> Every price traces back to the sentence it came from. A missing quote is never a zero. A
> claimed certificate is never shown as verified. A price whose currency the supplier never
> named is held out rather than guessed at — there is no fallback exchange rate anywhere in
> this system. Supplier documents are treated as untrusted input; text in them that looks like
> an instruction to the AI is dropped, and the buyer is told.
>
> And the model never calculates. It reads documents, plans a query, and describes a result.
> Everything in between is Python over SQLite, covered by 565 tests that run with no network
> and no model calls.
>
> So: the buyer starts with a requirement. The system turns it into an RFQ. Suppliers reply
> however they like. We turn that into one comparable dataset. The buyer interrogates it
> instead of rebuilding it. The decision is theirs, it's defensible, and it becomes executable.
>
> The plumbing is stubbed where it doesn't prove the thesis. The extraction, normalisation,
> analysis, validation and decision workflow are real.
>
> That's what we mean by killing the quote spreadsheet.

---

## Backup questions

First four are instant, no model call.

1. **Supplier coverage** — Anhui 30/30, Shenzhen 25/30, Istanbul 11/30. Partial responses as a first-class fact.
2. **Lead times** — shows Shenzhen's self-contradiction rather than averaging it.
3. **Quality status** — the claimed-vs-verified split on one screen.
4. **RFQ completeness** — coverage with its numerator, denominator and the definition of "valid".
5. *"Why didn't we choose Shenzhen for line 17?"* — typed, 60–90s, two model calls. Returns the MOQ reason with the sentence from their PDF and its page number.

## If it goes wrong

| Risk | Recovery |
|---|---|
| **A typed analyst question takes 60–120s** | Use the seven one-click suggestions — pre-built queries, instant, no model call. Type one only if you have slack. |
| **Playground run stalls** | It's the only deliberately live extraction in the demo. Skip it if you're behind; the Suppliers tab already shows real source documents. |
| **Claude CLI not signed in** | Sidebar shows this before you start. `claude auth login`. Already-extracted data still renders. |
| **A model call fails mid-demo** | Every failure path shows plain English and keeps the data. Letters always have a deterministic fallback. |
| **Award already exists** | Page shows the existing award instead of *Start the award*. Cancel it, or demo from where it is. |
| **Data drifted** (expiry warning stale) | `python3 scripts/seed_demo.py --extract`. |
| **Browser refresh loses the open RFQ** | Session state resets by design. Re-open via the *Start here* card. Nothing is lost. |
| **"Prove nothing is hardcoded"** | The Playground, or **Show diagnostics** in the sidebar — the AI call log with prompts, timings and schema-validation results. |
