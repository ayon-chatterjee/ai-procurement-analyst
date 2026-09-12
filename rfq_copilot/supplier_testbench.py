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
    documents: List[Dict[str, Any]] = field(default_factory=list)   # {filename, media_type, text}

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
                                      "text": d.raw_text})
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
    # fall back to the numeric part, for "8,000 pcs" against "MOQ is 8000 pcs per size"
    numbers = _numbers(value)
    if numbers:
        for source, label in _input_sources(run):
            for segment in _segments(source):
                if numbers & _numbers(segment):
                    return segment, label
    return "", ""


#: Sentence-ish boundaries. A supplier email is often one long paragraph, so citing a
#: whole line would quote the entire message and show nothing useful.
_SEGMENT = re.compile(r"[^.;\n!?]+(?:[.;!?]|$)")


def _segments(text: str) -> List[str]:
    out = []
    for raw in _SEGMENT.findall(text or ""):
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
