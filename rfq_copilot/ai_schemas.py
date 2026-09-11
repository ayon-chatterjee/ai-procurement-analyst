"""JSON Schemas for structured AI output.

Kept deliberately simple (type / properties / required / enum / items /
additionalProperties, nullable as type unions) so the same schema works with
``claude -p --json-schema`` and can be validated locally with ``jsonschema``.

There is ONE contract for every RFQ turn: ``TURN_OUTPUT_SCHEMA``.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable

STR: Dict[str, Any] = {"type": "string"}
NULL_STR: Dict[str, Any] = {"type": ["string", "null"]}
NUM: Dict[str, Any] = {"type": "number"}
NULL_NUM: Dict[str, Any] = {"type": ["number", "null"]}
INT: Dict[str, Any] = {"type": "integer"}
BOOL: Dict[str, Any] = {"type": "boolean"}


def enum(values: Iterable[str]) -> Dict[str, Any]:
    return {"type": "string", "enum": list(values)}


def arr(items: Dict[str, Any]) -> Dict[str, Any]:
    return {"type": "array", "items": items}


def obj(**props: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": list(props.keys()),
        "additionalProperties": False,
    }


SECTIONS = ["commercial", "sourcing", "logistics", "technical", "quality", "instructions"]
IMPORTANCE_ALL = ["required", "recommended", "optional", "not_applicable"]
IMPORTANCE_ASKABLE = ["required", "recommended", "optional"]
VALUE_KINDS = ["text", "number", "date", "list", "boolean"]
ANSWER_TYPES = ["text", "number", "date", "choice", "yes_no"]

FIELD_UPDATE = obj(
    key=STR,
    section=enum(SECTIONS),
    label=STR,
    value=STR,                       # always a string; service parses by value_kind
    value_kind=enum(VALUE_KINDS),
    unit=NULL_STR,
    source=enum(["buyer_explicit", "ai_recommended"]),
    status=enum(["provided", "recommended", "unknown", "conflict"]),
    revision=enum(["none", "correction"]),
    evidence=NULL_STR,               # verbatim span from the buyer's text (required for buyer_explicit)
    importance=enum(IMPORTANCE_ASKABLE),
    note=NULL_STR,
)

APPLICABILITY_UPDATE = obj(key=STR, importance=enum(IMPORTANCE_ALL), reason=STR)

LINE_ITEM_OUT = obj(
    line_id=NULL_STR,                # existing LINE-00x to update, or null for a new line
    product=STR,
    description=STR,
    specifications=arr(obj(name=STR, value=STR, unit=NULL_STR)),
    quantity=NULL_NUM,
    unit=STR,
    target_price=NULL_NUM,
    required_date=NULL_STR,
    evidence=NULL_STR,
)

ANSWERED_QUESTION = obj(
    question_id=STR,
    resolution=enum(["answered", "unknown", "not_applicable"]),
    answer_summary=STR,
    evidence=NULL_STR,
)

NEW_QUESTION = obj(
    section=enum(SECTIONS),
    field_key=NULL_STR,
    question=STR,
    reason=STR,
    importance=enum(IMPORTANCE_ASKABLE),
    answer_type=enum(ANSWER_TYPES),
    suggested_options=arr(STR),
)

COMPLETENESS_OUT = obj(
    score=INT,
    ready_to_send=BOOL,
    missing_required_fields=arr(STR),
    recommended_fields=arr(STR),
    open_ambiguities=arr(STR),
    explanation=STR,
)

TURN_OUTPUT_SCHEMA: Dict[str, Any] = obj(
    classification=obj(product=STR, category=STR, product_type=STR, confidence=NUM, note=STR),
    title=STR,
    assistant_message=STR,
    field_updates=arr(FIELD_UPDATE),
    applicability_updates=arr(APPLICABILITY_UPDATE),
    line_items=obj(mode=enum(["none", "replace", "append"]), items=arr(LINE_ITEM_OUT)),
    answered_questions=arr(ANSWERED_QUESTION),
    new_questions=arr(NEW_QUESTION),
    completeness=COMPLETENESS_OUT,
)

# Tiny schema used by the connection health check ("Test connection").
PING_SCHEMA: Dict[str, Any] = obj(ok=BOOL, model_note=STR)
