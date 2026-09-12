"""Orchestration for Phase 2: receive supplier responses, extract them, compare them.

The UI talks only to this class. It owns persistence, the per-supplier extraction loop
(one failure never takes the others down), revision handling, manual corrections, and
the normalised comparison dataset that Phase 3 will query.
"""
from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import supplier_guards as sg
from .ai_service import AIError, AIService
from .config import Settings
from .document_extractor import DocumentExtractorRegistry
from .fx import Converted, FxService, RateTable, convert_amount
from .persistence import RFQRepository, SupplierRepository
from .quote_normalizer import (
    apply_discount, check_moq, comparable_across, currencies_in, normalize_price, refresh_derived_values,
    unnamed_currency_quotes,
)
from .rfq_service import RFQStateError
from .schema import RFQ, new_id, utc_now
from .supplier_ai_extractor import STAGES, SupplierExtractor
from .supplier_models import (
    ClaimStatus, ExtractionStatus, MatchStatus, NormalizationStatus, QuoteStatus, ResponseBundle,
    ResponseType, Supplier, SupplierQuote, SupplierResponse, SupplierStatus, ValueSource,
)

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "suppliers")

#: The demo cast. Fabricated; nothing is ever sent anywhere.
DEMO_SUPPLIERS = [
    {"key": "a", "name": "Anhui Packaging Co", "country": "China", "contact_name": "Li Wei",
     "documents": ["supplier_a_quote.xlsx"], "note": "Clean spreadsheet quotation."},
    {"key": "b", "name": "Shenzhen Print & Pack", "country": "China", "contact_name": "Chen Hui",
     "documents": ["supplier_b_quote.pdf"], "note": "PDF quoting per 100 pieces.",
     "revision": {"documents": ["supplier_b_revision.pdf"], "note": "Revised pricing."}},
    {"key": "c", "name": "Viet Carton JSC", "country": "Vietnam", "contact_name": "Nguyen Thi Mai",
     "documents": ["supplier_c_response.docx"], "note": "Prose response, partial coverage."},
    {"key": "d", "name": "Gujarat Boxes Pvt Ltd", "country": "India", "contact_name": "Rakesh Patel",
     "documents": ["supplier_d_response.txt"], "note": "Short email, refers to sizes by position."},
    {"key": "e", "name": "Istanbul Ambalaj", "country": "Turkey", "contact_name": "Emre Yilmaz",
     "documents": ["supplier_e_quote.png"], "note": "Photographed quotation."},
]

#: A sixth supplier who was invited and never replied. Absence is data too.
SILENT_SUPPLIER = {"name": "Pacific Carton Works", "country": "Philippines", "contact_name": "Jose Ramos"}


# --------------------------------------------------------------------------- #
# Comparison dataset
# --------------------------------------------------------------------------- #
class CellState(object):
    QUOTED = "quoted"
    NOT_QUOTED = "not_quoted"
    NEEDS_REVIEW = "needs_review"
    CONFLICT = "conflict"
    UNRESOLVED = "unresolved"
    NO_RESPONSE = "no_response"


@dataclass
class ComparisonCell:
    line_item_id: str
    supplier_id: str
    state: str = CellState.NOT_QUOTED
    quote: Optional[SupplierQuote] = None
    converted: Optional[Converted] = None    # set when a display currency is in force

    @property
    def native_display(self) -> str:
        """The price in the currency the supplier actually used."""
        if self.state == CellState.NO_RESPONSE:
            return "no response"
        if self.quote is None or not self.quote.has_price:
            return "not quoted"
        if self.quote.normalized_unit_price is None:
            return "unresolved"
        return "%s %s" % (self.quote.currency, _trim(self.quote.normalized_unit_price))

    @property
    def display(self) -> str:
        """What the table shows: converted when we have a real rate, native otherwise."""
        if self.converted is not None:
            return "%s %s" % (self.converted.currency, _trim(self.converted.amount))
        return self.native_display


@dataclass
class ComparisonMatrix:
    rfq: RFQ
    suppliers: List[Supplier] = field(default_factory=list)
    bundles: Dict[str, ResponseBundle] = field(default_factory=dict)      # supplier_id -> active bundle
    cells: Dict[Tuple[str, str], ComparisonCell] = field(default_factory=dict)
    summary: Dict[str, Any] = field(default_factory=dict)
    display_currency: Optional[str] = None        # None = show each supplier's own currency
    rates: Optional[RateTable] = None

    def cell(self, line_item_id: str, supplier_id: str) -> ComparisonCell:
        return self.cells.get((line_item_id, supplier_id),
                              ComparisonCell(line_item_id, supplier_id, CellState.NO_RESPONSE))

    def currencies(self) -> List[str]:
        out = []
        for b in self.bundles.values():
            for c in currencies_in(b.quotes):
                if c not in out:
                    out.append(c)
        return out

    def single_currency(self) -> bool:
        return len(self.currencies()) <= 1


# --------------------------------------------------------------------------- #
class SupplierService:
    def __init__(self, repo: RFQRepository, ai: AIService, settings: Optional[Settings] = None,
                 extractor: Optional[SupplierExtractor] = None):
        self.repo = repo
        self.store = SupplierRepository(repo)
        self.settings = settings or Settings.from_env()
        self.ai = ai
        self.extractor = extractor or SupplierExtractor(ai, self.settings)
        #: SQLite is in WAL mode and each call opens its own connection, but a bundle
        #: write spans several statements, so writes are serialised.
        self._write_lock = threading.Lock()
        self.fx = FxService(cache_path=os.path.join(os.path.dirname(self.settings.db_path) or ".",
                                                    "fx_cache.json"))

    # ------------------------------------------------------------ ingestion
    def seed_demo_responses(self, rfq_id: str, fixture_dir: str = FIXTURE_DIR,
                            include_revision: bool = True) -> List[SupplierResponse]:
        """Register the demo suppliers and their documents against an RFQ.

        This only records that responses arrived; nothing is read or interpreted here,
        so 'loading' stays honestly separate from extraction.
        """
        rfq = self.repo.get_rfq(rfq_id)
        if rfq is None:
            raise RFQStateError("RFQ %s not found" % rfq_id)
        missing = [d["documents"][0] for d in DEMO_SUPPLIERS
                   if not os.path.exists(os.path.join(fixture_dir, d["documents"][0]))]
        if missing:
            raise RFQStateError("Supplier fixtures are missing (%s). Run scripts/make_supplier_fixtures.py."
                                % ", ".join(missing))

        self.store.delete_responses_for(rfq_id)
        existing = {s.name: s for s in self.store.list_suppliers()}
        created: List[SupplierResponse] = []

        for spec in DEMO_SUPPLIERS:
            supplier = existing.get(spec["name"]) or Supplier(
                name=spec["name"], country=spec["country"], contact_name=spec["contact_name"],
                contact_email="sales@%s.example" % spec["key"])
            supplier.status = SupplierStatus.RESPONDED
            self.store.save_supplier(supplier)
            existing[supplier.name] = supplier

            created.append(self._register(rfq_id, supplier, spec["documents"], fixture_dir,
                                          received_at="2026-09-%02d" % (8 + DEMO_SUPPLIERS.index(spec))))
            if include_revision and spec.get("revision"):
                created.append(self._register(rfq_id, supplier, spec["revision"]["documents"], fixture_dir,
                                              received_at="2026-09-14", is_revision=True))

        silent = existing.get(SILENT_SUPPLIER["name"]) or Supplier(**SILENT_SUPPLIER)
        silent.status = SupplierStatus.NO_RESPONSE
        self.store.save_supplier(silent)
        return created

    def register_response(self, rfq_id: str, supplier: Supplier, filenames: List[str], folder: str,
                          received_at: str, is_revision: bool = False) -> SupplierResponse:
        """Record that a supplier response arrived, without reading it yet.

        Public entry point for callers outside this module (the Quotation Extraction Playground
        registers generated supplier documents this way). Identical to the seeding path.
        """
        return self._register(rfq_id, supplier, filenames, folder, received_at, is_revision)

    def _register(self, rfq_id: str, supplier: Supplier, filenames: List[str], fixture_dir: str,
                  received_at: str, is_revision: bool = False) -> SupplierResponse:
        response = SupplierResponse(
            rfq_id=rfq_id, supplier_id=supplier.id, received_at=received_at,
            response_type=ResponseType.REVISION_RECEIVED if is_revision else ResponseType.QUOTE_RECEIVED,
            extraction_status=ExtractionStatus.PENDING)
        bundle = ResponseBundle(response=response, supplier=supplier)
        registry = DocumentExtractorRegistry(self.settings)
        from .supplier_models import SourceDocument
        for name in filenames:
            path = os.path.join(fixture_dir, name)
            bundle.documents.append(SourceDocument(
                response_id=response.id, filename=name, path=path,
                media_type=registry.media_type_for(path),
                byte_size=os.path.getsize(path) if os.path.exists(path) else 0,
                extraction_status=ExtractionStatus.PENDING))
        self.store.save_bundle(bundle)
        return response

    # ----------------------------------------------------------- extraction
    def extract_response(self, response_id: str, on_stage: Optional[Callable[[str], None]] = None) -> ResponseBundle:
        bundle = self.store.get_bundle(response_id)
        if bundle is None:
            raise RFQStateError("Supplier response %s not found" % response_id)
        rfq = self.repo.get_rfq(bundle.response.rfq_id)
        if rfq is None:
            raise RFQStateError("RFQ %s not found" % bundle.response.rfq_id)
        supplier = bundle.supplier or self.store.get_supplier(bundle.response.supplier_id)
        paths = [d.path for d in bundle.documents]

        outcome = self.extractor.extract(rfq, supplier, paths, response=bundle.response, on_stage=on_stage)
        with self._write_lock:
            for rec in outcome.ai_calls:
                self.store.add_ai_call(rec, response_id=response_id)
            self.store.save_bundle(outcome.bundle)
            self._resolve_revisions(rfq.id, supplier.id)
        return outcome.bundle

    def extract_all(self, rfq_id: str, on_stage: Optional[Callable[[str, str], None]] = None,
                    only_pending: bool = True, max_workers: Optional[int] = None) -> Dict[str, Any]:
        """Extract every pending response, several at a time.

        Each supplier's extraction is an independent subprocess call, so running them
        concurrently is a straight wall-clock win. One supplier failing never stops the
        others, and progress is reported from this thread as each finishes, so it stays
        safe to call from a UI.
        """
        results: Dict[str, Any] = {"succeeded": [], "failed": [], "skipped": []}
        todo = []
        for response in self.store.list_responses(rfq_id):
            supplier = self.store.get_supplier(response.supplier_id)
            name = supplier.name if supplier else response.supplier_id
            if only_pending and response.extraction_status not in (ExtractionStatus.PENDING, ExtractionStatus.FAILED):
                results["skipped"].append(name)
                continue
            todo.append((response.id, name))
        if not todo:
            return results

        workers = max(1, min(max_workers or self.settings.extraction_workers, len(todo)))
        if on_stage:
            on_stage("", "Reading %d supplier response%s, %d at a time…"
                     % (len(todo), "" if len(todo) == 1 else "s", workers))

        def run(item):
            response_id, name = item
            try:
                return name, self.extract_response(response_id), None
            except Exception as e:                 # one supplier must never abandon the rest
                return name, None, e

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(run, item) for item in todo]
            for done in as_completed(futures):
                name, bundle, error = done.result()
                if error is not None:
                    note = getattr(error, "user_message", None) or str(error)[:200]
                    results["failed"].append((name, note))
                    self._mark_failed_by_name(rfq_id, name, note)
                elif bundle.response.extraction_status in (ExtractionStatus.FAILED, ExtractionStatus.UNSUPPORTED):
                    results["failed"].append((name, bundle.response.extraction_note))
                else:
                    results["succeeded"].append(name)
                if on_stage:
                    on_stage(name, "done · %d of %d" % (len(results["succeeded"]) + len(results["failed"]), len(todo)))
        return results

    def _mark_failed_by_name(self, rfq_id: str, name: str, note: str) -> None:
        for r in self.store.list_responses(rfq_id):
            supplier = self.store.get_supplier(r.supplier_id)
            if supplier and supplier.name == name and r.extraction_status != ExtractionStatus.EXTRACTED:
                self._mark_failed(r.id, note)
                return

    def _mark_failed(self, response_id: str, note: str) -> None:
        with self._write_lock:
            bundle = self.store.get_bundle(response_id)
            if bundle is None:
                return
            bundle.response.extraction_status = ExtractionStatus.FAILED
            bundle.response.extraction_note = note
            bundle.response.updated_at = utc_now()
            self.store.save_bundle(bundle)

    def _resolve_revisions(self, rfq_id: str, supplier_id: str) -> None:
        """The newest response for a supplier is the active one; earlier ones are kept."""
        responses = [r for r in self.store.list_responses(rfq_id) if r.supplier_id == supplier_id]
        if len(responses) < 2:
            return
        sg.resolve_revision_chain(responses)
        for r in responses:
            bundle = self.store.get_bundle(r.id)
            if bundle:
                bundle.response.is_active = r.is_active
                bundle.response.superseded_by_id = r.superseded_by_id
                bundle.response.revises_response_id = r.revises_response_id
                self.store.save_bundle(bundle)

    # ---------------------------------------------------------- comparison
    def build_comparison(self, rfq_id: str, display_currency: Optional[str] = None) -> ComparisonMatrix:
        """The normalised dataset: one cell per RFQ line per supplier. Phase 3 reads this.

        When `display_currency` is given, prices are converted using a real published
        rate. The supplier's own figure and currency are kept on the quote either way, so
        a converted number can always be traced back to what they actually wrote.
        """
        rfq = self.repo.get_rfq(rfq_id)
        if rfq is None:
            raise RFQStateError("RFQ %s not found" % rfq_id)
        matrix = ComparisonMatrix(rfq=rfq, display_currency=(display_currency or "").strip().upper() or None)

        bundles = self.store.list_bundles(rfq_id, active_only=True)
        by_supplier = {b.response.supplier_id: b for b in bundles}
        matrix.bundles = by_supplier

        supplier_ids = list(by_supplier.keys())
        all_suppliers = {s.id: s for s in self.store.list_suppliers()}
        matrix.suppliers = [all_suppliers[i] for i in supplier_ids if i in all_suppliers]
        # suppliers who never replied still belong in the comparison
        for s in all_suppliers.values():
            if s.id not in by_supplier and s.status == SupplierStatus.NO_RESPONSE:
                matrix.suppliers.append(s)

        # Prices, discounts and MOQ flags are *derived*. Recompute them from the stored
        # facts on every read so a change to the rules cannot leave a stale figure on
        # screen, and so a quote never silently keeps a number the rules would now refuse.
        lines_by_id = {li.id: li for li in rfq.line_items}
        for bundle in bundles:
            for q in bundle.quotes:
                if q.value_source == ValueSource.BUYER_CORRECTED:
                    continue                      # a human decision is not re-derived
                refresh_derived_values(q, rfq, lines_by_id.get(q.line_item_id or ""))

        for line in rfq.line_items:
            for supplier in matrix.suppliers:
                bundle = by_supplier.get(supplier.id)
                if bundle is None:
                    matrix.cells[(line.id, supplier.id)] = ComparisonCell(
                        line.id, supplier.id, CellState.NO_RESPONSE)
                    continue
                quote = next((q for q in bundle.quotes if q.line_item_id == line.id), None)
                matrix.cells[(line.id, supplier.id)] = ComparisonCell(
                    line.id, supplier.id, _cell_state(quote), quote)

        if matrix.display_currency:
            matrix.rates = self.fx.rates(matrix.display_currency)
            for cell in matrix.cells.values():
                q = cell.quote
                if q is None or q.normalized_unit_price is None:
                    continue
                cell.converted = convert_amount(matrix.rates, q.normalized_unit_price,
                                                q.currency, matrix.display_currency)

        matrix.summary = self._summary(rfq, matrix, bundles, all_suppliers)
        return matrix

    def _summary(self, rfq: RFQ, matrix: ComparisonMatrix, bundles: List[ResponseBundle],
                 all_suppliers: Dict[str, Supplier]) -> Dict[str, Any]:
        """Every figure here is counted from the stored data, never hardcoded."""
        responded = {b.response.supplier_id for b in bundles}
        no_response = [s for s in all_suppliers.values()
                       if s.id not in responded and s.status == SupplierStatus.NO_RESPONSE]
        states: Dict[str, int] = {}
        for cell in matrix.cells.values():
            states[cell.state] = states.get(cell.state, 0) + 1

        issues: Dict[str, int] = {}
        for b in bundles:
            for k, v in b.issue_counts().items():
                issues[k] = issues.get(k, 0) + v

        revisions = [r for r in self.store.list_responses(rfq.id) if not r.is_active]
        return {
            "suppliers_total": len(responded) + len(no_response),
            "responses_received": len(bundles),
            "no_response": len(no_response),
            "rfq_lines": len(rfq.line_items),
            "line_responses": states.get(CellState.QUOTED, 0) + states.get(CellState.NEEDS_REVIEW, 0)
                              + states.get(CellState.CONFLICT, 0) + states.get(CellState.UNRESOLVED, 0),
            "missing_quotes": states.get(CellState.NOT_QUOTED, 0) + states.get(CellState.NO_RESPONSE, 0),
            "need_review": sum(1 for b in bundles if b.needs_review()),
            "cells": states,
            "issues": issues,
            "superseded_responses": len(revisions),
            "currencies": matrix.currencies(),
            "single_currency": matrix.single_currency(),
            "unnamed_currency": sum(len(unnamed_currency_quotes(b.quotes)) for b in bundles),
            "display_currency": matrix.display_currency,
            "rate_source": matrix.rates.source if (matrix.rates and matrix.rates.ok) else "",
            "rate_as_of": matrix.rates.as_of if (matrix.rates and matrix.rates.ok) else "",
            "rate_error": (matrix.rates.error if matrix.rates else "") if matrix.display_currency else "",
            "unconvertible": len([c for c in matrix.cells.values()
                                  if c.quote is not None and c.quote.normalized_unit_price is not None
                                  and c.converted is None]) if matrix.display_currency else 0,
        }

    # --------------------------------------------------- human in the loop
    def correct_quote(self, response_id: str, quote_id: str, **changes) -> ResponseBundle:
        """Apply a buyer correction. The extracted value is kept, never overwritten away."""
        bundle = self.store.get_bundle(response_id)
        if bundle is None:
            raise RFQStateError("Supplier response %s not found" % response_id)
        quote = next((q for q in bundle.quotes if q.id == quote_id), None)
        if quote is None:
            raise RFQStateError("Quote %s not found" % quote_id)

        before = {k: getattr(quote, k) for k in changes if hasattr(quote, k)}
        quote.history.append({"at": utc_now(), "by": "buyer", "was": _jsonable(before),
                              "reason": "manual correction"})
        for k, v in changes.items():
            if hasattr(quote, k):
                setattr(quote, k, v)
        quote.value_source = ValueSource.BUYER_CORRECTED
        quote.confidence = 1.0
        quote.issues = [i for i in quote.issues if "could not be traced" not in i]
        if quote.status in (QuoteStatus.NEEDS_REVIEW, QuoteStatus.UNRESOLVED, QuoteStatus.CONFLICT):
            quote.status = QuoteStatus.QUOTED

        rfq = self.repo.get_rfq(bundle.response.rfq_id)
        if rfq:
            apply_discount(quote, rfq)
            normalize_price(quote)
            line = next((li for li in rfq.line_items if li.id == quote.line_item_id), None)
            check_moq(quote, line)
        bundle.response.extraction_status = (ExtractionStatus.NEEDS_REVIEW if bundle.needs_review()
                                             else ExtractionStatus.EXTRACTED)
        self.store.save_bundle(bundle)
        return bundle

    def confirm_match(self, response_id: str, quote_id: str, line_item_id: Optional[str]) -> ResponseBundle:
        """Accept or redirect a proposed line match. Confirmation is a buyer decision."""
        bundle = self.store.get_bundle(response_id)
        if bundle is None:
            raise RFQStateError("Supplier response %s not found" % response_id)
        quote = next((q for q in bundle.quotes if q.id == quote_id), None)
        if quote is None:
            raise RFQStateError("Quote %s not found" % quote_id)
        rfq = self.repo.get_rfq(bundle.response.rfq_id)
        valid = {li.id for li in (rfq.line_items if rfq else [])}
        if line_item_id and line_item_id not in valid:
            raise RFQStateError("RFQ line %s does not exist" % line_item_id)

        quote.history.append({"at": utc_now(), "by": "buyer", "was": {"line_item_id": quote.line_item_id},
                              "reason": "match confirmed"})
        quote.line_item_id = line_item_id
        quote.match_status = MatchStatus.MATCHED if line_item_id else MatchStatus.UNMATCHED
        quote.match_reason = "Confirmed by the buyer." if line_item_id else "Buyer removed the match."
        if line_item_id and quote.status == QuoteStatus.NEEDS_REVIEW and quote.has_price:
            blocking = [i for i in quote.issues if "currency" in i.lower() or "traced" in i.lower()]
            if not blocking:
                quote.status = QuoteStatus.QUOTED
        if rfq:
            line = next((li for li in rfq.line_items if li.id == line_item_id), None)
            check_moq(quote, line)
        bundle.response.extraction_status = (ExtractionStatus.NEEDS_REVIEW if bundle.needs_review()
                                             else ExtractionStatus.EXTRACTED)
        self.store.save_bundle(bundle)
        return bundle

    def answer_supplier_question(self, response_id: str, question_id: str, answer: str) -> ResponseBundle:
        """Record the buyer's answer. Nothing is sent anywhere; this is a local draft."""
        bundle = self.store.get_bundle(response_id)
        if bundle is None:
            raise RFQStateError("Supplier response %s not found" % response_id)
        q = next((x for x in bundle.questions if x.id == question_id), None)
        if q is None:
            raise RFQStateError("Question %s not found" % question_id)
        q.buyer_answer = answer
        q.resolved = bool(answer.strip())
        self.store.save_bundle(bundle)
        return bundle

    # ------------------------------------------------------------- reading
    def bundles_for(self, rfq_id: str, active_only: bool = False) -> List[ResponseBundle]:
        return self.store.list_bundles(rfq_id, active_only=active_only)

    def bundle(self, response_id: str) -> Optional[ResponseBundle]:
        return self.store.get_bundle(response_id)

    def responses_for(self, rfq_id: str) -> List[SupplierResponse]:
        return self.store.list_responses(rfq_id)

    def supplier(self, supplier_id: str) -> Optional[Supplier]:
        return self.store.get_supplier(supplier_id)

    def extraction_calls(self, response_id: str) -> List[Any]:
        return self.store.list_extraction_calls(response_id)

    def has_responses(self, rfq_id: str) -> bool:
        return bool(self.store.list_responses(rfq_id))

    def review_queue(self, rfq_id: str) -> List[Dict[str, Any]]:
        """Everything the system is not willing to assert on its own."""
        items: List[Dict[str, Any]] = []
        seen_conflicts: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for b in self.bundles_for(rfq_id, active_only=True):
            name = b.supplier.name if b.supplier else b.response.supplier_id
            for q in b.quotes:
                if q.match_status == MatchStatus.PROBABLE_MATCH:
                    items.append({"kind": "probable_match", "supplier": name, "response_id": b.response.id,
                                  "quote_id": q.id, "label": q.supplier_line_label,
                                  "detail": q.match_reason, "line_item_id": q.line_item_id,
                                  "alternatives": q.match_candidates})
                elif q.match_status in (MatchStatus.UNMATCHED, MatchStatus.CONFLICT) and q.has_price:
                    items.append({"kind": "unmatched", "supplier": name, "response_id": b.response.id,
                                  "quote_id": q.id, "label": q.supplier_line_label,
                                  "detail": q.match_reason, "alternatives": q.match_candidates})
                # a response-level contradiction (one lead time stated twice) touches every
                # line it applies to; the buyer needs to see it once, not once per line
                for c in q.conflicts:
                    key = (b.response.id, (c.get("topic") or "").lower())
                    if key in seen_conflicts:
                        seen_conflicts[key]["affected_lines"] += 1
                        continue
                    entry = {"kind": "conflict", "supplier": name, "response_id": b.response.id,
                             "quote_id": q.id, "label": c.get("topic") or "contradiction",
                             "detail": c.get("description", "")[:240],
                             "values": [v.get("value") for v in c.get("values", [])],
                             "affected_lines": 1}
                    seen_conflicts[key] = entry
                    items.append(entry)
                if q.normalization_status == NormalizationStatus.UNRESOLVED and q.has_price:
                    items.append({"kind": "unresolved_price", "supplier": name, "response_id": b.response.id,
                                  "quote_id": q.id, "label": q.supplier_line_label,
                                  "detail": q.normalization_note})
            for c in b.certifications:
                if c.status == ClaimStatus.CLAIMED:
                    items.append({"kind": "unverified_claim", "supplier": name, "response_id": b.response.id,
                                  "label": c.name, "detail": c.note})
            for sq in b.questions:
                if not sq.resolved:
                    items.append({"kind": "supplier_question", "supplier": name, "response_id": b.response.id,
                                  "question_id": sq.id, "label": sq.question, "detail": sq.related_field_key})
        return items


# --------------------------------------------------------------------------- #
def _cell_state(quote: Optional[SupplierQuote]) -> str:
    if quote is None:
        return CellState.NOT_QUOTED
    return {
        QuoteStatus.QUOTED: CellState.QUOTED,
        QuoteStatus.NOT_QUOTED: CellState.NOT_QUOTED,
        QuoteStatus.NEEDS_REVIEW: CellState.NEEDS_REVIEW,
        QuoteStatus.CONFLICT: CellState.CONFLICT,
        QuoteStatus.UNRESOLVED: CellState.UNRESOLVED,
    }.get(quote.status, CellState.NEEDS_REVIEW)


def _trim(value: float) -> str:
    text = "%.4f" % value
    return text.rstrip("0").rstrip(".") if "." in text else text


def _jsonable(d: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for k, v in d.items():
        out[k] = v.value if hasattr(v, "value") else (v if isinstance(v, (int, float, str, bool, type(None))) else str(v))
    return out
