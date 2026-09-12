"""A bench for testing supplier-response extraction on arbitrary input.

Someone picks an RFQ line item, writes whatever a supplier might have written, and this
runs the real Phase 2 extractor over it and lines the output up against the input so any
deviation is visible.

It contains no extraction logic of its own. It arranges the inputs, calls
``SupplierExtractor.extract()``, and then reads the trust signals Phase 2 already records
— verified evidence, match status, normalisation status, claim status — rather than
forming a second opinion about them.

A run persists nothing. ``SupplierExtractor.extract()`` is pure, so a test is throwaway by
default; ``promote()`` is the only path that writes anything to the database.
"""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .ai_service import AIError, AIService
from .config import Settings
from .document_extractor import DocumentExtractorRegistry
from .schema import RFQ, AICallRecord, LineItem, utc_now
from .supplier_ai_extractor import SupplierExtractor
from .supplier_models import (
    ClaimStatus, Evidence, ExtractionStatus, MatchStatus, NormalizationStatus, QuoteStatus,
    ResponseBundle, Supplier, SupplierQuote, SupplierResponse, SupplierStatus,
)


class TestbenchError(Exception):
    """Something the user can act on, phrased for them."""


# --------------------------------------------------------------------------- #
@dataclass
class FieldRow:
    """One extracted value, next to the words it came from."""
    group: str                  # Price / Commercial terms / Quality / Line matching
    field: str
    extracted: str
    span: str = ""              # the supplier's own words
    location: str = ""          # where in the input
    traced: bool = False        # the span was found in what the user supplied
    deviation: bool = False
    note: str = ""

    @property
    def marker(self) -> str:
        if self.deviation:
            return "deviation"
        return "traced" if self.traced else "untraced"


@dataclass
class TestRun:
    """Everything one bench run produced. Held in memory; nothing is saved."""
    rfq_id: str
    rfq_title: str
    line_item_id: Optional[str]
    line_item_label: str
    supplier_name: str
    subject: str
    body: str
    attachments: List[str] = field(default_factory=list)      # filenames
    unreadable: List[Tuple[str, str]] = field(default_factory=list)   # (filename, why)
    bundle: Optional[ResponseBundle] = None
    rows: List[FieldRow] = field(default_factory=list)
    deviations: List[str] = field(default_factory=list)
    unclaimed_figures: List[str] = field(default_factory=list)
    ai_calls: List[AICallRecord] = field(default_factory=list)
    duration_ms: int = 0
    promoted: bool = False
    documents: List[Dict[str, Any]] = field(default_factory=list)   # {filename, media_type, text, path, bytes}
    table: "ExtractionTable" = field(default_factory=lambda: ExtractionTable())
    issues: List["Issue"] = field(default_factory=list)

    # -- counters for the summary strip ------------------------------------
    @property
    def extracted_count(self) -> int:
        return len(self.rows)

    @property
    def traced_count(self) -> int:
        return sum(1 for r in self.rows if r.traced)

    @property
    def deviation_count(self) -> int:
        return sum(1 for r in self.rows if r.deviation) + len(self.unclaimed_figures)

    @property
    def clean(self) -> bool:
        return self.deviation_count == 0 and self.extracted_count > 0

    def rows_for_selected_line(self) -> List[FieldRow]:
        return [r for r in self.rows if r.group != "Other lines"]

    def combined_input(self) -> str:
        parts = []
        if self.subject:
            parts.append("Subject: %s" % self.subject)
        if self.body:
            parts.append(self.body)
        for d in self.documents:
            parts.append("### %s\n%s" % (d["filename"], d["text"]))
        return "\n\n".join(parts)


# --------------------------------------------------------------------------- #
# Presentation model: the same extraction, shaped as a quotation table.
#
# Nothing here re-reads a document or re-judges a value. Every cell is a field Phase 2
# already extracted, carrying the span Phase 2 already recorded.
# --------------------------------------------------------------------------- #
@dataclass
class Cell:
    """One value in the quotation table, with where it came from."""
    value: str = ""
    span: str = ""
    location: str = ""
    traced: bool = False
    deviation: bool = False
    note: str = ""
    derived: bool = False        # calculated by us, not stated by the supplier
    from_rfq: bool = False       # carried over from the buyer's own line, not the supplier

    @property
    def missing(self) -> bool:
        return not str(self.value).strip()

    def display(self, placeholder: str = "—") -> str:
        return placeholder if self.missing else str(self.value)


@dataclass
class Column:
    key: str
    label: str
    core: bool = True            # core columns show even when every row is empty
    numeric: bool = False


@dataclass
class QuoteRow:
    index: int
    quote: Optional[SupplierQuote]
    label: str                   # what the supplier called it
    rfq_line_id: Optional[str]
    cells: Dict[str, Cell] = field(default_factory=dict)

    def cell(self, key: str) -> Cell:
        return self.cells.get(key, Cell())


@dataclass
class Issue:
    """One thing worth the buyer's attention, derived from existing Phase 2 records."""
    kind: str
    title: str
    subject: str = ""            # which line item it concerns
    detail: str = ""
    row_index: Optional[int] = None


#: Questionnaire keys the quotation table already has a column for. An answer restating
#: one of these would put the same figure on screen twice under two different headings -
#: a "Quantity" column next to "Qty" - so it is skipped rather than added as an extra.
COVERED_FIELD_KEYS = {
    "product", "description", "specification", "specifications",
    "quantity", "order_quantity", "unit", "uom",
    "price", "unit_price", "target_price", "line_total", "total_price",
    "moq", "minimum_order_quantity",
    "lead_time", "lead_time_days", "delivery_time",
    "delivery_terms", "incoterms", "delivery_location", "required_delivery_date",
    "payment_terms", "quote_validity", "validity", "validity_period",
}

#: Columns backed by fields the Phase 2 data model actually carries.
BASE_COLUMNS = [
    Column("product", "Product"),
    Column("rfq_line", "RFQ line"),
    Column("specification", "Specification"),
    Column("quantity", "Qty", numeric=True),
    Column("unit", "Unit"),
    Column("unit_price", "Unit price (as quoted)"),
    Column("per_piece", "Per piece", numeric=True),
    Column("line_total", "Line total", numeric=True),
    Column("moq", "MOQ"),
    Column("lead_time", "Lead time"),
    Column("delivery", "Delivery"),
    Column("payment", "Payment terms"),
    Column("validity", "Quote validity"),
]


@dataclass
class ExtractionTable:
    columns: List[Column] = field(default_factory=list)
    rows: List[QuoteRow] = field(default_factory=list)

    def column(self, key: str) -> Optional[Column]:
        return next((c for c in self.columns if c.key == key), None)

    def counts(self) -> Dict[str, int]:
        filled = sum(1 for r in self.rows for c in self.columns if not r.cell(c.key).missing)
        missing = sum(1 for r in self.rows for c in self.columns if r.cell(c.key).missing)
        return {"line_items": len(self.rows), "fields": filled, "missing": missing}


# --------------------------------------------------------------------------- #
class SupplierTestbench:
    def __init__(self, ai: AIService, settings: Optional[Settings] = None,
                 extractor: Optional[SupplierExtractor] = None,
                 registry: Optional[DocumentExtractorRegistry] = None):
        self.settings = settings or Settings.from_env()
        self.ai = ai
        self.registry = registry or DocumentExtractorRegistry(self.settings)
        self.extractor = extractor or SupplierExtractor(ai, self.settings, self.registry)

    # ------------------------------------------------------------------ run
    def run_test(self, rfq: RFQ, line_item_id: Optional[str], supplier_name: str,
                 subject: str, body: str, attachment_paths: Optional[List[str]] = None,
                 on_stage: Optional[Callable[[str], None]] = None) -> TestRun:
        body = (body or "").strip()
        subject = (subject or "").strip()
        attachment_paths = list(attachment_paths or [])
        if not body and not attachment_paths:
            raise TestbenchError("Write a supplier reply or attach a file before running the test.")
        if not rfq.line_items:
            raise TestbenchError("This RFQ has no line items, so there is nothing for a supplier to quote.")

        line = next((li for li in rfq.line_items if li.id == line_item_id), None)
        run = TestRun(
            rfq_id=rfq.id, rfq_title=rfq.title or rfq.product,
            line_item_id=line.id if line else None,
            line_item_label=_line_label(line) if line else "any line",
            supplier_name=(supplier_name or "Test Supplier").strip(),
            subject=subject, body=body,
            attachments=[os.path.basename(p) for p in attachment_paths])

        # The email body becomes a document, exactly as a real supplier email does.
        folder = tempfile.mkdtemp(prefix="testbench_")
        paths: List[str] = []
        if body or subject:
            email_path = os.path.join(folder, "supplier_email.txt")
            with open(email_path, "w", encoding="utf-8") as f:
                f.write(("Subject: %s\n\n" % subject if subject else "") + body)
            paths.append(email_path)
        paths.extend(attachment_paths)

        supplier = Supplier(name=run.supplier_name, status=SupplierStatus.RESPONDED,
                            note="Playground test run; not a real supplier.")
        response = SupplierResponse(rfq_id=rfq.id, supplier_id=supplier.id, subject=subject,
                                    received_at=utc_now())

        started = utc_now()
        try:
            outcome = self.extractor.extract(rfq, supplier, paths, response=response, on_stage=on_stage)
        except AIError as e:
            raise TestbenchError(e.user_message)
        run.bundle = outcome.bundle
        run.ai_calls = outcome.ai_calls
        run.duration_ms = sum(c.duration_ms for c in outcome.ai_calls)

        for d in outcome.bundle.documents:
            if d.raw_text.strip():
                run.documents.append({"filename": d.filename, "media_type": d.media_type,
                                      "text": d.raw_text, "path": d.path,
                                      "bytes": d.byte_size, "method": d.extraction_method})
            else:
                run.unreadable.append((d.filename, d.extraction_note or d.extraction_status.value))

        if not run.documents:
            why = "; ".join("%s: %s" % (n, w) for n, w in run.unreadable) or "nothing could be read"
            raise TestbenchError("Nothing in the supplier reply could be read (%s)." % why)

        # The extractor absorbs its own failures and records them on the response, so an
        # empty result would otherwise read as "the supplier said nothing".
        if not outcome.ok or outcome.bundle.response.extraction_status in (
                ExtractionStatus.FAILED, ExtractionStatus.UNSUPPORTED):
            raise TestbenchError(outcome.bundle.response.extraction_note
                                 or "The supplier reply could not be processed.")

        self._build_rows(run, rfq)
        return run

    # -------------------------------------------------------------- compare
    def _build_rows(self, run: TestRun, rfq: RFQ) -> None:
        bundle = run.bundle
        ev = bundle.evidence

        def trace(quote_or_ids) -> Tuple[str, str, bool]:
            ids = quote_or_ids if isinstance(quote_or_ids, list) else list(quote_or_ids or [])
            for eid in ids:
                e = ev.get(eid)
                if e and e.quoted_text:
                    return e.quoted_text, (e.location or ""), bool(e.verified)
            return "", "", False

        selected = run.line_item_id
        for q in bundle.quotes:
            is_selected = (selected is None) or (q.line_item_id == selected)
            group = "Price" if is_selected else "Other lines"
            span, loc, traced = trace(q.evidence_ids)

            if q.has_price:
                note, deviation = _quote_note(q)
                run.rows.append(FieldRow(
                    group=group,
                    field="Unit price — %s" % (q.supplier_line_label or q.line_item_id or "line"),
                    extracted=q.original_price_text() or "—",
                    span=span, location=loc, traced=traced,
                    deviation=deviation or not traced,
                    note=note or ("" if traced else "No span in the input supports this price.")))

                if q.normalized_unit_price is not None:
                    run.rows.append(FieldRow(
                        group=group, field="Normalized per piece",
                        extracted=q.normalized_price_text(), span=span, location=loc, traced=traced,
                        note=q.normalization_note))
                elif q.normalization_status == NormalizationStatus.UNRESOLVED:
                    run.rows.append(FieldRow(
                        group=group, field="Normalized per piece", extracted="not comparable",
                        span=span, location=loc, traced=traced, deviation=True,
                        note=q.normalization_note))

            # where the quote was attached, and how confidently
            match_note, match_dev = _match_note(q, rfq)
            run.rows.append(FieldRow(
                group="Line matching",
                field="%s →" % (q.supplier_line_label or "supplier line"),
                extracted=q.line_item_id or "not matched",
                span=span, location=loc, traced=traced,
                deviation=match_dev, note=match_note))

            if q.discount.is_present:
                d_span, d_loc, d_traced = trace(q.discount.evidence_ids)
                run.rows.append(FieldRow(
                    group=group, field="Discount", extracted=q.discount.describe(),
                    span=d_span, location=d_loc, traced=d_traced, deviation=not d_traced,
                    note=q.discount.applies_reason))

            if is_selected:
                # Phase 2 keeps evidence at the quote level, so reusing the price span for
                # every term would claim the MOQ came from a sentence about price. Instead
                # the bench finds where each value actually appears in the input.
                for label, value, extra in (
                        ("Minimum order quantity",
                         "{:,.0f} {}".format(q.minimum_order_quantity, q.moq_unit or "pcs")
                         if q.minimum_order_quantity else "", ""),
                        ("Lead time", q.lead_time_text,
                         "Read as %g days — an interpretation of a range." % q.lead_time_days
                         if q.lead_time_is_interpreted and q.lead_time_days else ""),
                        ("Quote validity", q.quote_validity_text,
                         "A condition, not a fixed period." if q.quote_validity_is_conditional else ""),
                        ("Payment terms", q.payment_terms, ""),
                        ("Delivery terms", q.delivery_terms, ""),
                        ("Currency", q.currency, "")):
                    if not value:
                        continue
                    if any(r.field == label for r in run.rows):
                        continue
                    # commercial terms are response-level, so the duplicate guard above
                    # keeps them to one row however many quote lines carry them
                    own_span, own_loc = _locate(str(value), run)
                    run.rows.append(FieldRow(
                        group="Commercial terms", field=label, extracted=str(value),
                        span=own_span, location=own_loc, traced=bool(own_span),
                        deviation=not own_span,
                        note=extra or ("" if own_span else
                                       "This value does not appear verbatim in the input.")))

        for c in bundle.certifications:
            span, loc, traced = trace(c.evidence_ids)
            run.rows.append(FieldRow(
                group="Quality", field="Certification — %s" % c.name,
                extracted=c.status.value.replace("_", " "),
                span=span, location=loc, traced=traced,
                deviation=(c.status == ClaimStatus.CLAIMED),
                note=c.note))

        for a in bundle.questionnaire:
            if not a.answer:
                continue
            span, loc, traced = trace(a.evidence_ids)
            run.rows.append(FieldRow(group="Quality", field="Answer — %s" % (a.field_key or a.question_id),
                                     extracted=a.answer[:80], span=span, location=loc, traced=traced,
                                     note=a.note))

        for sq in bundle.questions:
            span, loc, traced = trace(sq.evidence_ids)
            run.rows.append(FieldRow(group="Quality", field="Supplier asked",
                                     extracted=sq.question[:90], span=span, location=loc,
                                     traced=traced, note="Left unresolved until the buyer answers."))

        seen_conflicts = set()
        for q in bundle.quotes:
            for c in q.conflicts:
                key = (c.get("topic") or "").lower()
                if key in seen_conflicts:
                    continue
                seen_conflicts.add(key)
                run.rows.append(FieldRow(
                    group="Quality", field="Contradiction — %s" % c.get("topic", ""),
                    extracted=" vs ".join(str(v.get("value"))[:40] for v in c.get("values", [])),
                    traced=True, deviation=True,
                    note=c.get("description", "") or "Both statements kept; neither was chosen."))

        run.deviations = [("%s: %s" % (r.field, r.note or "needs a look")) for r in run.rows if r.deviation]
        run.unclaimed_figures = _unclaimed_figures(run)
        run.table = self._build_table(run, rfq)
        run.issues = self._build_issues(run, rfq)

    # ------------------------------------------------------- quotation table
    def _build_table(self, run: TestRun, rfq: RFQ) -> "ExtractionTable":
        """Shape the extracted quotes as a quotation table. No value is recomputed."""
        bundle = run.bundle
        ev = bundle.evidence
        lines_by_id = {li.id: li for li in rfq.line_items}
        table = ExtractionTable(columns=list(BASE_COLUMNS))

        # Any questionnaire item the supplier addressed becomes an extra column, so a
        # field this RFQ happens to ask about (warranty, origin, packaging) shows up
        # without the table needing to know about it in advance.
        answered = [a for a in bundle.questionnaire
                    if a.answer and a.field_key and a.status != ClaimStatus.MISSING]
        used_keys = {c.key for c in table.columns}
        used_labels = {c.label.strip().lower() for c in table.columns}
        extras = []
        for a in answered:
            if a.field_key in COVERED_FIELD_KEYS:
                continue          # the table already carries this under its own heading
            key = "q_" + a.field_key
            label = _humanise(a.field_key)
            if key in used_keys or label.strip().lower() in used_labels:
                continue
            used_keys.add(key)
            used_labels.add(label.strip().lower())
            extras.append((key, a))
            table.columns.append(Column(key, label, core=False))

        for i, q in enumerate(bundle.quotes, start=1):
            line = lines_by_id.get(q.line_item_id or "")
            span, loc, traced = _first_evidence(q.evidence_ids, ev)
            row = QuoteRow(index=i, quote=q, rfq_line_id=q.line_item_id,
                           label=q.supplier_line_label or (line.product if line else "line %d" % i))

            row.cells["product"] = Cell(value=row.label, span=span, location=loc, traced=traced)
            match_note, match_dev = _match_note(q, rfq)
            row.cells["rfq_line"] = Cell(value=q.line_item_id or "", deviation=match_dev,
                                         note=match_note, span=span, location=loc, traced=traced)
            row.cells["specification"] = Cell(
                value=(line.spec_summary() or line.description) if line else "",
                from_rfq=bool(line),
                note="From the RFQ line this quote was matched to." if line else "")

            qty = q.quoted_quantity if q.quoted_quantity else (line.quantity if line else None)
            own_qty = bool(q.quoted_quantity)
            row.cells["quantity"] = Cell(
                value="{:,.0f}".format(qty) if qty else "",
                span=span if own_qty else "", location=loc if own_qty else "",
                traced=traced and own_qty, from_rfq=bool(qty) and not own_qty,
                note="" if own_qty else
                     ("Taken from the RFQ line; the supplier did not restate it." if qty else ""))
            own_unit = bool(q.quoted_unit)
            row.cells["unit"] = Cell(
                value=q.quoted_unit or (line.unit if line else ""),
                span=span if own_unit else "", location=loc if own_unit else "",
                traced=traced and own_unit,
                from_rfq=not own_unit and bool(line and line.unit),
                note="" if q.quoted_unit else
                     ("Taken from the RFQ line; the supplier did not restate it."
                      if (line and line.unit) else ""))

            price_note, price_dev = _quote_note(q)
            row.cells["unit_price"] = Cell(
                value=q.original_price_text(), span=span, location=loc, traced=traced,
                deviation=price_dev or (q.has_price and not traced),
                note=price_note or ("" if traced or not q.has_price else
                                    "No span in the input supports this price."))
            row.cells["per_piece"] = Cell(
                value=q.normalized_price_text(), span=span, location=loc, traced=traced,
                deviation=q.normalization_status == NormalizationStatus.UNRESOLVED and q.has_price,
                note=q.normalization_note)

            total = None
            if q.normalized_unit_price is not None and qty:
                total = q.normalized_unit_price * float(qty)
            row.cells["line_total"] = Cell(
                value=("%s %s" % (q.currency, "{:,.2f}".format(total))) if total is not None else "",
                derived=True,
                note="Calculated as the per-piece price times the quantity. The supplier did not "
                     "state a line total.")

            row.cells["moq"] = _term_cell(
                "{:,.0f} {}".format(q.minimum_order_quantity, q.moq_unit or "pcs")
                if q.minimum_order_quantity else "", run,
                extra="Above this line's quantity." if q.moq_constraint else "")
            row.cells["lead_time"] = _term_cell(
                q.lead_time_text, run,
                extra="Read as %g days — an interpretation of a range." % q.lead_time_days
                if q.lead_time_is_interpreted and q.lead_time_days else "")
            row.cells["delivery"] = _term_cell(q.delivery_terms, run)
            row.cells["payment"] = _term_cell(q.payment_terms, run)
            row.cells["validity"] = _term_cell(
                q.quote_validity_text, run,
                extra="A condition, not a fixed period." if q.quote_validity_is_conditional else "")

            for key, a in extras:
                a_span, a_loc, a_traced = _first_evidence(a.evidence_ids, ev)
                row.cells[key] = Cell(value=a.answer, span=a_span, location=a_loc, traced=a_traced,
                                      note=(a.note or "") + " Stated for the whole response, "
                                                            "not this line specifically.")
            table.rows.append(row)
        return table

    # ------------------------------------------------------------- issues
    def _build_issues(self, run: TestRun, rfq: RFQ) -> List["Issue"]:
        """Everything worth attention, read off records Phase 2 already produced."""
        out: List[Issue] = []
        bundle = run.bundle
        for row in run.table.rows:
            q = row.quote
            subject = row.label
            if q is None:
                continue
            if not q.has_price and q.status != QuoteStatus.NOT_QUOTED:
                out.append(Issue("missing_price", "Missing price", subject,
                                 "The supplier did not give a unit price for this line.", row.index))
            elif q.status == QuoteStatus.NOT_QUOTED:
                out.append(Issue("not_quoted", "Not quoted", subject,
                                 "; ".join(q.issues) or "The supplier did not price this line.",
                                 row.index))
            if q.match_status in (MatchStatus.PROBABLE_MATCH, MatchStatus.UNMATCHED, MatchStatus.CONFLICT):
                out.append(Issue("line_match", "Line match needs confirming", subject,
                                 q.match_reason or "The RFQ line could not be identified safely.",
                                 row.index))
            if q.has_price and q.normalization_status == NormalizationStatus.UNRESOLVED:
                out.append(Issue("unresolved_price", "Price not comparable", subject,
                                 q.normalization_note, row.index))
            if q.status == QuoteStatus.CONFLICT:
                topics = ", ".join(sorted({c.get("topic", "") for c in q.conflicts}))
                out.append(Issue("conflict", "Contradictory values", subject,
                                 "The supplier gave more than one answer for %s." % (topics or "a term"),
                                 row.index))
            if q.moq_constraint:
                out.append(Issue("moq", "Minimum order above the requested quantity", subject,
                                 "; ".join(q.issues) or "", row.index))
            for key in ("lead_time", "validity", "moq", "delivery"):
                cell = row.cell(key)
                if cell.missing:
                    label = next((c.label for c in run.table.columns if c.key == key), key)
                    # Lower the first letter so it reads as prose, but leave an acronym
                    # alone: the column is "MOQ", never "moq".
                    label = label if label.isupper() else label[:1].lower() + label[1:]
                    out.append(Issue("missing_field", "Missing %s" % label, subject,
                                     "The supplier did not state this.", row.index))

        for c in bundle.certifications:
            if c.status == ClaimStatus.CLAIMED:
                out.append(Issue("unverified_claim", "Claim without a certificate", c.name,
                                 c.note or "Stated by the supplier with nothing attached."))
        for sq in bundle.questions:
            if not sq.resolved:
                out.append(Issue("supplier_question", "Supplier is waiting on you", "",
                                 sq.question))
        for fig in run.unclaimed_figures:
            out.append(Issue("unclaimed_figure", "Figure not extracted", fig,
                             "This appears in the supplier's text but in none of the extracted "
                             "fields. It may be irrelevant, or it may have been missed."))
        return _merge_repeats(out)

    # ------------------------------------------------------------- promote
    def promote(self, run: TestRun, supplier_service) -> str:
        """Keep this run: save the supplier and response into the RFQ for real."""
        if run.bundle is None:
            raise TestbenchError("There is no completed run to save.")
        if run.promoted:
            return run.bundle.response.id
        supplier = run.bundle.supplier
        if supplier is None:
            raise TestbenchError("This run has no supplier to save.")
        supplier_service.store.save_supplier(supplier)
        for rec in run.ai_calls:
            supplier_service.store.add_ai_call(rec, response_id=run.bundle.response.id)
        supplier_service.store.save_bundle(run.bundle)
        run.promoted = True
        return run.bundle.response.id


# --------------------------------------------------------------------------- #
def _first_evidence(ids, store) -> Tuple[str, str, bool]:
    for eid in list(ids or []):
        e = store.get(eid)
        if e and e.quoted_text:
            return e.quoted_text, (e.location or ""), bool(e.verified)
    return "", "", False


def _term_cell(value: str, run: TestRun, extra: str = "") -> Cell:
    """A response-level term, citing the sentence it appears in rather than the price line."""
    value = (value or "").strip()
    if not value:
        return Cell(note=extra)
    span, loc = _locate(value, run)
    return Cell(value=value, span=span, location=loc, traced=bool(span),
                deviation=not span,
                note=extra or ("" if span else "This value does not appear verbatim in the input."))



#: Issue kinds that describe the response as a whole rather than one line. Phase 2 records
#: them against every affected quote, so listing them per line would repeat one fact eight
#: times and bury the rest. They are merged into a single entry naming the lines instead.
RESPONSE_WIDE_KINDS = {"conflict"}

#: How many line names a merged issue spells out before it starts counting.
MERGED_SUBJECT_LIMIT = 3


def _merge_repeats(issues: List["Issue"]) -> List["Issue"]:
    """Collapse one response-wide finding repeated across lines into a single entry.

    The affected lines are named rather than dropped, and the first one is kept as the
    jump target, so nothing the buyer could act on is lost.
    """
    merged: Dict[Tuple[str, str], Issue] = {}
    subjects: Dict[Tuple[str, str], List[str]] = {}
    out: List[Issue] = []
    for issue in issues:
        if issue.kind not in RESPONSE_WIDE_KINDS:
            out.append(issue)
            continue
        key = (issue.kind, issue.detail)
        if key not in merged:
            merged[key] = issue
            subjects[key] = []
            out.append(issue)
        if issue.subject and issue.subject not in subjects[key]:
            subjects[key].append(issue.subject)

    for key, issue in merged.items():
        names = subjects[key]
        if len(names) <= 1:
            continue
        shown = names[:MERGED_SUBJECT_LIMIT]
        rest = len(names) - len(shown)
        issue.subject = ", ".join(shown) + (" and %d more" % rest if rest else "")
    return out


def _humanise(key: str) -> str:
    return (key or "").replace("_", " ").strip().capitalize()


def _locate(value: str, run: TestRun) -> Tuple[str, str]:
    """Find the line of input a value appears on, so a term cites its own words.

    Matching ignores case, spacing and punctuation, because a supplier writing
    "MOQ is 8000 pcs" and an extractor reporting "8,000 pcs" mean the same thing.
    """
    needle = _loose(value)
    if not needle or len(needle) < 2:
        return "", ""
    for source, label in _input_sources(run):
        for segment in _segments(source):
            if needle in _loose(segment):
                return segment, label
    # The extractor often rewords a term - "18 days from artwork approval" for "Lead time
    # 18 days after artwork approval" - so an exact match is too strict. But a shared number
    # on its own is not evidence: "30 days from date of issue" and "Payment 30% advance"
    # share a 30 and mean nothing alike, and citing one for the other would put words in the
    # supplier's mouth. Require the wording to overlap too, and cite nothing when it does not.
    numbers, words = _numbers(value), _words(value)
    needed_words = 1 if numbers else 2
    for source, label in _input_sources(run):
        for segment in _segments(source):
            if numbers and not (numbers & _numbers(segment)):
                continue
            if len(words & _words(segment)) >= needed_words:
                return segment, label
    return "", ""


#: Words too common to tie a value to a sentence. Sharing only these is not evidence.
_STOPWORDS = {"from", "with", "this", "that", "will", "been", "have", "your", "our", "and",
              "the", "for", "are", "per", "all", "please", "there", "their", "them", "into",
              "within", "after", "before", "against", "shall", "would", "which", "date"}


def _words(text: str) -> set:
    """Distinctive words in a piece of text, for judging whether two phrases are about
    the same thing."""
    return {w for w in re.findall(r"[a-z]{4,}", (text or "").lower()) if w not in _STOPWORDS}


#: Sentence boundaries, for citing the sentence a value appears in rather than the whole
#: message. Only punctuation *followed by whitespace* ends a sentence, so a price like
#: "0.42" is never cut in half; a spreadsheet row carrying no punctuation at all is a
#: sentence in its own right, which is why lines are split first.
_SENTENCE_BREAK = re.compile(r"(?<=[.;!?])\s+")


def _segments(text: str) -> List[str]:
    out = []
    for line in (text or "").splitlines():
        for raw in _SENTENCE_BREAK.split(line):
            seg = raw.strip()
            if seg:
                out.append(seg[:220])
    return out


def _input_sources(run: TestRun) -> List[Tuple[str, str]]:
    out = []
    if run.subject:
        out.append((run.subject, "subject"))
    if run.body:
        out.append((run.body, "email body"))
    for d in run.documents:
        if d["filename"] != "supplier_email.txt":
            out.append((d["text"], d["filename"]))
    return out


def _loose(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def _line_label(line: LineItem) -> str:
    spec = line.spec_summary() or line.description or ""
    return "%s · %s" % (line.id, spec or line.product)


def _quote_note(q: SupplierQuote) -> Tuple[str, bool]:
    if q.status == QuoteStatus.CONFLICT:
        return "The supplier contradicted themselves on this.", True
    if q.status == QuoteStatus.UNRESOLVED:
        return q.normalization_note or "Quoted on a basis that cannot be compared.", True
    if q.status == QuoteStatus.NEEDS_REVIEW:
        return "; ".join(q.issues)[:160] or "Held for review.", True
    return "", False


def _match_note(q: SupplierQuote, rfq: RFQ) -> Tuple[str, bool]:
    if q.match_status == MatchStatus.MATCHED:
        return q.match_reason or "Matched.", False
    if q.match_status == MatchStatus.PROBABLE_MATCH:
        return (q.match_reason or "Likely, but not certain.") + " Confirm before relying on it.", True
    if q.match_status == MatchStatus.CONFLICT:
        return q.match_reason or "Two supplier lines claim the same RFQ line.", True
    return (q.match_reason or "Nothing in the wording identifies an RFQ line."), True


#: Figures worth noticing: money amounts, quantities, and bare decimals.
_FIGURE = re.compile(
    r"(?:(?:USD|EUR|INR|GBP|CNY|RMB|JPY|VND|TRY|AUD|CAD|\$|€|£|₹)\s*[\d,]+(?:\.\d+)?)"
    r"|(?:[\d,]+(?:\.\d+)?\s*(?:pcs|pieces|units|nos|kg|days?|weeks?|%))"
    r"|(?:\b\d+\.\d{2}\b)", re.I)

#: Words that make a figure part of the requirement rather than the quotation.
_NOISE = re.compile(r"\b(page|ref|no\.|invoice|tel|phone|fax|gst|vat|pin|zip)\b", re.I)


def _unclaimed_figures(run: TestRun) -> List[str]:
    """Figures present in the input that turn up nowhere in the output.

    A hint, not a verdict: it is a prompt to check whether something was missed.

    Numbers are compared as discrete values rather than as one run of digits, because
    concatenating them invents matches - "5,000" next to "18" contains "250".
    """
    text = run.combined_input()
    if not text.strip():
        return []
    claimed = set()
    for r in run.rows:
        claimed |= _numbers(r.extracted)
        claimed |= _numbers(r.span)

    out: List[str] = []
    seen = set()
    for m in _FIGURE.finditer(text):
        token = m.group(0).strip()
        line_start = text.rfind("\n", 0, m.start()) + 1
        line_end = text.find("\n", m.end())
        context = text[line_start:line_end if line_end > 0 else len(text)]
        if _NOISE.search(context):
            continue
        values = _numbers(token)
        if not values or values & claimed:
            continue
        key = tuple(sorted(values))
        if key in seen:
            continue
        seen.add(key)
        out.append(token)
    return out[:8]


_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers(text: str) -> set:
    """Every distinct number in a string, normalised so 5,000 and 5000.0 agree."""
    out = set()
    for raw in _NUMBER.findall(text or ""):
        try:
            out.add("%g" % float(raw.replace(",", "")))
        except ValueError:
            continue
    return out
