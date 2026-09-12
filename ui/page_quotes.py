"""Screen 3 — Quotes & Comparison.

The question this screen answers is not "which supplier is cheapest". It is "what did
each supplier actually say, and what should I be careful about". So every price carries
its original wording and its source, missing quotes stay visibly missing, and anything
the system is unwilling to assert is listed rather than hidden.

No ranking, no recommendation: that is Phase 3's job, deliberately not this one's.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd
import streamlit as st

from rfq_copilot.ai_service import AIError
from rfq_copilot.rfq_service import RFQStateError
from rfq_copilot.supplier_models import (
    ClaimStatus, ExtractionStatus, MatchStatus, NormalizationStatus, QuoteStatus, ResponseType,
)
from rfq_copilot.supplier_service import CellState
from . import state
from .components import render_resume_hint
from .theme import badge, esc

CELL_BADGE = {
    CellState.QUOTED: ("", ""),
    CellState.NOT_QUOTED: ("na", "not quoted"),
    CellState.NEEDS_REVIEW: ("recommended", "needs review"),
    CellState.CONFLICT: ("conflict", "conflict"),
    CellState.UNRESOLVED: ("unknown", "unresolved"),
    CellState.NO_RESPONSE: ("na", "no response"),
}

ISSUE_LABELS = {
    "probable_match": "Line match to confirm",
    "unmatched": "Unmatched supplier line",
    "conflict": "Contradictory values",
    "unresolved_price": "Price that cannot be compared",
    "unverified_claim": "Claim without a certificate",
    "supplier_question": "Supplier is waiting on you",
}


# --------------------------------------------------------------------------- #
def render() -> None:
    svc = state.get_service()
    sup = state.get_supplier_service()
    _process_pending(sup)
    state.show_flash()

    rfq = state.current_rfq()
    if rfq is None:
        st.markdown('<div class="rfq-kicker">Quotes &amp; comparison</div>'
                    '<div class="rfq-title">No RFQ is open yet.</div>', unsafe_allow_html=True)

        def _open(rid):
            state.set_current(rid)
            st.rerun()
        render_resume_hint(svc, "quotes", _open)
        return

    _header(rfq, sup)
    if not sup.has_responses(rfq.id):
        _empty_state(rfq, sup)
        return

    _summary_bar(sup, rfq)
    tabs = st.tabs(["Comparison", "Suppliers", "Needs review", "Questionnaire & certifications"])
    with tabs[0]:
        _comparison(sup, rfq)
    with tabs[1]:
        _suppliers(sup, rfq)
    with tabs[2]:
        _review_queue(sup, rfq)
    with tabs[3]:
        _quality(sup, rfq)


# --------------------------------------------------------------------------- #
def _process_pending(sup) -> None:
    action = state.take_pending_quotes()
    if not action:
        return
    kind = action.get("type")
    if kind == "seed":
        try:
            created = sup.seed_demo_responses(action["rfq_id"])
            state.flash("%d supplier responses received. Run extraction to read them." % len(created))
        except RFQStateError as e:
            st.session_state[state.K_QUOTES_ERROR] = str(e)
        st.rerun()

    if kind in ("extract", "extract_one"):
        label = st.empty()
        with st.status("Processing supplier responses…", expanded=True) as status:
            def on_stage(name, stage_text):
                st.write(("**%s** — %s" % (name, stage_text)) if name else stage_text)
            try:
                if kind == "extract":
                    res = sup.extract_all(action["rfq_id"], on_stage=on_stage, only_pending=action.get("only_pending", True))
                    done, failed = len(res["succeeded"]), len(res["failed"])
                    total = done + failed
                    if failed:
                        status.update(label="%d of %d supplier responses processed" % (done, total), state="error")
                        state.flash("%d of %d processed. %s need attention: %s"
                                    % (done, total, failed, "; ".join("%s (%s)" % (n, w) for n, w in res["failed"])),
                                    kind="warning")
                    else:
                        status.update(label="All %d supplier responses processed" % done, state="complete")
                        state.flash("All %d supplier responses were read and normalized." % done)
                else:
                    sup.extract_response(action["response_id"], on_stage=lambda s: st.write(s))
                    status.update(label="Response processed", state="complete")
                    state.flash("Supplier response re-processed.")
            except AIError as e:
                status.update(label="Extraction stopped", state="error")
                st.session_state[state.K_QUOTES_ERROR] = e.user_message
            except RFQStateError as e:
                status.update(label="Nothing to process", state="error")
                st.session_state[state.K_QUOTES_ERROR] = str(e)
        label.empty()
        st.rerun()


def _header(rfq, sup) -> None:
    c1, c2, c3, c4 = st.columns([5, 1.7, 1.5, 1.6])
    with c1:
        st.markdown('<div class="rfq-kicker">Quotes &amp; comparison</div><div class="rfq-title">%s</div>'
                    % esc(rfq.title or rfq.product), unsafe_allow_html=True)
        st.markdown('<div class="rfq-sub">%d line items · %s</div>'
                    % (len(rfq.line_items), esc(rfq.category or "uncategorised")), unsafe_allow_html=True)
    with c2:
        # The analyst answers questions about this same dataset; it does not copy it.
        if sup.has_responses(rfq.id) and st.button("Ask the analyst", type="primary",
                                                   use_container_width=True, key="ask_analyst"):
            state.set_current(rfq.id)
            state.go("analyst")
    with c3:
        # The analyst explains; the award decides. Keeping them separate buttons is the
        # same boundary `analyst_guards._AWARD_LANGUAGE` states in code.
        if sup.has_responses(rfq.id) and st.button("Take a decision", use_container_width=True,
                                                   key="take_decision"):
            state.set_current(rfq.id)
            state.go("award")
    with c4:
        if sup.has_responses(rfq.id) and st.button("Re-run extraction", use_container_width=True, key="rerun_all"):
            state.queue_quotes({"type": "extract", "rfq_id": rfq.id, "only_pending": False})
            st.rerun()
    err = st.session_state.pop(state.K_QUOTES_ERROR, None)
    if err:
        st.error(err)
    st.markdown("")


def _empty_state(rfq, sup) -> None:
    with st.container(border=True):
        st.markdown('<div class="rfq-kicker">Supplier responses</div>', unsafe_allow_html=True)
        st.markdown("No supplier responses have arrived for this RFQ yet.")
        st.caption("The demo set contains five suppliers who answer in five different formats — a spreadsheet, "
                   "a PDF, a Word document, a plain email and a photographed quotation — plus one revision and "
                   "one supplier who never replies. Nothing is sent or received; the files are read from disk.")
        if st.button("Load supplier responses", type="primary", key="seed_btn"):
            state.queue_quotes({"type": "seed", "rfq_id": rfq.id})
            st.rerun()


def _summary_bar(sup, rfq) -> None:
    """Every figure counted from stored records."""
    matrix = _matrix(sup, rfq.id)
    s = matrix.summary
    pending = [r for r in sup.responses_for(rfq.id) if r.extraction_status == ExtractionStatus.PENDING]
    if pending:
        with st.container(border=True):
            a, b = st.columns([5, 1.6])
            with a:
                st.markdown("**%d supplier response%s received and not yet read.**"
                            % (len(pending), "" if len(pending) == 1 else "s"))
                st.caption("Extraction reads each document, then normalizes what it finds.")
            with b:
                if st.button("Run extraction", type="primary", use_container_width=True, key="extract_btn"):
                    state.queue_quotes({"type": "extract", "rfq_id": rfq.id, "only_pending": True})
                    st.rerun()

    cols = st.columns(5)
    cols[0].metric("Suppliers", s["suppliers_total"])
    cols[1].metric("Responses", s["responses_received"], help="%d never replied" % s["no_response"])
    cols[2].metric("Line responses", s["line_responses"], help="Supplier prices matched to an RFQ line")
    cols[3].metric("Missing quotes", s["missing_quotes"], help="Lines a supplier did not price")
    cols[4].metric("Need review", s["need_review"], help="Responses with something unresolved")
    _currency_control(matrix, s)
    if s.get("unnamed_currency"):
        st.warning("%d quoted price%s give a number without naming a currency. %s held out of the "
                   "comparison until the supplier confirms it, rather than being guessed at."
                   % (s["unnamed_currency"], "" if s["unnamed_currency"] == 1 else "s",
                      "It is" if s["unnamed_currency"] == 1 else "They are"))
    st.markdown("")


NATIVE = "Each supplier's own currency"


def _currency_control(matrix, summary) -> None:
    """Let the buyer pick a single currency, and say exactly which rate was applied."""
    options = [NATIVE] + sorted(set(summary["currencies"]) | {"USD", "EUR", "INR", "GBP", "CNY"})
    current = st.session_state.get(state.K_DISPLAY_CCY) or NATIVE
    if current not in options:
        options.append(current)
    c1, c2 = st.columns([1.6, 5])
    with c1:
        picked = st.selectbox("Show prices in", options, index=options.index(current), key="ccy_pick")
    chosen = None if picked == NATIVE else picked
    if chosen != st.session_state.get(state.K_DISPLAY_CCY):
        st.session_state[state.K_DISPLAY_CCY] = chosen
        st.rerun()

    with c2:
        if not chosen:
            if len(summary["currencies"]) > 1:
                st.caption("Suppliers quoted in %s. Pick a currency above to convert them at a live "
                           "published rate." % ", ".join(summary["currencies"]))
            return
        if summary.get("rate_source"):
            st.caption("Converted at rates from %s, published %s. Each supplier's original figure and "
                       "currency are kept and shown alongside."
                       % (summary["rate_source"], summary.get("rate_as_of") or "today"))
        if summary.get("rate_error"):
            st.warning("Exchange rates could not be refreshed: %s" % summary["rate_error"])
        if summary.get("unconvertible"):
            st.warning("%d price%s could not be converted and %s shown in their original currency."
                       % (summary["unconvertible"], "" if summary["unconvertible"] == 1 else "s",
                          "is" if summary["unconvertible"] == 1 else "are"))


def _matrix(sup, rfq_id: str):
    """Built fresh each render. It is a plain read over already-stored rows, and caching
    it forced Streamlit to pickle live dataclasses, which it cannot do."""
    return sup.build_comparison(rfq_id, display_currency=st.session_state.get(state.K_DISPLAY_CCY))


# --------------------------------------------------------------------------- #
def _comparison(sup, rfq) -> None:
    matrix = _matrix(sup, rfq.id)
    if not matrix.suppliers:
        st.info("No supplier responses have been extracted yet.")
        return

    st.markdown('<div class="rfq-section-head">Normalized price per piece</div>', unsafe_allow_html=True)
    rows = []
    for line in rfq.line_items:
        spec = line.spec_summary() or line.description or line.product
        row = {"Line": line.id, "Item": spec}
        for s in matrix.suppliers:
            row[s.name] = matrix.cell(line.id, s.id).display
        rows.append(row)
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    st.caption("A figure is a comparable price. *not quoted* means the supplier did not price that line. "
               "*needs review*, *conflict* and *unresolved* each mean something specific, explained below.")

    # An explicit picker rather than a hidden row selection: the evidence trail is the
    # point of this screen, so reaching it should never depend on discovering a click.
    st.markdown('<div class="rfq-section-head">Where did a number come from?</div>', unsafe_allow_html=True)
    labels = ["%s · %s" % (li.id, li.spec_summary() or li.product) for li in rfq.line_items]
    picked = st.selectbox("Line to inspect", labels, key="cmp_line", label_visibility="collapsed")
    line = rfq.line_items[labels.index(picked)]
    _line_detail(sup, rfq, matrix, line)


def _line_detail(sup, rfq, matrix, line) -> None:
    st.markdown('<div class="rfq-section-head">%s · %s</div>'
                % (esc(line.id), esc(line.spec_summary() or line.product)), unsafe_allow_html=True)
    for s in matrix.suppliers:
        cell = matrix.cell(line.id, s.id)
        with st.container(border=True):
            head, body = st.columns([2, 5])
            with head:
                st.markdown("**%s**" % esc(s.name))
                kind, label = CELL_BADGE.get(cell.state, ("na", cell.state))
                if label:
                    st.markdown(badge(kind, label), unsafe_allow_html=True)
                else:
                    st.markdown('<div class="rfq-price">%s</div>' % esc(cell.display), unsafe_allow_html=True)
            with body:
                _quote_detail(sup, matrix, cell)


def _quote_detail(sup, matrix, cell) -> None:
    q = cell.quote
    if q is None:
        if cell.state == CellState.NO_RESPONSE:
            st.caption("This supplier did not respond to the RFQ.")
        else:
            st.caption("This supplier did not price this line. That is an absence, not a zero.")
        return

    if q.has_price:
        conv = cell.converted
        showing_conversion = conv is not None and conv.is_conversion
        cols = st.columns(3 if showing_conversion else 2)
        if showing_conversion:
            with cols[0]:
                st.markdown('<div class="rfq-field-label">In %s</div><div class="rfq-field-value">%s %s</div>'
                            % (esc(conv.currency), esc(conv.currency), esc("%.4f" % conv.amount)),
                            unsafe_allow_html=True)
        with cols[1 if showing_conversion else 0]:
            st.markdown('<div class="rfq-field-label">Per piece, as they quoted it</div>'
                        '<div class="rfq-field-value">%s</div>'
                        % esc(q.normalized_price_text() or "not comparable"), unsafe_allow_html=True)
        with cols[2 if showing_conversion else 1]:
            st.markdown('<div class="rfq-field-label">As quoted</div>'
                        '<div class="rfq-field-value">%s</div>' % esc(q.original_price_text()),
                        unsafe_allow_html=True)
        if showing_conversion:
            st.caption(conv.describe_rate())
        if q.normalization_note:
            st.caption(q.normalization_note)
    if q.discount.is_present:
        applied = {True: "applied", False: "not applied", None: "cannot be evaluated"}[q.discount.applies]
        st.markdown("**Discount** %s — %s. %s" % (esc(q.discount.describe()), applied,
                                                  esc(q.discount.applies_reason)))
    facts = []
    if q.minimum_order_quantity:
        facts.append(("Minimum order", "{:,.0f} {}".format(q.minimum_order_quantity, q.moq_unit or "pcs")
                      + (" — above this line's quantity" if q.moq_constraint else "")))
    if q.lead_time_text:
        facts.append(("Lead time", q.lead_time_text + (" (read as %g days)" % q.lead_time_days
                                                       if q.lead_time_is_interpreted and q.lead_time_days else "")))
    if q.quote_validity_text:
        facts.append(("Quote validity", q.quote_validity_text +
                      (" — a condition, not a fixed period" if q.quote_validity_is_conditional else "")))
    if q.payment_terms:
        facts.append(("Payment terms", q.payment_terms))
    if q.delivery_terms:
        facts.append(("Delivery", q.delivery_terms))
    if facts:
        st.markdown("\n".join("- **%s** — %s" % (esc(k), esc(v)) for k, v in facts))

    for issue in q.issues:
        st.markdown(badge("recommended", "check") + " " + esc(issue), unsafe_allow_html=True)
    for c in q.conflicts:
        vals = " vs ".join(esc(v.get("value", "")) for v in c.get("values", []))
        st.markdown(badge("conflict", "conflict") + " **%s** — %s" % (esc(c.get("topic", "")), vals),
                    unsafe_allow_html=True)

    _evidence_popover(sup, q)


def _evidence_popover(sup, q) -> None:
    ev_ids = [e for e in q.evidence_ids if e]
    cols = st.columns([1.4, 1.4, 4])
    with cols[0]:
        with st.popover("Where from?", use_container_width=True):
            if not ev_ids:
                st.caption("No source span was recorded for this value.")
            for eid in ev_ids:
                ev = sup.store.get_evidence(eid)
                if ev is None:
                    continue
                st.markdown("**%s**" % esc(ev.describe() or "unknown location"))
                st.code(ev.quoted_text or "(no quoted text)", language=None)
                st.caption("Found in the document text." if ev.verified else
                           "This span could not be located in the document, so the value is held for review.")
            if q.match_reason:
                st.caption("Line match: %s" % q.match_reason)
            if q.confidence is not None:
                st.caption("Extraction confidence %.0f%%." % (q.confidence * 100))
    with cols[1]:
        with st.popover("Correct", use_container_width=True):
            _correction_form(sup, q)


def _correction_form(sup, q) -> None:
    st.caption("A correction becomes the active value. The extracted figure and its evidence are kept.")
    with st.form("fix_%s" % q.id, border=False):
        price = st.number_input("Unit price as quoted", value=float(q.unit_price or 0.0),
                                min_value=0.0, step=0.01, format="%.4f")
        currency = st.text_input("Currency", value=q.currency or "")
        if st.form_submit_button("Save correction", type="primary"):
            try:
                sup.correct_quote(q.response_id, q.id, unit_price=price or None,
                                  currency=currency.strip().upper())
                state.flash("Correction saved for %s." % (q.supplier_line_label or q.id))
                st.rerun()
            except RFQStateError as e:
                st.error(str(e))
    if q.history:
        st.caption("%d previous value%s kept." % (len(q.history), "" if len(q.history) == 1 else "s"))


# --------------------------------------------------------------------------- #
def _suppliers(sup, rfq) -> None:
    for bundle in sup.bundles_for(rfq.id):
        name = bundle.supplier.name if bundle.supplier else bundle.response.supplier_id
        r = bundle.response
        with st.container(border=True):
            c1, c2 = st.columns([5, 1.6])
            with c1:
                title = name + ("  ·  superseded" if not r.is_active else "")
                st.markdown("**%s**" % esc(title))
                priced = [q for q in bundle.quotes if q.has_price]
                matched = {q.line_item_id for q in priced if q.line_item_id}
                st.markdown('<div class="rfq-sub">%s · quoted %d of %d lines · %s</div>' % (
                    esc(r.response_type.value.replace("_", " ")), len(matched), len(rfq.line_items),
                    esc(", ".join(d.filename for d in bundle.documents))), unsafe_allow_html=True)
                if r.extraction_note:
                    st.caption(r.extraction_note)
            with c2:
                st.markdown(badge("recommended" if r.extraction_status == ExtractionStatus.NEEDS_REVIEW
                                  else ("missing" if r.extraction_status in (ExtractionStatus.FAILED,
                                                                             ExtractionStatus.UNSUPPORTED)
                                        else "buyer"),
                                  r.extraction_status.value.replace("_", " ")), unsafe_allow_html=True)
                if r.extraction_status in (ExtractionStatus.FAILED, ExtractionStatus.UNSUPPORTED,
                                           ExtractionStatus.PENDING):
                    if st.button("Process", key="one_%s" % r.id, use_container_width=True):
                        state.queue_quotes({"type": "extract_one", "response_id": r.id})
                        st.rerun()

            counts = bundle.issue_counts()
            if counts:
                st.markdown(" ".join(badge("recommended", "%s: %d" % (k.replace("_", " "), v))
                                     for k, v in counts.items()), unsafe_allow_html=True)
            if not r.is_active and r.superseded_by_id:
                st.caption("Replaced by a later response from the same supplier. Kept for the record.")
            if r.revises_response_id:
                st.caption("This response revises an earlier one, which is retained above.")

            with st.expander("Documents and uncertainties", expanded=False):
                for d in bundle.documents:
                    st.markdown("- **%s** (%s) — %s" % (esc(d.filename), esc(d.media_type),
                                                        esc(d.extraction_method or d.extraction_status.value)))
                    if d.extraction_note:
                        st.caption(d.extraction_note)
                for u in r.uncertainties:
                    st.markdown("- %s" % esc(u))
                if st.session_state.get(state.K_DIAGNOSTICS):
                    calls = sup.extraction_calls(r.id)
                    if calls:
                        st.caption("Extraction calls")
                        st.dataframe([{"type": c.call_type, "model": c.model,
                                       "seconds": round(c.duration_ms / 1000.0, 1),
                                       "ok": "yes" if c.ok else "no", "error": (c.error or "")[:60]}
                                      for c in calls], hide_index=True, use_container_width=True)


# --------------------------------------------------------------------------- #
def _review_queue(sup, rfq) -> None:
    items = sup.review_queue(rfq.id)
    if not items:
        st.success("Nothing is waiting for review. Every extracted value is traceable and unambiguous.")
        return
    st.caption("These are the things the system is not willing to assert on its own.")
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for it in items:
        grouped.setdefault(it["kind"], []).append(it)

    for kind, entries in grouped.items():
        st.markdown('<div class="rfq-section-head">%s (%d)</div>'
                    % (esc(ISSUE_LABELS.get(kind, kind)), len(entries)), unsafe_allow_html=True)
        for it in entries:
            with st.container(border=True):
                st.markdown("**%s** — %s" % (esc(it["supplier"]), esc(it["label"])[:140]))
                if it.get("values"):
                    st.markdown(" vs ".join(badge("conflict", str(v)[:60]) for v in it["values"]),
                                unsafe_allow_html=True)
                if it.get("affected_lines", 0) > 1:
                    st.caption("Applies to %d quoted lines." % it["affected_lines"])
                if it.get("detail"):
                    st.caption(esc(it["detail"])[:300])
                if kind in ("probable_match", "unmatched"):
                    _match_controls(sup, rfq, it)
                elif kind == "supplier_question":
                    _question_controls(sup, it)


def _match_controls(sup, rfq, item) -> None:
    options = ["(leave unmatched)"] + ["%s · %s" % (li.id, li.spec_summary() or li.product) for li in rfq.line_items]
    current = 0
    if item.get("line_item_id"):
        for i, li in enumerate(rfq.line_items, start=1):
            if li.id == item["line_item_id"]:
                current = i
                break
    choice = st.selectbox("Which RFQ line is this?", options, index=current,
                          key="m_%s" % item["quote_id"], label_visibility="collapsed")
    if st.button("Confirm match", key="mc_%s" % item["quote_id"]):
        line_id = None if choice.startswith("(") else choice.split(" · ")[0]
        try:
            sup.confirm_match(item["response_id"], item["quote_id"], line_id)
            state.flash("Match confirmed.")
            st.rerun()
        except RFQStateError as e:
            st.error(str(e))


def _question_controls(sup, item) -> None:
    with st.form("sq_%s" % item["question_id"], border=False):
        answer = st.text_input("Your answer", key="sqa_%s" % item["question_id"],
                               placeholder="This is recorded locally; nothing is sent to the supplier.")
        if st.form_submit_button("Save answer"):
            sup.answer_supplier_question(item["response_id"], item["question_id"], answer)
            state.flash("Answer recorded. Nothing was sent to the supplier.")
            st.rerun()


# --------------------------------------------------------------------------- #
def _quality(sup, rfq) -> None:
    bundles = sup.bundles_for(rfq.id, active_only=True)
    if not bundles:
        st.info("No extracted responses yet.")
        return

    st.markdown('<div class="rfq-section-head">Certifications</div>', unsafe_allow_html=True)
    st.caption("A supplier saying they hold a certificate is a claim. It becomes verified only when the "
               "certificate itself is among the documents received.")
    rows = []
    for b in bundles:
        name = b.supplier.name if b.supplier else "?"
        for c in b.certifications:
            rows.append({"Supplier": name, "Certification": c.name,
                         "Status": c.status.value.replace("_", " "),
                         "Number": c.certificate_number or "—",
                         "Expiry": c.expiry_date or "—", "Note": c.note})
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    else:
        st.caption("No certifications were mentioned by any supplier.")

    st.markdown('<div class="rfq-section-head">Questionnaire</div>', unsafe_allow_html=True)
    questions = [q for q in rfq.questions if q.field_key]
    if not questions:
        st.caption("This RFQ has no questionnaire items.")
        return
    rows = []
    for b in bundles:
        name = b.supplier.name if b.supplier else "?"
        by_field = {a.field_key: a for a in b.questionnaire}
        row = {"Supplier": name}
        for q in questions:
            a = by_field.get(q.field_key)
            if a is None or a.status == ClaimStatus.MISSING:
                row[q.field_key] = "not addressed"
            else:
                row[q.field_key] = (a.answer or a.status.value)[:60]
        rows.append(row)
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    st.caption("An item a supplier never addressed is shown as *not addressed*. It is never read as a no.")
