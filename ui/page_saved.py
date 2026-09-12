"""Screen 3 — Saved RFQs: everything persisted in SQLite, reopen or delete."""
from __future__ import annotations

import streamlit as st

from rfq_copilot import labels
from rfq_copilot.schema import RFQSummary
from . import state
from .theme import badge, esc

#: Keyed by the enum value so it is the same table the rest of the app reads. The list
#: used to say "Draft" where every other screen said "Not ready" for the same RFQ.
STATUS_BADGE = labels.RFQ_STATUS

#: The award, when there is one, in the same vocabulary the award screen uses. Deliberately
#: one badge and no controls: this list reopens work, it does not manage awards.
AWARD_BADGE = labels.AWARD_STATUS_IN_LIST


def render() -> None:
    svc = state.get_service()
    state.show_flash()
    st.markdown('<div class="rfq-hero"><h1>Saved RFQs</h1><p>Every RFQ is saved locally as you work. Reopen one to continue.</p></div>',
                unsafe_allow_html=True)
    rows = svc.list_rfqs()
    awards = state.get_award_service().statuses()
    top = st.columns([1.4, 5])
    with top[0]:
        if st.button("New RFQ", type="primary", use_container_width=True):
            state.set_current(None)
            state.go("copilot")
    with top[1]:
        if rows:
            st.caption("%d saved RFQ%s" % (len(rows), "" if len(rows) == 1 else "s"))
    if not rows:
        st.info("No saved RFQs yet. Start one in the Copilot.")
        return
    st.markdown("")
    current = st.session_state.get(state.K_RFQ_ID)
    for r in rows:
        _row(svc, r, is_current=(r.id == current), award_status=awards.get(r.id))


def _row(svc, r: RFQSummary, is_current: bool, award_status: str = None) -> None:
    kind, label = labels.label_for(STATUS_BADGE, r.status.value, "status-not")
    with st.container(border=True):
        c1, c2, c3, c4 = st.columns([5, 2.2, 1.5, 1.5])
        with c1:
            st.markdown('<div class="rfq-title" style="font-size:1.05rem">%s</div>' % esc(r.title or r.product or r.id), unsafe_allow_html=True)
            bits = [esc(r.product or "Unclassified")]
            if r.category:
                bits.append(esc(r.category))
            bits.append("%d line item%s" % (r.line_item_count, "" if r.line_item_count == 1 else "s"))
            aw_kind, aw_label = AWARD_BADGE.get(award_status or "", (None, None))
            st.markdown('<div class="rfq-sub">%s &nbsp; %s%s%s</div>' % (
                " · ".join(bits), badge(kind, label),
                badge(aw_kind, aw_label) if aw_kind else "",
                badge("buyer", "Open now") if is_current else ""), unsafe_allow_html=True)
            st.caption("Updated %s · turn %d" % (r.updated_at.replace("T", " ")[:16], r.turn))
        with c2:
            st.progress(min(100, max(0, r.readiness_score)) / 100.0, text="%d%% complete" % r.readiness_score)
        with c3:
            if st.button("Open", key="open_%s" % r.id, type="primary", use_container_width=True):
                state.set_current(r.id)
                state.go("copilot")
        with c4:
            if st.button("Review", key="rev_%s" % r.id, use_container_width=True):
                state.set_current(r.id)
                state.go("review")
            if award_status:
                if st.button("Award", key="awd_%s" % r.id, use_container_width=True):
                    state.set_current(r.id)
                    state.go("award")
            if st.button("Delete", key="del_%s" % r.id, use_container_width=True):
                _confirm_delete(svc, r.id, r.title or r.product or r.id)


@st.dialog("Delete this RFQ?")
def _confirm_delete(svc, rid: str, title: str) -> None:
    st.write("**%s**" % title)
    st.write("This removes the RFQ, its conversation, its audit log and any award made "
             "against it from the local database. This cannot be undone.")
    c1, c2 = st.columns(2)
    if c1.button("Delete", type="primary", use_container_width=True):
        svc.delete(rid)
        if st.session_state.get(state.K_RFQ_ID) == rid:
            state.set_current(None)
        state.flash("RFQ deleted.", "info")
        st.rerun()
    if c2.button("Cancel", use_container_width=True):
        st.rerun()
