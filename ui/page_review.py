"""Screen 2 — Review RFQ: the structured RFQ with provenance, manual correction and export."""
from __future__ import annotations

from typing import List

import pandas as pd
import streamlit as st

from rfq_copilot.rfq_service import RFQStateError
from rfq_copilot.schema import RFQ, SECTION_LABELS, FieldStatus, FieldValue, Importance, RFQStatus, Section
from . import state
from .components import (
    BLANK_LINE_ROW, LINE_COLUMNS, SECTION_ORDER, apply_editor_deltas, describe_changes, field_value_text,
    line_item_rows, pending_line_changes, provenance_badge, render_readiness_panel, render_resume_hint, status_badge,
)
from .theme import badge, esc


def render() -> None:
    svc = state.get_service()
    state.show_flash()
    rfq = state.current_rfq()
    if rfq is None:
        st.markdown('<div class="rfq-hero"><h1>Review RFQ</h1><p>No RFQ is open yet.</p></div>', unsafe_allow_html=True)

        def _open(rid):
            state.set_current(rid)
            st.rerun()
        render_resume_hint(svc, "review", _open)
        st.markdown("")
        c1, c2, _ = st.columns([1.3, 1.3, 4])
        if c1.button("New RFQ", type="primary", use_container_width=True):
            state.set_current(None)
            state.go("copilot")
        if c2.button("Saved RFQs", use_container_width=True):
            state.go("saved")
        return

    _header(svc, rfq)
    left, right = st.columns([7, 3.6], gap="large")
    with left:
        _overview(rfq)
        _line_items(svc, rfq)
        for sec in SECTION_ORDER:
            _section(svc, rfq, sec)
        _audit(svc, rfq)
    with right:
        with st.container(border=True):
            render_readiness_panel(rfq)
        _actions(svc, rfq)


# --------------------------------------------------------------------------- #
def _header(svc, rfq: RFQ) -> None:
    c1, c2 = st.columns([6, 1.3])
    with c1:
        st.markdown('<div class="rfq-kicker">Review RFQ</div><div class="rfq-title">%s</div>' % esc(rfq.title or rfq.product), unsafe_allow_html=True)
        st.markdown('<div class="rfq-sub">%s &nbsp; %d%% complete · %d line item%s · turn %d</div>' % (
            status_badge(rfq), rfq.completeness.score, len(rfq.line_items), "" if len(rfq.line_items) == 1 else "s", rfq.turn), unsafe_allow_html=True)
    with c2:
        if st.button("Back to Copilot", use_container_width=True):
            state.go("copilot")
    if rfq.status != RFQStatus.SUPPLIER_READY and not rfq.completeness.ready_to_send:
        st.warning(rfq.completeness.explanation)
    st.markdown("")


def _is_derived_summary(fv: FieldValue) -> bool:
    return fv.key == "technical_summary" and "derived from" in (fv.note or "").lower()


def _overview(rfq: RFQ) -> None:
    with st.container(border=True):
        st.markdown('<div class="rfq-kicker">Overview</div>', unsafe_allow_html=True)
        c1, c2, c3 = st.columns(3)
        c1.markdown('<div class="rfq-field-label">Product</div><div class="rfq-field-value">%s</div>' % esc(rfq.product or "—"), unsafe_allow_html=True)
        c2.markdown('<div class="rfq-field-label">Category</div><div class="rfq-field-value">%s</div>' % esc(rfq.category or "—"), unsafe_allow_html=True)
        c3.markdown('<div class="rfq-field-label">Product type</div><div class="rfq-field-value">%s</div>' % esc(rfq.product_type or "—"), unsafe_allow_html=True)
        if rfq.classification_note:
            st.caption(rfq.classification_note)
        ts = rfq.fields.get("technical_summary")
        if ts is not None and ts.is_filled and _is_derived_summary(ts):
            st.markdown('<div class="rfq-field-label" style="margin-top:.6rem">Specification summary</div>'
                        '<div class="rfq-field-value">%s</div>' % esc(ts.display_value()), unsafe_allow_html=True)
            st.caption("Assembled from the buyer-stated fields below.")


def _line_items(svc, rfq: RFQ) -> None:
    """An editable grid: type in a cell to change it, type in the bottom row to add a line.

    The grid's changes are read from its widget state rather than its return value; the
    return value silently dropped both edits and added rows in an earlier version.
    """
    locked = rfq.status == RFQStatus.SUPPLIER_READY
    rows = line_item_rows(rfq)
    # Re-key on updated_at so a save starts a clean grid instead of replaying stale deltas.
    editor_key = "li_grid_%s_%s" % (rfq.id, rfq.updated_at)

    with st.container(border=True):
        st.markdown('<div class="rfq-kicker">Line items</div>', unsafe_allow_html=True)
        if not rfq.line_items:
            st.markdown('<div class="rfq-field-value dim">No line items yet. List sizes or variants in the Copilot, '
                        'or type in the empty row below to add one.</div>', unsafe_allow_html=True)

        st.data_editor(
            pd.DataFrame(rows, columns=LINE_COLUMNS),
            key=editor_key,
            num_rows="fixed" if locked else "dynamic",
            disabled=True if locked else ["id", "source"],
            hide_index=True,
            use_container_width=True,
            column_config={
                "id": st.column_config.TextColumn("Line", width="small", help="Assigned automatically when you save"),
                "product": st.column_config.TextColumn("Product", required=True),
                "specifications": st.column_config.TextColumn("Specifications", help="Name: value; Name: value", width="large"),
                "quantity": st.column_config.NumberColumn("Quantity", min_value=0, step=1, format="%d"),
                "unit": st.column_config.TextColumn("Unit", width="small"),
                "target_price": st.column_config.NumberColumn("Target price", min_value=0.0, format="%.2f"),
                "required_date": st.column_config.TextColumn("Required date"),
                "source": st.column_config.TextColumn("Source", width="small"),
            },
        )
        if locked:
            return

        # Live count of what the grid is actually holding. A cell still being typed has not
        # reached the widget state yet, so this is the buyer's signal that an edit registered -
        # previously a half-finished cell was lost on save with no indication at all.
        deltas = st.session_state.get(editor_key) or {}
        pending = pending_line_changes(deltas)
        st.caption("Start typing in the **Product** column of the empty bottom row to add a line. "
                   "Select a row and press delete to remove it. Press Enter or Tab to commit a cell before saving.")
        if pending:
            st.markdown('<span class="rfq-pending">%s unsaved</span>' % esc(describe_changes(pending)), unsafe_allow_html=True)
        else:
            st.caption("No unsaved changes.")
        if st.button("Save line items", key="li_save_%s" % rfq.id, type="primary", disabled=not pending):
            edited = apply_editor_deltas(rows, deltas, BLANK_LINE_ROW)
            payload = [r for r in edited if str(r.get("product") or "").strip()]
            blanks = len(edited) - len(payload)
            try:
                svc.replace_line_items(rfq.id, payload)
                msg = "Line items saved. Your edits are recorded as buyer requirements."
                if blanks:
                    msg += " %d row%s without a product name %s ignored." % (
                        blanks, "" if blanks == 1 else "s", "was" if blanks == 1 else "were")
                state.flash(msg)
                st.rerun()
            except RFQStateError as e:
                st.error(str(e))

        needs = [li.id for li in rfq.line_items if li.source.value == "ai_recommended"]
        if needs:
            st.caption("%s could not be matched to your exact words. Confirm or edit them; unconfirmed lines block readiness."
                       % ", ".join(needs))


def _visible_fields(rfq: RFQ, sec: Section) -> List[FieldValue]:
    out = []
    for fv in rfq.section(sec).values():
        if fv.status == FieldStatus.MISSING and fv.importance == Importance.OPTIONAL:
            continue
        if _is_derived_summary(fv):
            continue   # shown once in the overview instead
        out.append(fv)
    rank = {FieldStatus.CONFLICT: 0, FieldStatus.PROVIDED: 1, FieldStatus.MISSING: 2, FieldStatus.UNKNOWN: 3, FieldStatus.RECOMMENDED: 4, FieldStatus.NOT_APPLICABLE: 5}
    return sorted(out, key=lambda f: (rank[f.status], f.label))


def _section(svc, rfq: RFQ, sec: Section) -> None:
    fields = _visible_fields(rfq, sec)
    if not fields and sec != Section.INSTRUCTIONS:
        return
    with st.container(border=True):
        st.markdown('<div class="rfq-kicker">%s</div>' % esc(SECTION_LABELS[sec]), unsafe_allow_html=True)
        if not fields:
            st.markdown('<div class="rfq-field-value dim">Nothing specified.</div>', unsafe_allow_html=True)
        for fv in fields:
            _field_row(svc, rfq, fv)


def _field_row(svc, rfq: RFQ, fv: FieldValue) -> None:
    c1, c2 = st.columns([3.4, 6.6])
    with c1:
        st.markdown('<div class="rfq-field-label">%s</div>' % esc(fv.label), unsafe_allow_html=True)
        if fv.status == FieldStatus.MISSING and fv.importance in (Importance.REQUIRED, Importance.RECOMMENDED):
            # "Missing" is already the value; the useful badge is how much it matters.
            st.markdown(badge(fv.importance.value, fv.importance.value.capitalize()), unsafe_allow_html=True)
        else:
            st.markdown(provenance_badge(fv), unsafe_allow_html=True)
    with c2:
        text = field_value_text(fv)
        dim = fv.status != FieldStatus.PROVIDED
        st.markdown('<div class="rfq-field-value%s">%s</div>' % (" dim" if dim else "", esc(text)), unsafe_allow_html=True)
        if fv.status == FieldStatus.PROVIDED and fv.note and "derived" not in fv.note.lower():
            st.caption(fv.note)
        if fv.status == FieldStatus.RECOMMENDED and fv.display_value() and fv.note:
            st.caption(fv.note)
        label = ("Edit" if fv.is_filled else "Fill in") if rfq.status != RFQStatus.SUPPLIER_READY else "Details"
        with st.popover(label):
            _field_popover(svc, rfq, fv)


def _field_popover(svc, rfq: RFQ, fv: FieldValue) -> None:
    st.markdown("**%s**" % fv.label)
    if fv.evidence:
        st.markdown("Evidence: _“%s”_" % fv.evidence)
    msgs = svc.evidence_for(rfq, fv.source_refs)
    for m in msgs[:2]:
        st.caption("From your message (turn %d): %s" % (m.turn, (m.payload or {}).get("turn_text") or m.content))
    if "manual" in fv.source_refs:
        st.caption("Edited by you on the review page.")
    if fv.history:
        with st.expander("History (%d)" % len(fv.history)):
            for h in reversed(fv.history):
                st.caption("%s — %s (turn %s)" % (h.get("value") if h.get("value") not in (None, "") else h.get("status"), h.get("reason"), h.get("turn")))
    locked = rfq.status == RFQStatus.SUPPLIER_READY
    if locked:
        st.caption("Locked: reopen the RFQ to edit.")
        return
    st.markdown("---")
    default = fv.display_value() if fv.status == FieldStatus.PROVIDED else ""
    if fv.status == FieldStatus.CONFLICT and fv.conflict_values:
        choice = st.radio("Which value should suppliers use?", [str(c.get("value")) for c in fv.conflict_values], key="cf_%s" % fv.key)
        if st.button("Resolve", key="cfb_%s" % fv.key, type="primary"):
            svc.set_field(rfq.id, fv.key, choice, unit=fv.unit)
            state.flash("Conflict on %s resolved." % fv.label)
            st.rerun()
    new_val = st.text_input("Set value", value=default, key="ed_%s" % fv.key, placeholder="Type the buyer's requirement")
    unit = st.text_input("Unit (optional)", value=fv.unit or "", key="edu_%s" % fv.key)
    b1, b2 = st.columns(2)
    if b1.button("Save", key="edb_%s" % fv.key, type="primary"):
        try:
            svc.set_field(rfq.id, fv.key, new_val, unit=unit or None)
            state.flash("%s updated — recorded as a buyer requirement." % fv.label)
            st.rerun()
        except RFQStateError as e:
            st.error(str(e))
    if fv.status != FieldStatus.NOT_APPLICABLE:
        if b2.button("Not applicable", key="edn_%s" % fv.key):
            svc.set_field_applicability(rfq.id, fv.key, Importance.NOT_APPLICABLE, reason="Marked not applicable by buyer.")
            st.rerun()
    else:
        if b2.button("Make applicable", key="eda_%s" % fv.key):
            svc.set_field_applicability(rfq.id, fv.key, Importance.RECOMMENDED)
            st.rerun()


def _audit(svc, rfq: RFQ) -> None:
    """Diagnostics. Off by default: useful for trust, but not what a buyer came here for."""
    if not st.session_state.get(state.K_DIAGNOSTICS):
        return
    st.markdown('<div class="rfq-muted">', unsafe_allow_html=True)
    with st.expander("Diagnostics · AI calls and guard decisions", expanded=False):
        calls = svc.ai_calls(rfq.id)
        if calls:
            st.caption("AI calls (Claude Code CLI, real model reasoning)")
            st.dataframe(
                [{"turn": c.turn, "type": c.call_type, "model": c.model, "seconds": round(c.duration_ms / 1000.0, 1),
                  "ok": "yes" if c.ok else "no", "schema": "valid" if c.schema_valid else "—",
                  "prompt": c.prompt_version, "error": (c.error or "")[:80]} for c in calls],
                hide_index=True, use_container_width=True)
        notes = [m for m in svc.transcript(rfq.id) if m.kind in ("guards", "manual_edit", "status")]
        if notes:
            st.caption("Guard decisions and manual edits")
            for m in notes:
                st.text("turn %d · %s" % (m.turn, m.content))
    st.markdown('</div>', unsafe_allow_html=True)


def _actions(svc, rfq: RFQ) -> None:
    st.download_button("Export JSON", data=svc.export_json(rfq.id), file_name="%s.json" % rfq.id, mime="application/json", use_container_width=True)
    if rfq.status == RFQStatus.SUPPLIER_READY:
        st.success("Marked supplier-ready. Supplier outreach is simulated in a later phase.")
        if st.button("Reopen for editing", use_container_width=True):
            svc.reopen(rfq.id)
            st.rerun()
        return
    if rfq.completeness.ready_to_send:
        if st.button("Mark supplier-ready", type="primary", use_container_width=True):
            _confirm_ready(svc, rfq)
    else:
        st.button("Mark supplier-ready", disabled=True, use_container_width=True,
                  help="Resolve the critical items first: %s" % ", ".join(rfq.completeness.missing_required_fields[:4]))


@st.dialog("Mark this RFQ supplier-ready?")
def _confirm_ready(svc, rfq: RFQ) -> None:
    st.write("The RFQ will be locked for editing. You can reopen it later. No emails are sent in this prototype.")
    c1, c2 = st.columns(2)
    if c1.button("Yes, mark ready", type="primary", use_container_width=True):
        try:
            svc.mark_supplier_ready(rfq.id)
            state.flash("RFQ marked supplier-ready.")
            st.rerun()
        except RFQStateError as e:
            st.error(str(e))
    if c2.button("Cancel", use_container_width=True):
        st.rerun()
