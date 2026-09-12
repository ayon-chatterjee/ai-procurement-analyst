"""Structured contract for the Quotation Extraction Playground extractor.

The playground answers a different question from Phase 1's copilot. The copilot
*interviews* a buyer; the playground is handed a pile of material — an email, a
spreadsheet, a drawing — and has to work out what is being asked for.

So the model reports requirements it can point at, and says which ones are ambiguous.
It never reports something as missing: absence is computed afterwards by Phase 1's
existing completeness rules, so the playground cannot disagree with the rest of the app
about what "missing" means.
"""
from __future__ import annotations

from typing import Any, Dict

from .ai_schemas import BOOL, NULL_NUM, NULL_STR, NUM, STR, arr, enum, obj

#: Where a requirement was found. The model names the document; exact positions are only
#: claimed when the supplied content actually carried them.
SOURCE_KINDS = ["email_text", "attachment"]

#: A requirement is either something we can point at, or something stated so vaguely that
#: a supplier could read it two ways. "Missing" is not in this list on purpose.
REQUIREMENT_STATUS = ["found", "ambiguous"]

REQUIREMENT = obj(
    field_key=STR,            # a universal field key where one fits, else a snake_case name
    label=STR,                # human label, e.g. "Material grade"
    value=STR,                # exactly as stated; never normalised or converted
    unit=NULL_STR,
    status=enum(REQUIREMENT_STATUS),
    source_kind=enum(SOURCE_KINDS),
    source_document=NULL_STR,   # the filename, when it came from an attachment
    source_location=NULL_STR,   # only when the supplied content showed one
    quoted_text=STR,            # verbatim span supporting the value
    note=NULL_STR,              # why it is ambiguous, if it is
    confidence=NUM,
)

SPEC_ATTR = obj(name=STR, value=STR, unit=NULL_STR)

LINE_ITEM = obj(
    name=STR,                 # short product name, e.g. "SS304 bracket"
    description=STR,
    quantity=NULL_NUM,
    unit=NULL_STR,
    specifications=arr(SPEC_ATTR),
    requirements=arr(REQUIREMENT),
    ambiguities=arr(STR),
    evidence=STR,             # verbatim span that establishes this line item exists
)

PLAYGROUND_EXTRACTION_SCHEMA: Dict[str, Any] = obj(
    classification=obj(product=STR, category=STR, product_type=STR, confidence=NUM),
    title=STR,
    summary=STR,                        # one plain sentence for the buyer
    line_items=arr(LINE_ITEM),
    shared_requirements=arr(REQUIREMENT),   # apply to the whole request, not one line
    ambiguities=arr(STR),
    nothing_found_reason=NULL_STR,      # set when no procurement requirement is present at all
)


#: Supplier replies for an arbitrary product have to come from somewhere for a demo.
#: This generates fabricated *supplier documents*, which are then read by the real
#: Phase 2 extraction pipeline, exactly like the committed carton fixtures are.
SIMULATED_SUPPLIER = obj(
    supplier_name=STR,
    country=STR,
    style=enum(["table", "email", "prose"]),
    document_text=STR,          # the quotation as the supplier would have written it
)

SIMULATED_RESPONSES_SCHEMA: Dict[str, Any] = obj(
    suppliers=arr(SIMULATED_SUPPLIER),
    note=STR,
)
