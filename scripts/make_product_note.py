"""Render the product & design note as a two-page PDF.

Kept as a script rather than a one-off so the note can be regenerated when the product
changes — the figures in it are claims about a running system, and a stale PDF making
claims about software is worse than no PDF.

Usage:  python3 scripts/make_product_note.py [output.pdf]
"""
from __future__ import annotations

import sys

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
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

_base = getSampleStyleSheet()

TITLE = ParagraphStyle("title", parent=_base["Title"], fontName="Helvetica-Bold",
                       fontSize=21, leading=24, textColor=INK, alignment=TA_LEFT,
                       spaceAfter=1)
KICKER = ParagraphStyle("kicker", parent=_base["Normal"], fontName="Helvetica",
                        fontSize=9.2, leading=12, textColor=MUTED, spaceAfter=7)
H = ParagraphStyle("h", parent=_base["Normal"], fontName="Helvetica-Bold", fontSize=9.8,
                   leading=11.6, textColor=BRAND, spaceBefore=9, spaceAfter=3.5,
                   tracking=0)
BODY = ParagraphStyle("body", parent=_base["Normal"], fontName="Helvetica", fontSize=9.2,
                      leading=12.6, textColor=INK, spaceAfter=4)
SMALL = ParagraphStyle("small", parent=BODY, fontSize=8.6, leading=11.4, textColor=MUTED)
BULLET = ParagraphStyle("bullet", parent=BODY, fontSize=9.0, leading=12.1, spaceAfter=3.0)
PRINCIPLE = ParagraphStyle("principle", parent=BODY, fontName="Helvetica-Bold",
                           fontSize=10.0, leading=13.4, textColor=INK)


def bullets(items):
    return ListFlowable(
        [ListItem(Paragraph(t, BULLET), leftIndent=10, value="circle") for t in items],
        bulletType="bullet", start="circle", leftIndent=11, bulletFontSize=4.5,
        bulletOffsetY=-1.5, spaceAfter=3)


def decision_table(rows):
    """Constraint -> decision -> consequence, the three-column shape the note argues in."""
    data = [[Paragraph("<b>%s</b>" % c, SMALL), Paragraph(d, SMALL), Paragraph(w, SMALL)]
            for c, d, w in rows]
    t = Table(data, colWidths=[42 * mm, 62 * mm, 66 * mm], hAlign="LEFT")
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
        ("LEFTPADDING", (0, 0), (0, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, LINE),
    ]))
    return t


def flow_strip(steps):
    """The end-to-end flow as one band, because the flow is the product."""
    cells = []
    for i, s in enumerate(steps):
        cells.append(Paragraph("<b>%s</b>" % s, ParagraphStyle(
            "step", parent=SMALL, fontSize=7.9, leading=9.8, textColor=INK)))
        if i < len(steps) - 1:
            cells.append(Paragraph("→", ParagraphStyle("arrow", parent=SMALL,
                                                       fontSize=8, textColor=BRAND)))
    widths = []
    for i in range(len(cells)):
        widths.append(5 * mm if i % 2 else (170 - 5 * (len(steps) - 1)) / len(steps) * mm)
    t = Table([cells], colWidths=widths, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BACKGROUND", (0, 0), (-1, -1), WASH),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t


def build(path: str) -> None:
    doc = SimpleDocTemplate(
        path, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm,
        topMargin=15 * mm, bottomMargin=14 * mm,
        title="Kill the Quote Spreadsheet — Product & Design Note",
        author="Ayon Chatterjee")
    s = []

    s.append(Paragraph("Kill the Quote Spreadsheet", TITLE))
    s.append(Paragraph("Product &amp; design note · AI Procurement Analyst prototype", KICKER))
    s.append(HRFlowable(width="100%", thickness=1, color=BRAND, spaceAfter=8))

    s.append(Paragraph("The goal", H))
    s.append(Paragraph(
        "A buyer sends an RFQ for 30 line items to five suppliers. Nine days later the replies "
        "arrive as a spreadsheet, a PDF, a Word document, a plain email and a photograph. Three "
        "days go into retyping them into Excel. A fourth goes into one question from the VP. We "
        "set out to delete that week.", BODY))

    s.append(Paragraph("The product bet", H))
    s.append(Paragraph(
        "The hard part is not collecting quotes, and it is not reading documents. It is that "
        "<b>every number arrives with different conditions attached</b> — a minimum order, a price "
        "basis, a validity window, a currency, a certificate that may or may not exist. The "
        "spreadsheet is slow because a human is manually deciding what is comparable with what. "
        "<b>That judgement is the product.</b>", BODY))

    s.append(Paragraph("What we built", H))
    s.append(flow_strip(["Requirement", "RFQ", "Replies in any format",
                         "One comparison", "Ask questions", "Award", "Execute"]))
    s.append(Spacer(1, 5))
    s.append(Paragraph(
        "Every field carries its provenance. Five document readers feed one AI extraction pass per "
        "reply, then deterministic line matching and price normalisation. The buyer interrogates "
        "the result in plain English, decides per line, and the decision becomes supplier letters "
        "and a structured order handoff with a full audit trail.", BODY))

    s.append(Paragraph("Constraints that shaped it", H))
    s.append(decision_table([
        ("The model must not become the database",
         "Claude turns a question into a structured query and describes the result. Retrieval, "
         "filtering, ranking and totals are Python over SQLite.",
         "The analyst can misread what you meant and still cannot be wrong about the number."),
        ("Suppliers reply however they like",
         "No template is ever sent. The system absorbs five formats, including a phone photo.",
         "Five companies don't have to change how they work."),
        ("Uncertainty is shown, not resolved",
         "A price we cannot reduce to a comparable figure is held out rather than estimated. "
         "No fallback exchange rate exists; “$” is deliberately unmapped.",
         "The buyer sometimes sees a gap. That is the honest answer."),
        ("Judgement beats scoring",
         "We were asked for a weighted best-value score and deliberately didn't build one.",
         "A score of 87.3 hides why one supplier won. A named price and a stated rule can be "
         "argued with."),
        ("No API key",
         "The app shells out to the Claude Code CLI on the user's own subscription.",
         "Runs anywhere with one sign-in, but CLI latency pushed the seven most useful analyst "
         "questions to be pre-built and instant."),
    ]))

    s.append(Paragraph("Trust by design", H))
    s.append(bullets([
        "<b>Evidence, or it didn't happen.</b> A value must trace to a verbatim span in the "
        "source or it is downgraded and flagged. The original document is one click from every price.",
        "<b>Absence is never zero.</b> <i>Not quoted</i>, <i>no response</i>, <i>unresolved</i> and "
        "a price are four different states.",
        # "≠" is not in the built-in Helvetica and rendered as "=", which inverted the
        # sentence. Spelling it out is clearer than a glyph anyway.
        "<b>A claimed certificate is not a verified one.</b> It counts as verified only when "
        "the certificate itself is among "
        "the documents received — one of six suppliers in the demo data.",
        "<b>Supplier text is untrusted input.</b> Text shaped like an instruction to the AI is "
        "dropped before it reaches a prompt, and the buyer is told.",
        "<b>One supplier per letter, structurally.</b> The facts handed to the model hold one "
        "supplier and no numeric field; every draft — and every buyer edit — is re-checked "
        "against that supplier's own data before it can be sent.",
        "<b>The buyer decides.</b> Overrides require a reason. The audit trail records every "
        "state change with what it replaced.",
    ]))

    s.append(PageBreak())

    s.append(Paragraph("Evaluation", H))
    s.append(Paragraph(
        "<b>565 automated tests</b>, no network and no model calls, about 30 seconds. They cover the "
        "CLI boundary and its failure modes, the Phase 1 trust guards, document extraction, "
        "normalisation and line matching, the comparison dataset, analyst calculations and guards, "
        "award seeding, validation and execution — plus one end-to-end test that runs a single RFQ "
        "through every phase.", BODY))
    s.append(Paragraph(
        "<b>Live smoke scripts</b> run the real model against the real fixtures for each phase. The "
        "award script fingerprints the upstream tables before and after to prove an award changes "
        "nothing it should not, and includes a deliberate cross-supplier leak attempt.", BODY))
    s.append(Paragraph(
        "<b>A built-in extraction playground</b> is the answer to “is any of this hardcoded?”. Type "
        "or paste any supplier reply, attach any file, and watch the same pipeline read it live — "
        "line items, terms, what it could not determine, and the span each value came from. Nothing "
        "is saved unless you keep it.", BODY))
    s.append(Paragraph(
        "<b>Deliberately ugly demo data:</b> a partial response covering 25 of 30 lines; a revision "
        "superseding an earlier PDF; a per-1,000 price basis; a footnote discount with a condition; "
        "a minimum order above the line quantity; a self-contradicting lead time; three currencies "
        "plus one unnamed and held out; positional line references; an angled photograph; a supplier "
        "who never replied; and a prompt-injection line inside a supplier email.", BODY))

    s.append(Paragraph("What we refused to build", H))
    s.append(bullets([
        "<b>The email loop — outreach, replies, and pulling attachments out of an inbox.</b> This "
        "was the biggest deliberate omission. Wiring a mailbox is a solved, unremarkable problem, "
        "and it would have consumed the time that went into the part that is actually hard: "
        "deciding what the system should refuse to say. Sending is simulated and labelled as such "
        "on every screen — no message leaves the machine. <b>Note that reading attachments is fully "
        "real;</b> only the transport is stubbed. <i>Next: an inbound mailbox watcher, feeding the "
        "pipeline that already exists.</i>",
        "<b>ERP and PO submission, payment, invoice matching, goods receipt.</b> The handoff is a "
        "downloadable document of record, already structured for it.",
        "<b>Supplier portal, logins, permissions.</b> Forcing suppliers into our UI is the exact "
        "behaviour we set out to avoid.",
        "<b>Splitting one line across suppliers.</b> Unrepresentable by design — a unique "
        "constraint in the schema holds that line.",
        "<b>OCR for scanned PDFs.</b> A PDF with no text layer is reported unsupported rather "
        "than guessed at.",
    ]))

    s.append(Paragraph("What we learned", H))
    s.append(Paragraph(
        "Extraction was not the hard part; it worked early. Almost all the difficulty was in "
        "deciding <b>what the system should refuse to say</b> — and the sharpest lesson came from "
        "our own screen. An early version of the award page recommended a supplier and then blocked "
        "the award because that supplier's certificate was unverified. The product was arguing with "
        "itself. The fix was not technical: by the time a buyer reaches the award screen they have "
        "already done the reviewing, so the system should report rather than gate.", BODY))
    s.append(Paragraph(
        "<b>The more interesting problem is one layer down.</b> Everyone frames this as document "
        "extraction. It isn't. Procurement comparability is a <i>judgement</i> — minimum order, "
        "price basis, validity, certification — and today that judgement lives only in a buyer's "
        "head, which is why it cannot be audited, delegated or checked six months later. Making it "
        "explicit and arguable is worth more than reading another PDF format. The most valuable "
        "moment in our demo is not the comparison table; it is watching one question — <i>cheapest "
        "per line, but only among suppliers who cleared quality</i> — reduce a 30-line answer to 11, "
        "because only one supplier's certificate is actually on file.", BODY))

    s.append(Paragraph("What we would build next", H))
    s.append(Paragraph(
        "Inbound email ingestion. Buyer-tunable comparability rules — today the quality bar is one "
        "toggle, and it wants to be a small, named, reusable policy. And a diff view across supplier "
        "revisions, since the revision chain is already stored and currently unexploited.", BODY))

    s.append(Spacer(1, 7))
    s.append(HRFlowable(width="100%", thickness=1, color=BRAND, spaceAfter=7))
    s.append(KeepTogether(Paragraph(
        "AI should remove the spreadsheet work, not the buyer from the decision — and where the "
        "data will not support an answer, the honest move is to show the gap.", PRINCIPLE)))

    doc.build(s)


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "Kill_the_Quote_Spreadsheet.pdf"
    build(out)
    print("wrote %s" % out)
