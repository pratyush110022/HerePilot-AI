"""Provider-neutral LLM interface.

The orchestrator talks only to :class:`LLMProvider`. Concrete providers (currently AWS
Bedrock) translate their SDK's failures into the :class:`LLMError` hierarchy below, so no
provider response body or exception text ever reaches a client or a prompt.
"""
from __future__ import annotations

import json
import re
from typing import Any, Protocol, runtime_checkable


class LLMError(Exception):
    """Base class. ``code`` is a short, stable, log-safe identifier."""

    code = "llm_error"
    retryable = False

    def __init__(self, detail: str = "") -> None:
        self.detail = detail  # diagnostic only: logged through the redacting formatter, never returned
        super().__init__(self.code)


class LLMNotConfigured(LLMError):
    code = "not_configured"


class LLMAuthError(LLMError):
    code = "auth_failed"


class LLMAccessDenied(LLMError):
    code = "access_denied"


class LLMThrottled(LLMError):
    code = "throttled"
    retryable = True


class LLMTimeout(LLMError):
    code = "timeout"
    retryable = True


class LLMModelUnavailable(LLMError):
    code = "model_unavailable"
    retryable = True


class LLMBadRequest(LLMError):
    code = "bad_request"


class LLMBadOutput(LLMError):
    code = "bad_output"


@runtime_checkable
class LLMProvider(Protocol):
    """Minimal text-in/text-out contract. Structure is enforced above this layer."""

    name: str
    model_id: str | None

    def status(self) -> dict[str, Any]:
        """Non-secret status for health checks and startup logs."""

    @property
    def available(self) -> bool:
        """True when a call could be attempted (configured), not when it is guaranteed to succeed."""

    async def complete(self, *, system: str, user: str, max_tokens: int = 600, temperature: float = 0.0) -> str:
        """Return the model's text. Raise an :class:`LLMError` subclass on any failure."""


class NullProvider:
    """Used when no LLM is configured. The app then relies on deterministic parsing/replies."""

    name = "none"
    model_id = None

    def __init__(self, reason: str = "disabled") -> None:
        self.reason = reason

    def status(self) -> dict[str, Any]:
        return {"provider": self.name, "configured": False, "reason": self.reason}

    @property
    def available(self) -> bool:
        return False

    async def complete(self, *, system: str, user: str, max_tokens: int = 600, temperature: float = 0.0) -> str:
        raise LLMNotConfigured(self.reason)


_FENCE_START = re.compile(r"^```(?:json)?\s*", re.IGNORECASE)
_FENCE_END = re.compile(r"\s*```$")


def extract_json_object(text: str) -> dict[str, Any]:
    """Parse the first JSON object in ``text``. Raises :class:`LLMBadOutput` if there isn't one."""
    if not text or not text.strip():
        raise LLMBadOutput("empty response")
    cleaned = _FENCE_END.sub("", _FENCE_START.sub("", text.strip()))
    try:
        value = json.loads(cleaned)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    start = cleaned.find("{")
    if start == -1:
        raise LLMBadOutput("no JSON object found")
    decoder = json.JSONDecoder()
    try:
        value, _ = decoder.raw_decode(cleaned[start:])
    except json.JSONDecodeError as exc:
        raise LLMBadOutput("invalid JSON") from exc
    if not isinstance(value, dict):
        raise LLMBadOutput("JSON value is not an object")
    return value


async def complete_json(provider: LLMProvider, *, system: str, user: str, max_tokens: int = 500) -> dict[str, Any]:
    """Ask for a JSON object and parse it. Retries are left to the SDK; malformed output is an error."""
    text = await provider.complete(system=system, user=user, max_tokens=max_tokens, temperature=0.0)
    return extract_json_object(text)
