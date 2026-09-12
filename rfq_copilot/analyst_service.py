"""The procurement analyst: a buyer's question in, a calculated answer out.

The whole design turns on one boundary. Claude reads the question and says what should be
computed; Claude later reads the computed result and describes it. In between, this
service and `analyst_calculations` do every piece of retrieval and arithmetic against the
same comparison dataset the Quotes screen renders. So an answer here can always be
reconciled with that screen, and a model that never sees a price cannot misreport one.

Nothing in this module writes to supplier data. A what-if changes which stored rows are
counted, never the rows.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .ai_service import AIError, AIInvalidOutput, AIResult, AIService
from .analyst_calculations import (
    CALCULATIONS, build_context, choose_comparison_currency,
)
from .analyst_guards import guard_explanation, validate_query
from .analyst_models import (
    AnalystQuery, AnalystQueryRecord, AnalystResult, AnalystTurn, Hypothetical, Intent,
    RawQuery, Refusal,
)
from .analyst_prompts import (
    EXPLAIN_PROMPT_VERSION, EXPLAIN_SYSTEM_PROMPT, PARSE_PROMPT_VERSION, PARSE_SYSTEM_PROMPT,
    build_explain_prompt, build_parse_prompt, build_vocabulary,
)
from .analyst_schemas import ANALYST_QUERY_SCHEMA, EXPLANATION_SCHEMA
from .config import Settings
from .persistence import AnalystRepository, RFQRepository, SupplierRepository
from .rfq_service import RFQStateError
from .schema import AICallRecord, new_id, utc_now
from .supplier_service import SupplierService

#: What the buyer sees while a question is being answered. No fake percentages: each line
#: appears when that stage actually begins.
STAGES = [
    "Understanding your question…",
    "Checking the supplier quotes…",
    "Preparing the procurement summary…",
]


class AnalystError(Exception):
    """Something the user can act on, phrased for them."""


class AnalystService:
    def __init__(self, ai: AIService, repo: RFQRepository, settings: Optional[Settings] = None,
                 supplier_service: Optional[SupplierService] = None):
        self.ai = ai
        self.repo = repo
        self.settings = settings or Settings.from_env()
        self.suppliers = supplier_service or SupplierService(repo, ai, self.settings)
        self.store = AnalystRepository(repo)
        self.audit = SupplierRepository(repo)

    # ----------------------------------------------------------------- ask
    def ask(self, rfq_id: str, question: str, display_currency: Optional[str] = None,
            explain: bool = True, on_stage: Optional[Callable[[str], None]] = None,
            today: Optional[_dt.date] = None) -> AnalystResult:
        """Answer one question. Two model calls at most, and neither of them holds a price."""
        text = (question or "").strip()
        if not text:
            raise AnalystError("Ask a question about this RFQ and I will work it out.")

        def stage(i: int) -> None:
            if on_stage:
                on_stage(STAGES[i])

        started = time.time()
        matrix = self._matrix(rfq_id)
        stage(0)

        vocabulary = build_vocabulary(matrix.rfq, matrix.suppliers, matrix.bundles,
                                      matrix.currencies(), display_currency)
        history = self._history_for_prompt(rfq_id)
        prompt = build_parse_prompt(text, vocabulary, history)
        try:
            data, records = self._call(prompt, ANALYST_QUERY_SCHEMA, PARSE_SYSTEM_PROMPT,
                                       "analyst_parse", PARSE_PROMPT_VERSION, rfq_id)
        except AIError as e:
            raise AnalystError(e.user_message)
        model = next((r.model for r in reversed(records) if r.ok), "")
        for record in records:
            self.audit.add_ai_call(record)

        raw = RawQuery.from_dict(data)
        raw = self._merge_with_previous(rfq_id, raw)
        checked = validate_query(raw, matrix.rfq, matrix.suppliers)
        if isinstance(checked, Refusal):
            result = AnalystResult.refusal(text, checked.reason, checked.attempted_intent,
                                           checked.candidates)
            result.duration_ms = int((time.time() - started) * 1000)
            self._persist(rfq_id, result, model, PARSE_PROMPT_VERSION)
            return result

        stage(1)
        result = self._execute(rfq_id, checked, text, display_currency, matrix, today)

        if explain and self.settings.analyst_explain and not result.refused and result.rows:
            stage(2)
            self._explain(result, rfq_id)
        result.duration_ms = int((time.time() - started) * 1000)
        self._persist(rfq_id, result, model, PARSE_PROMPT_VERSION)
        return result

    def run_query(self, rfq_id: str, query: AnalystQuery, display_currency: Optional[str] = None,
                  question: str = "", today: Optional[_dt.date] = None) -> AnalystResult:
        """Answer a question we already know the shape of. No model call at all.

        The suggested questions on the page go through here, so a buyer can get the common
        answers instantly and without spending a model call on a question we composed.
        """
        started = time.time()
        result = self._execute(rfq_id, query, question or query.reading, display_currency,
                               None, today)
        result.duration_ms = int((time.time() - started) * 1000)
        self._persist(rfq_id, result, "", "")
        return result

    # ------------------------------------------------------------ internals
    def _matrix(self, rfq_id: str, display_currency: Optional[str] = None):
        try:
            matrix = self.suppliers.build_comparison(rfq_id, display_currency=display_currency)
        except RFQStateError as e:
            raise AnalystError(str(e))
        if not matrix.bundles:
            raise AnalystError(
                "No supplier responses have been extracted for this RFQ yet, so there is "
                "nothing for me to analyse. Load and extract the responses first.")
        return matrix

    def _execute(self, rfq_id: str, query: AnalystQuery, question: str,
                 display_currency: Optional[str], matrix: Optional[Any],
                 today: Optional[_dt.date]) -> AnalystResult:
        """Choose the currency, rebuild the dataset in it if needed, and calculate."""
        matrix = matrix if matrix is not None else self._matrix(rfq_id)
        currency, reason = choose_comparison_currency(matrix, query, display_currency)

        # Converting costs a rate lookup, so only do it when more than one currency is in
        # play. With a single currency the stored figures are already comparable.
        if currency and len(matrix.currencies()) > 1 and matrix.display_currency != currency:
            matrix = self._matrix(rfq_id, display_currency=currency)

        ctx = build_context(matrix, query, currency, reason, today=today,
                            session_currency=display_currency)
        calculate = CALCULATIONS.get(query.intent)
        if calculate is None:
            return AnalystResult.refusal(question, "I have no way to work that out",
                                         query.intent)
        result = calculate(ctx)
        result.question = question
        if not result.intent:
            result.intent = query.intent
        return result

    def _explain(self, result: AnalystResult, rfq_id: str) -> None:
        """Ask for a sentence, and keep it only if the result already supports every word."""
        try:
            data, records = self._call(build_explain_prompt(result), EXPLANATION_SCHEMA,
                                       EXPLAIN_SYSTEM_PROMPT, "analyst_explain",
                                       EXPLAIN_PROMPT_VERSION, rfq_id, tier="fast")
        except AIError as e:
            result.explanation_status = "failed: %s" % e.user_message
            return
        for record in records:
            self.audit.add_ai_call(record)

        text, status = guard_explanation(str(data.get("explanation") or ""), result)
        result.explanation, result.explanation_status = text, status
        if text:
            for caveat in (data.get("caveats") or [])[:4]:
                caveat = str(caveat).strip()
                if caveat and caveat not in result.warnings:
                    kept, _ = guard_explanation(caveat, result)
                    if kept:
                        result.warnings.append(kept)

    def _merge_with_previous(self, rfq_id: str, raw: RawQuery) -> RawQuery:
        """Carry a what-if forward when the buyer refines rather than restarts.

        "Who is cheapest?" then "only among those who cleared QA" has to keep the first
        question's assumptions. Changing the subject must not, which is why the planner
        decides `refines_previous` and this only ever adds.
        """
        if not raw.refines_previous:
            return raw
        turns = self.history(rfq_id, limit=1)
        previous = turns[-1].query if turns else None
        if previous is None:
            return raw
        raw.hypothetical = Hypothetical.from_dict(previous.hypothetical.to_dict()).union(
            raw.hypothetical)
        if not raw.comparison_currency:
            raw.comparison_currency = previous.comparison_currency
        # Only the assumptions carry over. A line or a supplier named in an earlier
        # question belongs to that question: inheriting "line 17" from "why was X excluded
        # on line 17?" would silently answer a broad follow-up about one line.
        return raw

    def history(self, rfq_id: str, limit: int = 4) -> List[AnalystTurn]:
        """Recent exchanges, rebuilt from the audit trail so a page reload keeps context."""
        out: List[AnalystTurn] = []
        for record in self.store.list_queries(rfq_id, limit=limit):
            query = None
            payload = record.query_dict()
            if payload:
                try:
                    query = AnalystQuery.from_dict(payload)
                except (TypeError, ValueError):
                    query = None
            out.append(AnalystTurn(question=record.question, query=query,
                                   summary=record.result_summary, refused=record.refused,
                                   created_at=record.created_at))
        return out

    def _history_for_prompt(self, rfq_id: str) -> List[Dict[str, Any]]:
        """Questions and the requests they became — never the results they produced."""
        turns = self.history(rfq_id, limit=self.settings.analyst_context_turns)
        out: List[Dict[str, Any]] = []
        for turn in turns:
            if not turn.question:
                continue
            entry: Dict[str, Any] = {"question": turn.question, "refused": turn.refused}
            if turn.query is not None:
                payload = turn.query.to_dict()
                payload.pop("resolution_notes", None)
                entry["became"] = payload
            out.append(entry)
        return out

    def _persist(self, rfq_id: str, result: AnalystResult, model: str, version: str) -> None:
        self.store.add_query(AnalystQueryRecord.build(rfq_id, result, model, version))

    def suggested_queries(self, rfq_id: str) -> List[Tuple[str, AnalystQuery]]:
        """The questions worth a single click. Each is a real query, run deterministically."""
        return [
            ("Cheapest by line", AnalystQuery(
                intent=Intent.CHEAPEST_BY_LINE.value,
                reading="Which supplier is cheapest on each line?")),
            ("Supplier coverage", AnalystQuery(
                intent=Intent.SUPPLIER_COVERAGE.value,
                reading="How much of the RFQ did each supplier quote?")),
            ("What should I review?", AnalystQuery(
                intent=Intent.UNRESOLVED_ISSUES.value,
                reading="What should I review before deciding?")),
            ("RFQ completeness", AnalystQuery(
                intent=Intent.RFQ_COMPLETENESS.value,
                reading="How much of this RFQ has a valid quote?")),
            ("Quality status", AnalystQuery(
                intent=Intent.QUALIFICATION_STATUS.value,
                reading="Which suppliers have cleared quality?")),
            ("Lead times", AnalystQuery(
                intent=Intent.LEAD_TIME_COMPARISON.value,
                reading="How do the lead times compare?")),
        ]

    # ---------------------------------------------------------------- AI plumbing
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
            prompt_version=version, prompt_hash=hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16],
            prompt_chars=len(prompt), duration_ms=res.duration_ms if res else 0,
            ok=res is not None, schema_valid=bool(res and res.schema_valid),
            error=("%s: %s" % (type(err).__name__, err)) if err else None,
            raw_response=(res.raw if res else (getattr(err, "raw", "") or ""))[:100000],
            prompt_text=prompt if self.settings.log_prompts else None, created_at=started)
