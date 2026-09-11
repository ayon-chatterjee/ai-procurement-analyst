"""Deterministic checks between the model's reading of a supplier document and what
the application is willing to tell the buyer.

The model reports observations. This module decides trust, and it is deliberately
unforgiving:

  * evidence must be findable in the document, or the value is demoted
  * a certification is CLAIMED until a document actually backs it
  * a quote for an RFQ line that does not exist is dropped, not guessed at
  * a missing line stays missing; nothing here ever writes a zero
  * contradictory statements are preserved as a conflict, never averaged or picked
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .guards import norm_text, similarity
from .schema import RFQ
from .supplier_models import (
    Certification, ClaimStatus, Discount, Evidence, MatchStatus, NormalizationStatus, PriceBasis,
    QuestionnaireResponse, QuoteStatus, SourceDocument, SupplierQuestion, SupplierQuote, ValueSource,
)

#: How much of an evidence span must appear in the document before we call it verified.
EVIDENCE_THRESHOLD = 0.72


def _squash(text: str) -> str:
    return re.sub(r"[^0-9a-z]+", "", (text or "").lower())


def evidence_is_findable(quoted_text: str, haystacks: Sequence[str]) -> bool:
    """True when the quoted span really occurs in one of the documents.

    Compared with punctuation and spacing removed, because a model will faithfully
    quote "USD 0.47/pc" from a document that wrote "USD 0.47 / pc".
    """
    if not quoted_text or not quoted_text.strip():
        return False
    needle_soft, needle_hard = norm_text(quoted_text), _squash(quoted_text)
    if len(needle_hard) < 3:
        return False
    for hay in haystacks:
        if needle_soft and needle_soft in norm_text(hay):
            return True
        if needle_hard and needle_hard in _squash(hay):
            return True
    # fall back to token overlap for a long span the model lightly reflowed
    for hay in haystacks:
        if similarity(quoted_text, hay) >= 0.9:
            return True
    tokens = [t for t in norm_text(quoted_text).split() if len(t) > 1]
    if len(tokens) >= 4:
        for hay in haystacks:
            h = norm_text(hay)
            hits = sum(1 for t in tokens if t in h)
            if hits / float(len(tokens)) >= EVIDENCE_THRESHOLD:
                return True
    return False


def build_evidence(raw: Optional[Dict[str, Any]], response_id: str, documents: List[SourceDocument],
                   store: Dict[str, Evidence]) -> Optional[str]:
    """Turn a model evidence reference into a stored Evidence row, if it checks out.

    Returns the evidence id, or None when the span cannot be found in any document -
    in which case the caller must treat the value as unverified.
    """
    if not raw:
        return None
    quoted = str(raw.get("quoted_text") or "").strip()
    location = str(raw.get("location") or "").strip()
    if not quoted:
        return None

    haystacks = [d.raw_text for d in documents if d.raw_text]
    verified = evidence_is_findable(quoted, haystacks)

    doc = _document_for_location(location, documents) or (documents[0] if documents else None)
    ev = Evidence(
        response_id=response_id,
        document_id=doc.id if doc else "",
        document_name=doc.filename if doc else "",
        source_type=doc.media_type if doc else "",
        location=location,
        quoted_text=quoted[:400],
        verified=verified,
    )
    _fill_position(ev, location)
    store[ev.id] = ev
    return ev.id


def _document_for_location(location: str, documents: List[SourceDocument]) -> Optional[SourceDocument]:
    """Pick the document a location label refers to when several were supplied."""
    if len(documents) <= 1:
        return documents[0] if documents else None
    loc = (location or "").lower()
    for d in documents:
        if d.filename and d.filename.lower() in loc:
            return d
    hints = (("page", "pdf"), ("sheet", "xlsx"), ("cell", "xlsx"), ("paragraph", "docx"), ("image", "image"))
    for word, media in hints:
        if word in loc:
            for d in documents:
                if d.media_type == media:
                    return d
    return documents[0]


_PAGE = re.compile(r"page\s*(\d+)", re.I)
_SHEET = re.compile(r"sheet[:\s]+([^·|,]+)", re.I)
_CELL = re.compile(r"cell\s*([A-Z]{1,3}\d+)", re.I)
_PARA = re.compile(r"paragraph\s*(\d+)", re.I)
_LINE = re.compile(r"line\s*(\d+)", re.I)


def _fill_position(ev: Evidence, location: str) -> None:
    if not location:
        return
    m = _PAGE.search(location)
    if m:
        ev.page = int(m.group(1))
    m = _SHEET.search(location)
    if m:
        ev.sheet = m.group(1).strip()
    m = _CELL.search(location)
    if m:
        ev.cell = m.group(1).upper()
    m = _PARA.search(location)
    if m:
        ev.paragraph = int(m.group(1))
    m = _LINE.search(location)
    if m and ev.row is None:
        ev.row = int(m.group(1))


# --------------------------------------------------------------------------- #
# Quotes
# --------------------------------------------------------------------------- #
_VALID_BASES = {b.value for b in PriceBasis}


def guard_quote(quote: SupplierQuote, evidence: Dict[str, Evidence], confidence_ceiling: float = 1.0) -> SupplierQuote:
    """Downgrade anything we cannot stand behind, and say why."""
    # price basis must be one we understand
    if quote.price_basis.value not in _VALID_BASES:
        quote.price_basis = PriceBasis.UNKNOWN

    # a price we cannot name a currency for is not comparable
    guard_currency(quote)

    # evidence: a price with no findable evidence is not a quote we will assert
    has_evidence = any(evidence.get(e) and evidence[e].verified for e in quote.evidence_ids)
    if quote.has_price and not has_evidence:
        quote.status = QuoteStatus.NEEDS_REVIEW
        quote.confidence = min(quote.confidence or 1.0, 0.4)
        note = "The quoted price could not be traced back to the document text."
        if note not in quote.issues:
            quote.issues.append(note)

    # an indicative price is never presented as firm
    if quote.price_is_indicative and quote.status == QuoteStatus.QUOTED:
        quote.status = QuoteStatus.NEEDS_REVIEW
        note = "The supplier hedged this price rather than confirming it."
        if note not in quote.issues:
            quote.issues.append(note)

    # matching: an unmatched or contested line must not sit in a comparison cell
    if quote.match_status in (MatchStatus.UNMATCHED, MatchStatus.CONFLICT):
        quote.line_item_id = None
        if quote.status == QuoteStatus.QUOTED:
            quote.status = QuoteStatus.NEEDS_REVIEW
    elif quote.match_status == MatchStatus.PROBABLE_MATCH and quote.status == QuoteStatus.QUOTED:
        quote.status = QuoteStatus.NEEDS_REVIEW

    # never claim more certainty than the source medium allows
    if quote.confidence is None:
        quote.confidence = confidence_ceiling
    quote.confidence = max(0.0, min(float(quote.confidence), confidence_ceiling))

    # a quote with no price is an absence, not a zero
    if not quote.has_price and quote.status == QuoteStatus.QUOTED:
        quote.status = QuoteStatus.NOT_QUOTED
    return quote


def guard_line_ids(quotes: List[SupplierQuote], rfq: RFQ) -> List[str]:
    """Drop references to RFQ lines that do not exist. Returns audit notes."""
    valid = {li.id for li in rfq.line_items}
    notes = []
    for q in quotes:
        if q.line_item_id and q.line_item_id not in valid:
            notes.append("Quote %r referenced unknown RFQ line %s; the match was discarded."
                         % (q.supplier_line_label, q.line_item_id))
            q.line_item_id = None
            q.match_status = MatchStatus.UNMATCHED
            q.status = QuoteStatus.NEEDS_REVIEW
    return notes


def guard_no_invented_lines(quotes: List[SupplierQuote], documents: List[SourceDocument]) -> List[str]:
    """Every quoted price must correspond to a number that appears in the document."""
    haystacks = [_squash(d.raw_text) for d in documents if d.raw_text]
    notes = []
    for q in quotes:
        if q.unit_price is None:
            continue
        variants = {
            _squash("%g" % q.unit_price),
            _squash("%.2f" % q.unit_price),
            _squash("%.0f" % q.unit_price) if float(q.unit_price).is_integer() else "",
        }
        variants.discard("")
        if not any(v and any(v in h for h in haystacks) for v in variants):
            note = "The price %s does not appear in the supplier's document." % q.unit_price
            if note not in q.issues:
                q.issues.append(note)
            q.status = QuoteStatus.NEEDS_REVIEW
            q.confidence = min(q.confidence or 1.0, 0.3)
            notes.append("%s: %s" % (q.supplier_line_label, note))
    return notes


# --------------------------------------------------------------------------- #
# Certifications
# --------------------------------------------------------------------------- #
_CERT_CANON = {
    "iso9001": "ISO 9001", "iso 9001": "ISO 9001", "iso90012015": "ISO 9001",
    "iso14001": "ISO 14001", "fsc": "FSC", "brc": "BRC", "sedex": "SEDEX",
    "bsci": "BSCI", "iso22000": "ISO 22000", "reach": "REACH", "rohs": "RoHS",
}


def canonical_cert_name(raw: str) -> str:
    key = _squash(raw)
    for k, v in _CERT_CANON.items():
        if _squash(k) and _squash(k) in key:
            return v
    return (raw or "").strip()


def guard_certification(cert: Certification, documents: List[SourceDocument],
                        evidence: Dict[str, Evidence]) -> Certification:
    """A certification is CLAIMED unless a document we actually hold supports it.

    Saying "we are ISO 9001 certified" is a claim. Saying a certificate is attached is
    still only a claim unless the attachment is among the documents we received.
    """
    cert.name = canonical_cert_name(cert.raw_name or cert.name) or cert.name
    has_evidence = any(evidence.get(e) and evidence[e].verified for e in cert.evidence_ids)

    held = None
    if cert.document_reference:
        ref = _squash(cert.document_reference)
        for d in documents:
            if ref and (ref in _squash(d.filename) or _squash(d.filename) in ref):
                held = d
                break
    if held is None and cert.document_id:
        held = next((d for d in documents if d.id == cert.document_id), None)

    if held is not None:
        cert.status = ClaimStatus.VERIFIED
        cert.document_id = held.id
        cert.note = "Supported by %s." % held.filename
    else:
        cert.status = ClaimStatus.CLAIMED
        if cert.document_reference:
            cert.note = ("The supplier refers to a certificate (%s) but it was not among the documents "
                         "received, so this remains a claim." % cert.document_reference)
        else:
            cert.note = "Stated by the supplier with no certificate attached."
    if not has_evidence:
        cert.note = (cert.note + " The claim could not be traced to the document text.").strip()
        cert.confidence = min(cert.confidence or 1.0, 0.4)

    if cert.expiry_date:
        cert.status = _expiry_status(cert.expiry_date, cert.status)
    return cert


_DATE = re.compile(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})")


def _expiry_status(expiry: str, current: ClaimStatus) -> ClaimStatus:
    from datetime import date
    m = _DATE.search(expiry or "")
    if not m:
        return current
    try:
        d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return current
    return ClaimStatus.EXPIRED if d < date.today() else current


# --------------------------------------------------------------------------- #
# Questionnaire
# --------------------------------------------------------------------------- #
_AFFIRMATIVE = re.compile(r"\b(yes|we are|we do|certified|available|can (?:do|offer|supply)|compliant)\b", re.I)


def guard_questionnaire(answers: List[QuestionnaireResponse], rfq: RFQ,
                        documents: List[SourceDocument], evidence: Dict[str, Evidence]) -> Tuple[List[QuestionnaireResponse], List[str]]:
    """Keep only answers that map to a real RFQ question, and never upgrade a bare yes."""
    valid_fields = {fv.key for fv in rfq.fields.values()}
    valid_questions = {q.id: q for q in rfq.questions}
    kept, notes = [], []

    for a in answers:
        if a.question_id and a.question_id in valid_questions:
            q = valid_questions[a.question_id]
            a.field_key = a.field_key or (q.field_key or "")
            a.question_text = a.question_text or q.question
        elif a.field_key and a.field_key in valid_fields:
            match = next((q for q in rfq.questions if q.field_key == a.field_key), None)
            if match:
                a.question_id = match.id
                a.question_text = a.question_text or match.question
        else:
            notes.append("Dropped a questionnaire answer for unknown field %r." % (a.field_key or a.question_id))
            continue

        has_evidence = any(evidence.get(e) and evidence[e].verified for e in a.evidence_ids)
        if not a.answer.strip():
            a.status = ClaimStatus.MISSING
        elif has_evidence:
            # an affirmative answer is a claim; only a document makes it verified
            a.status = ClaimStatus.CLAIMED if _AFFIRMATIVE.search(a.answer) else ClaimStatus.CLAIMED
        else:
            a.status = ClaimStatus.CLAIMED
            a.note = "Recorded from the supplier's wording; the exact span could not be located."
            a.confidence = min(a.confidence or 1.0, 0.4)
        kept.append(a)
    return kept, notes


def missing_questionnaire_items(answered: List[QuestionnaireResponse], rfq: RFQ) -> List[QuestionnaireResponse]:
    """Unanswered questionnaire items are MISSING - never assumed to be 'no'."""
    seen = {a.field_key for a in answered if a.field_key}
    out = []
    for q in rfq.questions:
        if not q.field_key or q.field_key in seen:
            continue
        out.append(QuestionnaireResponse(
            question_id=q.id, field_key=q.field_key, question_text=q.question,
            answer="", status=ClaimStatus.MISSING, value_source=ValueSource.MISSING,
            note="The supplier did not address this."))
    return out


# --------------------------------------------------------------------------- #
# Conflicts and revisions
# --------------------------------------------------------------------------- #
def record_conflicts(raw_conflicts: List[Dict[str, Any]], response_id: str,
                     documents: List[SourceDocument], store: Dict[str, Evidence]) -> List[Dict[str, Any]]:
    """Keep every side of a contradiction, with its own evidence. Never resolve it here."""
    out = []
    for c in raw_conflicts or []:
        values = []
        for v in c.get("values") or []:
            ev_id = build_evidence(v.get("evidence"), response_id, documents, store)
            values.append({"value": str(v.get("value") or ""), "evidence_id": ev_id})
        if len(values) < 2:
            continue        # a "conflict" with one side is not a conflict
        out.append({
            "topic": str(c.get("topic") or "").strip() or "unlabelled",
            "description": str(c.get("description") or "").strip(),
            "values": values,
        })
    return out


def resolve_revision_chain(responses: List[Any]) -> List[str]:
    """Mark the newest response active and everything it supersedes inactive.

    Earlier responses are never deleted; they stay queryable as history.
    """
    notes = []
    if not responses:
        return notes
    ordered = sorted(responses, key=lambda r: (r.received_at or "", r.created_at or ""))
    active = ordered[-1]
    for r in ordered:
        was = r.is_active
        r.is_active = (r.id == active.id)
        r.superseded_by_id = None if r.is_active else active.id
        if was and not r.is_active:
            notes.append("%s superseded by the later response %s." % (r.id, active.id))
    if len(ordered) > 1:
        active.revises_response_id = ordered[-2].id
    return notes

# --------------------------------------------------------------------------- #
# Currency
# --------------------------------------------------------------------------- #
#: Codes we are willing to treat as a stated currency.
KNOWN_CURRENCIES = {
    "USD", "EUR", "GBP", "INR", "CNY", "RMB", "JPY", "VND", "TRY", "AUD", "CAD",
    "SGD", "HKD", "CHF", "SEK", "PLN", "MXN", "BRL", "ZAR", "AED", "THB", "IDR", "MYR", "KRW",
}

#: Only symbols with one plausible reading are mapped. "$" is deliberately absent:
#: it could be USD, AUD, CAD or SGD, and choosing one would be a guess.
UNAMBIGUOUS_SYMBOLS = {"€": "EUR", "£": "GBP", "₹": "INR", "₺": "TRY", "₫": "VND", "₩": "KRW"}

#: Words that name a fraction of some currency without naming the currency itself.
SUBUNIT_WORDS = {"cent", "cents", "paise", "paisa", "pence", "penny", "kurus", "kuruş", "fen", "sen"}


def guard_currency(quote: SupplierQuote) -> SupplierQuote:
    """A price is only comparable when we know what currency it is in.

    A supplier writing "41 cents" has not told us whether that is a US cent or an
    Indian paisa, and the difference is roughly a factor of eighty. Rather than pick
    one, the quote is held for review and kept out of the price column.
    """
    raw = (quote.currency or "").strip()
    if not raw:
        if quote.has_price:
            quote.status = QuoteStatus.NEEDS_REVIEW
            _add_issue(quote, "The supplier did not state a currency for this price.")
            _block_normalization(quote, "No currency was stated, so this price cannot be compared.")
        return quote

    upper = raw.upper()
    if upper in KNOWN_CURRENCIES:
        quote.currency = "RMB" if upper == "RMB" else upper
        return quote
    if raw in UNAMBIGUOUS_SYMBOLS:
        quote.currency = UNAMBIGUOUS_SYMBOLS[raw]
        return quote

    lowered = raw.lower().strip(". ")
    if lowered in SUBUNIT_WORDS:
        quote.status = QuoteStatus.NEEDS_REVIEW
        _add_issue(quote, "The supplier quoted in %r without naming the currency, so the price "
                          "cannot be compared until they confirm it." % raw)
        _block_normalization(quote, "%r names a fraction of an unnamed currency." % raw)
        return quote

    if raw == "$" or upper == "DOLLAR" or upper == "DOLLARS":
        quote.status = QuoteStatus.NEEDS_REVIEW
        _add_issue(quote, "The supplier wrote %r, which does not identify which dollar." % raw)
        _block_normalization(quote, "The dollar symbol alone does not identify a currency.")
        return quote

    quote.status = QuoteStatus.NEEDS_REVIEW
    _add_issue(quote, "%r was not recognised as a currency." % raw)
    _block_normalization(quote, "%r was not recognised as a currency." % raw)
    return quote


def _add_issue(quote: SupplierQuote, note: str) -> None:
    if note not in quote.issues:
        quote.issues.append(note)


def _block_normalization(quote: SupplierQuote, reason: str) -> None:
    quote.normalized_unit_price = None
    quote.normalization_status = NormalizationStatus.UNRESOLVED
    quote.normalization_note = reason
