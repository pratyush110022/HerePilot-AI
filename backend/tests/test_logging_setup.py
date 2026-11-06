from __future__ import annotations

import io
import logging

import httpx
import pytest

import services.orchestrator as orchestrator
from services.logging_setup import (
    HANDLER_FLAG,
    AccessLogFilter,
    configure_logging,
    describe_exception,
    redact,
    safe_token,
    scrub_request_path,
    short_id,
    url_host,
)
from services.trip_context import PlaceContext, TripContext

# Fake credentials and user content. None of these may ever reach a log line.
HERE_SENTINEL = "SENTINEL-HERE-KEY-0123456789"
GROQ_SENTINEL = "gsk_SENTINELGROQKEY0123456789abcdef"
BEARER_SENTINEL = "SENTINEL.BEARER.TOKEN-abcdef123456"
USER_SENTINEL = "SentinelTown"
ALL_SECRETS = (HERE_SENTINEL, GROQ_SENTINEL, BEARER_SENTINEL)


@pytest.fixture
def log_buffer():
    """Send a fresh redacting handler to a buffer, then restore the root logger exactly."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_levels = {name: logging.getLogger(name).level for name in ("httpx", "httpcore")}
    access_logger = logging.getLogger("uvicorn.access")
    saved_access_filters = list(access_logger.filters)
    for handler in saved_handlers:
        if getattr(handler, HANDLER_FLAG, False):
            root.removeHandler(handler)
    buffer = io.StringIO()
    configure_logging(level="DEBUG", stream=buffer)
    yield buffer
    access_logger.filters[:] = saved_access_filters
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)
    for name, level in saved_levels.items():
        logging.getLogger(name).setLevel(level)


class _StatusError(Exception):
    status_code = 429


def test_redact_strips_url_query_string_including_api_key():
    text = f"GET https://router.hereapi.com/v8/routes?origin=1,2&apiKey={HERE_SENTINEL} failed"
    result = redact(text)
    assert HERE_SENTINEL not in result
    assert "origin=1,2" not in result
    assert "https://router.hereapi.com/v8/routes" in result


@pytest.mark.parametrize(
    "text",
    [
        f"apiKey={HERE_SENTINEL}",
        f"api_key: {HERE_SENTINEL}",
        f'{{"apiKey": "{HERE_SENTINEL}"}}',
        f"params={{'apikey': '{HERE_SENTINEL}'}}",
        f"access_token={BEARER_SENTINEL}",
    ],
)
def test_redact_masks_key_value_forms(text):
    result = redact(text)
    assert HERE_SENTINEL not in result
    assert BEARER_SENTINEL not in result
    assert "[REDACTED]" in result


def test_redact_masks_bearer_tokens_and_authorization_headers():
    assert BEARER_SENTINEL not in redact(f"Authorization: Bearer {BEARER_SENTINEL}")
    assert BEARER_SENTINEL not in redact(f"upstream sent bearer {BEARER_SENTINEL} back")
    assert BEARER_SENTINEL not in redact(f"Proxy-Authorization: Basic {BEARER_SENTINEL}")


def test_redact_masks_groq_key_pattern():
    assert GROQ_SENTINEL not in redact(f"model call rejected for {GROQ_SENTINEL}")


def test_redact_masks_current_secret_environment_values_anywhere(monkeypatch):
    monkeypatch.setenv("HERE_API_KEY", "plain-secret-value-123456")
    assert "plain-secret-value-123456" not in redact("the value plain-secret-value-123456 leaked")


def test_redact_ignores_empty_and_short_environment_values(monkeypatch):
    monkeypatch.setenv("HERE_API_KEY", "")
    monkeypatch.setenv("GROQ_API_KEY", "short")
    assert redact("a short message") == "a short message"


def test_redact_leaves_ordinary_text_alone():
    text = "event=turn intent=find_pois max_tokens=450 tokens used"
    assert redact(text) == text


def test_message_and_traceback_are_redacted(log_buffer):
    logger = logging.getLogger("test.redaction")
    try:
        raise RuntimeError(f"upstream said https://x.example/v1?apiKey={HERE_SENTINEL} Bearer {BEARER_SENTINEL}")
    except RuntimeError:
        logger.exception("event=failure token=%s", GROQ_SENTINEL)
    output = log_buffer.getvalue()
    assert "Traceback" in output and "RuntimeError" in output
    for secret in ALL_SECRETS:
        assert secret not in output


def test_describe_exception_omits_message_by_default_but_keeps_status():
    assert describe_exception(_StatusError(f"user typed {USER_SENTINEL}")) == "_StatusError http_status=429"


def test_describe_exception_redacts_then_truncates_message():
    exc = RuntimeError(f"bad https://h.example/p?apiKey={HERE_SENTINEL}" + " filler" * 100)
    result = describe_exception(exc, include_message=True, limit=60)
    assert HERE_SENTINEL not in result
    assert result.startswith("RuntimeError: ")
    assert len(result) <= len("RuntimeError: ") + 60


def test_describe_exception_never_raises():
    class Hostile(Exception):
        def __str__(self):
            raise ValueError("cannot stringify")

    assert describe_exception(Hostile(), include_message=True) == "UnknownError"


def test_short_id_never_returns_the_full_identifier():
    session_id = "0123456789abcdef0123456789abcdef"
    assert session_id not in short_id(session_id)
    assert short_id("ab") != "ab"
    assert short_id(None) == "none"


def test_safe_token_removes_log_injection_characters():
    assert safe_token("plan_trip\nFAKE LINE") == "plan_tripFAKELINE"
    assert safe_token(None) == "none"


def test_url_host_drops_path_query_and_credentials():
    assert url_host(f"https://user:pw@router.hereapi.com/v8/routes?apiKey={HERE_SENTINEL}") == "router.hereapi.com"


def test_configure_logging_is_idempotent_and_holds_http_loggers_at_warning(log_buffer):
    configure_logging(level="DEBUG")
    configure_logging(level="DEBUG")
    flagged = [h for h in logging.getLogger().handlers if getattr(h, HANDLER_FLAG, False)]
    assert len(flagged) == 1
    assert logging.getLogger("httpx").level >= logging.WARNING
    assert logging.getLogger("httpcore").level >= logging.WARNING


def test_log_level_environment_variable(log_buffer, monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "warning")
    configure_logging()
    assert logging.getLogger().level == logging.WARNING
    monkeypatch.setenv("LOG_LEVEL", "LOUD")
    configure_logging()
    assert logging.getLogger().level == logging.INFO


def _get_with_api_key():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    with httpx.Client(transport=transport) as client:
        client.get("https://router.hereapi.com/v8/routes", params={"apiKey": HERE_SENTINEL})


def test_httpx_request_logging_is_suppressed_so_urls_are_not_logged(log_buffer):
    _get_with_api_key()
    output = log_buffer.getvalue()
    assert HERE_SENTINEL not in output
    assert "HTTP Request" not in output


def test_formatter_still_redacts_if_httpx_logging_is_raised_to_info(log_buffer):
    logging.getLogger("httpx").setLevel(logging.INFO)
    _get_with_api_key()
    output = log_buffer.getvalue()
    assert "HTTP Request" in output  # the line is emitted ...
    assert HERE_SENTINEL not in output  # ... but the key never reaches the log


@pytest.mark.asyncio
async def test_handle_message_logs_events_without_user_text_or_secrets(log_buffer, monkeypatch, fake_llm):
    import services.mcp_tools as tools
    from services.llm.provider import LLMBadRequest

    # The model fails with a message that carries secrets and user text.
    fake_llm(lambda system, user: LLMBadRequest(f"Groq rejected key {GROQ_SENTINEL} Bearer {BEARER_SENTINEL} text {USER_SENTINEL}"))

    async def fake_resolve_place(query: str, role: str = "stop"):
        return PlaceContext(USER_SENTINEL, f"{USER_SENTINEL}, India", 15.5, 73.8, role)

    leaky = f"HERE failed https://discover.search.hereapi.com/v1/discover?q={USER_SENTINEL}&apiKey={HERE_SENTINEL} Bearer {BEARER_SENTINEL}"

    async def failing_here(*_args, **_kwargs):
        raise RuntimeError(leaky)

    async def failing_osm(*_args, **_kwargs):
        raise RuntimeError(leaky)

    monkeypatch.setattr(orchestrator, "_resolve_place", fake_resolve_place)
    monkeypatch.setattr(tools, "find_nearby_places", failing_here)
    monkeypatch.setattr(tools, "find_osm_pois_near_points", failing_osm)

    message = f"Find 10 tourist places near {USER_SENTINEL} and show their name & distance"
    _, result, reply = await orchestrator.handle_message(TripContext(session_id="log-test-session"), message)

    output = log_buffer.getvalue()
    assert "event=llm_intent_failed" in output
    assert "event=here_search_failed" in output and "event=osm_fallback_failed" in output
    assert "event=turn intent=find_pois_near_location" in output
    assert f"message_chars={len(message)}" in output
    assert message not in output
    for secret in (*ALL_SECRETS, USER_SENTINEL):
        assert secret not in output

    # What the client sees is just as clean: a fixed reason code, never provider text.
    import json
    client_view = json.dumps({"result": result, "reply": reply}, default=str)
    for secret in (*ALL_SECRETS, "discover.search.hereapi.com"):
        assert secret not in client_view
    assert result["source_status"] == "unavailable"
    assert result["source_error"] == "HERE: provider_unavailable; OSM: provider_unavailable"


def test_traceback_keeps_frames_and_types_but_omits_exception_messages(log_buffer):
    logger = logging.getLogger("test.traceback")
    try:
        raise RuntimeError(f"could not route to {USER_SENTINEL}")
    except RuntimeError:
        logger.exception("event=failure")
    output = log_buffer.getvalue()
    assert USER_SENTINEL not in output
    assert "test_traceback_keeps_frames_and_types_but_omits_exception_messages" in output  # stack frame kept
    assert "RuntimeError: [message omitted]" in output


def test_chained_exception_messages_are_omitted_too(log_buffer):
    logger = logging.getLogger("test.chained")
    try:
        try:
            raise ValueError(f"inner mentions {USER_SENTINEL}")
        except ValueError as inner:
            raise RuntimeError(f"outer mentions {USER_SENTINEL}") from inner
    except RuntimeError:
        logger.exception("event=failure")
    output = log_buffer.getvalue()
    assert USER_SENTINEL not in output
    assert "ValueError: [message omitted]" in output and "RuntimeError: [message omitted]" in output


def test_describe_exception_reads_provider_status_from_our_error_text():
    exc = RuntimeError(f"HERE Routing returned 401: bad credentials for {USER_SENTINEL}")
    description = describe_exception(exc)
    assert description == "RuntimeError http_status=401"
    assert USER_SENTINEL not in description


FULL_SESSION_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"


def test_scrub_request_path_drops_query_and_shortens_session_id():
    assert scrub_request_path(f"/tata-chargers/search?q={USER_SENTINEL}&limit=1") == (
        "/tata-chargers/search?[QUERY REDACTED]"
    )
    scrubbed = scrub_request_path(f"/session/{FULL_SESSION_ID}")
    assert FULL_SESSION_ID not in scrubbed and scrubbed.startswith("/session/0f8fad5b")
    assert scrub_request_path("/health") == "/health"


def test_uvicorn_access_log_lines_are_scrubbed(log_buffer):
    access = logging.getLogger("uvicorn.access")
    access.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:5000", "GET", f"/session/{FULL_SESSION_ID}?q={USER_SENTINEL}", "1.1", 200)
    output = log_buffer.getvalue()
    assert "GET /session/0f8fad5b" in output and "200" in output  # still a useful access line
    assert FULL_SESSION_ID not in output
    assert USER_SENTINEL not in output


def test_access_log_filter_is_installed_once(log_buffer):
    configure_logging(level="DEBUG")
    configure_logging(level="DEBUG")
    installed = [f for f in logging.getLogger("uvicorn.access").filters if isinstance(f, AccessLogFilter)]
    assert len(installed) == 1
