"""Reusable UI pieces. Render state only; every action goes through RFQService."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd
import streamlit as st

from rfq_copilot.schema import (
    RFQ, SECTION_LABELS, AnswerType, FieldStatus, FieldValue, Importance, Question, RFQStatus, Section, Source,
)
from .theme import badge, esc

SECTION_ORDER = [Section.TECHNICAL, Section.COMMERCIAL, Section.LOGISTICS, Section.SOURCING, Section.QUALITY, Section.INSTRUCTIONS]


# --------------------------------------------------------------------------- #
# Labels
# --------------------------------------------------------------------------- #
def importance_badge(imp: Importance) -> str:
    return badge(imp.value, imp.value.replace("_", " ").capitalize())


def provenance_badge(fv: FieldValue) -> str:
    if fv.status == FieldStatus.PROVIDED:
        return badge("edited" if fv.source == Source.MANUAL_EDIT else "buyer", "Buyer edited" if fv.source == Source.MANUAL_EDIT else "Buyer stated")
    if fv.status == FieldStatus.RECOMMENDED:
        return badge("ai", "AI recommendation")
    if fv.status == FieldStatus.UNKNOWN:
        return badge("unknown", "Buyer unsure")
    if fv.status == FieldStatus.NOT_APPLICABLE:
        return badge("na", "Not applicable")
    if fv.status == FieldStatus.CONFLICT:
        return badge("conflict", "Conflict")
    return badge("missing", "Missing")


def status_badge(rfq: RFQ) -> str:
    if rfq.status == RFQStatus.SUPPLIER_READY:
        return badge("status-sent", "Supplier-ready")
    if rfq.completeness.ready_to_send:
        return badge("status-ready", "Ready to send")
    return badge("status-not", "Not ready")


def field_value_text(fv: FieldValue) -> str:
    if fv.status == FieldStatus.PROVIDED:
        return fv.display_value()
    if fv.status == FieldStatus.RECOMMENDED:
        val = fv.display_value()
        return ("Recommended: %s" % val) if val else (fv.note or "Recommended: confirm")
    if fv.status == FieldStatus.UNKNOWN:
        return "Buyer doesn't know yet"
    if fv.status == FieldStatus.NOT_APPLICABLE:
        return fv.note or "Not applicable"
    if fv.status == FieldStatus.CONFLICT:
        vals = []
        for c in fv.conflict_values:
            v = c.get("value")
            txt = "{:,}".format(int(v)) if isinstance(v, float) and float(v).is_integer() else str(v)
            vals.append(("%s %s" % (txt, c["unit"])) if c.get("unit") else txt)
        return " vs ".join(vals) or "Conflicting statements"
    return "Missing"


# --------------------------------------------------------------------------- #
# Readiness panel
# --------------------------------------------------------------------------- #
def _check_icon(fv: FieldValue) -> str:
    return {
        FieldStatus.PROVIDED: '<span class="ic ok">✓</span>',
        FieldStatus.RECOMMENDED: '<span class="ic ai">✦</span>',
        FieldStatus.UNKNOWN: '<span class="ic unk">?</span>',
        FieldStatus.NOT_APPLICABLE: '<span class="ic na">—</span>',
        FieldStatus.CONFLICT: '<span class="ic conf">!</span>',
        FieldStatus.MISSING: '<span class="ic miss">⚠</span>',
    }[fv.status]


def render_readiness_panel(rfq: RFQ) -> None:
    c = rfq.completeness
    st.markdown('<div class="rfq-kicker">RFQ readiness</div>', unsafe_allow_html=True)
    st.markdown('<div class="rfq-score">%d%%<small>complete</small></div>' % c.score, unsafe_allow_html=True)
    if rfq.status == RFQStatus.SUPPLIER_READY:
        st.markdown(badge("status-sent", "SUPPLIER-READY"), unsafe_allow_html=True)
    elif c.ready_to_send:
        st.markdown(badge("status-ready", "READY TO SEND"), unsafe_allow_html=True)
    else:
        st.markdown(badge("status-not", "NOT READY"), unsafe_allow_html=True)
    blockers = list(c.missing_required_fields) + ["Conflict: %s" % x for x in c.open_conflicts] + list(c.blocking_questions)
    if blockers:
        st.markdown("**Why it isn't ready**")
        st.markdown('<ul class="rfq-check">%s</ul>' % "".join(
            '<li><span class="ic miss">⚠</span><span class="lbl">%s</span></li>' % esc(b if len(b) < 70 else b[:67] + "…")
            for b in blockers[:8]), unsafe_allow_html=True)
        if len(blockers) > 8:
            st.caption("and %d more" % (len(blockers) - 8))
    else:
        st.caption(c.explanation)

    st.markdown("---")
    st.markdown('<div class="rfq-kicker">What the RFQ contains</div>', unsafe_allow_html=True)
    for sec in SECTION_ORDER:
        rows = [fv for fv in rfq.section(sec).values() if _show_in_checklist(fv)]
        if not rows:
            continue
        items = []
        for fv in sorted(rows, key=_checklist_rank):
            val = field_value_text(fv)
            if fv.status == FieldStatus.PROVIDED and fv.source == Source.MANUAL_EDIT:
                val += " · edited"
            items.append('<li>%s<span class="lbl">%s</span> <span class="val">— %s</span></li>' % (_check_icon(fv), esc(fv.label), esc(val)))
        st.markdown("**%s**" % SECTION_LABELS[sec])
        st.markdown('<ul class="rfq-check">%s</ul>' % "".join(items), unsafe_allow_html=True)
    if rfq.line_items:
        n_ok = sum(1 for li in rfq.line_items if li.quantity and li.source != Source.AI_RECOMMENDED)
        st.markdown("**Line items**")
        st.markdown('<ul class="rfq-check"><li>%s<span class="lbl">%d line item%s</span> <span class="val">— %d complete</span></li></ul>' % (
            '<span class="ic ok">✓</span>' if n_ok == len(rfq.line_items) else '<span class="ic miss">⚠</span>',
            len(rfq.line_items), "" if len(rfq.line_items) == 1 else "s", n_ok), unsafe_allow_html=True)
    st.markdown('<div class="rfq-sub" style="margin-top:.5rem">✓ Buyer provided &nbsp; ✦ AI recommended &nbsp; ⚠ Missing &nbsp; ? Buyer unsure &nbsp; — Not applicable &nbsp; ! Conflict</div>',
                unsafe_allow_html=True)


def _show_in_checklist(fv: FieldValue) -> bool:
    # A derived summary just restates the fields listed beside it; keep the checklist free of that echo.
    if fv.key == "technical_summary" and "derived from" in (fv.note or "").lower():
        return False
    if fv.status == FieldStatus.MISSING:
        return fv.importance in (Importance.REQUIRED, Importance.RECOMMENDED)
    return True


def _checklist_rank(fv: FieldValue) -> int:
    order = {FieldStatus.CONFLICT: 0, FieldStatus.MISSING: 1, FieldStatus.PROVIDED: 2, FieldStatus.UNKNOWN: 3,
             FieldStatus.RECOMMENDED: 4, FieldStatus.NOT_APPLICABLE: 5}
    return order[fv.status] * 10 + {Importance.REQUIRED: 0, Importance.RECOMMENDED: 1, Importance.OPTIONAL: 2, Importance.NOT_APPLICABLE: 3}[fv.importance]


# --------------------------------------------------------------------------- #
# Question cards (inside an st.form)
# --------------------------------------------------------------------------- #
def question_widget_keys(q: Question, turn: int) -> Dict[str, str]:
    return {"ans": "ans_%s_%d" % (q.id, turn), "opt": "opt_%s_%d" % (q.id, turn), "skip": "skip_%s_%d" % (q.id, turn)}


def render_question_card(q: Question, turn: int) -> None:
    keys = question_widget_keys(q, turn)
    multi = q.answer_type == AnswerType.MULTI_CHOICE
    with st.container(border=True):
        st.markdown('<div class="rfq-q">%s</div>' % esc(q.question), unsafe_allow_html=True)
        st.markdown('%s <span class="rfq-why">&nbsp; Why this matters: %s</span>' % (importance_badge(q.importance), esc(q.reason)), unsafe_allow_html=True)
        if q.suggested_options:
            st.pills("Choose" + (" any that apply" if multi else ""), q.suggested_options,
                     selection_mode="multi" if multi else "single", key=keys["opt"], label_visibility="collapsed")
            if multi:
                st.caption("Pick as many as apply.")
        placeholder = {
            "number": "e.g. 2,000",
            "date": "e.g. 15 Oct 2026 or 'within 6 weeks'",
            "yes_no": "Yes / No",
            "multi_choice": "Anything else to add",
            "choice": "Or type a different answer",
        }.get(q.answer_type.value, "Your answer")
        col_a, col_b = st.columns([5, 1.4])
        with col_a:
            st.text_input("Answer", key=keys["ans"], placeholder=placeholder, label_visibility="collapsed")
        with col_b:
            st.checkbox("Skip", key=keys["skip"], help="Don't ask me this again")


def collect_answers(questions: List[Question], turn: int) -> Dict[str, object]:
    answers: Dict[str, str] = {}
    skipped: List[str] = []
    for q in questions:
        keys = question_widget_keys(q, turn)
        multi = q.answer_type == AnswerType.MULTI_CHOICE
        typed = str(st.session_state.get(keys["ans"]) or "").strip()
        picked = st.session_state.get(keys["opt"])
        # Multi-select returns a list; several options can legitimately apply at once
        # (both sea and air freight, or several certifications).
        if isinstance(picked, (list, tuple, set)):
            chosen = ", ".join(str(p) for p in picked if str(p).strip())
        else:
            chosen = str(picked) if picked else ""
        if chosen and typed and multi:
            # Multi-select: the typed box is labelled "Anything else to add", so combine.
            answers[q.id] = "%s; %s" % (chosen, typed)
        elif typed:
            # Single choice: the typed box is labelled "Or type a different answer" - it overrides.
            answers[q.id] = typed
        elif chosen:
            answers[q.id] = chosen
        elif st.session_state.get(keys["skip"]):
            skipped.append(q.id)
    return {"answers": answers, "skipped": skipped}


# --------------------------------------------------------------------------- #
# Line items table
# --------------------------------------------------------------------------- #
LINE_COLUMNS = ["id", "product", "specifications", "quantity", "unit", "target_price", "required_date", "source"]
SOURCE_LABELS = {"buyer_explicit": "Buyer stated", "manual_edit": "Buyer edited", "ai_recommended": "Needs confirmation"}


def line_item_rows(rfq: RFQ) -> List[Dict[str, Any]]:
    """The grid's rows, in display order. Position is what the editor's deltas index into."""
    return [{
        "id": li.id,
        "product": li.product,
        "specifications": li.spec_summary() or li.description,
        "quantity": li.quantity,
        "unit": li.unit,
        "target_price": li.target_price,
        "required_date": li.required_date or "",
        "source": SOURCE_LABELS.get(li.source.value, li.source.value),
    } for li in rfq.line_items]


def line_items_frame(rfq: RFQ) -> pd.DataFrame:
    return pd.DataFrame(line_item_rows(rfq), columns=LINE_COLUMNS)


# --------------------------------------------------------------------------- #
# Resume hint
# --------------------------------------------------------------------------- #
def render_resume_hint(svc, key_prefix: str, on_open) -> bool:
    """When nothing is open but work exists, offer the most recent RFQ. Returns True if shown."""
    rows = svc.list_rfqs()
    if not rows:
        return False
    r = rows[0]
    with st.container(border=True):
        c1, c2 = st.columns([5, 1.6])
        with c1:
            st.markdown('<div class="rfq-kicker">Pick up where you left off</div>', unsafe_allow_html=True)
            st.markdown('<div class="rfq-field-value">%s</div>' % esc(r.title or r.product or r.id), unsafe_allow_html=True)
            st.caption("%d%% complete · %d line item%s · updated %s" % (
                r.readiness_score, r.line_item_count, "" if r.line_item_count == 1 else "s", r.updated_at.replace("T", " ")[:16]))
        with c2:
            if st.button("Open", key="%s_resume" % key_prefix, type="primary", use_container_width=True):
                on_open(r.id)
    return True




# --------------------------------------------------------------------------- #
# Data-editor deltas
# --------------------------------------------------------------------------- #
def _clean(v):
    """NaN/NaT from pandas become None; everything else passes through."""
    if v is None:
        return None
    if isinstance(v, float) and v != v:      # NaN is the only value unequal to itself
        return None
    return v


def apply_editor_deltas(base_rows: List[Dict[str, Any]], deltas: Optional[Dict[str, Any]],
                        blank: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Fold an st.data_editor widget state onto the rows it was rendered from.

    Streamlit keeps grid changes in session_state as
    ``{"edited_rows": {row_index: {column: value}}, "added_rows": [...], "deleted_rows": [...]}``
    where the indices are positions in the dataframe that was passed in. Reading that state
    is what makes edits and new rows land; the widget's return value proved unreliable here
    and dropped both silently.
    """
    deltas = deltas or {}
    edited = deltas.get("edited_rows") or {}
    added = deltas.get("added_rows") or []
    deleted = set()
    for i in deltas.get("deleted_rows") or []:
        try:
            deleted.add(int(i))
        except (TypeError, ValueError):
            continue

    out: List[Dict[str, Any]] = []
    for i, row in enumerate(base_rows):
        if i in deleted:
            continue
        r = {k: _clean(v) for k, v in row.items()}
        # index keys arrive as int or str depending on how the state was serialised
        patch = edited.get(i)
        if patch is None:
            patch = edited.get(str(i)) or {}
        for k, v in patch.items():
            r[k] = _clean(v)
        out.append(r)

    template = blank or {}
    for a in added:
        r = dict(template)
        for k, v in (a or {}).items():
            v = _clean(v)
            if v is not None:
                r[k] = v
        r["id"] = ""          # a new row always gets a freshly allocated line id
        out.append(r)
    return out


BLANK_LINE_ROW: Dict[str, Any] = {
    "id": "", "product": "", "specifications": "", "quantity": None,
    "unit": "pcs", "target_price": None, "required_date": "",
}


def pending_line_changes(deltas: Optional[Dict[str, Any]]) -> Dict[str, int]:
    """What the grid is holding that has not been saved yet.

    A row the buyer merely clicked into shows up in ``added_rows`` as an empty dict, so a
    blank new row is not counted as a change.
    """
    deltas = deltas or {}
    added = [a for a in (deltas.get("added_rows") or []) if str((a or {}).get("product") or "").strip()]
    counts = {
        "edited": len(deltas.get("edited_rows") or {}),
        "added": len(added),
        "deleted": len(deltas.get("deleted_rows") or []),
    }
    return {k: v for k, v in counts.items() if v}


def describe_changes(pending: Dict[str, int]) -> str:
    if not pending:
        return ""
    order = [("edited", "edited"), ("added", "new"), ("deleted", "removed")]
    parts = ["%d %s" % (pending[k], word) for k, word in order if pending.get(k)]
    total = sum(pending.values())
    return "%s (%s)" % ("1 line" if total == 1 else "%d lines" % total, ", ".join(parts))
