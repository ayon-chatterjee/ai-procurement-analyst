"""Universal RFQ field registry.

These are the procurement dimensions every RFQ *considers*. Presence in this
registry does not make a field required: the AI decides contextual importance
per product (``applicability_updates``) and may add product-specific technical
fields (``flute``, ``board_grade``, ``load_rating_kg`` ...) that are not listed
here. Deterministic code only enforces the resulting states.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .schema import AnswerType, FieldStatus, FieldValue, Importance, Section, Source, ValueKind


@dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    section: Section
    default_importance: Importance
    value_kind: ValueKind
    unit_hint: Optional[str]
    help: str
    # Standard question used ONLY as a safety net when a field is required but the AI never asked about it.
    question: str = ""
    reason: str = ""
    answer_type: AnswerType = AnswerType.TEXT
    options: Tuple[str, ...] = ()


UNIVERSAL_FIELDS: List[FieldSpec] = [
    # -- commercial ----------------------------------------------------------
    FieldSpec("quantity", "Order quantity", Section.COMMERCIAL, Importance.REQUIRED, ValueKind.NUMBER, "pcs",
              "Total quantity, or per line item when several variants are listed.",
              "How many units do you need (per variant, if there are several)?", "Quantity sets price tiers and whether the order meets supplier minimums.", AnswerType.NUMBER),
    FieldSpec("target_landed_cost", "Target landed cost", Section.COMMERCIAL, Importance.RECOMMENDED, ValueKind.NUMBER, "per unit",
              "Buyer's target delivered price per unit, if any.",
              "Do you have a target landed cost per unit?", "A target lets suppliers propose specs that fit the budget instead of over-quoting.", AnswerType.NUMBER),
    FieldSpec("currency", "Currency", Section.COMMERCIAL, Importance.RECOMMENDED, ValueKind.TEXT, None,
              "Currency suppliers should quote in.",
              "Which currency should suppliers quote in?", "A common currency keeps quotes comparable.", AnswerType.CHOICE, ("USD", "EUR", "INR", "CNY")),
    FieldSpec("price_basis", "Price basis (Incoterm)", Section.COMMERCIAL, Importance.RECOMMENDED, ValueKind.TEXT, None,
              "EXW / FOB / CIF / DDP etc.",
              "On what price basis should suppliers quote?", "EXW, FOB and DDP prices are not comparable without the basis.", AnswerType.CHOICE, ("EXW", "FOB", "CIF", "DDP")),
    FieldSpec("payment_terms", "Payment terms", Section.COMMERCIAL, Importance.OPTIONAL, ValueKind.TEXT, None,
              "Deposit / balance structure the buyer expects.",
              "What payment terms do you expect?", "Deposit size and timing affect supplier pricing and willingness to quote."),
    FieldSpec("quote_validity", "Quote validity", Section.COMMERCIAL, Importance.OPTIONAL, ValueKind.TEXT, None,
              "How long quotes must remain valid.",
              "How long should quotes stay valid?", "Without a validity period a quote can be withdrawn before award."),
    FieldSpec("sample_requirements", "Samples", Section.COMMERCIAL, Importance.OPTIONAL, ValueKind.TEXT, None,
              "Whether pre-production samples are needed and on what terms.",
              "Do you need pre-production samples?", "Sampling adds cost and lead time before production can start.", AnswerType.YES_NO),
    # -- sourcing -----------------------------------------------------------
    FieldSpec("sourcing_country", "Sourcing country", Section.SOURCING, Importance.RECOMMENDED, ValueKind.TEXT, None,
              "Country or region suppliers should be located in.",
              "Where should suppliers be located?", "Sourcing region drives freight cost, lead time and compliance."),
    FieldSpec("customization_type", "Custom / private label / off-the-shelf", Section.SOURCING, Importance.REQUIRED, ValueKind.TEXT, None,
              "Whether the item is made to spec, branded, or a stock item.",
              "Is this made to your specification, private-labelled with your branding, or an off-the-shelf item?",
              "Custom and branded items carry tooling, artwork and minimum-order costs that stock items do not.", AnswerType.CHOICE,
              ("Custom to my spec", "Private label", "Off-the-shelf")),
    FieldSpec("supplier_type", "Supplier type", Section.SOURCING, Importance.OPTIONAL, ValueKind.TEXT, None,
              "Factory vs trading company preference.",
              "Do you prefer factories or are trading companies acceptable?", "Trading companies add margin but simplify consolidation.", AnswerType.CHOICE,
              ("Factories only", "Trading companies acceptable")),
    # -- logistics ----------------------------------------------------------
    FieldSpec("destination", "Destination", Section.LOGISTICS, Importance.REQUIRED, ValueKind.TEXT, None,
              "Delivery location (city / port / warehouse).",
              "Where should the goods be delivered (city, port or warehouse)?", "Destination determines freight cost, duties and lead time."),
    FieldSpec("shipping_method", "Shipping method", Section.LOGISTICS, Importance.RECOMMENDED, ValueKind.TEXT, None,
              "Sea / air / road / rail.",
              "How should the goods ship?", "Sea and air freight differ several-fold in cost and transit time.", AnswerType.CHOICE, ("Sea", "Air", "Road", "Supplier to advise")),
    FieldSpec("required_delivery_date", "Required delivery date", Section.LOGISTICS, Importance.RECOMMENDED, ValueKind.DATE, None,
              "Date goods must arrive, or acceptable lead time.",
              "When do you need delivery?", "The deadline decides whether expedited production or air freight is needed.", AnswerType.DATE),
    FieldSpec("packaging_requirements", "Packaging / packing", Section.LOGISTICS, Importance.RECOMMENDED, ValueKind.TEXT, None,
              "How finished goods must be packed for shipment.",
              "Any requirements for how the goods are packed for shipment?", "Packing method affects damage risk and freight volume."),
    # -- technical ----------------------------------------------------------
    FieldSpec("technical_summary", "Technical requirements", Section.TECHNICAL, Importance.REQUIRED, ValueKind.TEXT, None,
              "Core specification summary; product-specific fields sit alongside.",
              "Do you have a specification or drawing suppliers should quote against?", "Suppliers cannot price an item without its core specification."),
    # -- quality ------------------------------------------------------------
    FieldSpec("certifications", "Certifications", Section.QUALITY, Importance.RECOMMENDED, ValueKind.LIST, None,
              "Certificates suppliers must hold (FSC, ISO 9001, CE, ...).",
              "Which certifications must suppliers hold, if any?", "Certification requirements exclude suppliers and change cost."),
    FieldSpec("quality_standards", "Quality / inspection", Section.QUALITY, Importance.OPTIONAL, ValueKind.TEXT, None,
              "Inspection, testing or tolerance standards.",
              "Any inspection or testing standards suppliers must meet?", "Inspection regimes add cost and gate acceptance."),
    # -- instructions -------------------------------------------------------
    FieldSpec("additional_supplier_instructions", "Additional supplier instructions", Section.INSTRUCTIONS, Importance.OPTIONAL, ValueKind.TEXT, None,
              "Anything else suppliers must know or answer.",
              "Anything else suppliers must know or answer in their quote?", "Extra instructions surface deal-breakers before quotes arrive."),
]

FIELD_SPECS: Dict[str, FieldSpec] = {f.key: f for f in UNIVERSAL_FIELDS}

# Vocabulary hint passed to the model (not enforced): canonical certificate names.
CERT_CANON = [
    "CE", "RoHS", "FCC", "FDA", "LFGB", "FSC", "BIS", "ISO 9001", "ISO 14001", "BSCI", "Sedex",
    "REACH", "EN 71", "UL", "GOTS", "OEKO-TEX", "GRS", "ISTA",
]


def new_field_set() -> Dict[str, FieldValue]:
    """All universal fields, all MISSING, with default importance."""
    out: Dict[str, FieldValue] = {}
    for spec in UNIVERSAL_FIELDS:
        out[spec.key] = FieldValue(
            key=spec.key,
            label=spec.label,
            section=spec.section,
            value_kind=spec.value_kind,
            importance=spec.default_importance,
            status=FieldStatus.MISSING,
            source=Source.MISSING,
        )
    return out


def registry_for_prompt() -> str:
    """Compact one-line-per-field description for the system prompt."""
    lines = []
    for spec in UNIVERSAL_FIELDS:
        lines.append("- %s (%s, default %s): %s" % (spec.key, spec.section.value, spec.default_importance.value, spec.help))
    return "\n".join(lines)
