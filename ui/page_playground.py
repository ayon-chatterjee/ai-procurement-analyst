"""Screen 4 — Quotation Extraction Playground.

A bench for answering one question in a demo: *given this supplier reply, does the system
extract the right things?* You pick an RFQ line item, write whatever a supplier might have
sent, and the screen puts the output next to the input so any deviation is visible.

The extraction is the real Phase 2 engine. Nothing is saved unless you choose to keep it.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from typing import Any, Dict, List, Optional

import pandas as pd
import streamlit as st

from rfq_copilot.document_extractor import ACCEPTED_UPLOAD_TYPES
from rfq_copilot.playground_service import PlaygroundError
from rfq_copilot.rfq_service import RFQStateError
from rfq_copilot.supplier_testbench import TestbenchError, TestRun
from . import previews
from . import errors, state
from .components import render_document_preview
from .theme import badge, esc

#: Declared next to the readers that open them, so a screen cannot offer a format nothing
#: can read.
ACCEPTED = ACCEPTED_UPLOAD_TYPES

#: The synthetic document the bench writes the email body into.
EMAIL_DOC = "supplier_email.txt"

SAMPLE = """Thank you for your enquiry.

We are pleased to quote USD 0.48 per piece for the size requested, at the quantity
in your RFQ. Minimum order quantity is 5,000 pieces per size.

Production lead time is 18 days after artwork approval. Payment 30% advance,
balance against B/L copy. Prices are FOB Shanghai and valid for 30 days.

We are ISO 9001 certified.

Best regards,
Sales"""


# --------------------------------------------------------------------------- #
def render() -> None:
    _process_pending()
    state.show_flash()
    _header()

    rfq = _step_context()
    if rfq is None:
        return
    _step_compose(rfq)
    run = st.session_state.get(state.K_PG_RUN)
    if run is not None and not isinstance(run, TestRun):
        # Reachable only in development: editing a source file makes Streamlit re-import the
        # module, so a run held from before the reload is an instance of the previous class
        # object. Say so and drop it - silently showing no result would look like a failed
        # extraction.
        st.session_state.pop(state.K_PG_RUN, None)
        st.warning("The app reloaded while that test was on screen, so the result was dropped. "
                   "Run the extraction again.")
        run = None
    if run is not None and run.rfq_id == rfq.id:
        _step_results(run)


# --------------------------------------------------------------------------- #
def _header() -> None:
    c1, c2 = st.columns([6, 1.5])
    with c1:
        st.markdown('<div class="rfq-kicker">Quotation extraction playground</div>'
                    '<div class="rfq-title">Does the system read a supplier reply correctly?</div>',
                    unsafe_allow_html=True)
        st.markdown('<div class="rfq-sub">Pick a line item, write what a supplier might send, and '
                    'compare the output against the input. Nothing is saved unless you keep it.</div>',
                    unsafe_allow_html=True)
    with c2:
        if st.session_state.get(state.K_PG_RUN) is not None:
            if st.button("Clear / start new test", use_container_width=True, key="pg_reset"):
                _reset()
                st.rerun()
    err = st.session_state.pop(state.K_PG_ERROR, None)
    if err:
        st.error(err)
    st.markdown("")


def _reset(keep_context: bool = True) -> None:
    for key in (state.K_PG_RUN, state.K_PG_ERROR, state.K_PG_PENDING, state.K_PG_SAMPLE):
        st.session_state.pop(key, None)
    for key in ("pg_body", "pg_subject", "pg_files", "pg_supplier"):
        st.session_state.pop(key, None)
    if not keep_context:
        for key in (state.K_PG_RESULT, state.K_PG_RFQ, state.K_PG_LINE):
            st.session_state.pop(key, None)


# --------------------------------------------------------------------------- #
# Step 1 — which requirement are we testing against?
# --------------------------------------------------------------------------- #
def _step_context():
    svc = state.get_service()
    st.markdown('<div class="rfq-section-head">Step 1 · choose the requirement</div>',
                unsafe_allow_html=True)

    rows = svc.list_rfqs()
    mode_options = ["Use an existing RFQ", "Create a new requirement"]
    default = 0 if rows else 1
    mode = st.radio("Requirement source", mode_options, index=default, horizontal=True,
                    key="pg_mode", label_visibility="collapsed")

    if mode == mode_options[1]:
        return _new_requirement(svc)

    if not rows:
        st.info("No RFQs exist yet. Switch to **Create a new requirement** above, or build one "
                "in the Copilot first.")
        return None

    labels = ["%s · %d line%s · %s" % (r.title or r.product or r.id, r.line_item_count,
                                       "" if r.line_item_count == 1 else "s", r.id) for r in rows]
    current = 0
    chosen_id = st.session_state.get(state.K_PG_RFQ)
    for i, r in enumerate(rows):
        if r.id == chosen_id:
            current = i
            break
    c1, c2 = st.columns([3, 3])
    with c1:
        picked = st.selectbox("RFQ", labels, index=current, key="pg_rfq_pick")
    rfq_id = rows[labels.index(picked)].id
    if rfq_id != st.session_state.get(state.K_PG_RFQ):
        st.session_state[state.K_PG_RFQ] = rfq_id
        st.session_state.pop(state.K_PG_RUN, None)

    try:
        rfq = svc.get(rfq_id)
    except RFQStateError as e:
        st.error(str(e))
        return None
    if not rfq.line_items:
        st.warning("This RFQ has no line items, so there is nothing for a supplier to quote against.")
        return None

    line_labels = ["%s · %s" % (li.id, li.spec_summary() or li.description or li.product)
                   for li in rfq.line_items]
    line_labels.insert(0, "Any line (let the system work it out)")
    cur_line = st.session_state.get(state.K_PG_LINE) or line_labels[0]
    if cur_line not in line_labels:
        cur_line = line_labels[0]
    with c2:
        picked_line = st.selectbox("Line item", line_labels, index=line_labels.index(cur_line),
                                   key="pg_line_pick")
    st.session_state[state.K_PG_LINE] = picked_line
    st.caption("The supplier reply below will be read as a response to **%s**." % esc(picked_line))
    return rfq


def _new_requirement(svc):
    """Reuses the buyer-side extractor: paste a requirement, get an RFQ to test against."""
    with st.container(border=True):
        st.markdown("**Describe what you want to buy.** It becomes an RFQ you can then test "
                    "supplier replies against.")
        st.text_area("Requirement", key="pg_req_text", height=140, label_visibility="collapsed",
                     placeholder="e.g. 500 SS304 brackets, 3mm thick, powder coated, delivered to Pune in 2 weeks.")
        files = st.file_uploader("Attachments (optional)", type=ACCEPTED, accept_multiple_files=True,
                                 key="pg_req_files")
        if st.button("Create requirement", type="primary", key="pg_req_go"):
            text = st.session_state.get("pg_req_text") or ""
            if not text.strip() and not files:
                st.session_state[state.K_PG_ERROR] = "Describe the requirement or attach a file first."
                st.rerun()
            state.queue_playground({"type": "new_requirement", "text": text,
                                    "files": [(f.name, f.getvalue()) for f in (files or [])]})
            st.rerun()
    return None


# --------------------------------------------------------------------------- #
# Step 2 — be the supplier
# --------------------------------------------------------------------------- #
def _step_compose(rfq) -> None:
    st.markdown("")
    st.markdown('<div class="rfq-section-head">Step 2 · write the supplier reply</div>',
                unsafe_allow_html=True)
    st.caption("Anything a supplier might send: a tidy quotation, a two-line email, a price "
               "list as an attachment, or something deliberately awkward.")

    c1, c2 = st.columns([2, 4])
    with c1:
        st.text_input("Supplier name", key="pg_supplier", placeholder="Test Supplier")
    with c2:
        st.text_input("Subject", key="pg_subject", placeholder="Re: your enquiry")

    st.session_state.setdefault("pg_body", "")
    if st.session_state.pop(state.K_PG_SAMPLE, False):
        st.session_state["pg_body"] = SAMPLE
    st.text_area("Supplier email / quotation text", key="pg_body", height=210,
                 placeholder="Paste or type what the supplier sent back.")
    files = st.file_uploader("Supplier attachments", type=ACCEPTED, accept_multiple_files=True,
                             key="pg_files",
                             help="A quotation spreadsheet, a PDF, a photographed quote. "
                                  "Legacy .xls cannot be read and will say so.")

    a, b, c = st.columns([1.6, 1.6, 4])
    with a:
        if st.button("Run extraction", type="primary", use_container_width=True, key="pg_run"):
            body = st.session_state.get("pg_body") or ""
            if not body.strip() and not files:
                st.session_state[state.K_PG_ERROR] = "Write a reply or attach a file first."
                st.rerun()
            state.queue_playground({
                "type": "run", "rfq_id": rfq.id,
                "line": st.session_state.get(state.K_PG_LINE),
                "supplier": st.session_state.get("pg_supplier") or "Test Supplier",
                "subject": st.session_state.get("pg_subject") or "",
                "body": body,
                "files": [(f.name, f.getvalue()) for f in (files or [])]})
            st.rerun()
    with b:
        if st.button("Use a sample reply", use_container_width=True, key="pg_sample"):
            # Not `st.session_state["pg_body"] = SAMPLE`: the text area above already
            # exists this run, and assigning its key raises. Flag it and fill the widget
            # on the next run, before it is created.
            st.session_state[state.K_PG_SAMPLE] = True
            st.rerun()
    with c:
        st.caption("The sample is editable text, not a shortcut: it is extracted the same way "
                   "as anything you type.")


# --------------------------------------------------------------------------- #
def _process_pending() -> None:
    action = st.session_state.pop(state.K_PG_PENDING, None)
    if not action:
        return
    kind = action.get("type")

    if kind == "new_requirement":
        svc = state.get_playground_service()
        paths = _materialise(action.get("files") or [])
        with st.status("Reading the requirement…", expanded=True) as status:
            try:
                result = svc.analyze(action.get("text") or "", paths, on_stage=lambda s: st.write(s))
                if not result.found_anything:
                    status.update(label="Nothing to create", state="error", expanded=False)
                    st.session_state[state.K_PG_ERROR] = (
                        result.nothing_found_reason or "No line items were found in that.")
                else:
                    rfq = svc.save_as_rfq(result)
                    st.session_state[state.K_PG_RFQ] = rfq.id
                    st.session_state.pop(state.K_PG_LINE, None)
                    st.session_state.pop(state.K_PG_RUN, None)
                    st.session_state["pg_mode"] = "Use an existing RFQ"
                    status.update(label="Requirement created", state="complete", expanded=False)
                    state.flash("Created %s with %d line item%s. Now write a supplier reply to test."
                                % (rfq.id, len(rfq.line_items), "" if len(rfq.line_items) == 1 else "s"))
            except PlaygroundError as e:
                status.update(label="Could not create that", state="error", expanded=False)
                st.session_state[state.K_PG_ERROR] = str(e)
            except Exception as e:
                status.update(label="Something went wrong", state="error", expanded=False)
                st.session_state[state.K_PG_ERROR] = errors.message_for(e, "reading the requirement")
        st.rerun()

    if kind == "run":
        bench = state.get_testbench()
        svc = state.get_service()
        paths = _materialise(action.get("files") or [])
        line_label = action.get("line") or ""
        line_id = line_label.split(" · ")[0] if line_label and not line_label.startswith("Any") else None
        with st.status("Reading the supplier reply…", expanded=True) as status:
            try:
                rfq = svc.get(action["rfq_id"])
                run = bench.run_test(rfq, line_id, action["supplier"], action["subject"],
                                     action["body"], paths, on_stage=lambda s: st.write(s))
                st.session_state[state.K_PG_RUN] = run
                status.update(label="Extraction complete", state="complete", expanded=False)
            except (TestbenchError, RFQStateError) as e:
                status.update(label="Could not run that", state="error", expanded=False)
                st.session_state[state.K_PG_ERROR] = str(e)
            except Exception as e:
                status.update(label="Something went wrong", state="error", expanded=False)
                st.session_state[state.K_PG_ERROR] = errors.message_for(e, "reading the supplier reply")
        st.rerun()

    if kind == "promote":
        bench = state.get_testbench()
        sup = state.get_supplier_service()
        run = st.session_state.get(state.K_PG_RUN)
        try:
            rid = bench.promote(run, sup)
            state.flash("Saved into %s as response %s. It now appears in Quotes & Comparison."
                        % (run.rfq_id, rid))
        except TestbenchError as e:
            st.session_state[state.K_PG_ERROR] = str(e)
        st.rerun()


#: How many uploaded batches to keep on disk. The document readers open files by path, so
#: the bytes have to land somewhere — but a long session used to leave one folder per run
#: behind forever. The current run's files must survive until its result is rendered, so
#: the previous few are kept and older ones swept.
_UPLOAD_KEEP = 3
_upload_folders: List[str] = []


def _materialise(files: List) -> List[str]:
    """Write uploaded bytes to a temp folder so the existing readers can open them."""
    if not files:
        return []
    folder = tempfile.mkdtemp(prefix="playground_upload_")
    _upload_folders.append(folder)
    while len(_upload_folders) > _UPLOAD_KEEP:
        shutil.rmtree(_upload_folders.pop(0), ignore_errors=True)
    paths = []
    for name, data in files:
        path = os.path.join(folder, os.path.basename(name) or "attachment")
        with open(path, "wb") as f:
            f.write(data)
        paths.append(path)
    return paths


# --------------------------------------------------------------------------- #
# Results — three sections: what arrived, what was understood, what needs a look
# --------------------------------------------------------------------------- #
def _step_results(run: TestRun) -> None:
    st.markdown("")
    _section_supplier_response(run)
    _section_extracted(run)
    _section_needs_review(run)
    _actions(run)


# --------------------------------------------------------------------------- #
# Section 1 — Supplier Response
# --------------------------------------------------------------------------- #
def _section_supplier_response(run: TestRun) -> None:
    st.markdown('<div class="pg-section"><div class="pg-section-title">Supplier Response</div>'
                '<div class="pg-section-sub">What the supplier sent us</div></div>',
                unsafe_allow_html=True)

    attachments = [d for d in run.documents if d["filename"] != EMAIL_DOC]
    body_col, att_col = st.columns([5, 4], gap="large") if attachments else (st.container(), None)

    with body_col:
        with st.container(border=True):
            st.markdown('<div class="pg-label">Email from %s</div>' % esc(run.supplier_name),
                        unsafe_allow_html=True)
            if run.subject:
                st.markdown('<div class="pg-subject">%s</div>' % esc(run.subject), unsafe_allow_html=True)
            if run.body:
                st.markdown('<div class="pg-email">%s</div>' % esc(run.body).replace("\n", "<br>"),
                            unsafe_allow_html=True)
            else:
                st.markdown('<div class="rfq-field-value dim">No email body — the quotation is in '
                            'the attachment%s.</div>' % ("" if len(attachments) == 1 else "s"),
                            unsafe_allow_html=True)

    if att_col is not None:
        with att_col:
            st.markdown('<div class="pg-label">Attachments · %d</div>' % len(attachments),
                        unsafe_allow_html=True)
            for i, doc in enumerate(attachments):
                _attachment_card(doc, i)

    for name, why in run.unreadable:
        # Not escaped: st.error renders markdown, so an escaped ampersand in a
        # supplier filename would reach the buyer as "&amp;".
        st.error("**%s** could not be read — %s" % (name, why))


def _attachment_card(doc: Dict[str, Any], index: int) -> None:
    media = doc.get("media_type", "")
    name = doc.get("filename", "attachment")
    path = doc.get("path", "")
    with st.container(border=True):
        thumb = previews.thumbnail(path, media)
        if thumb:
            st.image(thumb, use_container_width=True)
        else:
            st.markdown('<div class="pg-thumb-fallback">%s</div>' % previews.icon(media),
                        unsafe_allow_html=True)
        meta = " · ".join(x for x in (previews.type_label(media),
                                      previews.human_size(doc.get("bytes", 0))) if x)
        st.markdown('<div class="pg-att-name">%s</div><div class="pg-att-meta">%s</div>'
                    % (esc(name), esc(meta)), unsafe_allow_html=True)
        with st.popover("View", use_container_width=True):
            _attachment_preview(doc, thumb)


def _attachment_preview(doc: Dict[str, Any], thumb: Optional[str]) -> None:
    render_document_preview(doc.get("filename", ""), doc.get("path", ""),
                            doc.get("media_type", ""), doc.get("text", ""),
                            doc.get("method", ""), doc.get("bytes", 0))


# --------------------------------------------------------------------------- #
# Section 2 — What AI Extracted
# --------------------------------------------------------------------------- #
def _section_extracted(run: TestRun) -> None:
    st.markdown('<div class="pg-section"><div class="pg-section-title">What AI Extracted</div>'
                '<div class="pg-section-sub">Structured information identified from the supplier '
                'response</div></div>', unsafe_allow_html=True)

    table = run.table
    counts = table.counts()
    needs_review = len(run.issues)
    cols = st.columns(4)
    cols[0].metric("Line items", counts["line_items"],
                   help="Quotation lines found in the supplier's response.")
    cols[1].metric("Fields extracted", counts["fields"],
                   help="Individual values read out of the email and attachments.")
    cols[2].metric("Missing information", counts["missing"],
                   help="Fields the supplier did not provide for a line.")
    cols[3].metric("Needs review", needs_review,
                   help="Things the system will not assert on its own. Listed below.")

    if not table.rows:
        st.warning("No quotation lines were found in that response.")
        return

    st.dataframe(_frame(table), hide_index=True, use_container_width=True,
                 column_config={"#": st.column_config.NumberColumn("#", width="small")})
    st.caption("Scroll sideways for the remaining columns. A dash means the supplier did not "
               "provide that field. Pick a line below to see where each value came from.")

    _trace_panel(run)


def _frame(table) -> pd.DataFrame:
    data = []
    for row in table.rows:
        record = {"#": row.index}
        for col in table.columns:
            record[col.label] = row.cell(col.key).display()
        data.append(record)
    return pd.DataFrame(data, columns=["#"] + [c.label for c in table.columns])


def _trace_panel(run: TestRun) -> None:
    """Where each value came from, for one line at a time, instead of under every field."""
    table = run.table
    labels = ["%d · %s" % (r.index, r.label) for r in table.rows]
    target = st.session_state.pop(state.K_PG_FOCUS, None)
    index = 0
    if target is not None:
        index = max(0, min(int(target) - 1, len(labels) - 1))
    picked = st.selectbox("Trace a line", labels, index=index, key="pg_trace_pick")
    row = table.rows[labels.index(picked)]

    st.markdown('<div class="pg-label">Where each value came from</div>', unsafe_allow_html=True)
    left, right = st.columns(2, gap="large")
    for i, col in enumerate(table.columns):
        cell = row.cell(col.key)
        with (left if i % 2 == 0 else right):
            _trace_row(col.label, cell)


def _trace_row(label: str, cell) -> None:
    if cell.missing:
        st.markdown('%s <b>%s</b> — <span class="pg-missing">not provided</span>'
                    % (badge("na", "—"), esc(label)), unsafe_allow_html=True)
        return
    if cell.derived:
        mark = badge("ai", "calculated")
    elif cell.from_rfq:
        mark = badge("buyer", "from RFQ")
    elif cell.deviation:
        mark = badge("conflict", "⚠")
    elif cell.traced:
        mark = badge("buyer", "✓")
    else:
        mark = badge("recommended", "?")
    st.markdown("%s <b>%s</b> — %s" % (mark, esc(label), esc(cell.display())), unsafe_allow_html=True)
    if cell.span:
        where = (" · %s" % cell.location) if cell.location else ""
        st.markdown('<div class="pg-span">“%s”%s</div>' % (esc(cell.span[:160]), esc(where)),
                    unsafe_allow_html=True)
    elif not cell.derived and not cell.from_rfq:
        st.markdown('<div class="pg-span">no supporting span found</div>', unsafe_allow_html=True)
    if cell.note:
        st.markdown('<div class="pg-note">%s</div>' % esc(cell.note[:180]), unsafe_allow_html=True)


# --------------------------------------------------------------------------- #
# Section 3 — Needs Review
# --------------------------------------------------------------------------- #
def _section_needs_review(run: TestRun) -> None:
    st.markdown('<div class="pg-section"><div class="pg-section-title">Needs Review</div>'
                '<div class="pg-section-sub">Information that may require your attention</div></div>',
                unsafe_allow_html=True)
    if not run.issues:
        st.success("Nothing needs review. Every extracted value traces back to the supplier's "
                   "words, and no field was left unexplained.")
        return

    for i, issue in enumerate(run.issues):
        with st.container(border=True):
            c1, c2 = st.columns([7, 1.4])
            with c1:
                head = esc(issue.title)
                if issue.subject:
                    head += ' <span class="pg-issue-subject">%s</span>' % esc(issue.subject)
                st.markdown('<span class="pg-issue-mark">⚠</span> <b>%s</b>' % head,
                            unsafe_allow_html=True)
                if issue.detail:
                    st.markdown('<div class="pg-note">%s</div>' % esc(issue.detail[:220]),
                                unsafe_allow_html=True)
            with c2:
                if issue.row_index is not None:
                    if st.button("Show line", key="pg_jump_%d" % i, use_container_width=True):
                        st.session_state[state.K_PG_FOCUS] = issue.row_index
                        st.rerun()


def _actions(run: TestRun) -> None:
    st.markdown("")
    a, b, c = st.columns([1.8, 1.8, 4])
    with a:
        if st.button("Clear / start new test", use_container_width=True, key="pg_reset2"):
            _reset()
            st.rerun()
    with b:
        if run.promoted:
            st.success("Saved")
        elif st.button("Keep this in the RFQ", use_container_width=True, key="pg_promote"):
            state.queue_playground({"type": "promote"})
            st.rerun()
    with c:
        st.caption("Test runs are throwaway. Keeping one turns it into a real supplier response "
                   "on this RFQ, so it shows up in Quotes & Comparison.")

    if st.session_state.get(state.K_DIAGNOSTICS) and run.ai_calls:
        with st.expander("Diagnostics · extraction calls", expanded=False):
            st.dataframe([{"type": c.call_type, "model": c.model,
                           "seconds": round(c.duration_ms / 1000.0, 1),
                           "ok": "yes" if c.ok else "no", "error": (c.error or "")[:60]}
                          for c in run.ai_calls], hide_index=True, use_container_width=True)
