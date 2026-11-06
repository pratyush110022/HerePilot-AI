from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from services.errors import AppError
from services.here_places import (
    detect_ambiguity,
    find_area_places,
    MIN_NAME_MATCH,
    find_destination_highlights,
    find_named_places,
    find_nearby_places,
    find_route_places,
    search_places,
)
from services.logging_setup import describe_exception, safe_token
from services.overpass_pois import CATEGORY_DEFINITIONS as OSM_CATEGORIES
from services.overpass_pois import find_pois_along_route as find_osm_pois_along_route
from services.overpass_pois import find_pois_near_points as find_osm_pois_near_points
from services.overpass_pois import normalize_category as normalize_osm_category
from services.route_chargers import haversine_distance_km
from services.routing import get_multi_stop_route

ToolFn = Callable[..., Awaitable[Any]]

logger = logging.getLogger(__name__)


def osm_can_answer(category: str) -> bool:
    """OpenStreetMap is only a fallback for the categories it has tag definitions for (not airports, for instance)."""
    return normalize_osm_category(category) in OSM_CATEGORIES


def _reason(exc: BaseException) -> str:
    """Short, fixed, client-safe reason for a provider failure. Never exception text."""
    return exc.code if isinstance(exc, AppError) else "provider_unavailable"


async def geocode_place(query: str) -> list[dict[str, Any]]:
    return await search_places(query=query, limit=5)


async def calculate_route(stops: list[dict[str, Any]], ev: dict[str, Any] | None = None, transport_mode: str = "car") -> dict[str, Any]:
    return await get_multi_stop_route(stops, ev=ev, transport_mode=transport_mode)


async def find_route_pois(route_geometry: dict[str, Any], category: str, corridor_km: float, limit: int = 60, search_query: str | None = None):
    return await find_route_places(route_geometry, category, corridor_km, limit, search_query)


async def find_nearby_pois(points: list[dict[str, float]], category: str, radius_km: float, limit: int = 80, search_query: str | None = None):
    return await find_nearby_places(points, category, radius_km, limit, search_query)


def _unavailable(here_reason: str, osm_reason: str) -> dict[str, Any]:
    return {
        "pois": [],
        "source_status": "unavailable",
        "source": "HERE Search API + OpenStreetMap / Overpass",
        "source_error": f"HERE: {here_reason}; OSM: {osm_reason}",
    }


async def find_nearby_pois_with_source(
    points: list[dict[str, float]],
    category: str,
    radius_km: float,
    limit: int = 80,
    search_query: str | None = None,
) -> dict[str, Any]:
    """Prefer HERE Search and fall back to live OSM/Overpass if HERE fails or returns no matches.

    The source metadata is kept separate from the POIs so the chat never labels fallback data as
    HERE data and can distinguish an empty search from outages. ``source_error`` is a fixed reason
    code, never provider text.
    """
    here_reason: str | None = None
    stats: dict[str, Any] = {}
    try:
        pois = await find_nearby_places(points, category, radius_km, limit, search_query, stats=stats)
        if pois:
            return {"pois": pois, "source_status": "verified", "source": "HERE Search API", "source_error": None, "search": stats}
        if here_reason is None and not osm_can_answer(category):
            # HERE answered normally and OpenStreetMap has no tags for this kind of place: an honest empty result.
            return {"pois": [], "source_status": "empty", "source": "HERE Search API", "source_error": None, "search": stats}
    except Exception as exc:
        here_reason = _reason(exc)
        logger.warning(
            "event=here_search_failed scope=nearby category=%s error=%s",
            safe_token(category), describe_exception(exc),
        )
        if isinstance(exc, AppError) and exc.code == "provider_not_configured":
            # A missing/rejected key will not be fixed by a fallback; say so plainly.
            return {"pois": [], "source_status": "unavailable", "source": "HERE Search API", "source_error": here_reason, "search": stats}
        if not osm_can_answer(category):
            return {"pois": [], "source_status": "unavailable", "source": "HERE Search API", "source_error": here_reason, "search": stats}

    # OSM's category-tag query is a useful, live fallback for well-defined POI
    # categories such as tourist attractions. It intentionally does not invent
    # names or approximate coordinates.
    try:
        pois = await find_osm_pois_near_points(points, category, radius_km, limit)
        # Recompute the distance against the requested reference coordinates
        # and enforce the radius exactly, including for way/relation centroids.
        filtered_pois = []
        for poi in pois:
            try:
                latitude, longitude = float(poi["latitude"]), float(poi["longitude"])
                distance = min(
                    haversine_distance_km(float(point["latitude"]), float(point["longitude"]), latitude, longitude)
                    for point in points
                )
            except (KeyError, TypeError, ValueError):
                continue
            if distance > radius_km:
                continue
            poi["distance_from_reference_km"] = round(distance, 2)
            poi["source"] = "OpenStreetMap / Overpass"
            filtered_pois.append(poi)
        pois = filtered_pois
        if category == "tourist_attraction":
            pois = [poi for poi in pois if poi.get("name") and poi.get("name") != "Unnamed place"]
        source = "OpenStreetMap / Overpass" if pois else "OpenStreetMap / Overpass fallback"
        if here_reason is None and not pois:
            # HERE answered normally with zero matches and OSM agrees: that is an honest empty result.
            return {"pois": [], "source_status": "empty", "source": "HERE Search API", "source_error": None, "search": stats}
        return {"pois": pois, "source_status": "fallback", "source": source, "source_error": here_reason, "search": {**stats, "category_filtered": False}}
    except Exception as fallback_exc:
        osm_reason = _reason(fallback_exc)
        logger.error(
            "event=osm_fallback_failed scope=nearby category=%s here_failed=%s error=%s",
            safe_token(category), here_reason is not None, describe_exception(fallback_exc),
        )
        if here_reason:
            return _unavailable(here_reason, osm_reason)
        return {"pois": [], "source_status": "empty", "source": "HERE Search API", "source_error": osm_reason}


async def find_route_pois_with_source(
    route_geometry: dict[str, Any],
    category: str,
    corridor_km: float,
    limit: int = 60,
    search_query: str | None = None,
) -> dict[str, Any]:
    """HERE-first route-corridor POI search with a live OSM fallback.

    ``coverage`` reports how much of the route the search covered so callers never imply that a
    capped search on a very long route was exhaustive.
    """
    here_reason: str | None = None
    coverage: dict[str, Any] = {}
    stats: dict[str, Any] = {}
    try:
        pois = await find_route_places(route_geometry, category, corridor_km, limit, search_query, coverage=coverage, stats=stats)
        if pois:
            return {"pois": pois, "source_status": "verified", "source": "HERE Search API", "source_error": None, "coverage": coverage, "search": stats}
        if not osm_can_answer(category):
            return {"pois": [], "source_status": "empty", "source": "HERE Search API", "source_error": None, "coverage": coverage, "search": stats}
    except Exception as exc:
        here_reason = _reason(exc)
        logger.warning(
            "event=here_search_failed scope=route category=%s error=%s",
            safe_token(category), describe_exception(exc),
        )
        if isinstance(exc, AppError) and exc.code == "provider_not_configured":
            return {"pois": [], "source_status": "unavailable", "source": "HERE Search API", "source_error": here_reason, "coverage": coverage, "search": stats}
        if not osm_can_answer(category):
            return {"pois": [], "source_status": "unavailable", "source": "HERE Search API", "source_error": here_reason, "coverage": coverage, "search": stats}

    try:
        pois = await find_osm_pois_along_route(route_geometry, category, corridor_km, limit)
        if category == "tourist_attraction":
            pois = [poi for poi in pois if poi.get("name") and poi.get("name") != "Unnamed place"]
        if here_reason is None and not pois:
            return {"pois": [], "source_status": "empty", "source": "HERE Search API", "source_error": None, "coverage": coverage, "search": stats}
        return {
            "pois": pois,
            "source_status": "fallback",
            "source": "OpenStreetMap / Overpass" if pois else "OpenStreetMap / Overpass fallback",
            "source_error": here_reason,
            "coverage": coverage,
            "search": {**stats, "category_filtered": False},
        }
    except Exception as fallback_exc:
        osm_reason = _reason(fallback_exc)
        logger.error(
            "event=osm_fallback_failed scope=route category=%s here_failed=%s error=%s",
            safe_token(category), here_reason is not None, describe_exception(fallback_exc),
        )
        if here_reason:
            return {**_unavailable(here_reason, osm_reason), "coverage": coverage}
        return {"pois": [], "source_status": "empty", "source": "HERE Search API", "source_error": osm_reason, "coverage": coverage}


async def find_area_pois_with_source(
    area: dict[str, Any],
    category: str,
    limit: int = 80,
    search_query: str | None = None,
    rich: bool = False,
) -> dict[str, Any]:
    """State/district/city-wide search. HERE only: OpenStreetMap is not used as a stand-in, because an area-wide
    list from a different dataset would silently change what "all airports in Odisha" means.

    ``rich`` also asks HERE for Tripadvisor photos and ratings on every request, so the caller can rank by them."""
    stats: dict[str, Any] = {}
    try:
        if rich:
            pois = await find_area_places(area, category, limit, search_query, stats=stats, rich=True)
        else:
            pois = await find_area_places(area, category, limit, search_query, stats=stats)
    except AppError as exc:
        if exc.code in {"invalid_request", "place_not_found", "provider_not_configured"}:
            raise
        logger.warning("event=here_search_failed scope=area category=%s error=%s", safe_token(category), describe_exception(exc))
        return {"pois": [], "source_status": "unavailable", "source": "HERE Search API", "source_error": _reason(exc), "search": stats}
    except Exception as exc:
        logger.warning("event=here_search_failed scope=area category=%s error=%s", safe_token(category), describe_exception(exc))
        return {"pois": [], "source_status": "unavailable", "source": "HERE Search API", "source_error": _reason(exc), "search": stats}
    status = "verified" if pois else "empty"
    return {"pois": pois, "source_status": status, "source": "HERE Search API", "source_error": None, "search": stats}


async def find_destination_highlights_with_source(latitude: float, longitude: float, radius_km: float = 25.0, limit: int = 40) -> dict[str, Any]:
    """The notable places around a destination, from HERE only (a different dataset would change what "notable" means)."""
    stats: dict[str, Any] = {}
    try:
        pois = await find_destination_highlights(latitude, longitude, radius_km, limit, stats=stats)
    except Exception as exc:
        logger.warning("event=here_search_failed scope=highlights error=%s", describe_exception(exc))
        return {"pois": [], "source_status": "unavailable", "source": "HERE Search API", "source_error": _reason(exc), "search": stats}
    return {"pois": pois, "source_status": "verified" if pois else "empty", "source": "HERE Search API", "source_error": None, "search": stats}


_ADMIN_KINDS = {"state": "state", "county": "district"}


async def resolve_area_candidate(query: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Resolve "Odisha", "North Goa" or "Bhubaneswar" to an area with HERE's bounding box.

    Returns (area or None, ambiguous candidates). Only administrative areas and localities qualify; a business or an
    address with a similar name is not an area. A whole country is refused as too large to list.
    """
    candidates = await geocode_place(query)
    ambiguous = detect_ambiguity(candidates, query)
    for candidate in candidates:
        result_type = candidate.get("result_type")
        bbox = candidate.get("map_view")
        if result_type == "administrativeArea":
            admin_type = candidate.get("admin_type")
            if admin_type == "country":
                raise AppError("invalid_request", f"{query} is a whole country: too large to list in one search. Name a state, a district or a city.")
            kind = _ADMIN_KINDS.get(str(admin_type), "region")
        elif result_type == "locality":
            kind = "city"
        else:
            continue
        if not bbox:
            continue
        name = str(candidate.get("display_name") or query).split(",")[0].strip() or query
        return {
            "name": name, "kind": kind, "bbox": bbox, "state": candidate.get("state"), "state_code": candidate.get("state_code"),
            "county": candidate.get("county") if kind == "district" else None,
            "display_name": candidate.get("display_name") or name,
            "center": {"latitude": candidate["latitude"], "longitude": candidate["longitude"]},
        }, ambiguous
    return None, ambiguous


def _same_place(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """HERE sometimes holds two records for one real place (same name, a few hundred metres apart)."""
    if a.get("here_id") and a.get("here_id") == b.get("here_id"):
        return True
    same_name = str(a.get("name") or "").casefold().strip() == str(b.get("name") or "").casefold().strip()
    return same_name and haversine_distance_km(a["latitude"], a["longitude"], b["latitude"], b["longitude"]) < 1.0


def _richness(place: dict[str, Any]) -> tuple[int, int, int]:
    return int(place.get("rating_count") or 0), 1 if place.get("photo") else 0, 1 if place.get("opening_hours") else 0


async def resolve_subject(query: str, near: tuple[float, float] | None = None) -> dict[str, Any] | None:
    """What a name refers to, according to HERE.

    ``{"kind": "place", "place": {...}, "alternatives": [...]}`` for one specific place (an airport, a temple), using
    HERE's own record with its photo, rating and hours; ``{"kind": "area", "area": {...}, "ambiguous": [...]}`` for a
    city, district or state; ``None`` when HERE knows nothing by that name. Never guessed: a result must account for at
    least ``MIN_NAME_MATCH`` of the words asked for, so a street called "Airport Road" cannot stand in for an airport.
    """
    query = (query or "").strip()
    if not query:
        return None
    try:
        found = await find_named_places(query, near=near)
    except AppError as exc:
        if exc.code in {"provider_not_configured", "provider_rate_limited"}:
            raise
        logger.warning("event=named_place_lookup_failed error=%s", describe_exception(exc))
        found = []
    good = sorted((c for c in found if c["match"] >= MIN_NAME_MATCH), key=lambda c: (-round(c["match"], 2), c["order"]))
    if good and good[0]["kind"] == "place":
        places = [c["place"] for c in good if c["kind"] == "place"]
        twins = [p for p in places if _same_place(places[0], p)]
        chosen = max(twins, key=_richness)  # the record with the photo, rating and hours, when only one of a pair has them
        return {"kind": "place", "place": chosen, "alternatives": [p for p in places if not _same_place(chosen, p)][:3]}
    # A city, district or state (or nothing good from discover): the geocoder gives its bounding box.
    area, ambiguous = await resolve_area_candidate(query)
    if area is not None:
        return {"kind": "area", "area": area, "ambiguous": ambiguous}
    return None


async def resolve_place_candidates(query: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Geocode ``query``: (all candidates ranked best-first, ambiguous subset or ``[]``)."""
    candidates = await geocode_place(query)
    return candidates, detect_ambiguity(candidates, query)


TOOLS: dict[str, dict[str, Any]] = {
    "geocode_place": {
        "description": "Geocode a place using HERE Geocoding & Search, India-first. Cities and areas rank above businesses.",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        "handler": geocode_place,
    },
    "calculate_multi_stop_route": {
        "description": "Calculate an ordered route through all supplied stops using HERE Routing API v8 (car, pedestrian or bicycle).",
        "input_schema": {"type": "object", "properties": {"stops": {"type": "array"}, "transport_mode": {"type": "string"}}, "required": ["stops"]},
        "handler": calculate_route,
    },
    "find_pois_along_route": {
        "description": "Find places within a corridor of the active route with HERE Search first and live OpenStreetMap / Overpass fallback.",
        "input_schema": {"type": "object", "properties": {"category": {"type": "string"}, "search_query": {"type": "string"}, "corridor_km": {"type": "number"}}, "required": ["category"]},
        "handler": find_route_pois_with_source,
    },
    "find_pois_in_area": {
        "description": "List places of a HERE category across a state, district or city (HERE bounding box tiles merged by place ID).",
        "input_schema": {"type": "object", "properties": {"category": {"type": "string"}, "area": {"type": "string"}}, "required": ["category", "area"]},
        "handler": find_area_pois_with_source,
    },
    "destination_highlights": {
        "description": "The notable attractions around a destination from HERE, best known first, with Tripadvisor photos and ratings when HERE supplies them.",
        "input_schema": {"type": "object", "properties": {"latitude": {"type": "number"}, "longitude": {"type": "number"}, "radius_km": {"type": "number"}}, "required": ["latitude", "longitude"]},
        "handler": find_destination_highlights_with_source,
    },
    "find_pois_near_location": {
        "description": "Find places within a radius of a location with HERE Search first and live OpenStreetMap / Overpass fallback.",
        "input_schema": {"type": "object", "properties": {"category": {"type": "string"}, "search_query": {"type": "string"}, "radius_km": {"type": "number"}}, "required": ["category"]},
        "handler": find_nearby_pois_with_source,
    },
}


def list_tools() -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "description": definition["description"],
            "input_schema": definition["input_schema"],
        }
        for name, definition in TOOLS.items()
    ]
