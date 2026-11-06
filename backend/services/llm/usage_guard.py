"""Spend guard for the Bedrock provider: caps how many model calls the process will make.

Every call to the model costs money, and a public deployment cannot control how often it is asked. This
guard counts calls and refuses new ones once a limit is reached. Callers already treat any model failure as
"use the built-in rules", so hitting a limit degrades the assistant to rules mode; nothing breaks, and the
cap lifts by itself (the hourly window slides, the daily count resets at 00:00 UTC).

Configuration (environment variables; ``0`` switches that limit off):

``BEDROCK_HOURLY_CALL_LIMIT``  Model calls allowed in any rolling 60 minutes. Default 100.
``BEDROCK_DAILY_CALL_LIMIT``   Model calls allowed per UTC day. Default 600.
``BEDROCK_USAGE_STATE_FILE``   Optional path. When set, the daily count survives restarts.

One chat message can use 1 to 3 model calls (understand, answer, optional travel guide).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Callable

logger = logging.getLogger(__name__)

DEFAULT_HOURLY_LIMIT = 100
DEFAULT_DAILY_LIMIT = 600
_HOUR_SECONDS = 3600.0


def _env_limit(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


class UsageGuard:
    """Thread-safe call counter with an hourly (rolling) and a daily (UTC) limit."""

    def __init__(self, *, hourly_limit: int | None = None, daily_limit: int | None = None,
                 state_file: str | None = None, clock: Callable[[], float] = time.time) -> None:
        self.hourly_limit = _env_limit("BEDROCK_HOURLY_CALL_LIMIT", DEFAULT_HOURLY_LIMIT) if hourly_limit is None else hourly_limit
        self.daily_limit = _env_limit("BEDROCK_DAILY_CALL_LIMIT", DEFAULT_DAILY_LIMIT) if daily_limit is None else daily_limit
        self._state_file = (os.getenv("BEDROCK_USAGE_STATE_FILE", "").strip() or None) if state_file is None else (state_file or None)
        self._clock = clock
        self._lock = threading.Lock()
        self._recent: deque[float] = deque()   # start times of calls made in the last hour
        self._day = self._utc_day()
        self._day_calls = 0
        self._announced: str | None = None     # which limit was last logged, so a cap logs once, not per request
        self._save_failed = False
        self._load()

    # ---- public --------------------------------------------------------------------
    def blocked(self) -> str | None:
        """``"hourly"``, ``"daily"`` or ``None``. Counts nothing."""
        with self._lock:
            self._roll(self._clock())
            return self._limit_hit()

    def acquire(self) -> str | None:
        """Count one call. ``None`` means go ahead; otherwise the limit that stopped it (and nothing is counted)."""
        with self._lock:
            now = self._clock()
            self._roll(now)
            hit = self._limit_hit()
            if hit:
                return hit
            self._recent.append(now)
            self._day_calls += 1
            self._save()
            return None

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            self._roll(self._clock())
            return {"hour_calls": len(self._recent), "day_calls": self._day_calls,
                    "hourly_limit": self.hourly_limit, "daily_limit": self.daily_limit}

    # ---- internals (lock held) -----------------------------------------------------
    def _utc_day(self) -> str:
        return datetime.fromtimestamp(self._clock(), timezone.utc).strftime("%Y-%m-%d")

    def _roll(self, now: float) -> None:
        today = self._utc_day()
        if today != self._day:
            self._day, self._day_calls = today, 0
            self._save()
        while self._recent and self._recent[0] <= now - _HOUR_SECONDS:
            self._recent.popleft()

    def _limit_hit(self) -> str | None:
        hit = None
        if self.daily_limit and self._day_calls >= self.daily_limit:
            hit = "daily"
        elif self.hourly_limit and len(self._recent) >= self.hourly_limit:
            hit = "hourly"
        if hit != self._announced:
            if hit:
                logger.warning("event=llm_usage_cap limit=%s hour_calls=%s day_calls=%s hourly_limit=%s daily_limit=%s "
                               "action=using_rules_until_it_lifts", hit, len(self._recent), self._day_calls,
                               self.hourly_limit, self.daily_limit)
            else:
                logger.info("event=llm_usage_cap_lifted")
            self._announced = hit
        return hit

    def _load(self) -> None:
        if not self._state_file:
            return
        try:
            with open(self._state_file, encoding="utf-8") as handle:
                data = json.load(handle)
            if data.get("day") == self._day:
                self._day_calls = max(0, int(data.get("calls", 0)))
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, AttributeError):
            logger.warning("event=llm_usage_state_unreadable action=starting_count_from_zero")

    def _save(self) -> None:
        if not self._state_file:
            return
        tmp = f"{self._state_file}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump({"day": self._day, "calls": self._day_calls}, handle)
            os.replace(tmp, self._state_file)
            self._save_failed = False
        except OSError:
            if not self._save_failed:   # warn once; the in-memory count keeps working
                logger.warning("event=llm_usage_state_unwritable action=count_kept_in_memory_only")
            self._save_failed = True
