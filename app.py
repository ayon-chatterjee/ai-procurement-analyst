"""AI Procurement Analyst — Streamlit entry point.

Run:  python3 -m streamlit run app.py
The UI only renders state and submits actions; all logic lives in rfq_copilot/.
"""
from __future__ import annotations

import streamlit as st

from ui import page_copilot, page_playground, page_quotes, page_review, page_saved, state
from ui.theme import inject_css

st.set_page_config(page_title="AI Procurement Analyst", page_icon="📦", layout="wide", initial_sidebar_state="expanded")
inject_css()

# The default page is served at "/" — Streamlit ignores url_path there, so do not set one.
copilot = st.Page(page_copilot.render, title="Copilot", icon=":material/auto_awesome:", default=True)
review = st.Page(page_review.render, title="Review RFQ", icon=":material/fact_check:", url_path="review")
quotes = st.Page(page_quotes.render, title="Quotes & Comparison", icon=":material/table_chart:", url_path="quotes")
playground = st.Page(page_playground.render, title="Requirement Playground",
                     icon=":material/science:", url_path="playground")
saved = st.Page(page_saved.render, title="Saved RFQs", icon=":material/folder_open:", url_path="saved")
state.PAGES.update({"copilot": copilot, "review": review, "quotes": quotes,
                    "playground": playground, "saved": saved})

with st.sidebar:
    st.markdown("### AI Procurement Analyst")
    st.caption("RFQ Copilot · Supplier Response Intelligence")
    rfq = state.current_rfq()
    if rfq is not None:
        st.markdown("**Open RFQ**")
        st.caption(rfq.title or rfq.product)
        st.progress(min(100, max(0, rfq.completeness.score)) / 100.0, text="%d%% complete" % rfq.completeness.score)
    h = state.cached_health()
    if h.get("authenticated"):
        st.caption("Claude Code · signed in" + ((" · " + h["detail"]) if h.get("detail") else ""))
    else:
        st.caption("Claude Code · not signed in")
    st.markdown("")
    st.checkbox("Show diagnostics", key=state.K_DIAGNOSTICS,
                help="AI call log and guard decisions on the Review and Quotes pages. Off by default.")

st.navigation([copilot, review, quotes, playground, saved], position="sidebar").run()
