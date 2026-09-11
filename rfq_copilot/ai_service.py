"""AI service abstraction and the Claude Code CLI provider.

The real app uses ``ClaudeCLIProvider``: it shells out to ``claude -p`` with a
JSON Schema and reads the ``structured_output`` from the CLI's JSON envelope.
Authentication comes from the user's Claude subscription (``claude`` login);
no API key is involved. Tests use a stub that implements the same interface.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import jsonschema

from .config import Settings


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class AIError(Exception):
    """Base class. ``user_message`` is safe to show in the UI."""

    user_message = "The AI analysis failed."

    def __init__(self, message: str = "", raw: str = ""):
        super().__init__(message or self.user_message)
        self.raw = raw

    def __str__(self) -> str:  # pragma: no cover - trivial
        return super().__str__()


class AIUnavailable(AIError):
    user_message = "Claude Code CLI isn't available. Install it and run `claude` once to sign in."


class AINotAuthenticated(AIError):
    user_message = "Claude Code isn't authenticated. Run `claude` in your terminal and sign in with your Claude account."


class AITimeout(AIError):
    user_message = "The analysis took too long. Your answers have been saved. Try again."


class AITransient(AIError):
    """A dropped connection or an exhausted internal turn budget — safe to retry immediately."""

    user_message = "The connection to Claude dropped mid-analysis. Your answers have been saved. Try again."


class AIInvalidOutput(AIError):
    user_message = "The AI returned an unexpected response. Your answers have been saved. Try again."


# --------------------------------------------------------------------------- #
# Interface
# --------------------------------------------------------------------------- #
@dataclass
class AIResult:
    data: Dict[str, Any]
    raw: str
    provider: str
    model: str
    duration_ms: int
    schema_valid: bool = True
    attempts: int = 1


class AIService(ABC):
    name = "abstract"

    @abstractmethod
    def complete_json(self, prompt: str, schema: Dict[str, Any], system_prompt: str, tier: str = "quality") -> AIResult:
        """Return a schema-conforming JSON object or raise an ``AIError``."""

    def health(self) -> Dict[str, Any]:  # pragma: no cover - overridden where meaningful
        return {"available": True, "authenticated": True, "detail": ""}


def validate_against(schema: Dict[str, Any], data: Any) -> Optional[str]:
    """Return a short validation error message or None when valid."""
    try:
        jsonschema.Draft202012Validator(schema).validate(data)
        return None
    except jsonschema.ValidationError as e:  # pragma: no cover - message formatting
        path = "/".join(str(p) for p in e.absolute_path) or "<root>"
        return "%s at %s" % (e.message[:200], path)


# --------------------------------------------------------------------------- #
# Claude Code CLI provider
# --------------------------------------------------------------------------- #
Runner = Callable[..., "subprocess.CompletedProcess[str]"]

_STRIP_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")
_AUTH_MARKERS = ("not logged in", "please run /login", "not authenticated", "invalid api key", "authentication")
_LIMIT_MARKERS = ("usage limit", "rate limit", "rate_limit", "overloaded", "capacity")
_TRANSIENT_MARKERS = ("connection lost", "maximum number of turns", "api error", "timed out", "econnreset",
                      "failed to provide valid structured output")


class ClaudeCLIProvider(AIService):
    name = "claude_cli"

    def __init__(self, settings: Settings, runner: Optional[Runner] = None):
        self.settings = settings
        self._run: Runner = runner or subprocess.run
        self.effort = os.environ.get("RFQ_AI_EFFORT", "medium").strip() or "medium"
        self.max_turns = os.environ.get("RFQ_AI_MAX_TURNS", "6").strip() or "6"

    # -- public -------------------------------------------------------------
    def model_for(self, tier: str) -> str:
        return self.settings.model_fast if tier == "fast" else self.settings.model_quality

    def complete_json(self, prompt: str, schema: Dict[str, Any], system_prompt: str, tier: str = "quality") -> AIResult:
        model = self.model_for(tier)
        argv = self._argv(prompt, schema, system_prompt, model)
        started = time.time()
        try:
            proc = self._run(
                argv,
                capture_output=True,
                text=True,
                timeout=self.settings.ai_timeout_s,
                env=self._env(),
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            raise AIUnavailable("Claude CLI binary '%s' not found on PATH." % self.settings.claude_bin)
        except subprocess.TimeoutExpired as e:
            raise AITimeout("Claude CLI timed out after %ss." % self.settings.ai_timeout_s, raw=str(e.stdout or "")[:2000])
        duration_ms = int((time.time() - started) * 1000)
        return self._parse(proc, schema, model, duration_ms)

    def health(self) -> Dict[str, Any]:
        """Cheap check: CLI present + `claude auth status` (no model call)."""
        path = shutil.which(self.settings.claude_bin)
        if not path:
            return {"available": False, "authenticated": False,
                    "detail": "Claude CLI '%s' not found on PATH." % self.settings.claude_bin}
        try:
            proc = self._run([self.settings.claude_bin, "auth", "status"], capture_output=True, text=True,
                             timeout=20, env=self._env(), stdin=subprocess.DEVNULL)
            info = json.loads(proc.stdout or "{}")
            logged = bool(info.get("loggedIn"))
            # Deliberately omit the account email: it is noise in the UI and gets auto-linkified.
            detail = (info.get("subscriptionType") or info.get("authMethod") or "") if logged else ""
            return {"available": True, "authenticated": logged, "detail": str(detail).strip(), "info": info}
        except Exception as e:  # pragma: no cover - defensive
            return {"available": True, "authenticated": False, "detail": "Could not read auth status: %s" % e}

    # -- internals ----------------------------------------------------------
    def _argv(self, prompt: str, schema: Dict[str, Any], system_prompt: str, model: str) -> List[str]:
        argv = [
            self.settings.claude_bin, "-p", prompt,
            "--system-prompt", system_prompt,
            "--output-format", "json",
            "--json-schema", json.dumps(schema, separators=(",", ":")),
            "--model", model,
            "--max-turns", str(self.max_turns),
            "--tools", "",
            "--no-session-persistence",
        ]
        if self.effort and self.effort != "default":
            argv += ["--effort", self.effort]
        return argv

    @staticmethod
    def _env() -> Dict[str, str]:
        return {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}

    def _parse(self, proc: "subprocess.CompletedProcess[str]", schema: Dict[str, Any], model: str, duration_ms: int) -> AIResult:
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        raw = stdout if stdout.strip() else stderr
        combined_lower = (stdout + "\n" + stderr).lower()

        try:
            envelope = json.loads(stdout)
        except ValueError:
            if any(m in combined_lower for m in _AUTH_MARKERS):
                raise AINotAuthenticated(raw=raw[:4000])
            if proc.returncode != 0 and not stdout.strip():
                raise AIUnavailable("Claude CLI exited with code %s: %s" % (proc.returncode, stderr.strip()[:300]), raw=raw[:4000])
            raise AIInvalidOutput("Claude CLI did not return JSON.", raw=raw[:4000])

        if not isinstance(envelope, dict):
            raise AIInvalidOutput("Unexpected CLI envelope.", raw=raw[:4000])

        result_text = str(envelope.get("result") or "")
        if envelope.get("is_error"):
            errors = envelope.get("errors") or []
            joined = (" ".join(str(e) for e in errors) + " " + result_text).lower()
            if any(m in joined for m in _AUTH_MARKERS):
                raise AINotAuthenticated(raw=raw[:4000])
            if any(m in joined for m in _LIMIT_MARKERS):
                raise AIUnavailable("Claude is rate-limited or at its usage limit right now. Try again shortly.", raw=raw[:4000])
            if any(m in joined for m in _TRANSIENT_MARKERS):
                raise AITransient("The Claude connection dropped mid-response: %s" % joined.strip()[:200], raw=raw[:4000])
            raise AIInvalidOutput("Claude CLI reported an error: %s" % (joined.strip()[:300] or "unknown"), raw=raw[:4000])

        data = envelope.get("structured_output")
        if data is None and result_text.strip():
            try:
                data = json.loads(result_text)
            except ValueError:
                data = None
        if data is None:
            raise AIInvalidOutput("No structured output in CLI response.", raw=raw[:4000])

        err = validate_against(schema, data)
        if err:
            raise AIInvalidOutput("Structured output failed schema validation: %s" % err, raw=raw[:4000])
        return AIResult(data=data, raw=raw, provider=self.name, model=str(
            next(iter((envelope.get("modelUsage") or {}).keys()), model)), duration_ms=duration_ms)


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #
def get_ai_service(settings: Settings) -> AIService:
    """Phase 1 has exactly one real provider. The abstraction stays for later."""
    return ClaudeCLIProvider(settings)
