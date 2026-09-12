"""AI Procurement Analyst — Streamlit entry point.

Run:  python3 -m streamlit run app.py
The UI only renders state and submits actions; all logic lives in rfq_copilot/.
"""
from __future__ import annotations

import streamlit as st

from ui import (errors, page_analyst, page_award, page_copilot, page_playground, page_quotes,
                page_review, page_saved, state)
from ui.theme import inject_css

st.set_page_config(page_title="AI Procurement Analyst", page_icon="📦", layout="wide", initial_sidebar_state="expanded")
inject_css()


def _page(render, name: str):
    """Wrap a page so an unanticipated failure is a sentence, not a traceback.

    Each page already handles the failures it expects. This is the floor underneath that:
    `showErrorDetails` is on because it is the right default while building, and it renders
    a full Python traceback into the browser — fine for a developer, alarming and useless
    for anyone else, and the kind of thing that ends a demo.
    """
    return lambda: errors.guard_page(render, name)


# The default page is served at "/" — Streamlit ignores url_path there, so do not set one.
copilot = st.Page(_page(page_copilot.render, "the Copilot"), title="1 · RFQ Copilot",
                  icon=":material/auto_awesome:", default=True)
review = st.Page(_page(page_review.render, "the RFQ"), title="Review RFQ",
                 icon=":material/fact_check:", url_path="review")
quotes = st.Page(_page(page_quotes.render, "the comparison"), title="2 · Quotes & Comparison",
                 icon=":material/table_chart:", url_path="quotes")
analyst = st.Page(_page(page_analyst.render, "the analyst"), title="3 · Procurement Analyst",
                  icon=":material/query_stats:", url_path="analyst")
award = st.Page(_page(page_award.render, "the award"), title="4 · Award & Execution",
                icon=":material/gavel:", url_path="award")
saved = st.Page(_page(page_saved.render, "your saved RFQs"), title="Saved RFQs",
                icon=":material/folder_open:", url_path="saved")
playground = st.Page(_page(page_playground.render, "the playground"), title="Extraction Playground",
                     icon=":material/science:", url_path="playground")
state.PAGES.update({"copilot": copilot, "review": review, "quotes": quotes,
                    "analyst": analyst, "award": award, "playground": playground,
                    "saved": saved})

with st.sidebar:
    st.markdown("### AI Procurement Analyst")
    st.caption("From a vague requirement to an awarded order")
    state.render_sidebar_progress()
    h = state.cached_health()
    if h.get("authenticated"):
        st.caption("Claude Code · signed in" + ((" · " + h["detail"]) if h.get("detail") else ""))
    else:
        st.caption("Claude Code · not signed in")
    st.markdown("")
    st.checkbox("Show diagnostics", key=state.K_DIAGNOSTICS,
                help="AI call log, guard decisions and technical error detail. Off by default.")

# Grouped so the sidebar reads as the journey the product actually is, rather than seven
# flat entries in which a developer's test bench sits between two phases of the workflow.
st.navigation({
    "Build the RFQ": [copilot, review],
    "Read the quotes": [quotes, analyst],
    "Decide and execute": [award],
    "Library": [saved],
    "Tools": [playground],
}, position="sidebar").run()
