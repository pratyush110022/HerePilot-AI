"""Backward-compatible import path. The parser now lives in :mod:`services.intent_parser`."""
from __future__ import annotations

from services.intent_parser import (  # noqa: F401
    PLACE_SPLIT_RE,
    _number,
    _vehicle_update,
    extract_places,
    parse,
    reference_in,
)
