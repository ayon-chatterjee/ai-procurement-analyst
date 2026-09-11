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
