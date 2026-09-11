"""RFQService — orchestration layer between the UI and the AI + guards + DB.

One AI call per buyer turn. Buyer input is persisted *before* the AI runs so a
failed call never loses anything; a failed turn can be retried.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple

from . import guards
from .ai_schemas import PING_SCHEMA, TURN_OUTPUT_SCHEMA
from .ai_service import AIError, AIInvalidOutput, AIResult, AIService, AITransient
from .config import Settings
from .fields import FIELD_SPECS, new_field_set
from .persistence import RFQRepository
from .prompts import (
    PROMPT_VERSION, SYSTEM_PROMPT, build_first_turn_prompt, build_ping_prompt, build_turn_prompt,
    render_turn_display, render_turn_text,
)
from .schema import (
    RFQ, AICallRecord, FieldStatus, FieldValue, Importance, LineItem, Message, MessageRole, QuestionStatus,
    RFQStatus, RFQSummary, Section, Source, SpecAttr, ValueKind, new_id, utc_now,
)


class RFQStateError(Exception):
    pass


class RFQService:
    MAX_ATTEMPTS = 3   # first try + one transient/validation retry each

    def __init__(self, repo: RFQRepository, ai: AIService, settings: Settings):
        self.repo = repo
        self.ai = ai
        self.settings = settings

    # ------------------------------------------------------------------ reads
    def get(self, rfq_id: str) -> RFQ:
        rfq = self.repo.get_rfq(rfq_id)
        if rfq is None:
            raise RFQStateError("RFQ %s not found" % rfq_id)
        return rfq

    def list_rfqs(self) -> List[RFQSummary]:
        return self.repo.list_rfqs()

    def delete(self, rfq_id: str) -> None:
        self.repo.delete_rfq(rfq_id)

    def transcript(self, rfq_id: str) -> List[Message]:
        return self.repo.list_messages(rfq_id)

    def ai_calls(self, rfq_id: str) -> List[AICallRecord]:
        return self.repo.list_ai_calls(rfq_id)

    def export_json(self, rfq_id: str) -> str:
        rfq = self.get(rfq_id)
        return json.dumps({
            "rfq": rfq.to_dict(),
            "transcript": [m.to_dict() for m in self.transcript(rfq_id)],
            "exported_at": utc_now(),
        }, ensure_ascii=False, indent=2)

    def evidence_for(self, rfq: RFQ, refs: List[str]) -> List[Message]:
        out: List[Message] = []
        for r in refs:
            if r.startswith("msg:"):
                m = self.repo.get_message(r[4:])
                if m:
                    out.append(m)
        return out

    def health(self) -> Dict[str, Any]:
        return self.ai.health()

    # --------------------------------------------------------------- creation
    def start_rfq(self, buyer_text: str) -> RFQ:
        """Create the RFQ, persist the buyer's request, then run the first AI turn."""
        text = (buyer_text or "").strip()
        if not text:
            raise RFQStateError("Please describe what you want to source.")
        rfq = RFQ(id=new_id("rfq"), fields=new_field_set(), status=RFQStatus.DRAFT, turn=0)
        rfq.title = text[:70]
        self.repo.save_rfq(rfq)
        msg = Message(id=new_id("msg"), rfq_id=rfq.id, turn=1, role=MessageRole.BUYER, kind="request", content=text,
                      payload={"text": text})
        self.repo.add_message(msg)
        return self._run_first_turn(rfq, msg)

    def _run_first_turn(self, rfq: RFQ, msg: Message) -> RFQ:
        prompt = build_first_turn_prompt(msg.content)
        out = self._call_ai("first_turn", prompt, rfq, turn=1, turn_text=msg.content)
        rfq.turn = 1
        self._apply_turn_output(rfq, out, msg, turn_text=msg.content, is_first=True)
        self.repo.save_rfq(rfq)
        return rfq

    # ------------------------------------------------------------------ turns
    def submit_turn(self, rfq_id: str, answers: Dict[str, str], skipped: List[str], free_text: str) -> RFQ:
        rfq = self.get(rfq_id)
        if rfq.status == RFQStatus.SUPPLIER_READY:
            raise RFQStateError("This RFQ is marked supplier-ready. Reopen it to make changes.")
        answers = {k: str(v).strip() for k, v in (answers or {}).items() if str(v or "").strip()}
        skipped = [s for s in (skipped or []) if s and s not in answers]
        free_text = (free_text or "").strip()
        if not answers and not skipped and not free_text:
            raise RFQStateError("Answer a question, skip one, or tell me something in your own words.")

        turn = rfq.turn + 1
        # persist buyer input first
        for qid, text in answers.items():
            q = rfq.question(qid)
            if q is not None and q.status == QuestionStatus.OPEN:
                q.status, q.resolution, q.answer, q.answered_turn = QuestionStatus.ANSWERED, "answered", text, turn
        for qid in skipped:
            q = rfq.question(qid)
            if q is not None and q.status == QuestionStatus.OPEN:
                q.status, q.resolution, q.answered_turn = QuestionStatus.SKIPPED, "skipped", turn
        turn_text = render_turn_text(rfq, answers, skipped, free_text)
        msg = Message(id=new_id("msg"), rfq_id=rfq.id, turn=turn, role=MessageRole.BUYER, kind="answers",
                      content=render_turn_display(rfq, answers, skipped, free_text),
                      payload={"answers": answers, "skipped": skipped, "free_text": free_text, "turn_text": turn_text})
        for qid in answers:
            q = rfq.question(qid)
            if q is not None:
                q.answer_ref = "msg:%s" % msg.id
        self.repo.add_message(msg)
        rfq.turn = turn
        self.repo.save_rfq(rfq)

        prompt = build_turn_prompt(rfq, answers, skipped, free_text)
        out = self._call_ai("turn", prompt, rfq, turn=turn, turn_text=turn_text or msg.content)
        self._apply_turn_output(rfq, out, msg, turn_text=turn_text or msg.content, is_first=False)
        self.repo.save_rfq(rfq)
        return rfq

    def retry_last_turn(self, rfq_id: str) -> RFQ:
        """Re-run the AI for the most recent buyer message (after a failure)."""
        rfq = self.get(rfq_id)
        buyer_msgs = [m for m in self.transcript(rfq_id) if m.role == MessageRole.BUYER]
        if not buyer_msgs:
            raise RFQStateError("Nothing to retry.")
        msg = buyer_msgs[-1]
        if msg.kind == "request" and rfq.turn == 0:
            return self._run_first_turn(rfq, msg)
        p = msg.payload or {}
        answers, skipped, free_text = dict(p.get("answers") or {}), list(p.get("skipped") or []), str(p.get("free_text") or "")
        prompt = build_turn_prompt(rfq, answers, skipped, free_text)
        replay_text = str(p.get("turn_text") or msg.content)
        out = self._call_ai("turn", prompt, rfq, turn=rfq.turn, turn_text=replay_text)
        self._apply_turn_output(rfq, out, msg, turn_text=replay_text, is_first=False)
        self.repo.save_rfq(rfq)
        return rfq

    def has_pending_turn(self, rfq: RFQ) -> bool:
        """True when the last buyer message has no assistant reply yet (AI failed)."""
        msgs = self.transcript(rfq.id)
        if not msgs:
            return False
        return msgs[-1].role == MessageRole.BUYER

    # ----------------------------------------------------------- manual edits
    def recompute(self, rfq_id: str) -> RFQ:
        """Re-run the deterministic layer only (no AI). Used after external edits or a rules change."""
        rfq = self.get(rfq_id)
        self._recompute(rfq)
        self.repo.save_rfq(rfq)
        return rfq

    def set_field(self, rfq_id: str, key: str, value: Any, unit: Optional[str] = None, note: Optional[str] = None) -> RFQ:
        rfq = self.get(rfq_id)
        self._assert_editable(rfq)
        key = guards.normalize_key(key)
        fv = rfq.fields.get(key)
        if fv is None:
            spec = FIELD_SPECS.get(key)
            fv = FieldValue(key=key, label=spec.label if spec else key.replace("_", " ").capitalize(),
                            section=spec.section if spec else Section.TECHNICAL)
            rfq.fields[key] = fv
        turn = rfq.turn
        old = fv.display_value() or fv.display_status()
        if fv.value not in (None, "", []) or fv.status in (FieldStatus.CONFLICT, FieldStatus.UNKNOWN, FieldStatus.RECOMMENDED):
            fv.history.append(guards._snapshot(fv, "manual edit", turn))
        parsed, kind = guards.parse_value(value, fv.value_kind)
        if parsed in (None, "", []):
            fv.value, fv.status, fv.source, fv.evidence, fv.conflict_values = None, FieldStatus.MISSING, Source.MISSING, None, []
        else:
            fv.value, fv.value_kind = parsed, kind
            fv.unit = (unit or "").strip() or fv.unit
            fv.status, fv.source, fv.evidence, fv.confidence, fv.conflict_values = FieldStatus.PROVIDED, Source.MANUAL_EDIT, None, 1.0, []
            if fv.importance == Importance.NOT_APPLICABLE:
                spec = FIELD_SPECS.get(key)
                fv.importance = spec.default_importance if spec else Importance.RECOMMENDED
        if "manual" not in fv.source_refs:
            fv.source_refs.append("manual")
        if note:
            fv.note = note
        fv.updated_turn = turn
        self._record_manual_edit(rfq, "Edited %s: %s → %s" % (fv.label, old or "—", fv.display_value() or "cleared"),
                                 {"key": key, "value": fv.value, "unit": fv.unit})
        self._recompute(rfq)
        self.repo.save_rfq(rfq)
        return rfq

    def set_field_applicability(self, rfq_id: str, key: str, importance: Importance, reason: str = "") -> RFQ:
        rfq = self.get(rfq_id)
        self._assert_editable(rfq)
        fv = rfq.fields.get(guards.normalize_key(key))
        if fv is None:
            raise RFQStateError("Unknown field %s" % key)
        if importance == Importance.NOT_APPLICABLE:
            if fv.value not in (None, "", []):
                fv.history.append(guards._snapshot(fv, "buyer marked not applicable", rfq.turn))
            fv.value, fv.status, fv.source, fv.conflict_values = None, FieldStatus.NOT_APPLICABLE, Source.MANUAL_EDIT, []
            fv.note = reason or "Marked not applicable by buyer."
        else:
            if fv.status == FieldStatus.NOT_APPLICABLE:
                fv.status, fv.source, fv.note = FieldStatus.MISSING, Source.MISSING, None
        fv.importance = importance
        if "manual" not in fv.source_refs:
            fv.source_refs.append("manual")
        self._record_manual_edit(rfq, "Set %s to %s" % (fv.label, importance.value.replace("_", " ")), {"key": fv.key, "importance": importance.value})
        self._recompute(rfq)
        self.repo.save_rfq(rfq)
        return rfq

    def replace_line_items(self, rfq_id: str, rows: List[Dict[str, Any]]) -> RFQ:
        """Apply edits from the review table. Rows carry id (may be blank for new), product, spec text, quantity, unit, date."""
        rfq = self.get(rfq_id)
        self._assert_editable(rfq)
        old = {li.id: li for li in rfq.line_items}
        new_items: List[LineItem] = []
        for r in rows:
            product = str(r.get("product") or "").strip()
            if not product:
                continue
            lid = str(r.get("id") or "").strip()
            prev = old.get(lid)
            qty = r.get("quantity")
            try:
                qty = float(qty) if qty not in (None, "") and float(qty) > 0 else None
            except (TypeError, ValueError):
                qty = None
            spec_text = str(r.get("specifications") or "").strip()
            specs = prev.specifications if (prev and spec_text == prev.spec_summary()) else _parse_spec_text(spec_text)
            date = str(r.get("required_date") or "").strip() or None
            unit = str(r.get("unit") or (prev.unit if prev else "pcs")).strip() or "pcs"
            price = r.get("target_price")
            try:
                price = float(price) if price not in (None, "") and float(price) > 0 else None
            except (TypeError, ValueError):
                price = None
            li = LineItem(id=lid if prev else "", product=product, description=str(r.get("description") or (prev.description if prev else "")),
                          specifications=specs, quantity=qty, unit=unit, target_price=price, required_date=date,
                          source=Source.MANUAL_EDIT, source_refs=(prev.source_refs if prev else []) + ["manual"],
                          evidence=prev.evidence if prev else None, history=list(prev.history) if prev else [])
            if prev:
                changes = {}
                for f_ in ("product", "quantity", "unit", "target_price", "required_date"):
                    if getattr(prev, f_) != getattr(li, f_):
                        changes[f_] = {"old": getattr(prev, f_), "new": getattr(li, f_)}
                if prev.spec_summary() != li.spec_summary():
                    changes["specifications"] = {"old": prev.spec_summary(), "new": li.spec_summary()}
                if changes:
                    li.history.append({"turn": rfq.turn, "manual": True, "changes": changes})
                else:
                    li.source = prev.source
                    li.source_refs = prev.source_refs
            new_items.append(li)
        # assign ids to new rows
        rfq.line_items = new_items
        n = 0
        for li in rfq.line_items:
            if li.id:
                try:
                    n = max(n, int(li.id.split("-")[-1]))
                except ValueError:
                    pass
        for li in rfq.line_items:
            if not li.id:
                n += 1
                li.id = "LINE-%03d" % n
        removed = [lid for lid in old if lid not in {li.id for li in rfq.line_items}]
        self._record_manual_edit(rfq, "Edited line items (%d rows%s)" % (len(rfq.line_items), (", removed %s" % ", ".join(removed)) if removed else ""),
                                 {"removed": removed, "rows": [li.to_dict() for li in rfq.line_items]})
        self._recompute(rfq)
        self.repo.save_rfq(rfq)
        return rfq

    def dismiss_question(self, rfq_id: str, question_id: str) -> RFQ:
        rfq = self.get(rfq_id)
        q = rfq.question(question_id)
        if q is not None and q.status == QuestionStatus.OPEN:
            q.status, q.resolution, q.answered_turn = QuestionStatus.SKIPPED, "skipped", rfq.turn
            self._recompute(rfq)
            self.repo.save_rfq(rfq)
        return rfq

    # ----------------------------------------------------------- transitions
    def mark_supplier_ready(self, rfq_id: str) -> RFQ:
        rfq = self.get(rfq_id)
        if not rfq.completeness.ready_to_send:
            raise RFQStateError("The RFQ is not ready: %s" % rfq.completeness.explanation)
        rfq.status = RFQStatus.SUPPLIER_READY
        self.repo.add_message(Message(id=new_id("msg"), rfq_id=rfq.id, turn=rfq.turn, role=MessageRole.SYSTEM, kind="status",
                                      content="Marked supplier-ready."))
        self.repo.save_rfq(rfq)
        return rfq

    def reopen(self, rfq_id: str) -> RFQ:
        rfq = self.get(rfq_id)
        if rfq.status == RFQStatus.SUPPLIER_READY:
            rfq.status = RFQStatus.IN_PROGRESS
            self.repo.add_message(Message(id=new_id("msg"), rfq_id=rfq.id, turn=rfq.turn, role=MessageRole.SYSTEM, kind="status",
                                          content="Reopened for editing."))
            self._recompute(rfq)
            self.repo.save_rfq(rfq)
        return rfq

    def ping(self) -> AIResult:
        return self.ai.complete_json(build_ping_prompt(), PING_SCHEMA, "Reply with JSON only.", tier="fast")

    # -------------------------------------------------------------- internals
    def _assert_editable(self, rfq: RFQ) -> None:
        if rfq.status == RFQStatus.SUPPLIER_READY:
            raise RFQStateError("This RFQ is marked supplier-ready. Reopen it to make changes.")

    def _record_manual_edit(self, rfq: RFQ, text: str, payload: Dict[str, Any]) -> None:
        self.repo.add_message(Message(id=new_id("msg"), rfq_id=rfq.id, turn=rfq.turn, role=MessageRole.BUYER, kind="manual_edit",
                                      content=text, payload=payload))

    def _prior_buyer_texts(self, rfq: RFQ, exclude_msg_id: str) -> List[Tuple[str, str]]:
        out: List[Tuple[str, str]] = []
        for m in self.repo.list_messages(rfq.id):
            if m.role != MessageRole.BUYER or m.id == exclude_msg_id or m.kind == "manual_edit":
                continue
            text = str((m.payload or {}).get("turn_text") or m.content)
            out.append(("msg:%s" % m.id, text))
        out.reverse()  # most recent first
        return out

    def _recompute(self, rfq: RFQ) -> None:
        guards.derive_technical_summary(rfq, rfq.turn)
        guards.apply_quantity_semantics(rfq, rfq.turn)
        guards.reconcile_questions(rfq, [], [], "", "manual", rfq.turn, False, self.settings)
        rfq.completeness = guards.compute_completeness(rfq, {"score": rfq.completeness.ai_score,
                                                             "ready_to_send": rfq.completeness.ai_ready_claim,
                                                             "explanation": rfq.completeness.ai_explanation,
                                                             "open_ambiguities": rfq.completeness.open_ambiguities}, rfq.turn)
        rfq.status = guards.next_status(rfq)

    def _apply_turn_output(self, rfq: RFQ, out: Dict[str, Any], msg: Message, turn_text: str, is_first: bool) -> None:
        turn = rfq.turn
        turn_ref = "msg:%s" % msg.id
        prior = self._prior_buyer_texts(rfq, exclude_msg_id=msg.id)
        audit: List[str] = []

        cls = out.get("classification") or {}
        product = (cls.get("product") or "").strip()
        if product and (is_first or not rfq.product or guards.norm_text(product) != guards.norm_text(rfq.product)):
            if rfq.product and guards.norm_text(product) != guards.norm_text(rfq.product):
                audit.append("classification changed: %s → %s" % (rfq.product, product))
            rfq.product = product
            rfq.category = (cls.get("category") or rfq.category).strip()
            rfq.product_type = (cls.get("product_type") or rfq.product_type).strip()
            rfq.classification_note = (cls.get("note") or "").strip()
        title = (out.get("title") or "").strip()
        if title and (is_first or not rfq.title or rfq.title == rfq.product):
            rfq.title = title

        li = out.get("line_items") or {}
        audit += guards.merge_line_items(rfq, li.get("mode") or "none", li.get("items") or [], turn_text, turn_ref, turn, prior)
        audit += guards.apply_field_updates(rfq, out.get("field_updates") or [], turn_text, turn_ref, turn, prior)
        audit += guards.apply_applicability(rfq, out.get("applicability_updates") or [])
        audit += guards.apply_quantity_semantics(rfq, turn)
        guards.derive_technical_summary(rfq, turn)
        audit += guards.reconcile_questions(rfq, out.get("answered_questions") or [], out.get("new_questions") or [],
                                            turn_text, turn_ref, turn, is_first, self.settings)
        rfq.completeness = guards.compute_completeness(rfq, out.get("completeness") or {}, turn)
        rfq.status = guards.next_status(rfq)

        reply = (out.get("assistant_message") or "").strip() or "Thanks — I've updated the RFQ."
        self.repo.add_message(Message(id=new_id("msg"), rfq_id=rfq.id, turn=turn, role=MessageRole.ASSISTANT, kind="assistant",
                                      content=reply, payload={"ai_ready_claim": (out.get("completeness") or {}).get("ready_to_send")}))
        if audit:
            self.repo.add_message(Message(id=new_id("msg"), rfq_id=rfq.id, turn=turn, role=MessageRole.SYSTEM, kind="guards",
                                          content="\n".join(audit), payload={"notes": audit}))

    @staticmethod
    def _is_degenerate(out: Dict[str, Any], turn_text: str) -> bool:
        """A substantial buyer turn that yields no extraction, no questions and no answers is
        not an analysis. The CLI emits placeholder objects when it exhausts its structured-output
        attempts, and those must never be applied to an RFQ."""
        if len((turn_text or "").strip()) < 40:
            return False
        li = out.get("line_items") or {}
        produced = (len(out.get("field_updates") or []) + len(out.get("applicability_updates") or [])
                    + len(li.get("items") or []) + len(out.get("answered_questions") or [])
                    + len(out.get("new_questions") or []))
        if produced > 0:
            return False
        msg = (out.get("assistant_message") or "").strip()
        return len(msg) < 25 or msg.lower() in ("test", "ok", "done", "n/a")

    def _call_ai(self, call_type: str, prompt: str, rfq: RFQ, turn: int, turn_text: str = "") -> Dict[str, Any]:
        """One structured call; retry once on a dropped connection or invalid output. Always audited."""
        attempt_prompt = prompt
        last_err: Optional[AIError] = None
        for attempt in range(self.MAX_ATTEMPTS):
            started = utc_now()
            try:
                res = self.ai.complete_json(attempt_prompt, TURN_OUTPUT_SCHEMA, SYSTEM_PROMPT, tier="quality")
                if self._is_degenerate(res.data, turn_text):
                    err = AIInvalidOutput("The model returned an empty analysis of a non-empty buyer turn.", raw=res.raw)
                    self._log_call(call_type, attempt_prompt, rfq, turn, res, err, started)
                    last_err = err
                    attempt_prompt = prompt + (
                        "\n\nPREVIOUS ATTEMPT RETURNED AN EMPTY RESULT. The buyer's text above contains real information. "
                        "Extract every fact it states into field_updates and line_items with verbatim evidence, map it onto the open "
                        "questions via answered_questions, and write a real assistant_message. Placeholder values are not acceptable.")
                    continue
                self._log_call(call_type, attempt_prompt, rfq, turn, res, None, started)
                return res.data
            except AITransient as e:
                last_err = e
                self._log_call(call_type, attempt_prompt, rfq, turn, None, e, started)
                continue  # same prompt: the model never got to answer
            except AIInvalidOutput as e:
                last_err = e
                self._log_call(call_type, attempt_prompt, rfq, turn, None, e, started)
                attempt_prompt = prompt + "\n\nPREVIOUS ATTEMPT FAILED VALIDATION: %s\nReturn a corrected JSON object that matches the schema exactly." % str(e)[:400]
                continue
            except AIError as e:
                self._log_call(call_type, attempt_prompt, rfq, turn, None, e, started)
                raise
        assert last_err is not None
        raise last_err

    def _log_call(self, call_type: str, prompt: str, rfq: RFQ, turn: int, res: Optional[AIResult], err: Optional[AIError], started: str) -> None:
        rec = AICallRecord(
            id=new_id("call"), rfq_id=rfq.id, turn=turn, call_type=call_type,
            provider=getattr(self.ai, "name", "unknown"), model=(res.model if res else getattr(self.ai, "model_for", lambda t: "?")("quality")),
            prompt_version=PROMPT_VERSION, prompt_hash=hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16],
            prompt_chars=len(prompt), duration_ms=res.duration_ms if res else 0, ok=(res is not None and err is None),
            schema_valid=bool(res and res.schema_valid), error=(type(err).__name__ + ": " + str(err)) if err else None,
            raw_response=(res.raw if res else (err.raw if err else ""))[:200000],
            prompt_text=prompt if self.settings.log_prompts else None, created_at=started,
        )
        try:
            self.repo.add_ai_call(rec)
        except Exception:  # pragma: no cover - audit must never break the flow
            pass


def _parse_spec_text(text: str) -> List[SpecAttr]:
    """'Dimensions: 10 x 10 x 5 in; Flute: B' → SpecAttr list. Free text becomes one 'Specification' attribute."""
    out: List[SpecAttr] = []
    if not text:
        return out
    for chunk in [c.strip() for c in text.split(";") if c.strip()]:
        if ":" in chunk:
            name, value = chunk.split(":", 1)
            out.append(SpecAttr(name=name.strip(), value=value.strip()))
        else:
            out.append(SpecAttr(name="Specification", value=chunk))
    return out
