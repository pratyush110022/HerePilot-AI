"""Client-safe application errors.

Everything the API returns to a browser, and everything placed into an LLM prompt, must be
produced from these types. Provider bodies, exception text, URLs and credentials never
cross this boundary: services translate provider failures into an ``AppError`` with a
stable ``code`` and a fixed, human-readable ``message``; the diagnostic detail goes to the
(redacting) log instead.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ErrorSpec:
    status: int
    message: str
    retryable: bool


# code -> HTTP status, fixed client message, retryable?
ERROR_SPECS: dict[str, ErrorSpec] = {
    "invalid_request": ErrorSpec(400, "I couldn't understand that request. Please rephrase it.", False),
    "place_not_found": ErrorSpec(404, "I couldn't find that place. Try adding a city or state.", False),
    "place_ambiguous": ErrorSpec(409, "That place name matches more than one location. Please say which one you mean.", False),
    "no_route": ErrorSpec(422, "I couldn't find a drivable route between those places.", False),
    "needs_trip": ErrorSpec(409, "Plan a trip first by telling me where you are starting and where you are going.", False),
    "reference_unresolved": ErrorSpec(409, "I couldn't tell which place you mean. Please name it.", False),
    "needs_location": ErrorSpec(409, "I need your location for that. Tap the location icon on the map to share it, or tell me a place to search around.", False),
    "needs_vehicle": ErrorSpec(409, "EV planning needs your vehicle's range and current battery level.", False),
    "provider_unavailable": ErrorSpec(502, "A map service is temporarily unavailable. Please try again in a moment.", True),
    "provider_rate_limited": ErrorSpec(429, "The map service is busy right now. Please try again in a moment.", True),
    "provider_not_configured": ErrorSpec(503, "A required map service isn't configured on the server.", False),
    "ai_unavailable": ErrorSpec(502, "The AI service is temporarily unavailable.", True),
    "internal_error": ErrorSpec(500, "Something went wrong on our side. Please try again.", True),
}


class AppError(Exception):
    """An expected failure with a stable code and a message that is safe to show a user.

    ``detail`` is for logs only (it is redacted by the log formatter) and is never serialised
    into API responses. ``data`` may carry small, safe, structured hints for the UI, such as
    candidate place names for an ambiguity.
    """

    def __init__(self, code: str, message: str | None = None, *, detail: str | None = None,
                 data: dict[str, Any] | None = None) -> None:
        spec = ERROR_SPECS.get(code, ERROR_SPECS["internal_error"])
        self.code = code if code in ERROR_SPECS else "internal_error"
        self.spec = spec
        self.message = message or spec.message
        self.detail = detail
        self.data = data or {}
        super().__init__(self.message)

    @property
    def status(self) -> int:
        return self.spec.status

    @property
    def retryable(self) -> bool:
        return self.spec.retryable

    def to_payload(self, request_id: str | None = None) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message, "retryable": self.retryable}
        if self.data:
            error["data"] = self.data
        if request_id:
            error["request_id"] = request_id
        # ``detail`` mirrors the message so older callers that only read ``detail`` keep working.
        return {"detail": self.message, "error": error}


def internal_error_payload(request_id: str | None = None) -> dict[str, Any]:
    return AppError("internal_error").to_payload(request_id)


def provider_error_from_status(status_code: int, *, provider: str) -> AppError:
    """Map an upstream HTTP status to a safe error. The response body is deliberately ignored."""
    if status_code in (401, 403):
        return AppError("provider_not_configured", detail=f"{provider} rejected credentials (HTTP {status_code})")
    if status_code == 429:
        return AppError("provider_rate_limited", detail=f"{provider} rate limited (HTTP 429)")
    return AppError("provider_unavailable", detail=f"{provider} returned HTTP {status_code}")
