"""Turning a decision into something that has happened.

The buyer picks a supplier per line, the application checks the award against the quotes
it actually holds, and — once nothing is blocking — drafts a letter per supplier, records
that it was sent, and produces an order handoff. Every step writes an event carrying the
state it replaced, so the chain replays to the award as it stands.

One model call exists in this entire phase, and it writes prose a human reads before
sending. Seeding, picking, validating, totalling and the handoff are arithmetic. That is
the structural answer to "could the AI award the wrong supplier": it is never asked.

Two rules enforced here rather than in the UI:

* **The buyer's decision is the source of truth.** `reseed` steps over any line the buyer
  overrode, and a line still on its seed that moves records what it was.
* **Approval freezes the decision.** `_transition` is the only writer of status and
  consults the table in `award_models`; the line and threshold setters refuse outright
  once the award is past DRAFT/REVIEWED.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .ai_service import AIError, AIInvalidOutput, AIResult, AIService
from .analyst_calculations import (
    build_context, choose_comparison_currency, evidence_refs, first_priced, price_check,
    terms_text,
)
from .analyst_models import AnalystQuery, EvidenceTopic, Hypothetical, Intent
from .award_calculations import (
    award_totals, line_from_candidate, propose_award,
)
from .award_guards import guard_communication, leaked_names, neutralise, stated
from .award_models import (
    Award, AwardLine, AwardProposal, AwardStatus, AwardThresholds, AwardTotals,
    CommunicationFacts, CommunicationLine, CommunicationStatus, ExecutionEvent,
    ExecutionEventType, HandoffLine, NOT_PROVIDED, OrderHandoff, PickSource,
    SupplierCommunication, TRANSITIONS, ValidationReport, is_editable,
)
from .award_prompts import (
    COMMUNICATION_PROMPT_VERSION, COMMUNICATION_SCHEMA, COMMUNICATION_SYSTEM_PROMPT,
    assemble_communication, build_communication_prompt, communication_subject,
    render_fallback_communication,
)
from .award_validation import validate_award
from .config import Settings
from .persistence import AwardRepository, RFQRepository, SupplierRepository
from .rfq_service import RFQStateError
from .schema import AICallRecord, new_id, utc_now
from .supplier_service import SupplierService

STAGES = [
    "Reading the award…",
    "Drafting the supplier messages…",
    "Checking each draft against the award…",
]

#: What the buyer is told to expect next. Fixed wording chosen by the application, not the
#: model, because it is a statement about our process rather than about the goods.
NEXT_STEP = ("Please confirm acceptance of this award by reply. We will follow up with a "
             "purchase order once you have confirmed.")

BUYER_ORGANISATION = "Procurement"


class AwardError(Exception):
    """Something the user can act on, phrased for them."""


class AwardService:
    def __init__(self, ai: AIService, repo: RFQRepository,
                 settings: Optional[Settings] = None,
                 supplier_service: Optional[SupplierService] = None):
        self.ai = ai
        self.repo = repo
        self.settings = settings or Settings.from_env()
        self.suppliers = supplier_service or SupplierService(repo, ai, self.settings)
        self.store = AwardRepository(repo)
        self.audit = SupplierRepository(repo)

    # ------------------------------------------------------------ reading
    def current(self, rfq_id: str) -> Optional[Award]:
        """The award in play, if there is one. A closed award is not one."""
        return self.store.latest_award(rfq_id)

    def last_closed(self, rfq_id: str) -> Optional[Award]:
        """The most recent completed or cancelled award, so finishing one does not make it
        disappear from the screen that made it."""
        return self.store.last_closed_award(rfq_id)

    def get(self, award_id: str) -> Award:
        award = self.store.get_award(award_id)
        if award is None:
            raise AwardError("That award no longer exists.")
        return award

    def eligible_suppliers(self, award_id: str,
                           line_item_id: str) -> List[Tuple[str, str]]:
        """Who a buyer may override this line to, as `(id, name)`.

        Eligibility is `PriceCheck.valid` — a comparable price that no exclusion applies
        to — which is the same rule that decides whether a cell can be a candidate at all.
        The override screen and any script asking the question read it from here, so the
        two cannot answer differently.
        """
        award = self.get(award_id)
        ctx = self.context(award.rfq_id, award.currency)
        return [(s.id, s.name) for s in ctx.suppliers
                if price_check(ctx.cell(line_item_id, s.id), ctx).valid]

    def statuses(self) -> Dict[str, str]:
        """Live award status per RFQ, for the saved-RFQ list."""
        return self.store.award_statuses()

    def events(self, award_id: str) -> List[ExecutionEvent]:
        return self.store.list_events(award_id)

    def communications(self, award_id: str) -> List[SupplierCommunication]:
        return self.store.list_communications(award_id)

    def handoffs(self, award_id: str) -> List[OrderHandoff]:
        return self.store.list_handoffs(award_id)

    def context(self, rfq_id: str, currency: Optional[str] = None,
                today: Optional[_dt.date] = None):
        """The Phase 2 comparison, shaped for the award rules. One place reads the DB."""
        try:
            matrix = self.suppliers.build_comparison(rfq_id, display_currency=currency)
        except RFQStateError as e:
            raise AwardError(str(e))
        if not matrix.bundles:
            raise AwardError(
                "No supplier responses have been extracted for this RFQ, so there is "
                "nothing to award. Load and extract the responses first.")
        query = AnalystQuery(intent=Intent.CHEAPEST_BY_LINE.value, hypothetical=Hypothetical(),
                             comparison_currency=currency)
        chosen, reason = choose_comparison_currency(matrix, query, currency)
        if chosen and len(matrix.currencies()) > 1 and matrix.display_currency != chosen:
            matrix = self.suppliers.build_comparison(rfq_id, display_currency=chosen)
        return build_context(matrix, query, chosen, reason, today=today)

    def proposal(self, rfq_id: str, thresholds: AwardThresholds,
                 currency: Optional[str] = None,
                 today: Optional[_dt.date] = None) -> AwardProposal:
        """What the application would suggest. Never stored: it is a pure function of the
        comparison, and a cached copy would go stale the moment a quote was corrected."""
        ctx = self.context(rfq_id, currency, today)
        return propose_award(ctx.matrix, thresholds, currency=ctx.currency, today=today)

    def totals(self, award: Award) -> AwardTotals:
        return award_totals(award)

    def validate(self, award_id: str, today: Optional[_dt.date] = None) -> ValidationReport:
        award = self.get(award_id)
        ctx = self.context(award.rfq_id, award.currency, today)
        return validate_award(award, ctx, None, today=today)

    # ------------------------------------------------------------ deciding
    def start(self, rfq_id: str, thresholds: Optional[AwardThresholds] = None,
              today: Optional[_dt.date] = None) -> Award:
        """Open an award and seed every line from the comparison."""
        existing = self.current(rfq_id)
        if existing is not None:
            raise AwardError("This RFQ already has an award in progress. Open it, or cancel "
                             "it before starting another.")
        thresholds = thresholds or AwardThresholds()
        ctx = self.context(rfq_id, None, today)
        proposal = propose_award(ctx.matrix, thresholds, currency=ctx.currency, today=today)

        award = Award(rfq_id=rfq_id, rfq_title=ctx.rfq.title or ctx.rfq.product,
                      currency=proposal.currency, currency_reason=proposal.currency_reason,
                      thresholds=thresholds, assumptions=list(proposal.assumptions),
                      rate_provenance=dict(proposal.rate_provenance))
        award.lines = [self._seed_line(line) for line in proposal.lines]
        self.store.save_award(award)
        self._event(award, ExecutionEventType.AWARD_CREATED,
                    "Award opened for %s." % (award.rfq_title or rfq_id),
                    detail={"thresholds": thresholds.to_dict()})
        self._event(award, ExecutionEventType.PROPOSAL_SEEDED,
                    "Seeded %d lines: %d took best value, %d took the cheapest quote, "
                    "%d have no comparable quote."
                    % (len(award.lines),
                       sum(1 for l in award.lines if l.pick_source == PickSource.BEST_VALUE.value),
                       sum(1 for l in award.lines if l.pick_source == PickSource.CHEAPEST.value),
                       sum(1 for l in award.lines if not l.awarded)),
                    actor="system")
        return award

    @staticmethod
    def _seed_line(line) -> AwardLine:
        """Best value where it exists, else the cheapest. The buyer changes either."""
        candidate = line.best_value or line.cheapest
        source = (PickSource.BEST_VALUE.value if line.best_value
                  else PickSource.CHEAPEST.value) if candidate else PickSource.NONE.value
        return line_from_candidate(line, candidate, source)

    def set_thresholds(self, award_id: str, thresholds: AwardThresholds,
                       today: Optional[_dt.date] = None) -> Award:
        award = self._editable(award_id)
        previous = award.thresholds
        award.thresholds = thresholds
        self.store.save_award(award)
        self._event(award, ExecutionEventType.THRESHOLDS_SET,
                    thresholds.describe(), previous={"thresholds": previous.to_dict()},
                    detail={"thresholds": thresholds.to_dict()})
        return self.reseed(award_id, thresholds, today=today)

    def reseed(self, award_id: str, thresholds: Optional[AwardThresholds] = None,
               today: Optional[_dt.date] = None) -> Award:
        """Recompute the proposal and move the lines the buyer has not spoken for.

        A line the buyer overrode keeps its supplier: their decision outranks ours, and
        silently replacing it would be the one unforgivable behaviour in this phase. A line
        still sitting on its seed does move, and says what it was.
        """
        award = self._editable(award_id)
        thresholds = thresholds or award.thresholds
        ctx = self.context(award.rfq_id, award.currency, today)
        proposal = propose_award(ctx.matrix, thresholds, currency=ctx.currency, today=today)

        award.thresholds = thresholds
        award.currency = proposal.currency
        award.currency_reason = proposal.currency_reason
        award.assumptions = list(proposal.assumptions)
        award.rate_provenance = dict(proposal.rate_provenance)

        moved: List[Tuple[AwardLine, AwardLine]] = []
        rebuilt: List[AwardLine] = []
        for line in proposal.lines:
            existing = award.line(line.line_item_id)
            if existing is not None and existing.is_override:
                # Keep the buyer's supplier; refresh only what the screen explains with.
                existing.seeded_cheapest = line.cheapest.to_dict() if line.cheapest else None
                existing.seeded_best_value = line.best_value.to_dict() if line.best_value else None
                existing.difference_note = line.difference_note
                existing.absent_reason = line.absent_reason
                rebuilt.append(existing)
                continue
            fresh = self._seed_line(line)
            if existing is not None:
                fresh.id = existing.id
                if existing.supplier_id != fresh.supplier_id:
                    moved.append((existing, fresh))
            rebuilt.append(fresh)

        award.lines = rebuilt
        self.store.save_award(award)
        for was, now in moved:
            self._event(award, ExecutionEventType.LINE_RESEEDED,
                        "%s moved from %s to %s when the quality bar changed."
                        % (now.line_item_id, was.supplier_name or "no supplier",
                           now.supplier_name or "no supplier"),
                        subject_type="line", subject_id=now.line_item_id, actor="system",
                        previous={"supplier_id": was.supplier_id,
                                  "supplier_name": was.supplier_name,
                                  "unit_price": was.unit_price,
                                  "pick_source": was.pick_source},
                        detail={"supplier_id": now.supplier_id,
                                "supplier_name": now.supplier_name,
                                "unit_price": now.unit_price,
                                "pick_source": now.pick_source})
        return award

    def set_line(self, award_id: str, line_item_id: str, choice: str, reason: str = "",
                 today: Optional[_dt.date] = None) -> Award:
        """Award one line. `choice` is "cheapest", "best_value", "none", or a supplier id.

        Choosing a supplier outright requires a reason: an override nobody explained is a
        mystery six months later, when it matters most.
        """
        award = self._editable(award_id)
        existing = award.line(line_item_id)
        if existing is None:
            raise AwardError("%s is not a line on this award." % line_item_id)

        ctx = self.context(award.rfq_id, award.currency, today)
        proposal = propose_award(ctx.matrix, award.thresholds, currency=ctx.currency,
                                 today=today)
        line = proposal.line(line_item_id)
        if line is None:
            raise AwardError("%s is no longer a line on this RFQ." % line_item_id)

        if choice == PickSource.NONE.value:
            candidate, source = None, PickSource.NONE.value
        elif choice in (PickSource.CHEAPEST.value, PickSource.BEST_VALUE.value):
            candidate = line.candidate(choice)
            if candidate is None:
                raise AwardError("There is no %s quote for %s."
                                 % (choice.replace("_", " "), line_item_id))
            source = choice
        else:
            candidate = self._candidate_for(ctx, proposal, line_item_id, choice)
            if candidate is None:
                raise AwardError(
                    "%s has no price for %s that this award can use."
                    % (ctx.name(choice), line_item_id))
            source = PickSource.BUYER_OVERRIDE.value
            if not (reason or "").strip():
                raise AwardError("Tell me why you are choosing %s for %s. An override "
                                 "without a reason cannot be explained later."
                                 % (ctx.name(choice), line_item_id))

        fresh = line_from_candidate(line, candidate, source, (reason or "").strip())
        fresh.id = existing.id
        award.lines = [fresh if l.line_item_id == line_item_id else l for l in award.lines]
        award.decided_at = utc_now()
        self.store.save_award(award)

        if source == PickSource.NONE.value:
            event, summary = ExecutionEventType.LINE_CLEARED, \
                "%s will not be awarded." % line_item_id
        elif source == PickSource.BUYER_OVERRIDE.value:
            event, summary = ExecutionEventType.LINE_OVERRIDDEN, \
                "%s awarded to %s by your decision: %s" % (line_item_id, fresh.supplier_name,
                                                           fresh.override_reason)
        else:
            event, summary = ExecutionEventType.LINE_PICKED, \
                "%s awarded to %s (%s)." % (line_item_id, fresh.supplier_name,
                                            source.replace("_", " "))
        self._event(award, event, summary, subject_type="line", subject_id=line_item_id,
                    previous={"supplier_id": existing.supplier_id,
                              "supplier_name": existing.supplier_name,
                              "unit_price": existing.unit_price,
                              "pick_source": existing.pick_source,
                              "override_reason": existing.override_reason},
                    detail={"supplier_id": fresh.supplier_id,
                            "supplier_name": fresh.supplier_name,
                            "unit_price": fresh.unit_price, "pick_source": fresh.pick_source,
                            "override_reason": fresh.override_reason})
        return award

    @staticmethod
    def _candidate_for(ctx, proposal: AwardProposal, line_item_id: str, supplier_id: str):
        """A named supplier's quote for a line, if the award rules can use it."""
        from .analyst_calculations import price_check
        line = proposal.line(line_item_id)
        for candidate in (line.cheapest, line.best_value):
            if candidate is not None and candidate.supplier_id == supplier_id:
                return candidate
        cell = ctx.cell(line_item_id, supplier_id)
        check = price_check(cell, ctx)
        if not check.valid:
            return None
        from .award_models import Candidate
        quote = cell.quote
        qualification = ctx.qualifications.get(supplier_id)
        return Candidate(
            supplier_id=supplier_id, supplier_name=ctx.name(supplier_id),
            amount=check.amount, native=check.native,
            native_unit_price=quote.normalized_unit_price if quote else None,
            native_currency=quote.currency if quote else "", rate_note=check.rate_note,
            caveats=list(check.caveats), evidence_ids=list(check.evidence_ids),
            quote_id=quote.id if quote else "", response_id=quote.response_id if quote else "",
            qualification=qualification.status if qualification else "")

    # ------------------------------------------------------------ approving
    def review(self, award_id: str) -> Award:
        award = self.get(award_id)
        report = self.validate(award_id)
        self._transition(award, AwardStatus.REVIEWED.value,
                         ExecutionEventType.AWARD_REVIEWED,
                         "Validation read: %d blocking, %d to acknowledge."
                         % (len(report.blocking), len(report.warnings)),
                         detail={"blocking": len(report.blocking),
                                 "warnings": report.warning_codes()})
        return award

    def approve(self, award_id: str, acknowledged: Optional[List[str]] = None,
                today: Optional[_dt.date] = None) -> Award:
        """Freeze the decision. Nothing on the award changes after this."""
        award = self.get(award_id)
        report = self.validate(award_id, today)
        dropped = self._drop_stale_lines(award, report)
        if dropped:
            # A pick went stale after it was made — a corrected price, a revision. Re-read
            # the award rather than reporting on the one we just changed.
            self.store.save_award(award)
            report = self.validate(award_id, today)
        if report.blocking:
            raise AwardError("This award cannot be approved yet: %s"
                             % report.blocking[0].message)

        # Reading the validation *is* the review, so a caller who goes straight to approve
        # has still done it. The event is recorded either way, because the history should
        # show what the checks said before the commitment.
        if award.status == AwardStatus.DRAFT.value:
            self._transition(award, AwardStatus.REVIEWED.value,
                             ExecutionEventType.AWARD_REVIEWED,
                             "Validation read: %d note%s." % (len(report.warnings),
                                                              "" if len(report.warnings) == 1 else "s"),
                             detail={"warnings": report.warning_codes()})

        totals = award_totals(award)
        # What was on screen when the buyer committed. Nothing was ticked to get here —
        # making someone tick a checkbox is not the same as making them read it — but the
        # record still has to show what they were looking at.
        award.acknowledged_warnings = list(acknowledged or []) or report.warning_codes()
        award.validation_at_approval = report.to_dict()
        award.approved_at = utc_now()
        self._transition(award, AwardStatus.APPROVED.value,
                         ExecutionEventType.AWARD_APPROVED,
                         "Approved: %s to %d supplier%s across %d lines."
                         % (totals.describe(), len(award.supplier_ids),
                            "" if len(award.supplier_ids) == 1 else "s",
                            len(award.awarded_lines)),
                         detail={"notes_showing": report.warning_codes(),
                                 "dropped_lines": dropped,
                                 "totals": totals.to_dict()})
        return award

    def _drop_stale_lines(self, award: Award, report) -> List[str]:
        """Un-award any line whose chosen quote can no longer be committed to.

        These findings only appear when the data moved after the pick was made: a price
        corrected on the comparison screen, a revision that arrived since. Refusing the
        whole award over one of them stops thirty good lines for one bad one, so the line
        is dropped, its reason is kept where the buyer will read it, and the approval event
        names it.
        """
        from .award_validation import DROPS_THE_LINE

        reasons: Dict[str, str] = {}
        for finding in report.findings:
            if finding.code in DROPS_THE_LINE and finding.line_item_id:
                reasons.setdefault(finding.line_item_id, finding.message)
        dropped: List[str] = []
        for line in award.lines:
            reason = reasons.get(line.line_item_id)
            if reason is None or not line.awarded:
                continue
            line.supplier_id, line.supplier_name = "", ""
            line.unit_price, line.extended, line.native_text = None, None, ""
            line.quote_id, line.response_id = "", ""
            line.pick_source = PickSource.NONE.value
            line.absent_reason = reason
            dropped.append(line.line_item_id)
            self._event(award, ExecutionEventType.LINE_CLEARED,
                        "%s was dropped at approval: %s" % (line.line_item_id, reason),
                        subject_type="line", subject_id=line.line_item_id, actor="system")
        return dropped

    def cancel(self, award_id: str, reason: str) -> Award:
        """The only way back from an approved award. History stays whole."""
        award = self.get(award_id)
        if not (reason or "").strip():
            raise AwardError("Tell me why this award is being cancelled.")
        award.cancelled_reason = reason.strip()
        self._transition(award, AwardStatus.CANCELLED.value,
                         ExecutionEventType.AWARD_CANCELLED,
                         "Cancelled: %s" % reason.strip())
        return award

    # ------------------------------------------------------ communication
    def build_facts(self, award: Award, supplier_id: str, ctx) -> CommunicationFacts:
        """Everything the model may know about one supplier's award.

        Built from that supplier's rows and bundle only. There is no parameter here
        through which a second supplier's data could arrive, which is what makes the
        isolation structural rather than a matter of prompt wording.
        """
        supplier = next((s for s in ctx.matrix.suppliers if s.id == supplier_id), None)
        bundle = ctx.bundle(supplier_id)
        lines = award.lines_for(supplier_id)
        suppressed: List[str] = []

        def term(attr: str) -> str:
            text, dropped = stated(terms_text(bundle, attr) if bundle else "")
            if dropped:
                suppressed.append(attr)
            return text

        quote = first_priced(bundle) if bundle else None
        moq = NOT_PROVIDED
        if quote is not None and quote.minimum_order_quantity is not None:
            moq = "{:,.0f} {}".format(quote.minimum_order_quantity, quote.moq_unit or "pcs")

        facts = CommunicationFacts(
            award_id=award.id, supplier_id=supplier_id,
            supplier_name=supplier.name if supplier else supplier_id,
            supplier_contact_name=supplier.contact_name if supplier else "",
            supplier_country=supplier.country if supplier else "",
            buyer_organisation=BUYER_ORGANISATION,
            rfq_reference=award.rfq_id, rfq_title=award.rfq_title,
            currency=award.currency,
            lead_time_stated=term("lead_time_text"),
            payment_terms_stated=term("payment_terms"),
            delivery_terms_stated=term("delivery_terms"),
            validity_stated=term("quote_validity_text"),
            minimum_order_stated=moq, next_step=NEXT_STEP)

        subtotal = 0.0
        priced = 0
        for line in sorted(lines, key=lambda l: l.line_item_id):
            description, dropped = neutralise(line.line_label, 120)
            if dropped:
                suppressed.append("line_label:%s" % line.line_item_id)
            facts.lines.append(CommunicationLine(
                line_reference=line.line_item_id, description=description,
                quantity=line.quantity, unit=line.unit, unit_price=line.unit_price,
                extended=line.extended, as_quoted=line.native_text,
                native_currency=line.native_currency))
            if line.extended is not None:
                subtotal += line.extended
                priced += 1
        facts.subtotal = round(subtotal, 2) if priced == len(lines) and lines else None

        for label, value in (("lead time", facts.lead_time_stated),
                             ("payment terms", facts.payment_terms_stated),
                             ("delivery terms", facts.delivery_terms_stated)):
            if value == NOT_PROVIDED:
                facts.open_points.append("%s were not stated in your quotation" % label
                                         if label != "lead time"
                                         else "lead time was not stated in your quotation")
        if facts.subtotal is None and lines:
            facts.open_points.append("some awarded lines have no agreed price yet")
        facts.suppressed_fields = suppressed
        return facts

    def draft_communications(self, award_id: str, explain: bool = True,
                             on_stage: Optional[Callable[[str], None]] = None
                             ) -> List[SupplierCommunication]:
        """One letter per awarded supplier, each written from that supplier's facts alone."""
        award = self.get(award_id)
        if award.status not in (AwardStatus.APPROVED.value,
                                AwardStatus.READY_TO_EXECUTE.value):
            raise AwardError("Approve the award before preparing supplier messages.")

        def stage(i: int) -> None:
            if on_stage:
                on_stage(STAGES[i])

        stage(0)
        ctx = self.context(award.rfq_id, award.currency)
        existing = {c.supplier_id: c for c in self.communications(award_id)}
        out: List[SupplierCommunication] = []

        for supplier_id in award.supplier_ids:
            if existing.get(supplier_id) and existing[supplier_id].status in (
                    CommunicationStatus.SENT.value, CommunicationStatus.APPROVED.value):
                out.append(existing[supplier_id])
                continue
            facts = self.build_facts(award, supplier_id, ctx)
            others = [s.name for s in ctx.matrix.suppliers if s.id != supplier_id]
            stage(1)
            comm = self._draft_one(award, facts, others, explain)
            if existing.get(supplier_id):
                comm.id = existing[supplier_id].id
            self.store.save_communication(comm)
            self._event(award,
                        ExecutionEventType.COMMUNICATION_DRAFTED
                        if comm.guard_status == "ok"
                        else ExecutionEventType.COMMUNICATION_REJECTED,
                        "Draft prepared for %s%s." % (
                            facts.supplier_name,
                            "" if comm.guard_status == "ok"
                            else " — the written draft could not be verified, so the "
                                 "standard letter is shown"),
                        subject_type="communication", subject_id=comm.id, actor="system",
                        detail={"guard_status": comm.guard_status,
                                "generated_by": comm.generated_by,
                                "lines": len(facts.lines)})
            out.append(comm)

        if award.status == AwardStatus.APPROVED.value:
            self._transition(award, AwardStatus.READY_TO_EXECUTE.value,
                             ExecutionEventType.AWARD_ARMED,
                             "%d supplier message%s ready for your review."
                             % (len(out), "" if len(out) == 1 else "s"))
        return out

    def _draft_one(self, award: Award, facts: CommunicationFacts, others: List[str],
                   explain: bool) -> SupplierCommunication:
        """Ask for wording; keep it only if the award supports every word."""
        comm = SupplierCommunication(
            award_id=award.id, supplier_id=facts.supplier_id,
            supplier_name=facts.supplier_name,
            recipient=self._recipient(award, facts.supplier_id),
            subject=communication_subject(facts), facts=facts.to_dict())
        fallback = render_fallback_communication(facts)

        if not explain or not self.settings.analyst_explain:
            comm.body = fallback
            comm.generated_by = "deterministic"
            comm.guard_status = "skipped"
            return comm

        started = time.time()
        try:
            data, records = self._call(build_communication_prompt(facts),
                                       COMMUNICATION_SCHEMA, COMMUNICATION_SYSTEM_PROMPT,
                                       "award_communication", COMMUNICATION_PROMPT_VERSION,
                                       award.rfq_id)
        except AIError as e:
            comm.body = fallback
            comm.generated_by = "deterministic"
            comm.guard_status = "fallback: %s" % e.user_message
            return comm
        for record in records:
            self.audit.add_ai_call(record)
        comm.model = next((r.model for r in reversed(records) if r.ok), "")
        comm.prompt_version = COMMUNICATION_PROMPT_VERSION
        comm.duration_ms = int((time.time() - started) * 1000)
        comm.omitted = [str(x)[:120] for x in (data.get("omitted") or [])][:6]

        written = assemble_communication(data)
        body, status = guard_communication(written, facts, others)
        comm.guard_status = status
        if body is None:
            comm.body = fallback
            comm.rejected_body = written
            comm.generated_by = "deterministic"
            comm.status = CommunicationStatus.REJECTED.value
        else:
            comm.body = body
            comm.generated_by = "claude"
            subject = (data.get("subject") or "").strip()
            if subject:
                comm.subject = subject[:160]
        return comm

    def _recipient(self, award: Award, supplier_id: str) -> str:
        supplier = self.suppliers.supplier(supplier_id)
        if supplier is None:
            return ""
        return supplier.contact_email or supplier.contact_name or supplier.name

    def edit_communication(self, comm_id: str, body: str) -> SupplierCommunication:
        """Keep the buyer's wording alongside the draft, never instead of it."""
        comm = self.store.get_communication(comm_id)
        if comm is None:
            raise AwardError("That message no longer exists.")
        if comm.status == CommunicationStatus.SENT.value:
            raise AwardError("That message has already been recorded as sent.")
        award = self.get(comm.award_id)
        previous = comm.edited_body or comm.body
        comm.edited_body = (body or "").strip()
        comm.edited_at = utc_now()
        comm.status = CommunicationStatus.EDITED.value
        # The buyer's wording faces the same guard the model's draft faced. It does not
        # refuse the edit — losing someone's typing to a guard is its own kind of wrong —
        # but a rejection is recorded and `record_sent` will not accept it.
        _, comm.edit_status = self._verify(award, comm, comm.text)
        comm.edit_leaks = self._leaks(award, comm, comm.text)
        self.store.save_communication(comm)
        self._event(award, ExecutionEventType.COMMUNICATION_EDITED,
                    "You edited the message to %s." % comm.supplier_name,
                    subject_type="communication", subject_id=comm.id,
                    previous={"body": previous, "status": CommunicationStatus.DRAFT.value},
                    detail={"chars": len(comm.edited_body), "check": comm.edit_status,
                            "leaked_names": list(comm.edit_leaks)})
        return comm

    def _verify(self, award: Award, comm: SupplierCommunication,
                text: str) -> Tuple[Optional[str], str]:
        ctx = self.context(award.rfq_id, award.currency)
        facts = self.build_facts(award, comm.supplier_id, ctx)
        others = [s.name for s in ctx.matrix.suppliers if s.id != comm.supplier_id]
        return guard_communication(text, facts, others)

    def _leaks(self, award: Award, comm: SupplierCommunication, text: str) -> List[str]:
        ctx = self.context(award.rfq_id, award.currency)
        facts = self.build_facts(award, comm.supplier_id, ctx)
        others = [s.name for s in ctx.matrix.suppliers if s.id != comm.supplier_id]
        return leaked_names(text, facts, others)

    def check_edit(self, comm_id: str) -> Tuple[Optional[str], str]:
        """Run the buyer's own wording past the same guard the model's draft faced."""
        comm = self.store.get_communication(comm_id)
        if comm is None:
            raise AwardError("That message no longer exists.")
        return self._verify(self.get(comm.award_id), comm, comm.text)

    def record_sent(self, comm_id: str) -> SupplierCommunication:
        """Record that the buyer sent this. Nothing leaves this machine."""
        comm = self.store.get_communication(comm_id)
        if comm is None:
            raise AwardError("That message no longer exists.")
        if not comm.sendable:
            raise AwardError(
                "Your edit to the message for %s could not be verified against the award "
                "(%s), so it cannot be recorded as sent. Correct the wording, or revert to "
                "the prepared draft." % (comm.supplier_name, comm.edit_status))
        award = self.get(comm.award_id)
        previous = comm.status
        comm.status = CommunicationStatus.SENT.value
        comm.sent_at = utc_now()
        comm.approved_at = comm.approved_at or comm.sent_at
        self.store.save_communication(comm)
        self._event(award, ExecutionEventType.NOTIFICATION_RECORDED,
                    "Recorded as sent to %s at %s. This is a simulated send: the "
                    "application has no mail connection." % (comm.supplier_name,
                                                             comm.recipient or "no contact"),
                    subject_type="communication", subject_id=comm.id,
                    previous={"status": previous},
                    detail={"recipient": comm.recipient, "edited": comm.was_edited})

        sent = {c.supplier_id for c in self.communications(award.id)
                if c.status == CommunicationStatus.SENT.value}
        if award.status == AwardStatus.READY_TO_EXECUTE.value and \
                sent >= set(award.supplier_ids):
            award.notified_at = utc_now()
            self._transition(award, AwardStatus.SUPPLIER_NOTIFIED.value,
                             ExecutionEventType.NOTIFICATION_RECORDED,
                             "Every awarded supplier has been notified.")
        return comm

    # ---------------------------------------------------------- handoff
    def generate_handoff(self, award_id: str, today: Optional[_dt.date] = None
                         ) -> List[OrderHandoff]:
        """The structured order, per supplier, with no model involved.

        Re-validated here rather than trusting the approval snapshot: a supplier revision
        can land in between, and shipping an order against a superseded price is the most
        expensive mistake this phase can make.
        """
        award = self.get(award_id)
        if award.status not in (AwardStatus.SUPPLIER_NOTIFIED.value,
                                AwardStatus.ORDER_HANDOFF.value):
            raise AwardError("Record the supplier messages as sent before generating the "
                             "order handoff.")
        report = self.validate(award_id, today)
        if report.blocking:
            raise AwardError("The order handoff cannot be generated: %s"
                             % report.blocking[0].message)

        ctx = self.context(award.rfq_id, award.currency, today)
        # The decision screen reports and does not gate, because a buyer reaching it has
        # already worked through the comparison. This is the other end: the handoff is the
        # document treated as an order. Its commercial terms are read live, so a supplier
        # whose response has gone since approval would produce an order whose payment and
        # delivery terms are silently blank. That is worth stopping for.
        withdrawn = [award.supplier_name_for(sid) or sid for sid in award.supplier_ids
                     if ctx.bundle(sid) is None]
        if withdrawn:
            raise AwardError(
                "%s no longer has an active response on this RFQ, so the order would have "
                "no commercial terms. Re-check the quote before generating the handoff."
                % ", ".join(sorted(withdrawn)))
        out: List[OrderHandoff] = []
        for ordinal, supplier_id in enumerate(award.supplier_ids, start=1):
            out.append(self._handoff_for(award, supplier_id, ctx, ordinal))

        if award.status == AwardStatus.SUPPLIER_NOTIFIED.value:
            self._transition(award, AwardStatus.ORDER_HANDOFF.value,
                             ExecutionEventType.HANDOFF_GENERATED,
                             "%d order handoff%s generated."
                             % (len(out), "" if len(out) == 1 else "s"))
        return out

    def _handoff_for(self, award: Award, supplier_id: str, ctx, ordinal: int) -> OrderHandoff:
        supplier = self.suppliers.supplier(supplier_id)
        bundle = ctx.bundle(supplier_id)
        facts = self.build_facts(award, supplier_id, ctx)
        lines = award.lines_for(supplier_id)

        handoff = OrderHandoff(
            award_id=award.id, supplier_id=supplier_id,
            reference="HANDOFF-%s-%02d" % (award.rfq_id, ordinal),
            rfq_reference=award.rfq_id, rfq_title=award.rfq_title,
            buyer_organisation=BUYER_ORGANISATION,
            supplier_name=supplier.name if supplier else supplier_id,
            supplier_contact_name=supplier.contact_name if supplier else "",
            supplier_contact_email=supplier.contact_email if supplier else "",
            supplier_country=supplier.country if supplier else "",
            currency=award.currency or "",
            payment_terms=facts.payment_terms_stated,
            delivery_terms=facts.delivery_terms_stated,
            lead_time=facts.lead_time_stated,
            quote_validity=facts.validity_stated,
            minimum_order=facts.minimum_order_stated,
            assumptions=list(award.assumptions),
            open_points=list(facts.open_points),
            source_note="Every value here is the supplier's own, taken from %s. Nothing has "
                        "been negotiated or inferred."
                        % (", ".join(d.filename for d in bundle.documents)
                           if bundle and bundle.documents else "their quotation"))

        subtotal, priced = 0.0, 0
        for line in sorted(lines, key=lambda l: l.line_item_id):
            handoff.lines.append(HandoffLine(
                line_reference=line.line_item_id,
                description=neutralise(line.line_label, 120)[0],
                quantity=line.quantity, unit=line.unit, unit_price=line.unit_price,
                currency=award.currency or "", extended=line.extended,
                as_quoted=line.native_text, evidence_ids=list(line.evidence_ids)))
            if line.extended is not None:
                subtotal += line.extended
                priced += 1
        handoff.subtotal = round(subtotal, 2) if priced == len(lines) and lines else None

        if bundle is not None:
            handoff.certifications = ["%s — %s" % (c.name, c.status.value)
                                      for c in bundle.certifications]
        self.store.save_handoff(handoff)
        self._event(award, ExecutionEventType.HANDOFF_GENERATED,
                    "Order handoff %s prepared for %s: %d lines%s."
                    % (handoff.reference, handoff.supplier_name, len(handoff.lines),
                       (", %s %s" % (handoff.currency, "{:,.2f}".format(handoff.subtotal)))
                       if handoff.subtotal is not None else ""),
                    subject_type="handoff", subject_id=handoff.id, actor="system",
                    detail={"reference": handoff.reference, "lines": len(handoff.lines)})
        return handoff

    def complete(self, award_id: str) -> Award:
        award = self.get(award_id)
        self._transition(award, AwardStatus.COMPLETED.value,
                         ExecutionEventType.AWARD_COMPLETED,
                         "Award executed and closed.")
        award = self.get(award_id)
        award.completed_at = utc_now()
        self.store.save_award(award)
        return award

    # ---------------------------------------------------------- internals
    def _editable(self, award_id: str) -> Award:
        award = self.get(award_id)
        if not is_editable(award.status):
            raise AwardError(
                "This award was approved on %s and cannot be changed. Cancel it and start "
                "a new one if the decision has moved."
                % (award.approved_at[:10] or "approval"))
        return award

    def _transition(self, award: Award, to: str, event_type: ExecutionEventType,
                    summary: str, detail: Optional[Dict[str, Any]] = None) -> None:
        """The only writer of award status."""
        if to not in TRANSITIONS.get(award.status, frozenset()):
            raise AwardError("An award at '%s' cannot move to '%s'."
                             % (award.status.replace("_", " "), to.replace("_", " ")))
        previous = award.status
        award.status = to
        self.store.save_award(award)
        self._event(award, event_type, summary, previous={"status": previous},
                    detail=detail or {}, from_state=previous, to_state=to)

    def _event(self, award: Award, event_type: ExecutionEventType, summary: str, *,
               subject_type: str = "award", subject_id: str = "", actor: str = "buyer",
               previous: Optional[Dict[str, Any]] = None,
               detail: Optional[Dict[str, Any]] = None,
               from_state: str = "", to_state: str = "") -> None:
        self.store.add_event(ExecutionEvent(
            award_id=award.id, event_type=event_type.value, actor=actor,
            subject_type=subject_type, subject_id=subject_id or award.id,
            from_state=from_state, to_state=to_state, summary=summary,
            previous=dict(previous or {}), detail=dict(detail or {})))

    def _call(self, prompt: str, schema: Dict[str, Any], system: str, call_type: str,
              version: str, rfq_id: Optional[str] = None, tier: str = "quality"
              ) -> Tuple[Dict[str, Any], List[AICallRecord]]:
        """One structured call with a single corrective retry, audited like every other."""
        attempt, records = prompt, []
        last: Optional[AIError] = None
        for _ in range(2):
            started = utc_now()
            try:
                res: AIResult = self.ai.complete_json(attempt, schema, system, tier=tier)
                records.append(self._record(call_type, version, attempt, res, None, started,
                                            rfq_id, tier))
                return res.data, records
            except AIInvalidOutput as e:
                last = e
                records.append(self._record(call_type, version, attempt, None, e, started,
                                            rfq_id, tier))
                attempt = prompt + ("\n\nYOUR PREVIOUS OUTPUT FAILED VALIDATION: %s\n"
                                    "Return a corrected JSON object matching the schema exactly."
                                    % str(e)[:400])
            except AIError as e:
                records.append(self._record(call_type, version, attempt, None, e, started,
                                            rfq_id, tier))
                raise
        assert last is not None
        raise last

    def _record(self, call_type: str, version: str, prompt: str, res: Optional[AIResult],
                err: Optional[AIError], started: str, rfq_id: Optional[str],
                tier: str) -> AICallRecord:
        return AICallRecord(
            id=new_id("call"), rfq_id=rfq_id, turn=0, call_type=call_type,
            provider=getattr(self.ai, "name", "unknown"),
            model=res.model if res else getattr(self.ai, "model_for", lambda t: "?")(tier),
            prompt_version=version,
            prompt_hash=hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16],
            prompt_chars=len(prompt), duration_ms=res.duration_ms if res else 0,
            ok=res is not None, schema_valid=bool(res and res.schema_valid),
            error=("%s: %s" % (type(err).__name__, err)) if err else None,
            raw_response=(res.raw if res else (getattr(err, "raw", "") or ""))[:100000],
            prompt_text=prompt if self.settings.log_prompts else None, created_at=started)
