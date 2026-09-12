"""Session-state keys and service access for the Streamlit UI. No business logic here."""
from __future__ import annotations

from typing import Any, Dict, Optional

import streamlit as st

from rfq_copilot.ai_service import get_ai_service
from rfq_copilot.config import Settings
from rfq_copilot.persistence import RFQRepository
from rfq_copilot.rfq_service import RFQService
from rfq_copilot.schema import RFQ

K_RFQ_ID = "rfq_id"
K_PENDING = "pending_action"
K_ERROR = "last_error"
K_PREFILL = "prefill_text"
K_FLASH = "flash"
K_TARGET = "nav_target"
K_DIAGNOSTICS = "show_diagnostics"
K_PENDING_QUOTES = "pending_quotes_action"
K_QUOTES_ERROR = "quotes_error"
K_DISPLAY_CCY = "display_currency"
# Quotation Extraction Playground — held in session only; nothing is persisted until the user
# chooses to save the extracted requirement as an RFQ.
K_PG_RESULT = "pg_result"          # buyer-side extraction, for the new-requirement path
K_PG_SELECTED = "pg_selected"
K_PG_ERROR = "pg_error"
K_PG_PENDING = "pg_pending"
K_PG_RFQ = "pg_rfq_id"             # the RFQ being tested against
K_PG_LINE = "pg_line_label"        # the chosen line item
K_PG_FOCUS = "pg_focus_row"        # jump from a review issue to its table line
K_PG_RUN = "pg_test_run"           # the current TestRun; throwaway, never persisted.
#: Set by the "Use a sample reply" button and consumed on the next run, *before* the text
#: area is created. Writing `pg_body` directly is a StreamlitAPIException: a widget's key
#: cannot be assigned once the widget exists, and the button sits below the text area.
K_PG_SAMPLE = "pg_load_sample"

# Analyst (Phase 3). Values are deliberately unlike any widget key on the page: a widget
# of the same name would overwrite the stored conversation with its own value.
K_AN_HISTORY = "an_conversation"    # {rfq_id: [result.to_dict(), ...]}, newest last
K_AN_PENDING = "an_pending_action"
K_AN_ERROR = "an_error"
K_AN_RFQ = "an_rfq_id"

# Award & execution (Phase 5). Values avoid every widget key used on that page, for the
# reason recorded above.
K_AW_ID = "aw_award_id"
K_AW_PENDING = "aw_pending_action"
K_AW_ERROR = "aw_error"
K_AW_ACK = "aw_acknowledged"
                                   # Deliberately not "pg_run": a widget key of the same
                                   # name would overwrite it with the button's bool.

PAGES: Dict[str, Any] = {}   # filled by app.py: name -> st.Page


@st.cache_resource(show_spinner=False)
def get_service() -> RFQService:
    settings = Settings.from_env()
    repo = RFQRepository(settings.db_path)
    return RFQService(repo, get_ai_service(settings), settings)


@st.cache_resource(show_spinner=False)
def get_supplier_service() -> "SupplierService":
    """Phase 2 service, sharing the Phase 1 repository and AI service."""
    from rfq_copilot.supplier_service import SupplierService
    base = get_service()
    return SupplierService(base.repo, base.ai, base.settings)


@st.cache_resource(show_spinner=False)
def get_analyst_service() -> "AnalystService":
    """Phase 3 analyst, over the same repository, AI service and Phase 2 comparison."""
    from rfq_copilot.analyst_service import AnalystService
    base = get_service()
    return AnalystService(base.ai, base.repo, base.settings, get_supplier_service())


@st.cache_resource(show_spinner=False)
def get_award_service() -> "AwardService":
    """Phase 5 award and execution, over the same repository and Phase 2 comparison."""
    from rfq_copilot.award_service import AwardService
    base = get_service()
    return AwardService(base.ai, base.repo, base.settings, get_supplier_service())


@st.cache_resource(show_spinner=False)
def get_playground_service() -> "PlaygroundService":
    """Quotation Extraction Playground, sharing the same repository and AI service."""
    from rfq_copilot.playground_service import PlaygroundService
    base = get_service()
    return PlaygroundService(base.ai, base.repo, base.settings)


@st.cache_resource(show_spinner=False)
def get_testbench() -> "SupplierTestbench":
    """Supplier-response test harness, sharing the same AI service as everything else."""
    from rfq_copilot.supplier_testbench import SupplierTestbench
    base = get_service()
    return SupplierTestbench(base.ai, base.settings)


def queue_award(action: Dict[str, Any]) -> None:
    st.session_state[K_AW_PENDING] = action


def take_pending_award() -> Optional[Dict[str, Any]]:
    return st.session_state.pop(K_AW_PENDING, None)


def queue_analyst(action: Dict[str, Any]) -> None:
    st.session_state[K_AN_PENDING] = action


def take_pending_analyst() -> Optional[Dict[str, Any]]:
    return st.session_state.pop(K_AN_PENDING, None)


def queue_playground(action: Dict[str, Any]) -> None:
    st.session_state[K_PG_PENDING] = action


def queue_quotes(action: Dict[str, Any]) -> None:
    st.session_state[K_PENDING_QUOTES] = action


def take_pending_quotes() -> Optional[Dict[str, Any]]:
    return st.session_state.pop(K_PENDING_QUOTES, None)


@st.cache_data(ttl=120, show_spinner=False)
def cached_health() -> Dict[str, Any]:
    return get_service().health()


def current_rfq() -> Optional[RFQ]:
    rid = st.session_state.get(K_RFQ_ID)
    if not rid:
        return None
    try:
        return get_service().get(rid)
    except Exception:
        st.session_state.pop(K_RFQ_ID, None)
        return None


#: The RFQ the landing page offers first. It is the one the README's walkthrough uses and
#: the only one seeded with every case the product is meant to handle.
DEMO_RFQ_ID = "rfq_stress_30"


def render_sidebar_progress() -> None:
    """Where the open RFQ has got to, in the same four steps the navigation names.

    The sidebar used to show only the Phase 1 readiness percentage, which reads as "this
    RFQ is 0% done" long after the quotes are in and an award has been made — the score
    stops being the story once the RFQ has been sent.
    """
    rfq = current_rfq()
    if rfq is None:
        st.caption("No RFQ open. Start one in the Copilot, or open a saved one.")
        return
    st.markdown("**Open RFQ**")
    st.caption(rfq.title or rfq.product or rfq.id)

    steps = [("RFQ built", rfq.completeness.ready_to_send or rfq.status.value == "supplier_ready")]
    try:
        sup = get_supplier_service()
        extracted = sup.has_responses(rfq.id) and any(
            b.response.extraction_status.value in ("extracted", "needs_review")
            for b in sup.store.list_bundles(rfq.id, active_only=True))
        steps.append(("Quotes read", bool(extracted)))
    except Exception:
        steps.append(("Quotes read", False))
    try:
        award = get_award_service().statuses().get(rfq.id, "")
        steps.append(("Award made", award in ("approved", "ready_to_execute", "supplier_notified",
                                              "order_handoff", "completed")))
        steps.append(("Order handed off", award in ("order_handoff", "completed")))
    except Exception:
        steps += [("Award made", False), ("Order handed off", False)]

    done = sum(1 for _, ok in steps if ok)
    st.progress(done / float(len(steps)), text="%d of %d steps" % (done, len(steps)))
    st.markdown('<div class="rfq-sub">%s</div>'
                % "<br>".join("%s %s" % ("✓" if ok else "○", label) for label, ok in steps),
                unsafe_allow_html=True)


def set_current(rfq_id: Optional[str]) -> None:
    if rfq_id:
        st.session_state[K_RFQ_ID] = rfq_id
    else:
        st.session_state.pop(K_RFQ_ID, None)


def queue(action: Dict[str, Any]) -> None:
    st.session_state[K_PENDING] = action


def take_pending() -> Optional[Dict[str, Any]]:
    return st.session_state.pop(K_PENDING, None)


def flash(text: str, kind: str = "success") -> None:
    st.session_state[K_FLASH] = (kind, text)


def show_flash() -> None:
    item = st.session_state.pop(K_FLASH, None)
    if not item:
        return
    kind, text = item
    getattr(st, kind, st.info)(text)


def go(page_name: str) -> None:
    page = PAGES.get(page_name)
    if page is not None:
        st.switch_page(page)
