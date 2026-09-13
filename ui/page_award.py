"""Award and execution.

The page reads as one sentence: here is the decision, here is what will happen, review it,
execute it. Four sections stacked down the page rather than tabs — a tab lets a buyer
approve something they never scrolled past, and the consequence has to be visible above
the button that commits to it.

This module renders. Every figure on screen was computed by `award_calculations`; every
letter was checked by `award_guards`.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd
import streamlit as st

from rfq_copilot import labels
from rfq_copilot.ai_service import AIError
from rfq_copilot.award_models import (
    AwardStatus, AwardThresholds, CommunicationStatus, NOT_AVAILABLE, NOT_PROVIDED,
    PickSource, Severity,
)
from rfq_copilot.award_prompts import line_table
from rfq_copilot.award_service import AwardError
from rfq_copilot.rfq_service import RFQStateError

from . import errors, state
from .components import render_no_rfq
from .theme import badge, esc

#: What the primary button says at each stage. The label is the next thing that happens,
#: never a generic "Continue".
NEXT_ACTION = {
    AwardStatus.DRAFT.value: ("Approve this award", "approve"),
    AwardStatus.REVIEWED.value: ("Approve this award", "approve"),
    AwardStatus.APPROVED.value: ("Prepare supplier messages", "draft"),
    AwardStatus.READY_TO_EXECUTE.value: ("Record that you sent these", None),
    AwardStatus.SUPPLIER_NOTIFIED.value: ("Generate order handoff", "handoff"),
    AwardStatus.ORDER_HANDOFF.value: ("Close this award", "complete"),
}

#: One table, shared with the saved-RFQ list, so an award does not read "ready to execute"
#: on its own page and "Messages ready" in the list.
STATUS_BADGE = labels.AWARD_STATUS


def render() -> None:
    svc = state.get_service()
    sup = state.get_supplier_service()
    _process_pending()
    state.show_flash()

    rfq = state.current_rfq()
    if rfq is None:
        render_no_rfq(svc, "aw", "Award & execution",
                      lambda rfq_id: (state.set_current(rfq_id), st.rerun()))
        return

    award_svc = state.get_award_service()
    if not sup.has_responses(rfq.id):
        _header(rfq, None)
        _empty_state()
        return

    try:
        award = award_svc.current(rfq.id)
    except AwardError as e:
        _header(rfq, None)
        st.error(str(e))
        return

    _header(rfq, award)
    if award is None:
        _start_panel(rfq, award_svc)
        return

    try:
        report = award_svc.validate(award.id)
        totals = award_svc.totals(award)
        proposal = award_svc.proposal(rfq.id, award.thresholds, award.currency)
    except (AwardError, RFQStateError) as e:
        st.error(str(e))
        return

    _decision(award, proposal, award_svc, totals)
    _consequence(award, totals, report, proposal, award_svc)
    _execute(award, report, award_svc)


# --------------------------------------------------------------------------- #
def _header(rfq, award) -> None:
    c1, c2 = st.columns([6, 1.8])
    with c1:
        st.markdown('<div class="rfq-kicker">Award &amp; execution</div>'
                    '<div class="rfq-title">%s</div>' % esc(rfq.title or rfq.product),
                    unsafe_allow_html=True)
        if award is not None:
            kind, label = STATUS_BADGE.get(award.status, ("unknown", award.status))
            st.markdown('<div class="rfq-sub">%s &nbsp; %d of %d lines awarded to %d '
                        'supplier%s</div>'
                        % (badge(kind, label), len(award.awarded_lines), len(award.lines),
                           len(award.supplier_ids),
                           "" if len(award.supplier_ids) == 1 else "s"),
                        unsafe_allow_html=True)
        else:
            st.markdown('<div class="rfq-sub">%d line items</div>' % len(rfq.line_items),
                        unsafe_allow_html=True)
    with c2:
        if award is not None:
            _history(award, state.get_award_service())
    err = st.session_state.pop(state.K_AW_ERROR, None)
    if err:
        st.error(err)
    st.markdown("")


def _empty_state() -> None:
    with st.container(border=True):
        st.markdown('<div class="rfq-kicker">Nothing to award yet</div>',
                    unsafe_allow_html=True)
        st.markdown("No supplier responses have been extracted for this RFQ, so there are "
                    "no quotes to award against.")
        if st.button("Go to Quotes & Comparison", type="primary", key="aw_to_quotes"):
            state.go("quotes")


def _start_panel(rfq, award_svc) -> None:
    # A finished award is history, not a blocker: it keeps its record and its trail, and
    # the buyer can still award this RFQ again. Showing it here is what stops completing an
    # award from making it vanish from the screen that made it.
    closed = award_svc.last_closed(rfq.id)
    with st.container(border=True):
        st.markdown('<div class="aw-step-title">%s</div>'
                    % ("Award this RFQ again" if closed else "Take a decision on this RFQ"),
                    unsafe_allow_html=True)
        st.markdown('<div class="aw-step-sub">Every line will be seeded with two proposals '
                    '— the cheapest comparable quote, and the cheapest that also clears '
                    'the quality bar. You change any line you like.</div>',
                    unsafe_allow_html=True)
        if st.button("Start the award", type="primary", key="aw_start"):
            state.queue_award({"type": "start", "rfq_id": rfq.id})
            st.rerun()

    if closed:
        with st.container(border=True):
            label = "completed" if closed.status == AwardStatus.COMPLETED.value else "cancelled"
            st.markdown('<div class="aw-step-title">The previous award</div>',
                        unsafe_allow_html=True)
            st.markdown('<div class="aw-step-sub">%s on %s — %s. It stays on the record, '
                        'and starting a new one does not change it.</div>'
                        % (label.capitalize(), esc(closed.decided_at[:10] or
                                                   closed.created_at[:10]),
                           esc(award_svc.totals(closed).describe())),
                        unsafe_allow_html=True)
            _history(closed, award_svc)


def _history(award, award_svc) -> None:
    with st.popover("History", use_container_width=True):
        st.markdown('<div class="aw-step-sub">Every change, oldest first. Nothing here is '
                    'overwritten.</div>', unsafe_allow_html=True)
        for event in award_svc.events(award.id):
            st.markdown('<div class="aw-event">%s</div>'
                        '<div class="aw-event-at">%s · %s</div>'
                        % (esc(event.summary), esc(event.at[:16].replace("T", " ")),
                           esc(event.actor)), unsafe_allow_html=True)


# --------------------------------------------------------------------------- #
# 1 — the decision
# --------------------------------------------------------------------------- #
def _quality_bar(award) -> None:
    """The one thing the buyer sets, beside the table it changes.

    This was a "Step 1" panel of its own with a lead-time limit nobody used. A settings
    step before the decision made the decision feel gated; the single switch that actually
    changes the table belongs next to the table.
    """
    if not award.editable:
        st.caption("Settled when the award was approved: %s" % award.thresholds.describe())
        return
    strict = st.toggle("Best value requires a certificate we hold a copy of",
                       value=award.thresholds.require_document_backed_certification,
                       key="aw_docs",
                       help="Off: a supplier who states a certificate counts. On: only a "
                            "certificate among the documents we actually received. Either "
                            "way, cheapest is unaffected and every price stays on screen.")
    if strict != award.thresholds.require_document_backed_certification:
        state.queue_award({"type": "thresholds", "award_id": award.id,
                           "require_docs": bool(strict)})
        st.rerun()


def _decision(award, proposal, award_svc, totals) -> None:
    overrides = [l for l in award.lines if l.is_override]
    st.markdown('<div class="aw-step">Step 1 · The decision</div>'
                '<div class="aw-step-title">Who gets each line</div>',
                unsafe_allow_html=True)
    st.markdown('<div class="aw-step-sub">Each line offers the two proposals the system '
                'calculated. Pick one, or use <b>Award a line to someone else</b> below. '
                'The ⓘ shows every supplier who priced the line, and why the pick won. '
                '%s</div>'
                % ("You have changed %d line%s." % (len(overrides), "" if len(overrides) == 1 else "s")
                   if overrides else "Nothing is committed until you approve."),
                unsafe_allow_html=True)

    c1, c2 = st.columns([3, 2])
    with c1:
        _quality_bar(award)
    with c2:
        if proposal.lines_with_no_best_value:
            st.caption("%d of %d lines have no supplier that meets the bar; those offer "
                       "the cheapest quote only."
                       % (len(proposal.lines_with_no_best_value), len(proposal.lines)))

    head = st.columns(_GRID)
    for col, title in zip(head, ("Line", "Item", "Qty", "Best value", "Cheapest",
                                 "Your pick", "Unit price", "Total")):
        col.markdown('<div class="aw-head">%s</div>' % title, unsafe_allow_html=True)
    st.markdown('<div class="aw-rule"></div>', unsafe_allow_html=True)

    for line in award.lines:
        _decision_row(award, proposal.line(line.line_item_id), line)

    st.markdown('<div class="aw-rule"></div>', unsafe_allow_html=True)
    _footer(award, proposal, totals)

    if award.editable:
        _change_line(award, proposal, award_svc)


#: Column widths, shared by the header and every row so they line up.
_GRID = [0.7, 2.6, 0.6, 2.3, 2.3, 1.9, 1.5, 1.2]


def _decision_row(award, entry, line) -> None:
    """One line item: the two proposals, the chooser, and what it costs."""
    cols = st.columns(_GRID)
    cols[0].markdown('<div class="aw-cell">%s</div>' % esc(line.line_item_id),
                     unsafe_allow_html=True)
    cols[1].markdown('<div class="aw-cell">%s</div>'
                     % esc(line.line_label or NOT_AVAILABLE), unsafe_allow_html=True)
    cols[2].markdown('<div class="aw-cell">%s</div>'
                     % (_text(line.quantity) if line.quantity is not None else NOT_AVAILABLE),
                     unsafe_allow_html=True)

    _proposal_cell(cols[3], entry, line, PickSource.BEST_VALUE.value, award.currency)
    _proposal_cell(cols[4], entry, line, PickSource.CHEAPEST.value, award.currency)
    _pick_cell(cols[5], award, entry, line)

    with cols[6]:
        st.markdown('<div class="aw-cell">%s</div>'
                    % (_fmt(line.unit_price, award.currency) if line.unit_price is not None
                       else NOT_AVAILABLE), unsafe_allow_html=True)
        if line.native_text:
            st.markdown('<div class="aw-cell-sub">%s</div>' % esc(line.native_text),
                        unsafe_allow_html=True)
    cols[7].markdown('<div class="aw-cell">%s</div>'
                     % (_fmt(line.extended, award.currency) if line.extended is not None
                        else NOT_AVAILABLE), unsafe_allow_html=True)
    st.markdown('<div class="aw-rule"></div>', unsafe_allow_html=True)


def _proposal_cell(col, entry, line, source: str, currency) -> None:
    """A proposal's supplier and price, with the ⓘ that justifies it."""
    candidate = entry.candidate(source) if entry else None
    with col:
        if candidate is None:
            st.markdown('<div class="aw-cell-none">%s</div>'
                        % ("none meets the bar" if source == PickSource.BEST_VALUE.value
                           else "no comparable price"), unsafe_allow_html=True)
        else:
            st.markdown('<div class="aw-cell"><b>%s</b></div>'
                        '<div class="aw-cell-sub">%s</div>'
                        % (esc(candidate.supplier_name), _fmt(candidate.amount, currency)),
                        unsafe_allow_html=True)
        if entry is not None:
            with st.popover("ⓘ", use_container_width=False):
                _comparison(entry, source, currency)


def _pick_cell(col, award, entry, line) -> None:
    """The chooser. Present only where there is a genuine choice to make."""
    with col:
        if line.is_override:
            st.markdown('<div class="aw-cell"><b>You</b></div>'
                        '<div class="aw-cell-sub">%s</div>'
                        % esc(line.supplier_name or "no award"), unsafe_allow_html=True)
            return
        if entry is None or entry.cheapest is None:
            st.markdown('<div class="aw-cell-none">nothing to pick</div>',
                        unsafe_allow_html=True)
            return
        if entry.same_supplier:
            st.markdown('<div class="aw-cell-sub">same supplier either way</div>',
                        unsafe_allow_html=True)
            return
        if not award.editable:
            st.markdown('<div class="aw-cell">%s</div>' % esc(_decided_by(line)),
                        unsafe_allow_html=True)
            return

        options = ["Best value", "Cheapest"] if entry.best_value else ["Cheapest"]
        current = "Best value" if line.pick_source == PickSource.BEST_VALUE.value else "Cheapest"
        # The key carries the seed, so a re-seed or a change of the quality bar rebuilds
        # the control against the new server state instead of holding a stale selection.
        key = "aw_pick_%s_%s_%s" % (line.line_item_id, line.supplier_id or "none",
                                    line.pick_source)
        st.segmented_control("Pick", options, default=current if current in options else None,
                             key=key, label_visibility="collapsed",
                             on_change=_queue_pick, args=(award.id, line.line_item_id, key))


def _queue_pick(award_id: str, line_item_id: str, key: str) -> None:
    """Queue the chosen proposal. Inside `on_change`, because Streamlit refuses a write to
    a widget's own key once the widget exists."""
    picked = st.session_state.get(key)
    if not picked:
        return
    state.queue_award({
        "type": "set_line", "award_id": award_id, "line_item_id": line_item_id,
        "choice": (PickSource.BEST_VALUE.value if picked == "Best value"
                   else PickSource.CHEAPEST.value), "reason": ""})


def _footer(award, proposal, totals) -> None:
    """What the decision above adds up to."""
    from rfq_copilot.award_calculations import basket_delta

    left, right = st.columns([2.6, 3])
    with left:
        st.markdown('<div class="aw-cell"><b>%s</b></div>'
                    '<div class="aw-cell-sub">across %d line%s to %d supplier%s</div>'
                    % (esc(totals.describe()), len(award.awarded_lines),
                       "" if len(award.awarded_lines) == 1 else "s",
                       len(award.supplier_ids),
                       "" if len(award.supplier_ids) == 1 else "s"),
                    unsafe_allow_html=True)
    with right:
        note = basket_delta(proposal).get("note")
        if note:
            st.markdown('<div class="aw-cell-sub">%s</div>' % esc(note),
                        unsafe_allow_html=True)


# --------------------------------------------------------------------------- #
# Why this one? — the whole field for a line, not an assertion about two of it
# --------------------------------------------------------------------------- #
_QUALIFICATION_WORD = {
    "cleared": "on file",
    "unverified": "stated",
    "not_cleared": "not cleared",
    "not_assessed": "none on file",
}


def _certificate_word(candidate) -> str:
    """What this supplier's certificate actually is, not what the bar concluded.

    The two differ: a supplier whose only failing check is a certificate it states but
    cannot evidence is NOT_CLEARED overall, yet passes the relaxed bar. Printing the
    qualification verdict put "not cleared" in the certificate column of the row marked
    best value, which reads as a contradiction and undersells a supplier that did state one.
    """
    if candidate.qualification == "not_cleared" and candidate.meets_bar:
        return "stated"
    return _QUALIFICATION_WORD.get(candidate.qualification, candidate.qualification or "—")


def _comparison(entry, source: str, currency) -> None:
    """Every supplier who priced this line, and what separates them.

    A markdown table rather than `st.dataframe`: two of these per row over thirty rows is
    sixty Arrow grids per render, which is the one thing that would make this page slow.
    """
    pick = entry.candidate(source)
    label = "Best value" if source == PickSource.BEST_VALUE.value else "Cheapest"
    st.markdown("**%s · %s**" % (label, esc(entry.line_item_id)))

    if pick is None:
        st.markdown(esc(entry.absent_reason or "No supplier has a comparable price here."))
    elif source == PickSource.BEST_VALUE.value:
        st.markdown("**%s** at %s — the lowest price from a supplier that meets the "
                    "quality bar.%s" % (esc(pick.supplier_name), _fmt(pick.amount, currency),
                                        " " + esc(entry.difference_note)
                                        if entry.difference_note else ""))
    else:
        st.markdown("**%s** at %s — the lowest price the application will compare on this "
                    "line." % (esc(pick.supplier_name), _fmt(pick.amount, currency)))

    rows = ["| Supplier | Price (%s) | As quoted | Certificate | Validity | Lead time | |"
            % (currency or "—"),
            "|---|---:|---|---|---|---|---|"]
    for c in entry.candidates:
        marks = []
        if entry.cheapest and c.supplier_id == entry.cheapest.supplier_id:
            marks.append("cheapest")
        if entry.best_value and c.supplier_id == entry.best_value.supplier_id:
            marks.append("**best value**")
        if not c.meets_bar and c.bar_reason:
            marks.append(c.bar_reason)
        rows.append("| %s | %s | %s | %s | %s | %s | %s |" % (
            _cell(c.supplier_name), _text(c.amount), _cell(c.native),
            _certificate_word(c),
            "conditional" if c.validity_is_conditional else "firm",
            ("%g d" % c.lead_time_days) if c.lead_time_days is not None else "—",
            _cell("; ".join(marks))))
    for ex in entry.exclusions:
        rows.append("| %s | — | — | — | — | — | %s |"
                    % (_cell(ex.supplier_name), _cell(ex.reason)))
    st.markdown("\n".join(rows))
    st.caption("Cheapest is the lowest price the application is willing to compare. Best "
               "value is the cheapest of those whose supplier also meets the quality bar.")


def _cell(text) -> str:
    """Markdown-table-safe: a pipe in a supplier's name would split the row."""
    return esc(str(text or "—")).replace("|", "\\|").replace("\n", " ")


#: The one column that answers "is this a recommendation or my decision?". It was
#: previously the raw `pick_source` value — "best value", "buyer override" — which names
#: the mechanism rather than the author.
DECIDED_BY = {
    PickSource.CHEAPEST.value: "System · cheapest",
    PickSource.BEST_VALUE.value: "System · best value",
    PickSource.BUYER_OVERRIDE.value: "You",
    PickSource.NONE.value: "No award",
}


def _decided_by(line) -> str:
    return DECIDED_BY.get(line.pick_source, line.pick_source.replace("_", " "))


def _why(line, entry) -> str:
    if not line.awarded:
        return line.absent_reason or "not awarded"
    if line.is_override:
        return "your decision: %s" % (line.override_reason or "no reason given")
    if entry is not None and entry.difference_note:
        return entry.difference_note
    if line.pick_source == PickSource.BEST_VALUE.value:
        return "cheapest quote that meets the quality bar"
    return "cheapest comparable quote"


def _change_line(award, proposal, award_svc) -> None:
    labels = ["%s · %s" % (l.line_item_id, l.line_label or "") for l in award.lines]
    with st.expander("Award a line to someone else", expanded=False):
        picked = st.selectbox("Line", labels, key="aw_line_pick")
        line_id = picked.split(" · ")[0]
        entry = proposal.line(line_id)
        line = award.line(line_id)
        if entry is None or line is None:
            return

        # Cheapest and best value are chosen in the row itself; this is only for the
        # cases the row cannot express — a third supplier, or awarding nobody.
        others = _other_suppliers(award_svc, award, entry)
        options = ["%s (your choice)" % name for _, name in others]
        options.append("Do not award this line")

        choice = st.radio("Award to", options, key="aw_choice_%s" % line_id)
        reason = ""
        if "(your choice)" in choice:
            reason = st.text_input("Why this supplier?", key="aw_reason_%s" % line_id,
                                   placeholder="Recorded on the award and in the history")
        if st.button("Apply to %s" % line_id, type="primary", key="aw_setline_%s" % line_id):
            state.queue_award({"type": "set_line", "award_id": award.id,
                               "line_item_id": line_id,
                               "choice": _choice_value(choice, others), "reason": reason})
            st.rerun()


def _other_suppliers(award_svc, award, entry) -> List:
    """Suppliers with a usable price on this line, beyond the two proposals."""
    seen = {c.supplier_id for c in (entry.cheapest, entry.best_value) if c}
    try:
        eligible = award_svc.eligible_suppliers(award.id, entry.line_item_id)
    except AwardError:
        return []
    return [(sid, name) for sid, name in eligible if sid not in seen]


def _choice_value(choice: str, others) -> str:
    if choice.startswith("Cheapest"):
        return PickSource.CHEAPEST.value
    if choice.startswith("Best value"):
        return PickSource.BEST_VALUE.value
    if choice.startswith("Do not award"):
        return PickSource.NONE.value
    name = choice.replace(" (your choice)", "")
    return next((sid for sid, other in others if other == name), PickSource.NONE.value)


# --------------------------------------------------------------------------- #
# 3 — what will happen
# --------------------------------------------------------------------------- #
def _consequence(award, totals, report, proposal, award_svc) -> None:
    st.markdown('<div class="aw-step">Step 2 · What will happen</div>'
                '<div class="aw-step-title">%s</div>'
                '<div class="aw-step-sub">across %d lines to %d supplier%s</div>'
                % (esc(totals.describe()), len(award.awarded_lines),
                   len(award.supplier_ids), "" if len(award.supplier_ids) == 1 else "s"),
                unsafe_allow_html=True)

    from rfq_copilot.award_calculations import basket_delta
    delta = basket_delta(proposal)
    if delta.get("note"):
        st.markdown('<div class="aw-delta">%s</div>' % esc(delta["note"]),
                    unsafe_allow_html=True)

    if totals.by_supplier:
        rows = []
        for sub in totals.by_supplier:
            rows.append({
                "Supplier": sub.supplier_name, "Lines": sub.lines,
                "Subtotal (%s)" % (totals.currency or ""): sub.subtotal
                if sub.subtotal is not None else NOT_AVAILABLE,
                "As quoted": ("%s %s" % (sub.native_currency,
                                         "{:,.2f}".format(sub.native_subtotal)))
                if sub.native_subtotal is not None else NOT_AVAILABLE,
            })
        st.dataframe(_typed(pd.DataFrame(rows)), hide_index=True, use_container_width=True)
    for note in totals.notes:
        st.caption(note)

    _findings(award, report)
    _method(award, totals)


def _findings(award, report) -> None:
    """Everything the checks found, as notes.

    These used to be three groups with a red block at the top and a checkbox beside every
    warning. Both were theatre: the red block refused suppliers the system had proposed,
    and ticking a box is not the same as reading a sentence. What the buyer was looking at
    is recorded with the approval either way.
    """
    notes = list(report.warnings) + list(report.infos)
    if not notes:
        return
    with st.expander("Notes — %d" % len(notes), expanded=False):
        st.caption("None of these stops the award. They are recorded with it, so what you "
                   "were looking at when you committed is on the record.")
        for finding in report.warnings:
            st.markdown('<div class="aw-why">· %s</div>' % esc(finding.message),
                        unsafe_allow_html=True)
        for finding in report.infos:
            st.markdown('<div class="rfq-muted" style="font-size:.84rem">· %s</div>'
                        % esc(finding.message), unsafe_allow_html=True)


def _method(award, totals) -> None:
    with st.expander("How this was worked out", expanded=False):
        st.markdown('<div class="an-kind">Assumptions</div>', unsafe_allow_html=True)
        for item in award.assumptions:
            st.markdown('<div class="an-note">· %s</div>' % esc(item), unsafe_allow_html=True)
        pairs = (award.rate_provenance or {}).get("pairs") or []
        if pairs:
            st.markdown('<div class="an-kind">Exchange rates used</div>',
                        unsafe_allow_html=True)
            for pair in pairs:
                st.markdown('<div class="an-note">· %s</div>' % esc(pair),
                            unsafe_allow_html=True)
        st.markdown('<div class="an-kind">Totals</div>', unsafe_allow_html=True)
        st.markdown('<div class="an-note">· Each line is its quantity times its unit price, '
                    'rounded once. Subtotals add those rounded figures, so the column '
                    'adds up to the footer.</div>', unsafe_allow_html=True)
        for note in totals.notes:
            st.markdown('<div class="an-note">· %s</div>' % esc(note), unsafe_allow_html=True)


# --------------------------------------------------------------------------- #
# 4 — execute
# --------------------------------------------------------------------------- #
def _execute(award, report, award_svc) -> None:
    st.markdown('<div class="aw-step">Step 3 · Execute</div>', unsafe_allow_html=True)

    if award.status == AwardStatus.COMPLETED.value:
        st.success("This award is complete. The history above has the whole journey.")
    elif award.status == AwardStatus.CANCELLED.value:
        st.info("This award was cancelled: %s" % award.cancelled_reason)
        return

    _pending_line(award, award_svc)

    label, action = NEXT_ACTION.get(award.status, (None, None))
    if label and action:
        # The only thing that stops a buyer here is an award that could not mean anything:
        # nothing chosen, a line awarded twice, a line that is not on this RFQ. Notes are
        # notes.
        disabled = bool(report.blocking) and action == "approve"
        help_text = report.blocking[0].message if disabled else None
        c1, c2 = st.columns([2, 5])
        with c1:
            if st.button(label, type="primary", disabled=disabled, use_container_width=True,
                         key="aw_primary", help=help_text):
                state.queue_award({"type": action, "award_id": award.id})
                st.rerun()
        with c2:
            if disabled and help_text:
                st.caption(help_text)

    if award.status in (AwardStatus.READY_TO_EXECUTE.value,
                        AwardStatus.SUPPLIER_NOTIFIED.value,
                        AwardStatus.ORDER_HANDOFF.value,
                        AwardStatus.COMPLETED.value):
        _communications(award, award_svc)
    if award.status in (AwardStatus.ORDER_HANDOFF.value, AwardStatus.COMPLETED.value):
        _handoffs(award, award_svc)

    if award.editable or award.status == AwardStatus.APPROVED.value:
        with st.expander("Cancel this award", expanded=False):
            reason = st.text_input("Why?", key="aw_cancel_reason")
            if st.button("Cancel award", key="aw_cancel", disabled=not reason.strip()):
                state.queue_award({"type": "cancel", "award_id": award.id, "reason": reason})
                st.rerun()


def _pending_line(award, award_svc) -> None:
    """What is still outstanding, at every status.

    At READY_TO_EXECUTE the primary button disappears — the next move is per-supplier,
    inside the message cards below — and the screen said nothing about what remained. A
    buyer reading it had no way to tell "finished" from "waiting on me".
    """
    status = award.status
    if status in (AwardStatus.DRAFT.value, AwardStatus.REVIEWED.value):
        st.caption("Nothing has been committed yet. Approving fixes the lines above; "
                   "the messages and the order handoff come after that.")
        return
    if status == AwardStatus.CANCELLED.value:
        return

    try:
        comms = award_svc.communications(award.id)
        handoffs = award_svc.handoffs(award.id)
    except AwardError:
        return
    unsent = [c for c in comms if c.status != CommunicationStatus.SENT.value]
    bits = []
    if status == AwardStatus.APPROVED.value:
        bits.append("Lines are fixed. No supplier has been told anything yet.")
    if comms:
        bits.append("%d of %d supplier message%s recorded as sent."
                    % (len(comms) - len(unsent), len(comms),
                       "" if len(comms) == 1 else "s"))
    if unsent:
        bits.append("Still to send: %s." % ", ".join(c.supplier_name for c in unsent))
    if status in (AwardStatus.SUPPLIER_NOTIFIED.value,) and not handoffs:
        bits.append("The order handoff has not been generated.")
    if handoffs:
        bits.append("%d order handoff%s generated."
                    % (len(handoffs), "" if len(handoffs) == 1 else "s"))
    if bits:
        st.caption(" ".join(bits))


def _communications(award, award_svc) -> None:
    comms = award_svc.communications(award.id)
    if not comms:
        return
    st.markdown('<div class="aw-step-title">Supplier messages</div>'
                '<div class="aw-step-sub">One per awarded supplier, each written only from '
                'that supplier\'s own lines.</div>', unsafe_allow_html=True)

    for comm in comms:
        sent = comm.status == CommunicationStatus.SENT.value
        with st.expander("%s — %s" % (comm.supplier_name, comm.status.replace("_", " ")),
                         expanded=not sent):
            c1, c2 = st.columns([4, 2])
            with c1:
                st.markdown('<div class="pg-label">To</div><div>%s</div>'
                            % esc(comm.recipient or "no contact on file"),
                            unsafe_allow_html=True)
                st.markdown('<div class="pg-label">Subject</div><div class="pg-subject">%s</div>'
                            % esc(comm.subject), unsafe_allow_html=True)
            with c2:
                if comm.guard_status == "ok":
                    st.markdown(badge("buyer", "checked against the award"),
                                unsafe_allow_html=True)
                elif comm.guard_status.startswith("rejected"):
                    st.markdown(badge("conflict", "draft not verified"), unsafe_allow_html=True)
                else:
                    st.markdown(badge("recommended", "standard letter"), unsafe_allow_html=True)
                if comm.was_edited:
                    st.markdown(badge("edited", "edited by you"), unsafe_allow_html=True)
                    st.markdown(badge("buyer", "your wording checked") if comm.sendable
                                else badge("conflict", "your wording failed the check"),
                                unsafe_allow_html=True)

            if comm.was_edited and not comm.sendable:
                st.error("Your wording could not be verified against this award: %s%s. "
                         "This message cannot be recorded as sent until it is corrected."
                         % (labels.describe_guard_status(comm.edit_status),
                            ", and it names %s" % ", ".join(comm.edit_leaks)
                            if comm.edit_leaks else ""),
                         icon=":material/block:")

            if comm.guard_status.startswith("rejected"):
                st.warning("The written draft could not be verified against the award — %s — "
                           "so the standard letter is shown instead."
                           % labels.describe_guard_status(comm.guard_status),
                           icon=":material/info:")

            body = st.text_area("Message", value=comm.text, height=260, disabled=sent,
                                key="aw_body_%s" % comm.id, label_visibility="collapsed")
            st.caption("Editing the message never changes the award.")

            rows = line_table_for(comm)
            if rows:
                st.dataframe(_typed(pd.DataFrame(rows)), hide_index=True,
                             use_container_width=True)

            if not sent:
                b1, b2, b3 = st.columns([1.5, 1.5, 3])
                with b1:
                    if st.button("Save edit", key="aw_save_%s" % comm.id,
                                 disabled=body.strip() == comm.text.strip(),
                                 use_container_width=True):
                        state.queue_award({"type": "edit", "comm_id": comm.id, "body": body})
                        st.rerun()
                with b2:
                    if st.button("Send to supplier", type="primary",
                                 key="aw_send_%s" % comm.id, use_container_width=True,
                                 disabled=not comm.sendable,
                                 help=None if comm.sendable else
                                 "Your edit did not pass the award check."):
                        state.queue_award({"type": "send", "comm_id": comm.id})
                        st.rerun()
                with b3:
                    st.markdown('<div class="aw-sim">Sending is simulated: this prototype '
                                'has no mail connection, and nothing leaves this machine.'
                                '</div>', unsafe_allow_html=True)
            else:
                st.markdown('<div class="aw-sim">Recorded as sent on %s — simulated, '
                            'demo only.</div>' % esc(comm.sent_at[:16].replace("T", " ")),
                            unsafe_allow_html=True)
            if comm.omitted:
                st.caption("Left out of the draft: %s" % "; ".join(comm.omitted))
            st.download_button("Download message", data=comm.text,
                               file_name="award_%s.txt" % comm.supplier_name.replace(" ", "_"),
                               key="aw_dl_%s" % comm.id)


def line_table_for(comm) -> List[Dict[str, Any]]:
    """The awarded lines under the letter, rendered from the stored facts."""
    facts = comm.facts or {}
    rows = []
    for line in facts.get("lines") or []:
        rows.append({
            "Line": line.get("line_reference", ""),
            "Description": line.get("description") or NOT_AVAILABLE,
            "Qty": line.get("quantity") if line.get("quantity") is not None else NOT_AVAILABLE,
            "Unit price": line.get("unit_price") if line.get("unit_price") is not None
            else NOT_AVAILABLE,
            "Total": line.get("extended") if line.get("extended") is not None
            else NOT_AVAILABLE,
            "As quoted": line.get("as_quoted") or NOT_AVAILABLE,
        })
    return rows


def _handoffs(award, award_svc) -> None:
    handoffs = award_svc.handoffs(award.id)
    if not handoffs:
        return
    st.markdown('<div class="aw-step-title">Order handoff</div>'
                '<div class="aw-step-sub">Generated from the award. No model was involved, '
                'and an unstated term reads "Not provided" rather than a guess.</div>',
                unsafe_allow_html=True)
    for handoff in handoffs:
        with st.expander("%s — %s" % (handoff.reference, handoff.supplier_name),
                         expanded=False):
            c1, c2 = st.columns(2)
            with c1:
                st.markdown('<div class="pg-label">Supplier</div><div>%s<br>%s</div>'
                            % (esc(handoff.supplier_name),
                               esc(handoff.supplier_contact_email or NOT_PROVIDED)),
                            unsafe_allow_html=True)
            with c2:
                st.markdown('<div class="pg-label">Total</div><div class="aw-total">%s</div>'
                            % esc("%s %s" % (handoff.currency,
                                             "{:,.2f}".format(handoff.subtotal))
                                  if handoff.subtotal is not None else NOT_AVAILABLE),
                            unsafe_allow_html=True)
            st.dataframe(_typed(pd.DataFrame(handoff.rows())), hide_index=True,
                         use_container_width=True)
            terms = pd.DataFrame([
                {"Term": "Payment", "Value": handoff.payment_terms},
                {"Term": "Delivery", "Value": handoff.delivery_terms},
                {"Term": "Lead time", "Value": handoff.lead_time},
                {"Term": "Quote validity", "Value": handoff.quote_validity},
                {"Term": "Minimum order", "Value": handoff.minimum_order},
            ])
            st.dataframe(terms, hide_index=True, use_container_width=True)
            if handoff.open_points:
                st.caption("Still open: " + "; ".join(handoff.open_points))
            st.caption(handoff.source_note)
            d1, d2 = st.columns(2)
            with d1:
                st.download_button("Download CSV", data=handoff.to_csv(),
                                   file_name="%s.csv" % handoff.reference, mime="text/csv",
                                   key="aw_csv_%s" % handoff.id, use_container_width=True)
            with d2:
                st.download_button("Download Markdown", data=handoff.to_markdown(),
                                   file_name="%s.md" % handoff.reference,
                                   key="aw_md_%s" % handoff.id, use_container_width=True)


# --------------------------------------------------------------------------- #
def _typed(frame: pd.DataFrame) -> pd.DataFrame:
    """A column that mixes a figure with the "—" placeholder is rendered as text.

    The dash has to stay — a blank would read as zero — and Arrow cannot serialise a
    mixed column, so the whole column becomes text when both are present.
    """
    for column in frame.columns:
        values = list(frame[column])
        numeric = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
        other = [v for v in values if v not in ("", None)
                 and not (isinstance(v, (int, float)) and not isinstance(v, bool))]
        if numeric and other:
            frame[column] = [_text(v) for v in values]
    return frame


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return ("%.4f" % value).rstrip("0").rstrip(".")
    return str(value)


def _fmt(value: Optional[float], currency: Optional[str]) -> str:
    if value is None:
        return NOT_AVAILABLE
    text = ("%.4f" % value).rstrip("0").rstrip(".")
    return "%s %s" % (currency, text) if currency else text


# --------------------------------------------------------------------------- #
def _process_pending() -> None:
    action = state.take_pending_award()
    if not action:
        return
    svc = state.get_award_service()
    kind = action.get("type")

    try:
        if kind == "start":
            award = svc.start(action["rfq_id"])
            state.flash("Award opened and every line seeded.")
        elif kind == "thresholds":
            svc.set_thresholds(action["award_id"],
                               AwardThresholds(
                                   require_document_backed_certification=action["require_docs"]))
            state.flash("Quality bar updated. Your own choices were kept.")
        elif kind == "set_line":
            svc.set_line(action["award_id"], action["line_item_id"], action["choice"],
                         action.get("reason", ""))
        elif kind == "approve":
            before = {l.line_item_id for l in svc.get(action["award_id"]).awarded_lines}
            approved = svc.approve(action["award_id"])
            dropped = before - {l.line_item_id for l in approved.awarded_lines}
            if dropped:
                state.flash("Award approved. %s dropped because the quote behind it can no "
                            "longer be committed to — see Notes."
                            % ", ".join(sorted(dropped)), "warning")
            else:
                state.flash("Award approved. The lines are now fixed.")
        elif kind == "draft":
            with st.status("Preparing supplier messages…", expanded=True) as status:
                svc.draft_communications(action["award_id"], on_stage=lambda s: st.write(s))
                status.update(label="Messages ready", state="complete", expanded=False)
        elif kind == "edit":
            comm = svc.edit_communication(action["comm_id"], action["body"])
            if comm.sendable:
                state.flash("Your wording saved and checked against the award. "
                            "The award is unchanged.")
            else:
                state.flash("Your wording was saved, but it does not match the award: %s. "
                            "It cannot be sent until you correct it."
                            % labels.describe_guard_status(comm.edit_status),
                            "warning")
        elif kind == "send":
            comm = svc.record_sent(action["comm_id"])
            state.flash("Recorded as sent to %s — simulated, demo only." % comm.supplier_name,
                        "info")
        elif kind == "handoff":
            svc.generate_handoff(action["award_id"])
            state.flash("Order handoff generated.")
        elif kind == "complete":
            svc.complete(action["award_id"])
            state.flash("Award closed.")
        elif kind == "cancel":
            svc.cancel(action["award_id"], action.get("reason", ""))
            state.flash("Award cancelled. The history is intact.", "info")
    except AwardError as e:
        st.session_state[state.K_AW_ERROR] = errors.message_for(e, "completing that step")
    except AIError as e:
        st.session_state[state.K_AW_ERROR] = e.user_message
    except Exception as e:                          # pragma: no cover - last resort
        st.session_state[state.K_AW_ERROR] = errors.message_for(e, "completing that step")
    st.rerun()
