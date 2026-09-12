"""What the application is willing to send in a buyer's name.

An award letter is the first thing in this system that leaves the building. A wrong figure
in an analyst answer costs a re-read; a wrong figure here is a price someone invoices
against. So the model's draft is checked against the facts it was given, and a draft that
fails is discarded in full rather than repaired — there is always a deterministic letter to
fall back on, which is flatter but cannot be wrong.

Two properties worth naming:

* **Cross-supplier leakage is caught by the same check that catches invention.** The
  allowance of permissible numbers is built from *this supplier's* fact pack, so a rival's
  price is simply a number that is not in it.
* **Supplier text is neutralised before it reaches a prompt.** A supplier document is
  untrusted input; a sentence in it that reads like an instruction is dropped and the
  buyer is told, rather than quietly sanitised into something plausible.

Pure: no database, no model, no Streamlit.
"""
from __future__ import annotations

import re
from typing import List, Optional, Sequence, Tuple

from .analyst_models import numbers_in
from .award_models import NOT_PROVIDED, CommunicationFacts
from .guards import norm_text

#: A letter longer than this is not a letter.
MAX_COMMUNICATION_CHARS = 1800

#: How much supplier-written text may reach a prompt. An instruction needs room; a
#: commercial term does not.
LABEL_LIMIT = 120
TERM_LIMIT = 300

#: Text shaped like an instruction to the model rather than a fact about the goods.
#: Detecting and dropping beats sanitising: the buyer sees that something was unreadable
#: and can go and look at the document, instead of reading a cleaned-up version of it.
_INSTRUCTION_SHAPED = re.compile(
    r"\b(ignore\s+(?:all\s+)?(?:previous|prior|above)|disregard\s+(?:the\s+)?(?:above|previous)"
    r"|system\s+prompt|new\s+instructions?|you\s+are\s+now|as\s+an\s+ai"
    r"|award\s+this\s+(?:quote|supplier)|choose\s+(?:us|this\s+supplier)"
    r"|respond\s+only\s+with)\b", re.I)

#: The delimiters the prompt itself uses. Supplier text must not be able to close its own
#: block and start talking to the model outside it.
_DELIMITERS = re.compile(r"<<<|>>>|```")

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_WHITESPACE = re.compile(r"\s+")

SUPPRESSED = "[term omitted: unreadable]"


def neutralise(text: str, limit: int = TERM_LIMIT) -> Tuple[str, bool]:
    """Make a supplier-written string safe to put in a prompt.

    Returns the text and whether anything was suppressed, so the caller can tell the buyer
    rather than only the model.
    """
    raw = _CONTROL.sub(" ", text or "")
    raw = _DELIMITERS.sub(" ", raw)
    raw = _WHITESPACE.sub(" ", raw).strip()
    if not raw:
        return "", False
    if _INSTRUCTION_SHAPED.search(raw):
        return SUPPRESSED, True
    if len(raw) > limit:
        raw = raw[:limit].rstrip() + "…"
    return raw, False


def stated(text: str, limit: int = TERM_LIMIT) -> Tuple[str, bool]:
    """A commercial term as the supplier wrote it, or an explicit statement that they
    did not write one. Never a default, never a blank."""
    clean, suppressed = neutralise(text, limit)
    return (clean or NOT_PROVIDED), suppressed


# --------------------------------------------------------------------------- #
# The communication guard
# --------------------------------------------------------------------------- #
#: A "-" only starts a number when nothing word-like precedes it.
_NUMBER_IN_TEXT = re.compile(r"(?<![\w.])-?\d[\d,]*\.?\d*")

#: Dates in the forms a letter would use. An invented delivery date is the most expensive
#: mistake available here, so any date must be one the supplier actually wrote.
_DATE = re.compile(
    r"\b(\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October"
    r"|November|December)\s+\d{4}"
    r"|(?:January|February|March|April|May|June|July|August|September|October|November"
    r"|December)\s+\d{1,2},?\s+\d{4})\b", re.I)

#: Commercial terms that must be quoted from the supplier, never composed.
_TERM_TOKEN = re.compile(
    r"\b(FOB|EXW|CIF|CFR|DDP|DAP|L/?C|letter of credit|net\s+\d+|\d+%\s*(?:advance|deposit))\b",
    re.I)

#: Promises the buyer has not made. A letter may confirm an award; it may not invent a
#: commitment, and it may not claim to have been sent by a system that sends nothing.
_COMMITMENT = re.compile(
    r"\b(we\s+guarantee|we\s+commit\s+to|this\s+is\s+(?:a\s+)?binding"
    r"|purchase\s+order\s+(?:is|has\s+been)\s+(?:issued|raised)"
    r"|payment\s+(?:will|shall)\s+be\s+made\s+(?:on|within))\b", re.I)

_SENT_CLAIM = re.compile(
    r"\b(this\s+(?:email|message)\s+(?:was|has\s+been)\s+sent"
    r"|we\s+have\s+(?:sent|dispatched|emailed)"
    r"|sent\s+via\s+our\s+system)\b", re.I)

_PLACEHOLDER = re.compile(r"\[[^\]]{0,60}\]|\bTBD\b|\bXXX+\b|<insert", re.I)

#: Figures a letter may use without them being in the fact pack: small ordinals and counts
#: that describe the letter itself ("the three lines below"). Anything larger has to be a
#: fact we supplied.
_FREE_SMALL_INTEGERS = 20.0


def _tolerance(value: float) -> float:
    return max(0.005, abs(value) * 1e-4)


def guard_communication(text: str, facts: CommunicationFacts,
                        other_supplier_names: Optional[Sequence[str]] = None
                        ) -> Tuple[Optional[str], str]:
    """Accept a draft only if the award data already supports every word of it.

    Returns `(body, status)`. A rejected draft is dropped whole: the deterministic letter
    is already a complete and correct message, so there is nothing to gain from showing
    one we cannot stand behind.
    """
    body = (text or "").strip()
    if not body:
        return None, "rejected: empty"
    if len(body) > MAX_COMMUNICATION_CHARS:
        return None, "rejected: too long"

    blob = norm_text(body)

    # -- another supplier must not appear, by name or by distinctive word ----
    # A word we ourselves supplied is not a leak: this RFQ is for "Corrugated Carton
    # Boxes", and one of the suppliers is "Gujarat Boxes Pvt Ltd". Flagging "boxes" would
    # reject every correct letter.
    supplied_words = set(norm_text(" ".join(facts.strings())).split())
    words = set(blob.split())
    for name in (other_supplier_names or []):
        if not name or norm_text(name) == norm_text(facts.supplier_name):
            continue
        if norm_text(name) and norm_text(name) in blob:
            return None, "rejected: names %s, who is not this supplier" % name
        for token in norm_text(name).split():
            if len(token) >= 5 and token not in supplied_words and token in words:
                return None, "rejected: mentions '%s', which belongs to %s" % (token, name)

    # -- every figure must be one we supplied --------------------------------
    allowed = facts.numbers()
    for token in _NUMBER_IN_TEXT.findall(body):
        try:
            value = float(token.replace(",", ""))
        except ValueError:
            continue
        if value.is_integer() and abs(value) <= _FREE_SMALL_INTEGERS:
            continue
        if not any(abs(value - a) <= _tolerance(a) for a in allowed):
            return None, "rejected: the figure %s is not in the award" % token

    # -- a date must be one the supplier wrote -------------------------------
    supplied = " ".join(facts.strings())
    for match in _DATE.findall(body):
        if match not in supplied:
            return None, "rejected: the date %s was never stated" % match

    # -- a commercial term must be quoted, not composed -----------------------
    # Padded so the comparison is word-wise: "L/C" normalises to "l c", which would
    # otherwise be found inside "B/L copy" and let an upgraded term through.
    supplied_terms = " %s " % norm_text(" ".join([
        facts.lead_time_stated, facts.payment_terms_stated, facts.delivery_terms_stated,
        facts.validity_stated, facts.minimum_order_stated]))
    for match in _TERM_TOKEN.findall(body):
        if (" %s " % norm_text(match)) not in supplied_terms:
            return None, "rejected: '%s' is not a term this supplier stated" % match

    hit = _COMMITMENT.search(body)
    if hit and norm_text(hit.group(0)) not in norm_text(supplied):
        return None, "rejected: promises something the buyer has not agreed (%s)" % hit.group(0)

    hit = _SENT_CLAIM.search(body)
    if hit:
        return None, "rejected: claims the message was sent (%s)" % hit.group(0)

    hit = _PLACEHOLDER.search(body)
    if hit:
        return None, "rejected: contains a placeholder (%s)" % hit.group(0).strip()

    return body, "ok"


def leaked_names(text: str, facts: CommunicationFacts,
                 other_supplier_names: Sequence[str]) -> List[str]:
    """Which rivals a piece of text mentions. Used to explain a rejection, and to check a
    buyer's own edit before it is sent."""
    blob = norm_text(text or "")
    out: List[str] = []
    for name in other_supplier_names or []:
        if not name or norm_text(name) == norm_text(facts.supplier_name):
            continue
        if norm_text(name) and norm_text(name) in blob and name not in out:
            out.append(name)
    return out
