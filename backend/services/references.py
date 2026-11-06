"""Resolve conversational references against what the session actually contains.

A reference ("the second stop", "that station", "charging stop 4", "my destination", "the Taj hotel")
resolves to a concrete place with real coordinates taken from the active trip, the active EV plan
or the last displayed results. If nothing matches it raises ``reference_unresolved`` /
``needs_trip``; it never guesses and never turns a reference into a geocoder query.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from services.errors import AppError
from services.trip_context import TripContext


@dataclass
class ResolvedPlace:
    name: str
    latitude: float
    longitude: float
    kind: str           # stop | destination | origin | charging_stop | result | focus | named_result | named_stop | named_charger
    index: int | None   # 0-based position within its list, if any
    detail: dict[str, Any] | None = None

    def point(self) -> dict[str, float]:
        return {"latitude": self.latitude, "longitude": self.longitude}


def _coords(item: dict[str, Any]) -> tuple[float, float] | None:
    try:
        lat, lon = float(item["latitude"]), float(item["longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    return (lat, lon) if -90 <= lat <= 90 and -180 <= lon <= 180 else None


def charging_stops(context: TripContext) -> list[dict[str, Any]]:
    return list((context.ev_plan or {}).get("charging_stops") or [])


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def resolve_index(index: int | None, size: int, label: str) -> int:
    """0-based index (``-1`` means last) validated against ``size``."""
    if size <= 0:
        raise AppError("reference_unresolved", f"There are no {label}s yet.")
    if index is None:
        raise AppError("reference_unresolved", f"Which {label}? Give me its number, for example “{label} 2”.")
    resolved = size - 1 if index == -1 else index
    if resolved < 0 or resolved >= size:
        raise AppError("reference_unresolved", f"There {'is' if size == 1 else 'are'} only {size} {label}{'' if size == 1 else 's'}, so I can't use number {index + 1}.")
    return resolved


def resolve_scope(context: TripContext, scope: str, index: int | None) -> ResolvedPlace:
    """Resolve a (scope, index) reference to one place."""
    if scope in {"stop", "destination", "origin"}:
        if not context.stops:
            raise AppError("needs_trip")
        if scope == "destination":
            i = len(context.stops) - 1
        elif scope == "origin":
            i = 0
        else:
            i = resolve_index(index, len(context.stops), "stop")
        stop = context.stops[i]
        return ResolvedPlace(stop.name, stop.latitude, stop.longitude, scope if scope != "stop" else "stop", i)

    if scope == "charging_stop":
        stops = charging_stops(context)
        if not stops:
            if not context.ev_mode:
                raise AppError("reference_unresolved", "There are no charging stops because the EV Planner isn't active for this trip.")
            raise AppError("reference_unresolved", "There are no planned charging stops in the current EV plan.")
        i = resolve_index(index, len(stops), "charging stop")
        charger = stops[i].get("charger") or {}
        point = _coords(charger)
        if point is None:
            raise AppError("reference_unresolved", "That charging stop has no usable location.")
        return ResolvedPlace(str(charger.get("name") or charger.get("address") or f"Charging stop {i + 1}"), point[0], point[1], "charging_stop", i, charger)

    if scope == "result":
        pois = context.pois
        if not pois:
            raise AppError("reference_unresolved", "I don't have any search results on screen to refer to.")
        i = resolve_index(index, len(pois), "result")
        point = _coords(pois[i])
        if point is None:
            raise AppError("reference_unresolved", "That result has no usable location.")
        return ResolvedPlace(str(pois[i].get("name") or f"Result {i + 1}"), point[0], point[1], "result", i, pois[i])

    if scope == "focus":
        return resolve_focus(context)
    raise AppError("reference_unresolved")


def resolve_focus(context: TripContext) -> ResolvedPlace:
    """'That place / that station': the entity the conversation last centred on."""
    focus = context.focus
    if focus:
        point = _coords(focus)
        if point is not None:
            return ResolvedPlace(str(focus.get("name") or "that place"), point[0], point[1], str(focus.get("kind") or "focus"), focus.get("index"), focus.get("detail"))
    if len(context.pois) == 1:
        return resolve_scope(context, "result", 0)
    stops = charging_stops(context)
    if len(stops) == 1:
        return resolve_scope(context, "charging_stop", 0)
    raise AppError("reference_unresolved", "I'm not sure which place you mean. Name it or give its number from the list.")


def find_named_known_place(context: TripContext, text: str) -> ResolvedPlace | None:
    """Match ``text`` to a trip stop, planned charging station or displayed result by name.

    Used before geocoding so "hotels near Hotel X" or "near EBHRAHIMJEE FILLING CENTRE" search around the
    real known coordinates rather than asking a geocoder to find a business.
    """
    needle = _norm(text)
    if len(needle) < 3:
        return None

    def matches(candidate: str) -> bool:
        hay = _norm(candidate)
        return bool(hay) and (needle == hay or (len(needle) >= 5 and (needle in hay or hay in needle)))

    for i, stop in enumerate(context.stops):
        if matches(stop.name) or matches(stop.display_name):
            return ResolvedPlace(stop.name, stop.latitude, stop.longitude, "named_stop", i)
    for i, planned in enumerate(charging_stops(context)):
        charger = planned.get("charger") or {}
        point = _coords(charger)
        if point and (matches(str(charger.get("name") or "")) or matches(str(charger.get("address") or ""))):
            return ResolvedPlace(str(charger.get("name") or charger.get("address")), point[0], point[1], "named_charger", i, charger)
    for i, poi in enumerate(context.pois):
        point = _coords(poi)
        if point and matches(str(poi.get("name") or "")):
            return ResolvedPlace(str(poi.get("name")), point[0], point[1], "named_result", i, poi)
    return None


def stop_index_by_name(context: TripContext, name: str) -> int | None:
    needle = _norm(name)
    if not needle:
        return None
    for i, stop in enumerate(context.stops):
        if needle in {_norm(stop.name), _norm(stop.display_name)} or (len(needle) >= 4 and needle in _norm(stop.display_name).split()):
            return i
    return None
