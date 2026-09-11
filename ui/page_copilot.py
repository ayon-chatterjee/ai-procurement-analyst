"""Screen 1 — AI RFQ Copilot.

The screen answers one question at a time: *what does the analyst still need from me?*
So it shows the analyst's latest note and the open questions grouped by section, and
nothing else. The turn-by-turn conversation is kept for traceability but stays collapsed,
because a growing transcript pushes the actual work off the screen.
"""
from __future__ import annotations

import streamlit as st

from rfq_copilot.ai_service import AIError
from rfq_copilot.rfq_service import RFQStateError
from rfq_copilot.schema import RFQ, SECTION_LABELS, MessageRole, RFQStatus
from . import state
from .components import (
    SECTION_ORDER, collect_answers, render_question_card, render_readiness_panel, render_resume_hint, status_badge,
)
from .theme import esc

# Short chip labels; the value is what gets typed into the box for the buyer to edit.
EXAMPLES = {
    "Carton boxes": "I need corrugated carton boxes.",
    "Detailed carton request": "I need 30,000 carton boxes, 12×10×6 inches, from China, delivered to Mumbai by October 15.",
    "Steel brackets": "I need steel construction brackets.",
    "LED desk lamps": "I need LED desk lamps with a USB-C plug for the EU market.",
}

STATUS_STEPS = ["Understanding the product…", "Checking what information materially affects pricing…", "Preparing the next questions…"]


# --------------------------------------------------------------------------- #
def render() -> None:
    svc = state.get_service()
    _process_pending(svc)
    state.show_flash()
    rfq = state.current_rfq()
    if rfq is None:
        _render_hero(svc)
    else:
        _render_workspace(svc, rfq)


# --------------------------------------------------------------------------- #
def _process_pending(svc) -> None:
    action = state.take_pending()
    if not action:
        return
    with st.status("Analyzing your requirement…", expanded=True) as status:
        for step in STATUS_STEPS[:2]:
            st.write(step)
        try:
            kind = action.get("type")
            if kind == "start":
                rfq = svc.start_rfq(action["text"])
                state.set_current(rfq.id)
            elif kind == "turn":
                svc.submit_turn(action["rfq_id"], action.get("answers") or {}, action.get("skipped") or [], action.get("free_text") or "")
            elif kind == "retry":
                svc.retry_last_turn(action["rfq_id"])
            st.write(STATUS_STEPS[2])
            status.update(label="Analysis complete", state="complete", expanded=False)
            st.session_state.pop(state.K_ERROR, None)
        except AIError as e:
            # The plain-English message goes to the buyer; the technical detail is in the audit log.
            status.update(label="Analysis did not complete", state="error", expanded=False)
            st.session_state[state.K_ERROR] = e.user_message
            if getattr(e, "rfq_id", None):
                state.set_current(e.rfq_id)   # keep the buyer's request open so they can retry it
        except RFQStateError as e:
            status.update(label="Nothing to analyze", state="error", expanded=False)
            st.session_state[state.K_ERROR] = str(e)
    st.rerun()


# --------------------------------------------------------------------------- #
def _render_hero(svc) -> None:
    health = state.cached_health()
    st.markdown('<div class="rfq-hero"><h1>AI RFQ Copilot</h1><p>Tell me what you want to source. I\'ll work out what suppliers need to quote accurately.</p></div>',
                unsafe_allow_html=True)
    if not health.get("available"):
        st.error("Claude Code CLI isn't available on this machine. Install it, then run `claude` once to sign in. " + str(health.get("detail", "")))
    elif not health.get("authenticated"):
        st.error("Claude Code isn't authenticated. Run `claude` in your terminal and sign in with your Claude account.")

    if st.session_state.get(state.K_ERROR):
        st.error(st.session_state.pop(state.K_ERROR))

    st.session_state.setdefault("start_text", "")
    st.caption("Try an example")
    picked = st.pills("Try an example", list(EXAMPLES), selection_mode="single", key="example_pick", label_visibility="collapsed")
    if picked and picked != st.session_state.get("_last_example"):
        # Writing a widget's key only takes effect when the widget is created fresh, so rerun
        # before the text area below is instantiated.
        st.session_state["_last_example"] = picked
        st.session_state["start_text"] = EXAMPLES[picked]
        st.rerun()
    with st.form("start_form", border=False):
        text = st.text_area("What do you need to source?", key="start_text", height=110,
                            placeholder="e.g. I need corrugated carton boxes for shipping ceramic mugs to our Mumbai warehouse",
                            label_visibility="collapsed")
        go = st.form_submit_button("Continue", type="primary", use_container_width=False,
                                   disabled=not (health.get("available") and health.get("authenticated")))
    if go:
        if not (text or "").strip():
            st.warning("Describe what you want to source first.")
        else:
            state.queue({"type": "start", "text": text.strip()})
            st.rerun()
    st.caption("Runs on Claude Code through your Claude subscription · no API key · nothing is sent to suppliers.")

    def _open(rid):
        state.set_current(rid)
        st.rerun()
    st.markdown("")
    render_resume_hint(svc, "copilot", _open)


# --------------------------------------------------------------------------- #
def _render_workspace(svc, rfq: RFQ) -> None:
    left, right = st.columns([7, 3.6], gap="large")
    with left:
        _render_header(rfq)
        _render_analyst_note(svc, rfq)
        _render_errors(svc, rfq)
        _render_answer_surface(svc, rfq)
        _render_history(svc, rfq)
    with right:
        with st.container(border=True):
            render_readiness_panel(rfq)
            st.markdown("")
            primary = rfq.completeness.ready_to_send or rfq.status == RFQStatus.SUPPLIER_READY
            if st.button("Review RFQ", type="primary" if primary else "secondary", use_container_width=True, key="review_btn"):
                state.go("review")
            if not primary:
                st.caption("You can review the draft any time; the missing items above stay flagged.")


def _render_header(rfq: RFQ) -> None:
    c1, c2 = st.columns([5.4, 1.7])
    with c1:
        st.markdown('<div class="rfq-kicker">RFQ draft</div><div class="rfq-title">%s</div>' % esc(rfq.title or rfq.product), unsafe_allow_html=True)
        bits = [esc(x) for x in (rfq.product, rfq.category, rfq.product_type) if x]
        subtitle = " · ".join(bits) if bits else "Not classified yet"
        st.markdown('<div class="rfq-sub">%s &nbsp; %s</div>' % (subtitle, status_badge(rfq)), unsafe_allow_html=True)
    with c2:
        if st.button("New RFQ", key="new_rfq_btn", use_container_width=True):
            state.set_current(None)
            for k in ("start_text", "_last_example", "example_pick"):
                st.session_state.pop(k, None)
            st.rerun()
    st.markdown("")


def _render_analyst_note(svc, rfq: RFQ) -> None:
    """Only the analyst's latest note. Earlier turns live in the collapsed history."""
    latest = None
    for m in svc.transcript(rfq.id):
        if m.role == MessageRole.ASSISTANT and m.kind == "assistant":
            latest = m
    if latest is None:
        return
    with st.container(border=True):
        st.markdown('<div class="rfq-kicker">What I understood</div>', unsafe_allow_html=True)
        st.markdown(latest.content)


def _render_errors(svc, rfq: RFQ) -> None:
    err = st.session_state.get(state.K_ERROR)
    pending = svc.has_pending_turn(rfq)
    if not err and not pending:
        return
    st.error(err or "The last analysis didn't complete. Your answers are saved.")
    if st.button("Try again", key="retry_btn", type="primary"):
        st.session_state.pop(state.K_ERROR, None)
        state.queue({"type": "retry", "rfq_id": rfq.id})
        st.rerun()


def _render_answer_surface(svc, rfq: RFQ) -> None:
    if rfq.status == RFQStatus.SUPPLIER_READY:
        st.info("This RFQ is marked supplier-ready and locked. Reopen it from the Review page to make changes.")
        return
    if svc.has_pending_turn(rfq):
        return
    open_qs = rfq.open_questions()
    turn = rfq.turn
    with st.form("turn_form_%s_%d" % (rfq.id, turn), border=False):
        if open_qs:
            st.markdown('<div class="rfq-kicker" style="margin-top:.6rem">%d question%s that affect%s supplier pricing</div>' % (
                len(open_qs), "" if len(open_qs) == 1 else "s", "s" if len(open_qs) == 1 else ""), unsafe_allow_html=True)
            st.caption("Answer what you know. Skip anything that doesn't apply.")
            for sec in SECTION_ORDER:
                sec_qs = [q for q in open_qs if q.category == sec]
                if not sec_qs:
                    continue
                st.markdown('<div class="rfq-section-head">%s</div>' % esc(SECTION_LABELS[sec]), unsafe_allow_html=True)
                for q in sec_qs:
                    render_question_card(q, turn)
        else:
            st.success("No further questions — suppliers have what they need." if rfq.completeness.ready_to_send
                       else "No open questions right now. Fill the missing items on the right, or tell me more below.")
        st.markdown('<div class="rfq-section-head">Anything else</div>', unsafe_allow_html=True)
        st.text_area("Or just tell me in your own words", key="free_%s_%d" % (rfq.id, turn), height=90,
                     label_visibility="collapsed",
                     placeholder="e.g. They're 12 by 10 by 6 inches, 2,000 pieces each, shipping to Mumbai. Actually make the small one 3,000.")
        submitted = st.form_submit_button("Send to analyst", type="primary")
    if submitted:
        payload = collect_answers(open_qs, turn)
        free_text = str(st.session_state.get("free_%s_%d" % (rfq.id, turn)) or "").strip()
        if not payload["answers"] and not payload["skipped"] and not free_text:
            st.warning("Answer a question, skip one, or tell me something in your own words.")
            return
        state.queue({"type": "turn", "rfq_id": rfq.id, "answers": payload["answers"], "skipped": payload["skipped"], "free_text": free_text})
        st.rerun()


def _render_history(svc, rfq: RFQ) -> None:
    """Kept for traceability, collapsed so it never competes with the questions."""
    msgs = [m for m in svc.transcript(rfq.id) if m.kind in ("request", "answers", "assistant")]
    if len(msgs) <= 1:
        return
    st.markdown("")
    with st.expander("Conversation history (%d turns)" % rfq.turn, expanded=False):
        for m in msgs:
            who = "You" if m.role == MessageRole.BUYER else "Analyst"
            st.markdown('<div class="rfq-kicker" style="margin-top:.5rem">%s · turn %d</div>' % (who, m.turn), unsafe_allow_html=True)
            st.markdown(m.content)
