"""What the application is willing to accept from the analyst model.

The model proposes an analytical request and, later, a sentence describing a result.
Neither is trusted. This module turns a proposal into something the engine can execute —
or refuses it. A refusal is a legitimate answer: saying "I can't answer that from this
data" is always better than answering a question we only half understood.

Three jobs:

* resolve the names a buyer used to the ids this RFQ actually holds
* validate the request against the intent/filter tables in `analyst_models`
* check the model's narration says nothing the calculated result does not support
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple, Union

from .analyst_models import (
    FILTER_RULES, GRAINS, INTENT_RULES, LOOKUP_FIELDS,
    QUESTIONNAIRE_PREFIX, AnalystQuery, AnalystResult, EvidenceTarget, EvidenceTopic,
    FIELD_GRAINS, Filter, Grain, Hypothetical, Intent, RawQuery, Refusal,
)
from .guards import norm_text
from .schema import RFQ, LineItem
from .supplier_guards import KNOWN_CURRENCIES, canonical_cert_name
from .supplier_models import Supplier
from .supplier_service import CellState


def _squash(text: str) -> str:
    return norm_text(text).replace(" ", "")


def _tokens(text: str) -> List[str]:
    return [t for t in norm_text(text).split(" ") if t]


# --------------------------------------------------------------------------- #
# Name resolution
# --------------------------------------------------------------------------- #
@dataclass
class Resolved:
    id: str
    how: str = ""            # a note for the audit trail, e.g. "'Anhui' read as …"


@dataclass
class Ambiguous:
    candidates: List[str]


@dataclass
class Unknown:
    candidates: List[str]


Resolution = Union[Resolved, Ambiguous, Unknown]

#: "Supplier B" is how the brief talks, not how this RFQ does. Mapping a letter onto a
#: position in the supplier list would be a guess dressed as an answer, so it is refused
#: and the real names are offered instead.
_LETTER_LABEL = re.compile(r"^(?:supplier|vendor)?\s*([a-z])$", re.I)


def resolve_supplier(text: str, suppliers: Sequence[Supplier]) -> Resolution:
    """Find the one supplier a buyer meant, or say why we cannot."""
    names = [s.name for s in suppliers]
    raw = (text or "").strip()
    if not raw:
        return Unknown(names)

    for s in suppliers:                                   # an id, verbatim
        if raw == s.id:
            return Resolved(s.id)
    for s in suppliers:                                   # the exact name
        if raw.casefold() == (s.name or "").casefold():
            return Resolved(s.id)

    if _LETTER_LABEL.match(raw):
        return Unknown(names)

    squashed = _squash(raw)
    if squashed:
        hits = [s for s in suppliers if _squash(s.name) == squashed]
        if len(hits) == 1:
            return Resolved(hits[0].id, "'%s' read as %s" % (raw, hits[0].name))

    want = set(_tokens(raw))
    if want:
        hits = [s for s in suppliers if want.issubset(set(_tokens(s.name)))]
        if len(hits) == 1:
            return Resolved(hits[0].id, "'%s' read as %s" % (raw, hits[0].name))
        if len(hits) > 1:
            return Ambiguous([s.name for s in hits])

        # a prefix, but only a long enough one to be meaningful
        long_enough = [t for t in want if len(t) >= 4]
        if long_enough:
            hits = [s for s in suppliers
                    if all(any(tok.startswith(t) for tok in _tokens(s.name)) for t in long_enough)]
            if len(hits) == 1:
                return Resolved(hits[0].id, "'%s' read as %s" % (raw, hits[0].name))
            if len(hits) > 1:
                return Ambiguous([s.name for s in hits])
    return Unknown(names)


_LINE_NUMBER = re.compile(r"^(?:line[\s\-_]*|#)?0*(\d{1,4})$", re.I)
_LINE_ID = re.compile(r"^line[\s\-_]*0*(\d{1,4})$", re.I)


def resolve_line(text: str, lines: Sequence[LineItem]) -> Resolution:
    """Accept the several ways a buyer refers to a line: its id, its number, or what it is."""
    ids = [li.id for li in lines]
    raw = (text or "").strip()
    if not raw:
        return Unknown(ids)

    for li in lines:
        if raw.casefold() == li.id.casefold():
            return Resolved(li.id)

    m = _LINE_ID.match(raw) or _LINE_NUMBER.match(raw)
    if m:
        want = "LINE-%03d" % int(m.group(1))
        for li in lines:
            if li.id == want:
                return Resolved(li.id, "'%s' read as %s" % (raw, li.id))
        return Unknown(ids)

    squashed = _squash(raw)
    if squashed:
        hits = [li for li in lines
                if squashed in _squash("%s %s %s" % (li.product, li.description, li.spec_summary()))]
        if len(hits) == 1:
            return Resolved(hits[0].id, "'%s' read as %s" % (raw, hits[0].id))
        if len(hits) > 1:
            return Ambiguous([h.id for h in hits])

    want = set(_tokens(raw))
    if want:
        hits = [li for li in lines
                if want.issubset(set(_tokens("%s %s %s" % (li.product, li.description,
                                                           li.spec_summary()))))]
        if len(hits) == 1:
            return Resolved(hits[0].id, "'%s' read as %s" % (raw, hits[0].id))
        if len(hits) > 1:
            return Ambiguous([h.id for h in hits])
    return Unknown(ids)


# --------------------------------------------------------------------------- #
# Query validation
# --------------------------------------------------------------------------- #
_NUMERIC = re.compile(r"-?\d[\d,]*\.?\d*")


def _as_number(text: str) -> Optional[float]:
    m = _NUMERIC.search((text or "").replace(" ", ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def validate_query(raw: RawQuery, rfq: RFQ,
                   suppliers: Sequence[Supplier]) -> Union[AnalystQuery, Refusal]:
    """Turn a model proposal into an executable query, or refuse it.

    Everything the model named has to exist here and now. That is what stops a
    plausible-sounding but wrong answer: an unknown supplier is a refusal, not a silent
    drop, because silently dropping an exclusion the buyer asked for would change the
    answer without telling them.
    """
    intent = (raw.intent or "").strip()
    if intent not in INTENT_RULES:
        return Refusal("'%s' is not something I know how to work out" % intent[:60])
    if intent == Intent.UNSUPPORTED.value:
        return Refusal(raw.unsupported_reason
                       or "That is outside what this RFQ's data can answer")

    rule = INTENT_RULES[intent]
    notes: List[str] = []
    names = [s.name for s in suppliers]

    def resolve_supplier_or_refuse(text: str) -> Union[str, Refusal]:
        res = resolve_supplier(text, suppliers)
        if isinstance(res, Resolved):
            if res.how:
                notes.append(res.how)
            return res.id
        if isinstance(res, Ambiguous):
            return Refusal("'%s' could mean %s" % (text, " or ".join(res.candidates)),
                           res.candidates, intent)
        return Refusal("No supplier called '%s' responded to this RFQ" % text, names, intent)

    def resolve_line_or_refuse(text: str) -> Union[str, Refusal]:
        res = resolve_line(text, rfq.line_items)
        if isinstance(res, Resolved):
            if res.how:
                notes.append(res.how)
            return res.id
        if isinstance(res, Ambiguous):
            return Refusal("'%s' matches more than one line: %s"
                           % (text, ", ".join(res.candidates)), res.candidates, intent)
        return Refusal("This RFQ has no line matching '%s'" % text,
                       [li.id for li in rfq.line_items], intent)

    # -- subjects ----------------------------------------------------------
    supplier_ids: List[str] = []
    for text in raw.subject_suppliers:
        got = resolve_supplier_or_refuse(text)
        if isinstance(got, Refusal):
            return got
        if got not in supplier_ids:
            supplier_ids.append(got)

    if rule.subjects == "none" and supplier_ids:
        supplier_ids = []                       # harmless extra; the intent ignores it
    elif rule.subjects == "one" and len(supplier_ids) != 1:
        return Refusal("That question needs to name exactly one supplier", names, intent)
    elif rule.subjects == "one_or_two":
        if not supplier_ids or len(supplier_ids) > 2:
            return Refusal("A price comparison needs one or two suppliers, not %d"
                           % len(supplier_ids), names, intent)

    line_ids: List[str] = []
    for text in raw.subject_lines:
        got = resolve_line_or_refuse(text)
        if isinstance(got, Refusal):
            return got
        if got not in line_ids:
            line_ids.append(got)
    if rule.lines == "none":
        line_ids = []
    elif rule.lines == "one" and len(line_ids) != 1:
        return Refusal("That question needs to name exactly one line item",
                       [li.id for li in rfq.line_items], intent)

    # -- filters -----------------------------------------------------------
    question_keys = {q.field_key for q in rfq.questions if getattr(q, "field_key", "")}
    filters: List[Filter] = []
    for f in raw.filters:
        fld, op = (f.field or "").strip(), (f.op or "").strip()
        if fld not in FILTER_RULES:
            return Refusal("I don't hold a field called '%s'" % fld[:40], [], intent)
        if fld not in rule.filters:
            return Refusal("A %s filter does not apply to that question"
                           % fld.replace("_", " "), [], intent)
        frule = FILTER_RULES[fld]
        if op not in frule.ops:
            return Refusal("'%s' is not something I can do with %s"
                           % (op, fld.replace("_", " ")), sorted(frule.ops), intent)
        values = [v for v in (f.values or []) if str(v).strip()]
        if len(values) < frule.min_values or len(values) > frule.max_values:
            return Refusal("The %s filter needs between %d and %d values"
                           % (fld.replace("_", " "), frule.min_values, frule.max_values),
                           [], intent)

        if frule.value_kind == "number":
            nums = [_as_number(v) for v in values]
            if any(n is None for n in nums):
                return Refusal("'%s' is not a number I can compare against"
                               % ", ".join(values), [], intent)
            values = [("%g" % n) for n in nums if n is not None]
        elif frule.value_kind == "enum":
            bad = [v for v in values if v.strip().lower() not in frule.choices]
            if bad:
                return Refusal("'%s' is not one of %s"
                               % (bad[0], ", ".join(sorted(frule.choices))),
                               sorted(frule.choices), intent)
            values = [v.strip().lower() for v in values]
        elif fld == "supplier":
            resolved = []
            for v in values:
                got = resolve_supplier_or_refuse(v)
                if isinstance(got, Refusal):
                    return got
                resolved.append(got)
            values = resolved
        elif fld == "line":
            resolved = []
            for v in values:
                got = resolve_line_or_refuse(v)
                if isinstance(got, Refusal):
                    return got
                resolved.append(got)
            values = resolved
        elif fld == "currency":
            values = [v.strip().upper() for v in values]
            bad = [v for v in values if v not in KNOWN_CURRENCIES]
            if bad:
                return Refusal("'%s' is not a currency I recognise" % bad[0],
                               sorted(KNOWN_CURRENCIES), intent)
        elif fld == "certification":
            values = [canonical_cert_name(v) for v in values]
        elif fld == "questionnaire":
            unknown = [v for v in values if v not in question_keys]
            if unknown:
                return Refusal("This RFQ did not ask suppliers about '%s'" % unknown[0],
                               sorted(question_keys), intent)
        elif fld == "quote_state":
            allowed = {v for k, v in vars(CellState).items() if not k.startswith("_")
                       and isinstance(v, str)}
            values = [v.strip().lower().replace(" ", "_") for v in values]
            bad = [v for v in values if v not in allowed]
            if bad:
                return Refusal("'%s' is not a quote state" % bad[0], sorted(allowed), intent)
        filters.append(Filter(field=fld, op=op, values=values))

    # -- hypothetical ------------------------------------------------------
    hyp = raw.hypothetical
    if not rule.hypothetical:
        hyp = Hypothetical()
    else:
        excluded: List[str] = []
        for text in hyp.exclude_supplier_ids:
            got = resolve_supplier_or_refuse(text)
            if isinstance(got, Refusal):
                return got
            if got not in excluded:
                excluded.append(got)
        hyp.exclude_supplier_ids = excluded

    # -- lookup projection -------------------------------------------------
    fields: List[str] = []
    grain = (raw.grain or Grain.SUPPLIER.value).strip()
    sort_by, group_by = raw.sort_by, raw.group_by
    if rule.fields:
        if grain not in GRAINS:
            return Refusal("I can list this by supplier, by line or by quote, not by '%s'"
                           % grain[:30], list(GRAINS), intent)
        for name in raw.fields:
            key = (name or "").strip()
            if key.startswith(QUESTIONNAIRE_PREFIX):
                fkey = key[len(QUESTIONNAIRE_PREFIX):]
                if fkey not in question_keys:
                    return Refusal("This RFQ did not ask suppliers about '%s'" % fkey,
                                   sorted(question_keys), intent)
            elif key not in LOOKUP_FIELDS:
                return Refusal("I don't hold a field called '%s'" % key[:40],
                               LOOKUP_FIELDS, intent)
            elif grain not in FIELD_GRAINS.get(key, frozenset()):
                return Refusal("'%s' cannot be listed per %s" % (key, grain), [], intent)
            if key not in fields:
                fields.append(key)
        if not fields:
            fields = ["supplier"] if grain == Grain.SUPPLIER.value else ["line", "supplier", "price"]
        for label, value in (("sort_by", sort_by), ("group_by", group_by)):
            if value and value not in fields:
                return Refusal("I can only %s by a field I am showing; '%s' is not one"
                               % (label.split("_")[0], value[:40]), fields, intent)
    else:
        grain, sort_by, group_by = Grain.SUPPLIER.value, None, None

    # -- currency, limit, evidence target ----------------------------------
    ccy = (raw.comparison_currency or "").strip().upper() or None
    if ccy and not rule.currency:
        ccy = None
    if ccy and ccy not in KNOWN_CURRENCIES:
        return Refusal("'%s' is not a currency I can convert to" % ccy,
                       sorted(KNOWN_CURRENCIES), intent)

    top_n = raw.top_n if rule.top_n else None
    if top_n is not None and (not isinstance(top_n, int) or top_n < 1 or top_n > 200):
        return Refusal("I can show between 1 and 200 rows, not %s" % top_n, [], intent)

    target: Optional[EvidenceTarget] = None
    if rule.evidence_target:
        t = raw.evidence_target
        if not t.is_set():
            return Refusal("Tell me which value you want the source for "
                           "(a price, an MOQ, a certification, and for which supplier)",
                           [], intent)
        sid = None
        if t.supplier:
            got = resolve_supplier_or_refuse(t.supplier)
            if isinstance(got, Refusal):
                return got
            sid = got
        if not sid:
            return Refusal("Tell me which supplier's value you want the source for",
                           names, intent)
        lid = None
        if t.line:
            got = resolve_line_or_refuse(t.line)
            if isinstance(got, Refusal):
                return got
            lid = got
        if t.topic == EvidenceTopic.PRICE.value and not lid:
            return Refusal("A price belongs to one line — tell me which",
                           [li.id for li in rfq.line_items], intent)
        name = (t.name or "").strip() or None
        if t.topic in (EvidenceTopic.CERTIFICATION.value, EvidenceTopic.QUESTIONNAIRE.value,
                       EvidenceTopic.CONFLICT.value) and not name:
            return Refusal("Tell me which %s you mean" % t.topic.replace("_", " "), [], intent)
        if t.topic == EvidenceTopic.CERTIFICATION.value and name:
            name = canonical_cert_name(name)
        target = EvidenceTarget(supplier=sid, line=lid, topic=t.topic, name=name)

    return AnalystQuery(
        intent=intent, subject_supplier_ids=supplier_ids, subject_line_ids=line_ids,
        filters=filters, hypothetical=hyp, fields=fields, grain=grain, sort_by=sort_by,
        descending=bool(raw.descending), group_by=group_by, comparison_currency=ccy,
        top_n=top_n, evidence_target=target, reading=(raw.reading or "").strip()[:300],
        resolution_notes=notes)


# --------------------------------------------------------------------------- #
# The explanation guard
# --------------------------------------------------------------------------- #
#: Phase 3 analyses; it does not award. Phase 4 owns the decision, so narration that
#: reads like a recommendation is rejected even when its arithmetic is right.
_AWARD_LANGUAGE = re.compile(
    r"\b(award|recommend|you should (?:choose|pick|select|go with)|"
    r"best supplier|the winner|i suggest choosing)\b", re.I)

#: A "-" only means minus when it starts a number; inside "LINE-017" it is punctuation.
_NUMBER_IN_TEXT = re.compile(r"(?<![\w.])-?\d[\d,]*\.?\d*")

#: Years, counts of items in a sentence and the like are not claims about the data.
MAX_EXPLANATION_CHARS = 900


def guard_explanation(text: str, result: AnalystResult) -> Tuple[Optional[str], str]:
    """Accept the model's narration only if the result already says everything in it.

    Returns `(explanation, status)`. A rejected explanation is dropped entirely rather
    than repaired: the deterministic summary is already a complete answer, so there is
    nothing to gain from showing a sentence we could not stand behind.
    """
    body = (text or "").strip()
    if not body:
        return None, "rejected: empty"
    if len(body) > MAX_EXPLANATION_CHARS:
        return None, "rejected: too long"

    hit = _AWARD_LANGUAGE.search(body)
    if hit:
        return None, "rejected: award language (%s)" % hit.group(0)

    allowed = result.numbers_shown()
    for token in _NUMBER_IN_TEXT.findall(body):
        try:
            value = float(token.replace(",", ""))
        except ValueError:
            continue
        if not any(abs(value - a) <= max(0.0005, abs(a) * 1e-4) for a in allowed):
            return None, "rejected: figure %s is not in the result" % token

    in_scope = {norm_text(e.supplier_name) for e in result.exclusions}
    for row in result.rows:
        for value in row.values():
            if isinstance(value, str):
                in_scope.add(norm_text(value))
    for name in result.metrics.get("supplier_names", []) or []:
        in_scope.add(norm_text(name))
    known = {n for n in in_scope if n}
    if known:
        blob = norm_text(body)
        for name in result.metrics.get("out_of_scope_names", []) or []:
            if norm_text(name) and norm_text(name) in blob:
                return None, "rejected: mentions %s, which is not in this answer" % name
    return body, "ok"
