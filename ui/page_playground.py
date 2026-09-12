"""Screen 4 — Requirement Playground.

A bench for answering one question in a demo: *given this supplier reply, does the system
extract the right things?* You pick an RFQ line item, write whatever a supplier might have
sent, and the screen puts the output next to the input so any deviation is visible.

The extraction is the real Phase 2 engine. Nothing is saved unless you choose to keep it.
"""
from __future__ import annotations

import os
import tempfile
from typing import Any, Dict, List, Optional

import pandas as pd
import streamlit as st

from rfq_copilot.playground_service import PlaygroundError
from rfq_copilot.rfq_service import RFQStateError
from rfq_copilot.supplier_testbench import TestbenchError, TestRun
from . import state
from .theme import badge, esc

ACCEPTED = ["pdf", "xlsx", "xlsm", "xls", "csv", "tsv", "docx", "txt", "md", "eml",
            "png", "jpg", "jpeg", "webp"]

GROUP_ORDER = ["Price", "Commercial terms", "Line matching", "Quality", "Other lines"]

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
    if isinstance(run, TestRun) and run.rfq_id == rfq.id:
        _step_results(run)


# --------------------------------------------------------------------------- #
def _header() -> None:
    c1, c2 = st.columns([6, 1.5])
    with c1:
        st.markdown('<div class="rfq-kicker">Requirement playground</div>'
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
    for key in (state.K_PG_RUN, state.K_PG_ERROR, state.K_PG_PENDING):
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
            st.session_state["pg_body"] = SAMPLE
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
                st.session_state[state.K_PG_ERROR] = "The requirement could not be read: %s" % str(e)[:200]
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
                st.session_state[state.K_PG_ERROR] = "The reply could not be processed: %s" % str(e)[:200]
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


def _materialise(files: List) -> List[str]:
    """Write uploaded bytes to a temp folder so the existing readers can open them."""
    if not files:
        return []
    folder = tempfile.mkdtemp(prefix="playground_upload_")
    paths = []
    for name, data in files:
        path = os.path.join(folder, os.path.basename(name) or "attachment")
        with open(path, "wb") as f:
            f.write(data)
        paths.append(path)
    return paths


# --------------------------------------------------------------------------- #
# Step 3 — input vs output
# --------------------------------------------------------------------------- #
def _step_results(run: TestRun) -> None:
    st.markdown("")
    st.markdown('<div class="rfq-section-head">Step 3 · what the system understood</div>',
                unsafe_allow_html=True)

    cols = st.columns(4)
    cols[0].metric("Fields extracted", run.extracted_count)
    cols[1].metric("Traced to the text", run.traced_count,
                   help="The extracted value quotes a span that really appears in what you sent.")
    cols[2].metric("Deviations", run.deviation_count,
                   help="Values the system will not assert on its own, plus figures it never picked up.")
    cols[3].metric("Extraction time", "%.0fs" % (run.duration_ms / 1000.0))

    if run.clean:
        st.success("Every extracted value traces back to your text, and nothing was left unexplained.")
    elif run.extracted_count == 0:
        st.warning("Nothing was extracted from that reply. It may not read as a quotation.")
    else:
        st.warning("%d thing%s worth a look below." % (run.deviation_count,
                                                       "" if run.deviation_count == 1 else "s"))

    if run.unreadable:
        for name, why in run.unreadable:
            st.error("**%s** could not be read — %s" % (esc(name), esc(why)))

    left, right = st.columns([4, 6])
    with left:
        _input_panel(run)
    with right:
        _output_panel(run)

    _unclaimed_panel(run)
    _actions(run)


def _input_panel(run: TestRun) -> None:
    st.markdown("**Input — what the supplier sent**")
    with st.container(border=True, height=520):
        if run.subject:
            st.markdown('<div class="rfq-field-label">Subject</div>', unsafe_allow_html=True)
            st.markdown(esc(run.subject))
        if run.body:
            st.markdown('<div class="rfq-field-label">Body</div>', unsafe_allow_html=True)
            st.code(run.body, language=None)
        for d in run.documents:
            if d["filename"] == "supplier_email.txt":
                continue
            st.markdown('<div class="rfq-field-label">%s (%s)</div>'
                        % (esc(d["filename"]), esc(d["media_type"])), unsafe_allow_html=True)
            st.code(d["text"][:4000], language=None)


def _output_panel(run: TestRun) -> None:
    st.markdown("**Output — what was extracted, and from which words**")
    with st.container(border=True, height=520):
        if not run.rows:
            st.caption("Nothing was extracted.")
            return
        groups: Dict[str, List] = {}
        for r in run.rows:
            groups.setdefault(r.group, []).append(r)
        for group in GROUP_ORDER:
            rows = groups.get(group)
            if not rows:
                continue
            label = "Quotes for other lines" if group == "Other lines" else group
            st.markdown('<div class="rfq-field-label">%s</div>' % esc(label), unsafe_allow_html=True)
            for r in rows:
                _row(r)
            st.markdown("")


def _row(r) -> None:
    mark = {"deviation": ("conflict", "⚠"), "traced": ("buyer", "✓"), "untraced": ("recommended", "?")}[r.marker]
    st.markdown('%s **%s** — %s' % (badge(mark[0], mark[1]), esc(r.field), esc(r.extracted or "—")),
                unsafe_allow_html=True)
    if r.span:
        where = (" · %s" % r.location) if r.location else ""
        st.markdown('<div class="rfq-sub" style="margin-left:1.4rem">from “%s”%s</div>'
                    % (esc(r.span[:150]), esc(where)), unsafe_allow_html=True)
    elif r.marker != "traced":
        st.markdown('<div class="rfq-sub" style="margin-left:1.4rem">no supporting span found</div>',
                    unsafe_allow_html=True)
    if r.note:
        st.markdown('<div class="rfq-sub" style="margin-left:1.4rem;color:#92400E">%s</div>'
                    % esc(r.note[:200]), unsafe_allow_html=True)


def _unclaimed_panel(run: TestRun) -> None:
    if not run.unclaimed_figures:
        return
    with st.container(border=True):
        st.markdown("**Figures in your text that appear nowhere in the output**")
        st.markdown(" ".join(badge("conflict", f) for f in run.unclaimed_figures), unsafe_allow_html=True)
        st.caption("A hint, not a verdict. Some of these will be irrelevant — a reference number, "
                   "a phone number, a figure the supplier mentioned in passing. Worth checking "
                   "whether any of them should have been picked up.")


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
