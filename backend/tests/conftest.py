"""Shared test fixtures. No test may reach a real provider: the network guard and these fakes enforce that."""
from __future__ import annotations

import os

# Tests must never read the developer's real backend/.env (python-dotenv honours this switch).
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
# HERE photos and ratings are an optional extra request parameter; tests that are about something else keep it off so
# their call counts and parameters stay simple. Tests of that feature switch it on explicitly.
os.environ["HERE_RICH_PLACES"] = "0"
# Weather lookups are an extra, optional call on many results. Tests keep them off so no test touches the network for it.
os.environ["HERE_WEATHER"] = "0"

import json
from typing import Any, Callable

import pytest

from services import llm
from services.llm.provider import LLMError, NullProvider


class FakeProvider:
    """Scripted LLM. ``responder(system, user)`` returns text, or raises an LLMError to simulate a failure."""

    name = "fake"
    model_id = "fake-model"

    def __init__(self, responder: Callable[[str, str], str] | None = None, available: bool = True) -> None:
        self.responder = responder or (lambda system, user: "{}")
        self._available = available
        self.calls: list[dict[str, str]] = []

    @property
    def available(self) -> bool:
        return self._available

    def status(self) -> dict[str, Any]:
        return {"provider": self.name, "configured": self._available}

    async def complete(self, *, system: str, user: str, max_tokens: int = 600, temperature: float = 0.0) -> str:
        self.calls.append({"system": system, "user": user})
        result = self.responder(system, user)
        if isinstance(result, LLMError):
            raise result
        return result


def json_responder(intent: dict[str, Any], answer: str = "ok") -> Callable[[str, str], str]:
    """Return ``intent`` as JSON for the intent prompt and ``answer`` for answer prompts."""
    def respond(system: str, user: str) -> str:
        return json.dumps(intent) if "intent parser" in system else answer
    return respond


@pytest.fixture(autouse=True)
def no_llm_by_default():
    """Every test starts with AI disabled. Tests that need a model install a FakeProvider."""
    llm.set_provider(NullProvider("tests"))
    yield
    llm.set_provider(None)


@pytest.fixture
def fake_llm():
    def install(responder: Callable[[str, str], str] | None = None, available: bool = True) -> FakeProvider:
        provider = FakeProvider(responder, available)
        llm.set_provider(provider)
        return provider
    return install
