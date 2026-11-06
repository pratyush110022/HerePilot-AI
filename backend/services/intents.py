"""Intent vocabulary, normalisation and validation.

Both the deterministic parser and the LLM produce a plain dict. :func:`validate_intent` is the
single gate between that dict and the handlers: it checks the intent name, coerces and clamps
every argument, and drops anything it does not recognise. Nothing downstream trusts raw model
output.
"""
from __future__ import annotations

import math
import re
from typing import Any

MAX_STOPS = 10
MIN_RADIUS_KM = 0.1
MAX_RADIUS_KM = 50.0
DEFAULT_RADIUS_KM = 5.0
MAX_LIMIT = 100
DEFAULT_LIMIT = 80
MAX_PLACE_CHARS = 120

# Intents the handlers implement. Names for existing behaviour are unchanged on purpose.
GENERAL_INTENTS = frozenset({
    "help", "general_question", "follow_up_question", "geocode", "plan_trip", "modify_trip",
    "find_pois_near_location", "find_pois", "find_pois_in_area", "find_pois_near_anchor", "route_info", "show_pois", "context",
    "set_trip_options", "clarify", "compound_trip_request", "destination_guide", "ask", "trip_itinerary", "suggest_destinations", "directions",
})
EV_INTENTS = frozenset({
    "ev_plan", "update_vehicle", "update_settings", "simulate", "show_charging_stop",
    "recommend_charger", "search_charger_nearby",
})
INTENTS = GENERAL_INTENTS | EV_INTENTS

# "destination_guide": travel advice about a place ("best time to visit Ooty") answered with a structured guide.
GUIDE_TOPICS = frozenset({"best_time", "things_to_do", "overview"})

# "ask": an open question the model answers itself, with HERE resolving the named places it is about.
MAX_SUBJECTS = 3

# "trip_itinerary": a day-by-day plan for one destination. "suggest_destinations": ideas for where to go.
MAX_ITINERARY_DAYS = 7
DEFAULT_ITINERARY_DAYS = 2
MAX_TAGS = 6
BUDGETS = frozenset({"budget", "mid", "luxury"})
MIN_WITHIN_KM, MAX_WITHIN_KM = 20.0, 1500.0

MODIFY_OPERATIONS = frozenset({"add_stop", "remove_stop", "set_origin", "set_destination", "add_charging_stop"})
# "midpoint": the point halfway along the active route. "me": the location the user chose to share.
POI_SCOPES = frozenset({"route", "stop", "destination", "origin", "all_stops", "charging_stop", "focus", "result", "midpoint", "me"})
MAX_QUALIFIERS = 5
ROUTE_INFO_TARGETS = frozenset({"total", "stop", "destination", "origin", "named"})

VEHICLE_TYPES = ("petrol", "diesel", "hybrid", "electric")
VEHICLE_TYPE_WORDS = {
    "petrol": "petrol", "gasoline": "petrol", "gas": "petrol",
    "diesel": "diesel", "hybrid": "hybrid", "electric": "electric", "ev": "electric",
}
TRAVEL_MODES = ("car", "pedestrian", "bicycle")
TRAVEL_MODE_WORDS = {
    "car": "car", "drive": "car", "driving": "car", "by car": "car",
    "walk": "pedestrian", "walking": "pedestrian", "on foot": "pedestrian", "pedestrian": "pedestrian",
    "cycle": "bicycle", "cycling": "bicycle", "bike": "bicycle", "biking": "bicycle", "bicycle": "bicycle",
}

CATEGORY_ALIASES: dict[str, str] = {
    "restaurant": "restaurant", "restaurants": "restaurant", "food": "restaurant", "eatery": "restaurant",
    "lunch stop": "restaurant", "lunch spot": "restaurant", "lunch place": "restaurant", "dinner spot": "restaurant",
    "place to eat": "restaurant", "places to eat": "restaurant", "place for lunch": "restaurant", "place for dinner": "restaurant",
    "cafe": "cafe", "cafes": "cafe", "coffee shop": "cafe", "coffee shops": "cafe", "coffee": "cafe",
    "coffee break": "cafe", "coffee place": "cafe", "coffee spot": "cafe", "café": "cafe", "cafés": "cafe",
    "airport": "airport", "airports": "airport", "airfield": "airport", "airfields": "airport", "aerodrome": "airport",
    "hotel": "hotel", "hotels": "hotel", "lodging": "hotel", "place to stay": "hotel", "places to stay": "hotel",
    "hospital": "hospital", "hospitals": "hospital",
    "clinic": "medical", "clinics": "medical", "doctor": "medical", "doctors": "medical",
    "medical facility": "medical", "medical facilities": "medical", "health care facility": "medical", "healthcare facility": "medical",
    "health care facilities": "medical", "healthcare facilities": "medical",
    "pharmacy": "pharmacy", "pharmacies": "pharmacy", "chemist": "pharmacy", "medical store": "pharmacy",
    "fuel": "fuel", "fuel station": "fuel", "fuel stations": "fuel",
    "petrol pump": "fuel", "petrol pumps": "fuel", "petrol station": "fuel", "petrol stations": "fuel",
    "petrol bunk": "fuel", "petrol bunks": "fuel", "gas station": "fuel", "gas stations": "fuel",
    "filling station": "fuel", "filling stations": "fuel", "diesel pump": "fuel", "diesel pumps": "fuel",
    "supermarket": "supermarket", "supermarkets": "supermarket",
    "convenience store": "convenience", "convenience stores": "convenience",
    "tourist attraction": "tourist_attraction", "tourist attractions": "tourist_attraction",
    "tourist place": "tourist_attraction", "tourist places": "tourist_attraction",
    "tourist spot": "tourist_attraction", "tourist spots": "tourist_attraction",
    "places to visit": "tourist_attraction", "attractions": "tourist_attraction", "sightseeing": "tourist_attraction",
    "parking": "parking", "parking lot": "parking", "parking lots": "parking",
    "pub": "pub_bar", "pubs": "pub_bar", "bar": "pub_bar", "bars": "pub_bar",
    "pub & bar": "pub_bar", "pubs & bars": "pub_bar", "pub and bar": "pub_bar",
    "nightlife": "nightlife", "nightlife spots": "nightlife",
    "shopping mall": "shopping_mall", "shopping malls": "shopping_mall", "mall": "shopping_mall", "malls": "shopping_mall",
    "bank": "bank", "banks": "bank", "atm": "atm", "atms": "atm",
    "car repair": "car_repair", "car repair shops": "car_repair", "garage": "car_repair", "mechanic": "car_repair",
    "car wash": "car_wash", "car washes": "car_wash",
    "ev charging": "ev_charging", "charging station": "ev_charging", "charging stations": "ev_charging",
    "charger": "ev_charging", "chargers": "ev_charging",
}
KNOWN_CATEGORIES = frozenset(CATEGORY_ALIASES.values())


def canonical_category(value: Any) -> str | None:
    """Known category key for ``value`` (an alias or key), else ``None``."""
    text = re.sub(r"\s+", " ", str(value or "").strip().lower().replace("_", " "))
    if not text:
        return None
    if text in CATEGORY_ALIASES:
        return CATEGORY_ALIASES[text]
    key = text.replace(" ", "_")
    return key if key in KNOWN_CATEGORIES else None


def category_or_place(value: Any) -> str:
    """Category key, or the generic ``place`` so HERE can interpret a free-text query."""
    return canonical_category(value) or "place"


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def clamp_radius(value: Any, default: float = DEFAULT_RADIUS_KM) -> float:
    number = _finite(value)
    if number is None or number <= 0:
        return default
    return max(MIN_RADIUS_KM, min(number, MAX_RADIUS_KM))


def clamp_limit(value: Any, default: int = DEFAULT_LIMIT) -> int:
    number = _finite(value)
    if number is None or number < 1:
        return default
    return max(1, min(int(number), MAX_LIMIT))


def clean_text(value: Any, limit: int = MAX_PLACE_CHARS) -> str:
    """Single-line, control-character-free text, safe to echo back or hand to a geocoder."""
    if isinstance(value, dict):
        for key in ("name", "place", "query", "display_name", "text"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                value = candidate
                break
        else:
            return ""
    if value is None or isinstance(value, (list, tuple, set)):
        return ""
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value))
    return re.sub(r"\s+", " ", text).strip(" \t.,;:!?\"'")[:limit]


def _index(value: Any) -> int | None:
    number = _finite(value)
    if number is None or number != int(number):
        return None
    return int(number)


def _qualifiers(raw: Any) -> list[str]:
    """Short, cleaned wishes like "quiet" or "family-friendly". They are reported back, never silently acted on."""
    if not isinstance(raw, (list, tuple)):
        return []
    seen: list[str] = []
    for item in raw:
        text = clean_text(item, 30).lower()
        if text and text not in seen:
            seen.append(text)
    return seen[:MAX_QUALIFIERS]


def _fraction(value: Any, default: float = 0.5) -> float:
    number = _finite(value)
    if number is None:
        return default
    return min(max(number, 0.05), 0.95)


def _days(value: Any, default: int | None = None) -> int | None:
    """A trip length in whole days, 1 to ``MAX_ITINERARY_DAYS``; ``default`` when it is missing or nonsense."""
    number = _finite(value)
    if number is None or number < 1:
        return default
    return max(1, min(int(round(number)), MAX_ITINERARY_DAYS))


def _month(value: Any) -> int | None:
    number = _finite(value)
    return int(number) if number is not None and number == int(number) and 1 <= int(number) <= 12 else None


def _tags(raw: Any, width: int = 50) -> list[str]:
    """Who is travelling and what they enjoy, as the user said it ("wife", "8 month baby girl", "temples")."""
    if not isinstance(raw, (list, tuple)):
        return []
    seen: dict[str, str] = {}
    for item in raw:
        text = clean_text(item, width)
        if text and text.casefold() not in seen:
            seen[text.casefold()] = text
    return list(seen.values())[:MAX_TAGS]


def _within_km(value: Any) -> float | None:
    number = _finite(value)
    return max(MIN_WITHIN_KM, min(number, MAX_WITHIN_KM)) if number is not None and number > 0 else None


class TooManyStops(ValueError):
    """More places than a route may have. Never truncated silently: the user is told."""


def _places(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    places = [clean_text(item) for item in value]
    places = [place for place in places if place]
    if len(places) > MAX_STOPS:
        raise TooManyStops(f"{len(places)} places given; the maximum is {MAX_STOPS}")
    return places


def _subjects(value: Any) -> list[str]:
    """Names of the places or destinations a question is about: cleaned, de-duplicated, at most ``MAX_SUBJECTS``."""
    if not isinstance(value, (list, tuple)):
        return []
    seen: dict[str, str] = {}
    for item in value:
        name = clean_text(item, 80)
        if name and name.casefold() not in seen:
            seen[name.casefold()] = name
    return list(seen.values())[:MAX_SUBJECTS]


def vehicle_type_from(value: Any) -> str | None:
    return VEHICLE_TYPE_WORDS.get(str(value or "").strip().lower())


def travel_mode_from(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    return text if text in TRAVEL_MODES else TRAVEL_MODE_WORDS.get(text)


def _vehicle_updates(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    updates: dict[str, Any] = {}
    for key in ("make", "model"):
        text = clean_text(raw.get(key), 40)
        if text:
            updates[key] = text
    range_km = _finite(raw.get("max_range_km"))
    if range_km is not None and 0 < range_km <= 2000:
        updates["max_range_km"] = range_km
    capacity = _finite(raw.get("battery_capacity_kwh"))
    if capacity is not None and 0 < capacity <= 500:
        updates["battery_capacity_kwh"] = capacity
    battery = _finite(raw.get("current_battery_percent"))
    if battery is not None and 0 <= battery <= 100:
        updates["current_battery_percent"] = battery
    return updates


def _settings(raw: Any) -> dict[str, float]:
    if not isinstance(raw, dict):
        return {}
    limits = {"reserve_percent": (0, 99), "target_charge_percent": (1, 100), "corridor_km": (0.1, 50)}
    out: dict[str, float] = {}
    for key, (low, high) in limits.items():
        number = _finite(raw.get(key))
        if number is not None and low <= number <= high:
            out[key] = number
    return out


def validate_intent(raw: Any) -> dict[str, Any] | None:
    """Return a normalised intent dict, or ``None`` if ``raw`` is not a usable intent.

    Raises :class:`TooManyStops` (a ``ValueError``) when a trip lists more than ``MAX_STOPS`` places, so the
    caller can tell the user instead of planning a silently shortened route.
    """
    if not isinstance(raw, dict):
        return None
    name = raw.get("intent")
    if not isinstance(name, str) or name not in INTENTS:
        return None
    out: dict[str, Any] = {"intent": name}

    if name == "plan_trip":
        out["places"] = _places(raw.get("places"))
        out["ev_required"] = bool(raw.get("ev_required"))
        for key, fn in (("vehicle_type", vehicle_type_from), ("travel_mode", travel_mode_from)):
            value = fn(raw.get(key))
            if value:
                out[key] = value
        # "EV trip from Pune to Goa, my car has 350 km range and 90% battery": the vehicle and charging settings in the same message.
        vehicle = _vehicle_updates(raw.get("vehicle_updates"))
        if vehicle and out["ev_required"]:
            out["vehicle_updates"] = vehicle
        settings = _settings(raw.get("settings"))
        if settings:
            out["settings"] = settings
    elif name == "modify_trip":
        operation = raw.get("operation")
        if operation not in MODIFY_OPERATIONS:
            return None
        out["operation"] = operation
        place = clean_text(raw.get("place"))
        index = _index(raw.get("index"))
        if place:
            out["place"] = place
        if index is not None:
            out["index"] = index
        if operation in {"add_stop", "set_origin", "set_destination", "add_charging_stop"} and not place:
            return None
        if operation == "remove_stop" and not place and index is None:
            return None
        for key in ("after", "before"):
            hint = clean_text(raw.get(key))
            if hint:
                out[key] = hint
    elif name == "find_pois_near_location":
        place = clean_text(raw.get("place"))
        if not place:
            return None
        out.update({
            "place": place,
            "category": category_or_place(raw.get("category")),
            "query": clean_text(raw.get("query"), 80) or None,
            "radius_km": clamp_radius(raw.get("radius_km")),
            "radius_explicit": bool(raw.get("radius_explicit")),
            "limit": clamp_limit(raw.get("limit")),
            "qualifiers": _qualifiers(raw.get("qualifiers")),
            "nearest": bool(raw.get("nearest")),
        })
    elif name == "find_pois_in_area":
        area = clean_text(raw.get("area") or raw.get("place"))
        if not area:
            return None
        out.update({
            "area": area,
            "category": category_or_place(raw.get("category")),
            "query": clean_text(raw.get("query"), 80) or None,
            "limit": clamp_limit(raw.get("limit")),
            "qualifiers": _qualifiers(raw.get("qualifiers")),
        })
    elif name == "find_pois_near_anchor":
        place = clean_text(raw.get("place"))
        anchor_category = canonical_category(raw.get("anchor_category"))
        if not place or not anchor_category:
            return None
        out.update({
            "place": place,
            "category": category_or_place(raw.get("category")),
            "anchor_category": anchor_category,
            "query": clean_text(raw.get("query"), 80) or None,
            "radius_km": clamp_radius(raw.get("radius_km"), 0.5),
            "anchor_radius_km": clamp_radius(raw.get("anchor_radius_km"), 2.0),
            "limit": clamp_limit(raw.get("limit")),
            "qualifiers": _qualifiers(raw.get("qualifiers")),
        })
    elif name == "find_pois":
        scope = raw.get("scope") or "route"
        if scope not in POI_SCOPES:
            return None
        out.update({
            "category": category_or_place(raw.get("category") or "restaurant"),
            "scope": scope,
            "index": _index(raw.get("index")),
            "query": clean_text(raw.get("query"), 80) or None,
            "radius_km": clamp_radius(raw.get("radius_km"), 3.0),
            "radius_explicit": bool(raw.get("radius_explicit")),
            "limit": clamp_limit(raw.get("limit")),
            "qualifiers": _qualifiers(raw.get("qualifiers")),
            "fraction": _fraction(raw.get("fraction")),
            "nearest": bool(raw.get("nearest")),
        })
    elif name == "route_info":
        target = raw.get("target") or "total"
        if target not in ROUTE_INFO_TARGETS:
            return None
        out.update({"target": target, "index": _index(raw.get("index")), "place": clean_text(raw.get("place")) or None})
    elif name == "geocode":
        place = clean_text(raw.get("place"))
        if not place:
            return None
        out["place"] = place
        out["named"] = bool(raw.get("named"))
    elif name == "ask":
        out["subjects"] = _subjects(raw.get("subjects"))
    elif name == "destination_guide":
        place = clean_text(raw.get("place"))
        if not place:
            return None
        topic = str(raw.get("topic") or "").strip().lower().replace(" ", "_")
        out.update({"place": place, "topic": topic if topic in GUIDE_TOPICS else "overview"})
    elif name == "trip_itinerary":
        destination = clean_text(raw.get("destination") or raw.get("place"))
        # A way of travelling is not somewhere to go ("a EV trip" has no destination called EV); the rules or the route planner take it.
        if not destination or re.fullmatch(r"(?:ev|evs|electric(?:\s+(?:car|vehicle|bike|scooter))?|car|cars|bike|bikes|vehicle|suv)", destination.strip(), re.IGNORECASE):
            return None
        budget = str(raw.get("budget") or "").strip().lower()
        out.update({
            "destination": destination,
            "origin": clean_text(raw.get("origin") or raw.get("from")) or None,
            "days": _days(raw.get("days"), DEFAULT_ITINERARY_DAYS),
            "party": _tags(raw.get("party")),
            "interests": _tags(raw.get("interests"), 40),
            "month": _month(raw.get("month")),
            "budget": budget if budget in BUDGETS else None,
        })
    elif name == "suggest_destinations":
        origin = clean_text(raw.get("origin") or raw.get("from") or raw.get("place")) or None
        region = clean_text(raw.get("region") or raw.get("area"), 60) or None
        kind = clean_text(raw.get("kind") or raw.get("type"), 40) or None
        if not (origin or region or kind):
            return None
        out.update({
            "origin": origin, "region": region, "kind": kind,
            "within_km": _within_km(raw.get("within_km")),
            "within_minutes": _finite(raw.get("within_minutes")) if _finite(raw.get("within_minutes")) and 10 <= _finite(raw.get("within_minutes")) <= 1500 else None,
            "days": _days(raw.get("days")),
            "party": _tags(raw.get("party")),
            "interests": _tags(raw.get("interests"), 40),
            "month": _month(raw.get("month")),
        })
    elif name == "directions":
        destination = clean_text(raw.get("destination") or raw.get("place"))
        index = _index(raw.get("index"))
        if not destination and index is None and not raw.get("choose"):
            return None
        lat, lon = _finite(raw.get("dest_latitude")), _finite(raw.get("dest_longitude"))
        out.update({
            "destination": destination or None, "origin": clean_text(raw.get("origin") or raw.get("from")) or None, "index": index,
            "choose": bool(raw.get("choose")),
            "dest_latitude": lat if lat is not None and -90 <= lat <= 90 else None,
            "dest_longitude": lon if lon is not None and -180 <= lon <= 180 else None,
        })
    elif name == "set_trip_options":
        for key, fn in (("vehicle_type", vehicle_type_from), ("travel_mode", travel_mode_from)):
            value = fn(raw.get(key))
            if value:
                out[key] = value
        if len(out) == 1:
            return None
    elif name == "update_vehicle":
        updates = _vehicle_updates(raw.get("vehicle_updates") or raw.get("vehicle"))
        if not updates:
            return None
        out["vehicle_updates"] = updates
    elif name == "update_settings":
        settings = _settings(raw.get("settings"))
        if not settings:
            return None
        out["settings"] = settings
    elif name == "simulate":
        field = raw.get("field", "battery_percent")
        value = _finite(raw.get("value"))
        if field not in {"battery_percent", "reserve_percent", "target_charge_percent"} or value is None:
            return None
        out.update({"field": field, "value": value})
    elif name == "show_charging_stop":
        out["index"] = max(0, _index(raw.get("index")) or 0)
    elif name == "search_charger_nearby":
        place = clean_text(raw.get("place"))
        if not place:
            return None
        out.update({"place": place, "radius_km": max(1.0, min(_finite(raw.get("radius_km")) or 25.0, 100.0))})
    elif name == "clarify":
        question = clean_text(raw.get("question"), 240)
        if question:
            # clean_text strips trailing punctuation; a question keeps its own ending.
            given = str(raw.get("question")).rstrip()[-1:] if isinstance(raw.get("question"), str) else ""
            question += given if given in {".", "?", "!"} else "."
        out["question"] = question or "Could you tell me a bit more about what you'd like to do?"
        pending = raw.get("pending")
        if isinstance(pending, dict) and pending.get("intent") == "modify_trip" and pending.get("operation") == "add_stop":
            hints = {k: clean_text(pending.get(k)) for k in ("after", "before") if clean_text(pending.get(k))}
            out["pending"] = {"intent": "modify_trip", "operation": "add_stop", **hints}
        elif isinstance(pending, dict) and pending.get("intent") == "directions":
            # "a route to <which one?>" or "a route to X, from <where?>": the next short reply completes it.
            out["pending"] = {
                "intent": "directions", "choose": bool(pending.get("choose")), "destination": clean_text(pending.get("destination")) or None,
                "dest_latitude": _finite(pending.get("dest_latitude")), "dest_longitude": _finite(pending.get("dest_longitude")),
            }
        elif isinstance(pending, dict) and pending.get("intent") == "find_pois" and pending.get("scope") == "result":
            # "restaurants within 500 m of <which hotel?>": the next short reply picks one of the listed results.
            out["pending"] = {
                "intent": "find_pois", "scope": "result", "category": category_or_place(pending.get("category")),
                "radius_km": clamp_radius(pending.get("radius_km"), 0.5), "radius_explicit": True,
                "limit": clamp_limit(pending.get("limit")), "query": clean_text(pending.get("query"), 80) or None,
                "qualifiers": _qualifiers(pending.get("qualifiers")),
            }
    elif name == "compound_trip_request":
        out["places"] = _places(raw.get("places"))
        out["ev_required"] = bool(raw.get("ev_required"))
        out["vehicle_updates"] = _vehicle_updates(raw.get("vehicle_updates"))
        out["settings"] = _settings(raw.get("settings"))
        out["recommend_charger"] = bool(raw.get("recommend_charger"))
        requests = []
        for item in (raw.get("poi_requests") or [])[:6]:
            if not isinstance(item, dict):
                continue
            scope = item.get("scope") if item.get("scope") in POI_SCOPES else "route"
            requests.append({
                "category": category_or_place(item.get("category") or "restaurant"),
                "scope": scope,
                "index": _index(item.get("index")),
                "radius_km": clamp_radius(item.get("radius_km"), 3.0),
                "radius_explicit": bool(item.get("radius_explicit")),
                "limit": clamp_limit(item.get("limit")),
                "query": clean_text(item.get("query"), 80) or None,
                "qualifiers": _qualifiers(item.get("qualifiers")),
                "fraction": _fraction(item.get("fraction")),
            })
        out["poi_requests"] = requests
    return out
