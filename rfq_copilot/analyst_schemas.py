"""Structured contracts for the procurement analyst.

Two calls, two schemas. The first turns a buyer's question into an analytical *request*;
the second narrates a result the application has already calculated. Neither schema has
a field the model could put a price, a percentage or a supplier fact into: the only free
numeric value in the whole contract is `top_n`, and everything else is either an
enumerated term or a name that must resolve against the current dataset before it is used.

Same strict-JSON rules as the rest of the app: every property required,
`additionalProperties: false`, nullability as a type union, no `$ref`/`oneOf`/`format`.
"""
from __future__ import annotations

from typing import Any, Dict

from .ai_schemas import BOOL, STR, arr, enum, obj
from .analyst_models import (
    ELIGIBILITY_VALUES, EVIDENCE_TOPICS, FILTER_FIELDS, FILTER_OPS, GRAINS, INTENTS,
)

#: Integers may be absent. `obj()` requires every property, so "unset" is a null union.
NULL_INT: Dict[str, Any] = {"type": ["integer", "null"]}
NULL_STR_: Dict[str, Any] = {"type": ["string", "null"]}

FILTER_OUT = obj(
    field=enum(FILTER_FIELDS),
    op=enum(FILTER_OPS),
    values=arr(STR),            # always strings; the validator types them
)

#: Each flag suspends one Phase 2 trust decision. None of them can introduce a value.
HYPOTHETICAL_OUT = obj(
    exclude_suppliers=arr(STR),
    treat_claimed_as_verified=BOOL,
    ignore_moq_constraints=BOOL,
    include_probable_matches=BOOL,
)

#: Always present, with topic "none" when the question is not an evidence lookup — the
#: same convention the supplier contract uses for an evidence object that may be empty.
EVIDENCE_TARGET_OUT = obj(
    supplier=NULL_STR_,
    line=NULL_STR_,
    topic=enum(EVIDENCE_TOPICS),
    name=NULL_STR_,
)

ANALYST_QUERY_SCHEMA: Dict[str, Any] = obj(
    intent=enum(INTENTS),
    subject_suppliers=arr(STR),        # as the buyer named them; resolved deterministically
    subject_lines=arr(STR),
    filters=arr(FILTER_OUT),
    hypothetical=HYPOTHETICAL_OUT,
    fields=arr(STR),                   # lookup only; checked against the field whitelist
    grain=enum(GRAINS),
    sort_by=NULL_STR_,
    descending=BOOL,
    group_by=NULL_STR_,
    comparison_currency=NULL_STR_,
    top_n=NULL_INT,
    evidence_target=EVIDENCE_TARGET_OUT,
    refines_previous=BOOL,             # true when this narrows the previous question
    reading=STR,                       # one sentence back to the buyer, for the audit trail
    unsupported_reason=NULL_STR_,      # why, when intent is "unsupported"
)


#: The explanation call sees only the calculated result. `caveats` exists so the model
#: has somewhere to put a qualification instead of burying it in the prose.
EXPLANATION_SCHEMA: Dict[str, Any] = obj(
    explanation=STR,
    caveats=arr(STR),
)

#: Kept here so prompts and validation cannot drift apart.
ELIGIBILITY_CHOICES = list(ELIGIBILITY_VALUES)
