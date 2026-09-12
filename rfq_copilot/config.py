"""Runtime settings, read from environment variables with sensible defaults."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    """All tunables in one place. Only the Claude Code CLI provider exists in Phase 1."""

    model_quality: str = "sonnet"       # RFQ_AI_MODEL — default model for RFQ turns
    model_fast: str = "haiku"           # RFQ_AI_FAST_MODEL — for light, on-demand calls
    # The floor for one CLI call. The real deadline scales with how much the buyer sent:
    # a turn carrying a thirty-row variant table and fourteen answers is not the same
    # amount of work as "I need motors", and giving both 180 seconds meant the big turn —
    # exactly the one a buyer has put most effort into — was the one that failed.
    ai_timeout_s: int = 180             # RFQ_AI_TIMEOUT — floor for one CLI call
    ai_timeout_max_s: int = 600         # RFQ_AI_TIMEOUT_MAX — ceiling, so nothing hangs forever
    #: Seconds of extra allowance per 1,000 prompt characters. Cost is dominated by the
    #: output, and the output grows with what the buyer put in.
    ai_timeout_per_kchar_s: int = 25    # RFQ_AI_TIMEOUT_PER_KCHAR
    claude_bin: str = "claude"          # RFQ_CLAUDE_BIN — path or name of the CLI
    db_path: str = str(REPO_ROOT / "data" / "rfq_copilot.db")  # RFQ_DB_PATH
    log_prompts: bool = False           # RFQ_AI_LOG_PROMPTS — store full prompt text in ai_calls
    # The first screen should carry everything that materially matters, grouped by section.
    # Trickling basic universal fields into later turns reads as an interrogation.
    max_questions_first: int = 14
    max_questions_turn: int = 3
    max_open_questions: int = 14
    #: Supplier extractions are independent subprocess calls, so a few run at once.
    extraction_workers: int = 4          # RFQ_EXTRACTION_WORKERS
    #: The analyst always computes its answer; this only controls whether a second call is
    #: made to word it. Turning it off costs prose, never correctness.
    analyst_explain: bool = True         # RFQ_ANALYST_EXPLAIN
    #: How many earlier questions a follow-up can refine. Only questions and the requests
    #: they became are carried, never results.
    analyst_context_turns: int = 4       # RFQ_ANALYST_CONTEXT_TURNS

    @classmethod
    def from_env(cls) -> "Settings":
        s = cls()
        s.model_quality = os.environ.get("RFQ_AI_MODEL", s.model_quality).strip() or s.model_quality
        s.model_fast = os.environ.get("RFQ_AI_FAST_MODEL", s.model_fast).strip() or s.model_fast
        s.ai_timeout_s = _env_int("RFQ_AI_TIMEOUT", s.ai_timeout_s)
        s.ai_timeout_max_s = _env_int("RFQ_AI_TIMEOUT_MAX", s.ai_timeout_max_s)
        s.ai_timeout_per_kchar_s = _env_int("RFQ_AI_TIMEOUT_PER_KCHAR", s.ai_timeout_per_kchar_s)
        s.claude_bin = os.environ.get("RFQ_CLAUDE_BIN", s.claude_bin).strip() or s.claude_bin
        db = os.environ.get("RFQ_DB_PATH", "").strip()
        if db:
            p = Path(db).expanduser()
            s.db_path = str(p if p.is_absolute() else REPO_ROOT / p)
        s.log_prompts = _env_bool("RFQ_AI_LOG_PROMPTS", s.log_prompts)
        s.max_questions_first = _env_int("RFQ_MAX_QUESTIONS_FIRST", s.max_questions_first)
        s.max_questions_turn = _env_int("RFQ_MAX_QUESTIONS_TURN", s.max_questions_turn)
        s.max_open_questions = _env_int("RFQ_MAX_OPEN_QUESTIONS", s.max_open_questions)
        s.extraction_workers = max(1, _env_int("RFQ_EXTRACTION_WORKERS", s.extraction_workers))
        s.analyst_explain = _env_bool("RFQ_ANALYST_EXPLAIN", s.analyst_explain)
        s.analyst_context_turns = max(0, _env_int("RFQ_ANALYST_CONTEXT_TURNS",
                                                  s.analyst_context_turns))
        return s
