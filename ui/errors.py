"""What the buyer is told when something fails.

Every service in this app already raises errors written for a person — `AIError` carries a
`user_message`, `RFQStateError` and its siblings carry a sentence. The failure mode this
module exists to prevent is the *other* kind: an exception nobody anticipated reaching the
screen as `str(e)[:200]`, or as a Streamlit traceback, both of which tell the buyer
something true and useless while implying their work is gone.

Three things every failure message here does:

  * says what did not happen, in the buyer's terms
  * says what is still safe — almost always "nothing you entered was lost", because the
    services persist before they call the model
  * says what to do next

The technical detail is not thrown away; it moves behind the Show diagnostics switch,
where the person who needs it is the person who turned it on.
"""
from __future__ import annotations

import traceback
from typing import Optional

import streamlit as st

from rfq_copilot.ai_service import AIError


def message_for(e: BaseException, doing: str) -> str:
    """The sentence to show for this failure.

    `doing` completes "while …" — pass a phrase from the buyer's point of view, like
    "reading the supplier responses", not a function name.
    """
    if isinstance(e, AIError):
        return getattr(e, "user_message", "") or "The AI analysis failed."
    text = str(e).strip()
    if _is_expected(e) and text:
        return text
    return ("Something went wrong while %s. Nothing you entered was lost — reopen this "
            "page or try again. If it keeps happening, switch on Show diagnostics in the "
            "sidebar for the technical detail." % doing)


def _is_expected(e: BaseException) -> bool:
    """Errors the services raise deliberately, already phrased for a person.

    Matched by name rather than imported, so this module stays free of a dependency on
    every service package and a new one does not have to register itself here.
    """
    return type(e).__name__ in {
        "RFQStateError", "AnalystError", "AwardError", "PlaygroundError", "TestbenchError",
    }


def render_failure(e: BaseException, doing: str, container=None) -> None:
    """Show a failure, with the technical detail available but not in the way."""
    target = container or st
    target.error(message_for(e, doing), icon=":material/error:")
    _render_detail(e, target)


def _render_detail(e: BaseException, target) -> None:
    from . import state
    if not st.session_state.get(state.K_DIAGNOSTICS):
        return
    with target.expander("Technical detail", expanded=False):
        st.code("%s: %s" % (type(e).__name__, e))
        tb = "".join(traceback.format_exception(type(e), e, e.__traceback__))
        if tb.strip():
            st.code(tb[-4000:])


def guard_page(render, name: str) -> None:
    """Run a page, and turn any unhandled failure into a sentence.

    Streamlit renders a full traceback into the browser when a page raises, which is the
    right default while building and the wrong one in front of anyone else. Each page
    keeps its own handling for the failures it expects; this is the floor underneath it.
    """
    try:
        render()
    except Exception as e:                       # noqa: BLE001 - the whole point
        render_failure(e, "opening %s" % name)
        st.caption("The rest of the app is unaffected — pick another page from the sidebar.")


def describe_rate_error(error: str) -> str:
    """Rate-provider failures in the buyer's terms.

    The provider's own message ("HTTPError 429 …") says nothing a buyer can act on; what
    matters is that the figures on screen are each supplier's own.
    """
    if not error:
        return ""
    if "Showing rates from" in error:
        return error          # the FX service already wrote this one for a person
    return ("Live exchange rates could not be fetched, so prices are shown in each "
            "supplier's own currency rather than converted at a guessed rate.")
