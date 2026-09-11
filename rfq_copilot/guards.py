"""Deterministic guards that make AI output trustworthy.

The AI decides *what matters and what the buyer said*; this module decides
*what is allowed to change*:

* evidence guard      – buyer_explicit claims need a verifiable verbatim span
* overwrite guard     – recommendations never overwrite buyer facts
* conflict guard      – contradictory explicit values become CONFLICT, never guessed
* N/A guard           – not_applicable needs a justification, never applied to buyer facts
* line-item guards    – no accidental merging, stable IDs, per-line quantities
* question guards     – dedupe (incl. rephrasings), never re-ask, bounded counts
* readiness           – deterministic completeness score and ready_to_send

All functions are pure with respect to I/O: they mutate the RFQ in memory and
return a list of human-readable audit notes.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import Settings
from .fields import FIELD_SPECS
from .schema import (
    RFQ, AnswerType, Completeness, FieldStatus, FieldValue, Importance, LineItem, Question,
    QuestionStatus, RFQStatus, Section, Source, SpecAttr, ValueKind, new_id,
)

# --------------------------------------------------------------------------- #
# Text normalisation / evidence
# --------------------------------------------------------------------------- #
_WS = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^0-9a-z]+")
_STOP = {
    "the", "a", "an", "of", "for", "to", "in", "on", "is", "are", "be", "do", "you", "your", "what", "which", "how",
    "should", "we", "need", "needed", "any", "please", "each", "per", "and", "or", "with", "this", "that", "these",
    "those", "it", "its", "there", "will", "would", "can", "could", "have", "has", "does", "did", "want", "like",
    "require", "required", "requirement", "requirements", "specify", "provide", "tell", "me", "us", "about", "at",
    "by", "from", "into", "as", "if", "so", "than", "then", "also", "e.g", "eg", "etc",
}


def normalize_key(key: Optional[str]) -> str:
    k = (key or "").strip().lower().replace("-", "_").replace(" ", "_")
    k = re.sub(r"[^a-z0-9_]", "", k)
    k = re.sub(r"_+", "_", k).strip("_")
    return k


def norm_text(s: Optional[str]) -> str:
    """Casefold, unify multiplication signs and units spacing, drop punctuation."""
    t = (s or "").casefold().replace("×", "x").replace("*", "x").replace("’", "'")
    t = _NON_ALNUM.sub(" ", t)
    return _WS.sub(" ", t).strip()


def _squash(s: str) -> str:
    return norm_text(s).replace(" ", "")


def content_tokens(s: Optional[str]) -> set:
    toks = set()
    for t in norm_text(s).split(" "):
        if len(t) < 2 or t in _STOP:
            continue
        if len(t) > 4 and t.endswith("s"):
            t = t[:-1]
        toks.add(t)
    return toks


def similarity(a: str, b: str) -> float:
    ta, tb = content_tokens(a), content_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / float(len(ta | tb))


def evidence_supported(evidence: Optional[str], text: Optional[str], threshold: float = 0.8) -> bool:
    """True when the evidence span is (nearly) verbatim present in ``text``."""
    if not evidence or not text:
        return False
    ev, tx = norm_text(evidence), norm_text(text)
    if not ev:
        return False
    if ev in tx or _squash(evidence) in _squash(text):
        return True
    ev_tokens = [t for t in ev.split(" ") if t]
    if len(ev_tokens) < 2:
        return False
    hits = sum(1 for t in ev_tokens if t in tx.split(" ") or t in _squash(text))
    return hits / float(len(ev_tokens)) >= threshold


def find_evidence_ref(evidence: Optional[str], turn_text: str, turn_ref: str,
                      prior: Sequence[Tuple[str, str]] = (), threshold: float = 0.8) -> Optional[str]:
    """Return the message ref whose text supports the evidence (current turn first)."""
    if evidence_supported(evidence, turn_text, threshold):
        return turn_ref
    for ref, text in prior:
        if evidence_supported(evidence, text, threshold):
            return ref
    return None


# --------------------------------------------------------------------------- #
# Value parsing
# --------------------------------------------------------------------------- #
_NUM_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")
_TRUE = {"yes", "y", "true", "required", "needed"}
_FALSE = {"no", "n", "false", "none", "not required", "not needed"}


def parse_value(value: Any, kind: ValueKind) -> Tuple[Any, ValueKind]:
    if value is None:
        return None, kind
    if isinstance(value, (int, float)) and kind == ValueKind.NUMBER:
        return float(value), kind
    text = str(value).strip()
    if not text:
        return None, kind
    if kind == ValueKind.NUMBER:
        m = _NUM_RE.search(text.replace(" ", ""))
        if m:
            try:
                return float(m.group(0).replace(",", "")), ValueKind.NUMBER
            except ValueError:
                pass
        return text, ValueKind.TEXT
    if kind == ValueKind.BOOLEAN:
        low = text.casefold()
        if low in _TRUE:
            return True, kind
        if low in _FALSE:
            return False, kind
        return text, ValueKind.TEXT
    if kind == ValueKind.LIST:
        parts = [p.strip() for p in re.split(r"[;,\n]| and ", text) if p.strip()]
        return parts or None, kind
    return text, kind


def _strip_trailing_unit(value: str, unit: str) -> str:
    """'8,000 units' + unit 'units' -> '8,000', so the UI never prints the unit twice."""
    v, u = value.strip(), unit.strip()
    if u and v.lower().endswith(u.lower()) and len(v) > len(u):
        trimmed = v[: -len(u)].strip(" ,")
        if trimmed:
            return trimmed
    return v


def values_equal(a: Any, b: Any) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) < 1e-9
    if isinstance(a, list) and isinstance(b, list):
        return [norm_text(str(x)) for x in a] == [norm_text(str(x)) for x in b]
    return norm_text(str(a)) == norm_text(str(b))


def _rank(imp: Importance) -> int:
    return {Importance.REQUIRED: 0, Importance.RECOMMENDED: 1, Importance.OPTIONAL: 2, Importance.NOT_APPLICABLE: 3}[imp]


def _enum_or(cls, v, default):
    try:
        return cls(v)
    except Exception:
        return default


# --------------------------------------------------------------------------- #
# Field updates
# --------------------------------------------------------------------------- #
def _snapshot(fv: FieldValue, reason: str, turn: int) -> Dict[str, Any]:
    return {
        "value": fv.value, "unit": fv.unit, "status": fv.status.value, "source": fv.source.value,
        "evidence": fv.evidence, "source_refs": list(fv.source_refs), "turn": fv.updated_turn,
        "superseded_turn": turn, "reason": reason,
    }


def _set_buyer_value(fv: FieldValue, parsed: Any, kind: ValueKind, unit: Optional[str], evidence: Optional[str],
                     ref: Optional[str], note: Optional[str], turn: int, source: Source = Source.BUYER_EXPLICIT) -> None:
    fv.value = parsed
    fv.value_kind = kind
    if unit:
        fv.unit = unit
    fv.status = FieldStatus.PROVIDED
    fv.source = source
    fv.evidence = evidence
    if ref and ref not in fv.source_refs:
        fv.source_refs.append(ref)
    fv.confidence = 1.0
    if note:
        fv.note = note
    fv.conflict_values = []
    fv.updated_turn = turn


def apply_field_updates(rfq: RFQ, updates: List[Dict[str, Any]], turn_text: str, turn_ref: str, turn: int,
                        prior: Sequence[Tuple[str, str]] = ()) -> List[str]:
    audit: List[str] = []
    for u in updates or []:
        key = normalize_key(u.get("key"))
        if not key:
            continue
        spec = FIELD_SPECS.get(key)
        fv = rfq.fields.get(key)
        if fv is None:
            fv = FieldValue(
                key=key,
                label=(u.get("label") or (spec.label if spec else key.replace("_", " ").capitalize())).strip(),
                section=(spec.section if spec else _enum_or(Section, u.get("section"), Section.TECHNICAL)),
                value_kind=_enum_or(ValueKind, u.get("value_kind"), ValueKind.TEXT),
                importance=(spec.default_importance if spec else _enum_or(Importance, u.get("importance"), Importance.RECOMMENDED)),
                status=FieldStatus.MISSING,
                source=Source.MISSING,
            )
            rfq.fields[key] = fv
            audit.append("field %s created by AI (%s)" % (key, fv.section.value))

        source = u.get("source")
        status = u.get("status") or "provided"
        evidence = u.get("evidence")
        note = (u.get("note") or "").strip() or None
        # For a universal field the registry is authoritative about the type: the model sometimes
        # sends a numeric field as text ("8,000 units"), which must still store a number.
        kind = spec.value_kind if spec is not None else _enum_or(ValueKind, u.get("value_kind"), fv.value_kind)
        unit = (u.get("unit") or "").strip() or None
        parsed, kind = parse_value(u.get("value"), kind)
        if isinstance(parsed, str) and unit:
            parsed = _strip_trailing_unit(parsed, unit)

        # ---- evidence guard --------------------------------------------------
        ref: Optional[str] = None
        if source == "buyer_explicit":
            ref = find_evidence_ref(evidence, turn_text, turn_ref, prior)
            if ref is None:
                if status in ("unknown", "conflict"):
                    audit.append("field %s: %s claim ignored — evidence %r not found in buyer text" % (key, status, evidence))
                    continue
                audit.append("field %s: buyer_explicit downgraded to recommendation — evidence %r not found in buyer text" % (key, evidence))
                source, status = "ai_recommended", "recommended"
                note = ((note + " ") if note else "") + "(unverified: no matching buyer evidence)"

        # ---- recommendation path (overwrite guard) ------------------------
        if source != "buyer_explicit" or status == "recommended":
            if fv.is_filled or fv.status in (FieldStatus.UNKNOWN, FieldStatus.CONFLICT):
                audit.append("field %s: recommendation skipped — buyer fact present" % key)
                continue
            if fv.status == FieldStatus.NOT_APPLICABLE:
                audit.append("field %s: recommendation skipped — field is not applicable" % key)
                continue
            fv.status = FieldStatus.RECOMMENDED
            fv.source = Source.AI_RECOMMENDED
            fv.value = parsed
            fv.value_kind = kind
            fv.unit = unit or fv.unit
            fv.note = note or fv.note
            fv.evidence = None
            fv.confidence = None
            fv.updated_turn = turn
            continue

        # ---- buyer-explicit, verified -------------------------------------
        if status == "unknown":
            if fv.is_filled:
                fv.history.append(_snapshot(fv, "buyer now says unknown", turn))
            fv.status = FieldStatus.UNKNOWN
            fv.source = Source.BUYER_EXPLICIT
            fv.value = None
            fv.evidence = evidence
            if ref and ref not in fv.source_refs:
                fv.source_refs.append(ref)
            fv.note = note or fv.note
            fv.updated_turn = turn
            continue

        if status == "conflict":
            entries = list(fv.conflict_values)
            if not entries and fv.is_filled:
                entries.append({"value": fv.value, "unit": fv.unit, "evidence": fv.evidence, "source_refs": list(fv.source_refs), "turn": fv.updated_turn})
            entries.append({"value": parsed, "unit": unit or fv.unit, "evidence": evidence, "source_refs": [ref], "turn": turn})
            fv.conflict_values = _dedupe_conflicts(entries)
            if len(fv.conflict_values) >= 2:
                fv.status = FieldStatus.CONFLICT
                fv.source = Source.BUYER_EXPLICIT
                fv.note = note or fv.note
                fv.updated_turn = turn
                audit.append("field %s: conflict recorded (AI-flagged)" % key)
            else:
                _set_buyer_value(fv, parsed, kind, unit, evidence, ref, note, turn)
            continue

        # status == provided
        if fv.status == FieldStatus.CONFLICT:
            for c in fv.conflict_values:
                fv.history.append({"value": c.get("value"), "unit": c.get("unit"), "status": "conflict", "source": "buyer_explicit",
                                   "evidence": c.get("evidence"), "source_refs": c.get("source_refs") or [], "turn": c.get("turn"),
                                   "superseded_turn": turn, "reason": "conflict resolved by buyer"})
            _set_buyer_value(fv, parsed, kind, unit, evidence, ref, note, turn)
            audit.append("field %s: conflict resolved by explicit buyer statement" % key)
            continue

        if fv.is_filled:
            same_value = values_equal(fv.value, parsed) and (not unit or not fv.unit or norm_text(unit) == norm_text(fv.unit))
            if same_value:
                if ref and ref not in fv.source_refs:
                    fv.source_refs.append(ref)
                continue
            if u.get("revision") == "correction":
                fv.history.append(_snapshot(fv, "buyer correction", turn))
                _set_buyer_value(fv, parsed, kind, unit, evidence, ref, note, turn)
                audit.append("field %s: buyer correction applied" % key)
            else:
                fv.conflict_values = _dedupe_conflicts([
                    {"value": fv.value, "unit": fv.unit, "evidence": fv.evidence, "source_refs": list(fv.source_refs), "turn": fv.updated_turn},
                    {"value": parsed, "unit": unit or fv.unit, "evidence": evidence, "source_refs": [ref], "turn": turn},
                ])
                fv.status = FieldStatus.CONFLICT
                fv.note = note or "Two explicit buyer statements disagree."
                fv.updated_turn = turn
                audit.append("field %s: conflict — %r vs %r without an explicit correction" % (key, fv.value, parsed))
            continue

        if fv.status in (FieldStatus.RECOMMENDED, FieldStatus.UNKNOWN, FieldStatus.NOT_APPLICABLE):
            fv.history.append(_snapshot(fv, "replaced by buyer statement", turn))
            if fv.status == FieldStatus.NOT_APPLICABLE and fv.importance == Importance.NOT_APPLICABLE:
                fv.importance = FIELD_SPECS[key].default_importance if key in FIELD_SPECS else Importance.RECOMMENDED
        _set_buyer_value(fv, parsed, kind, unit, evidence, ref, note, turn)
    return audit


def _dedupe_conflicts(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for e in entries:
        if e.get("value") in (None, ""):
            continue
        if any(values_equal(o.get("value"), e.get("value")) for o in out):
            continue
        out.append(e)
    return out


# --------------------------------------------------------------------------- #
# Applicability
# --------------------------------------------------------------------------- #
def apply_applicability(rfq: RFQ, updates: List[Dict[str, Any]]) -> List[str]:
    audit: List[str] = []
    for a in updates or []:
        key = normalize_key(a.get("key"))
        fv = rfq.fields.get(key)
        if fv is None:
            audit.append("applicability for unknown field %s ignored" % key)
            continue
        imp = _enum_or(Importance, a.get("importance"), None)
        reason = (a.get("reason") or "").strip()
        if imp is None:
            continue
        if imp == Importance.NOT_APPLICABLE:
            if len(reason) < 8:
                audit.append("field %s: not_applicable rejected — no justification" % key)
                continue
            if fv.status in (FieldStatus.PROVIDED, FieldStatus.UNKNOWN, FieldStatus.CONFLICT):
                audit.append("field %s: not_applicable rejected — buyer fact present" % key)
                continue
            if fv.status == FieldStatus.RECOMMENDED:
                fv.history.append(_snapshot(fv, "marked not applicable", rfq.turn))
                fv.value = None
                fv.source = Source.MISSING
            fv.importance = Importance.NOT_APPLICABLE
            fv.status = FieldStatus.NOT_APPLICABLE
            fv.note = reason
            continue
        if fv.status == FieldStatus.NOT_APPLICABLE:
            fv.status = FieldStatus.MISSING
            fv.note = reason or None
        fv.importance = imp
        if reason and not fv.note:
            fv.note = reason
    return audit


# --------------------------------------------------------------------------- #
# Line items
# --------------------------------------------------------------------------- #
def _specs_from(items: List[Dict[str, Any]]) -> List[SpecAttr]:
    out: List[SpecAttr] = []
    for s in items or []:
        name = str(s.get("name") or "").strip()
        value = str(s.get("value") or "").strip()
        if not name or not value:
            continue
        unit = (s.get("unit") or "").strip() or None
        out.append(SpecAttr(name=name, value=value, unit=unit))
    return out


def _line_signature(product: str, specs: List[SpecAttr]) -> str:
    parts = [norm_text(product)] + sorted("%s=%s%s" % (norm_text(s.name), _squash(s.value), _squash(s.unit or "")) for s in specs)
    return "|".join(parts)


def merge_line_items(rfq: RFQ, mode: str, items: List[Dict[str, Any]], turn_text: str, turn_ref: str, turn: int,
                     prior: Sequence[Tuple[str, str]] = ()) -> List[str]:
    audit: List[str] = []
    if mode not in ("replace", "append") or not items:
        return audit
    if mode == "replace":
        if rfq.line_items:
            audit.append("line items replaced: %d removed (%s)" % (len(rfq.line_items), ", ".join(li.id for li in rfq.line_items)))
        rfq.line_items = []

    for it in items:
        product = (it.get("product") or "").strip() or rfq.product or "Item"
        specs = _specs_from(it.get("specifications") or [])
        qty = it.get("quantity")
        try:
            qty = float(qty) if qty is not None and float(qty) > 0 else None
        except (TypeError, ValueError):
            qty = None
        unit = (it.get("unit") or "pcs").strip() or "pcs"
        price = it.get("target_price")
        try:
            price = float(price) if price is not None and float(price) > 0 else None
        except (TypeError, ValueError):
            price = None
        date = (it.get("required_date") or "").strip() or None
        desc = (it.get("description") or "").strip()
        evidence = it.get("evidence")
        ref = find_evidence_ref(evidence, turn_text, turn_ref, prior, threshold=0.6)
        source = Source.BUYER_EXPLICIT if ref else Source.AI_RECOMMENDED

        # update existing by id
        lid = (it.get("line_id") or "").strip()
        existing = rfq.line_item(lid) if lid else None
        if existing is None:
            sig = _line_signature(product, specs)
            for li in rfq.line_items:
                if _line_signature(li.product, li.specifications) == sig:
                    existing = li
                    break
        if existing is not None:
            changed = []
            if qty is not None and (existing.quantity is None or abs(existing.quantity - qty) > 1e-9):
                existing.history.append({"field": "quantity", "old": existing.quantity, "new": qty, "turn": turn, "ref": ref})
                existing.quantity = qty
                changed.append("quantity")
            if specs and not existing.specifications:
                existing.specifications = specs
                changed.append("specifications")
            if price is not None and existing.target_price != price:
                existing.history.append({"field": "target_price", "old": existing.target_price, "new": price, "turn": turn, "ref": ref})
                existing.target_price = price
                changed.append("target_price")
            if date and existing.required_date != date:
                existing.history.append({"field": "required_date", "old": existing.required_date, "new": date, "turn": turn, "ref": ref})
                existing.required_date = date
                changed.append("required_date")
            if desc and not existing.description:
                existing.description = desc
            if unit and unit != existing.unit and qty is not None:
                existing.unit = unit
            if ref and ref not in existing.source_refs:
                existing.source_refs.append(ref)
            if changed:
                audit.append("%s updated: %s" % (existing.id, ", ".join(changed)))
            continue

        li = LineItem(
            id=rfq.next_line_item_id(), product=product, description=desc, specifications=specs, quantity=qty, unit=unit,
            target_price=price, required_date=date, source=source, source_refs=[ref] if ref else [], evidence=evidence,
        )
        if source == Source.AI_RECOMMENDED:
            audit.append("%s added without verifiable buyer evidence — needs buyer confirmation" % li.id)
        rfq.line_items.append(li)
    return audit


def apply_quantity_semantics(rfq: RFQ, turn: int) -> List[str]:
    """Keep the RFQ-level quantity honest relative to per-line quantities."""
    audit: List[str] = []
    q = rfq.fields.get("quantity")
    if q is None:
        return audit
    lines = rfq.line_items
    if not lines:
        return audit
    with_qty = [li for li in lines if li.quantity]
    if len(lines) == 1:
        li = lines[0]
        if li.quantity and not q.is_filled and q.status != FieldStatus.CONFLICT:
            _set_buyer_value(q, float(li.quantity), ValueKind.NUMBER, li.unit, li.evidence, li.source_refs[0] if li.source_refs else None,
                             "Mirrored from %s" % li.id, turn, source=li.source if li.source != Source.AI_RECOMMENDED else Source.BUYER_EXPLICIT)
            if li.source == Source.AI_RECOMMENDED:
                q.status, q.source = FieldStatus.RECOMMENDED, Source.AI_RECOMMENDED
        elif q.is_filled and not li.quantity and isinstance(q.value, (int, float)):
            li.quantity = float(q.value)
            li.unit = q.unit or li.unit
            li.source_refs = list(set(li.source_refs) | set(q.source_refs))
            audit.append("%s quantity mirrored from RFQ quantity" % li.id)
        return audit

    # several lines
    if len(with_qty) == len(lines):
        total = sum(float(li.quantity) for li in lines)
        units = {li.unit for li in lines}
        unit = units.pop() if len(units) == 1 else "units"
        refs: List[str] = []
        for li in lines:
            for r in li.source_refs:
                if r not in refs:
                    refs.append(r)
        if q.status == FieldStatus.CONFLICT:
            return audit
        per_line = {float(li.quantity) for li in lines}
        uniform_each = per_line.pop() if len(per_line) == 1 else None
        if q.is_filled and isinstance(q.value, (int, float)) and "per line" not in (q.note or "").lower():
            stated = float(q.value)
            is_total = abs(stated - total) < 1e-9
            is_each = uniform_each is not None and abs(stated - uniform_each) < 1e-9   # "2,000 pieces each"
            if not is_total and not is_each:
                # buyer gave a total that disagrees with the per-line sum → conflict, do not guess
                q.conflict_values = _dedupe_conflicts([
                    {"value": q.value, "unit": q.unit, "evidence": q.evidence, "source_refs": list(q.source_refs), "turn": q.updated_turn},
                    {"value": total, "unit": unit, "evidence": "sum of line-item quantities", "source_refs": refs, "turn": turn},
                ])
                q.status = FieldStatus.CONFLICT
                q.note = "Stated total differs from the sum of line-item quantities."
                audit.append("quantity: total %s conflicts with per-line sum %s" % (q.value, total))
                return audit
        q.value = total
        q.value_kind = ValueKind.NUMBER
        q.unit = unit
        q.status = FieldStatus.PROVIDED
        q.source = Source.BUYER_EXPLICIT if all(li.source != Source.AI_RECOMMENDED for li in lines) else Source.AI_RECOMMENDED
        if q.source == Source.AI_RECOMMENDED:
            q.status = FieldStatus.RECOMMENDED
        if uniform_each is not None:
            each_txt = "{:,}".format(int(uniform_each)) if float(uniform_each).is_integer() else "%g" % uniform_each
            q.note = "Specified per line item: %d lines × %s each; total shown for reference." % (len(lines), each_txt)
        else:
            q.note = "Specified per line item (%d lines); total shown for reference." % len(lines)
        q.evidence = q.evidence or "per-line quantities"
        q.source_refs = refs or q.source_refs
        q.confidence = 1.0
        q.updated_turn = turn
    else:
        missing = [li.id for li in lines if not li.quantity]
        if q.is_filled and q.status != FieldStatus.CONFLICT:
            q.note = "Total stated by buyer; %d of %d line items still need their own quantity." % (len(missing), len(lines))
        else:
            q.note = "%d of %d line items have no quantity yet." % (len(missing), len(lines))
    return audit


def derive_technical_summary(rfq: RFQ, turn: int) -> None:
    """Fill ``technical_summary`` from buyer-provided technical fields (no new facts)."""
    ts = rfq.fields.get("technical_summary")
    if ts is None or ts.status in (FieldStatus.NOT_APPLICABLE, FieldStatus.CONFLICT):
        return
    if ts.is_filled and "derived from" not in (ts.note or "").lower():
        return
    parts = []
    refs: List[str] = []
    for fv in rfq.fields.values():
        if fv.key == "technical_summary" or fv.section != Section.TECHNICAL or not fv.is_filled:
            continue
        parts.append("%s: %s" % (fv.label, fv.display_value()))
        refs.extend(r for r in fv.source_refs if r not in refs)
    if rfq.line_items and any(li.specifications for li in rfq.line_items):
        parts.append("%d line item(s) with per-line specifications" % len(rfq.line_items))
    if not parts:
        return
    ts.value = "; ".join(parts)
    ts.value_kind = ValueKind.TEXT
    ts.status = FieldStatus.PROVIDED
    ts.source = Source.BUYER_EXPLICIT
    ts.source_refs = refs
    ts.evidence = None
    ts.note = "Derived from buyer-provided technical fields."
    ts.updated_turn = turn


# --------------------------------------------------------------------------- #
# Questions
# --------------------------------------------------------------------------- #
def reconcile_questions(rfq: RFQ, ai_answered: List[Dict[str, Any]], new_questions: List[Dict[str, Any]],
                        turn_text: str, turn_ref: str, turn: int, is_first_turn: bool, settings: Settings) -> List[str]:
    audit: List[str] = []

    # 1. answers the AI mapped from free text
    for a in ai_answered or []:
        q = rfq.question(str(a.get("question_id") or ""))
        if q is None or q.status != QuestionStatus.OPEN:
            continue
        resolution = a.get("resolution") or "answered"
        q.status = QuestionStatus.ANSWERED
        q.resolution = resolution
        q.answer = (a.get("answer_summary") or "").strip() or None
        q.answered_turn = turn
        q.answer_ref = turn_ref
        fv = rfq.fields.get(normalize_key(q.field_key)) if q.field_key else None
        if fv is None or fv.is_filled or fv.status == FieldStatus.CONFLICT:
            continue
        if resolution == "unknown":
            fv.status = FieldStatus.UNKNOWN
            fv.source = Source.BUYER_EXPLICIT
            fv.value = None
            ev = a.get("evidence")
            fv.evidence = ev if evidence_supported(ev, turn_text) else None
            if turn_ref not in fv.source_refs:
                fv.source_refs.append(turn_ref)
            fv.updated_turn = turn
        elif resolution == "not_applicable":
            fv.status = FieldStatus.NOT_APPLICABLE
            fv.importance = Importance.NOT_APPLICABLE
            fv.source = Source.BUYER_EXPLICIT
            fv.value = None
            fv.note = "Buyer: %s" % (q.answer or "not applicable")
            if turn_ref not in fv.source_refs:
                fv.source_refs.append(turn_ref)
            fv.updated_turn = turn

    # 2. close open questions whose field got resolved another way
    for q in rfq.open_questions():
        fv = rfq.fields.get(normalize_key(q.field_key)) if q.field_key else None
        if fv is None:
            continue
        if fv.is_filled:
            q.status, q.resolution, q.answer, q.answered_turn = QuestionStatus.ANSWERED, "filled", fv.display_value(), turn
            q.answer_ref = fv.source_refs[-1] if fv.source_refs else turn_ref
        elif fv.status == FieldStatus.UNKNOWN:
            q.status, q.resolution, q.answered_turn = QuestionStatus.ANSWERED, "unknown", turn
            q.answer = q.answer or "Buyer doesn't know"
        elif fv.status == FieldStatus.NOT_APPLICABLE:
            q.status, q.resolution, q.answered_turn = QuestionStatus.DISMISSED, "not_applicable", turn

    # 3. conflicts always get a deterministic clarifying question
    for fv in rfq.fields.values():
        if fv.status != FieldStatus.CONFLICT:
            continue
        if any(q.field_key == fv.key and q.status == QuestionStatus.OPEN for q in rfq.questions):
            continue
        options = []
        for c in fv.conflict_values:
            v = c.get("value")
            txt = ("{:,}".format(int(v)) if isinstance(v, float) and float(v).is_integer() else str(v))
            if c.get("unit"):
                txt = "%s %s" % (txt, c["unit"])
            options.append(txt)
        rfq.questions.append(Question(
            id=new_id("q"), category=fv.section,
            question="You gave different values for %s: %s. Which should suppliers quote against?" % (fv.label.lower(), " vs ".join(options)),
            reason="Suppliers cannot price contradictory requirements; the RFQ stays on hold until this is settled.",
            importance=Importance.REQUIRED, field_key=fv.key, answer_type=AnswerType.CHOICE, suggested_options=options,
            asked_turn=turn,
        ))
        audit.append("conflict question added for %s" % fv.key)

    # 4. new AI questions: dedupe, never re-ask, cap
    cap = settings.max_questions_first if is_first_turn else settings.max_questions_turn
    added = 0
    ordered = sorted(new_questions or [], key=lambda n: _rank(_enum_or(Importance, n.get("importance"), Importance.RECOMMENDED)))
    for nq in ordered:
        text = (nq.get("question") or "").strip()
        if not text:
            continue
        fk = normalize_key(nq.get("field_key")) or None
        skip = None
        if fk:
            fv = rfq.fields.get(fk)
            if fv is not None and (fv.is_filled or fv.status in (FieldStatus.UNKNOWN, FieldStatus.NOT_APPLICABLE)):
                skip = "field %s already resolved" % fk
            elif any(q.field_key == fk and q.status in (QuestionStatus.OPEN, QuestionStatus.SKIPPED) for q in rfq.questions):
                skip = "question for %s already open or skipped" % fk
        if skip is None:
            for q in rfq.questions:
                if similarity(q.question, text) >= 0.5:
                    skip = "rephrasing of %s" % q.id
                    break
        if skip is None and (added >= cap or len(rfq.open_questions()) >= settings.max_open_questions):
            skip = "question cap reached"
        if skip:
            audit.append("question dropped (%s): %s" % (skip, text[:80]))
            continue
        rfq.questions.append(Question(
            id=new_id("q"),
            category=_enum_or(Section, nq.get("section"), Section.TECHNICAL),
            question=text,
            reason=(nq.get("reason") or "").strip(),
            importance=_enum_or(Importance, nq.get("importance"), Importance.RECOMMENDED),
            field_key=fk,
            answer_type=_enum_or(AnswerType, nq.get("answer_type"), AnswerType.TEXT),
            suggested_options=[str(o) for o in (nq.get("suggested_options") or []) if str(o).strip()][:8],
            asked_turn=turn,
        ))
        added += 1

    # 5. safety net: a REQUIRED universal field that nobody has ever asked about gets the
    #    registry's standard question, so readiness is always reachable. Bounded by the open cap.
    for fv in rfq.fields.values():
        if fv.importance != Importance.REQUIRED or fv.status != FieldStatus.MISSING:
            continue
        if any(q.field_key == fv.key for q in rfq.questions):
            continue
        spec = FIELD_SPECS.get(fv.key)
        if spec is None or not spec.question:
            continue
        if len(rfq.open_questions()) >= settings.max_open_questions:
            break
        rfq.questions.append(Question(
            id=new_id("q"), category=fv.section, question=spec.question, reason=spec.reason, importance=Importance.REQUIRED,
            field_key=fv.key, answer_type=spec.answer_type, suggested_options=list(spec.options), asked_turn=turn,
        ))
        audit.append("standard question added for required field %s (no question had covered it)" % fv.key)
    return audit


# --------------------------------------------------------------------------- #
# Completeness & readiness
# --------------------------------------------------------------------------- #
_WEIGHT = {Importance.REQUIRED: 3, Importance.RECOMMENDED: 1, Importance.OPTIONAL: 0, Importance.NOT_APPLICABLE: 0}


def join_labels(labels: List[str], limit: int = 4) -> str:
    """Readable list that never claims to show more than it does."""
    items = [str(x) for x in labels if str(x).strip()]
    if not items:
        return ""
    if len(items) <= limit:
        return ", ".join(items[:-1]) + (" and " + items[-1] if len(items) > 1 else items[0]) if len(items) > 1 else items[0]
    return "%s and %d more" % (", ".join(items[:limit]), len(items) - limit)


def line_item_gaps(rfq: RFQ) -> List[str]:
    gaps: List[str] = []
    for li in rfq.line_items:
        if not li.product.strip():
            gaps.append("%s · product" % li.id)
        if not li.quantity:
            gaps.append("%s · quantity" % li.id)
        if li.source == Source.AI_RECOMMENDED:
            gaps.append("%s · needs buyer confirmation" % li.id)
    return gaps


def compute_completeness(rfq: RFQ, ai_completeness: Optional[Dict[str, Any]], turn: int) -> Completeness:
    earned = 0.0
    total = 0.0
    missing_required: List[str] = []
    recommended: List[str] = []
    conflicts: List[str] = []
    for fv in rfq.fields.values():
        if not fv.counts_for_score:
            continue
        w = _WEIGHT.get(fv.importance, 0)
        total += w
        if fv.is_filled:
            earned += w
            continue
        if fv.status == FieldStatus.CONFLICT:
            conflicts.append(fv.label)
            continue
        if fv.status == FieldStatus.UNKNOWN:
            continue  # acknowledged gap: not a blocker, but earns nothing
        if fv.importance == Importance.REQUIRED:
            missing_required.append(fv.label)
        elif fv.importance == Importance.RECOMMENDED:
            recommended.append(fv.label)

    gaps = line_item_gaps(rfq)
    for li in rfq.line_items:
        total += 3
        if li.product.strip() and li.quantity and li.source != Source.AI_RECOMMENDED:
            earned += 3

    open_required = [q for q in rfq.open_questions() if q.importance == Importance.REQUIRED and q.field_key not in
                     {normalize_key(k) for k in []}]
    # required open questions whose field is already counted as missing are not double-listed
    missing_labels = {norm_text(x) for x in missing_required}
    required_questions = [q.question for q in open_required
                          if not (q.field_key and rfq.fields.get(q.field_key) and norm_text(rfq.fields[q.field_key].label) in missing_labels)
                          and not (q.field_key and rfq.fields.get(q.field_key) and rfq.fields[q.field_key].status == FieldStatus.CONFLICT)]

    score = int(round(100.0 * earned / total)) if total > 0 else 0
    if not rfq.is_classified:
        score = min(score, 20)
    score = max(0, min(100, score))

    ready = rfq.is_classified and not missing_required and not gaps and not conflicts and not required_questions

    blockers = len(missing_required) + len(gaps) + len(conflicts) + len(required_questions)
    if ready:
        expl = "Ready to send. Suppliers have what they need to quote accurately."
        if recommended:
            expl += " Answering %s could sharpen pricing." % join_labels(recommended)
    else:
        if not rfq.is_classified:
            expl = "Not ready: the product has not been identified yet."
        else:
            parts = []
            if missing_required:
                parts.append("%d critical requirement%s missing (%s)" % (
                    len(missing_required), "" if len(missing_required) == 1 else "s", join_labels(missing_required)))
            if gaps:
                parts.append("line items incomplete (%s)" % join_labels(gaps, 3))
            if conflicts:
                parts.append("%d conflict%s to resolve (%s)" % (
                    len(conflicts), "" if len(conflicts) == 1 else "s", join_labels(conflicts)))
            if required_questions:
                parts.append("%d required question%s unanswered" % (len(required_questions), "" if len(required_questions) == 1 else "s"))
            expl = "I recommend answering %d more item%s before sending this RFQ because %s could materially affect supplier pricing: %s." % (
                blockers, "" if blockers == 1 else "s", "it" if blockers == 1 else "they", "; ".join(parts))

    ai = ai_completeness or {}
    return Completeness(
        score=score,
        ready_to_send=ready,
        missing_required_fields=missing_required + gaps,
        recommended_fields=recommended,
        open_conflicts=conflicts,
        blocking_questions=required_questions,
        open_ambiguities=[str(x) for x in (ai.get("open_ambiguities") or [])][:6],
        explanation=expl,
        ai_score=ai.get("score"),
        ai_ready_claim=ai.get("ready_to_send"),
        ai_explanation=(ai.get("explanation") or None),
        computed_turn=turn,
    )


def next_status(rfq: RFQ) -> RFQStatus:
    if rfq.status == RFQStatus.SUPPLIER_READY:
        return rfq.status
    if rfq.turn == 0:
        return RFQStatus.DRAFT
    return RFQStatus.READY if rfq.completeness.ready_to_send else RFQStatus.IN_PROGRESS
