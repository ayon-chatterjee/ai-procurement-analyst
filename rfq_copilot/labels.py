"""The words the product uses, in one place.

Every screen names the same things: an RFQ's status, who supplied a value, whether a
certificate is a claim or a fact, why a price cannot be compared. Those words were
previously written out at each call site, and they drifted — the same RFQ read "Draft" on
one screen and "Not ready" on another, an award was "Messages ready" in the list and
"ready to execute" on its own page, and a value the buyer typed was "Buyer provided",
"Buyer stated" and "Needs confirmation" depending on where you looked.

A reader cannot tell a deliberate distinction from an accident, so every difference in
wording reads as a difference in meaning. This module makes the vocabulary a single fact:
one label per state, imported by every page.

Pure: no Streamlit, no database. UI modules import the tables; nothing here knows how a
badge is drawn.
"""
from __future__ import annotations

from typing import Dict

# --------------------------------------------------------------------------- #
# RFQ status
# --------------------------------------------------------------------------- #
#: `(badge kind, label)`. "Ready to send" and "Supplier-ready" are a real distinction —
#: the first is computed, the second is something the buyer did — so both survive.
RFQ_STATUS: Dict[str, tuple] = {
    "draft": ("status-not", "Draft"),
    "in_progress": ("status-not", "In progress"),
    "ready": ("status-ready", "Ready to send"),
    "supplier_ready": ("status-sent", "Supplier-ready"),
}

# --------------------------------------------------------------------------- #
# Award status
# --------------------------------------------------------------------------- #
AWARD_STATUS: Dict[str, tuple] = {
    "draft": ("edited", "Draft"),
    "reviewed": ("recommended", "Reviewed"),
    "approved": ("status-ready", "Approved"),
    "ready_to_execute": ("status-ready", "Messages ready"),
    "supplier_notified": ("status-sent", "Suppliers notified"),
    "order_handoff": ("status-sent", "Order handed off"),
    "completed": ("status-sent", "Completed"),
    "cancelled": ("na", "Cancelled"),
}

#: What the Saved list shows beside an RFQ. Same states, prefixed so the word "Award" is
#: not ambiguous next to the RFQ's own status badge.
AWARD_STATUS_IN_LIST: Dict[str, tuple] = {
    key: (kind, "Award · %s" % label) for key, (kind, label) in AWARD_STATUS.items()
}

# --------------------------------------------------------------------------- #
# Where a value came from
# --------------------------------------------------------------------------- #
#: Keyed by `FieldStatus`. The buyer needs one question answered — did I say this, or did
#: the assistant suggest it? — so the labels lead with who, not with what.
PROVENANCE: Dict[str, tuple] = {
    "provided": ("buyer", "Buyer stated"),
    "recommended": ("ai", "AI recommended"),
    "unknown": ("unknown", "Buyer unsure"),
    "not_applicable": ("na", "Not applicable"),
    "conflict": ("conflict", "Conflict"),
    "missing": ("missing", "Missing"),
}

#: The same vocabulary for a line item's `Source`, where "edited" is worth distinguishing
#: because the buyer changed a value the assistant had proposed.
SOURCE: Dict[str, str] = {
    "buyer_explicit": "Buyer stated",
    "manual_edit": "Buyer edited",
    "ai_recommended": "AI recommended",
    "missing": "Missing",
}

#: The readiness checklist marks, in the same order the legend prints them.
PROVENANCE_MARKS = (
    ("ok", "✓", "Buyer stated"),
    ("ai", "✦", "AI recommended"),
    ("miss", "⚠", "Missing"),
    ("unk", "?", "Buyer unsure"),
    ("na", "—", "Not applicable"),
    ("conf", "!", "Conflict"),
)

# --------------------------------------------------------------------------- #
# Supplier claims
# --------------------------------------------------------------------------- #
#: A claim is what the supplier said; verified is what a document we hold shows. The
#: labels keep that difference in the words themselves, not in a colour.
CLAIM: Dict[str, tuple] = {
    "verified": ("buyer", "Verified by document"),
    "claimed": ("recommended", "Supplier claim"),
    "missing": ("missing", "Not provided"),
    "failed": ("conflict", "Failed"),
    "expired": ("conflict", "Expired"),
    "conflict": ("conflict", "Conflict"),
    "not_applicable": ("na", "Not applicable"),
}

# --------------------------------------------------------------------------- #
# Comparison cells
# --------------------------------------------------------------------------- #
#: What a cell that is not a plain price means. The table prints the short form; these
#: are the words used wherever there is room to explain.
CELL: Dict[str, tuple] = {
    "quoted": ("buyer", "Comparable price"),
    "needs_review": ("recommended", "Needs review"),
    "conflict": ("conflict", "Contradiction"),
    "unresolved": ("unknown", "Cannot be compared"),
    "not_quoted": ("missing", "Not quoted"),
    "no_response": ("na", "No response"),
}

#: Review-queue groupings, in the order a buyer should work through them: things that
#: stop a comparison first, things that merely want an answer last.
REVIEW_KINDS = (
    ("unmatched", "Unmatched supplier line"),
    ("probable_match", "Line match to confirm"),
    ("conflict", "Contradictory values"),
    ("unresolved_price", "Price that cannot be compared"),
    ("unverified_claim", "Claim without a certificate"),
    ("supplier_question", "Supplier is waiting on you"),
)

#: What the buyer can actually do about each kind. Section 9 of a review screen is
#: useless without this: a badge that says "needs review" and nothing else is a dead end.
REVIEW_GUIDANCE: Dict[str, str] = {
    "unmatched": "Pick the RFQ line this quote belongs to, or leave it unmatched and it "
                 "stays out of the comparison.",
    "probable_match": "Confirm the line the system inferred, or choose the right one. "
                      "Until you do, the price is shown with a review flag.",
    "conflict": "The supplier stated two different values. Ask them which applies, then "
                "record it here — both statements stay on the record either way.",
    "unresolved_price": "The price cannot be reduced to a per-piece figure. If the "
                        "supplier confirms one, enter it with Correct in the comparison "
                        "below; otherwise this line stays out of the totals.",
    "unverified_claim": "Ask the supplier for the certificate itself. It becomes verified "
                        "when that document is among the ones received — a quotation that "
                        "mentions a certificate is not the certificate.",
    "supplier_question": "Answer it here for the record. Nothing is sent to the supplier.",
}


# --------------------------------------------------------------------------- #
# Guard verdicts
# --------------------------------------------------------------------------- #
def describe_guard_status(status: str) -> str:
    """Turn a guard's internal verdict into something a buyer can act on.

    The guards speak in codes — `rejected: names Anhui Packaging Co, who is not this
    supplier` — which are exactly right in a log and wrong in a sentence addressed to the
    person who just typed something.
    """
    text = (status or "").strip()
    if not text or text == "ok":
        return ""
    if text == "fallback":
        return "the written draft could not be produced, so the standard letter is shown"
    reason = text.split(":", 1)[1].strip() if ":" in text else text
    return reason or "it could not be checked against the award"


def label_for(table: Dict[str, tuple], key: str, fallback_kind: str = "na") -> tuple:
    """A `(kind, label)` for a status, degrading to the raw value rather than raising.

    A status this build does not know about is a bug, but showing the buyer a blank badge
    or a traceback is worse than showing them the word itself.
    """
    known = table.get((key or "").strip())
    if known:
        return known
    return fallback_kind, (key or "").replace("_", " ").strip() or "unknown"
