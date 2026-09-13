"""Render the product & design note as a two-page PDF.

Kept as a script rather than a one-off so the note can be regenerated when the product
changes — every figure in it is a claim about running software, and a stale PDF making
claims about software is worse than no PDF.

Usage:  python3 scripts/make_product_note.py [output.pdf]
"""
from __future__ import annotations

import sys

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable, KeepTogether, ListFlowable, ListItem, PageBreak, Paragraph,
    SimpleDocTemplate, Spacer, Table, TableStyle,
)

INK = colors.HexColor("#0F172A")
MUTED = colors.HexColor("#475569")
BRAND = colors.HexColor("#2563EB")
LINE = colors.HexColor("#CBD5E1")
WASH = colors.HexColor("#F1F5F9")
AI_BG = colors.HexColor("#EFF6FF")
AI_FG = colors.HexColor("#1D4ED8")
GUARD_BG = colors.HexColor("#FFFBEB")
GUARD_FG = colors.HexColor("#92400E")
CODE_BG = colors.HexColor("#F1F5F9")

_base = getSampleStyleSheet()

TITLE = ParagraphStyle("title", parent=_base["Title"], fontName="Helvetica-Bold",
                       fontSize=20, leading=23, textColor=INK, alignment=TA_LEFT,
                       spaceAfter=1)
KICKER = ParagraphStyle("kicker", parent=_base["Normal"], fontName="Helvetica",
                        fontSize=8.8, leading=11, textColor=MUTED, spaceAfter=6)
H = ParagraphStyle("h", parent=_base["Normal"], fontName="Helvetica-Bold", fontSize=9.3,
                   leading=11.0, textColor=BRAND, spaceBefore=7.5, spaceAfter=3)
BODY = ParagraphStyle("body", parent=_base["Normal"], fontName="Helvetica", fontSize=8.4,
                      leading=11.1, textColor=INK, spaceAfter=3.5)
SMALL = ParagraphStyle("small", parent=BODY, fontSize=7.8, leading=10.0, textColor=MUTED)
BULLET = ParagraphStyle("bullet", parent=BODY, fontSize=8.2, leading=10.7, spaceAfter=2.2)
PRINCIPLE = ParagraphStyle("principle", parent=BODY, fontName="Helvetica-Bold",
                           fontSize=9.6, leading=12.8, textColor=INK)
CAPTION = ParagraphStyle("caption", parent=SMALL, fontSize=7.4, leading=9.2)

STEP_NO = ParagraphStyle("stepno", parent=BODY, fontName="Helvetica-Bold", fontSize=8.2,
                         leading=9.8, textColor=colors.white, alignment=TA_CENTER)
STEP_T = ParagraphStyle("stept", parent=BODY, fontName="Helvetica-Bold", fontSize=7.7,
                        leading=9.2, textColor=INK, spaceAfter=0)
TAG_AI = ParagraphStyle("tagai", parent=BODY, fontName="Helvetica-Bold", fontSize=7.0,
                        leading=8.8, textColor=AI_FG, alignment=TA_CENTER)
TAG_CODE = ParagraphStyle("tagcode", parent=BODY, fontName="Helvetica-Bold", fontSize=7.0,
                          leading=8.8, textColor=MUTED, alignment=TA_CENTER)
GUARD = ParagraphStyle("guard", parent=BODY, fontSize=7.0, leading=8.7, textColor=GUARD_FG)


def bullets(items, style=BULLET):
    return ListFlowable(
        [ListItem(Paragraph(t, style), leftIndent=10, value="circle") for t in items],
        bulletType="bullet", start="circle", leftIndent=11, bulletFontSize=4.5,
        bulletOffsetY=-1.5, spaceAfter=3)


def architecture(rows):
    """The pipeline as a diagram: one row per step, and for each step who does the work
    and what stops it going wrong.

    A drawn flowchart would carry less here. The reader's question is not "what is the
    order of operations" — it is "where is the model allowed to touch this, and what
    catches it when it is wrong" — so the two right-hand columns are the substance, not
    decoration. The numbered spine down the left carries the sequence.
    """
    data = [[Paragraph("<b>#</b>", CAPTION), Paragraph("<b>WHAT HAPPENS</b>", CAPTION),
             Paragraph("<b>WHO</b>", CAPTION),
             Paragraph("<b>WHAT KEEPS IT HONEST</b>", CAPTION)]]
    styles = [
        ("BACKGROUND", (0, 0), (-1, 0), WASH),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        ("ALIGN", (2, 0), (2, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 2.1),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.1),
        ("LEFTPADDING", (0, 0), (-1, -1), 3.5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3.5),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE),
        ("LINEAFTER", (0, 0), (0, -1), 0.4, LINE),
        ("LINEAFTER", (2, 0), (2, -1), 0.4, LINE),
    ]
    for i, (n, title, detail, who, guard) in enumerate(rows, start=1):
        is_ai = "AI" in who
        data.append([
            Paragraph(n, STEP_NO),
            Paragraph("<b>%s</b>  <font size=7.1 color='#475569'>%s</font>" % (title, detail),
                      STEP_T),
            Paragraph(who, TAG_AI if is_ai else TAG_CODE),
            Paragraph(guard, GUARD),
        ])
        styles.append(("BACKGROUND", (0, i), (0, i), BRAND))
        styles.append(("BACKGROUND", (2, i), (2, i), AI_BG if is_ai else CODE_BG))
        styles.append(("BACKGROUND", (3, i), (3, i), GUARD_BG))
    t = Table(data, colWidths=[8 * mm, 77 * mm, 20 * mm, 65 * mm], hAlign="LEFT")
    t.setStyle(TableStyle(styles))
    return t


def decision_table(rows):
    data = [[Paragraph("<b>%s</b>" % c, SMALL), Paragraph(d, SMALL), Paragraph(w, SMALL)]
            for c, d, w in rows]
    t = Table(data, colWidths=[40 * mm, 63 * mm, 67 * mm], hAlign="LEFT")
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
        ("LEFTPADDING", (0, 0), (0, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, LINE),
    ]))
    return t


def build(path: str) -> None:
    doc = SimpleDocTemplate(
        path, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=12 * mm, bottomMargin=10 * mm,
        title="Kill the Quote Spreadsheet - Product and Design Note",
        author="Ayon Chatterjee")
    s = []

    s.append(Paragraph("Kill the Quote Spreadsheet", TITLE))
    s.append(Paragraph("Product &amp; design note · AI Procurement Analyst prototype", KICKER))
    s.append(HRFlowable(width="100%", thickness=1, color=BRAND, spaceAfter=7))

    s.append(Paragraph("Problem", H))
    s.append(Paragraph(
        "A category buyer sends an RFQ for 30 line items to five suppliers. Nine days later the "
        "replies arrive as a spreadsheet, a PDF, a Word document, a plain email and a photograph "
        "of a printed rate card. Three days go into retyping it into Excel. Then one question — "
        "<i>cheapest per line, but only among suppliers who cleared quality</i> — costs a fourth.",
        BODY))
    s.append(Paragraph(
        "The re-keying is the visible cost. The real one is that the comparison ends up in a "
        "spreadsheet nobody can audit, so the reasoning behind a six-figure decision evaporates.",
        BODY))

    s.append(Paragraph("Users", H))
    s.append(bullets([
        "<b>The category buyer</b> — owns the RFQ, does the re-keying today. Needs one comparable "
        "view, and a follow-up answered without rebuilding the sheet.",
        "<b>The approver</b> — asks the question that costs the fourth day. Needs the answer "
        "<i>defensible</i> more than fast: who was considered, who excluded, on what basis.",
        "<b>Whoever opens this in six months</b> — a dispute, an audit, or the same buy again. "
        "Needs to see why a line went where it went, without asking whoever decided.",
    ]))

    s.append(Paragraph("What I'm building", H))
    s.append(Paragraph(
        "One continuous flow, requirement to executed award. A co-pilot that asks only what "
        "materially affects price. Suppliers reply in any format. The system reads every reply, "
        "matches it to the buyer's lines, normalises what is safe to normalise, and lands it in "
        "one comparison with the evidence attached. The buyer interrogates that comparison in "
        "plain English, decides per line, and the decision becomes supplier letters, an order "
        "handoff and an audit trail.", BODY))

    s.append(Paragraph("What I've chosen not to build", H))
    s.append(bullets([
        "<b>The email loop — outreach, replies, and pulling attachments out of an inbox.</b> The "
        "biggest deliberate omission. Wiring a mailbox is a solved problem, and it would have "
        "consumed the time that went into the part that is actually hard: deciding what the "
        "system should refuse to say. Sending is simulated and labelled as such on every screen. "
        "<b>Reading attachments is fully real</b> — only the transport is stubbed.",
        "<b>ERP and PO submission, payment, invoice matching, goods receipt.</b> The handoff is a "
        "document of record, already structured for whatever consumes it next.",
        "<b>Supplier portal, logins, permissions.</b> Forcing suppliers into my UI is the exact "
        "behaviour I set out to avoid.",
        "<b>A weighted best-value score.</b> Asked for, deliberately declined — 87.3 tells you "
        "nothing about why a supplier won. Best value is <i>the cheapest quote from a supplier "
        "that meets an explicit, buyer-set bar</i>: a rule you can argue with.",
        "<b>Splitting one line across suppliers</b>, and <b>OCR for scanned PDFs</b>. The first is "
        "unrepresentable by design; the second is reported unsupported rather than guessed at.",
    ]))

    # No forced break: the architecture table is the one block that must not split, so it
    # is kept whole and allowed to find its own page. Forcing a break here left page one
    # half empty.
    s.append(Paragraph("System architecture — and why the output is grounded", H))
    s.append(Paragraph(
        "Every row is a real stage in the running system. The two right-hand columns are the "
        "argument: the model is confined to interpretation, and everything it produces is "
        "schema-validated and then checked against the source before it reaches a screen.", BODY))
    s.append(Spacer(1, 3))
    s.append(KeepTogether(architecture([
        ("1", "Buyer describes a need",
         "Co-pilot asks only what changes a price.",
         "AI", "A stated fact must appear verbatim in the buyer's own words, or it is downgraded "
               "to a visible suggestion."),
        ("2", "RFQ is assembled",
         "30 lines, questionnaire, terms, readiness.",
         "Code", "Provenance on every field: buyer-stated, AI-recommended, or missing."),
        ("3", "Replies arrive in any format",
         "xlsx · pdf · docx · email · phone photo.",
         "Code", "A scanned PDF with no text layer is reported unsupported, never guessed at."),
        ("4", "Each reply is read",
         "One structured extraction call per response.",
         "AI", "Instruction-shaped supplier text is stripped before it reaches a prompt. Values "
               "from a photo are capped at 75% confidence."),
        ("5", "Lines are matched",
         "Supplier rows mapped to the buyer's lines.",
         "Code + AI", "An uncertain match becomes <b>Needs Review</b>, never a silent guess."),
        ("6", "Prices are normalised",
         "Per-1,000 → per piece; discount, MOQ, FX.",
         "Code", "No currency named, or a basis that cannot be reduced → held out of the "
                 "comparison entirely."),
        ("7", "One comparison",
         "A cell per line per supplier, evidence attached.",
         "Code", "<i>Not quoted</i>, <i>no response</i> and <i>unresolved</i> are three distinct "
                 "states. None of them is zero."),
        ("8", "Buyer asks questions",
         "Plain English over the whole dataset.",
         "AI + Code", "The model turns the question into a query and describes the result. It "
                      "never sees a price while planning, and never calculates."),
        ("9", "Award is proposed",
         "Cheapest and best value per line.",
         "Code", "A claimed certificate never counts as verified. The buyer overrides freely, "
                 "with a recorded reason."),
        ("10", "Decision is executed",
         "Letters, order handoff, append-only audit trail.",
         "AI + Code", "The letter schema has no numeric field. Every draft — and every buyer "
                      "edit — is re-checked against that supplier's own data."),
    ])))
    s.append(Spacer(1, 5))
    s.append(Paragraph(
        "<b>Evaluation.</b> 565 automated tests, no network and no model calls, about 30 seconds "
        "— the CLI boundary and its failure modes, the trust guards, extraction, normalisation, "
        "matching, the comparison dataset, analyst calculations, award validation and execution, "
        "plus one end-to-end test carrying a single RFQ through every phase. Live scripts run the "
        "real model against the real fixtures per phase; the award script fingerprints the "
        "upstream tables before and after to prove an award changes nothing it should not, and "
        "includes a deliberate cross-supplier leak attempt. A built-in extraction playground "
        "answers <i>“is any of this hardcoded?”</i> on demand: paste any supplier reply and watch "
        "the same pipeline read it live.", BODY))

    s.append(Paragraph("Constraints kept", H))
    s.append(decision_table([
        ("The model must not become the database",
         "Claude turns a question into a structured query and describes the result. Retrieval, "
         "filtering, ranking and totals are Python over SQLite.",
         "The analyst can misread what you meant and still cannot be wrong about the number."),
        ("Suppliers reply however they like",
         "No template is ever sent; five document readers feed one extraction pass per reply.",
         "Five companies don't have to change how they work."),
        ("Uncertainty is shown, not resolved",
         "A price that cannot be reduced to a comparable figure is held out rather than estimated. "
         "No fallback exchange rate exists.",
         "The buyer sometimes sees a gap. That is the honest answer."),
        ("No API key",
         "The app shells out to the Claude Code CLI on the user's own subscription.",
         "Runs anywhere after one sign-in, but CLI latency pushed the seven most useful analyst "
         "questions to be pre-built and instant."),
    ]))

    s.append(Paragraph("Guardrails", H))
    s.append(Paragraph(
        "The right-hand column above is the guardrail layer. Three properties of it are worth "
        "stating separately, because they are what make the rest trustworthy:", BODY))
    s.append(bullets([
        "<b>A failed check discards, it never repairs.</b> A supplier letter that invents a figure "
        "is thrown away whole and replaced by a deterministic one — there is no path where the "
        "model gets to patch its own output into something plausible.",
        "<b>Nothing is inferred to fill a gap.</b> No fallback exchange rate exists anywhere; "
        "“$” is deliberately unmapped because it could be four currencies; an unstated term reads "
        "<i>Not provided</i>, never a default.",
        "<b>Everything is logged.</b> Every AI call with its prompt, timing and schema result; "
        "every award state change with what it replaced. The buyer can inspect both in the app.",
    ]))

    s.append(Paragraph("What I've learnt", H))
    s.append(Paragraph(
        "Extraction was not the hard part; it worked early. Almost all the difficulty was in "
        "deciding <b>what the system should refuse to say</b> — and the sharpest lesson came from "
        "my own screen. An early version of the award page recommended a supplier, then blocked "
        "the award because that supplier's certificate was unverified. The product was arguing "
        "with itself. The fix was not technical: a buyer reaching the award screen has already "
        "done the reviewing, so the system should report rather than gate.", BODY))
    s.append(Paragraph(
        "<b>The more interesting problem is one layer down.</b> Everyone frames this as document "
        "extraction. It isn't. Procurement comparability is a <i>judgement</i> — minimum order, "
        "price basis, validity, certification — and today it lives only in a buyer's head, which "
        "is why it cannot be audited, delegated or checked six months later. Making it explicit "
        "and arguable is worth more than reading another PDF format. The most valuable moment in "
        "the demo is not the comparison table — it is one question reducing a 30-line answer to "
        "11, because only one certificate is actually on file.", BODY))

    s.append(Paragraph("What's next", H))
    s.append(Paragraph(
        "Inbound email ingestion, feeding the pipeline that already exists. Buyer-tunable "
        "comparability rules — today the quality bar is one toggle, and it wants to be a small, "
        "named policy a category team agrees once and applies to every RFQ. And a diff view "
        "across supplier revisions, since the revision chain is already stored and unexploited.",
        BODY))

    s.append(Spacer(1, 6))
    s.append(HRFlowable(width="100%", thickness=1, color=BRAND, spaceAfter=6))
    s.append(KeepTogether(Paragraph(
        "AI should remove the spreadsheet work, not the buyer from the decision — and where the "
        "data will not support an answer, the honest move is to show the gap.", PRINCIPLE)))

    doc.build(s)


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "Kill_the_Quote_Spreadsheet.pdf"
    build(out)
    print("wrote %s" % out)
