"""The strict structured-output contract for supplier response extraction.

One schema, one AI call per supplier response. The model reports **observations**:
what the supplier wrote, where, and how sure it is. It never assigns trust levels.
Whether a certification counts as verified, which RFQ line a quote belongs to, and
whether a price can be normalised are all decided afterwards by deterministic code,
because those are judgements the application must be able to defend.

Kept to type / properties / required / enum / items / additionalProperties with
nullable type unions, matching the Phase 1 contract that the Claude CLI accepts.
"""
from __future__ import annotations

from typing import Any, Dict

from .ai_schemas import BOOL, INT, NULL_NUM, NULL_STR, NUM, STR, arr, enum, obj

PRICE_BASES = ["per_unit", "per_100", "per_1000", "per_set", "per_kg", "per_lot", "unknown"]
RESPONSE_TYPES = ["quote_received", "partial_quote", "question", "declined", "revision_received"]

#: Where in the document a value was found. The model quotes the supplier verbatim so the
#: application can verify the span really exists before trusting it as evidence.
EVIDENCE = obj(
    quoted_text=STR,      # verbatim span from the document, <= 200 chars
    location=STR,         # the location label as printed in the supplied content, e.g. "Page 2"
)

NULL_EVIDENCE = obj(
    quoted_text=NULL_STR,
    location=NULL_STR,
)

QUOTE_LINE = obj(
    supplier_line_label=STR,        # exactly how the supplier referred to this line
    supplier_line_number=NULL_STR,  # the supplier's own numbering, if any
    described_size=NULL_STR,        # dimensions as written, e.g. "12 x 10 x 6"
    product_description=NULL_STR,
    quoted_quantity=NULL_NUM,
    quoted_unit=NULL_STR,           # the supplier's unit word: pcs, pieces, kg, set
    unit_price=NULL_NUM,            # the number exactly as quoted, not converted
    currency=NULL_STR,              # ISO code or symbol as written
    price_basis=enum(PRICE_BASES),  # what one quoted price covers
    price_is_indicative=BOOL,       # "approximately", "around", "subject to confirmation"
    minimum_order_quantity=NULL_NUM,   # only if stated for THIS line
    lead_time_text=NULL_STR,           # only if stated for THIS line
    evidence=EVIDENCE,
    confidence=NUM,
)

DISCOUNT = obj(
    percent=NULL_NUM,
    amount=NULL_NUM,
    condition=NULL_STR,             # the qualifying condition, verbatim in meaning
    evidence=NULL_EVIDENCE,
)

COMMERCIAL_TERMS = obj(
    currency=NULL_STR,
    minimum_order_quantity=NULL_NUM,
    moq_unit=NULL_STR,
    moq_evidence=NULL_EVIDENCE,
    lead_time_text=NULL_STR,        # verbatim, including any condition
    lead_time_evidence=NULL_EVIDENCE,
    payment_terms=NULL_STR,
    payment_evidence=NULL_EVIDENCE,
    delivery_terms=NULL_STR,        # Incoterm and place, e.g. "FOB Shenzhen"
    delivery_evidence=NULL_EVIDENCE,
    quote_validity_text=NULL_STR,
    quote_validity_is_conditional=BOOL,   # true for "subject to material prices"
    quote_validity_evidence=NULL_EVIDENCE,
    discount=DISCOUNT,
)

CERTIFICATION = obj(
    name=STR,                       # canonical where obvious, e.g. "ISO 9001"
    raw_name=STR,                   # exactly as the supplier wrote it
    certificate_number=NULL_STR,
    issuing_body=NULL_STR,
    expiry_date=NULL_STR,
    document_attached=BOOL,         # did the supplier say a certificate is attached/provided
    document_reference=NULL_STR,
    evidence=EVIDENCE,
    confidence=NUM,
)

QUESTIONNAIRE_ANSWER = obj(
    field_key=STR,                  # the RFQ field this answers
    question_id=NULL_STR,           # the RFQ question id when it was supplied to you
    answer=STR,                     # the supplier's answer in their own terms
    evidence=EVIDENCE,
    confidence=NUM,
)

SUPPLIER_QUESTION = obj(
    question=STR,                   # what the supplier is asking the buyer
    related_field_key=NULL_STR,
    evidence=EVIDENCE,
)

CONFLICT = obj(
    topic=STR,                      # short label, e.g. "lead time"
    description=STR,                # why the two statements disagree
    values=arr(obj(value=STR, evidence=EVIDENCE)),
)

SUPPLIER_EXTRACTION_SCHEMA: Dict[str, Any] = obj(
    supplier=obj(
        stated_name=NULL_STR,
        quote_reference=NULL_STR,
        quote_date=NULL_STR,
        country_or_place=NULL_STR,
    ),
    response=obj(
        response_type=enum(RESPONSE_TYPES),
        is_revision=BOOL,
        supersedes_reference=NULL_STR,   # the earlier quote reference it replaces
        summary=STR,                     # one plain sentence for the buyer
    ),
    quote_lines=arr(QUOTE_LINE),
    lines_explicitly_not_quoted=arr(obj(
        supplier_line_label=STR,
        reason=NULL_STR,
        evidence=NULL_EVIDENCE,
    )),
    commercial_terms=COMMERCIAL_TERMS,
    questionnaire_answers=arr(QUESTIONNAIRE_ANSWER),
    certifications=arr(CERTIFICATION),
    supplier_questions=arr(SUPPLIER_QUESTION),
    conflicts=arr(CONFLICT),
    uncertainties=arr(STR),          # anything you could not resolve from the document
)


#: Line matching is a second, separate call: it reasons only about which RFQ line each
#: supplier line refers to, and is allowed to say it does not know.
MATCH_DECISION = obj(
    supplier_line_label=STR,
    rfq_line_item_id=NULL_STR,       # null when you cannot identify one safely
    basis=enum(["line_id", "sku", "dimensions", "description", "quantity", "position", "none"]),
    confidence=NUM,
    reason=STR,                      # short, concrete
    alternative_line_item_ids=arr(STR),
)

LINE_MATCH_SCHEMA: Dict[str, Any] = obj(
    matches=arr(MATCH_DECISION),
    notes=arr(STR),
)
