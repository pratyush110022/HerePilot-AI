"""Conversation orchestrator.

``handle_message`` interprets a message into one validated intent, dispatches it to a small handler,
and phrases the result. The pieces are deliberately separate:

* **Interpretation** (:func:`_interpret`): the LLM proposes a structured intent; the rule parser is the
  always-available fallback and wins where it is exact (route edits, numbered EV details).
* **Handlers** (``_h_*``): deterministic, provider-backed work. They never trust raw model text.
* **Replies** (:func:`_compose_reply`): deterministic wording for every place, distance, time and count; the LLM
  only answers open-ended follow-ups about results already on screen, from an allow-listed view, and its numbers
  must trace back to those results.

A general trip never touches the EV planner. EV results exist only while the optional EV Planner is on.
"""
from __future__ import annotations

import asyncio
import copy
import logging
import re
from datetime import date
from typing import Any, Awaitable, Callable

from services import llm_assistant
from services.conversation import arrange_places as arrange_answer_places
from services.conversation import write_answer
from services.destination_guide import arrange_places, write_guide
from services.errors import AppError
from services.ev_planner import plan_ev_trip
from services.geo_utils import bbox_size_km, dedupe_places, place_identity, point_along_route
from services.heuristic_intent import parse as rule_parse
from services.here_places import search_here_ev_chargers
from services.intents import (
    DEFAULT_ITINERARY_DAYS,
    TRAVEL_MODES,
    VEHICLE_TYPES,
    TooManyStops,
    clamp_limit,
    clamp_radius,
    clean_text,
    validate_intent,
)
from services.intent_parser import explicit_route_request, forbids_route_change, has_itinerary_signals, is_me_place, is_midpoint_place, pick_index
from services.logging_setup import describe_exception, safe_token
from services.mcp_tools import (
    calculate_route,
    find_area_pois_with_source,
    find_destination_highlights_with_source,
    find_nearby_pois_with_source,
    find_route_pois_with_source,
    resolve_area_candidate,
    resolve_place_candidates,
    resolve_subject,
)
from services.place_categories import spec_for
from services.references import (
    ResolvedPlace,
    charging_stops,
    find_named_known_place,
    resolve_focus,
    resolve_scope,
    stop_index_by_name,
)
from services.route_chargers import (
    calculate_distance_to_route_km,
    calculate_route_position_percent,
    filter_chargers_near_route,
)
from services.tata_chargers import get_tata_chargers_in_bbox, search_tata_chargers_nearby
from services.travel_planner import (
    MAX_DESTINATIONS,
    MAX_HOTELS,
    MAX_RESTAURANTS,
    compact_itinerary,
    kit_for_prompt,
    name_matches,
    propose_destinations,
    rank_places,
    real_stays,
    tidy_attractions,
    write_itinerary,
)
from services.trip_context import PlaceContext, TripContext, VehicleContext
from services.weather import (
    climate_conflicts,
    climate_words,
    get_current_weather,
    weather_bundle,
    weather_enabled,
    weather_sentence,
    weather_snapshot_metadata,
)

logger = logging.getLogger(__name__)

EV_DATASET_LABEL = "Tata EV Charging Dataset (bundled)"
MAX_TRIP_STOPS = 10

Handler = Callable[[TripContext, dict[str, Any], str], Awaitable[dict[str, Any]]]


# ---------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------
def _role_stops(stops: list[PlaceContext]) -> None:
    for i, stop in enumerate(stops):
        stop.role = "origin" if i == 0 else "destination" if i == len(stops) - 1 else "waypoint"


def _result_base(context: TripContext) -> dict[str, Any]:
    snapshot = context.to_dict()
    return {
        "session_id": context.session_id,
        "stops": snapshot["stops"],
        "vehicle": snapshot.get("vehicle"),
        "vehicle_type": context.vehicle_type,
        "travel_mode": context.travel_mode,
        "weather": context.weather,
    }


def _place_from_candidate(query: str, candidate: dict[str, Any], role: str) -> PlaceContext:
    return PlaceContext(
        name=query,
        display_name=candidate.get("display_name") or query,
        latitude=float(candidate["latitude"]),
        longitude=float(candidate["longitude"]),
        role=role,
        result_type=candidate.get("result_type"),
    )


async def _resolve_place(query: str, role: str = "stop") -> PlaceContext:
    """Geocode one place. Empty results and provider failures are different, and ambiguity is asked about."""
    query = clean_text(query)
    if not query:
        raise AppError("invalid_request", "Tell me which place you mean.")
    candidates, ambiguous = await resolve_place_candidates(query)
    if not candidates:
        raise AppError("place_not_found", f"I couldn't find “{query}”. Try adding a city or state.", data={"query": query})
    if ambiguous:
        options = [", ".join(p for p in (c.get("display_name"),) if p) for c in ambiguous[:4]]
        raise AppError(
            "place_ambiguous",
            f"“{query}” matches more than one place: {'; '.join(options)}. Which one do you mean?",
            data={"query": query, "options": options},
        )
    return _place_from_candidate(query, candidates[0], role)


def _route_midpoint(context: TripContext, fraction: float = 0.5) -> dict[str, Any]:
    """The point ``fraction`` of the way along the ACTIVE route, by distance along its real geometry."""
    if len(context.stops) < 2 or not context.route:
        raise AppError("needs_trip", "Plan a route first, then I can search around its halfway point.")
    coordinates = (context.route.get("geometry") or {}).get("coordinates") or []
    midpoint = point_along_route(coordinates, fraction)
    if midpoint is None:
        raise AppError("needs_trip", "I couldn't work out the middle of this route.")
    return midpoint


def _user_point(context: TripContext) -> tuple[float, float]:
    location = context.user_location
    if not location:
        raise AppError("needs_location")
    return float(location["latitude"]), float(location["longitude"])


async def _resolve_search_center(context: TripContext, text: str) -> tuple[str, float, float]:
    """A search center by name: a known trip stop/charger/result first, otherwise geocode it.

    "me" and "the halfway point of my trip" are not names a geocoder could resolve; they are resolved from the session.
    """
    if is_me_place(text):
        lat, lon = _user_point(context)
        return "your location", lat, lon
    if is_midpoint_place(text):
        midpoint = _route_midpoint(context)
        return "the halfway point of your trip", midpoint["latitude"], midpoint["longitude"]
    known = find_named_known_place(context, text)
    if known is not None:
        return known.name, known.latitude, known.longitude
    place = await _resolve_place(text, "poi_search")
    return text, place.latitude, place.longitude


def _vehicle_error(context: TripContext) -> str | None:
    vehicle = context.vehicle
    if vehicle is None:
        return "Tell me your vehicle's range in km and its current battery %."
    missing = []
    if vehicle.max_range_km is None:
        missing.append("estimated range in km")
    if vehicle.current_battery_percent is None:
        missing.append("current battery %")
    return ("Please share your " + " and ".join(missing) + ".") if missing else None


def _validate_settings(context: TripContext) -> None:
    if not 0 <= context.reserve_percent < 100:
        raise AppError("invalid_request", "Reserve must be between 0 and 99%.")
    if not 0 < context.target_charge_percent <= 100:
        raise AppError("invalid_request", "Target charge must be between 1 and 100%.")
    if context.target_charge_percent <= context.reserve_percent:
        raise AppError("invalid_request", "Target charge must be above the reserve percentage.")
    if context.corridor_km <= 0 or context.corridor_km > 50:
        raise AppError("invalid_request", "Corridor must be between 0 and 50 km.")


def _update_vehicle(context: TripContext, updates: dict[str, Any]) -> None:
    vehicle = context.vehicle or VehicleContext()
    for field in ("make", "model", "max_range_km", "battery_capacity_kwh", "current_battery_percent"):
        if field in updates and updates[field] is not None:
            value = updates[field]
            if field in {"max_range_km", "battery_capacity_kwh", "current_battery_percent"}:
                value = float(value)
            setattr(vehicle, field, value)
    context.vehicle = vehicle


def _apply_settings(context: TripContext, updates: dict[str, Any]) -> None:
    for field in ("reserve_percent", "target_charge_percent", "corridor_km"):
        if field in updates and updates[field] is not None:
            setattr(context, field, float(updates[field]))
    _validate_settings(context)


def _set_focus(context: TripContext, name: str, lat: float, lon: float, kind: str, index: int | None = None, detail: dict[str, Any] | None = None) -> None:
    context.focus = {"name": name, "latitude": lat, "longitude": lon, "kind": kind, "index": index}
    if detail and kind == "charging_stop":
        context.focus["detail"] = {"id": detail.get("id")}


def _charger_as_poi(charger: dict[str, Any]) -> dict[str, Any]:
    """Adapt the bundled EV dataset to the common POI shape."""
    return {
        "osm_id": None, "osm_type": None,
        "name": charger.get("name") or charger.get("address") or "EV charging station",
        "category": "ev_charging",
        "latitude": float(charger["latitude"]), "longitude": float(charger["longitude"]),
        "operator": charger.get("cpoName"), "address": charger.get("address"),
        "source": charger.get("source") or EV_DATASET_LABEL,
        "charger_id": charger.get("id"), "charger": charger,
        "socket_type": charger.get("socket_type") or charger.get("chargerType"),
        "charging_speed": charger.get("charging_speed"),
        "estimated_power_kw": charger.get("estimated_power_kw"),
        "brand_key": charger.get("brand_key", "generic-ev"),
        "distance_from_route_km": charger.get("distance_from_route_km"),
        "route_position_percent": charger.get("route_position_percent"),
    }


def _route_bbox(route: dict[str, Any], padding_km: float) -> dict[str, float]:
    import math
    coords = route.get("geometry", {}).get("coordinates", [])
    if len(coords) < 2:
        raise AppError("needs_trip", "There is no route geometry yet.")
    lons = [float(pair[0]) for pair in coords]
    lats = [float(pair[1]) for pair in coords]
    mid_lat = sum(lats) / len(lats)
    lat_pad = padding_km / 111.32
    lon_pad = padding_km / max(111.32 * abs(math.cos(math.radians(mid_lat))), 1e-6)
    return {"south": min(lats) - lat_pad, "west": min(lons) - lon_pad, "north": max(lats) + lat_pad, "east": max(lons) + lon_pad}


def _route_chargers(route: dict[str, Any], corridor_km: float) -> list[dict[str, Any]]:
    candidates = get_tata_chargers_in_bbox(**_route_bbox(route, corridor_km))
    return filter_chargers_near_route(route["geometry"], candidates, corridor_km)


# ---------------------------------------------------------------------------------------------
# Weather (advisory only; never changes any calculation)
# ---------------------------------------------------------------------------------------------
async def _refresh_trip_weather(context: TripContext) -> None:
    """Fetch non-blocking advisory conditions for trip stops and planned chargers."""
    if not context.stops:
        context.weather = {}
        return

    weather_points: dict[str, tuple[float, float]] = {}

    def coord_key(latitude: float, longitude: float) -> str:
        return f"{latitude:.3f}:{longitude:.3f}"

    stop_keys: list[str] = []
    for stop in context.stops:
        key = coord_key(float(stop.latitude), float(stop.longitude))
        weather_points.setdefault(key, (float(stop.latitude), float(stop.longitude)))
        stop_keys.append(key)

    charger_keys: dict[str, str] = {}
    for index, planned_stop in enumerate(charging_stops(context)):
        charger = planned_stop.get("charger") or {}
        try:
            latitude, longitude = float(charger["latitude"]), float(charger["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        key = coord_key(latitude, longitude)
        weather_points.setdefault(key, (latitude, longitude))
        charger_keys[str(charger.get("id") or f"stop-{index + 1}")] = key

    point_keys = list(weather_points)
    values = await asyncio.gather(*(get_current_weather(*weather_points[key]) for key in point_keys))
    readings = dict(zip(point_keys, values))
    unavailable = {"available": False, "provider": "Open-Meteo"}

    route_stops = [{
        "name": stop.name, "display_name": stop.display_name, "role": stop.role,
        "latitude": stop.latitude, "longitude": stop.longitude,
        "weather": readings.get(stop_keys[index], unavailable),
    } for index, stop in enumerate(context.stops)]

    context.weather = {
        **weather_snapshot_metadata(),
        "route_stops": route_stops,
        "destination": route_stops[-1] if route_stops else None,
        "charging_stops": {cid: readings.get(key, unavailable) for cid, key in charger_keys.items()},
    }


# ---------------------------------------------------------------------------------------------
# Route calculation and the (optional) EV plan
# ---------------------------------------------------------------------------------------------
async def _calculate_route_for(context: TripContext) -> dict[str, Any]:
    if len(context.stops) < 2:
        raise AppError("needs_trip")
    route_stops = [{"name": s.name, "latitude": s.latitude, "longitude": s.longitude} for s in context.stops]
    ev_profile = None
    vehicle = context.vehicle
    if context.ev_mode and vehicle is not None and context.travel_mode == "car":
        ev_profile = {
            "max_range_km": vehicle.max_range_km, "battery_capacity_kwh": vehicle.battery_capacity_kwh,
            "current_battery_percent": vehicle.current_battery_percent,
            "target_charge_percent": context.target_charge_percent, "reserve_percent": context.reserve_percent,
        }
    try:
        return await calculate_route(route_stops, ev=ev_profile, transport_mode=context.travel_mode)
    except AppError:
        if ev_profile is None:
            raise
        # Native EV routing is optional. Fall back to an ordinary route; the deterministic EV planner
        # does not depend on it. The cause is logged, not shown.
        logger.warning("event=ev_routing_fallback")
        route = await calculate_route(route_stops, transport_mode=context.travel_mode)
        route["native_ev_routing"] = False
        route["ev_routing_fallback_reason"] = "native_ev_routing_unavailable"
        return route


def _run_ev_plan(context: TripContext) -> None:
    """Compute the EV plan from the active route. Sets ``ev_status`` honestly."""
    vehicle_error = _vehicle_error(context)
    if vehicle_error:
        context.ev_plan, context.ev_status = None, "needs_vehicle"
        return
    vehicle, route = context.vehicle, context.route
    assert vehicle is not None and route is not None
    try:
        context.ev_plan = plan_ev_trip(
            route_distance_km=route["distance_km"], chargers=context.chargers,
            max_range_km=float(vehicle.max_range_km), starting_battery_percent=float(vehicle.current_battery_percent),
            reserve_percent=context.reserve_percent, target_charge_percent=context.target_charge_percent,
            battery_capacity_kwh=float(vehicle.battery_capacity_kwh) if vehicle.battery_capacity_kwh is not None else None,
            route_duration_minutes=float(route["duration_minutes"]) if route.get("duration_minutes") is not None else None,
        )
        context.ev_status = "calculated"
    except Exception as exc:
        logger.error("event=ev_plan_failed error=%s", describe_exception(exc))
        context.ev_plan, context.ev_status = None, "failed"


def _ev_into(context: TripContext, result: dict[str, Any]) -> None:
    """Compute chargers + the EV plan for the ACTIVE route and record the outcome in ``result``."""
    context.chargers = _route_chargers(context.route, context.corridor_km)
    _run_ev_plan(context)
    result.update(chargers=context.chargers, ev_plan=context.ev_plan, ev_status=context.ev_status)
    if context.ev_status == "needs_vehicle":
        result["needs_vehicle"] = True
        result["message"] = _vehicle_error(context)


async def _recompute_ev(context: TripContext) -> dict[str, Any]:
    """Re-run only the EV calculations on the existing route. No re-routing, search results untouched."""
    if not context.route or len(context.stops) < 2:
        raise AppError("needs_trip", "Tell me where you're driving first, then I can plan charging for it.")
    _validate_settings(context)
    result: dict[str, Any] = {"success": True, "action": "plan_trip", "route": context.route}
    _ev_into(context, result)
    await _refresh_trip_weather(context)
    result["weather"] = context.weather
    return result


async def _apply_route(context: TripContext) -> dict[str, Any]:
    """(Re)calculate the route for the current stops and rebuild everything that depends on it."""
    _validate_settings(context)
    _role_stops(context.stops)
    route = await _calculate_route_for(context)
    context.route = route
    context.invalidate_route_dependents()  # POIs, EV plan, weather and focus described the OLD route
    result: dict[str, Any] = {"success": True, "action": "plan_trip", "route": route}
    if context.ev_mode:
        _ev_into(context, result)
    await _refresh_trip_weather(context)
    result["weather"] = context.weather
    return result


def _route_summary_extras(context: TripContext, result: dict[str, Any]) -> dict[str, Any]:
    result.update({
        "stops": context.to_dict()["stops"], "vehicle_type": context.vehicle_type,
        "travel_mode": context.travel_mode, "ev_mode": context.ev_mode,
    })
    return result


# ---------------------------------------------------------------------------------------------
# Handlers: general assistant
# ---------------------------------------------------------------------------------------------
async def _h_help(context: TripContext, action: dict[str, Any], message: str) -> dict[str, Any]:
    return {"success": True, "action": "help"}


async def _h_clarify(context: TripContext, action: dict[str, Any], message: str) -> dict[str, Any]:
    return {"success": True, "action": "clarify", "question": action.get("question"), "pending": action.get("pending")}


async def _h_general_question(context: TripContext, action: dict[str, Any], message: str) -> dict[str, Any]:
    doc = await write_answer(message, [], [], context.memory, llm_assistant.llm_view(_context_dict(context)), date.today().month)
    if doc:
        return _answer_result(context, doc, subjects=[], pois=[], source_status="empty")
    answer = await llm_assistant.answer_general(message, context.to_dict())
    if answer:
        return {"success": True, "action": "general_answer", "answer": answer, "answer_source": "ai_general_knowledge"}
    return {"success": True, "action": "general_answer", "answer": None}


def _guide_radius_km(area: dict[str, Any] | None) -> float:
    """How far from a destination's centre its highlights are looked for: wider for a state, 15 km for a town."""
    bbox = (area or {}).get("bbox")
    if not bbox:
        return 15.0
    try:
        width, height = bbox_size_km(bbox)
    except (KeyError, TypeError, ValueError):
        return 15.0
    return float(max(15.0, min(60.0, max(width, height) / 2.0)))


async def _h_destination_guide(context: TripContext, action: dict[str, Any], message: str) -> dict[str, Any]:
    """"best time to visit Ooty": HERE finds the notable places, the model writes the advice around them.

    The model never supplies a place: it can only point at HERE places by id, and seasonal advice is labelled as the
    model's general knowledge. With no AI configured the guide is the HERE places alone, and says so.
    """
    place_text = clean_text(action.get("place"))
    topic = str(action.get("topic") or "overview")
    try:
        area, ambiguous = await resolve_area_candidate(place_text)
    except AppError as exc:
        if exc.code == "invalid_request":
            # A whole country is too big for a place list; the model can still answer from general knowledge.
            return await _h_general_question(context, action, message)
        raise
    if ambiguous:
        options = [str(c.get("display_name")) for c in ambiguous[:4]]
        raise AppError("place_ambiguous", f"“{place_text}” matches more than one place: {'; '.join(options)}. Which one do you mean?",
                       data={"query": place_text, "options": options})
    if area is not None:
        name, lat, lon = area["name"], float(area["center"]["latitude"]), float(area["center"]["longitude"])
        region = area.get("state") if area.get("kind") == "city" else area.get("display_name")
    else:
        name, lat, lon = await _resolve_search_center(context, place_text)
        region = None
    radius_km = _guide_radius_km(area)
    search = await find_destination_highlights_with_source(lat, lon, radius_km, 40)
    shortlist = search.get("pois") or []
    # The model chooses from HERE's shortlist; what is shown is the places it placed in the guide, then the best of the rest.
    guide = await write_guide(name, region, message, topic, shortlist, date.today().month)
    pois = arrange_places(guide, shortlist)
    context.pois = pois
    context.pois_label = f"top places in {name}"
    _set_focus(context, name, lat, lon, "place")
    return {
        **_result_base(context), "success": True, "action": "destination_guide", "place": name, "region": region, "topic": topic,
        "center": {"latitude": lat, "longitude": lon, "name": name}, "radius_km": radius_km, "category": "tourist_attraction",
        "scope": "location", "pois": pois, "source_status": search.get("source_status", "verified"), "source": search.get("source"),
        "source_error": search.get("source_error"), "search": search.get("search") or {}, "guide": guide,
        "guide_source": "ai_general_knowledge" if guide else None,
    }


MAX_GROUNDING_PER_SUBJECT = 15
MAX_MEMORY_TURNS = 6


def _answer_result(context: TripContext, doc: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {**_result_base(context), "success": True, "action": "ask", "answer_doc": doc, "answer_source": "ai_general_knowledge", **extra}


def _remember(context: TripContext, message: str, result: dict[str, Any], reply: str) -> None:
    """Keep the last few turns (server-side only) so a follow-up like "which of those is best for kids?" can be understood."""
    shown = [str(p.get("name")) for p in (result.get("pois") or [])[:8] if isinstance(p, dict) and p.get("name")]
    context.memory.append({"user": clean_text(message, 300), "assistant": reply[:600], "shown": shown})
    del context.memory[:-MAX_MEMORY_TURNS]


async def _ground_subject(name: str) -> dict[str, Any]:
    """HERE's view of one thing a question is about: a specific place, a destination with its notable places, or
    nothing. A name HERE does not know, or a HERE outage, never fails the question."""
    try:
        subject = await resolve_subject(name)
        if subject is None:
            return {"name": name, "kind": "unknown", "places": []}
        if subject["kind"] == "place":
            place = {**subject["place"], "subject": name}
            return {"name": name, "kind": "place", "places": [place], "alternatives": subject.get("alternatives", []),
                    "center": (place["latitude"], place["longitude"])}
        area = subject["area"]
        if subject.get("ambiguous"):
            return {"name": name, "kind": "ambiguous", "places": [], "options": [str(c.get("display_name")) for c in subject["ambiguous"][:4]]}
        lat, lon = float(area["center"]["latitude"]), float(area["center"]["longitude"])
        found = await find_destination_highlights_with_source(lat, lon, _guide_radius_km(area), MAX_GROUNDING_PER_SUBJECT)
        return {"name": area["name"], "kind": "destination", "region": area.get("state"), "center": (lat, lon),
                "places": [{**p, "subject": area["name"]} for p in found.get("pois") or []], "source_error": found.get("source_error")}
    except AppError as exc:
        logger.info("event=ask_ground_failed code=%s", exc.code)
        return {"name": name, "kind": "unknown", "places": [], "source_error": exc.code}


async def _h_ask(context: TripContext, action: dict[str, Any], message: str) -> dict[str, Any]:
    """An open question. HERE resolves the places it names; the model reasons about them and chooses the answer's
    format; it can only point at the places HERE returned."""
    names = [clean_text(n, 80) for n in (action.get("subjects") or []) if clean_text(n, 80)][:3]
    grounded = list(await asyncio.gather(*(_ground_subject(n) for n in names))) if names else []
    ambiguous = [g for g in grounded if g["kind"] == "ambiguous"]
    if ambiguous and len(names) == 1:
        options = ambiguous[0]["options"]
        raise AppError("place_ambiguous", f"“{names[0]}” matches more than one place: {'; '.join(options)}. Which one do you mean?",
                       data={"query": names[0], "options": options})
    places = dedupe_places([p for g in grounded for p in g["places"]])
    identity = {place_identity(p): i for i, p in enumerate(places)}
    pinned = [identity[place_identity(g["places"][0])] for g in grounded if g["kind"] == "place" and g["places"]]
    subjects_public = [{"name": g["name"], "kind": g["kind"]} for g in grounded]
    doc = await write_answer(message, subjects_public, places, context.memory, llm_assistant.llm_view(_context_dict(context)), date.today().month)
    # With an answer, show the places it points at (the subject itself first). Without one, show what HERE has.
    shown = arrange_answer_places(doc, places, pinned) if doc else dedupe_places([places[i] for i in pinned] + places)[:12]
    if shown:
        context.pois, context.pois_label = shown, f"places for “{message[:60]}”"
    first = next((g for g in grounded if g.get("center")), None)
    if first:
        _set_focus(context, first["name"], first["center"][0], first["center"][1], "place")
    position = {place_identity(p): i for i, p in enumerate(shown)}
    subjects_out = []
    for g in grounded:
        entry: dict[str, Any] = {"name": g["name"], "kind": g["kind"]}
        if g["kind"] == "place" and g["places"]:
            entry["poi_index"] = position.get(place_identity(g["places"][0]))
        if g.get("alternatives"):
            entry["alternatives"] = len(g["alternatives"])
        subjects_out.append(entry)
    unavailable = any(g.get("source_error") for g in grounded) and not shown
    extra = {"subjects": subjects_out, "pois": shown, "source": "HERE Search API", "ai_available": llm_assistant.get_provider().available,
             "source_status": "unavailable" if unavailable else "verified" if shown else "empty"}
    if doc:
        return _answer_result(context, doc, **extra)
    return {**_result_base(context), "success": True, "action": "ask", "answer_doc": None, "answer_source": None, **extra}


async def _h_follow_up(context: TripContext, action: dict[str, Any], message: str) -> dict[str, Any]:
    if not context.pois and not context.stops and not context.memory:
        return {"success": True, "action": "general_answer", "answer": None}
    # Reason over what is on screen and what was said, with the places on screen as the only places the answer may name.
    doc = await write_answer(message, [], context.pois, context.memory, llm_assistant.llm_view(_context_dict(context)), date.today().month)
    if doc:
        return _answer_result(context, doc, subjects=[], pois=context.pois, source="HERE Search API", source_status="verified" if context.pois else "empty")
    return {"success": True, "action": "follow_up", **_result_base(context), "pois": context.pois, "pois_label": context.pois_label}


async def _h_context(context: TripContext, action: dict[str, Any], message: str) -> dict[str, Any]:
    route = context.route or {}
    return {
        **_result_base(context), "success": True, "action": "context",
        "route_distance_km": route.get("distance_km"), "route_duration_minutes": route.get("duration_minutes"),
        "pois_count": len(context.pois), "ev_mode": context.ev_mode,
    }


async def _h_geocode(context: TripContext, action: dict[str, Any], message: str) -> dict[str, Any]:
    query = clean_text(action.get("place")) or clean_text(message)
    # A specific place (an airport, a temple): HERE's own record, not the street that happens to carry its name.
    try:
        subject = await resolve_subject(query)
    except AppError as exc:
        if exc.code in {"provider_not_configured", "provider_rate_limited", "provider_unavailable"}:
            raise
        subject = None
    if subject and subject["kind"] == "place":
        place = subject["place"]
        _set_focus(context, place["name"], place["latitude"], place["longitude"], "place")
        shown = dedupe_places([place, *subject.get("alternatives", [])])
        context.pois, context.pois_label = shown, place["name"]
        candidate = {"display_name": place.get("address") or place["name"], "latitude": place["latitude"], "longitude": place["longitude"],
                     "state": place.get("state"), "country": place.get("country"), "result_type": "place"}
        return {**_result_base(context), "success": True, "action": "geocode", "query": query, "results": [candidate], "ambiguous": [],
                "named_place": place, "pois": shown, "source_status": "verified", "source": "HERE Search API", "alternatives": len(shown) - 1}
    candidates, ambiguous = await resolve_place_candidates(query)
    if candidates:
        best = candidates[0]
        _set_focus(context, best.get("display_name") or query, best["latitude"], best["longitude"], "place")
    return {"success": bool(candidates), "action": "geocode", "query": query, "results": candidates[:5],
            "ambiguous": [c.get("display_name") for c in ambiguous[:4]]}


async def _h_set_trip_options(context: TripContext, action: dict[str, Any], message: str) -> dict[str, Any]:
    changed: list[str] = []
    if action.get("vehicle_type") in VEHICLE_TYPES:
        context.vehicle_type = action["vehicle_type"]
        changed.append("vehicle_type")
    mode = action.get("travel_mode")
    reroute = False
    if mode in TRAVEL_MODES and mode != context.travel_mode:
        context.travel_mode = mode
        changed.append("travel_mode")
        reroute = len(context.stops) >= 2
    result: dict[str, Any] = {"success": True, "action": "set_trip_options", "changed": changed}
    if reroute:
        result.update(await _apply_route(context))
        result["action"] = "set_trip_options"
    return _route_summary_extras(context, result)


async def _h_plan_trip(context: TripContext, action: dict[str, Any], message: str) -> dict[str, Any]:
    places = action.get("places") or [s.name for s in context.stops]
    if len(places) < 2:
        raise AppError("needs_trip", "Tell me where you're starting and where you're going.")
    if len(places) > MAX_TRIP_STOPS:
        raise AppError("invalid_request", f"A trip can have at most {MAX_TRIP_STOPS} stops.")
    if action.get("vehicle_type") in VEHICLE_TYPES:
        context.vehicle_type = action["vehicle_type"]
    if action.get("travel_mode") in TRAVEL_MODES:
        context.travel_mode = action["travel_mode"]
    elif context.travel_mode not in TRAVEL_MODES:
        context.travel_mode = "car"
    resolved: list[PlaceContext] = []
    for i, place in enumerate(places):
        role = "origin" if i == 0 else "destination" if i == len(places) - 1 else "waypoint"
        resolved.append(await _resolve_place(str(place), role))
    if _same_point(resolved[0], resolved[-1]) and len(resolved) == 2:
        raise AppError("invalid_request", "The origin and destination are the same place.")
    context.stops = resolved
    if action.get("settings"):
        _apply_settings(context, action["settings"])
    if action.get("ev_required"):
        context.ev_mode = True
        context.vehicle_type = context.vehicle_type or "electric"
        if action.get("vehicle_updates"):
            _update_vehicle(context, action["vehicle_updates"])
    result = await _apply_route(context)
    context.focus = None
    return _route_summary_extras(context, result)


def _is_active_trip(context: TripContext, places: list[str]) -> bool:
    """Do the named places match the trip already planned (same stops in the same order)?"""
    def norm(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()
    names = [norm(stop.name) for stop in context.stops]
    return len(names) == len(places) >= 2 and names == [norm(place) for place in places]


def _same_point(a: PlaceContext, b: PlaceContext) -> bool:
    return abs(a.latitude - b.latitude) < 1e-4 and abs(a.longitude - b.longitude) < 1e-4


def _insert_position(context: TripContext, action: dict[str, Any]) -> int:
    """Where a new waypoint goes: after/before a named stop, at an index, or just before the destination."""
    last = len(context.stops) - 1
    after, before = action.get("after"), action.get("before")
    if after:
        i = stop_index_by_name(context, after)
        if i is None:
            raise AppError("reference_unresolved", f"“{after}” isn't one of your stops. Your route is {' → '.join(context.trip_names())}.")
        return min(i + 1, last)
    if before:
        i = stop_index_by_name(context, before)
        if i is None:
            raise AppError("reference_unresolved", f"“{before}” isn't one of your stops. Your route is {' → '.join(context.trip_names())}.")
        return max(i, 1)
    index = action.get("index")
    if index is not None:
        return max(1, min(int(index), last))
    return max(1, last)


async def _h_modify_trip(context: TripContext, action: dict[str, Any], message: str) -> dict[str, Any]:
    op = action.get("operation")
    if op == "add_charging_stop":
        return await _add_charging_stop(context, action)

    if len(context.stops) < 2:
        if op == "add_stop":
            raise AppError("needs_trip", "There's no route to add a stop to yet. Tell me where you're starting and where you're going first.")
        raise AppError("needs_trip")

    before_names = list(context.trip_names())
    snapshot = copy.deepcopy(context.stops)  # restored if the new route cannot be calculated
    if op == "add_stop":
        place_text = clean_text(action.get("place"))
        existing = stop_index_by_name(context, place_text)
        if existing is not None:
            raise AppError("invalid_request", f"{context.stops[existing].name} is already on your route.")
        if len(context.stops) >= MAX_TRIP_STOPS:
            raise AppError("invalid_request", f"A trip can have at most {MAX_TRIP_STOPS} stops.")
        new_stop = await _resolve_place(place_text, "waypoint")
        position = _insert_position(context, action)
        context.stops.insert(position, new_stop)
        change = f"Added {new_stop.name} as stop {position + 1}."
    elif op == "remove_stop":
        if len(context.stops) <= 2:
            raise AppError("invalid_request", "A trip needs at least an origin and a destination.")
        if action.get("place"):
            index = stop_index_by_name(context, action["place"])
            if index is None:
                raise AppError("reference_unresolved", f"“{action['place']}” isn't one of your stops. Your route is {' → '.join(context.trip_names())}.")
        else:
            index = int(action.get("index", -1))
        if index < 0 or index >= len(context.stops):
            raise AppError("reference_unresolved", "That stop number isn't on your route.")
        if index in (0, len(context.stops) - 1):
            raise AppError("invalid_request", "That's your origin or destination. Change it with “change the destination to …” instead.")
        removed = context.stops.pop(index)
        change = f"Removed {removed.name}."
    elif op == "set_origin":
        context.stops[0] = await _resolve_place(clean_text(action.get("place")), "origin")
        change = f"Origin is now {context.stops[0].name}."
    elif op == "set_destination":
        context.stops[-1] = await _resolve_place(clean_text(action.get("place")), "destination")
        change = f"Destination is now {context.stops[-1].name}."
    else:
        raise AppError("invalid_request", "I can add or remove stops and change the origin or destination.")

    try:
        result = await _apply_route(context)
    except AppError:
        # Never leave the session listing stops that have no route behind them: restore exactly.
        context.stops = snapshot
        raise
    result["action"] = "modify_trip"
    result["change"] = change
    result["previous_stops"] = before_names
    return _route_summary_extras(context, result)


async def _add_charging_stop(context: TripContext, action: dict[str, Any]) -> dict[str, Any]:
    if not context.ev_mode:
        raise AppError("invalid_request", "Switch on the EV Planner first to work with charging stops.")
    place = clean_text(action.get("place"))
    if not place:
        raise AppError("invalid_request", "Tell me where you want the charging stop.")
    if len(context.stops) < 2 or not context.route:
        raise AppError("needs_trip", "Plan the trip first, then add a charging stop at a location.")
    _, lat, lon = await _resolve_search_center(context, place)
    radius_km = 25.0
    local = search_tata_chargers_nearby(lat, lon, radius_km, 50)
    try:
        here = await search_here_ev_chargers(lat, lon, radius_km, 50)
    except AppError as exc:
        logger.warning("event=here_ev_charger_search_failed error=%s", exc.code)
        here = []
    candidates = local + here
    if not candidates:
        raise AppError("place_not_found", f"I couldn't find an EV charging station near {place}.")
    candidates.sort(key=lambda item: float(item.get("distance_from_reference_km", 999999)))
    selected = dict(candidates[0])
    route_coords = (context.route.get("geometry") or {}).get("coordinates") or []
    distance_from_route = calculate_distance_to_route_km(route_coords, selected["latitude"], selected["longitude"])
    if distance_from_route is None:
        raise AppError("invalid_request", "That charging station couldn't be positioned on the current route.")
    if distance_from_route > max(float(context.corridor_km), 10.0):
        raise AppError("invalid_request", f"The nearest charging station near {place} is {distance_from_route:.1f} km off the route, beyond the current corridor.")
    selected["distance_from_route_km"] = distance_from_route
    selected["route_position_percent"] = calculate_route_position_percent(route_coords, selected["latitude"], selected["longitude"])
    selected["route_relevant"] = True
    selected["source"] = selected.get("source") or EV_DATASET_LABEL
    if str(selected.get("id")) not in {str(c.get("id")) for c in context.chargers}:
        context.chargers.append(selected)
    context.chargers.sort(key=lambda item: float(item.get("route_position_percent") or 0))
    vehicle_error = _vehicle_error(context)
    if vehicle_error:
        raise AppError("needs_vehicle", vehicle_error)
    _run_ev_plan(context)
    return {**_result_base(context), "success": True, "action": "modify_trip", "change": f"Charging stop added near {place}.",
            "charging_stop": selected, "route": context.route, "chargers": context.chargers,
            "ev_plan": context.ev_plan, "ev_status": context.ev_status}


# ---------------------------------------------------------------------------------------------
# Handlers: search
# ---------------------------------------------------------------------------------------------
def _fmt_distance(km: Any) -> str:
    value = float(km)
    return f"{round(value * 1000)} m" if value < 1 else f"{value:g} km"


def _radius_for(action: dict[str, Any], category: str, fallback: float = 5.0) -> float:
    """An explicit radius wins. Otherwise the category's own default: airports are sparse, so 30 km, not 5."""
    value = clamp_radius(action.get("radius_km"), fallback)
    if action.get("radius_explicit"):
        return value
    spec = spec_for(category)
    return max(value, spec.default_radius_km) if spec else value


def _qualifier_note(qualifiers: list[str] | None, category: str) -> str | None:
    """HERE exposes categories, names, addresses and (sparsely) ratings, but nothing about atmosphere or audience."""
    wishes = [q for q in (qualifiers or []) if q]
    if not wishes:
        return None
    joined = wishes[0] if len(wishes) == 1 else ", ".join(wishes[:-1]) + " or " + wishes[-1]
    return (f"HERE has no data on whether a place is {joined}, so I haven't filtered or ranked on that. "
            f"These are {_category_noun(category, 2)} ranked by category match and distance.")


NEAREST_MAX_KM = 200.0
NEAREST_MAX_STEPS = 6
NEAREST_SHOWN = 8


async def _nearby(
    points: list[dict[str, float]], category: str, radius_km: float, limit: int, search_query: str | None, *, nearest: bool = False,
) -> tuple[dict[str, Any], float]:
    """One HERE search around the points. With ``nearest`` the radius doubles until something is found, so "the nearest
    airport from Puri" is answered even though it is 50 km away. Returns (search, the radius that was used)."""
    if not nearest:
        return await find_nearby_pois_with_source(points, category, radius_km, limit, search_query), radius_km
    ceiling = NEAREST_MAX_KM if spec_for(category) is not None else 100.0  # free-text discover is limited to a 100 km circle
    radius, steps = radius_km, 0
    while True:
        steps += 1
        search = await find_nearby_pois_with_source(points, category, radius, min(limit, NEAREST_SHOWN), search_query)
        if search.get("pois") or search.get("source_status") == "unavailable" or radius >= ceiling or steps >= NEAREST_MAX_STEPS:
            return search, radius
        radius = min(radius * 2, ceiling)


def _search_result(context: TripContext, action_name: str, category: str, search: dict[str, Any], **extra: Any) -> dict[str, Any]:
    pois = dedupe_places(search.get("pois") or [])
    if category == "hotel":
        pois = real_stays(pois)  # drop event / promotion listings that HERE files under hotels
    context.pois = pois
    context.pois_label = extra.get("label")
    qualifiers = extra.pop("qualifiers", None) or []
    result = {
        **_result_base(context), "success": True, "action": action_name, "category": category,
        "pois": pois, "source_status": search.get("source_status", "verified"),
        "source": search.get("source"), "source_error": search.get("source_error"),
        "fallback_available": search.get("source_status") == "unavailable",
        "search": search.get("search") or {},
        "qualifiers": qualifiers, "qualifier_note": _qualifier_note(qualifiers, category),
    }
    if search.get("coverage"):
        result["coverage"] = search["coverage"]
    result.update({k: v for k, v in extra.items() if k != "label"})
    return result


async def _h_find_near_location(context: TripContext, action: dict[str, Any], message: str) -> dict[str, Any]:
    text = clean_text(action.get("place"))
    if is_me_place(text) and not context.user_location:
        return _needs_location_result(context)
    name, lat, lon = await _resolve_search_center(context, text)
    category = str(action.get("category") or "place").replace(" ", "_")
    radius_km = _radius_for(action, category, 5.0)
    limit = clamp_limit(action.get("limit"))
    search_query = action.get("query") or None
    # Tourist sites are spread out; with no explicit radius use 15 km rather than an empty 5 km search.
    if category == "tourist_attraction" and not action.get("radius_explicit"):
        radius_km = max(radius_km, 15.0)
    nearest = bool(action.get("nearest"))
    search, radius_km = await _nearby([{"latitude": lat, "longitude": lon}], category, radius_km, limit, search_query, nearest=nearest)
    _set_focus(context, name, lat, lon, "place")
    return _search_result(
        context, "find_pois_near_location", category, search,
        scope="location", query=text, location_query=text, search_query=search_query, nearest=nearest,
        radius_km=radius_km, limit=limit, center={"latitude": lat, "longitude": lon, "name": name},
        label=f"{_category_noun(category, 2)} near {name}", qualifiers=action.get("qualifiers"),
    )


async def _h_find_in_area(context: TripContext, action: dict[str, Any], message: str) -> dict[str, Any]:
    """"airports in Odisha": every place HERE classifies under the category inside the state, district or city."""
    area_text = clean_text(action.get("area"))
    category = str(action.get("category") or "place").replace(" ", "_")
    limit = clamp_limit(action.get("limit"))
    search_query = action.get("query") or None
    area, ambiguous = (None, []) if spec_for(category) is None else await resolve_area_candidate(area_text)
    if ambiguous:
        options = [str(c.get("display_name")) for c in ambiguous[:4]]
        raise AppError("place_ambiguous", f"“{area_text}” matches more than one place: {'; '.join(options)}. Which one do you mean?",
                       data={"query": area_text, "options": options})
    if area is None:
        # Not a state, district or city (a neighbourhood, a landmark) or a kind of place HERE cannot list by area:
        # search near it instead, and say so.
        near = {"intent": "find_pois_near_location", "place": area_text, "category": category, "query": search_query,
                "radius_km": clamp_radius(action.get("radius_km"), 5.0), "radius_explicit": False, "limit": limit,
                "qualifiers": action.get("qualifiers")}
        result = await _h_find_near_location(context, near, message)
        result["area_fallback"] = area_text
        return result
    # "Best restaurants in Mysuru": ratings are fetched for every map tile and the list is ranked BEFORE it is cut to the top results,
    # otherwise the 80 nearest to the city centre would be ranked instead of the 80 best. Plain area lists skip this (it costs more).
    wants_ranking = bool(_RANK_WORDS.search(message)) and category in {"restaurant", "cafe", "hotel", "tourist_attraction"}
    if wants_ranking:
        search = await find_area_pois_with_source(area, category, 100, search_query, rich=True)
    else:
        search = await find_area_pois_with_source(area, category, limit, search_query)
    if wants_ranking and search.get("pois"):
        search = {**search, "pois": rank_places(search["pois"])[:limit]}
    center = area["center"]
    _set_focus(context, area["name"], center["latitude"], center["longitude"], "place")
    public = {k: area.get(k) for k in ("name", "kind", "display_name", "state", "bbox")}
    return _search_result(
        context, "find_pois_in_area", category, search,
        scope="area", area=public, query=search_query or category, search_query=search_query, limit=limit,
        center={"latitude": center["latitude"], "longitude": center["longitude"], "name": area["name"]},
        label=f"{_category_noun(category, 2)} in {area['name']}", qualifiers=action.get("qualifiers"),
    )


async def _search_around_point(
    context: TripContext, action: dict[str, Any], category: str, *, lat: float, lon: float, name: str, scope: str,
    default_radius: float, label_where: str, **extra: Any,
) -> dict[str, Any]:
    radius_km = _radius_for(action, category, default_radius)
    limit = clamp_limit(action.get("limit"))
    search_query = action.get("query") or None
    nearest = bool(action.get("nearest"))
    search, radius_km = await _nearby([{"latitude": lat, "longitude": lon}], category, radius_km, limit, search_query, nearest=nearest)
    _set_focus(context, name, lat, lon, "midpoint" if scope == "midpoint" else "place")
    return _search_result(
        context, "find_pois", category, search, scope=scope, radius_km=radius_km, query=search_query or category, nearest=nearest,
        search_query=search_query, limit=limit, target=name, center={"latitude": lat, "longitude": lon, "name": name},
        label=f"{_category_noun(category, 2)} {label_where}", qualifiers=action.get("qualifiers"), **extra,
    )


async def _find_at_midpoint(context: TripContext, action: dict[str, Any], category: str) -> dict[str, Any]:
    midpoint = _route_midpoint(context, float(action.get("fraction") or 0.5))
    names = " → ".join(context.trip_names())
    result = await _search_around_point(
        context, action, category, lat=midpoint["latitude"], lon=midpoint["longitude"], name="the halfway point of your trip",
        scope="midpoint", default_radius=5.0, label_where=f"near the halfway point of {names}",
        midpoint={"distance_from_start_km": midpoint["distance_from_start_km"], "route_km": midpoint["route_km"]},
        reference_note=(f"Distances are measured from the halfway point of the route, about {midpoint['distance_from_start_km']:g} km "
                        f"of {midpoint['route_km']:g} km from {context.stops[0].name}."),
    )
    # Also say how far each place is from the route itself, so a detour is visible.
    route_coords = [list(pair[:2]) for pair in (context.route.get("geometry") or {}).get("coordinates", [])]
    for poi in result["pois"]:
        try:
            distance = calculate_distance_to_route_km(route_coords, float(poi["latitude"]), float(poi["longitude"]))
        except (KeyError, TypeError, ValueError):
            distance = None
        if distance is not None:
            poi["distance_from_route_km"] = distance
    return result


def _needs_location_result(context: TripContext) -> dict[str, Any]:
    """A friendly question instead of a failure: the browser offers a "Share my location" button on it."""
    return {
        **_result_base(context), "success": True, "action": "clarify", "needs_location": True,
        "question": "I can't see your location yet. Tap the location icon on the map to share it, or tell me a city or address and I'll search there.",
    }


async def _find_near_me(context: TripContext, action: dict[str, Any], category: str) -> dict[str, Any]:
    if not context.user_location:
        return _needs_location_result(context)
    lat, lon = _user_point(context)
    return await _search_around_point(
        context, action, category, lat=lat, lon=lon, name="your location", scope="me", default_radius=5.0,
        label_where="near you", reference_note="Distances are measured from the location you shared.",
    )


async def _h_find_pois(context: TripContext, action: dict[str, Any], message: str) -> dict[str, Any]:
    category = str(action.get("category") or "restaurant").replace(" ", "_")
    scope = str(action.get("scope") or "route")
    radius_km = clamp_radius(action.get("radius_km"), 3.0)
    limit = clamp_limit(action.get("limit"))
    search_query = action.get("query") or None
    qualifiers = action.get("qualifiers")

    if category == "ev_charging":
        if not context.ev_mode:
            raise AppError("invalid_request", "EV charging search is part of the optional EV Planner. Switch it on from the EV Planner panel.")
        return await _find_ev_chargers(context, scope, action)

    if scope == "midpoint":
        return await _find_at_midpoint(context, action, category)
    if scope == "me":
        return await _find_near_me(context, action, category)

    if scope == "route":
        if len(context.stops) < 2:
            raise AppError("needs_trip", "Plan a route first, then I can search along it. Or tell me a place, like “restaurants in Pune”.")
        if not context.route:
            await _apply_route(context)
        corridor = max(context.corridor_km, radius_km) if action.get("radius_km") else context.corridor_km
        spec = spec_for(category)
        if not action.get("radius_explicit") and spec is not None and spec.default_radius_km > corridor:
            corridor = min(spec.default_radius_km, 25.0)  # sparse categories (airports): a 5 km corridor would find nothing
        search = await find_route_pois_with_source(context.route["geometry"], category, corridor, limit, search_query)
        label = f"{_category_noun(category, 2)} along your route"
        return _search_result(context, "find_pois", category, search, scope="route", radius_km=corridor, query=search_query or category,
                              search_query=search_query, label=label, qualifiers=qualifiers)

    radius_km = _radius_for(action, category, 3.0)
    if scope == "all_stops":
        if not context.stops:
            raise AppError("needs_trip")
        points = [{"latitude": s.latitude, "longitude": s.longitude} for s in context.stops]
        target = "your stops"
    else:
        ref = resolve_scope(context, scope, action.get("index"))
        points = [ref.point()]
        target = ref.name
        _set_focus(context, ref.name, ref.latitude, ref.longitude, ref.kind, ref.index, ref.detail)
    nearest = bool(action.get("nearest"))
    search, radius_km = await _nearby(points, category, radius_km, limit, search_query, nearest=nearest)
    extra: dict[str, Any] = {"nearest": nearest}
    if len(points) == 1:
        extra["center"] = {"latitude": points[0]["latitude"], "longitude": points[0]["longitude"], "name": target}
    return _search_result(context, "find_pois", category, search, scope=scope, radius_km=radius_km, query=search_query or category,
                          search_query=search_query, target=target, label=f"{_category_noun(category, 2)} near {target}",
                          qualifiers=qualifiers, **extra)


async def _h_find_near_anchor(context: TripContext, action: dict[str, Any], message: str) -> dict[str, Any]:
    """"restaurants within 500 m of a hotel near Baga Beach".

    The hotel is a stepping stone. Several hotels may fit, and choosing one for the user would silently change the
    question, so they are listed and the user picks. A single candidate is used and named.
    """
    category = str(action.get("category") or "restaurant").replace(" ", "_")
    anchor_category = str(action.get("anchor_category") or "hotel").replace(" ", "_")
    place_text = clean_text(action.get("place"))
    name, lat, lon = await _resolve_search_center(context, place_text)
    anchor_radius = clamp_radius(action.get("anchor_radius_km"), 2.0)
    anchor_search = await find_nearby_pois_with_source([{"latitude": lat, "longitude": lon}], anchor_category, anchor_radius, 10, None)
    anchors = anchor_search.get("pois") or []
    anchor_label = f"{_category_noun(anchor_category, 2)} near {name}"
    shared = dict(scope="anchor", anchor={"category": anchor_category, "place": name, "radius_km": anchor_radius},
                  target_category=category, requested_radius_km=clamp_radius(action.get("radius_km"), 0.5), query=place_text,
                  qualifiers=action.get("qualifiers"))
    if anchor_search.get("source_status") == "unavailable" or not anchors:
        result = _search_result(context, "find_pois_near_anchor", anchor_category, anchor_search, radius_km=anchor_radius, label=anchor_label, **shared)
        result["anchor_step"] = "unavailable" if anchor_search.get("source_status") == "unavailable" else "none_found"
        return result
    if len(anchors) == 1:
        chosen = anchors[0]
        lat2, lon2 = float(chosen["latitude"]), float(chosen["longitude"])
        _set_focus(context, str(chosen.get("name")), lat2, lon2, "result", 0)
        follow = {**action, "radius_km": clamp_radius(action.get("radius_km"), 0.5), "radius_explicit": True}
        result = await _search_around_point(
            context, follow, category, lat=lat2, lon=lon2, name=str(chosen.get("name") or anchor_category), scope="result",
            default_radius=0.5, label_where=f"near {chosen.get('name')}", anchor={"category": anchor_category, "place": name, "name": chosen.get("name"), "only_candidate": True},
        )
        return result
    result = _search_result(context, "find_pois_near_anchor", anchor_category, anchor_search, radius_km=anchor_radius, label=anchor_label, **shared)
    result["anchor_step"] = "choose"
    anchor_stats = anchor_search.get("search") or {}
    # A full HERE page (100) or more matches than are listed means these are only the nearest ones.
    more_exist = bool(anchor_stats.get("page_full")) or int(anchor_stats.get("matched") or 0) > len(anchors)
    found = (f"Here are the nearest {len(anchors)} {_category_noun(anchor_category, 2)} within {_fmt_distance(anchor_radius)} of {name} "
             "(there are more than I can list)." if more_exist
             else f"I found {len(anchors)} {_category_noun(anchor_category, 2)} within {_fmt_distance(anchor_radius)} of {name}.")
    result["question"] = (f"{found} "
                          f"Which one should I search around for {_category_noun(category, 2)} within {_fmt_distance(clamp_radius(action.get('radius_km'), 0.5))}? "
                          "Tell me its number or name.")
    # Completed by the next short reply that picks one of the listed places (see _interpret).
    action["pending"] = {"intent": "find_pois", "scope": "result", "category": category,
                         "radius_km": clamp_radius(action.get("radius_km"), 0.5), "radius_explicit": True,
                         "limit": clamp_limit(action.get("limit")), "query": action.get("query") or None,
                         "qualifiers": action.get("qualifiers") or []}
    return result


async def _find_ev_chargers(context: TripContext, scope: str, action: dict[str, Any]) -> dict[str, Any]:
    if scope == "route":
        if not context.chargers:
            raise AppError("needs_trip", "Plan a route with the EV Planner on to see chargers along it.")
        pois = [_charger_as_poi(c) for c in context.chargers]
    else:
        ref = resolve_scope(context, scope, action.get("index"))
        pois = [_charger_as_poi(c) for c in context.chargers[:40]] if scope in {"destination", "origin", "stop"} else [_charger_as_poi(ref.detail or {})]
    search = {"pois": pois, "source_status": "verified", "source": EV_DATASET_LABEL, "source_error": None}
    return _search_result(context, "find_pois", "ev_charging", search, scope=scope, radius_km=0.0, query="EV charging",
                          label="EV charging stations")


async def _h_show_pois(context: TripContext, action: dict[str, Any], message: str) -> dict[str, Any]:
    return {**_result_base(context), "success": True, "action": "show_pois", "pois": context.pois, "count": len(context.pois), "pois_label": context.pois_label}


async def _h_route_info(context: TripContext, action: dict[str, Any], message: str) -> dict[str, Any]:
    if not context.route or len(context.stops) < 2:
        raise AppError("needs_trip", "There's no active route yet. Tell me where you're going and I'll work it out.")
    route = context.route
    positions = route.get("stop_positions") or []
    target = action.get("target") or "total"
    answer: dict[str, Any] = {"success": True, "action": "route_info", "target": target,
                              "route": route, **_result_base(context)}
    if target == "total":
        answer.update(distance_km=route.get("distance_km"), duration_minutes=route.get("duration_minutes"), to="your destination")
        return answer
    if target == "named":
        i = stop_index_by_name(context, action.get("place") or "")
        if i is None:
            raise AppError("reference_unresolved", f"“{action.get('place')}” isn't one of your stops. Your route is {' → '.join(context.trip_names())}.")
        ref = ResolvedPlace(context.stops[i].name, context.stops[i].latitude, context.stops[i].longitude, "stop", i)
    else:
        ref = resolve_scope(context, target, action.get("index"))
    if ref.index is None or ref.index >= len(positions):
        raise AppError("reference_unresolved")
    position = positions[ref.index]
    answer.update(distance_km=position.get("distance_from_start_km"), duration_minutes=position.get("duration_from_start_minutes"),
                  to=ref.name, stop_number=ref.index + 1, measured_from=context.stops[0].name)
    _set_focus(context, ref.name, ref.latitude, ref.longitude, ref.kind, ref.index)
    return answer


# ---------------------------------------------------------------------------------------------
# Handlers: optional EV Planner (unchanged calculations, honest status)
# ---------------------------------------------------------------------------------------------
def _enable_ev(context: TripContext) -> None:
    context.ev_mode = True
    if context.vehicle_type is None:
        context.vehicle_type = "electric"
    if context.ev_status == "not_requested":
        context.ev_status = "needs_vehicle"


def disable_ev(context: TripContext) -> None:
    """Switch the optional EV Planner off and drop every EV-derived result."""
    context.ev_mode = False
    context.ev_status = "not_requested"
    context.ev_plan = None
    context.chargers = []
    context.pois = [p for p in context.pois if p.get("category") != "ev_charging"]
    context.weather = {**context.weather, "charging_stops": {}} if context.weather else {}


async def _h_ev_plan(context: TripContext, action: dict[str, Any], message: str) -> dict[str, Any]:
    if len(context.stops) < 2 or not context.route:
        raise AppError("needs_trip", "Tell me where you're driving first, then I can plan charging for it.")
    _enable_ev(context)
    result = await _recompute_ev(context)
    result["action"] = "ev_plan"
    return _route_summary_extras(context, result)


async def _h_update_vehicle(context: TripContext, action: dict[str, Any], message: str) -> dict[str, Any]:
    updates = action.get("vehicle_updates") or {}
    _update_vehicle(context, updates)
    wants_ev = context.ev_mode or bool(re.search(r"\b(?:ev|electric|battery|charge|charging|range|kwh)\b", message, flags=re.IGNORECASE))
    result: dict[str, Any] = {**_result_base(context), "success": True, "action": "update_vehicle", "vehicle": context.to_dict()["vehicle"]}
    if wants_ev:
        _enable_ev(context)
        if context.route and len(context.stops) >= 2:
            result.update(await _recompute_ev(context))
            result["action"] = "update_vehicle"
    return _route_summary_extras(context, result)


async def _h_update_settings(context: TripContext, action: dict[str, Any], message: str) -> dict[str, Any]:
    _apply_settings(context, action.get("settings") or {})
    result: dict[str, Any] = {
        **_result_base(context), "success": True, "action": "update_settings",
        "settings": {"reserve_percent": context.reserve_percent, "target_charge_percent": context.target_charge_percent, "corridor_km": context.corridor_km},
    }
    if context.route and len(context.stops) >= 2 and context.ev_mode:
        result.update(await _recompute_ev(context))
        result["action"] = "update_settings"
    return _route_summary_extras(context, result)


async def _h_simulate(context: TripContext, action: dict[str, Any], message: str) -> dict[str, Any]:
    if len(context.stops) < 2:
        raise AppError("needs_trip", "Plan a trip before running an EV what-if.")
    field = action.get("field", "battery_percent")
    if field == "battery_percent":
        _update_vehicle(context, {"current_battery_percent": action.get("value")})
    elif field in {"reserve_percent", "target_charge_percent"}:
        _apply_settings(context, {field: action.get("value")})
    else:
        raise AppError("invalid_request", "I can change the battery %, reserve % or charging target in a what-if.")
    _enable_ev(context)
    result = await _recompute_ev(context)
    result.update(action="simulate", simulated_field=field, simulated_value=action.get("value"))
    return _route_summary_extras(context, result)


async def _h_show_charging_stop(context: TripContext, action: dict[str, Any], message: str) -> dict[str, Any]:
    ref = resolve_scope(context, "charging_stop", action.get("index"))
    plan_stops = charging_stops(context)
    selected = plan_stops[ref.index]
    charger = dict(selected.get("charger") or {})
    weather = (context.weather.get("charging_stops") or {}).get(str(charger.get("id") or f"stop-{ref.index + 1}"))
    _set_focus(context, ref.name, ref.latitude, ref.longitude, "charging_stop", ref.index, charger)
    return {
        **_result_base(context), "success": True, "action": "show_charging_stop",
        "charging_stop_index": ref.index, "charging_stop_number": ref.index + 1, "charging_stop_count": len(plan_stops),
        "charging_stop": selected, "charger": charger,
        "weather_at_charger": weather or {"available": False, "provider": "Open-Meteo", "message": "Weather data is not available for this station."},
        "ev_plan": context.ev_plan, "source": charger.get("source") or EV_DATASET_LABEL,
    }


async def _h_recommend_charger(context: TripContext, action: dict[str, Any], message: str) -> dict[str, Any]:
    if len(context.stops) < 2:
        raise AppError("needs_trip", "Plan a trip before asking for a recommended charging stop.")
    vehicle_error = _vehicle_error(context)
    if vehicle_error:
        raise AppError("needs_vehicle", vehicle_error)
    _enable_ev(context)
    if context.route:
        await _recompute_ev(context)
    plan = context.ev_plan or {}
    stops = plan.get("charging_stops", [])
    if not stops:
        return {**_result_base(context), "success": bool(plan.get("trip_possible")), "action": "recommend_charger",
                "recommended_charger": None, "ev_plan": plan, "ev_status": context.ev_status,
                "message": "No charging stop is needed, or no feasible one was found with the current battery, range and reserve."}
    first = stops[0]
    charger = dict(first.get("charger", {}))
    charger["source"] = EV_DATASET_LABEL
    return {**_result_base(context), "success": True, "action": "recommend_charger", "recommended_charger": charger,
            "charging_stop": first, "weather_at_charger": (context.weather.get("charging_stops") or {}).get(str(charger.get("id") or "")),
            "ev_plan": plan, "ev_status": context.ev_status}


async def _h_search_charger_nearby(context: TripContext, action: dict[str, Any], message: str) -> dict[str, Any]:
    place = clean_text(action.get("place"))
    name, lat, lon = await _resolve_search_center(context, place)
    radius_km = max(1.0, min(float(action.get("radius_km") or 25.0), 100.0))
    local = search_tata_chargers_nearby(lat, lon, radius_km, 50)
    try:
        here = await search_here_ev_chargers(lat, lon, radius_km, 50)
    except AppError as exc:
        logger.warning("event=here_ev_charger_search_failed error=%s", exc.code)
        here = []
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for charger in sorted(local + here, key=lambda item: float(item.get("distance_from_reference_km", 999999))):
        key = f"{round(float(charger['latitude']), 5)}:{round(float(charger['longitude']), 5)}"
        if key not in seen:
            seen.add(key)
            deduped.append(charger)
    context.chargers = deduped
    context.pois = [_charger_as_poi(c) for c in deduped]
    context.pois_label = f"EV charging stations near {name}"
    _set_focus(context, name, lat, lon, "place")
    return {**_result_base(context), "success": True, "action": "search_charger_nearby", "query": place,
            "location": {"latitude": lat, "longitude": lon, "display_name": name}, "radius_km": radius_km,
            "chargers": deduped, "pois": context.pois, "source": f"{EV_DATASET_LABEL} + HERE EV Charging Search"}


async def _h_compound(context: TripContext, action: dict[str, Any], message: str) -> dict[str, Any]:
    places = action.get("places") or []
    if len(places) < 2:
        raise AppError("needs_trip", "Tell me an origin and destination for the trip.")
    plan = {k: action.get(k) for k in ("places", "ev_required", "vehicle_updates", "settings") if action.get(k)}
    plan["intent"] = "plan_trip"
    if context.route and _is_active_trip(context, places) and not action.get("ev_required"):
        # "halfway between Bengaluru and Mysuru" while that trip is already planned: use it, don't re-route.
        route_result = {"success": True, "action": "plan_trip", "route": context.route}
        _route_summary_extras(context, route_result)
    else:
        route_result = await _h_plan_trip(context, plan, message)
    if route_result.get("needs_vehicle"):
        return {**route_result, "action": "compound_trip_request"}
    recommended = recommended_stop = None
    if action.get("recommend_charger") and context.ev_plan:
        stops = context.ev_plan.get("charging_stops", [])
        if stops:
            recommended_stop = stops[0]
            recommended = dict(recommended_stop.get("charger", {}))
            recommended["source"] = EV_DATASET_LABEL
    groups: list[dict[str, Any]] = []
    combined: list[dict[str, Any]] = []
    for request in action.get("poi_requests", []):
        category = str(request.get("category") or "restaurant").replace(" ", "_")
        scope = request.get("scope") or "route"
        try:
            sub = {"category": category, "scope": scope, "index": request.get("index"),
                   "radius_km": request.get("radius_km"), "radius_explicit": request.get("radius_explicit"),
                   "limit": request.get("limit"), "query": request.get("query"),
                   "qualifiers": request.get("qualifiers"), "fraction": request.get("fraction")}
            found = await _h_find_pois(context, sub, message)
            # Keep everything the reply needs to describe THIS search honestly (scope, radius, notes, coverage).
            group = {**request, **{k: found[k] for k in ("scope", "radius_km", "target", "center", "reference_note", "qualifier_note",
                                                          "search", "coverage", "midpoint") if k in found},
                     "category": category, "scope": found.get("scope", scope), "pois": found.get("pois", []),
                     "source_status": found.get("source_status"), "source": found.get("source"), "source_error": found.get("source_error")}
        except AppError as exc:
            logger.warning("event=poi_search_failed scope=%s category=%s error=%s", safe_token(scope), safe_token(category), exc.code)
            group = {**request, "category": category, "scope": scope, "pois": [], "source_status": "unavailable",
                     "source": "HERE Search API + OpenStreetMap / Overpass", "source_error": exc.code, "fallback_available": False}
        groups.append(group)
        combined.extend(group["pois"])
    context.pois = combined
    context.pois_label = "places for your trip"
    return {**route_result, "action": "compound_trip_request", "route": context.route, "ev_plan": context.ev_plan,
            "recommended_charger": recommended, "charging_stop": recommended_stop, "poi_groups": groups, "pois": combined,
            "weather_at_charger": ((context.weather.get("charging_stops") or {}).get(str((recommended or {}).get("id") or "")) if recommended else None),
            "poi_source_failures": [g.get("category") for g in groups if g.get("source_status") == "unavailable"]}


# ---------------------------------------------------------------------------------------------
# Trip planning: itineraries, destination ideas, directions; weather and ranking on results
# ---------------------------------------------------------------------------------------------
async def _weather_for(lat: float, lon: float, *, days: int = 5, climate: bool = False) -> dict[str, Any] | None:
    if not weather_enabled():
        return None
    return await weather_bundle(lat, lon, forecast_days=days, climate=climate, timeout=4.0)


def _weather_point(context: TripContext, result: dict[str, Any]) -> tuple[str, float, float, bool] | None:
    """Where a result's weather should be read, and whether the climate by month is worth showing: (name, lat, lon, climate)."""
    action = result.get("action")
    center = result.get("center") if isinstance(result.get("center"), dict) else {}
    area_center = ((result.get("area") or {}).get("center") or {}) if isinstance(result.get("area"), dict) else {}
    focus = context.focus or {}
    try:
        if action in {"find_pois_near_location", "find_pois_near_anchor", "destination_guide"} and center.get("latitude") is not None:
            return str(center.get("name") or result.get("place") or "there"), float(center["latitude"]), float(center["longitude"]), action == "destination_guide"
        if action == "find_pois_in_area" and area_center.get("latitude") is not None:
            return str((result.get("area") or {}).get("name") or "the area"), float(area_center["latitude"]), float(area_center["longitude"]), False
        if action in {"find_pois", "ask", "geocode"} and focus.get("latitude") is not None and focus.get("kind") in {"place", "stop", "origin", "destination"}:
            if action == "find_pois" and result.get("scope") in {"route", "midpoint", "me"}:
                return None
            return str(focus.get("name") or "there"), float(focus["latitude"]), float(focus["longitude"]), False
        if action in {"plan_trip", "modify_trip", "set_trip_options"} and len(context.stops) >= 2:
            last = context.stops[-1]
            return last.name, float(last.latitude), float(last.longitude), True
    except (KeyError, TypeError, ValueError):
        return None
    return None


_RANK_WORDS = re.compile(
    r"\b(?:best|top|good|great|finest|popular|famous|recommend\w*|suggest\w*|worth|highly[\s-]*rated|well[\s-]*rated|must[\s-]*(?:try|visit)|favou?rite)\b",
    re.IGNORECASE)
_RANKABLE = {"find_pois_near_location", "find_pois_in_area", "find_pois"}
MIN_SOLID_REVIEWS = 10      # a rating from fewer people than this is not used to rank
MIN_RATED_FOR_RANKING = 3   # and at least this many places need one, or the "ranking" would rest on a handful of stray ratings


def _apply_ranking(context: TripContext, result: dict[str, Any], message: str) -> None:
    """"Best restaurants in Mysuru": order by Tripadvisor rating (weighted by how many people rated), not just by distance."""
    if result.get("action") not in _RANKABLE or result.get("nearest") or not _RANK_WORDS.search(message):
        return
    pois = result.get("pois") or []
    # Ranking by rating is only honest when enough places carry a rating that means something. A rating from one or two people is
    # not evidence, and HERE's Tripadvisor coverage is patchy: when it is thin, keep the distance order and say why.
    solid = sum(1 for p in pois if isinstance(p.get("rating"), (int, float)) and int(p.get("rating_count") or 0) >= MIN_SOLID_REVIEWS)
    if len(pois) < 2 or solid < MIN_RATED_FOR_RANKING:
        if len(pois) >= 2:
            have = "None of these" if solid == 0 else f"Only {solid} of these"
            result["ranking_note"] = (f"{have} {len(pois)} places have enough Tripadvisor reviews to rank on, so they are in order of "
                                      "distance, not quality. HERE doesn't carry ratings for most places here.")
        return
    ranked = rank_places(pois)
    result["pois"], context.pois, result["ranked_by"] = ranked, ranked, "rating"
    best = [float(p["rating"]) for p in ranked[:5] if isinstance(p.get("rating"), (int, float)) and int(p.get("rating_count") or 0) >= MIN_SOLID_REVIEWS]
    if best and max(best) < 3.5:
        # "Best" of a poorly rated or thinly covered set is not good: say so rather than present a 2.9 as a recommendation.
        result["ranking_note"] = ("The highest-rated of these on Tripadvisor score under 3.5 out of 5, and many places here have no rating in HERE's "
                                  "data at all, so check recent reviews elsewhere before you choose.")


async def _decorate_result(context: TripContext, result: dict[str, Any], message: str) -> None:
    """Weather and ranking for a result. Both are extras: any failure here leaves the result as the handler made it."""
    try:
        _apply_ranking(context, result, message)
        if "place_weather" in result:
            return
        point = _weather_point(context, result)
        if point:
            name, lat, lon, climate = point
            bundle = await _weather_for(lat, lon, days=7 if climate else 5, climate=climate)
            if bundle:
                result["place_weather"] = {**bundle, "place": name}
    except Exception as exc:  # never let an extra break the answer
        logger.warning("event=decorate_failed error=%s", describe_exception(exc))


def _normalise(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()


def _fmt_route_note(route: dict[str, Any] | None) -> str | None:
    if not route or route.get("distance_km") is None:
        return None
    return f"{round(float(route['distance_km']))} km, about {_fmt_minutes(route.get('duration_minutes'))} by road"


async def _h_trip_itinerary(context: TripContext, action: dict[str, Any], message: str) -> dict[str, Any]:
    """A day-by-day plan for one destination. HERE supplies the places, the road route and the weather; the model arranges
    them for these travellers and writes the advice, pointing at places by id only."""
    dest_text = clean_text(action.get("destination"))
    origin_text = clean_text(action.get("origin")) or None
    days = int(action.get("days") or DEFAULT_ITINERARY_DAYS)
    party, interests, budget = list(action.get("party") or []), list(action.get("interests") or []), action.get("budget")
    month = int(action.get("month") or date.today().month)

    area, ambiguous = await resolve_area_candidate(dest_text)
    if ambiguous:
        options = [str(c.get("display_name")) for c in ambiguous[:4]]
        raise AppError("place_ambiguous", f"“{dest_text}” matches more than one place: {'; '.join(options)}. Which one do you mean?",
                       data={"query": dest_text, "options": options})
    if area is not None and area.get("kind") in {"state", "region"} and not action.get("base_of"):
        # A whole state is too wide for one day-by-day plan. Find good bases (the model names them, HERE verifies them), plan around
        # the best fit, and tell the user which others they can ask for. Without a verified base, show the bases to pick from.
        suggestions = await _h_suggest_destinations(
            context, {"intent": "suggest_destinations", "origin": origin_text, "region": area["name"], "kind": None, "days": days, "party": party,
                      "interests": interests, "month": action.get("month"), "within_km": None, "within_minutes": None},
            message, note=f"{area['name']} is too big for one day-by-day plan, so here are good places to base yourself. Pick one and I'll plan the days.")
        # Only bases inside the region asked for: the model may be drawn towards the starting point ("Goa from Pune" -> Lonavala).
        wanted = _normalise(area["name"])
        bases = [d for d in (suggestions.get("destinations") or []) if d.get("name") and _normalise(str(d.get("region") or "")) == wanted]
        if not bases:
            return suggestions
        best = min(bases, key=lambda d: d.get("fit_rank", 99))
        others = [d["name"] for d in bases if d is not best][:3]
        planned = await _h_trip_itinerary(context, {**action, "destination": f"{best['name']}, {area['name']}", "base_of": area["name"]}, message)
        planned["base_note"] = (f"{area['name']} is wide, so I based this plan around {best['name']}."
                                + (f" Ask me to plan around {', '.join(others[:-1]) + ' or ' + others[-1] if len(others) > 1 else others[0]} for a different feel." if others else ""))
        return planned
    if area is not None:
        name, lat, lon = area["name"], float(area["center"]["latitude"]), float(area["center"]["longitude"])
        region = area.get("state") if area.get("kind") == "city" else area.get("display_name")
        radius = _guide_radius_km(area)
    else:
        name, lat, lon = await _resolve_search_center(context, dest_text)
        region, radius = None, 12.0

    origin_place = await _resolve_place(origin_text, "origin") if origin_text else None
    centre = [{"latitude": lat, "longitude": lon}]
    route_job = None
    if origin_place is not None:
        context.stops = [origin_place, PlaceContext(name=name, display_name=f"{name}, {region}" if region else name, latitude=lat, longitude=lon, role="destination")]
        route_job = _apply_route(context)
    elif not (context.route and context.stops and _normalise(context.stops[-1].name) == _normalise(name)):
        context.stops = []
        context.clear_derived()

    jobs: list[Any] = [
        find_destination_highlights_with_source(lat, lon, radius, 40),
        _nearby(centre, "hotel", min(radius, 8.0), 30, None),
        _nearby(centre, "restaurant", min(radius, 6.0), 30, None),
        _weather_for(lat, lon, days=7, climate=True),
    ]
    if route_job is not None:
        jobs.append(route_job)
    out = await asyncio.gather(*jobs, return_exceptions=True)

    def places_of(item: Any) -> list[dict[str, Any]]:
        search = item[0] if isinstance(item, tuple) else item
        return list(search.get("pois") or []) if isinstance(search, dict) else []

    provider_error = next((o for o in out[:3] if isinstance(o, AppError) and o.code == "provider_not_configured"), None)
    if provider_error is not None:
        raise provider_error
    attractions = tidy_attractions(places_of(out[0]))
    hotels = rank_places(real_stays(places_of(out[1])))[:MAX_HOTELS]
    restaurants = rank_places(places_of(out[2]))[:MAX_RESTAURANTS]
    bundle = out[3] if isinstance(out[3], dict) else None
    if route_job is not None and isinstance(out[4], Exception):
        logger.info("event=itinerary_route_failed error=%s", describe_exception(out[4]))
        context.stops = []
        context.clear_derived()
        origin_place = None

    route = context.route if origin_place is not None else None
    words = climate_words((bundle or {}).get("climate"), month)
    kit = kit_for_prompt(attractions, hotels, restaurants)
    plan = await write_itinerary(name, region, origin_place.name if origin_place else None, days, party, interests, budget, month,
                                 message, kit, words, _fmt_route_note(route))
    plan, shown = compact_itinerary(plan, attractions, hotels, restaurants)
    if plan is not None:
        conflict = climate_conflicts(plan.get("best_months") or [], (bundle or {}).get("climate"))
        if conflict:
            plan["caveats"] = [conflict, *plan["caveats"]][:4]
            plan["climate_conflict"] = True

    pois = [*shown["attractions"], *shown["restaurants"], *shown["hotels"]]
    context.pois, context.pois_label = pois, f"{days}-day plan for {name}"
    _set_focus(context, name, lat, lon, "place")
    itinerary = {
        "destination": name, "region": region, "origin": origin_place.name if origin_place else None, "days": days, "party": party,
        "interests": interests, "month": month, "budget": budget, "ai": plan is not None, "plan": plan,
        "attractions": shown["attractions"], "more_attractions": shown["more_attractions"], "hotels": shown["hotels"], "restaurants": shown["restaurants"],
        "route": ({"distance_km": route.get("distance_km"), "duration_minutes": route.get("duration_minutes"), "mode": route.get("transport_mode") or "car"}
                  if route else None),
    }
    status = "verified" if (attractions or hotels) else "empty"
    result = {**_result_base(context), "success": True, "action": "trip_itinerary", "itinerary": itinerary, "pois": pois,
              "source": "HERE Search API", "source_status": status, "center": {"latitude": lat, "longitude": lon, "name": name},
              "ai_available": llm_assistant.get_provider().available}
    if bundle:
        result["place_weather"] = {**bundle, "place": name}
    return result


def _destination_reason(kind: str | None, region: str | None, origin: str | None) -> str:
    return " ".join(x for x in (kind, f"in {region}" if region else None, f"near {origin}" if origin else None) if x) or "destinations"


async def _h_suggest_destinations(context: TripContext, action: dict[str, Any], message: str, note: str | None = None) -> dict[str, Any]:
    """"Hill stations near Bangalore": the model names real destinations, HERE finds each one and measures the drive from the
    starting point. A name HERE cannot find is dropped, so nothing in the list is invented."""
    origin_text = clean_text(action.get("origin")) or None
    region, kind = clean_text(action.get("region"), 60) or None, clean_text(action.get("kind"), 40) or None
    month = int(action.get("month") or date.today().month)
    origin_name, origin_point = None, None
    if origin_text:
        if is_me_place(origin_text):
            lat0, lon0 = _user_point(context)
            origin_name, origin_point = "your location", (lat0, lon0)
        else:
            place = await _resolve_place(origin_text, "origin")
            origin_name, origin_point = origin_text, (place.latitude, place.longitude)

    proposals = await propose_destinations(
        message, origin_name, region, kind, action.get("days"), list(action.get("party") or []), list(action.get("interests") or []), month,
        action.get("within_km"), action.get("within_minutes"))
    if proposals is None:
        # No AI (or no usable answer): the nearest honest thing HERE can do is list its notable places around the starting point.
        if origin_point is not None:
            result = await _h_find_near_location(context, {"place": origin_text, "category": "tourist_attraction", "radius_km": 50.0, "radius_explicit": True, "limit": 40}, message)
        elif region:
            result = await _h_find_in_area(context, {"area": region, "category": "tourist_attraction", "limit": 40}, message)
        else:
            raise AppError("invalid_request", "Tell me where you are starting from, for example “hill stations near Bangalore”.")
        result["destinations_note"] = "The AI that suggests destinations isn't available right now, so these are the best-known places HERE lists, not a curated list of destinations."
        return result

    gate = asyncio.Semaphore(4)
    for rank, proposed in enumerate(proposals["destinations"]):
        proposed["fit_rank"] = rank  # the model's best-fit-first order, kept after the list is re-sorted by drive time

    async def verify(item: dict[str, Any]) -> dict[str, Any] | None:
        async with gate:
            looked_up = item.get("map_name") or item["name"]
            query = f"{looked_up}, {item['region']}" if item.get("region") else looked_up
            try:
                subject = await resolve_subject(query, near=origin_point)
            except AppError as exc:
                if exc.code in {"provider_not_configured", "provider_rate_limited"}:
                    raise
                subject = None
            if subject is None:
                return None
            if subject["kind"] == "place":
                found = subject["place"]
                lat, lon, found_name, state = float(found["latitude"]), float(found["longitude"]), str(found.get("name") or ""), found.get("state")
            else:
                if subject.get("ambiguous"):
                    return None
                found = subject["area"]
                lat, lon, found_name, state = float(found["center"]["latitude"]), float(found["center"]["longitude"]), str(found.get("name") or ""), found.get("state")
            if not (name_matches(item["name"], found_name) or (item.get("map_name") and name_matches(item["map_name"], found_name))):
                logger.info("event=destination_dropped reason=name_mismatch")
                return None
            jobs: list[Any] = [find_destination_highlights_with_source(lat, lon, 12.0, 8), _weather_for(lat, lon, days=3)]
            if origin_point is not None:
                jobs.append(calculate_route([{"name": origin_name, "latitude": origin_point[0], "longitude": origin_point[1]},
                                              {"name": item["name"], "latitude": lat, "longitude": lon}]))
            got = await asyncio.gather(*jobs, return_exceptions=True)
            top = (got[0].get("pois") or []) if isinstance(got[0], dict) else []
            bundle = got[1] if isinstance(got[1], dict) else None
            route = got[2] if len(got) > 2 and isinstance(got[2], dict) else None
            hero = next((p for p in top if p.get("photo")), None)
            return {
                **item, "region": item.get("region") or state, "latitude": lat, "longitude": lon, "found_name": found_name,
                "distance_km": round(float(route["distance_km"]), 1) if route and route.get("distance_km") is not None else None,
                "duration_minutes": round(float(route["duration_minutes"])) if route and route.get("duration_minutes") is not None else None,
                "photo": (hero or {}).get("photo"), "photo_name": (hero or {}).get("name"),
                "top_places": [{k: p.get(k) for k in ("name", "rating", "rating_count", "here_category", "latitude", "longitude")} for p in top[:3]],
                "weather": ({"current": bundle.get("current"), "days": (bundle.get("days") or [])[:3]} if bundle else None),
            }

    checked = await asyncio.gather(*(verify(i) for i in proposals["destinations"]), return_exceptions=True)
    for c in checked:
        if isinstance(c, AppError) and c.code in {"provider_not_configured", "provider_rate_limited"}:
            raise c
    verified = [c for c in checked if isinstance(c, dict)]
    max_km, max_min = action.get("within_km"), action.get("within_minutes")
    limited_note = None
    if (max_km or max_min) and any(v["distance_km"] is not None for v in verified):
        inside = [v for v in verified if v["distance_km"] is not None
                  and (not max_km or v["distance_km"] <= max_km) and (not max_min or (v["duration_minutes"] or 1e9) <= max_min)]
        if inside:
            verified = inside
        else:
            limited_note = "None of the suggestions are within the distance you asked for by road, so these are the closest good matches."
    # With a starting point, the nearest by road comes first: the model chose WHAT to suggest, HERE's drive times decide the order.
    # Without drive times (no starting point, or routing failed) the model's best-fit-first order stands.
    if origin_point is not None and any(v["duration_minutes"] is not None for v in verified):
        verified.sort(key=lambda v: (v["duration_minutes"] is None, v["duration_minutes"] or 0))
    verified = verified[:MAX_DESTINATIONS]

    for n, v in enumerate(verified, start=1):
        v["order"] = n
    pois = [{
        "name": v["name"], "latitude": v["latitude"], "longitude": v["longitude"], "category": "tourist_attraction", "here_category": "Destination",
        "address": v.get("region") or "", "marker": "destination", "order": v["order"], "source": "HERE Search API",
        "distance_from_reference_km": v["distance_km"],
    } for v in verified]
    if origin_point is not None:
        pois.insert(0, {"name": origin_name.title() if origin_name != "your location" else "Your location", "latitude": origin_point[0], "longitude": origin_point[1],
                        "category": "place", "marker": "origin", "order": 0, "source": "HERE Search API"})
    context.stops = []
    context.clear_derived()
    context.pois, context.pois_label = pois, f"{kind or 'destinations'} {('near ' + origin_name) if origin_name else ''}".strip()
    if origin_point is not None:
        _set_focus(context, origin_name or "start", origin_point[0], origin_point[1], "place")
    result = {**_result_base(context), "success": True, "action": "suggest_destinations", "pois": pois, "source": "HERE Search API",
              "source_status": "verified" if verified else "empty", "ai_available": True,
              "destinations": verified, "intro": proposals.get("intro") or "", "origin": origin_name, "kind": kind, "region": region,
              "dropped": len(proposals["destinations"]) - len(verified) if not (max_km or max_min) else None,
              "destinations_note": note or limited_note}
    if origin_point is not None:
        result["center"] = {"latitude": origin_point[0], "longitude": origin_point[1], "name": origin_name}
    return result


async def _h_directions(context: TripContext, action: dict[str, Any], message: str) -> dict[str, Any]:
    """"Route to Hotel Hanumanthu": a place on screen (by name or number) or any name, from a said, shared or asked-for start."""
    if action.get("choose") and action.get("index") is None:
        action["pending"] = {"intent": "directions", "choose": True}
        names = [str(p.get("name")) for p in context.pois[:8] if p.get("name")]
        hint = f" For example: {', '.join(names[:3])}." if names else ""
        return {**_result_base(context), "success": True, "action": "clarify", "question": f"Which place would you like a route to? Name it or say its number.{hint}"}
    destination: PlaceContext | None = None
    index = action.get("index")
    if index is not None and context.pois:
        i = len(context.pois) - 1 if index == -1 else index
        if 0 <= i < len(context.pois):
            poi = context.pois[i]
            destination = PlaceContext(name=str(poi.get("name") or "Destination"), display_name=str(poi.get("address") or poi.get("name") or "Destination"),
                                       latitude=float(poi["latitude"]), longitude=float(poi["longitude"]), role="destination")
    if destination is None and action.get("dest_latitude") is not None:
        destination = PlaceContext(name=str(action.get("destination") or "Destination"), display_name=str(action.get("destination") or "Destination"),
                                   latitude=float(action["dest_latitude"]), longitude=float(action["dest_longitude"]), role="destination")
    if destination is None:
        text = clean_text(action.get("destination"))
        known = next((p for p in context.pois if _normalise(p.get("name", "")) == _normalise(text)), None) if text else None
        if known is not None:
            destination = PlaceContext(name=str(known["name"]), display_name=str(known.get("address") or known["name"]), latitude=float(known["latitude"]),
                                       longitude=float(known["longitude"]), role="destination")
        elif text:
            destination = await _resolve_place(text, "destination")
    if destination is None:
        raise AppError("reference_unresolved", "Which place would you like a route to?")

    origin_text = clean_text(action.get("origin"))
    if origin_text:
        origin = await _resolve_place(origin_text, "origin")
    elif context.user_location:
        origin = PlaceContext(name="Your location", display_name="Your location", latitude=float(context.user_location["latitude"]),
                              longitude=float(context.user_location["longitude"]), role="origin")
    else:
        action["pending"] = {"intent": "directions", "choose": False, "destination": destination.name,
                             "dest_latitude": destination.latitude, "dest_longitude": destination.longitude}
        return {**_result_base(context), "success": True, "action": "clarify",
                "question": f"Where are you starting from? Tell me a place (for example “Mysuru railway station”), or share your location with the map button, and I'll route you to {destination.name}."}
    context.stops = [origin, destination]
    result = await _apply_route(context)
    context.focus = None
    return _route_summary_extras(context, result)


_HANDLERS: dict[str, Handler] = {
    "trip_itinerary": _h_trip_itinerary,
    "suggest_destinations": _h_suggest_destinations,
    "directions": _h_directions,
    "help": _h_help,
    "clarify": _h_clarify,
    "general_question": _h_general_question,
    "follow_up_question": _h_follow_up,
    "context": _h_context,
    "geocode": _h_geocode,
    "destination_guide": _h_destination_guide,
    "ask": _h_ask,
    "set_trip_options": _h_set_trip_options,
    "plan_trip": _h_plan_trip,
    "modify_trip": _h_modify_trip,
    "find_pois_near_location": _h_find_near_location,
    "find_pois_in_area": _h_find_in_area,
    "find_pois_near_anchor": _h_find_near_anchor,
    "find_pois": _h_find_pois,
    "show_pois": _h_show_pois,
    "route_info": _h_route_info,
    "ev_plan": _h_ev_plan,
    "update_vehicle": _h_update_vehicle,
    "update_settings": _h_update_settings,
    "simulate": _h_simulate,
    "show_charging_stop": _h_show_charging_stop,
    "recommend_charger": _h_recommend_charger,
    "search_charger_nearby": _h_search_charger_nearby,
    "compound_trip_request": _h_compound,
}


# ---------------------------------------------------------------------------------------------
# Interpretation
# ---------------------------------------------------------------------------------------------
# Intents whose rule-based form is exact enough that the rules decide, even over a plausible LLM guess.
_RULES_WIN = {"modify_trip", "show_charging_stop", "compound_trip_request", "update_vehicle", "update_settings", "simulate", "clarify", "help", "set_trip_options", "ev_plan",
              "find_pois_in_area", "find_pois_near_anchor", "directions"}
# Intents that rewrite the route. A message that says "don't change my route" must never end in one of these.
ROUTE_MUTATING = {"modify_trip", "plan_trip", "compound_trip_request"}


# Rule readings that are only a best guess: a model's reading of the message is better when it has one.
_SOFT_RULES = {"geocode", "general_question", "follow_up_question"}
# A guide or a plain "tell me about" is a template; when the model reads the same question as an open one ("explain the
# pros and cons"), its reading wins. A model that merely names a different place never overrides what the rules read.
_OPEN_QUESTION_RULES = {"destination_guide", "ask"}


_PLANNING = {"trip_itinerary", "suggest_destinations"}


def _yields_to_planning(rule_action: dict[str, Any], message: str) -> bool:
    """Is the rules' reading weak enough for the model's planning reading to replace it?"""
    name = rule_action["intent"]
    if name in _SOFT_RULES or name in {"destination_guide", "ask"}:
        return True
    if name == "find_pois_near_location" and rule_action.get("category") == "place":
        return True  # a text search for a word that is not a kind of business ("hill station", "getaway")
    if name == "plan_trip":
        # "plan a 2 day trip to X with my wife" is more than a route. An EV trip between two places is the route and its charging
        # stops, whatever else the sentence says, so the model's itinerary reading never replaces it.
        return has_itinerary_signals(message) and not rule_action.get("ev_required")
    return False


def _rules_decide(rule_action: dict[str, Any]) -> bool:
    """Exact, tested rule readings outrank a model's guess: route edits, numbered EV stops, and scope words that
    name a computed place (the halfway point, the user's own location)."""
    if rule_action["intent"] in _RULES_WIN:
        return True
    if rule_action["intent"] == "geocode" and rule_action.get("named"):
        return True  # "Where is <a named place>?" is exact; the model's reading must not turn it into a search
    return rule_action["intent"] == "find_pois" and rule_action.get("scope") in {"midpoint", "me"}
EV_ONLY = {"ev_plan", "update_vehicle", "update_settings", "simulate", "show_charging_stop", "recommend_charger", "search_charger_nearby"}


def _context_dict(context: TripContext) -> dict[str, Any]:
    return context.to_dict()


async def _interpret(message: str, context: TripContext) -> tuple[dict[str, Any], str]:
    action, source = await _interpret_message(message, context)
    if action["intent"] in ROUTE_MUTATING and context.stops and forbids_route_change(message):
        # "...but don't change my route": no reading of the message may rewrite the trip.
        logger.info("event=route_change_blocked intent=%s source=%s", safe_token(action["intent"]), source)
        return {"intent": "clarify", "question": "You asked me not to change your route, so I haven't touched it. "
                "Tell me what to look for and where, for example “restaurants near my destination”."}, "guard"
    return action, source


async def _interpret_message(message: str, context: TripContext) -> tuple[dict[str, Any], str]:
    snapshot = _context_dict(context)
    try:
        rule_action = validate_intent(rule_parse(message, snapshot)) or {"intent": "general_question"}
    except TooManyStops:
        raise AppError("invalid_request", f"A trip can have at most {MAX_TRIP_STOPS} stops including the start and end. Please shorten the list.") from None
    rule_name = rule_action["intent"]

    # A half-finished edit from the previous turn ("add a stop between A and B" -> "which place?")
    # is completed by a short answer that names a place.
    pending = (context.last_action or {}).get("pending") if isinstance(context.last_action, dict) else None
    if pending and pending.get("intent") == "directions" and 0 < len(message.split()) <= 8:
        if pending.get("choose"):
            # "a route to <which one?>" -> "the second one" / "2" / a name from the list on screen.
            index = pick_index(message, [str(poi.get("name") or "") for poi in context.pois])
            completed = validate_intent({"intent": "directions", "index": index}) if index is not None else None
        else:
            # "a route to X, from <where?>" -> the start, as a short reply.
            completed = validate_intent({"intent": "directions", "destination": pending.get("destination"), "origin": clean_text(message),
                                         "dest_latitude": pending.get("dest_latitude"), "dest_longitude": pending.get("dest_longitude")})
        if completed:
            return completed, "pending"
    if pending and pending.get("intent") == "find_pois" and 0 < len(message.split()) <= 8:
        # "which hotel?" -> "the second one" / "2" / a hotel's name: search around that listed place.
        index = pick_index(message, [str(poi.get("name") or "") for poi in context.pois])
        completed = validate_intent({**pending, "index": index}) if index is not None else None
        if completed:
            return completed, "pending"
    if pending and rule_name in {"geocode", "general_question"} and 0 < len(message.split()) <= 4 and pending.get("intent") == "modify_trip":
        completed = validate_intent({
            "intent": "modify_trip", "operation": "add_stop", "place": clean_text(message),
            **{k: v for k, v in pending.items() if k in {"after", "before"}},
        })
        if completed:
            return completed, "pending"

    # The model is asked about every message, so each turn gets its reading. Exact, evidence-based rules still have
    # the final say on route edits, numbered EV details, vehicle numbers, area searches and computed places such as
    # "the halfway point": for those the model's reading is only compared and logged, and never replaces the rules.
    rules_decide = _rules_decide(rule_action)
    try:
        llm_action = await llm_assistant.interpret(message, snapshot)
    except TooManyStops:
        if not rules_decide:
            if context.pois:
                # "a route to these restaurants": the model read every listed place as a stop. Ask which one, don't error.
                return validate_intent({"intent": "directions", "choose": True}) or rule_action, "guard"
            raise AppError("invalid_request", f"A trip can have at most {MAX_TRIP_STOPS} stops including the start and end. Please shorten the list.") from None
        llm_action = None  # the rules already read this message; a model's over-long stop list is not the user's problem
    if rules_decide:
        if llm_action is None:
            return rule_action, "rules"
        return rule_action, "rules_llm_agree" if llm_action["intent"] == rule_name else "rules_llm_differ"
    if llm_action is None:
        return rule_action, "rules"

    # A model reading that rewrites the route against an explicit "don't change my route" is ignored.
    if llm_action["intent"] in ROUTE_MUTATING and forbids_route_change(message):
        return rule_action, "rules"
    # So is one that edits the route when the user only searched or asked: a route changes only on an explicit request.
    if llm_action["intent"] in ROUTE_MUTATING and rule_name not in ROUTE_MUTATING and not explicit_route_request(message):
        logger.info("event=route_change_ignored source=llm intent=%s", safe_token(llm_action["intent"]))
        return rule_action, "rules"

    # EV intents only from the model when the EV Planner is already on, or the rules also saw EV evidence.
    if llm_action["intent"] in EV_ONLY and not context.ev_mode and rule_name not in EV_ONLY:
        return rule_action, "rules"

    # The model saw a trip to plan or destinations to suggest where the rules found only a weak reading (a bare lookup, a text
    # search for a vague word like "hill station", a guide, a route): the planning reading is the one the user asked for.
    if llm_action["intent"] in _PLANNING and _yields_to_planning(rule_action, message):
        return llm_action, "llm"

    # The rules fell back to a guess (a bare geocode / generic question): the model's reading is better.
    if rule_name in _SOFT_RULES and not (rule_name == "geocode" and rule_action.get("named")):
        return llm_action, "llm"
    if rule_name in _OPEN_QUESTION_RULES and llm_action["intent"] == "ask":
        return llm_action, "llm"

    # Both produced a concrete search/trip/info intent. Agreement merges (rules fill any gaps);
    # disagreement keeps the rules, whose patterns are tested, rather than an unverified model guess.
    if llm_action["intent"] == rule_name:
        merged = {**llm_action, **{k: v for k, v in rule_action.items() if v not in (None, [], {})}}
        # "my location" / "the halfway point" are resolved from the session, which a longer rule-captured phrase would hide.
        model_place = str(llm_action.get("place") or "")
        if model_place and (is_me_place(model_place) or is_midpoint_place(model_place)):
            merged["place"] = model_place
        # A rule-captured place that still holds a "near the palace" tail is a phrase, not a name a geocoder can use; the model
        # reads it as a landmark and city ("Mysore Palace, Mysuru").
        rule_place = str(rule_action.get("place") or "")
        if model_place and re.search(r"\b(?:near|around|close to|next to|by)\b", rule_place, flags=re.IGNORECASE) \
                and not re.search(r"\b(?:near|around|close to|next to|by)\b", model_place, flags=re.IGNORECASE):
            merged["place"] = model_place
        return validate_intent(merged) or rule_action, "llm+rules"
    return rule_action, "rules"


# ---------------------------------------------------------------------------------------------
# Replies
# ---------------------------------------------------------------------------------------------
_HELP_TEXT = (
    "I'm HerePilot-AI, a conversational map assistant. Try:\n"
    "• “Find restaurants near Connaught Place” or “hospitals within 5 km of Pune”\n"
    "• “Plan a trip from Mumbai to Bhubaneswar” then “add Hyderabad to my route”\n"
    "• “Find petrol pumps along my route” or “hotels near my destination”\n"
    "• “How far is the second stop?”\n"
    "EV Planner is optional: open it to add your vehicle's range and battery for charging plans."
)



def _fmt_minutes(minutes: Any) -> str:
    try:
        # Round half up, like the web UI does, so a reply and its card never differ by a minute.
        total = int(float(minutes) + 0.5)
    except (TypeError, ValueError):
        return "—"
    hours, mins = divmod(total, 60)
    return f"{hours}h {mins}m" if hours and mins else f"{hours}h" if hours else f"{mins} min"


def _mode_word(mode: str | None) -> str:
    return {"pedestrian": "walking", "bicycle": "cycling"}.get(mode or "car", "driving")


def _route_sentence(context: TripContext, route: dict[str, Any]) -> str:
    text = f"{' → '.join(context.trip_names())}: {route['distance_km']:g} km, about {_fmt_minutes(route['duration_minutes'])} {_mode_word(route.get('transport_mode'))}."
    legs = route.get("legs") or []
    if len(legs) > 1:
        names = context.trip_names()
        parts = [f"{names[leg['from_stop_index']]} → {names[min(leg['to_stop_index'], len(names) - 1)]} {leg['distance_km']:g} km" for leg in legs if leg['from_stop_index'] < len(names)]
        text += " Legs: " + "; ".join(parts) + "."
    return text


def _poi_lines(pois: list[dict[str, Any]], key: str = "distance_from_reference_km", limit: int = 8, scope: str | None = None) -> str:
    def line(i: int, p: dict[str, Any]) -> str:
        name = f"{i + 1}. {p['name']}"
        if scope == "area":
            return name + (f" — {p['city']}" if p.get("city") else "")
        if scope == "midpoint" and p.get(key) is not None:
            detour = f", {_fmt_distance(p['distance_from_route_km'])} from your route" if p.get("distance_from_route_km") is not None else ""
            return f"{name} — {_fmt_distance(p[key])} from the halfway point{detour}"
        if p.get(key) is not None:
            return f"{name} — {_fmt_distance(p[key]) if scope in {'me', 'result', 'anchor'} else str(p[key]) + ' km'} away"
        if p.get("distance_from_route_km") is not None:
            return f"{name} — {p['distance_from_route_km']} km from route"
        return name
    return "\n".join(line(i, p) for i, p in enumerate([p for p in pois[:limit] if p.get("name")]))


def _ask_reply(result: dict[str, Any]) -> str:
    """The direct answer, written by the model. Without one, only what HERE verified, and why there is no more."""
    doc = result.get("answer_doc")
    if doc:
        return str(doc["answer"])
    names = [s["name"] for s in result.get("subjects") or []]
    found = [s["name"] for s in result.get("subjects") or [] if s.get("kind") in {"place", "destination"}]
    why = ("The AI service that writes explanations isn't connected right now, so I haven't guessed."
           if not result.get("ai_available") else "I couldn't put an answer together this time. Please try again.")
    if found:
        return f"Here is what HERE lists for {', '.join(found)}. {why}"
    if names:
        return f"I couldn't find {', '.join('“' + n + '”' for n in names)} in HERE's data. {why}"
    return f"I can't answer that without the AI service. {why}" if not result.get("ai_available") else why


def _guide_reply(result: dict[str, Any]) -> str:
    """The written lead of a destination guide. Advice is the model's; the places below it are HERE's, and it says so."""
    guide = result.get("guide")
    place = result.get("place") or "there"
    pois = result.get("pois") or []
    if guide:
        lead = " ".join(part for part in (guide.get("headline"), guide.get("summary")) if part) or f"Here is a guide to {place}."
        if pois:
            return f"{lead}\nThe places below come from HERE; the seasonal advice is the AI's general knowledge."
        if result.get("source_status") == "unavailable":
            return f"{lead}\nI couldn't load places from HERE this time, so this guide has none. The advice is the AI's general knowledge."
        return f"{lead}\nHERE lists no notable places around {place}. The advice is the AI's general knowledge."
    if pois:
        return (f"Here are the best-known places HERE lists around {place}. Advice on the best time to visit needs the AI service, "
                "which isn't connected right now, so I haven't guessed at it.")
    if result.get("source_status") == "unavailable":
        return "Place search is unavailable right now, and the AI service that writes travel advice isn't connected, so there is nothing reliable to show yet."
    return f"HERE lists no notable places around {place}, and the AI service that writes travel advice isn't connected."


def _category_noun(category: str, count: int) -> str:
    """'hotel' / 'hotels', 'fuel' -> 'petrol pumps': wording that reads naturally for the count."""
    spec = spec_for(category)
    if spec is not None:
        return spec.singular if count == 1 else spec.plural
    key = str(category or "place").replace(" ", "_")
    singular, plural = _CATEGORY_NOUNS.get(key) or (key.replace("_", " "), key.replace("_", " ") + "s")
    return singular if count == 1 else plural


_CATEGORY_NOUNS = {
    "tourist_attraction": ("tourist attraction", "tourist attractions"),
    "supermarket": ("supermarket", "supermarkets"),
    "car_wash": ("car wash", "car washes"),
}


def _area_completeness_note(result: dict[str, Any], noun: str) -> str | None:
    search = result.get("search") or {}
    area = result.get("area") or {}
    if result.get("scope") != "area" or "complete" not in search:
        return None
    if search.get("complete"):
        return (f"That is everything HERE's data classifies as {noun} in {area.get('name', 'that area')}. "
                "Places HERE doesn't list or classify that way won't appear.")
    reasons = []
    if search.get("failed_requests"):
        reasons.append("some map requests failed")
    if search.get("budget_exhausted") or search.get("unresolved_full_tiles"):
        reasons.append("some parts returned HERE's 100-place maximum and I stopped splitting them")
    return (f"This list may be incomplete: HERE returns at most 100 places per request, so I searched {search.get('requests', 0)} map tiles "
            f"and {' and '.join(reasons) or 'could not cover everything'}. Name a district or city for a fuller list.")


def _search_notes(result: dict[str, Any], noun: str) -> list[str]:
    notes: list[str] = []
    search = result.get("search") or {}
    if result.get("area_fallback"):
        notes.append(f"I couldn't find a state, district or city called “{result['area_fallback']}”, so I searched near it instead.")
    rejected = int(search.get("rejected") or 0) + int(search.get("outside_area") or 0)
    if search.get("category_filtered") and int(search.get("rejected") or 0):
        notes.append(f"I left out {search['rejected']} result{'s' if search['rejected'] != 1 else ''} that HERE doesn't classify as {noun} (for example look-alike names or sub-entries).")
    if int(search.get("outside_area") or 0):
        notes.append(f"{search['outside_area']} more fell outside {(result.get('area') or {}).get('name', 'the area')} (its bounding box also covers neighbouring places).")
    area_note = _area_completeness_note(result, noun)
    if area_note:
        notes.append(area_note)
    scope = result.get("scope")
    shown, matched = len(result.get("pois") or []), int(search.get("matched") or 0)
    if scope not in {"area", "route"} and (search.get("page_full") or matched > shown):
        where = f" within {_fmt_distance(result['radius_km'])}" if result.get("radius_km") else ""
        if search.get("page_full"):
            notes.append(f"HERE returns at most 100 places per search, so this is the nearest {shown}; there may be more{where}.")
        else:
            notes.append(f"Showing the nearest {shown} of {matched}{where}.")
    coverage = result.get("coverage") or {}
    if coverage and coverage.get("complete") is False:
        reasons = []
        if coverage.get("sample_cap_reached"):
            reasons.append(f"the route is long ({coverage.get('route_km')} km), so I searched {coverage.get('sampled_points')} points along it")
        if coverage.get("full_pages"):
            reasons.append("in busy stretches HERE returned its 100-place maximum")
        if coverage.get("failed_points"):
            reasons.append(f"{coverage['failed_points']} searches failed")
        notes.append("Note: this may be incomplete: " + "; ".join(reasons or ["not every stretch could be covered"]) + ".")
    if result.get("reference_note"):
        notes.append(str(result["reference_note"]))
    if result.get("qualifier_note"):
        notes.append(str(result["qualifier_note"]))
    return notes


def _scope_label(result: dict[str, Any]) -> str:
    """Where a search looked, in words: "along your route", "near the halfway point of your trip", "near Pune"."""
    scope = result.get("scope")
    if scope == "area":
        area = result.get("area") or {}
        kind = {"state": "state", "district": "district", "city": "city area", "region": "region"}.get(str(area.get("kind")))
        return f"in {area.get('name') or 'that area'}" + (f" ({kind})" if kind else "")
    return {"route": "along your route", "midpoint": "near the halfway point of your trip", "me": "near you"}.get(str(scope)) or f"near {result.get('target') or 'there'}"


def _anchor_reply(result: dict[str, Any]) -> str:
    anchor = result.get("anchor") or {}
    step = result.get("anchor_step")
    anchor_plural = _category_noun(anchor.get("category", "place"), 2)
    wanted = _category_noun(result.get("target_category", "place"), 2)
    if step == "unavailable":
        return _places_reply(result, f"near {anchor.get('place')}")
    if step == "none_found":
        return (f"I couldn't find any {anchor_plural} within {_fmt_distance(anchor.get('radius_km') or 2)} of {anchor.get('place')}, "
                f"so I haven't searched for {wanted}. Tell me a different place, or name the exact {_category_noun(anchor.get('category', 'place'), 1)}.")
    if step == "choose":
        return f"{result.get('question')}\nThe {len(result.get('pois', []))} options are in the card below and on the map."
    return "I couldn't complete that request."


def _places_reply(result: dict[str, Any], place_label: str) -> str:
    pois = result.get("pois", [])
    category = str(result.get("category", "place")).replace("_", " ")
    noun = _category_noun(result.get("category", "place"), len(pois))
    status = result.get("source_status")
    if status == "unavailable":
        reason = result.get("source_error")
        why = " The map service isn't configured on the server." if reason == "provider_not_configured" else ""
        return f"I couldn't check {category} {place_label} right now because the place-search services are unavailable.{why} I haven't made up any results. Please try again shortly."
    scope = result.get("scope")
    radius = result.get("radius_km")
    within = f" within {_fmt_distance(radius)}" if radius and scope != "area" else ""
    source = result.get("source") or "place search"
    notes = _search_notes(result, _category_noun(result.get("category", "place"), 2))
    if not pois:
        hint = " Try a wider radius or a different search term." if scope != "area" else " Try a nearby city or a neighbouring district."
        return f"I found no {noun} {place_label}{within}.{hint}" + ("".join("\n" + n for n in notes) if notes else "")
    head = f"Found {len(pois)} {noun} {place_label}{within} using {source}."
    if status == "fallback":
        head = f"HERE Search had no usable answer, so I used {source}: {len(pois)} {noun} {place_label}{within}."
    if result.get("nearest"):
        first = pois[0]
        km = first.get("distance_from_reference_km")
        head = (f"The nearest {_category_noun(result.get('category', 'place'), 1)} HERE lists {place_label} is {first['name']}"
                + (f", about {_fmt_distance(round(float(km), 1))} away in a straight line (not a driving distance)." if km is not None else "."))
        if len(pois) > 1:
            head += f" {len(pois) - 1} more {'is' if len(pois) == 2 else 'are'} further out."
    # The places themselves are in the results card and on the map; repeating them here as a list is just noise.
    return head + "".join("\n" + n for n in notes)


def _saved_reply(action: str, result: dict[str, Any], context: TripContext) -> str:
    if action == "update_settings":
        s = result.get("settings") or {}
        parts = [f"{label} {s[key]:g}{unit}" for key, label, unit in (("reserve_percent", "reserve", "%"), ("target_charge_percent", "charge to", "%"), ("corridor_km", "search corridor", " km"))
                 if isinstance(s.get(key), (int, float))]
        saved = "Saved your charging settings" + (f": {', '.join(parts)}." if parts else ".")
    else:
        v = result.get("vehicle") or {}
        parts = []
        if v.get("make") or v.get("model"):
            parts.append(" ".join(str(x) for x in (v.get("make"), v.get("model")) if x))
        if isinstance(v.get("max_range_km"), (int, float)):
            parts.append(f"{v['max_range_km']:g} km range")
        if isinstance(v.get("current_battery_percent"), (int, float)):
            parts.append(f"{v['current_battery_percent']:g}% battery")
        if isinstance(v.get("battery_capacity_kwh"), (int, float)):
            parts.append(f"{v['battery_capacity_kwh']:g} kWh battery")
        saved = "Saved your vehicle" + (f": {', '.join(parts)}." if parts else ".")
    return f"{saved} Tell me where you're driving, for example “Mumbai to Bhubaneswar”, and I'll plan the charging stops for it."


def _compose_reply(message: str, result: dict[str, Any], context: TripContext) -> str:
    action = result.get("action")
    if action == "help":
        return _HELP_TEXT
    if action == "clarify":
        return str(result.get("question") or "Could you tell me a bit more?")
    if result.get("needs_vehicle"):
        route = result.get("route")
        head = _route_sentence(context, route) + " " if route else ""
        return head + str(result.get("message") or "Tell me your vehicle's range in km and its current battery % for charging plans.")
    if action in {"update_vehicle", "update_settings"} and not result.get("route"):
        # Saved, but there is no trip to plan charging for yet: say what was kept and what to say next.
        return _saved_reply(action, result, context)
    if action in {"plan_trip", "set_trip_options", "ev_plan", "modify_trip", "simulate", "update_vehicle", "update_settings"} and result.get("route"):
        route = result["route"]
        text = ""
        if result.get("change"):
            text += result["change"] + " "
        text += _route_sentence(context, route)
        if context.ev_mode:
            plan = result.get("ev_plan") or context.ev_plan
            if context.ev_status == "failed":
                text += " I couldn't calculate the EV charging plan this time."
            elif plan and plan.get("trip_possible"):
                n = len(plan.get("charging_stops", []))
                text += f" EV plan: {'no charging stop needed' if n == 0 else str(n) + ' charging stop(s)'}."
            elif plan:
                text += " I couldn't find a feasible charging plan with the current settings."
        elif context.vehicle_type in {"petrol", "diesel", "hybrid"}:
            text += f" (Trip set up for a {context.vehicle_type} vehicle; fuel use and cost aren't calculated.)"
        return text
    if action == "route_info":
        to = result.get("to") or "your destination"
        if result.get("target") == "total":
            return f"The route is {result.get('distance_km'):g} km, about {_fmt_minutes(result.get('duration_minutes'))}."
        return f"{to} (stop {result.get('stop_number')}) is {result.get('distance_km'):g} km, about {_fmt_minutes(result.get('duration_minutes'))}, from {result.get('measured_from')} along your route."
    if action == "geocode" and result.get("named_place"):
        place = result["named_place"]
        where = str(place.get("address") or "").strip()
        if where.casefold().startswith(place["name"].casefold()):
            where = where[len(place["name"]):].lstrip(", ") or where  # HERE's address label starts with the name; don't say it twice
        text = f"{place['name']} is at {where or 'the location shown on the map'}."
        if result.get("alternatives"):
            n = int(result["alternatives"])
            text += f" HERE also lists {n} other place{'s' if n != 1 else ''} with a similar name; they are in the card."
        return text
    if action == "geocode":
        rows = result.get("results", [])
        if rows:
            best = rows[0]
            text = f"{best.get('display_name', result.get('query'))} — {best.get('latitude'):.4f}, {best.get('longitude'):.4f}."
            if result.get("ambiguous"):
                text += " Other matches: " + "; ".join(result["ambiguous"][1:]) + "."
            return text
        return f"I couldn't find “{result.get('query')}”. Try adding a city or state."
    if action == "find_pois_near_location":
        return _places_reply(result, f"near {result.get('location_query') or 'that place'}")
    if action == "find_pois_in_area":
        return _places_reply(result, _scope_label({**result, "scope": "area"}))
    if action == "find_pois_near_anchor":
        return _anchor_reply(result)
    if action == "find_pois":
        text = _places_reply(result, _scope_label(result))
        anchor = result.get("anchor") or {}
        if anchor.get("only_candidate"):
            text = (f"The only {_category_noun(anchor.get('category', 'place'), 1)} I found within {_fmt_distance(2)} of {anchor.get('place')} is "
                    f"{anchor.get('name')}, so I searched around it.\n") + text
        return text
    if action == "show_pois":
        pois = result.get("pois", [])
        return f"Showing {len(pois)} place{'s' if len(pois) != 1 else ''} on the map." if pois else "There are no results on screen to show. Try a search first."
    if action == "search_charger_nearby":
        chargers = result.get("chargers", [])
        if not chargers:
            return f"I couldn't find an EV charging station near {result.get('query')} within {result.get('radius_km'):g} km."
        return f"Found {len(chargers)} EV charging station(s) near {result.get('query')} within {result.get('radius_km'):g} km."
    if action == "show_charging_stop":
        charger, stop, weather = result.get("charger") or {}, result.get("charging_stop") or {}, result.get("weather_at_charger") or {}
        text = f"Charging stop #{result.get('charging_stop_number', 1)}: {charger.get('name') or charger.get('address') or 'EV charging station'}. "
        text += f"Arrival battery {stop.get('battery_arrival_percent', 'n/a')}%; planned departure {stop.get('battery_departure_percent', 'n/a')}%. "
        text += (f"Weather nearby: {weather.get('condition', 'unavailable')}, {weather.get('temperature_c', '—')}°C. " if weather.get("available") else "Current weather data is unavailable. ")
        return text + "Station details are in the information card."
    if action == "recommend_charger":
        charger = result.get("recommended_charger")
        if not charger:
            return result.get("message", "No feasible charging stop was found.")
        stop = result.get("charging_stop") or {}
        return (f"Recommended charging stop: {charger.get('name') or charger.get('address') or 'Unnamed station'}. "
                f"About {charger.get('distance_from_route_km', 'n/a')} km from the route, at {charger.get('route_position_percent', 'n/a')}% of the route. "
                f"Battery arrival about {stop.get('battery_arrival_percent', 'n/a')}%, departure about {stop.get('battery_departure_percent', 'n/a')}%. "
                f"Source: {EV_DATASET_LABEL}.")
    if action == "compound_trip_request" and result.get("route"):
        text = _route_sentence(context, result["route"])
        plan = result.get("ev_plan") or {}
        if plan.get("trip_possible"):
            text += f" EV plan: {len(plan.get('charging_stops', []))} charging stop(s)."
        for group in result.get("poi_groups", []):
            text += "\n\n" + _places_reply(group, _scope_label(group))
        return text
    if action == "context":
        return f"Current trip: {' → '.join(context.trip_names()) or 'not set'}."
    if action == "destination_guide":
        return _guide_reply(result)
    if action == "trip_itinerary":
        return _itinerary_reply(result)
    if action == "suggest_destinations":
        return _destinations_reply(result)
    if action == "ask":
        return _ask_reply(result)
    if action == "general_answer":
        return result.get("answer") or "I can answer that better with the AI service connected. I can still search places, plan routes and answer distance questions."
    if action == "follow_up":
        return ("I couldn't answer that from what's on screen. Try naming a place, or ask me to search somewhere, "
                "add or remove a stop, or give a distance or travel time. Open-ended questions need an AI model connected.")
    return "I couldn't complete that request."


def _itinerary_reply(result: dict[str, Any]) -> str:
    it = result.get("itinerary") or {}
    name, days = it.get("destination") or "your destination", it.get("days") or 2
    who = f" Built around: {', '.join(it['party'])}." if it.get("party") else ""
    start = ""
    route = it.get("route")
    if it.get("origin") and route and route.get("distance_km") is not None:
        start = f" From {it['origin']} it is {round(float(route['distance_km']))} km, about {_fmt_minutes(route.get('duration_minutes'))} by road."
    elif it.get("origin"):
        start = f" I couldn't work out a road route from {it['origin']}."
    if not it.get("ai"):
        extra = " The AI that writes plans isn't available right now, so I haven't written a day-by-day plan. These are the best-known places, hotels and restaurants HERE lists."
        return f"Here's what HERE lists for {name}.{start}{extra}"
    if result.get("source_status") == "empty":
        return f"I couldn't find places for {name} to build a plan from.{start}"
    note = f" {result['base_note']}" if result.get("base_note") else ""
    return f"Here's a {days}-day plan for {name}.{who}{start}{note}"


def _destinations_reply(result: dict[str, Any]) -> str:
    items = result.get("destinations") or []
    origin = result.get("origin")
    kind = result.get("kind") or "destinations"
    if not items:
        return f"I couldn't verify any {kind}{(' near ' + origin) if origin else ''} on the map right now. Try naming a region or a different kind of place."
    head = (result.get("intro") or "").strip()
    nearest = min((i for i in items if i.get("duration_minutes") is not None), key=lambda i: i["duration_minutes"], default=None)
    tail = ""
    if origin and nearest:
        tail = f" Nearest by road: {nearest['name']}, about {_fmt_minutes(nearest['duration_minutes'])} from {origin}."
    elif result.get("region"):
        tail = f" All are in or around {result['region']}."
    base = head or f"Here are {len(items)} {kind}{(' near ' + origin) if origin else ''}."
    note = f" {result['destinations_note']}" if result.get("destinations_note") else ""
    return f"{base}{tail}{note} Drive times are measured by HERE; the suggestions are the AI's general knowledge."


async def _compose(message: str, result: dict[str, Any], context: TripContext) -> str:
    """Reply text. Everything that states a place, distance, time or count comes from deterministic templates built
    from HERE results. The model is used only to answer an open-ended follow-up about results already on screen, and
    even then its numbers must trace back to those results (otherwise the deterministic reply is used)."""
    if result.get("action") == "follow_up":
        phrased = await llm_assistant.phrase_answer(message, _context_dict(context), result)
        if phrased:
            return phrased
    text = _compose_reply(message, result, context)
    if result.get("ranked_by") == "rating":
        text += "\nRanked by Tripadvisor rating (weighted by how many people rated), then by distance. Places without a rating come after."
    elif result.get("ranking_note"):
        text += "\n" + result["ranking_note"]
    weather = result.get("place_weather")
    if isinstance(weather, dict) and result.get("action") != "suggest_destinations":
        line = weather_sentence(weather, str(weather.get("place") or "there"))
        if line:
            text += "\n" + line
    return text


async def handle_message(context: TripContext, message: str) -> tuple[TripContext, dict[str, Any], str]:
    context.turn_count += 1
    action, source = await _interpret(message, context)
    intent = action["intent"]
    logger.info(
        "event=turn intent=%s source=%s message_chars=%d turn=%d ev_mode=%s",
        safe_token(intent), source, len(message), context.turn_count, context.ev_mode,
    )
    handler = _HANDLERS.get(intent)
    if handler is None:
        raise AppError("invalid_request")
    result = await handler(context, action, message)
    await _decorate_result(context, result, message)
    context.last_action = action
    reply = await _compose(message, result, context)
    _remember(context, message, result, reply)
    return context, result, reply


# ---------------------------------------------------------------------------------------------
# Structured controls used by the UI panels (same validation gate as chat)
# ---------------------------------------------------------------------------------------------
async def set_options(context: TripContext, *, vehicle_type: str | None = None, travel_mode: str | None = None) -> tuple[dict[str, Any], str]:
    """Set the general trip options. Fuel type is informational; travel mode re-routes."""
    action = validate_intent({"intent": "set_trip_options", "vehicle_type": vehicle_type, "travel_mode": travel_mode})
    if action is None:
        raise AppError("invalid_request", "Choose petrol, diesel, hybrid or electric, and driving, walking or cycling.")
    result = await _h_set_trip_options(context, action, "")
    return result, _options_reply(context, result)


def _options_reply(context: TripContext, result: dict[str, Any]) -> str:
    parts = []
    if context.vehicle_type:
        parts.append(f"vehicle type {context.vehicle_type}")
    parts.append(f"{_mode_word(context.travel_mode)} mode")
    text = "Trip options set: " + ", ".join(parts) + "."
    if result.get("route"):
        text += " " + _route_sentence(context, result["route"])
    return text


async def configure_ev(
    context: TripContext,
    *,
    enabled: bool,
    vehicle: dict[str, Any] | None = None,
    settings: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    """Switch the optional EV Planner on/off and apply vehicle details and settings."""
    if not enabled:
        disable_ev(context)
        result = {**_result_base(context), "success": True, "action": "ev_mode", "ev_mode": False, "ev_status": context.ev_status}
        return result, "EV Planner is off. Your route is unchanged and no charging information is shown."

    _enable_ev(context)
    if vehicle:
        checked = validate_intent({"intent": "update_vehicle", "vehicle_updates": vehicle})
        if checked is None:
            raise AppError("invalid_request", "Check the vehicle details: range in km, battery % between 0 and 100, and capacity in kWh.")
        _update_vehicle(context, checked["vehicle_updates"])
    if settings:
        checked_settings = validate_intent({"intent": "update_settings", "settings": settings})
        if checked_settings is None:
            raise AppError("invalid_request", "Check the settings: reserve 0 to 99%, charging target 1 to 100%, corridor up to 50 km.")
        _apply_settings(context, checked_settings["settings"])

    result: dict[str, Any] = {**_result_base(context), "success": True, "action": "ev_mode"}
    if context.route and len(context.stops) >= 2:
        result.update(await _recompute_ev(context))
        result["action"] = "ev_mode"
    else:
        result["ev_status"] = "needs_vehicle" if _vehicle_error(context) else "ready"
    result["ev_mode"] = True
    _route_summary_extras(context, result)
    if context.ev_status == "needs_vehicle":
        return result, str(result.get("message") or _vehicle_error(context)) if _vehicle_error(context) else "EV Planner is on. Plan a trip to see charging stops."
    plan = context.ev_plan or {}
    if context.ev_status == "calculated" and plan:
        n = len(plan.get("charging_stops", []))
        if plan.get("trip_possible"):
            return result, f"EV Planner is on: {'no charging stop is needed' if n == 0 else str(n) + ' charging stop(s)'} for this route."
        return result, "EV Planner is on, but I couldn't find a feasible charging plan with these settings."
    if context.ev_status == "failed":
        return result, "EV Planner is on, but the charging plan couldn't be calculated this time."
    return result, "EV Planner is on. Plan a trip to see charging stops."
