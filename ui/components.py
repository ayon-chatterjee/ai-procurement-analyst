"""Reusable UI pieces. Render state only; every action goes through RFQService."""
from __future__ import annotations

from typing import Dict, List

import pandas as pd
import streamlit as st

from rfq_copilot.schema import (
    RFQ, SECTION_LABELS, FieldStatus, FieldValue, Importance, Question, RFQStatus, Section, Source,
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
    with st.container(border=True):
        st.markdown('<div class="rfq-q">%s</div>' % esc(q.question), unsafe_allow_html=True)
        st.markdown('%s <span class="rfq-why">&nbsp; Why this matters: %s</span>' % (importance_badge(q.importance), esc(q.reason)), unsafe_allow_html=True)
        if q.suggested_options:
            st.pills("Choose or type below", q.suggested_options, selection_mode="single", key=keys["opt"], label_visibility="collapsed")
        placeholder = {"number": "e.g. 2,000", "date": "e.g. 15 Oct 2026 or 'within 6 weeks'", "yes_no": "Yes / No"}.get(q.answer_type.value, "Your answer")
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
        typed = str(st.session_state.get(keys["ans"]) or "").strip()
        picked = st.session_state.get(keys["opt"])
        if typed:
            answers[q.id] = typed
        elif picked:
            answers[q.id] = str(picked)
        elif st.session_state.get(keys["skip"]):
            skipped.append(q.id)
    return {"answers": answers, "skipped": skipped}


# --------------------------------------------------------------------------- #
# Line items table
# --------------------------------------------------------------------------- #
def line_items_frame(rfq: RFQ) -> pd.DataFrame:
    rows = []
    for li in rfq.line_items:
        rows.append({
            "id": li.id,
            "product": li.product,
            "specifications": li.spec_summary() or li.description,
            "quantity": li.quantity,
            "unit": li.unit,
            "target_price": li.target_price,
            "required_date": li.required_date or "",
            "source": {"buyer_explicit": "Buyer stated", "manual_edit": "Buyer edited", "ai_recommended": "Needs confirmation"}.get(li.source.value, li.source.value),
        })
    return pd.DataFrame(rows, columns=["id", "product", "specifications", "quantity", "unit", "target_price", "required_date", "source"])


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


