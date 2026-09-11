"""Real exchange rates, fetched and cited — never invented.

Converting currencies is only defensible if the buyer can see which rate was used and
when it was published. So every converted figure here carries its rate, the provider it
came from and the date that provider published it, and the supplier's original quote is
never overwritten.

If a rate cannot be fetched, or a currency is not in the table, conversion simply does
not happen and the original currency is shown. There is no fallback rate, because a
made-up rate is worse than no comparison at all.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

#: Keyless public endpoints, tried in order. Each returns rates relative to a base.
PROVIDERS = [
    {"name": "open.er-api.com", "url": "https://open.er-api.com/v6/latest/%s",
     "rates_key": "rates", "date_keys": ("time_last_update_utc", "time_last_update")},
    {"name": "frankfurter.app (ECB)", "url": "https://api.frankfurter.app/latest?from=%s",
     "rates_key": "rates", "date_keys": ("date",)},
]

DEFAULT_TTL_SECONDS = 6 * 3600
TIMEOUT_SECONDS = 12


@dataclass
class RateTable:
    """A set of rates from one provider at one point in time."""
    base: str
    rates: Dict[str, float] = field(default_factory=dict)
    source: str = ""
    as_of: str = ""            # the date the provider published
    fetched_at: float = 0.0    # unix time we retrieved it
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.rates) and not self.error

    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.fetched_at) if self.fetched_at else float("inf")

    def supports(self, currency: str) -> bool:
        c = (currency or "").strip().upper()
        return c == self.base or c in self.rates

    def rate(self, frm: str, to: str) -> Optional[float]:
        """Units of `to` per one unit of `frm`, via the table's base currency."""
        frm, to = (frm or "").strip().upper(), (to or "").strip().upper()
        if not frm or not to:
            return None
        if frm == to:
            return 1.0
        per_from = 1.0 if frm == self.base else self.rates.get(frm)
        per_to = 1.0 if to == self.base else self.rates.get(to)
        if not per_from or not per_to:
            return None
        return per_to / per_from

    def convert(self, amount: float, frm: str, to: str) -> Optional[Tuple[float, float]]:
        """Return (converted_amount, rate_used), or None when we cannot do it honestly."""
        r = self.rate(frm, to)
        if r is None or amount is None:
            return None
        return (amount * r, r)

    def describe(self) -> str:
        if not self.ok:
            return "No exchange rates available"
        return "1 %s reference, rates from %s published %s" % (self.base, self.source, self.as_of or "recently")

    def to_dict(self) -> Dict[str, object]:
        return {"base": self.base, "rates": self.rates, "source": self.source,
                "as_of": self.as_of, "fetched_at": self.fetched_at, "error": self.error}

    @classmethod
    def from_dict(cls, d: Dict[str, object]) -> "RateTable":
        return cls(base=str(d.get("base") or "USD"),
                   rates={str(k): float(v) for k, v in (d.get("rates") or {}).items()},
                   source=str(d.get("source") or ""), as_of=str(d.get("as_of") or ""),
                   fetched_at=float(d.get("fetched_at") or 0.0), error=str(d.get("error") or ""))


class FxService:
    """Fetches and caches rate tables. One network call per base per TTL."""

    def __init__(self, cache_path: Optional[str] = None, ttl_seconds: int = DEFAULT_TTL_SECONDS,
                 opener=None):
        self.cache_path = cache_path
        self.ttl = ttl_seconds
        self._opener = opener or urllib.request.urlopen
        self._memory: Dict[str, RateTable] = {}

    # -- public ---------------------------------------------------------------
    def rates(self, base: str = "USD", force: bool = False) -> RateTable:
        base = (base or "USD").strip().upper()
        cached = self._memory.get(base) or self._read_cache(base)
        if cached and cached.ok and not force and cached.age_seconds() < self.ttl:
            self._memory[base] = cached
            return cached

        fetched = self._fetch(base)
        if fetched.ok:
            self._memory[base] = fetched
            self._write_cache(fetched)
            return fetched
        # a stale table still names its own date, so it beats refusing outright
        if cached and cached.ok:
            cached.error = "Showing rates from %s; a fresh fetch failed (%s)." % (cached.as_of, fetched.error)
            self._memory[base] = cached
            return cached
        return fetched

    # -- internals ------------------------------------------------------------
    def _fetch(self, base: str) -> RateTable:
        problems = []
        for p in PROVIDERS:
            try:
                req = urllib.request.Request(p["url"] % base, headers={"User-Agent": "ai-procurement-analyst/1.0"})
                with self._opener(req, timeout=TIMEOUT_SECONDS) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
            except (urllib.error.URLError, urllib.error.HTTPError, ValueError, OSError) as e:
                problems.append("%s: %s" % (p["name"], type(e).__name__))
                continue
            raw = payload.get(p["rates_key"]) or {}
            rates = {}
            for k, v in raw.items():
                try:
                    rates[str(k).upper()] = float(v)
                except (TypeError, ValueError):
                    continue
            if not rates:
                problems.append("%s: no rates in response" % p["name"])
                continue
            as_of = ""
            for key in p["date_keys"]:
                if payload.get(key):
                    as_of = str(payload[key])
                    break
            return RateTable(base=base, rates=rates, source=p["name"], as_of=as_of, fetched_at=time.time())
        return RateTable(base=base, error="; ".join(problems) or "no provider responded")

    def _read_cache(self, base: str) -> Optional[RateTable]:
        if not self.cache_path or not os.path.exists(self.cache_path):
            return None
        try:
            with open(self.cache_path, "r", encoding="utf-8") as f:
                blob = json.load(f)
        except (ValueError, OSError):
            return None
        entry = (blob or {}).get(base)
        return RateTable.from_dict(entry) if entry else None

    def _write_cache(self, table: RateTable) -> None:
        if not self.cache_path:
            return
        blob = {}
        if os.path.exists(self.cache_path):
            try:
                with open(self.cache_path, "r", encoding="utf-8") as f:
                    blob = json.load(f) or {}
            except (ValueError, OSError):
                blob = {}
        blob[table.base] = table.to_dict()
        try:
            os.makedirs(os.path.dirname(self.cache_path) or ".", exist_ok=True)
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(blob, f)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
@dataclass
class Converted:
    """One converted figure, with everything needed to defend it."""
    amount: float
    currency: str
    original_amount: float
    original_currency: str
    rate: float
    source: str
    as_of: str

    @property
    def is_conversion(self) -> bool:
        return self.original_currency != self.currency

    def describe_rate(self) -> str:
        if not self.is_conversion:
            return ""
        return ("1 %s = %s %s, %s as of %s"
                % (self.original_currency, _trim(self.rate), self.currency, self.source, self.as_of or "today"))


def convert_amount(table: Optional[RateTable], amount: Optional[float], frm: str, to: str) -> Optional[Converted]:
    """Convert, or return None so the caller shows the original untouched."""
    if amount is None or not frm or not to:
        return None
    frm, to = frm.strip().upper(), to.strip().upper()
    if frm == to:
        return Converted(amount=amount, currency=to, original_amount=amount, original_currency=frm,
                         rate=1.0, source=table.source if table else "", as_of=table.as_of if table else "")
    if table is None or not table.ok:
        return None
    got = table.convert(amount, frm, to)
    if got is None:
        return None
    value, rate = got
    return Converted(amount=value, currency=to, original_amount=amount, original_currency=frm,
                     rate=rate, source=table.source, as_of=table.as_of)


def _trim(value: float) -> str:
    text = "%.6f" % value
    text = text.rstrip("0").rstrip(".")
    return text or "0"
