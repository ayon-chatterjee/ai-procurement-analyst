"""Make supplier prices comparable — but only where that is mathematically honest.

Three separate jobs, deliberately not merged:

  normalise   divide a quoted price down to one piece, when we know how many pieces
              the quoted price covers. Per-kg or per-lot prices cannot be divided
              without information we do not have, so they stay UNRESOLVED.
  discount    evaluate a conditional discount only when the RFQ answers the condition.
              The base price is never overwritten.
  MOQ         compare the supplier's minimum against what the buyer actually wants.

Nothing here converts currency. Two prices in different currencies are not comparable
without a rate, and inventing one would be the most damaging thing this file could do.
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

from .schema import RFQ, LineItem
from .supplier_models import (
    PRICE_BASIS_DIVISOR, NormalizationStatus, PriceBasis, QuoteStatus, SupplierQuote,
)

_BASIS_WORDS = [
    (PriceBasis.PER_1000, r"per\s*1[,\s]?000|per\s*1k\b|/\s*1000|per\s*thousand"),
    (PriceBasis.PER_100, r"per\s*100\b|/\s*100\b|per\s*hundred"),
    (PriceBasis.PER_KG, r"per\s*kg|per\s*kilo|/\s*kg"),
    (PriceBasis.PER_SET, r"per\s*set|/\s*set"),
    (PriceBasis.PER_LOT, r"per\s*lot|lump\s*sum|/\s*lot"),
    (PriceBasis.PER_UNIT, r"per\s*(piece|pc|pcs|unit|each|box|carton)|/\s*(pc|pcs|piece|unit)"),
]


def infer_price_basis(text: str) -> PriceBasis:
    """Read a price basis out of the supplier's own phrasing."""
    t = (text or "").lower()
    for basis, pattern in _BASIS_WORDS:
        if re.search(pattern, t):
            return basis
    return PriceBasis.UNKNOWN


def normalize_price(quote: SupplierQuote) -> SupplierQuote:
    """Fill normalized_unit_price when, and only when, the division is safe."""
    if quote.unit_price is None:
        quote.normalization_status = NormalizationStatus.NOT_APPLICABLE
        quote.normalized_unit_price = None
        return quote

    divisor = PRICE_BASIS_DIVISOR.get(quote.price_basis)
    if divisor:
        base = quote.effective_unit_price if quote.effective_unit_price is not None else quote.unit_price
        quote.normalized_unit_price = round(base / divisor, 6)
        quote.normalization_status = NormalizationStatus.NORMALIZED
        if divisor != 1.0:
            quote.normalization_note = ("Reduced to a single piece from %s." % quote.original_price_text())
            if quote.effective_unit_price is not None:
                quote.normalization_note += (" The %s discount is included; the undiscounted price is %s."
                                             % (quote.discount.describe().split(" when")[0],
                                                _money(quote.currency, quote.unit_price / divisor)))
        else:
            quote.normalization_note = ""
            if quote.effective_unit_price is not None:
                quote.normalization_note = ("Includes the %s discount; the undiscounted price is %s."
                                            % (quote.discount.describe().split(" when")[0],
                                               _money(quote.currency, quote.unit_price)))
        return quote

    quote.normalized_unit_price = None
    quote.normalization_status = NormalizationStatus.UNRESOLVED
    reasons = {
        PriceBasis.PER_KG: "Quoted by weight. A per-piece price needs a verified weight per carton, "
                           "which the supplier has not given.",
        PriceBasis.PER_SET: "Quoted per set. A per-piece price needs the number of pieces in a set.",
        PriceBasis.PER_LOT: "Quoted as a lot price, which cannot be split across pieces.",
        PriceBasis.UNKNOWN: "The supplier did not make clear what one quoted price covers.",
    }
    quote.normalization_note = reasons.get(quote.price_basis, "Cannot be reduced to a per-piece price.")
    if quote.status == QuoteStatus.QUOTED:
        quote.status = QuoteStatus.UNRESOLVED
    return quote


# --------------------------------------------------------------------------- #
# Discounts
# --------------------------------------------------------------------------- #
_THRESHOLD = re.compile(
    r"(?P<word>above|over|exceed(?:ing|s)?|more than|greater than|at least|minimum of|from)\s*"
    r"(?P<qty>[0-9][0-9,\.]*)\s*(pcs|pieces|units|cartons|boxes)?", re.I)

#: Wordings that include the stated number. "At least 10,000" is met by exactly 10,000;
#: "above 10,000" is not. Reading both as strictly-greater once denied a discount to an
#: order that met its condition on the nose.
_INCLUSIVE_WORDS = ("at least", "minimum of", "from")


def parse_discount_threshold(condition: str) -> Optional[Tuple[float, bool]]:
    """The quantity threshold in a discount condition and whether it includes that number.

    Returns `(threshold, inclusive)`, or None when no threshold is stated.
    """
    if not condition:
        return None
    m = _THRESHOLD.search(condition)
    if not m:
        return None
    try:
        value = float(m.group("qty").replace(",", ""))
    except ValueError:
        return None
    word = (m.group("word") or "").lower()
    return value, any(word.startswith(w) for w in _INCLUSIVE_WORDS)


def rfq_total_quantity(rfq: RFQ) -> Optional[float]:
    total = 0.0
    seen = False
    for li in rfq.line_items:
        if li.quantity:
            total += float(li.quantity)
            seen = True
    return total if seen else None


def apply_discount(quote: SupplierQuote, rfq: Optional[RFQ] = None) -> SupplierQuote:
    """Work out the effective price, but only when the RFQ settles the condition.

    The base price is left untouched either way, and an unevaluable condition stays
    visible rather than being quietly treated as met or unmet.
    """
    d = quote.discount
    if not d.is_present or quote.unit_price is None:
        quote.effective_unit_price = None
        return quote

    parsed = parse_discount_threshold(d.condition)
    threshold, inclusive = parsed if parsed else (None, False)
    if not d.condition:
        d.applies, d.applies_reason = True, "The supplier attached no condition to this discount."
    elif threshold is None:
        d.applies = None
        d.applies_reason = ("The condition is not a quantity threshold we can evaluate: %r. "
                            "The base price is shown." % d.condition)
    else:
        total = rfq_total_quantity(rfq) if rfq is not None else None
        if total is None:
            d.applies = None
            d.applies_reason = ("The RFQ has no total quantity yet, so the %s threshold cannot be checked."
                                % "{:,.0f}".format(threshold))
        elif total >= threshold if inclusive else total > threshold:
            d.applies = True
            d.applies_reason = ("RFQ total of {:,.0f} {} the {:,.0f} threshold."
                                .format(total, "meets" if total == threshold else "exceeds",
                                        threshold))
        else:
            d.applies = False
            d.applies_reason = ("RFQ total of {:,.0f} does not reach the {:,.0f} threshold."
                                .format(total, threshold))

    if d.applies:
        if d.percent is not None:
            quote.effective_unit_price = round(quote.unit_price * (1.0 - d.percent / 100.0), 6)
        elif d.amount is not None:
            quote.effective_unit_price = round(max(quote.unit_price - d.amount, 0.0), 6)
    else:
        quote.effective_unit_price = None
    return quote


# --------------------------------------------------------------------------- #
# MOQ
# --------------------------------------------------------------------------- #
def check_moq(quote: SupplierQuote, line: Optional[LineItem]) -> SupplierQuote:
    """Flag, but never reject, a supplier whose minimum exceeds what the buyer wants."""
    quote.moq_constraint = False
    if quote.minimum_order_quantity is None or line is None or not line.quantity:
        return quote
    if float(line.quantity) < float(quote.minimum_order_quantity):
        quote.moq_constraint = True
        note = ("Supplier minimum is {:,.0f} but this line asks for {:,.0f}."
                .format(float(quote.minimum_order_quantity), float(line.quantity)))
        if note not in quote.issues:
            quote.issues.append(note)
    return quote


# --------------------------------------------------------------------------- #
# Lead time
# --------------------------------------------------------------------------- #
_RANGE = re.compile(r"(\d+)\s*(?:-|–|—|to)\s*(\d+)\s*(working\s+)?days?", re.I)
_SINGLE = re.compile(r"(\d+)\s*(working\s+)?days?", re.I)
_WEEKS = re.compile(r"(\d+)\s*weeks?", re.I)


def parse_lead_time(text: str) -> Tuple[Optional[float], bool]:
    """Return (days, was_interpreted). A range is interpreted, so it is labelled as such."""
    if not text:
        return None, False
    m = _RANGE.search(text)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        return (lo + hi) / 2.0, True           # midpoint, and we say so
    m = _SINGLE.search(text)
    if m:
        return float(m.group(1)), False
    m = _WEEKS.search(text)
    if m:
        return float(m.group(1)) * 7.0, True
    return None, False


_VALIDITY_DAYS = re.compile(r"(\d+)\s*days?", re.I)
_CONDITIONAL = re.compile(r"subject to|depend(?:ent|ing) on|conditional|fluctuat|volatil", re.I)


def parse_quote_validity(text: str) -> Tuple[Optional[float], bool]:
    """Return (days, is_conditional). A conditional validity is not a fixed period."""
    if not text:
        return None, False
    conditional = bool(_CONDITIONAL.search(text))
    m = _VALIDITY_DAYS.search(text)
    days = float(m.group(1)) if m else None
    if conditional:
        return (days, True)
    return (days, False)


# --------------------------------------------------------------------------- #
def refresh_derived_values(quote: SupplierQuote, rfq: Optional[RFQ] = None,
                           line: Optional[LineItem] = None) -> SupplierQuote:
    """Recompute everything derived from the supplier's stated figures.

    Order matters: the discount decides the effective price, normalisation divides it
    down, and the currency guard has the final say, because a number whose currency we
    cannot name must not reach the comparison however well it divides.
    """
    from .supplier_guards import guard_currency
    apply_discount(quote, rfq)
    normalize_price(quote)
    guard_currency(quote)
    check_moq(quote, line)
    return quote


def currencies_in(quotes: List[SupplierQuote]) -> List[str]:
    """Only currencies we actually recognise. A supplier writing "cents" has not named
    one, and listing it alongside USD and EUR would imply we had understood it."""
    from .supplier_guards import KNOWN_CURRENCIES
    out = []
    for q in quotes:
        c = (q.currency or "").strip().upper()
        if c and c in KNOWN_CURRENCIES and c not in out:
            out.append(c)
    return out


def unnamed_currency_quotes(quotes: List[SupplierQuote]) -> List[SupplierQuote]:
    """Priced quotes whose currency we could not identify."""
    from .supplier_guards import KNOWN_CURRENCIES
    return [q for q in quotes
            if q.has_price and (q.currency or "").strip().upper() not in KNOWN_CURRENCIES]


def comparable_across(quotes: List[SupplierQuote]) -> bool:
    """False when more than one currency is in play; we never invent a rate."""
    return len(currencies_in(quotes)) <= 1


def _money(currency: str, value: float) -> str:
    text = "%.4f" % value
    return "%s %s" % (currency, text.rstrip("0").rstrip(".") if "." in text else text)
