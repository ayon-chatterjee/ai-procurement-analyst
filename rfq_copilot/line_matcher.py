"""Decide which RFQ line a supplier's quotation line refers to.

Suppliers reorder rows, rename products, drop ids and abbreviate. The one thing we must
never do is assume the supplier's row 17 is the buyer's line 17, because that quietly
attributes a price to the wrong product.

Two signals are combined:

  deterministic   dimensions first, since for this product class "12 x 10 x 6" is a
                  near-identifier, then description overlap and quantity as support
  model           an AI decision, used to confirm or to break a tie, never to overrule
                  a clear dimension match

The output is a status, not just an id. MATCHED means we are willing to state it;
PROBABLE_MATCH means the buyer is asked to confirm; UNMATCHED means we would rather
show nothing than show something wrong.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .guards import content_tokens, norm_text
from .schema import RFQ, LineItem
from .supplier_models import MatchStatus

#: Below this, a deterministic match is not worth asserting on its own.
STRONG = 0.82
PROBABLE = 0.45

_DIM = re.compile(r"(\d+(?:\.\d+)?)\s*(?:x|×|\*)\s*(\d+(?:\.\d+)?)\s*(?:x|×|\*)\s*(\d+(?:\.\d+)?)", re.I)


def extract_dimensions(text: str) -> Optional[Tuple[float, float, float]]:
    """Pull an L x W x H triple out of free text, in the order written."""
    if not text:
        return None
    m = _DIM.search(text.replace(",", ""))
    if not m:
        return None
    try:
        return (float(m.group(1)), float(m.group(2)), float(m.group(3)))
    except ValueError:
        return None


def dimensions_match(a: Optional[Tuple[float, float, float]], b: Optional[Tuple[float, float, float]]) -> bool:
    """Same three numbers, regardless of the order the supplier wrote them in."""
    if not a or not b:
        return False
    return sorted(a) == sorted(b)


def line_dimension_text(line: LineItem) -> str:
    return " ".join([line.spec_summary() or "", line.description or "", line.product or ""])


@dataclass
class MatchCandidate:
    line_item_id: str
    score: float
    basis: str


@dataclass
class MatchResult:
    supplier_line_label: str
    line_item_id: Optional[str] = None
    status: MatchStatus = MatchStatus.UNMATCHED
    basis: str = "none"
    confidence: float = 0.0
    reason: str = ""
    alternatives: List[str] = field(default_factory=list)


def score_candidates(rfq: RFQ, supplier_line: Dict[str, Any]) -> List[MatchCandidate]:
    """Rank RFQ lines against one supplier line, using evidence we can point at."""
    label = " ".join(str(supplier_line.get(k) or "") for k in
                     ("supplier_line_label", "described_size", "product_description"))
    sup_dims = extract_dimensions(label)
    sup_tokens = content_tokens(label)
    sup_qty = supplier_line.get("quoted_quantity")

    out: List[MatchCandidate] = []
    for li in rfq.line_items:
        rfq_text = line_dimension_text(li)
        rfq_dims = extract_dimensions(rfq_text)

        if dimensions_match(sup_dims, rfq_dims):
            score, basis = 0.95, "dimensions"
        else:
            shared = sup_tokens & content_tokens(rfq_text)
            union = sup_tokens | content_tokens(rfq_text)
            score = (len(shared) / float(len(union))) if union else 0.0
            basis = "description"
            if sup_dims and rfq_dims and not dimensions_match(sup_dims, rfq_dims):
                score *= 0.25          # different explicit dimensions is strong counter-evidence
                basis = "description (dimensions differ)"

        # the supplier writing the RFQ's own line id is decisive
        if li.id and li.id.lower() in norm_text(label).replace(" ", "-"):
            score, basis = 1.0, "line_id"

        if sup_qty and li.quantity and abs(float(sup_qty) - float(li.quantity)) < 1e-9 and score > 0:
            score = min(1.0, score + 0.03)     # agreeing quantity is weak support only

        if score > 0:
            out.append(MatchCandidate(line_item_id=li.id, score=round(score, 4), basis=basis))
    out.sort(key=lambda c: -c.score)
    return out


def match_supplier_line(rfq: RFQ, supplier_line: Dict[str, Any],
                        ai_decision: Optional[Dict[str, Any]] = None) -> MatchResult:
    label = str(supplier_line.get("supplier_line_label") or supplier_line.get("described_size") or "?")
    candidates = score_candidates(rfq, supplier_line)
    result = MatchResult(supplier_line_label=label)

    best = candidates[0] if candidates else None
    runner_up = candidates[1] if len(candidates) > 1 else None
    valid_ids = {li.id for li in rfq.line_items}

    ai_id = None
    ai_conf = 0.0
    ai_basis = "none"
    if ai_decision:
        raw = ai_decision.get("rfq_line_item_id")
        ai_id = raw if raw in valid_ids else None     # a hallucinated id is simply dropped
        try:
            ai_conf = float(ai_decision.get("confidence") or 0.0)
        except (TypeError, ValueError):
            ai_conf = 0.0
        ai_basis = str(ai_decision.get("basis") or "none")

    # A clear dimension match is the most reliable signal we have.
    if best and best.score >= STRONG:
        result.line_item_id = best.line_item_id
        result.basis = best.basis
        result.confidence = best.score
        result.reason = "Dimensions match the RFQ line." if best.basis == "dimensions" else \
                        "The supplier quoted the RFQ line id." if best.basis == "line_id" else \
                        "Description matches closely."
        if ai_id and ai_id != best.line_item_id:
            # The model disagrees with a strong deterministic match: surface it, do not silently pick.
            result.status = MatchStatus.PROBABLE_MATCH
            result.alternatives = [ai_id]
            result.reason += " The model proposed a different line, so this needs confirming."
        else:
            result.status = MatchStatus.MATCHED
        if runner_up and runner_up.score >= best.score - 0.02:
            result.status = MatchStatus.PROBABLE_MATCH
            result.alternatives = [runner_up.line_item_id]
            result.reason = "Two RFQ lines fit this description equally well."
        return result

    # Otherwise lean on the model, but never above a cautious ceiling.
    if ai_id:
        result.line_item_id = ai_id
        result.basis = ai_basis
        result.confidence = min(ai_conf, 0.8)
        result.status = MatchStatus.MATCHED if ai_conf >= 0.85 and ai_basis in ("line_id", "sku", "dimensions") \
            else MatchStatus.PROBABLE_MATCH
        result.reason = str(ai_decision.get("reason") or "Identified from the supplier's wording.")
        alts = [x for x in (ai_decision.get("alternative_line_item_ids") or []) if x in valid_ids]
        result.alternatives = alts
        return result

    if best and best.score >= PROBABLE:
        result.line_item_id = best.line_item_id
        result.basis = best.basis
        result.confidence = best.score
        result.status = MatchStatus.PROBABLE_MATCH
        result.reason = "Partial description match; please confirm."
        if runner_up:
            result.alternatives = [runner_up.line_item_id]
        return result

    result.status = MatchStatus.UNMATCHED
    result.reason = "Nothing in the supplier's wording identifies an RFQ line safely."
    result.alternatives = [c.line_item_id for c in candidates[:2]]
    return result


def match_all(rfq: RFQ, supplier_lines: Sequence[Dict[str, Any]],
              ai_decisions: Optional[Sequence[Dict[str, Any]]] = None) -> List[MatchResult]:
    """Match every supplier line, then resolve any two lines claiming the same RFQ line."""
    by_label: Dict[str, Dict[str, Any]] = {}
    for d in (ai_decisions or []):
        key = norm_text(str(d.get("supplier_line_label") or ""))
        if key:
            by_label[key] = d

    results = []
    for sl in supplier_lines:
        key = norm_text(str(sl.get("supplier_line_label") or ""))
        results.append(match_supplier_line(rfq, sl, by_label.get(key)))

    # two supplier lines cannot both own one RFQ line
    seen: Dict[str, MatchResult] = {}
    for r in results:
        if not r.line_item_id:
            continue
        prior = seen.get(r.line_item_id)
        if prior is None:
            seen[r.line_item_id] = r
            continue
        loser = r if r.confidence <= prior.confidence else prior
        winner = prior if loser is r else r
        seen[r.line_item_id] = winner
        loser.status = MatchStatus.CONFLICT
        loser.reason = ("Both %r and %r claim this RFQ line; the buyer needs to say which is which."
                        % (loser.supplier_line_label, winner.supplier_line_label))
        if winner.line_item_id and winner.line_item_id not in loser.alternatives:
            loser.alternatives.append(winner.line_item_id)
        loser.line_item_id = None
    return results
