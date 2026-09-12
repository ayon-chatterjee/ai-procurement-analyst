"""Ask the procurement analyst.

The buyer types a question; the application works the answer out and shows it with
everything needed to check it — the table it was computed from, what was left out and
why, the assumptions in force, and the supplier's own words behind each figure.

This module renders. It calculates nothing: every number on screen comes from an
`AnalystResult` that `AnalystService` produced.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd
import streamlit as st

from rfq_copilot.ai_service import AIError
from rfq_copilot.analyst_models import AnalystResult
from rfq_copilot.analyst_service import AnalystError
from rfq_copilot.rfq_service import RFQStateError

from . import errors, state
from .components import render_no_rfq
from .theme import badge, esc

#: Kinds of exclusion that are a supplier's own choice or silence, rather than something
#: the buyer could act on. Shown, but not as a problem to fix.
NEUTRAL_CODES = {"no_response", "not_quoted", "not_quoted_explicit", "filtered_out",
                 "hypothetical_exclusion"}


def render() -> None:
    svc = state.get_service()
    sup = state.get_supplier_service()
    _process_pending()
    state.show_flash()

    rfq = state.current_rfq()
    if rfq is None:
        render_no_rfq(svc, "an", "Procurement analyst",
                      lambda rfq_id: (state.set_current(rfq_id), st.rerun()))
        return

    _header(rfq, sup)
    if not sup.has_responses(rfq.id):
        _empty_state()
        return

    _ask_box(rfq)
    _conversation(rfq, sup)
    _handoff(rfq)


def _handoff(rfq) -> None:
    """Where this page ends.

    The analyst explains and stops — `analyst_guards._AWARD_LANGUAGE` discards any
    narration that strays into recommending. That boundary is the right one, but it left
    the page with no exit: the buyer had read the answer and had nowhere to go with it.
    """
    st.markdown("---")
    c1, c2 = st.columns([2, 5])
    with c1:
        if st.button("Take a decision", use_container_width=True, key="an_to_award"):
            state.go("award")
    with c2:
        st.caption("The analyst describes what the quotes say. Choosing who gets the "
                   "business happens on the award screen, where the decision is recorded.")


# --------------------------------------------------------------------------- #
def _header(rfq, sup) -> None:
    c1, c2 = st.columns([6, 1.6])
    with c1:
        st.markdown('<div class="rfq-kicker">Procurement analyst</div>'
                    '<div class="rfq-title">%s</div>' % esc(rfq.title or rfq.product),
                    unsafe_allow_html=True)
        try:
            summary = sup.build_comparison(rfq.id).summary
            st.markdown('<div class="rfq-sub">%d line items · %d suppliers · %d responses</div>'
                        % (len(rfq.line_items), summary.get("suppliers_total", 0),
                           summary.get("responses_received", 0)), unsafe_allow_html=True)
        except RFQStateError:
            st.markdown('<div class="rfq-sub">%d line items</div>' % len(rfq.line_items),
                        unsafe_allow_html=True)
    with c2:
        store = st.session_state.get(state.K_AN_HISTORY)
        if isinstance(store, dict) and store.get(rfq.id) and st.button(
                "Clear conversation", use_container_width=True, key="an_clear"):
            store.pop(rfq.id, None)
            st.rerun()
    err = st.session_state.pop(state.K_AN_ERROR, None)
    if err:
        st.error(err)
    st.markdown("")


def _empty_state() -> None:
    with st.container(border=True):
        st.markdown('<div class="rfq-kicker">Nothing to analyze yet</div>', unsafe_allow_html=True)
        st.markdown("No supplier responses have been extracted for this RFQ, so there are "
                    "no quotes for me to work from.")
        if st.button("Go to Quotes & Comparison", type="primary", key="an_to_quotes"):
            state.go("quotes")


def _ask_box(rfq) -> None:
    st.markdown('<div class="an-kind">Ask anything about this RFQ</div>', unsafe_allow_html=True)
    asked = st.chat_input("e.g. Who is cheapest for each line? Which suppliers cleared quality?",
                          key="an_input")
    if asked:
        state.queue_analyst({"type": "ask", "rfq_id": rfq.id, "question": asked})
        st.rerun()

    svc = state.get_analyst_service()
    suggestions = [label for label, _ in svc.suggested_queries(rfq.id)]

    def on_pick() -> None:
        # Queueing and clearing happen in the callback: Streamlit refuses to let a script
        # reset a widget's own state after the widget has been drawn, but a callback may.
        label = st.session_state.get("an_suggested")
        if label:
            state.queue_analyst({"type": "suggested", "rfq_id": rfq.id, "label": label})
            st.session_state["an_suggested"] = None

    st.pills("Suggested questions", suggestions, selection_mode="single", key="an_suggested",
             label_visibility="collapsed", on_change=on_pick)
    st.caption("Answers are calculated from the same quotes the comparison shows. "
               "Nothing here changes a supplier's record.")


# --------------------------------------------------------------------------- #
def _process_pending() -> None:
    action = state.take_pending_analyst()
    if not action:
        return
    svc = state.get_analyst_service()
    rfq_id = action.get("rfq_id", "")
    display_currency = st.session_state.get(state.K_DISPLAY_CCY)

    if action.get("type") == "ask":
        question = action.get("question", "")
        with st.status("Understanding your question…", expanded=True) as status:
            try:
                result = svc.ask(rfq_id, question, display_currency=display_currency,
                                 on_stage=lambda s: st.write(s))
                _remember(rfq_id, result)
                status.update(label="Answered", state="complete", expanded=False)
            except AnalystError as e:
                status.update(label="Could not answer that", state="error", expanded=False)
                st.session_state[state.K_AN_ERROR] = str(e)
            except AIError as e:
                status.update(label="The analysis failed", state="error", expanded=False)
                st.session_state[state.K_AN_ERROR] = e.user_message
            except Exception as e:                     # pragma: no cover - last resort
                status.update(label="Something went wrong", state="error", expanded=False)
                st.session_state[state.K_AN_ERROR] = errors.message_for(e, "answering that question")
        st.rerun()

    if action.get("type") == "suggested":
        label = action.get("label", "")
        query = next((q for lbl, q in svc.suggested_queries(rfq_id) if lbl == label), None)
        if query is None:
            return
        with st.status("Checking the supplier quotes…", expanded=False) as status:
            try:
                result = svc.run_query(rfq_id, query, display_currency=display_currency,
                                       question=query.reading)
                _remember(rfq_id, result)
                status.update(label="Answered", state="complete", expanded=False)
            except (AnalystError, RFQStateError) as e:
                status.update(label="Could not answer that", state="error", expanded=False)
                st.session_state[state.K_AN_ERROR] = str(e)
        st.rerun()


def _remember(rfq_id: str, result: AnalystResult) -> None:
    """Keep the conversation as plain data.

    Streamlit cannot pickle live dataclasses across a source reload, so what is held here
    is `result.to_dict()`; the page renders from that alone.
    """
    # Kept per RFQ rather than one transcript that is thrown away whenever the open RFQ
    # changes: a buyer who steps over to the comparison, opens something else and comes
    # back used to find their questions silently gone.
    store = st.session_state.get(state.K_AN_HISTORY)
    if not isinstance(store, dict):
        store = {}
    history = store.get(rfq_id)
    if not isinstance(history, list):
        history = []
    history.append(result.to_dict())
    store[rfq_id] = history[-12:]
    st.session_state[state.K_AN_HISTORY] = store
    st.session_state[state.K_AN_RFQ] = rfq_id


# --------------------------------------------------------------------------- #
def _conversation(rfq, sup) -> None:
    store = st.session_state.get(state.K_AN_HISTORY)
    history = store.get(rfq.id) if isinstance(store, dict) else None
    if not isinstance(history, list):
        history = []
    if not history:
        st.markdown('<div class="an-empty">Ask a question above, or pick one of the '
                    'suggestions, and the answer will appear here.</div>',
                    unsafe_allow_html=True)
        return
    for index, answer in enumerate(history):
        _answer_card(answer, index, sup)


def _answer_card(answer: Dict[str, Any], index: int, sup) -> None:
    with st.container(border=True):
        question = answer.get("question") or ""
        if question:
            st.markdown('<div class="an-q">%s</div>' % esc(question), unsafe_allow_html=True)
        query = answer.get("query") or {}
        reading = query.get("reading") or ""
        if reading and reading != question:
            st.markdown('<div class="an-reading">Read as: %s</div>' % esc(reading),
                        unsafe_allow_html=True)

        if answer.get("refused"):
            st.markdown('<div class="an-refusal">%s</div>' % esc(answer.get("summary", "")),
                        unsafe_allow_html=True)
            return

        if answer.get("hypothetical"):
            labels = "; ".join(answer.get("hypothetical_labels") or [])
            st.markdown('<div class="an-hyp"><b>What-if.</b> This answer assumes %s. '
                        'Nothing in the supplier records has changed.</div>' % esc(labels),
                        unsafe_allow_html=True)

        st.markdown('<div class="an-answer">%s</div>'
                    % esc(answer.get("explanation") or answer.get("summary") or ""),
                    unsafe_allow_html=True)
        if answer.get("explanation") and answer.get("summary"):
            st.caption(answer["summary"])

        _table(answer, index)
        _metrics(answer)
        for warning in answer.get("warnings") or []:
            st.warning(warning, icon=":material/info:")
        _exclusions(answer, index)
        _method(answer)
        _evidence(answer, index, sup)


def _frame(answer: Dict[str, Any]) -> Optional[pd.DataFrame]:
    """A table whose columns are each one type.

    An analyst column legitimately mixes a number with a placeholder — a price on one
    line, "—" on the next because nobody quoted it — and Arrow cannot serialise that. The
    dash has to stay (a blank would read as zero), so a column holding both is rendered
    as text throughout.
    """
    rows = answer.get("rows") or []
    if not rows:
        return None
    columns = [c for c in (answer.get("columns") or []) if any(c in r for r in rows)]
    if not columns:
        columns = list(rows[0].keys())

    shaped: List[Dict[str, Any]] = []
    text_columns = set()
    for column in columns:
        values = [r.get(column) for r in rows]
        numeric = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
        other = [v for v in values if v is not None and v not in ("", None)
                 and not (isinstance(v, (int, float)) and not isinstance(v, bool))]
        if numeric and other:
            text_columns.add(column)
    for row in rows:
        shaped.append({c: (_as_text(row.get(c)) if c in text_columns else row.get(c, ""))
                       for c in columns})
    return pd.DataFrame(shaped, columns=columns)


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return ("%.4f" % value).rstrip("0").rstrip(".")
    return str(value)


def _table(answer: Dict[str, Any], index: int) -> None:
    frame = _frame(answer)
    if frame is None:
        return
    st.dataframe(frame, hide_index=True, use_container_width=True)
    if len(frame) > 8:
        st.caption("%d rows. Scroll sideways for the remaining columns." % len(frame))
    st.download_button("Export this result (CSV)", data=frame.to_csv(index=False),
                       file_name="analyst_%s_%d.csv" % (answer.get("intent", "result"), index),
                       mime="text/csv", key="an_csv_%d" % index)


def _metrics(answer: Dict[str, Any]) -> None:
    metrics = {k: v for k, v in (answer.get("metrics") or {}).items()
               if k not in ("supplier_names", "out_of_scope_names")
               and isinstance(v, (int, float, str)) and not isinstance(v, bool)}
    if not metrics:
        return
    items = list(metrics.items())[:4]
    columns = st.columns(len(items))
    for column, (key, value) in zip(columns, items):
        with column:
            st.metric(key.replace("_", " ").capitalize(), value)


def _exclusions(answer: Dict[str, Any], index: int) -> None:
    exclusions = answer.get("exclusions") or []
    if not exclusions:
        return
    actionable = [e for e in exclusions if e.get("code") not in NEUTRAL_CODES]
    label = "Left out of this answer (%d)" % len(exclusions)
    with st.expander(label, expanded=False):
        st.caption("Nothing is dropped silently. Each row says who was left out of which "
                   "line and why.")
        rows = [{"Supplier": e.get("supplier_name", ""), "Line": e.get("line_id") or "—",
                 "Why": e.get("reason", ""),
                 "Kind": (e.get("code") or "").replace("_", " ")} for e in exclusions]
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        if actionable:
            st.caption("%d of these could change if the supplier supplied more information."
                       % len(actionable))


def _method(answer: Dict[str, Any]) -> None:
    notes = answer.get("calculation_notes") or []
    assumptions = answer.get("assumptions") or []
    rates = answer.get("rate_provenance") or {}
    pairs = rates.get("pairs") or []
    if not (notes or assumptions or pairs):
        return
    with st.expander("How this was calculated", expanded=False):
        if notes:
            st.markdown('<div class="an-kind">Steps</div>', unsafe_allow_html=True)
            for note in notes:
                st.markdown('<div class="an-note">· %s</div>' % esc(note), unsafe_allow_html=True)
        if assumptions:
            st.markdown('<div class="an-kind">Assumptions</div>', unsafe_allow_html=True)
            for item in assumptions:
                st.markdown('<div class="an-note">· %s</div>' % esc(item), unsafe_allow_html=True)
        if pairs:
            st.markdown('<div class="an-kind">Exchange rates used</div>', unsafe_allow_html=True)
            for pair in pairs:
                st.markdown('<div class="an-note">· %s</div>' % esc(pair), unsafe_allow_html=True)


def _evidence(answer: Dict[str, Any], index: int, sup) -> None:
    refs = answer.get("evidence_refs") or []
    if not refs:
        return
    st.markdown('<div class="an-kind">Evidence</div>', unsafe_allow_html=True)
    columns = st.columns(min(3, len(refs)))
    for position, ref in enumerate(refs[:9]):
        with columns[position % len(columns)]:
            title = "%s · %s" % (ref.get("supplier_name", ""), ref.get("topic", "").replace("_", " "))
            with st.popover(title[:38], use_container_width=True):
                st.markdown('<div class="pg-label">%s</div>'
                            % esc(ref.get("location") or "no recorded location"),
                            unsafe_allow_html=True)
                quoted = ref.get("quoted_text") or ""
                if quoted:
                    st.markdown('<div class="pg-span">“%s”</div>' % esc(quoted[:400]),
                                unsafe_allow_html=True)
                else:
                    st.caption("No source location was recorded for this value — the "
                               "wording is what the supplier wrote, but where in the "
                               "document it appeared was not captured.")
                st.markdown(badge("buyer" if ref.get("verified") else "recommended",
                                  "found in the document" if ref.get("verified")
                                  else "not located in the document"), unsafe_allow_html=True)
    if len(refs) > 9:
        st.caption("%d more evidence spans are attached to this answer." % (len(refs) - 9))
