"""AI Procurement Analyst — Phase 1: RFQ Copilot (Streamlit entry point).

Run:  python3 -m streamlit run app.py
The UI only renders state and submits actions; all logic lives in rfq_copilot/.
"""
from __future__ import annotations

import streamlit as st

from ui import page_copilot, page_review, page_saved, state
from ui.theme import inject_css

st.set_page_config(page_title="AI RFQ Copilot", page_icon="📦", layout="wide", initial_sidebar_state="expanded")
inject_css()

copilot = st.Page(page_copilot.render, title="Copilot", icon=":material/auto_awesome:", url_path="copilot", default=True)
review = st.Page(page_review.render, title="Review RFQ", icon=":material/fact_check:", url_path="review")
saved = st.Page(page_saved.render, title="Saved RFQs", icon=":material/folder_open:", url_path="saved")
state.PAGES.update({"copilot": copilot, "review": review, "saved": saved})

with st.sidebar:
    st.markdown("### AI Procurement Analyst")
    st.caption("Phase 1 · RFQ Copilot")
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

st.navigation([copilot, review, saved], position="sidebar").run()
