"""Read one supplier's response end to end.

    documents  ->  raw text (+ where each bit sits)
               ->  one structured AI extraction
               ->  guards: evidence, invented prices, bad line ids, claimed vs verified
               ->  line matching against the RFQ
               ->  price / discount / MOQ normalisation
               ->  a ResponseBundle

Document parsing and procurement reasoning stay apart: this module never opens a file
itself, and the DocumentExtractor never has an opinion about prices.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from . import supplier_guards as sg
from .ai_service import AIError, AIInvalidOutput, AIResult, AIService
from .config import Settings
from .document_extractor import DocumentExtractorRegistry
from .line_matcher import match_all
from .quote_normalizer import apply_discount, check_moq, normalize_price, parse_lead_time, parse_quote_validity
from .schema import RFQ, AICallRecord, new_id, utc_now
from .supplier_ai_schemas import LINE_MATCH_SCHEMA, SUPPLIER_EXTRACTION_SCHEMA
from .supplier_models import (
    Certification, ClaimStatus, Discount, Evidence, ExtractionStatus, MatchStatus, PriceBasis,
    QuestionnaireResponse, QuoteStatus, ResponseBundle, ResponseType, SourceDocument, Supplier,
    SupplierQuestion, SupplierQuote, SupplierResponse, ValueSource,
)
from .supplier_prompts import (
    EXTRACTION_PROMPT_VERSION, EXTRACTION_SYSTEM_PROMPT, MATCH_PROMPT_VERSION, MATCH_SYSTEM_PROMPT,
    build_extraction_prompt, build_match_prompt,
)

#: Reported back to the UI so progress reflects work that actually happened.
STAGES = [
    "Reading supplier documents…",
    "Extracting quotation lines and commercial terms…",
    "Matching supplier lines to RFQ lines…",
    "Normalizing units and prices…",
    "Checking questionnaire, certifications and conflicts…",
]


@dataclass
class ExtractionOutcome:
    bundle: ResponseBundle
    audit: List[str]
    ai_calls: List[AICallRecord]
    ok: bool


class SupplierExtractor:
    def __init__(self, ai: AIService, settings: Optional[Settings] = None,
                 registry: Optional[DocumentExtractorRegistry] = None):
        self.ai = ai
        self.settings = settings or Settings.from_env()
        self.registry = registry or DocumentExtractorRegistry(self.settings)

    # ------------------------------------------------------------------ #
    def extract(self, rfq: RFQ, supplier: Supplier, document_paths: List[str],
                response: Optional[SupplierResponse] = None,
                on_stage=None) -> ExtractionOutcome:
        audit: List[str] = []
        calls: List[AICallRecord] = []
        response = response or SupplierResponse(rfq_id=rfq.id, supplier_id=supplier.id)
        bundle = ResponseBundle(response=response, supplier=supplier)

        def stage(i: int) -> None:
            if on_stage:
                on_stage(STAGES[i])

        # 1. documents ---------------------------------------------------
        stage(0)
        documents, ceiling = self._read_documents(response.id, document_paths, audit)
        bundle.documents = documents
        readable = [d for d in documents if d.raw_text.strip()]
        response.raw_content = "\n\n".join("### %s\n%s" % (d.filename, d.raw_text) for d in readable)

        if not readable:
            response.extraction_status = ExtractionStatus.UNSUPPORTED
            response.extraction_note = "None of the supplied documents could be read."
            audit.append(response.extraction_note)
            return ExtractionOutcome(bundle=bundle, audit=audit, ai_calls=calls, ok=False)

        # 2. structured extraction --------------------------------------
        stage(1)
        payload = [{"filename": d.filename, "media_type": d.media_type,
                    "note": d.extraction_note, "text": d.raw_text} for d in readable]
        prompt = build_extraction_prompt(rfq, supplier.name, payload)
        try:
            data, rec = self._call(prompt, SUPPLIER_EXTRACTION_SCHEMA, EXTRACTION_SYSTEM_PROMPT,
                                   "supplier_extraction", EXTRACTION_PROMPT_VERSION, response.id)
            calls.append(rec)
        except AIError as e:
            calls.extend(getattr(e, "records", []))
            response.extraction_status = ExtractionStatus.FAILED
            response.extraction_note = e.user_message
            audit.append("Extraction failed: %s" % e.user_message)
            return ExtractionOutcome(bundle=bundle, audit=audit, ai_calls=calls, ok=False)

        # 3. turn observations into guarded records ----------------------
        self._apply_response_meta(response, data)
        evidence: Dict[str, Evidence] = {}
        quotes = self._build_quotes(rfq, supplier, response, data, documents, evidence, ceiling)
        bundle.evidence = evidence

        # 4. matching -----------------------------------------------------
        stage(2)
        raw_lines = data.get("quote_lines") or []
        ai_matches = self._match_with_model(rfq, raw_lines, response.id, calls, audit)
        results = match_all(rfq, raw_lines, ai_matches)
        for quote, result in zip(quotes, results):
            quote.line_item_id = result.line_item_id
            quote.match_status = result.status
            quote.match_reason = result.reason
            quote.match_candidates = result.alternatives
        audit.extend(sg.guard_line_ids(quotes, rfq))
        audit.extend(sg.guard_no_invented_lines(quotes, documents))

        # 5. normalisation ------------------------------------------------
        stage(3)
        lines_by_id = {li.id: li for li in rfq.line_items}
        for q in quotes:
            apply_discount(q, rfq)
            normalize_price(q)
            check_moq(q, lines_by_id.get(q.line_item_id or ""))
            sg.guard_quote(q, evidence, ceiling)
        bundle.quotes = quotes

        # 6. questionnaire, certifications, questions, conflicts ---------
        stage(4)
        answers = self._build_questionnaire(response, data, documents, evidence, ceiling)
        answers, notes = sg.guard_questionnaire(answers, rfq, documents, evidence)
        audit.extend(notes)
        bundle.questionnaire = answers + sg.missing_questionnaire_items(answers, rfq)

        bundle.certifications = [
            sg.guard_certification(c, documents, evidence)
            for c in self._build_certifications(response, data, documents, evidence, ceiling)]
        bundle.questions = self._build_questions(response, data, documents, evidence)
        conflicts = sg.record_conflicts(data.get("conflicts") or [], response.id, documents, evidence)
        for c in conflicts:
            self._attach_conflict(bundle, c)
        response.uncertainties = [str(u) for u in (data.get("uncertainties") or [])][:12]

        self._finalise_status(bundle, rfq)
        return ExtractionOutcome(bundle=bundle, audit=audit, ai_calls=calls, ok=True)

    # ------------------------------------------------------------------ #
    def _read_documents(self, response_id: str, paths: List[str], audit: List[str]) -> Tuple[List[SourceDocument], float]:
        docs, ceiling = [], 1.0
        for path in paths:
            content = self.registry.extract(path)
            doc = SourceDocument(
                response_id=response_id, filename=os.path.basename(path), path=path,
                media_type=content.media_type or self.registry.media_type_for(path),
                byte_size=os.path.getsize(path) if os.path.exists(path) else 0,
                extraction_method=content.method, extraction_status=content.status,
                extraction_note=content.note, raw_text=content.text,
            )
            docs.append(doc)
            ceiling = min(ceiling, content.confidence_ceiling)
            if content.status != ExtractionStatus.EXTRACTED:
                audit.append("%s: %s" % (doc.filename, content.note or content.status.value))
            elif content.note:
                audit.append("%s: %s" % (doc.filename, content.note))
        return docs, ceiling

    def _call(self, prompt: str, schema: Dict[str, Any], system: str, call_type: str,
              version: str, response_id: str) -> Tuple[Dict[str, Any], AICallRecord]:
        """One structured call with a single corrective retry, always audited."""
        attempt = prompt
        last: Optional[AIError] = None
        records: List[AICallRecord] = []
        for _ in range(2):
            started = utc_now()
            try:
                res: AIResult = self.ai.complete_json(attempt, schema, system, tier="quality")
                records.append(self._record(call_type, version, attempt, res, None, response_id, started))
                return res.data, records[-1]
            except AIInvalidOutput as e:
                last = e
                records.append(self._record(call_type, version, attempt, None, e, response_id, started))
                attempt = prompt + ("\n\nYOUR PREVIOUS OUTPUT FAILED VALIDATION: %s\n"
                                    "Return a corrected JSON object matching the schema exactly." % str(e)[:400])
            except AIError as e:
                records.append(self._record(call_type, version, attempt, None, e, response_id, started))
                e.records = records
                raise
        assert last is not None
        last.records = records
        raise last

    def _record(self, call_type: str, version: str, prompt: str, res: Optional[AIResult],
                err: Optional[AIError], response_id: str, started: str) -> AICallRecord:
        return AICallRecord(
            id=new_id("call"), rfq_id=None, turn=0, call_type=call_type,
            provider=getattr(self.ai, "name", "unknown"),
            model=res.model if res else getattr(self.ai, "model_for", lambda t: "?")("quality"),
            prompt_version=version, prompt_hash=hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16],
            prompt_chars=len(prompt), duration_ms=res.duration_ms if res else 0,
            ok=res is not None, schema_valid=bool(res and res.schema_valid),
            error=("%s: %s" % (type(err).__name__, err)) if err else None,
            raw_response=(res.raw if res else (getattr(err, "raw", "") or ""))[:200000],
            prompt_text=prompt if self.settings.log_prompts else None, created_at=started,
        )

    def _match_with_model(self, rfq: RFQ, raw_lines: List[Dict[str, Any]], response_id: str,
                          calls: List[AICallRecord], audit: List[str]) -> List[Dict[str, Any]]:
        """A second opinion on line identity. Failure here is survivable: the
        deterministic matcher still runs, and unmatched lines simply need review."""
        if not raw_lines or not rfq.line_items:
            return []
        try:
            data, rec = self._call(build_match_prompt(rfq, raw_lines), LINE_MATCH_SCHEMA,
                                   MATCH_SYSTEM_PROMPT, "supplier_line_match", MATCH_PROMPT_VERSION, response_id)
            calls.append(rec)
            return list(data.get("matches") or [])
        except AIError as e:
            calls.extend(getattr(e, "records", []))
            audit.append("Line matching by model unavailable (%s); fell back to dimension matching."
                         % type(e).__name__)
            return []

    # ------------------------------------------------------------------ #
    def _apply_response_meta(self, response: SupplierResponse, data: Dict[str, Any]) -> None:
        meta = data.get("response") or {}
        try:
            response.response_type = ResponseType(str(meta.get("response_type") or "quote_received"))
        except ValueError:
            response.response_type = ResponseType.QUOTE_RECEIVED
        if meta.get("is_revision"):
            response.response_type = ResponseType.REVISION_RECEIVED
        response.subject = str((data.get("supplier") or {}).get("quote_reference") or "") or response.subject
        response.extraction_note = str(meta.get("summary") or "")[:400]

    def _build_quotes(self, rfq: RFQ, supplier: Supplier, response: SupplierResponse, data: Dict[str, Any],
                      documents: List[SourceDocument], evidence: Dict[str, Evidence],
                      ceiling: float) -> List[SupplierQuote]:
        terms = data.get("commercial_terms") or {}
        shared = self._shared_terms(terms, response.id, documents, evidence)
        quotes = []
        for raw in data.get("quote_lines") or []:
            ev_id = sg.build_evidence(raw.get("evidence"), response.id, documents, evidence)
            q = SupplierQuote(
                response_id=response.id, rfq_id=rfq.id, supplier_id=supplier.id,
                supplier_line_label=str(raw.get("supplier_line_label") or raw.get("described_size") or "")[:120],
                quoted_quantity=_num(raw.get("quoted_quantity")),
                quoted_unit=str(raw.get("quoted_unit") or "") or shared["quoted_unit"],
                unit_price=_num(raw.get("unit_price")),
                currency=(str(raw.get("currency") or "") or shared["currency"]).strip().upper()[:8],
                price_basis=_basis(raw.get("price_basis")),
                price_is_indicative=bool(raw.get("price_is_indicative")),
                minimum_order_quantity=_num(raw.get("minimum_order_quantity")) if raw.get("minimum_order_quantity") is not None else shared["moq"],
                moq_unit=shared["moq_unit"],
                lead_time_text=str(raw.get("lead_time_text") or "") or shared["lead_time_text"],
                delivery_terms=shared["delivery_terms"],
                payment_terms=shared["payment_terms"],
                quote_validity_text=shared["validity_text"],
                quote_validity_is_conditional=shared["validity_conditional"],
                discount=shared["discount"],
                value_source=ValueSource.SUPPLIER_STATED,
                confidence=_num(raw.get("confidence")),
                evidence_ids=[e for e in (ev_id, shared["moq_evidence"]) if e],
            )
            days, interpreted = parse_lead_time(q.lead_time_text)
            q.lead_time_days, q.lead_time_is_interpreted = days, interpreted
            vdays, conditional = parse_quote_validity(q.quote_validity_text)
            q.quote_validity_days = vdays
            q.quote_validity_is_conditional = q.quote_validity_is_conditional or conditional
            quotes.append(q)

        # lines the supplier said outright they will not quote: recorded as absent, not zero
        for skipped in data.get("lines_explicitly_not_quoted") or []:
            ev_id = sg.build_evidence(skipped.get("evidence"), response.id, documents, evidence)
            q = SupplierQuote(
                response_id=response.id, rfq_id=rfq.id, supplier_id=supplier.id,
                supplier_line_label=str(skipped.get("supplier_line_label") or "")[:120],
                status=QuoteStatus.NOT_QUOTED, value_source=ValueSource.SUPPLIER_STATED,
                evidence_ids=[e for e in (ev_id,) if e], confidence=ceiling,
                issues=["Supplier stated they cannot quote this line: %s"
                        % (skipped.get("reason") or "no reason given")],
            )
            quotes.append(q)
        return quotes

    def _shared_terms(self, terms: Dict[str, Any], response_id: str, documents: List[SourceDocument],
                      evidence: Dict[str, Evidence]) -> Dict[str, Any]:
        raw_disc = terms.get("discount") or {}
        disc_ev = sg.build_evidence(raw_disc.get("evidence"), response_id, documents, evidence)
        discount = Discount(
            percent=_num(raw_disc.get("percent")), amount=_num(raw_disc.get("amount")),
            condition=str(raw_disc.get("condition") or ""),
            evidence_ids=[e for e in (disc_ev,) if e])
        return {
            "currency": str(terms.get("currency") or "").strip().upper(),
            "quoted_unit": "",
            "moq": _num(terms.get("minimum_order_quantity")),
            "moq_unit": str(terms.get("moq_unit") or ""),
            "moq_evidence": sg.build_evidence(terms.get("moq_evidence"), response_id, documents, evidence),
            "lead_time_text": str(terms.get("lead_time_text") or ""),
            "payment_terms": str(terms.get("payment_terms") or ""),
            "delivery_terms": str(terms.get("delivery_terms") or ""),
            "validity_text": str(terms.get("quote_validity_text") or ""),
            "validity_conditional": bool(terms.get("quote_validity_is_conditional")),
            "discount": discount,
        }

    def _build_questionnaire(self, response: SupplierResponse, data: Dict[str, Any],
                             documents: List[SourceDocument], evidence: Dict[str, Evidence],
                             ceiling: float) -> List[QuestionnaireResponse]:
        out = []
        for raw in data.get("questionnaire_answers") or []:
            ev_id = sg.build_evidence(raw.get("evidence"), response.id, documents, evidence)
            out.append(QuestionnaireResponse(
                response_id=response.id,
                question_id=str(raw.get("question_id") or ""),
                field_key=str(raw.get("field_key") or ""),
                answer=str(raw.get("answer") or ""),
                value_source=ValueSource.SUPPLIER_STATED,
                confidence=min(_num(raw.get("confidence")) or ceiling, ceiling),
                evidence_ids=[e for e in (ev_id,) if e]))
        return out

    def _build_certifications(self, response: SupplierResponse, data: Dict[str, Any],
                              documents: List[SourceDocument], evidence: Dict[str, Evidence],
                              ceiling: float) -> List[Certification]:
        out = []
        for raw in data.get("certifications") or []:
            ev_id = sg.build_evidence(raw.get("evidence"), response.id, documents, evidence)
            out.append(Certification(
                response_id=response.id,
                name=str(raw.get("name") or ""), raw_name=str(raw.get("raw_name") or raw.get("name") or ""),
                certificate_number=str(raw.get("certificate_number") or ""),
                issuing_body=str(raw.get("issuing_body") or ""),
                expiry_date=str(raw.get("expiry_date") or ""),
                document_reference=str(raw.get("document_reference") or ""),
                confidence=min(_num(raw.get("confidence")) or ceiling, ceiling),
                evidence_ids=[e for e in (ev_id,) if e]))
        return out

    def _build_questions(self, response: SupplierResponse, data: Dict[str, Any],
                         documents: List[SourceDocument], evidence: Dict[str, Evidence]) -> List[SupplierQuestion]:
        out = []
        for raw in data.get("supplier_questions") or []:
            ev_id = sg.build_evidence(raw.get("evidence"), response.id, documents, evidence)
            out.append(SupplierQuestion(
                response_id=response.id, question=str(raw.get("question") or ""),
                related_field_key=str(raw.get("related_field_key") or ""),
                evidence_ids=[e for e in (ev_id,) if e]))
        return out

    @staticmethod
    def _attach_conflict(bundle: ResponseBundle, conflict: Dict[str, Any]) -> None:
        """Hang a conflict on the quotes it affects, or on the response when it is global."""
        topic = (conflict.get("topic") or "").lower()
        touched = False
        if "lead" in topic or "deliver" in topic:
            for q in bundle.quotes:
                q.conflicts.append(conflict)
                if q.status == QuoteStatus.QUOTED:
                    q.status = QuoteStatus.CONFLICT
                touched = True
        elif "price" in topic or "cost" in topic:
            for q in bundle.quotes:
                if q.has_price:
                    q.conflicts.append(conflict)
                    q.status = QuoteStatus.CONFLICT
                    touched = True
        if not touched:
            bundle.response.uncertainties.append(
                "%s: %s" % (conflict.get("topic"), conflict.get("description")))

    @staticmethod
    def _finalise_status(bundle: ResponseBundle, rfq: RFQ) -> None:
        r = bundle.response
        priced = [q for q in bundle.quotes if q.has_price]
        matched = {q.line_item_id for q in priced if q.line_item_id}
        total_lines = len(rfq.line_items)

        if bundle.questions and not priced:
            r.response_type = ResponseType.QUESTION
        elif r.response_type != ResponseType.REVISION_RECEIVED:
            if priced and total_lines and len(matched) < total_lines:
                r.response_type = ResponseType.PARTIAL_QUOTE
            elif priced:
                r.response_type = ResponseType.QUOTE_RECEIVED

        r.extraction_status = ExtractionStatus.NEEDS_REVIEW if bundle.needs_review() else ExtractionStatus.EXTRACTED
        r.updated_at = utc_now()


# --------------------------------------------------------------------------- #
def _num(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f          # drop NaN


def _basis(v: Any) -> PriceBasis:
    try:
        return PriceBasis(str(v))
    except ValueError:
        return PriceBasis.UNKNOWN
