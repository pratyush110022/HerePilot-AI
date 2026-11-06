"""Logging configuration and secret redaction for the backend.

Optional environment variable
-----------------------------
LOG_LEVEL
    Level for application logs: DEBUG, INFO, WARNING, ERROR or CRITICAL
    (case-insensitive). Defaults to INFO. Unrecognised values fall back to INFO.
    Example: ``LOG_LEVEL=DEBUG python -m uvicorn main:app``

Logging policy
--------------
* Never log API keys, bearer tokens, full session IDs, raw user messages or search
  queries. Call sites log event names, categories, counts and exception types.
* Every record written by the handler installed by :func:`configure_logging` passes
  through :func:`redact`, including formatted tracebacks. That is a safety net, not
  permission to hand secrets to the logger.
* Tracebacks keep stack frames and exception types but omit exception messages, which
  can echo user input (place names, search text). Provider-failure call sites opt in to
  a redacted, truncated message with ``describe_exception(..., include_message=True)``.
* uvicorn's access log lines (``"GET /path?query HTTP/1.1" 200``) are filtered so query
  strings and full session IDs in request paths are not written.
* ``httpx`` and ``httpcore`` are held at WARNING or above. At INFO, httpx logs the
  full request URL, and HERE requests carry the API key in the query string.
"""
from __future__ import annotations

import logging
import os
import re
import sys
import traceback
from typing import IO
from urllib.parse import urlsplit

REDACTED = "[REDACTED]"
QUERY_REDACTED = "?[QUERY REDACTED]"
DEFAULT_LEVEL = logging.INFO
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
HANDLER_FLAG = "_app_logging_handler"

# HTTP client loggers that print full request URLs (including query strings) at INFO.
_QUIET_LOGGERS = ("httpx", "httpcore")

# Environment variables whose *values* are scrubbed from every log line.
_SECRET_NAME_RE = re.compile(r"(?i)(api[_-]?key|token|secret|password)")
_MIN_SECRET_LENGTH = 8  # shorter values would corrupt ordinary text when replaced

_URL_QUERY_RE = re.compile(r"(https?://[^\s?#'\"<>]+)\?[^\s#'\"<>]*")
_AUTH_HEADER_RE = re.compile(r"(?i)\b(proxy-authorization|authorization|x-api-key)(\s*[:=]\s*)[^\r\n]+")
_BEARER_RE = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=\-]{6,}")
_GROQ_KEY_RE = re.compile(r"\bgsk_[A-Za-z0-9_\-]{8,}")
_KEY_VALUE_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|token|password|secret)"
    r"([\"']?\s*[=:]\s*[\"']?)[^\s&,;\"'<>]+"
)


# Our provider modules phrase failures as "HERE Routing returned 401: ..."; the status
# number is safe and useful, the rest of the text may not be.
_RETURNED_STATUS_RE = re.compile(r"\breturned\s+(\d{3})\b")


def _secret_values() -> list[str]:
    values = {
        value
        for name, value in os.environ.items()
        if len(value) >= _MIN_SECRET_LENGTH and _SECRET_NAME_RE.search(name)
    }
    return sorted(values, key=len, reverse=True)


def redact(text: object) -> str:
    """Return ``text`` with credentials and URL query strings removed."""
    value = text if isinstance(text, str) else str(text)
    for secret in _secret_values():
        value = value.replace(secret, REDACTED)
    value = _URL_QUERY_RE.sub(lambda match: match.group(1) + QUERY_REDACTED, value)
    value = _AUTH_HEADER_RE.sub(lambda match: match.group(1) + match.group(2) + REDACTED, value)
    value = _BEARER_RE.sub(lambda match: match.group(1) + " " + REDACTED, value)
    value = _GROQ_KEY_RE.sub(REDACTED, value)
    value = _KEY_VALUE_RE.sub(lambda match: match.group(1) + match.group(2) + REDACTED, value)
    return value


def _frames_and_types(exc: BaseException) -> str:
    """Render an exception chain as stack frames and exception types, without messages."""
    chunks: list[str] = []
    seen: set[int] = set()

    def walk(error: BaseException | None) -> None:
        if error is None or id(error) in seen:
            return
        seen.add(id(error))
        parent = error.__cause__ if error.__cause__ is not None else (
            None if error.__suppress_context__ else error.__context__
        )
        if parent is not None:
            walk(parent)
            chunks.append("--- the exception above led to ---\n")
        chunks.append("Traceback (most recent call last):\n")
        chunks.extend(traceback.format_list(traceback.extract_tb(error.__traceback__)))
        chunks.append(f"{type(error).__name__}: [message omitted]\n")

    walk(exc)
    return "".join(chunks).rstrip("\n")


class RedactingFormatter(logging.Formatter):
    """Formatter that redacts the final text and keeps exception messages out of tracebacks."""

    def formatException(self, ei) -> str:  # noqa: N802 (stdlib method name)
        try:
            return _frames_and_types(ei[1])
        except Exception:
            return "Traceback unavailable"

    def format(self, record: logging.LogRecord) -> str:
        # logging caches the formatted traceback on the record, and the first handler to
        # format it wins. Ignore any text another handler cached so a full traceback
        # (with messages) can never be reused here, and restore it for the other handlers.
        cached = record.exc_text
        record.exc_text = None
        try:
            return redact(super().format(record))
        finally:
            record.exc_text = cached


def describe_exception(exc: BaseException, *, include_message: bool = False, limit: int = 160) -> str:
    """Short, log-safe description of an exception. Never raises.

    The message is omitted unless ``include_message`` is true, because LLM and user-input
    errors can echo user text. When included it is redacted, collapsed to one line and
    truncated *after* redaction so a secret can never be cut in half and slip through.
    """
    try:
        description = type(exc).__name__
        status = getattr(exc, "status_code", None)
        if status is None:
            status = getattr(getattr(exc, "response", None), "status_code", None)
        if not isinstance(status, int):
            found = _RETURNED_STATUS_RE.search(str(exc))
            status = int(found.group(1)) if found else None
        if isinstance(status, int):
            description += f" http_status={status}"
        if include_message:
            message = " ".join(redact(str(exc)).split())
            if message:
                description += ": " + message[:limit]
        return description
    except Exception:
        return "UnknownError"


def safe_token(value: object, limit: int = 40) -> str:
    """Reduce a value to ``[A-Za-z0-9_.-]`` so it is safe as a single log field."""
    if value is None:
        return "none"
    try:
        cleaned = re.sub(r"[^A-Za-z0-9_.\-]", "", str(value))[:limit]
    except Exception:
        cleaned = ""
    return cleaned or "none"


def short_id(value: object) -> str:
    """Log-safe prefix of an identifier. Never returns more than half of it."""
    text = "" if value is None else str(value)
    if not text:
        return "none"
    return text[: min(8, max(len(text) // 2, 1))] + "..."


def url_host(url: object) -> str:
    """Hostname only: no path, query string or credentials."""
    try:
        return urlsplit(str(url)).hostname or "unknown"
    except Exception:
        return "unknown"


_SESSION_PATH_RE = re.compile(r"(/session/)([^/?#\s]+)")


def scrub_request_path(path: str) -> str:
    """Request path that is safe to log: query string removed, session ID shortened."""
    try:
        base, separator, _query = path.partition("?")
        base = _SESSION_PATH_RE.sub(lambda match: match.group(1) + short_id(match.group(2)), base)
        return base + (QUERY_REDACTED if separator else "")
    except Exception:
        return "[path omitted]"


class AccessLogFilter(logging.Filter):
    """Keeps query strings and full session IDs out of uvicorn's access-log lines.

    uvicorn writes ``"GET /path?query HTTP/1.1" 200`` through its own ``uvicorn.access``
    logger, which does not pass through the redacting handler above.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            record.args = (*args[:2], scrub_request_path(args[2]), *args[3:])
        return True


def _resolve_level(value: str | int | None) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        mapped = logging.getLevelName(value.strip().upper())
        if isinstance(mapped, int):
            return mapped
    return DEFAULT_LEVEL


def configure_logging(level: str | int | None = None, stream: IO[str] | None = None) -> logging.Handler:
    """Install (once) a redacting handler on the root logger and quiet HTTP clients.

    Safe to call repeatedly: an existing handler is reused. ``stream`` only applies when
    the handler is first created and defaults to stderr.
    """
    resolved = _resolve_level(level if level is not None else os.getenv("LOG_LEVEL"))
    root = logging.getLogger()
    handler = next((h for h in root.handlers if getattr(h, HANDLER_FLAG, False)), None)
    if handler is None:
        handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
        setattr(handler, HANDLER_FLAG, True)
        handler.setFormatter(RedactingFormatter(LOG_FORMAT))
        root.addHandler(handler)
    root.setLevel(resolved)
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(existing, AccessLogFilter) for existing in access_logger.filters):
        access_logger.addFilter(AccessLogFilter())
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(max(logging.WARNING, resolved))
    return handler
