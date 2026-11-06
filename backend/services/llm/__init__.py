"""LLM access for the assistant. Use :func:`get_provider`; do not import an SDK elsewhere."""
from __future__ import annotations

import os
import threading

from services.llm.provider import (
    LLMAccessDenied,
    LLMAuthError,
    LLMBadOutput,
    LLMBadRequest,
    LLMError,
    LLMModelUnavailable,
    LLMNotConfigured,
    LLMProvider,
    LLMThrottled,
    LLMTimeout,
    NullProvider,
    complete_json,
    extract_json_object,
)

__all__ = [
    "LLMAccessDenied", "LLMAuthError", "LLMBadOutput", "LLMBadRequest", "LLMError",
    "LLMModelUnavailable", "LLMNotConfigured", "LLMProvider", "LLMThrottled", "LLMTimeout",
    "NullProvider", "complete_json", "extract_json_object", "get_provider", "set_provider",
]

_provider: LLMProvider | None = None
_lock = threading.Lock()


def _build_provider() -> LLMProvider:
    choice = os.getenv("LLM_PROVIDER", "bedrock").strip().lower()
    if choice in {"none", "off", "disabled", ""}:
        return NullProvider("LLM_PROVIDER disables AI features")
    if choice == "bedrock":
        from services.llm.bedrock import BedrockProvider

        return BedrockProvider()
    return NullProvider(f"unsupported LLM_PROVIDER '{choice[:20]}'")


def get_provider() -> LLMProvider:
    """Process-wide provider, built lazily from the environment on first use."""
    global _provider
    if _provider is None:
        with _lock:
            if _provider is None:
                _provider = _build_provider()
    return _provider


def set_provider(provider: LLMProvider | None) -> None:
    """Install a provider (tests) or reset to environment-driven construction with ``None``."""
    global _provider
    with _lock:
        _provider = provider
