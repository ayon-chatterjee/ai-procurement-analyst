"""Requirement Playground — a new way in, not a new engine.

Give it an email, some attachments, or both, and it works out what is being asked for.
Everything downstream is the existing application:

    raw text + attachments
      -> DocumentExtractorRegistry        (Phase 2's reader, unchanged)
      -> one structured AI extraction     (new, this module)
      -> adapter into an RFQ              (new, this module)
      -> guards.compute_completeness      (Phase 1's rules, unchanged)
      -> SupplierService                  (Phase 2's engine, unchanged)

The adapter is the whole trick: once requirements are expressed as an ``RFQ`` with
``LineItem`` and ``FieldValue`` records, every existing rule about missing information,
readiness, supplier matching and comparison applies without modification.
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import guards
from .ai_service import AIError, AIInvalidOutput, AIResult, AIService
from .config import Settings
from .document_extractor import DocumentExtractorRegistry
from .fields import FIELD_SPECS, new_field_set
from .persistence import RFQRepository
from .playground_prompts import (
    EXTRACTION_SYSTEM_PROMPT, PLAYGROUND_PROMPT_VERSION, SIMULATION_PROMPT_VERSION,
    SIMULATION_SYSTEM_PROMPT, build_extraction_prompt, build_simulation_prompt,
)
from .playground_schemas import PLAYGROUND_EXTRACTION_SCHEMA, SIMULATED_RESPONSES_SCHEMA
from .rfq_service import RFQStateError
from .schema import (
    RFQ, AICallRecord, FieldStatus, FieldValue, Importance, LineItem, Question, RFQStatus, Section,
    Source, SpecAttr, ValueKind, new_id, utc_now,
)
from .supplier_models import ExtractionStatus, Supplier, SupplierStatus

STAGES = [
    "Reading attachments…",
    "Working out what is being asked for…",
    "Matching requirements to the procurement checklist…",
]


class PlaygroundError(Exception):
    """Something the user can act on, phrased for them."""


# --------------------------------------------------------------------------- #
# Result objects (held in session state; nothing is persisted until the user asks)
# --------------------------------------------------------------------------- #
@dataclass
class SourceFile:
    filename: str
    media_type: str
    status: ExtractionStatus
    note: str = ""
    chars: int = 0
    method: str = ""

    @property
    def readable(self) -> bool:
        return self.status == ExtractionStatus.EXTRACTED and self.chars > 0


@dataclass
class Requirement:
    """One stated requirement, with where it came from."""
    field_key: str
    label: str
    value: str
    unit: Optional[str] = None
    status: str = "found"            # found | ambiguous
    source_kind: str = "email_text"
    source_document: str = ""
    source_location: str = ""
    quoted_text: str = ""
    note: str = ""
    confidence: Optional[float] = None
    verified: bool = False           # the quoted span was found in the supplied material

    def source_label(self) -> str:
        if self.source_kind == "email_text":
            return "Email / request text"
        bits = [self.source_document or "attachment"]
        if self.source_location:
            bits.append(self.source_location)
        return " · ".join(bits)

    def display_value(self) -> str:
        """Value plus unit, unless the buyer already wrote the unit into the value."""
        value = (self.value or "").strip()
        unit = (self.unit or "").strip()
        if not unit:
            return value
        tail = re.sub(r"[^a-z0-9]+", "", value.lower())
        if tail.endswith(re.sub(r"[^a-z0-9]+", "", unit.lower())):
            return value
        return ("%s %s" % (value, unit)).strip()


@dataclass
class PlaygroundLineItem:
    index: int
    name: str
    description: str = ""
    quantity: Optional[float] = None
    unit: str = ""
    specifications: List[Tuple[str, str, Optional[str]]] = field(default_factory=list)
    requirements: List[Requirement] = field(default_factory=list)
    ambiguities: List[str] = field(default_factory=list)
    evidence: str = ""

    def label(self) -> str:
        qty = (" · %s %s" % ("{:,.0f}".format(self.quantity), self.unit or "units")) if self.quantity else ""
        return "%s%s" % (self.name, qty)


@dataclass
class PlaygroundResult:
    title: str = ""
    product: str = ""
    category: str = ""
    product_type: str = ""
    summary: str = ""
    line_items: List[PlaygroundLineItem] = field(default_factory=list)
    shared_requirements: List[Requirement] = field(default_factory=list)
    ambiguities: List[str] = field(default_factory=list)
    sources: List[SourceFile] = field(default_factory=list)
    nothing_found_reason: str = ""
    request_text: str = ""
    ai_calls: List[AICallRecord] = field(default_factory=list)
    rfq_id: Optional[str] = None          # set once sent into Phase 2

    @property
    def found_anything(self) -> bool:
        return bool(self.line_items)

    def all_requirements(self) -> List[Requirement]:
        out = list(self.shared_requirements)
        for li in self.line_items:
            out.extend(li.requirements)
        return out

    def source_counts(self) -> Dict[str, int]:
        counts = {"email_text": 0, "attachment": 0}
        for r in self.all_requirements():
            counts[r.source_kind] = counts.get(r.source_kind, 0) + 1
        return counts


# --------------------------------------------------------------------------- #
class PlaygroundService:
    """Extraction plus the adapter into the existing RFQ/Phase 2 machinery."""

    def __init__(self, ai: AIService, repo: RFQRepository, settings: Optional[Settings] = None,
                 registry: Optional[DocumentExtractorRegistry] = None):
        self.ai = ai
        self.repo = repo
        self.settings = settings or Settings.from_env()
        self.registry = registry or DocumentExtractorRegistry(self.settings)

    # -------------------------------------------------------------- reading
    def read_attachments(self, paths: List[str]) -> Tuple[List[SourceFile], List[Dict[str, Any]]]:
        """Read each attachment with the existing Phase 2 reader. Never fakes a parse."""
        sources, payload = [], []
        for path in paths:
            content = self.registry.extract(path)
            src = SourceFile(
                filename=os.path.basename(path),
                media_type=content.media_type or self.registry.media_type_for(path),
                status=content.status, note=content.note,
                chars=len(content.text or ""), method=content.method)
            sources.append(src)
            if src.readable:
                payload.append({"filename": src.filename, "media_type": src.media_type,
                                "note": content.note, "text": content.text})
        return sources, payload

    # ------------------------------------------------------------ extraction
    def analyze(self, request_text: str, attachment_paths: Optional[List[str]] = None,
                on_stage: Optional[Callable[[str], None]] = None) -> PlaygroundResult:
        request_text = (request_text or "").strip()
        attachment_paths = attachment_paths or []
        if not request_text and not attachment_paths:
            raise PlaygroundError("Paste a requirement or attach a file before analyzing.")

        def stage(i):
            if on_stage:
                on_stage(STAGES[i])

        stage(0)
        sources, payload = self.read_attachments(attachment_paths)
        unreadable = [s for s in sources if not s.readable]
        if not request_text and not payload:
            detail = "; ".join("%s: %s" % (s.filename, s.note or s.status.value) for s in unreadable)
            raise PlaygroundError(
                "None of the attachments could be read, and no request text was given. %s" % detail)

        stage(1)
        prompt = build_extraction_prompt(request_text, payload)
        try:
            data, calls = self._call(prompt, PLAYGROUND_EXTRACTION_SCHEMA, EXTRACTION_SYSTEM_PROMPT,
                                     "playground_extraction", PLAYGROUND_PROMPT_VERSION)
        except AIError as e:
            raise PlaygroundError(e.user_message)

        stage(2)
        haystacks = [request_text] + [d["text"] for d in payload]
        result = self._to_result(data, sources, request_text, haystacks)
        result.ai_calls = calls
        return result

    def _to_result(self, data: Dict[str, Any], sources: List[SourceFile], request_text: str,
                   haystacks: List[str]) -> PlaygroundResult:
        cls = data.get("classification") or {}
        result = PlaygroundResult(
            title=str(data.get("title") or "").strip(),
            product=str(cls.get("product") or "").strip(),
            category=str(cls.get("category") or "").strip(),
            product_type=str(cls.get("product_type") or "").strip(),
            summary=str(data.get("summary") or "").strip(),
            ambiguities=[str(a) for a in (data.get("ambiguities") or [])][:10],
            sources=sources,
            nothing_found_reason=str(data.get("nothing_found_reason") or "").strip(),
            request_text=request_text,
        )
        known = {s.filename for s in sources}
        result.shared_requirements = [self._requirement(r, haystacks, known)
                                      for r in (data.get("shared_requirements") or [])]
        for i, raw in enumerate(data.get("line_items") or []):
            qty = raw.get("quantity")
            try:
                qty = float(qty) if qty is not None else None
            except (TypeError, ValueError):
                qty = None
            li = PlaygroundLineItem(
                index=i,
                name=str(raw.get("name") or "").strip() or "Unnamed item",
                description=str(raw.get("description") or "").strip(),
                quantity=qty if (qty or 0) > 0 else None,
                unit=str(raw.get("unit") or "").strip(),
                specifications=[(str(s.get("name") or ""), str(s.get("value") or ""), s.get("unit"))
                                for s in (raw.get("specifications") or []) if s.get("name") and s.get("value")],
                requirements=[self._requirement(r, haystacks, known) for r in (raw.get("requirements") or [])],
                ambiguities=[str(a) for a in (raw.get("ambiguities") or [])][:6],
                evidence=str(raw.get("evidence") or ""),
            )
            result.line_items.append(li)
        return result

    @staticmethod
    def _requirement(raw: Dict[str, Any], haystacks: List[str], known_files: set) -> Requirement:
        quoted = str(raw.get("quoted_text") or "").strip()
        doc = str(raw.get("source_document") or "").strip()
        kind = str(raw.get("source_kind") or "email_text")
        # A document the user never supplied cannot be a source.
        if kind == "attachment" and doc and doc not in known_files:
            doc, kind = "", "email_text"
        conf = raw.get("confidence")
        try:
            conf = float(conf) if conf is not None else None
        except (TypeError, ValueError):
            conf = None
        return Requirement(
            field_key=guards.normalize_key(raw.get("field_key")) or "requirement",
            label=str(raw.get("label") or "").strip() or "Requirement",
            value=str(raw.get("value") or "").strip(),
            unit=(str(raw.get("unit") or "").strip() or None),
            status=str(raw.get("status") or "found"),
            source_kind=kind,
            source_document=doc,
            source_location=str(raw.get("source_location") or "").strip(),
            quoted_text=quoted,
            note=str(raw.get("note") or "").strip(),
            confidence=conf,
            verified=guards.evidence_supported(quoted, "\n".join(haystacks)) if quoted else False,
        )

    # --------------------------------------------------------------- adapter
    def to_rfq(self, result: PlaygroundResult, rfq_id: Optional[str] = None) -> RFQ:
        """Turn extracted requirements into a real RFQ.

        This is the only bridge the playground needs. Once the requirements are an ``RFQ``,
        Phase 1's completeness rules and Phase 2's supplier engine both apply unchanged.
        """
        if not result.found_anything:
            raise PlaygroundError(result.nothing_found_reason
                                  or "No line items were detected, so there is nothing to send on.")

        rfq = RFQ(id=rfq_id or new_id("rfq"), fields=new_field_set(), turn=1,
                  title=result.title or result.product or "Requirement test",
                  product=result.product, category=result.category, product_type=result.product_type,
                  classification_note=result.summary, status=RFQStatus.DRAFT)

        for i, li in enumerate(result.line_items, start=1):
            specs = [SpecAttr(name=n, value=v, unit=u) for n, v, u in li.specifications]
            # requirements that read as physical attributes belong on the line, so supplier
            # line matching has something concrete to match against
            for r in li.requirements:
                if _is_spec_like(r) and not any(guards.norm_text(s.name) == guards.norm_text(r.label) for s in specs):
                    specs.append(SpecAttr(name=r.label, value=r.value, unit=r.unit))
            rfq.line_items.append(LineItem(
                id="LINE-%03d" % i, product=li.name, description=li.description,
                specifications=specs, quantity=li.quantity, unit=li.unit or "pcs",
                source=Source.BUYER_EXPLICIT if li.evidence else Source.AI_RECOMMENDED,
                source_refs=["playground"], evidence=li.evidence or None))
        rfq.line_seq = len(rfq.line_items)

        # shared requirements, and any line requirement that maps to a universal field,
        # populate the RFQ field set that the completeness rules already understand
        for r in result.shared_requirements:
            self._apply_requirement(rfq, r)
        for li in result.line_items:
            for r in li.requirements:
                if r.field_key in FIELD_SPECS:
                    self._apply_requirement(rfq, r)

        guards.apply_quantity_semantics(rfq, 1)
        guards.derive_technical_summary(rfq, 1)
        rfq.completeness = guards.compute_completeness(rfq, None, 1)
        rfq.status = guards.next_status(rfq)
        return rfq

    @staticmethod
    def _apply_requirement(rfq: RFQ, r: Requirement) -> None:
        """Write one requirement onto the RFQ field set, preserving its provenance."""
        key = r.field_key
        spec = FIELD_SPECS.get(key)
        fv = rfq.fields.get(key)
        if fv is None:
            fv = FieldValue(
                key=key, label=r.label or key.replace("_", " ").capitalize(),
                section=spec.section if spec else Section.TECHNICAL,
                importance=spec.default_importance if spec else Importance.RECOMMENDED,
                value_kind=spec.value_kind if spec else ValueKind.TEXT)
            rfq.fields[key] = fv
        if fv.is_filled:
            return                                    # first statement wins; no silent overwrite
        parsed, kind = guards.parse_value(r.value, fv.value_kind)
        fv.value, fv.value_kind = parsed, kind
        fv.unit = r.unit or fv.unit
        fv.evidence = r.quoted_text or None
        fv.source_refs = ["playground:%s" % (r.source_document or "request text")]
        fv.confidence = r.confidence
        fv.updated_turn = 1
        if r.status == "ambiguous":
            # an ambiguity is not a fact: keep the buyer's words, but do not let it satisfy
            # a required field, and leave a question behind so the gap stays visible
            fv.status, fv.source = FieldStatus.RECOMMENDED, Source.AI_RECOMMENDED
            fv.note = r.note or "Stated ambiguously; a supplier could read this more than one way."
            rfq.questions.append(Question(
                id=new_id("q"), category=fv.section, field_key=key,
                question="What exactly is required for %s? The request says %r." % (fv.label.lower(), r.value),
                reason=r.note or "Ambiguous wording leads suppliers to quote different things.",
                importance=Importance.REQUIRED if (spec and spec.default_importance == Importance.REQUIRED)
                else Importance.RECOMMENDED,
                asked_turn=1))
        else:
            fv.status, fv.source = FieldStatus.PROVIDED, Source.BUYER_EXPLICIT

    # ------------------------------------------------- hand over to Phase 2
    def save_as_rfq(self, result: PlaygroundResult) -> RFQ:
        """Persist the extracted requirement as an RFQ so Phase 2 can work on it."""
        rfq = self.to_rfq(result, rfq_id=result.rfq_id)
        self.repo.save_rfq(rfq)
        result.rfq_id = rfq.id
        return rfq

    def draft_supplier_reply(self, rfq, line_item_id: Optional[str] = None) -> str:
        """Draft one plausible supplier reply as *editable text* for the composer.

        Purely a convenience so a demo need not be typed from scratch. Nothing is
        registered or extracted here: the text lands in the box, visible, and is then
        read by the real engine exactly like anything typed by hand.
        """
        line = next((li for li in rfq.line_items if li.id == line_item_id), None)
        items = [{"name": li.product, "description": li.description, "quantity": li.quantity,
                  "unit": li.unit,
                  "specifications": [{"name": s.name, "value": s.value, "unit": s.unit}
                                     for s in li.specifications]}
                 for li in ([line] if line else rfq.line_items)]
        prompt = build_simulation_prompt(rfq.product, rfq.category, items, [], supplier_count=1)
        try:
            data, _ = self._call(prompt, SIMULATED_RESPONSES_SCHEMA, SIMULATION_SYSTEM_PROMPT,
                                 "playground_draft", SIMULATION_PROMPT_VERSION)
        except AIError as e:
            raise PlaygroundError("Could not draft a sample reply: %s" % e.user_message)
        for raw in data.get("suppliers") or []:
            text = str(raw.get("document_text") or "").strip()
            if text:
                return text
        raise PlaygroundError("No usable draft was produced. Try again.")

    # ------------------------------------------------------------ internals
    def _call(self, prompt: str, schema: Dict[str, Any], system: str, call_type: str,
              version: str) -> Tuple[Dict[str, Any], List[AICallRecord]]:
        """One structured call with a single corrective retry, audited like every other."""
        attempt, records = prompt, []
        last: Optional[AIError] = None
        for _ in range(2):
            started = utc_now()
            try:
                res: AIResult = self.ai.complete_json(attempt, schema, system, tier="quality")
                records.append(self._record(call_type, version, attempt, res, None, started))
                return res.data, records
            except AIInvalidOutput as e:
                last = e
                records.append(self._record(call_type, version, attempt, None, e, started))
                attempt = prompt + ("\n\nYOUR PREVIOUS OUTPUT FAILED VALIDATION: %s\n"
                                    "Return a corrected JSON object matching the schema exactly." % str(e)[:400])
            except AIError as e:
                records.append(self._record(call_type, version, attempt, None, e, started))
                raise
        assert last is not None
        raise last

    def _record(self, call_type: str, version: str, prompt: str, res: Optional[AIResult],
                err: Optional[AIError], started: str) -> AICallRecord:
        return AICallRecord(
            id=new_id("call"), rfq_id=None, turn=0, call_type=call_type,
            provider=getattr(self.ai, "name", "unknown"),
            model=res.model if res else getattr(self.ai, "model_for", lambda t: "?")("quality"),
            prompt_version=version, prompt_hash=hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16],
            prompt_chars=len(prompt), duration_ms=res.duration_ms if res else 0,
            ok=res is not None, schema_valid=bool(res and res.schema_valid),
            error=("%s: %s" % (type(err).__name__, err)) if err else None,
            raw_response=(res.raw if res else (getattr(err, "raw", "") or ""))[:100000],
            prompt_text=prompt if self.settings.log_prompts else None, created_at=started)


# --------------------------------------------------------------------------- #
_SPEC_WORDS = re.compile(
    r"dimension|size|thick|material|grade|finish|tolerance|colou?r|weight|length|width|height|"
    r"diameter|gsm|flute|ply|coating|load|voltage|wattage|capacity|fabric", re.I)


def _is_spec_like(r: Requirement) -> bool:
    """Physical attributes belong on the line item, where line matching can use them."""
    if r.field_key in FIELD_SPECS:
        return False
    return bool(_SPEC_WORDS.search(r.field_key) or _SPEC_WORDS.search(r.label))
