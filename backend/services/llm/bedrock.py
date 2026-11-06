"""AWS Bedrock provider using the Converse API (boto3 ``bedrock-runtime``).

Configuration (environment variables or standard AWS configuration; nothing is hard-coded):

``BEDROCK_MODEL_ID``       Required. A model ID or inference-profile ID you have been granted
                           access to. There is deliberately no default: choosing a billable model
                           is the operator's decision.
``BEDROCK_REGION``         Optional. Falls back to ``AWS_REGION``/``AWS_DEFAULT_REGION`` and then
                           to the region of the active AWS profile.
``AWS_PROFILE``            Optional. Standard AWS profile selection. This module never picks a
                           profile for you; with no profile or credentials set it reports
                           ``not_configured`` instead of guessing.
``BEDROCK_TIMEOUT_SECONDS`` Optional read timeout, default 30.

Authentication uses the normal AWS credential chain (IAM role, SSO/login session, shared
profile, environment). Corporate TLS inspection is handled by the standard ``AWS_CA_BUNDLE``
or the profile's ``ca_bundle``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from typing import Any

from services.llm.usage_guard import UsageGuard
from services.llm.provider import (
    LLMAccessDenied,
    LLMAuthError,
    LLMBadOutput,
    LLMBadRequest,
    LLMError,
    LLMModelUnavailable,
    LLMNotConfigured,
    LLMThrottled,
    LLMTimeout,
)

logger = logging.getLogger(__name__)

_AUTH_CODES = {
    "UnrecognizedClientException", "InvalidSignatureException", "ExpiredTokenException",
    "InvalidClientTokenId", "SignatureDoesNotMatch", "RequestExpired", "ExpiredToken",
}
_DENIED_CODES = {"AccessDeniedException", "AccessDenied", "UnauthorizedOperation"}
_THROTTLE_CODES = {"ThrottlingException", "TooManyRequestsException", "ServiceQuotaExceededException", "Throttling"}
_TIMEOUT_CODES = {"ModelTimeoutException", "RequestTimeout", "RequestTimeoutException"}
_UNAVAILABLE_CODES = {
    "ModelNotReadyException", "ServiceUnavailableException", "InternalServerException",
    "ModelErrorException", "ResourceNotFoundException", "ModelStreamErrorException",
}
_VALIDATION_CODES = {"ValidationException"}

# After a failed client build (expired login session, missing credentials) the provider reports itself unavailable
# for this long, then tries again, so signing in again fixes it without restarting the backend.
_BUILD_RETRY_SECONDS = 30.0


def translate_exception(exc: BaseException) -> LLMError:
    """Map a botocore/boto3 exception to a closed set of safe categories.

    The exception *message* is intentionally not copied: AWS messages can include account IDs,
    ARNs and request IDs. Only the error code is kept for diagnostics.
    """
    if isinstance(exc, LLMError):
        return exc
    try:
        from botocore import exceptions as bce
    except Exception:  # pragma: no cover - boto3 absent
        return LLMError(type(exc).__name__)

    if isinstance(exc, (bce.NoCredentialsError, bce.PartialCredentialsError, bce.ProfileNotFound,
                        bce.NoRegionError, bce.CredentialRetrievalError, bce.MissingDependencyException)):
        return LLMNotConfigured(type(exc).__name__)
    if isinstance(exc, bce.ClientError):
        code = str((getattr(exc, "response", {}) or {}).get("Error", {}).get("Code", ""))
        if code in _AUTH_CODES:
            return LLMAuthError(code)
        if code in _DENIED_CODES:
            return LLMAccessDenied(code)
        if code in _THROTTLE_CODES:
            return LLMThrottled(code)
        if code in _TIMEOUT_CODES:
            return LLMTimeout(code)
        if code in _UNAVAILABLE_CODES:
            return LLMModelUnavailable(code)
        if code in _VALIDATION_CODES:
            return LLMBadRequest(code)
        return LLMError(code or "ClientError")
    if isinstance(exc, (bce.ConnectTimeoutError, bce.ReadTimeoutError)):
        return LLMTimeout(type(exc).__name__)
    if isinstance(exc, (bce.EndpointConnectionError, bce.ConnectionClosedError, bce.HTTPClientError)):
        return LLMModelUnavailable(type(exc).__name__)
    if isinstance(exc, bce.BotoCoreError):
        return LLMError(type(exc).__name__)
    return LLMError(type(exc).__name__)


class BedrockProvider:
    name = "bedrock"

    def __init__(self, *, model_id: str | None = None, region: str | None = None,
                 client: Any = None, timeout_seconds: float | None = None,
                 usage_guard: UsageGuard | None = None) -> None:
        self.model_id = (model_id if model_id is not None else os.getenv("BEDROCK_MODEL_ID", "")).strip() or None
        self._region_override = (region or os.getenv("BEDROCK_REGION", "")).strip() or None
        self._timeout = timeout_seconds if timeout_seconds is not None else _env_float("BEDROCK_TIMEOUT_SECONDS", 30.0)
        self._client = client  # tests inject a fake; production builds lazily
        self._client_lock = threading.Lock()
        self._build_error: LLMError | None = None
        self._build_error_at = 0.0
        # Caps the number of billable calls (see usage_guard.py). Tests inject their own; production reads env limits.
        self._usage = usage_guard if usage_guard is not None else UsageGuard()

    # ---- configuration -------------------------------------------------------------
    def _recent_build_error(self) -> LLMError | None:
        if self._build_error is not None and time.monotonic() - self._build_error_at < _BUILD_RETRY_SECONDS:
            return self._build_error
        return None

    @property
    def available(self) -> bool:
        # While a usage cap is active the provider reports itself unavailable, so callers skip the model and use the rules.
        return (self.model_id is not None and self._recent_build_error() is None and self._boto3_present()
                and self._usage.blocked() is None)

    @staticmethod
    def _boto3_present() -> bool:
        try:
            import boto3  # noqa: F401
            return True
        except Exception:
            return False

    def status(self) -> dict[str, Any]:
        reason = None
        if not self.model_id:
            reason = "BEDROCK_MODEL_ID is not set"
        elif not self._boto3_present():
            reason = "boto3 is not installed"
        elif self._recent_build_error() is not None:
            reason = f"client error: {self._build_error.code}"
        elif (capped := self._usage.blocked()) is not None:
            reason = f"usage cap reached ({capped}); using built-in rules until it lifts"
        return {
            "provider": self.name,
            "configured": reason is None,
            "model_id_set": bool(self.model_id),
            "region": self._region_override or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "profile-default",
            "profile_selected": bool(os.getenv("AWS_PROFILE")),
            "reason": reason,
        }

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        with self._client_lock:
            if self._client is not None:
                return self._client
            try:
                import boto3
                from botocore.config import Config

                session = boto3.Session()  # honours AWS_PROFILE / env credentials / SSO / login sessions
                region = (
                    self._region_override
                    or os.getenv("AWS_REGION")
                    or os.getenv("AWS_DEFAULT_REGION")
                    or session.region_name
                )
                if not region:
                    raise LLMNotConfigured("no AWS region configured")
                if session.get_credentials() is None:
                    raise LLMNotConfigured("no AWS credentials found")
                config = Config(
                    region_name=region,
                    connect_timeout=5,
                    read_timeout=self._timeout,
                    retries={"max_attempts": 2, "mode": "standard"},
                    user_agent_extra="herepilot-ai",
                )
                self._client = session.client("bedrock-runtime", config=config)
                self._build_error = None
            except LLMError as exc:
                self._build_error, self._build_error_at = exc, time.monotonic()
                raise
            except Exception as exc:
                translated = translate_exception(exc)
                self._build_error, self._build_error_at = translated, time.monotonic()
                raise translated from exc
            return self._client

    # ---- inference -----------------------------------------------------------------
    def _converse(self, system: str, user: str, max_tokens: int, temperature: float) -> str:
        if not self.model_id:
            raise LLMNotConfigured("BEDROCK_MODEL_ID is not set")
        client = self._get_client()
        capped = self._usage.acquire()   # counted right before the billable call; nothing is counted when refused
        if capped:
            raise LLMThrottled(f"usage cap reached ({capped})")
        try:
            response = client.converse(
                modelId=self.model_id,
                system=[{"text": system}],
                messages=[{"role": "user", "content": [{"text": user}]}],
                inferenceConfig={"maxTokens": int(max_tokens), "temperature": float(temperature)},
            )
        except Exception as exc:
            raise translate_exception(exc) from exc
        return self._extract_text(response)

    @staticmethod
    def _extract_text(response: Any) -> str:
        try:
            blocks = response["output"]["message"]["content"]
        except (KeyError, TypeError) as exc:
            raise LLMBadOutput("unexpected response shape") from exc
        text = "".join(block["text"] for block in blocks if isinstance(block, dict) and isinstance(block.get("text"), str))
        if not text.strip():
            raise LLMBadOutput("no text content")
        if response.get("stopReason") == "max_tokens":
            # A truncated JSON object or sentence is worse than no answer.
            raise LLMBadOutput("output truncated at max_tokens")
        return text

    async def complete(self, *, system: str, user: str, max_tokens: int = 600, temperature: float = 0.0) -> str:
        # boto3 is synchronous; keep the event loop free while waiting on the network.
        return await asyncio.to_thread(self._converse, system, user, max_tokens, temperature)


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, ""))
        return value if value > 0 else default
    except ValueError:
        return default
