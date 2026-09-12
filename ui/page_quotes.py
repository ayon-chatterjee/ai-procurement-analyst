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

from rfq_copilot import labels
from rfq_copilot.document_extractor import ACCEPTED_UPLOAD_TYPES as ACCEPTED
from rfq_copilot.ai_service import AIError
from rfq_copilot.rfq_service import RFQStateError
from rfq_copilot.supplier_models import (
    ClaimStatus, ExtractionStatus, MatchStatus, NormalizationStatus, QuoteStatus, ResponseType,
)
from rfq_copilot.supplier_service import CellState
from . import errors, state
from . import previews
from .components import render_document_preview, render_no_rfq
from .theme import badge, esc

CELL_BADGE = {
    CellState.QUOTED: ("", ""),
    CellState.NOT_QUOTED: ("na", "not quoted"),
    CellState.NEEDS_REVIEW: ("recommended", "needs review"),
    CellState.CONFLICT: ("conflict", "conflict"),
    CellState.UNRESOLVED: ("unknown", "unresolved"),
    CellState.NO_RESPONSE: ("na", "no response"),
}

#: Ordered so the buyer works through what blocks a comparison before what merely wants
#: an answer. `dict` preserves insertion order, and the review tab groups by this order
#: rather than by whatever order the service happened to return.
ISSUE_LABELS = dict(labels.REVIEW_KINDS)


# --------------------------------------------------------------------------- #
def render() -> None:
    svc = state.get_service()
    sup = state.get_supplier_service()
    _process_pending(sup)
    state.show_flash()

    rfq = state.current_rfq()
    if rfq is None:
        def _open(rid):
            state.set_current(rid)
            st.rerun()
        render_no_rfq(svc, "quotes", "Quotes & comparison", _open)
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

    if kind == "simulate":
        with st.status("Writing supplier quotations for this RFQ…", expanded=True) as status:
            def on_stage(name, text):
                st.write(("**%s** — %s" % (name, text)) if name else text)
            try:
                created = sup.simulate_responses(action["rfq_id"], action.get("count", 4),
                                                 on_stage=on_stage)
                st.write("Reading what they sent…")
                res = sup.extract_all(action["rfq_id"], on_stage=on_stage, only_pending=True)
                done, failed = len(res["succeeded"]), len(res["failed"])
                status.update(label="%d supplier responses written and read" % done,
                              state="error" if failed else "complete", expanded=False)
                state.flash("%d sample supplier responses added and read. They are "
                            "fabricated; everything read from them is not."
                            % len(created))
            except AIError as e:
                status.update(label="Could not write the responses", state="error")
                st.session_state[state.K_QUOTES_ERROR] = e.user_message
            except RFQStateError as e:
                status.update(label="Could not write the responses", state="error")
                st.session_state[state.K_QUOTES_ERROR] = str(e)
        st.rerun()

    if kind == "remove_response":
        try:
            name = sup.remove_response(action["response_id"])
            state.flash("Removed %s's response and everything read from it." % name, "info")
        except RFQStateError as e:
            st.session_state[state.K_QUOTES_ERROR] = str(e)
        st.rerun()

    if kind == "add_response":
        # Registering and reading are one action here: a buyer who has just uploaded a
        # supplier's quotation means "read this", and leaving it sitting unread behind a
        # second button is a step with no decision in it.
        with st.status("Reading %s's response…" % action["name"], expanded=True) as status:
            try:
                resp = sup.add_response(action["rfq_id"], action["name"], action["files"],
                                        contact_email=action.get("email", ""))
                st.write("Saved %d document(s)." % len(action["files"]))
                sup.extract_response(resp.id, on_stage=lambda t: st.write(t))
                status.update(label="%s's response was read" % action["name"], state="complete",
                              expanded=False)
                state.flash("%s added. Their prices are in the comparison below."
                            % action["name"])
            except AIError as e:
                status.update(label="Could not read that response", state="error")
                st.session_state[state.K_QUOTES_ERROR] = e.user_message
            except RFQStateError as e:
                status.update(label="Could not add that response", state="error")
                st.session_state[state.K_QUOTES_ERROR] = str(e)
        st.rerun()

    if kind in ("extract", "extract_one"):
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
        st.rerun()


def _header(rfq, sup) -> None:
    c1, c2, c3, c4 = st.columns([5, 1.7, 1.5, 1.6])
    with c1:
        st.markdown('<div class="rfq-kicker">Quotes &amp; comparison</div><div class="rfq-title">%s</div>'
                    % esc(rfq.title or rfq.product), unsafe_allow_html=True)
        st.markdown('<div class="rfq-sub">%d line items · %s</div>'
                    % (len(rfq.line_items), esc(rfq.category or "uncategorized")), unsafe_allow_html=True)
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


#: What the built-in demo documents are actually about. Loading them against an RFQ for
#: something else registers five carton quotations, reads them correctly, and matches
#: nothing — which looks exactly like extraction failing.
DEMO_PRODUCT_WORDS = ("carton", "box", "packaging", "corrugated")


def _demo_suits(rfq) -> bool:
    text = " ".join([rfq.product or "", rfq.category or "", rfq.product_type or "",
                     rfq.title or ""]).lower()
    return any(w in text for w in DEMO_PRODUCT_WORDS)


def _empty_state(rfq, sup) -> None:
    st.markdown('<div class="rfq-kicker">Supplier responses</div>', unsafe_allow_html=True)
    st.markdown("No supplier responses have arrived for this RFQ yet. Add the replies your "
                "suppliers sent you, or load the built-in demo set.")
    _add_response_form(rfq, key="empty")

    _simulate_panel(rfq)

    with st.container(border=True):
        st.markdown('<div class="pg-label">Or load the five-format demo set</div>',
                    unsafe_allow_html=True)
        st.caption("Five suppliers answering **a request for corrugated carton boxes** — a "
                   "spreadsheet, a PDF, a Word document, a plain email and a photographed "
                   "quotation — plus one revision and one supplier who never replies. These "
                   "are committed files, so they are the way to see the document readers "
                   "work on real formats rather than on text.")
        if not _demo_suits(rfq):
            # The failure this prevents: carton quotations attached to an RFQ for pneumatic
            # cylinders, every cell reading "not quoted", and the product looking broken
            # when it was in fact refusing to match a carton to a cylinder.
            st.warning("This RFQ is for **%s**, and these documents quote carton boxes. "
                       "They will load and read correctly, but nothing in them matches these "
                       "line items, so every price will show as *not quoted*. Use one of the "
                       "options above instead." % (rfq.product or "something else"),
                       icon=":material/info:")
        if st.button("Load the carton-box demo set", key="seed_btn",
                     type="primary" if _demo_suits(rfq) else "secondary"):
            state.queue_quotes({"type": "seed", "rfq_id": rfq.id})
            st.rerun()


def _simulate_panel(rfq) -> None:
    """Supplier replies written for whatever this RFQ is actually about.

    A new RFQ arrives with nobody having answered it, which is correct and unhelpful:
    there is nothing to look at until real quotes come back. This writes a few, for this
    product and these line items, so the comparison, the analyst and the award have
    something to work on within a couple of minutes of building an RFQ.
    """
    with st.container(border=True):
        st.markdown('<div class="pg-label">Or generate sample supplier responses</div>',
                    unsafe_allow_html=True)
        st.caption("Quotations written for **%s** and these %d line items: several "
                   "suppliers, disagreeing on price, minimum order, lead time and currency, "
                   "with at least one who does not quote everything. The documents are "
                   "fabricated — everything that happens to them afterwards is the real "
                   "pipeline." % (rfq.product or "this RFQ", len(rfq.line_items)))
        c1, c2 = st.columns([1.2, 4])
        with c1:
            count = st.number_input("How many", min_value=2, max_value=6, value=4, step=1,
                                    key="sim_count", label_visibility="collapsed")
        with c2:
            if st.button("Generate %d supplier responses" % int(count), type="primary",
                         key="sim_btn", disabled=not rfq.line_items):
                state.queue_quotes({"type": "simulate", "rfq_id": rfq.id,
                                    "count": int(count)})
                st.rerun()
        if not rfq.line_items:
            st.caption("This RFQ has no line items yet, so there is nothing to quote. Add "
                       "them in the Copilot or on the Review page first.")
        else:
            st.caption("Writing them takes about a minute; reading them takes about another "
                       "minute each, four at a time.")


def _add_response_form(rfq, key: str) -> None:
    """Attach what a supplier actually sent — pasted text, files, or both.

    Most supplier replies are an email, so text is the first field rather than an
    afterthought: requiring a file to exist before a quotation could be recorded meant a
    buyer had to save an email to disk before the system would look at it.
    """
    with st.container(border=True):
        st.markdown('<div class="pg-label">Add a supplier response</div>',
                    unsafe_allow_html=True)
        with st.form("add_resp_%s_%s" % (key, rfq.id), border=False):
            c1, c2 = st.columns([2, 2])
            with c1:
                name = st.text_input("Supplier name", key="ar_name_%s" % key,
                                     placeholder="e.g. Festo India Pvt Ltd")
            with c2:
                email = st.text_input("Contact email (optional)", key="ar_mail_%s" % key,
                                      placeholder="sales@supplier.example")
            body = st.text_area(
                "What they wrote", key="ar_body_%s" % key, height=150,
                placeholder="Paste their email or quotation here — prices, terms, "
                            "whatever they sent. Untidy is fine; that is the point.")
            files = st.file_uploader(
                "Attachments (optional)", type=ACCEPTED, accept_multiple_files=True,
                key="ar_files_%s" % key,
                help="A quotation spreadsheet, a PDF, a Word file, or a photograph of a "
                     "printed quote. Several files are fine, with or without text above.")
            submitted = st.form_submit_button("Add and read this response", type="primary")
        if submitted:
            payload = [(f.name, f.getvalue()) for f in (files or [])]
            if (body or "").strip():
                # The email itself is a document, and is read by the same reader.
                payload.insert(0, ("%s_email.txt" % (name or "supplier").strip().lower()
                                   .replace(" ", "_")[:40], body.strip().encode("utf-8")))
            if not (name or "").strip():
                st.warning("Give the supplier a name so their quote can be attributed.")
            elif not payload:
                st.warning("Paste what %s wrote, or attach their quotation."
                           % (name.strip() or "the supplier"))
            else:
                state.queue_quotes({
                    "type": "add_response", "rfq_id": rfq.id, "name": name.strip(),
                    "email": (email or "").strip(), "files": payload})
                st.rerun()
        st.caption("Text and attachments are read the same way: prices, terms and "
                   "certifications are extracted, matched to your line items, and every "
                   "figure keeps a link back to the words it came from.")


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

    # Four numbers, each with the denominator it is counted against. The old row mixed
    # three: two counted supplier-line pairs, one counted responses, and "Missing quotes"
    # included every line of a supplier who never wrote back — which read as a failure of
    # the system rather than an absence of a reply.
    cols = st.columns(4)
    cols[0].metric("Suppliers", "%d of %d replied" % (s["responses_received"], s["suppliers_total"]),
                   help="Everyone invited to quote on this RFQ."
                        + (" %d never replied." % s["no_response"] if s["no_response"] else ""))
    cols[1].metric("Lines priced", "%d of %d" % (s["line_responses"], s["comparable_cells"]),
                   help="Across the suppliers who replied: %d lines x %d responses. A line a "
                        "supplier chose not to quote is an absence, not a zero."
                        % (s["rfq_lines"], s["responses_received"]))
    cols[2].metric("Needs review", s["review_items_total"],
                   help="Things the system will not assert on its own, across %d response%s. "
                        "Listed in the Needs review tab."
                        % (s["need_review"], "" if s["need_review"] == 1 else "s"))
    cols[3].metric("Currencies", " · ".join(s["currencies"]) or "—",
                   help="What suppliers actually quoted in. Nothing is converted unless you "
                        "pick a currency below.")
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
            st.warning(errors.describe_rate_error(summary["rate_error"]))
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
    st.caption("A bare figure is a comparable price. A figure marked **· review** or **· conflict** "
               "is a number the system will not stand behind on its own — pick the line below to "
               "see why. *not quoted* is an absence, not a zero; *unresolved* means the price "
               "cannot be reduced to a per-piece figure; *no response* means the supplier never "
               "replied.")

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
    _add_response_form(rfq, key="tab")
    st.markdown("")

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
                # Loading documents onto the wrong RFQ used to be a one-way door.
                with st.popover("Remove", use_container_width=True):
                    st.caption("Removes this response and everything read from it — its "
                               "quotes, evidence and certifications. The RFQ and every "
                               "other supplier are untouched.")
                    if st.button("Remove this response", key="rm_%s" % r.id,
                                 type="primary", use_container_width=True):
                        state.queue_quotes({"type": "remove_response", "response_id": r.id})
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
                    dc1, dc2 = st.columns([5, 1.4])
                    with dc1:
                        st.markdown("**%s** (%s) — %s"
                                    % (esc(d.filename), esc(previews.type_label(d.media_type)),
                                       esc(d.extraction_method or d.extraction_status.value)))
                        if d.extraction_note:
                            st.caption(d.extraction_note)
                    with dc2:
                        # The same "what did they actually send?" the extraction bench
                        # offers. Reading a price without being able to open the document
                        # behind it asks the buyer to take the number on trust.
                        with st.popover("Open", use_container_width=True):
                            render_document_preview(d.filename, d.path, d.media_type,
                                                    d.raw_text, d.extraction_method,
                                                    d.byte_size)
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
    st.caption("These are the things the system is not willing to assert on its own. "
               "Each one says what it is, where it came from, and what you can do about it.")
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for it in items:
        grouped.setdefault(it["kind"], []).append(it)

    # Worked through in severity order rather than in whatever order the records came
    # back: what stops a price being comparable first, what merely wants an answer last.
    for kind in list(ISSUE_LABELS) + [k for k in grouped if k not in ISSUE_LABELS]:
        entries = grouped.get(kind)
        if not entries:
            continue
        st.markdown('<div class="rfq-section-head">%s (%d)</div>'
                    % (esc(ISSUE_LABELS.get(kind, kind.replace("_", " ").capitalize())),
                       len(entries)), unsafe_allow_html=True)
        guidance = labels.REVIEW_GUIDANCE.get(kind)
        if guidance:
            st.caption(guidance)
        for it in entries:
            with st.container(border=True):
                st.markdown("**%s** — %s" % (esc(it["supplier"]), esc(it["label"])[:140]))
                settled = (it.get("resolution") or {}).get("value")
                if it.get("values"):
                    st.markdown(" vs ".join(
                        badge("buyer" if settled and str(v) == settled else "conflict", str(v)[:60])
                        for v in it["values"]), unsafe_allow_html=True)
                if it.get("affected_lines", 0) > 1:
                    st.caption("Applies to %d quoted lines." % it["affected_lines"])
                if it.get("detail"):
                    st.caption(it["detail"][:300])
                if kind in ("probable_match", "unmatched"):
                    _match_controls(sup, rfq, it)
                elif kind == "supplier_question":
                    _question_controls(sup, it)
                elif kind == "conflict":
                    _conflict_controls(sup, it, settled)


def _conflict_controls(sup, item, settled: str) -> None:
    """Let the buyer record which stated value applies.

    The system will not choose between two things a supplier said — it has no basis to —
    but the buyer can ask them and write the answer down. Without this the contradiction
    sat in the queue permanently with nothing to do about it.
    """
    if settled:
        st.markdown(badge("buyer", "you recorded: %s" % settled[:60]), unsafe_allow_html=True)
        note = (item.get("resolution") or {}).get("note")
        if note:
            st.caption(note[:200])
        return
    values = [str(v) for v in (item.get("values") or []) if v]
    if len(values) < 2:
        return
    key = "cf_%s_%s" % (item["response_id"], abs(hash(item["label"])) % 100000)
    with st.form(key, border=False):
        choice = st.radio("Which applies?", values, key="%s_v" % key, horizontal=True)
        note = st.text_input("How do you know?", key="%s_n" % key,
                             placeholder="e.g. supplier confirmed by email on 14 March")
        if st.form_submit_button("Record which applies"):
            try:
                sup.resolve_conflict(item["response_id"], item["label"], choice, note)
                state.flash("Recorded: %s applies. Both stated values are kept." % choice)
                st.rerun()
            except RFQStateError as e:
                st.error(str(e))


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
                         "Status": labels.label_for(labels.CLAIM, c.status.value)[1],
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
            # Headed by the question the buyer actually asked, not its internal field key:
            # a column called "required_delivery_date" tells a reader nothing about what
            # the supplier was asked.
            col = (q.question or q.field_key)[:60]
            a = by_field.get(q.field_key)
            if a is None or a.status == ClaimStatus.MISSING:
                row[col] = "not addressed"
            else:
                row[col] = (a.answer or labels.label_for(labels.CLAIM, a.status.value)[1])[:60]
        rows.append(row)
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    st.caption("An item a supplier never addressed is shown as *not addressed*. It is never read as a no.")
