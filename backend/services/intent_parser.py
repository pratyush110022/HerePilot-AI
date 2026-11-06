"""Deterministic intent parser.

This is the always-available path (no AI needed) and the safety net behind the LLM. Rules run in
a fixed priority order and each one needs positive evidence, because order is what decides
meaning:

    help > compound > EV detail > route edits > plan > options/vehicle/EV > route info >
    searches (along route / near a reference / near a named place) > follow-ups > geocode > question

"add a stop at Hyderabad" is a route edit, so route edits are tested *before* the generic
"<thing> at <place>" search. A search is only recognised when there is a category or a search verb.

Geographic scope of a search is decided by the wording, never by guessing:

    "airports in Odisha"             -> find_pois_in_area      (the whole state/district/city)
    "airports near Bhubaneswar"      -> find_pois_near_location (a point and a radius)
    "within 5 km of Pune"            -> find_pois_near_location (explicit radius)
    "along my route"                 -> find_pois scope=route
    "near my destination"            -> find_pois scope=destination
    "halfway point of my trip"       -> find_pois scope=midpoint
    "within 5 km of me"              -> find_pois scope=me
    "restaurants near a hotel near X"-> find_pois_near_anchor  (the hotel is a stepping stone, so ask which one)
"""
from __future__ import annotations

import re
from typing import Any

from services.intents import (
    CATEGORY_ALIASES,
    TRAVEL_MODE_WORDS,
    VEHICLE_TYPE_WORDS,
    canonical_category,
)
from services.place_categories import extract_qualifiers, spec_for

PLACE_SPLIT_RE = re.compile(r"\s*(?:,|\band\b|\bthen\b|->|→)\s*", re.IGNORECASE)

ORDINALS = {
    "first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4,
    "fifth": 5, "5th": 5, "sixth": 6, "6th": 6, "seventh": 7, "7th": 7, "eighth": 8, "8th": 8,
    "ninth": 9, "9th": 9, "tenth": 10, "10th": 10, "last": -1, "final": -1,
}
_ORD = "|".join(sorted(ORDINALS, key=len, reverse=True))
_NUM_OR_ORD = rf"(?:\d{{1,2}}(?:st|nd|rd|th)?|{_ORD})"

_SEARCH_VERBS = re.compile(r"\b(?:find|show|search|list|give|share|get|where|look(?:ing)?\s+for|need|want|any|nearest|closest|recommend|suggest|best|top|nearby)\b", re.IGNORECASE)
# With no category named, "get" only starts a search in phrasings like "get me ..." ("towns get cooler at night" is not one).
_CATEGORYLESS_SEARCH = re.compile(_SEARCH_VERBS.pattern.replace("|get|", "|get\\s+(?:me|us|some|an?|the)|"), re.IGNORECASE)
_EV_WORDS = re.compile(r"\b(?:ev|evs|electric|battery|charging|charger|chargers|charge)\b", re.IGNORECASE)


def _ordinal(token: str | None) -> int | None:
    if not token:
        return None
    token = token.lower()
    if token in ORDINALS:
        return ORDINALS[token]
    digits = re.sub(r"(?:st|nd|rd|th)$", "", token)
    return int(digits) if digits.isdigit() else None


def _index_from(token: str | None, total: int | None = None) -> int | None:
    """0-based index from a 1-based number/ordinal token. 'last' becomes -1 for the handler."""
    value = _ordinal(token)
    if value is None:
        return None
    if value == -1:
        return -1
    return max(0, value - 1)


# ---------------------------------------------------------------------------------------------
# Vehicle / settings extraction (shared by EV updates and plan requests)
# ---------------------------------------------------------------------------------------------
def _number(message: str, patterns: list[str]) -> float | None:
    for pattern in patterns:
        match = re.search(pattern, message, flags=re.IGNORECASE)
        if match:
            return float(match.group(1))
    return None


def _vehicle_update(message: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    max_range = _number(message, [
        r"range\s*(?:of|is|=)?\s*(\d+(?:\.\d+)?)\s*km",
        r"(\d+(?:\.\d+)?)\s*km\s*(?:of\s+)?(?:range|driving range)",
        r"(\d+(?:\.\d+)?)\s*km\s+range",
    ])
    battery = _number(message, [
        r"(?:current|currently)\s+battery\s*(?:is|at|=)?\s*(\d+(?:\.\d+)?)\s*%",
        r"battery\s*(?:level\s*)?(?:is\s+|=\s*)?(?:currently\s+|now\s+|right now\s+)?(?:at|is|=|of|level|with)?\s*(\d+(?:\.\d+)?)\s*%",
        r"battery[^.%\d]{0,25}?(\d+(?:\.\d+)?)\s*%",
        r"(\d+(?:\.\d+)?)\s*%\s+(?:battery|charge|charged|soc)\b",
        r"(?:currently\s+|now\s+)?(?:at|having|with|on)\s*(\d+(?:\.\d+)?)\s*%\s*(?:charge|battery|charged)",
        r"(?:start(?:ing)?|leave|leaving)\s+(?:with|at)\s*(\d+(?:\.\d+)?)\s*%",
    ])
    capacity = _number(message, [
        r"battery\s*capacity\s*(?:of|is|=)?\s*(\d+(?:\.\d+)?)\s*kwh",
        r"(\d+(?:\.\d+)?)\s*kwh\s*(?:battery\s*)?pack?",
        r"(\d+(?:\.\d+)?)\s*kwh\b",
    ])
    if max_range is not None:
        result["max_range_km"] = max_range
    if battery is not None:
        result["current_battery_percent"] = battery
    if capacity is not None:
        result["battery_capacity_kwh"] = capacity

    for make, model in [
        ("Tata", "Nexon EV"), ("Tata", "Tiago EV"), ("Tata", "Punch EV"), ("Tata", "Tigor EV"),
        ("MG", "ZS EV"), ("MG", "Comet EV"), ("Hyundai", "Kona Electric"), ("Hyundai", "Ioniq 5"),
        ("Mahindra", "XUV400"), ("Mahindra", "BE 6"), ("BYD", "Atto 3"), ("BYD", "Seal"),
    ]:
        if re.search(rf"\b{re.escape(model)}\b", message, flags=re.IGNORECASE):
            result["make"] = make
            result["model"] = model
            return result

    # Free-form "I drive a Tata Nexon" style. Stop at the first word that begins a spec clause,
    # so "Tata Nexon with 100% battery" yields model "Nexon", not "Nexon with".
    match = re.search(
        r"(?:\bvehicle\b|\bcar\b|\bi\s*(?:have|drive|am driving|own|'m driving)\b|\bmy car\b)\s*(?:is|=)?\s*(?:a|an|the)?\s*"
        r"([A-Za-z][A-Za-z0-9-]*(?:\s+[A-Za-z0-9][A-Za-z0-9-]*){0,3})",
        message, flags=re.IGNORECASE,
    )
    if match:
        words = match.group(1).split()
        # "electric car trip from Mumbai to X" is not a car called "Trip from": words that start a journey end the vehicle name.
        stop_words = {"with", "having", "and", "that", "which", "has", "at", "of", "in", "on", "for", "range", "battery", "currently", "current", "the", "a", "an",
                      "trip", "journey", "route", "drive", "ride", "from", "to", "between", "via", "travel", "tour", "plan", "going", "heading", "starting"}
        kept: list[str] = []
        for word in words:
            if word.lower() in stop_words or word.lower() in VEHICLE_TYPE_WORDS:
                break
            kept.append(word)
        if len(kept) >= 2:
            result["make"] = kept[0].title()
            result["model"] = " ".join(kept[1:])
        elif len(kept) == 1:
            result["model"] = kept[0]
    return result


def _settings_from(message: str) -> dict[str, float]:
    settings: dict[str, float] = {}
    reserve = _number(message, [r"reserve\s*(?:to|=|at|of)?\s*(\d+(?:\.\d+)?)\s*%?", r"(\d+(?:\.\d+)?)\s*%\s+reserve\b"])
    target = _number(message, [r"(?:charge|target charge)\s*(?:to|up to|=|at|only to)\s*(\d+(?:\.\d+)?)\s*%?"])
    corridor = _number(message, [r"corridor\s*(?:to|=|of)\s*(\d+(?:\.\d+)?)\s*km"])
    if reserve is not None:
        settings["reserve_percent"] = reserve
    if target is not None:
        settings["target_charge_percent"] = target
    if corridor is not None:
        settings["corridor_km"] = corridor
    return settings


def _vehicle_type_in(text: str) -> str | None:
    for word, value in VEHICLE_TYPE_WORDS.items():
        if word in {"ev", "gas"}:
            continue
        if re.search(rf"\b{word}\b", text, flags=re.IGNORECASE):
            return value
    return None


def _travel_mode_in(text: str) -> str | None:
    for phrase, value in sorted(TRAVEL_MODE_WORDS.items(), key=lambda kv: -len(kv[0])):
        if value == "car":
            continue
        if re.search(rf"\b{re.escape(phrase)}\b", text, flags=re.IGNORECASE):
            return value
    return None


# ---------------------------------------------------------------------------------------------
# References: "the second stop", "charging stop 4", "my destination", "that station"
# ---------------------------------------------------------------------------------------------
_CHARGING_REF = re.compile(
    rf"\b(?:the\s+)?(?:(?P<a>{_NUM_OR_ORD})\s+(?:actual\s+)?(?:charging|charger)\s+stop|(?:charging|charger)\s+stop\s*(?:number\s*|no\.?\s*|#)?(?P<b>{_NUM_OR_ORD}))\b",
    re.IGNORECASE,
)
_STOP_REF = re.compile(
    rf"\b(?:the\s+)?(?:(?P<a>{_NUM_OR_ORD})\s+(?:trip\s+)?(?:stop|waypoint)|(?:trip\s+)?(?:stop|waypoint)\s*(?:number\s*|no\.?\s*|#)?(?P<b>\d{{1,2}}))\b",
    re.IGNORECASE,
)
_DEST_REF = re.compile(r"\b(?:my|the|our)\s+(?:final\s+)?destination\b|\bwhere\s+i(?:'m|\s+am)\s+going\b|\bthe\s+(?:end|final)\s+(?:point|stop)\b", re.IGNORECASE)
_ORIGIN_REF = re.compile(r"\b(?:my|the|our)\s+(?:origin|starting\s+(?:point|place|location)|start(?:ing)?\s+point)\b", re.IGNORECASE)
_RESULT_REF = re.compile(rf"\b(?:the\s+)?(?:(?P<a>{_NUM_OR_ORD})\s+(?:result|one|option|place|hotel|restaurant|station)|(?:result|option|number|no\.?|#)\s*(?P<b>\d{{1,3}}))\b", re.IGNORECASE)
_FOCUS_REF = re.compile(r"\b(?:that|this|the\s+same)\s+(?:station|charger|place|hotel|restaurant|location|spot|one|city|town|area)\b|\b(?:there|that\s+place)\b", re.IGNORECASE)


def reference_in(text: str) -> dict[str, Any] | None:
    """The first structured reference in ``text`` as ``{scope, index}``, else ``None``."""
    match = _CHARGING_REF.search(text)
    if match:
        return {"scope": "charging_stop", "index": _index_from(match.group("a") or match.group("b"))}
    match = _STOP_REF.search(text)
    if match:
        return {"scope": "stop", "index": _index_from(match.group("a") or match.group("b"))}
    if _DEST_REF.search(text):
        return {"scope": "destination", "index": None}
    if _ORIGIN_REF.search(text):
        return {"scope": "origin", "index": None}
    match = _RESULT_REF.search(text)
    if match:
        return {"scope": "result", "index": _index_from(match.group("a") or match.group("b"))}
    if _FOCUS_REF.search(text):
        return {"scope": "focus", "index": None}
    if re.search(r"\b(?:near|around|close\s+to|next\s+to|of)\s+(?:me|my\s+(?:current\s+)?location)\b", text, flags=re.IGNORECASE):
        return {"scope": "me", "index": None}
    return None


# ---------------------------------------------------------------------------------------------
# Places in a trip request
# ---------------------------------------------------------------------------------------------
_PLACE_TAIL = re.compile(
    r"(?:\s+(?:with|using|driving|while|if|so that|because)\b.*|\s+(?:in|by|on|for)\s+(?:my|a|an|the|our)\b.*|[.!?;]\s.*|\s+(?:and|then)\s+(?:find|show|share|list|get|also|recommend|suggest|give|tell)\b.*|\s+please$|"
    # a second clause after the place: "to Goa, my car has 350 km range" / "to Goa and my battery is at 80%"
    r",\s*(?:my|our|i|i'm|we|vehicle|range|battery|currently|starting|using|with|and|then|also)\b.*|\s+(?:and\s+)?(?:my|our)\s+(?:car|vehicle|ev|battery|range)\b.*)",
    re.IGNORECASE,
)


def _trim_place(value: str) -> str:
    value = _PLACE_TAIL.sub("", value.strip())
    value = re.sub(r"^(?:the\s+)?", "", value, flags=re.IGNORECASE)
    return value.strip(" .,!?;:\"'")


def extract_places(text: str) -> list[str]:
    """Ordered places in a journey phrase, with 'via' stops between origin and destination."""
    patterns = [
        r"(?:\bfrom|\bbetween)\s+(?P<a>.+?)\s+(?:\bto\b|\band\b)\s+(?P<b>.+)$",
        r"^(?:please\s+)?(?:route|directions?|navigate|drive|trip|journey|travel|go)\s+(?P<a>[^,]+?)\s+to\s+(?P<b>.+)$",
        # "plan a EV trip Mumbai to Bhubaneswar": the two places follow the word trip, with no "from". Not "plan a trip to Goa" (nothing
        # between "trip" and "to"), nor "the journey time to Delhi" or "a trip with my wife to Goa".
        r"\b(?:trip|journey|drive|route|ride|tour)\s+(?!(?:to|from|with|for|in|at|on|by|via|near|around|and|of|plan|planner|time|times|"
        r"duration|distance|length|cost|details|options|back|home)\b)(?P<a>[a-z][a-z .'-]{1,40}?)\s+to\s+(?P<b>.+)$",
    ]
    for pattern in patterns:
        match = re.search(pattern, text.strip(), flags=re.IGNORECASE)
        if not match:
            continue
        first = _trim_place(match.group("a"))
        rest = match.group("b").strip()
        pieces = re.split(r"\s+(?:via|through|with stop at|stopping at|passing through)\s+", rest, maxsplit=1, flags=re.IGNORECASE)
        destination = _trim_place(pieces[0])
        middle: list[str] = []
        if len(pieces) == 2:
            tail = _PLACE_TAIL.sub("", pieces[1].strip())
            middle = [_trim_place(p) for p in re.split(r"\s*,\s*|\s+and\s+", tail) if _trim_place(p)]
        places = [first] + middle + [destination]
        if all(places) and len(places) >= 2 and first.lower() != destination.lower():
            return places
    arrow = re.split(r"\s*(?:->|→)\s*", text)
    if len(arrow) >= 2:
        return [_trim_place(p) for p in arrow if _trim_place(p)]
    return []


def _trip_request(text: str, context: dict[str, Any]) -> dict[str, Any] | None:
    places = extract_places(text)
    if len(places) < 2:
        return None
    lower = text.lower()
    # A search verb with a category ("find hotels from X to Y") is a search, not a trip.
    if _find_category(text) and _SEARCH_VERBS.search(lower) and not re.search(r"\b(?:plan|trip|journey|route|drive|travel|directions?)\b", lower):
        return None
    action: dict[str, Any] = {"intent": "plan_trip", "places": places, "ev_required": bool(_EV_WORDS.search(lower))}
    vehicle_updates = _vehicle_update(text) if _EV_WORDS.search(lower) or "range" in lower else {}
    if vehicle_updates:
        action["vehicle_updates"] = vehicle_updates
    settings = _settings_from(text)
    if settings:
        action["settings"] = settings
    vehicle_type = _vehicle_type_in(text)
    if vehicle_type:
        action["vehicle_type"] = vehicle_type
    elif action["ev_required"]:
        action["vehicle_type"] = "electric"
    mode = _travel_mode_in(text)
    if mode:
        action["travel_mode"] = mode
    return action


# ---------------------------------------------------------------------------------------------
# Compound: one message with a trip plus POI/charging requests (kept from the original design)
# ---------------------------------------------------------------------------------------------
def _compound_trip_request(text: str, context: dict[str, Any]) -> dict[str, Any] | None:
    lower = text.lower()
    plan_match = re.search(
        r"(?:plan|plan an|plan a)\s+(?:an?\s+)?(?:[a-z]+\s+)?(?:car\s+)?(?:trip|journey)\s+from\s+(.+?)\s+to\s+(.+?)(?:\s+via\s+(.+?))?(?:[.!?]|$)",
        text, flags=re.IGNORECASE,
    )
    if not plan_match or not any(k in lower for k in ("restaurant", "hotel", "tourist", "recommended charging", "charging stop", "fuel", "petrol pump")):
        return None
    origin = _trim_place(plan_match.group(1))
    destination = _trim_place(plan_match.group(2))
    via = (plan_match.group(3) or "").strip()
    places = [origin]
    if via:
        places.extend([_trim_place(p) for p in re.split(r"\s*(?:,|and)\s*", via) if p.strip()])
    places.append(destination)

    requests: list[dict[str, Any]] = []
    categories = ("restaurant", "hotel", "tourist attraction", "tourist places", "attractions", "fuel station", "petrol pump")
    for category in categories:
        for match in re.finditer(rf"{re.escape(category)}s?\s+(?:near|at|around|in|on)\s+(?:the\s+)?([^.!?;]+)", text, flags=re.IGNORECASE):
            ref = match.group(1).strip().rstrip(" ,")
            ref = re.split(r"\s+(?:and|then)\s+(?=(?:restaurants?|hotels?|tourist|attractions?|fuel|petrol))", ref, maxsplit=1, flags=re.IGNORECASE)[0].strip()
            ref_lower = ref.lower()
            request: dict[str, Any] = {"category": canonical_category(category) or category.replace(" ", "_"), "scope": "route", "index": None, "radius_km": 3.0}
            reference = reference_in(ref_lower)
            if reference and reference["scope"] in {"charging_stop", "destination", "origin", "stop"}:
                request["scope"], request["index"] = reference["scope"], reference["index"]
            else:
                for i, place in enumerate(places):
                    if place.lower() in ref_lower or ref_lower in place.lower():
                        request["scope"], request["index"] = "stop", i
                        break
            requests.append(request)
    recommend = bool(re.search(r"\b(?:recommend|recommended|suggest|show)\b.*\b(?:charging|charger)\s+stop\b", lower))
    ev_required = bool(_EV_WORDS.search(lower))
    return {
        "intent": "compound_trip_request",
        "places": places,
        "ev_required": ev_required,
        "vehicle_updates": _vehicle_update(text) if ev_required else {},
        "settings": _settings_from(text),
        "recommend_charger": recommend,
        "poi_requests": requests,
    }


# ---------------------------------------------------------------------------------------------
# Route edits
# ---------------------------------------------------------------------------------------------
_ROUTE_NOUN = r"(?:route|trip|journey|itinerary|path|plan)"
_ROUTE_TAIL = re.compile(
    rf"(?:\s+(?:to|into|onto|on|in|along|for)\s+(?:my|the|this|our|current)\s+(?:current\s+)?{_ROUTE_NOUN}\b.*"
    r"|\s*(?:,|\band\b|\bthen\b)\s*(?:re-?calculate|update|re-?plan|re-?route|show|display|find|give|tell|also)\b.*"
    r"|\s+(?:please|thanks|thank you)\b.*)$",
    re.IGNORECASE,
)


def _route_place(raw: str) -> tuple[str, dict[str, str]]:
    """Clean a place captured from an edit phrase and pull out position hints."""
    hints: dict[str, str] = {}
    text = raw.strip()
    between = re.search(r"^(?P<p>.+?)\s+between\s+(?P<a>.+?)\s+and\s+(?P<b>.+)$", text, flags=re.IGNORECASE)
    if between:
        text = between.group("p")
        hints["after"] = _ROUTE_TAIL.sub("", between.group("a")).strip(" .,")
        hints["before"] = _ROUTE_TAIL.sub("", between.group("b")).strip(" .,")
    else:
        position = re.search(r"^(?P<p>.+?)\s+(?P<kind>after|before)\s+(?P<ref>.+)$", text, flags=re.IGNORECASE)
        if position:
            text = position.group("p")
            hints[position.group("kind").lower()] = _ROUTE_TAIL.sub("", position.group("ref")).strip(" .,")
    text = _ROUTE_TAIL.sub("", text).strip()
    text = re.sub(r"\s+(?:stop|waypoint|stopover)$", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"^(?:the\s+|a\s+|an\s+)", "", text, flags=re.IGNORECASE).strip(" .,!?;:\"'")
    return text, hints


def _route_edit(text: str, context: dict[str, Any]) -> dict[str, Any] | None:
    lower = text.lower()
    # --- remove -----------------------------------------------------------------------
    remove = re.match(
        rf"^(?:please\s+)?(?:remove|delete|drop|skip|cancel)\s+(?:the\s+)?(?:(?:stop|waypoint)\s*(?:number\s*|no\.?\s*|#)?(?P<n>\d{{1,2}})|(?P<o>{_ORD})\s+(?:stop|waypoint)|(?:stop\s+)?(?P<name>.+?))(?:\s+(?:from|off)\s+(?:my|the|this)\s+{_ROUTE_NOUN})?(?:\s+and\s+.*)?$",
        text, flags=re.IGNORECASE,
    )
    if remove and not re.search(r"\b(?:charging|charger)\b", lower):
        if remove.group("n"):
            return {"intent": "modify_trip", "operation": "remove_stop", "index": int(remove.group("n")) - 1}
        if remove.group("o"):
            return {"intent": "modify_trip", "operation": "remove_stop", "index": _index_from(remove.group("o"))}
        name, _ = _route_place(remove.group("name") or "")
        if name and not re.search(r"^(?:all|everything|it|them|results?|markers?|pins?|filters?)$", name, flags=re.IGNORECASE):
            return {"intent": "modify_trip", "operation": "remove_stop", "place": name}

    # --- endpoints ----------------------------------------------------------------------
    dest = re.search(r"(?:change|set|make|update|switch)\s+(?:my\s+|the\s+)?destination\s+(?:to|as)\s+(.+)$", text, flags=re.IGNORECASE)
    if dest:
        return {"intent": "modify_trip", "operation": "set_destination", "place": _route_place(dest.group(1))[0]}
    dest2 = re.search(r"(?:make|set)\s+(.+?)\s+(?:my|the)\s+(?:new\s+)?destination\b", text, flags=re.IGNORECASE)
    if dest2:
        return {"intent": "modify_trip", "operation": "set_destination", "place": _route_place(dest2.group(1))[0]}
    origin = re.search(r"(?:change|set|make|update|switch)\s+(?:my\s+|the\s+)?(?:origin|start(?:ing)?(?:\s+point)?)\s+(?:to|as)\s+(.+)$", text, flags=re.IGNORECASE)
    if origin:
        return {"intent": "modify_trip", "operation": "set_origin", "place": _route_place(origin.group(1))[0]}
    origin2 = re.search(r"\bstart\s+(?:from|at)\s+(.+?)\s+instead\b", text, flags=re.IGNORECASE)
    if origin2:
        return {"intent": "modify_trip", "operation": "set_origin", "place": _route_place(origin2.group(1))[0]}

    # --- EV charging stop edit (separate from waypoints) -----------------------------------
    charging_add = re.search(r"(?:add|insert|include)\s+(?:a\s+)?(?:charging|charger)\s+stop\s+(?:at|near|in)\s+(.+)$", text, flags=re.IGNORECASE)
    if charging_add:
        return {"intent": "modify_trip", "operation": "add_charging_stop", "place": _route_place(charging_add.group(1))[0]}

    # --- add a waypoint ---------------------------------------------------------------------
    # A category with a search verb ("add ... restaurants near Pune") is a search, not an edit.
    # "add a stop between A and B" names no place to add: ask which one.
    if re.match(r"^(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?(?:add|insert|put|include)\s+(?:in\s+)?(?:a\s+|another\s+|one\s+more\s+)?(?:new\s+)?(?:stop|waypoint|stopover)\s+(?:in\s+)?between\s+\S.*\band\b", text, flags=re.IGNORECASE):
        between = re.search(r"between\s+(?P<a>.+?)\s+and\s+(?P<b>.+?)[.?! ]*$", text, flags=re.IGNORECASE)
        return {
            "intent": "clarify",
            "question": "Which place should I add as a stop"
            + (f" between {_trim_place(between.group('a'))} and {_trim_place(between.group('b'))}" if between else "")
            + "? Name a city or town and I'll insert it there.",
            "pending": {"intent": "modify_trip", "operation": "add_stop",
                        **({"after": _trim_place(between.group("a")), "before": _trim_place(between.group("b"))} if between else {})},
        }
    candidates = [
        re.match(r"^(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?(?:please\s+)?(?:add|include|insert|put|append)\s+(?:in\s+)?(?:a\s+|another\s+|one\s+more\s+)?(?:new\s+)?(?:(?:stop|waypoint|stopover|halt)\s*)?(?:(?:at|in|near|for|called|named|of)\s+)?(?P<p>.+)$", text, flags=re.IGNORECASE),
        re.match(r"^(?:i\s+(?:want|need|would like|'d like)\s+to\s+|let'?s\s+|please\s+)?(?:stop|halt|break)\s+(?:over\s+)?(?:at|in)\s+(?P<p>.+)$", text, flags=re.IGNORECASE),
        re.match(r"^(?:(?:i\s+(?:want|need)\s+to\s+|can\s+we\s+|let'?s\s+|please\s+)?(?:go|travel|drive|route|pass|stop|detour)\s+(?:via|through|by way of|past)|via|through)\s+(?P<p>.+)$", text, flags=re.IGNORECASE),
    ]
    for match in candidates:
        if not match:
            continue
        place, hints = _route_place(match.group("p"))
        if not place or len(place) > 80:
            continue
        if re.match(r"^(?:charging|charger)\b", place, flags=re.IGNORECASE):
            continue
        category = _find_category(place)
        if category and re.search(r"\b(?:near|around|in|at|along|within)\b", place, flags=re.IGNORECASE):
            continue  # "add restaurants near Pune" is a search
        if category and canonical_category(place):
            continue  # "add hotels" alone has no place to add
        action: dict[str, Any] = {"intent": "modify_trip", "operation": "add_stop", "place": place}
        action.update({k: v for k, v in hints.items() if v})
        return action
    return None


# ---------------------------------------------------------------------------------------------
# Category and searches
# ---------------------------------------------------------------------------------------------
_ME_PLACE = re.compile(r"^(?:me|myself|my\s+(?:current\s+)?(?:location|position|place)|where\s+i\s+am|here)$", re.IGNORECASE)
_MIDPOINT_WORDS = re.compile(r"\b(?:half[- ]?way|mid[- ]?way|mid[- ]?point|(?:the\s+)?middle\s+of|half[- ]?way\s+point)\b", re.IGNORECASE)
_ROUTE_THING = r"(?:trip|route|journey|drive|way|path|itinerary)"


def is_me_place(text: str) -> bool:
    return bool(_ME_PLACE.match(text.strip(" .,!?")))


def is_midpoint_place(text: str) -> bool:
    return bool(_MIDPOINT_WORDS.search(text)) and bool(re.search(_ROUTE_THING, text, flags=re.IGNORECASE))


_FORBIDS_ROUTE_CHANGE = re.compile(
    r"\b(?:don'?t|do\s+not|dont|never|no\s+need\s+to)\s+(?:\w+\s+)?(?:change|modify|alter|touch|update|edit|re-?route|re-?plan|re-?calculate|reset)\b[^.?!]*\b(?:route|trip|journey|stops?|itinerary|destination|plan)\b"
    r"|\bwithout\s+(?:changing|modifying|altering|touching|updating|editing|re-?routing|re-?planning)\s+(?:my|the|this|our)\s+(?:\w+\s+)?(?:route|trip|journey|stops?|itinerary|plan)\b"
    r"|\bkeep\s+(?:my|the|this|our)\s+(?:current\s+)?(?:route|trip|journey|stops|itinerary)\s+(?:as\s+is|the\s+same|unchanged|intact)\b"
    r"|\b(?:leave|keep)\s+(?:my|the|this|our)\s+(?:route|trip|journey|stops|itinerary)\s+(?:alone|as\s+it\s+is)\b",
    re.IGNORECASE,
)


def forbids_route_change(text: str) -> bool:
    """True when the user says to leave the route alone ("but don't change my route")."""
    return bool(_FORBIDS_ROUTE_CHANGE.search(text))

def _find_category(text: str) -> tuple[str, str] | None:
    for phrase in sorted(CATEGORY_ALIASES, key=len, reverse=True):
        if re.search(rf"(?<![\w]){re.escape(phrase)}(?![\w])", text, flags=re.IGNORECASE):
            return CATEGORY_ALIASES[phrase], phrase
    return None


def _extract_count(text: str, default: int = 80) -> int:
    match = re.search(r"\b(?:find|show|give|share|list|search|get)\s+(?:me\s+)?(?:the\s+|top\s+)?(\d{1,3})(?!\s*[-]?\s*star)\b", text, flags=re.IGNORECASE)
    if not match:
        return default
    return max(1, min(int(match.group(1)), 100))


_UNIT_KM = {"km": 1.0, "kms": 1.0, "kilometer": 1.0, "kilometers": 1.0, "kilometre": 1.0, "kilometres": 1.0,
            "m": 0.001, "meter": 0.001, "meters": 0.001, "metre": 0.001, "metres": 0.001}
_DISTANCE = r"(\d+(?:\.\d+)?)\s*(km|kms|kilomet(?:er|re)s?|m|met(?:er|re)s?)\b"


def _radius_in(text: str) -> float | None:
    """A radius in km from "within 5 km", "within 500 metres", "radius of 2 km", "3 km around". None if not stated."""
    for pattern in (
        rf"within\s*(?:a\s+)?(?:radius\s+of\s+)?{_DISTANCE}",
        rf"radius\s*(?:of|=)\s*{_DISTANCE}",
        rf"{_DISTANCE}\s+(?:radius|around|of)\b",
    ):
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            unit = match.group(2).lower()
            return float(match.group(1)) * _UNIT_KM.get(unit, 1.0)
    return None


def _search_query(before: str) -> str:
    return re.sub(r"\s+", " ", before).strip(" ,")


def _generic_location_search(text: str, context: dict[str, Any]) -> dict[str, Any] | None:
    """'5 star hotel in Goa', 'best pub near Goa', 'hospitals within 5 km of Pune', 'airports in Odisha'.

    The connector decides the scope: "in" names an area (state, district, city) and lists what is inside it;
    "near/around/at/by" and "within N km of" are a point plus a radius.
    """
    count = _extract_count(text)
    cleaned = re.sub(r"\b(?:find|show|search|give|share|list|get|suggest|recommend|tell\s+me\s+about|i\s+(?:want|need)|looking\s+for)\s+(?:me\s+)?(?:some\s+|any\s+|a\s+few\s+)?", "", text, count=1, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"^(\d{1,3})(?!\s*[-]?\s*star)\s+", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"^(?:where\s+can\s+(?:i|we)\s+(?:find|found|get)|can\s+(?:i|you)\s+find|please\s+find|are\s+there\s+any|is\s+there\s+(?:a|an|any))\s+", "", cleaned, flags=re.IGNORECASE).strip()
    # "within N km of X" first: it is a radius, and its own "of" must not be mistaken for an area connector.
    location_match = re.search(rf"\s+within\s+{_DISTANCE}\s+of\s+(?P<p>.+)$", cleaned, flags=re.IGNORECASE)
    connector = "within"
    if not location_match:
        location_match = re.search(
            r"\s+(?P<conn>near|around|by|inside|across|throughout|in|at|close to|next to)\s+(?P<p>.+?)(?:\s+(?:within|and\s+(?:show|list|give|share|display|tell|how|what|which|where|their|also|sort|rank|compare|get)|with\s+their)\b.*)?$",
            cleaned, flags=re.IGNORECASE,
        )
        if not location_match:
            return None
        connector = location_match.group("conn").lower()
    place = location_match.group("p").strip(" .,!?")
    place = re.split(
        r"\s+(?:and\s+(?:show|list|give|share|display|tell|how|what|which|where|their|also|sort|rank|compare|get)\b|with\s+(?:the|its)\b|with\s+their\b|sorted\b|ordered\b|that\s+are\b|which\s+are\b)",
        place, maxsplit=1, flags=re.IGNORECASE,
    )[0].strip(" .,!?")
    place = re.sub(r"\s+within\s+[\d.]+\s*(?:km|kms|m|metres?|meters?)\b.*$", "", place, flags=re.IGNORECASE).strip(" .,!?")
    place = re.sub(r",?\s+(?:district|state|city|region)$", "", place, flags=re.IGNORECASE).strip(" .,!?")
    # Who it is for is a wish, not part of the place ("... near the palace for a family"). Trim it before reading the landmark.
    place = re.sub(
        r"\s+(?:for|with)\s+(?:a\s+|an\s+|the\s+|my\s+|our\s+)?(?:family|families|kids?|children|couples?|friends|babies|baby|toddlers?|seniors?|groups?|"
        r"parents|solo\s+travell?ers?|honeymoon(?:ers)?|wife|husband)\b.*$", "", place, flags=re.IGNORECASE).strip(" .,!?")
    # "Mysuru near the palace" is a landmark inside a city. A geocoder reads that phrase as some shop on a street of that name;
    # "palace, Mysuru" (landmark first, then the city) finds the landmark itself.
    landmark = re.fullmatch(r"(?P<city>[A-Za-z][A-Za-z .'-]{1,40}?)\s+(?:near|around|by|close\s+to|next\s+to|opposite)\s+(?:the\s+)?(?P<lm>[A-Za-z][A-Za-z .'-]{2,40})", place, flags=re.IGNORECASE)
    if landmark and not _find_category(landmark.group("lm")):
        place = f"{landmark.group('lm').strip()}, {landmark.group('city').strip()}"
        # "in <city> near <landmark>" is a point search around the landmark, never a list of the whole city.
        connector = "near"
    # Who it is for is a wish, not part of the place: "hotels in Mysuru for a family" searches Mysuru.
    place = re.sub(
        r"\s+(?:for|with)\s+(?:a\s+|an\s+|the\s+|my\s+|our\s+)?(?:family|families|kids?|children|couples?|friends|babies|baby|toddlers?|seniors?|groups?|"
        r"parents|solo\s+travell?ers?|honeymoon(?:ers)?|wife|husband)\b.*$", "", place, flags=re.IGNORECASE).strip(" .,!?")

    stop_match = re.fullmatch(r"(.+?)\s+(?:trip\s+)?stop", place, flags=re.IGNORECASE)
    if stop_match:
        candidate = stop_match.group(1).strip()
        for stop in context.get("stops", []) or []:
            stop_name = str(stop.get("name") or stop.get("display_name") or "").strip() if isinstance(stop, dict) else str(stop).strip()
            if stop_name and candidate.lower() == stop_name.lower():
                place = stop_name
                break
    before = cleaned[: location_match.start()].strip(" ,")
    if not before or not place:
        return None
    found = _find_category(before)
    # No category and no search verb: this is not a search ("add a stop at Hyderabad").
    if not found and not _CATEGORYLESS_SEARCH.search(text):
        return None
    category = found[0] if found else "place"
    explicit_radius = _radius_in(text)

    if is_me_place(place):
        return {
            "intent": "find_pois", "category": category, "scope": "me", "index": None, "query": before,
            "radius_km": explicit_radius if explicit_radius is not None else 5.0, "radius_explicit": explicit_radius is not None, "limit": count,
        }
    if is_midpoint_place(place) or is_midpoint_place(text):
        return {
            "intent": "find_pois", "category": category, "scope": "midpoint", "index": None, "query": before,
            "radius_km": explicit_radius if explicit_radius is not None else 5.0, "radius_explicit": explicit_radius is not None, "limit": count,
        }
    if connector in {"in", "inside", "across", "throughout"} and spec_for(category) is not None and explicit_radius is None:
        return {"intent": "find_pois_in_area", "category": category, "query": before, "area": place, "limit": count}
    return {
        "intent": "find_pois_near_location",
        "category": category,
        "query": before,
        "place": place,
        "radius_km": explicit_radius if explicit_radius is not None else 5.0,
        "radius_explicit": explicit_radius is not None,
        "limit": count,
    }


_ANCHOR = re.compile(
    rf"^(?:(?:please\s+)?(?:find|show|search(?:\s+for)?|list|get|give(?:\s+me)?|share)\s+(?:me\s+)?)?(?P<what>.+?)\s+"
    rf"(?:within\s+{_DISTANCE}\s+of|near(?:by)?|around|close\s+to|next\s+to|by)\s+"
    r"(?:a|an|any|one\s+of\s+the|some|the)\s+(?P<anchor>[a-z][a-z &-]{2,30}?)\s+"
    r"(?:near|in|at|around|close\s+to|next\s+to|by|on)\s+(?P<place>.+?)[.?! ]*$",
    re.IGNORECASE,
)


def _anchor_search(text: str, context: dict[str, Any]) -> dict[str, Any] | None:
    """"restaurants within 500 metres of a hotel near Baga Beach": the hotel is a stepping stone, not the search centre."""
    match = _ANCHOR.match(text.strip())
    if not match:
        return None
    what = _find_category(match.group("what"))
    anchor = _find_category(match.group("anchor"))
    if not what or not anchor or what[0] == anchor[0]:
        return None
    place = _trim_place(match.group("place"))
    if not place or reference_in(place) or is_me_place(place):
        return None
    radius = None
    if match.group(2):
        radius = float(match.group(2)) * _UNIT_KM.get(match.group(3).lower(), 1.0)
    return {
        "intent": "find_pois_near_anchor", "category": what[0], "anchor_category": anchor[0], "place": place,
        "query": match.group("what"), "radius_km": radius if radius is not None else 1.0, "limit": _extract_count(text),
    }


def _midpoint_request(text: str, context: dict[str, Any]) -> dict[str, Any] | None:
    """Searches around the halfway point of a route: "coffee break halfway between A and B", "lunch within 10 km of
    the halfway point of my trip". The midpoint is calculated from the real route, not from the end points."""
    if not _MIDPOINT_WORDS.search(text):
        return None
    found = _find_category(text)
    if not found:
        return None
    category, phrase = found
    radius = _radius_in(text)
    request = {
        "category": category, "scope": "midpoint", "index": None, "radius_km": radius if radius is not None else 5.0,
        "radius_explicit": radius is not None, "limit": _extract_count(text), "query": phrase, "fraction": 0.5,
    }
    between = re.search(r"between\s+(?P<a>.+?)\s+and\s+(?P<b>.+?)(?:[.?!;]|\s+(?:and|then|but|so|with|please)\b|$)", text, flags=re.IGNORECASE)
    if between:
        a, b = _trim_place(between.group("a")), _trim_place(between.group("b"))
        if a and b and a.lower() != b.lower():
            return {"intent": "compound_trip_request", "places": [a, b], "ev_required": False, "vehicle_updates": {}, "settings": {},
                    "recommend_charger": False, "poi_requests": [request]}
    return {"intent": "find_pois", **request}


_ALONG_ROUTE = re.compile(
    r"\b(?:along|on|across|alongside|enroute|en\s+route\s+(?:on|along)?|through(?:out)?)\s+(?:the\s+|my\s+|this\s+|current\s+|our\s+)?(?:route|way|road|trip|journey|highway|drive|path)\b|\bon\s+the\s+way\b|\bbetween\s+(?:the\s+)?stops\b",
    re.IGNORECASE,
)


def _along_route_search(text: str, context: dict[str, Any]) -> dict[str, Any] | None:
    if not _ALONG_ROUTE.search(text):
        return None
    if not (_SEARCH_VERBS.search(text) or _find_category(text)):
        return None
    found = _find_category(text)
    if not found:
        return None
    category, phrase = found
    radius = _radius_in(text)
    return {
        "intent": "find_pois", "category": category, "scope": "route", "index": None,
        "radius_km": radius or 3.0, "radius_explicit": radius is not None, "limit": _extract_count(text),
        "query": phrase if category != "ev_charging" else None,
    }


def _near_reference_search(text: str, context: dict[str, Any]) -> dict[str, Any] | None:
    found = _find_category(text)
    if not found:
        return None
    category, phrase = found
    if not re.search(r"\b(?:near|around|at|in|by|close to|next to|within|from)\b", text, flags=re.IGNORECASE):
        return None
    reference = reference_in(text)
    if not reference:
        return None
    radius = _radius_in(text)
    return {
        "intent": "find_pois", "category": category, "scope": reference["scope"], "index": reference["index"],
        "radius_km": radius or 5.0, "radius_explicit": radius is not None, "limit": _extract_count(text), "query": phrase,
    }


# ---------------------------------------------------------------------------------------------
# Route info, EV, options and follow-ups
# ---------------------------------------------------------------------------------------------
def _route_info(text: str, context: dict[str, Any]) -> dict[str, Any] | None:
    lower = text.lower()
    if not re.search(r"\b(?:distance|how\s+far|how\s+long|eta|travel\s+time|drive\s+time|duration|how\s+many\s+(?:km|kilomet))", lower):
        return None
    if extract_places(text):
        return None  # "distance from A to B" is a trip request
    reference = reference_in(text)
    if reference and reference["scope"] in {"stop", "destination", "origin"}:
        return {"intent": "route_info", "target": reference["scope"], "index": reference["index"]}
    named = re.search(r"\b(?:to|till|until|reach|at)\s+(?!the\b|my\b)(?P<p>[A-Z][\w .'-]{1,40})", text)
    if named:
        return {"intent": "route_info", "target": "named", "place": named.group("p").strip(" .?!")}
    return {"intent": "route_info", "target": "total", "index": None}


def _vehicle_or_ev(text: str, context: dict[str, Any]) -> dict[str, Any] | None:
    lower = text.lower()
    # Fuel type / travel mode statements are options, not EV vehicle updates.
    fuel = _vehicle_type_in(text)
    has_ev_spec = bool(re.search(r"\b(?:range|battery|kwh|charge|charging)\b", lower)) or bool(_EV_WORDS.search(lower) and re.search(r"\d", lower))
    if fuel and not has_ev_spec and re.search(r"\b(?:car|vehicle|drive|driving|have|own|using|use|switch|set|mode|journey|trip)\b", lower):
        return {"intent": "set_trip_options", "vehicle_type": fuel}
    mode = _travel_mode_in(text)
    if mode and re.search(r"\b(?:switch|set|use|change|mode|instead)\b", lower):
        return {"intent": "set_trip_options", "travel_mode": mode}

    fields = _vehicle_update(text)
    numeric_fields = {k: v for k, v in fields.items() if k in {"max_range_km", "current_battery_percent", "battery_capacity_kwh"}}
    if numeric_fields and re.search(r"\b(?:vehicle|car|range|battery|kwh|charge|ev)\b", lower):
        return {"intent": "update_vehicle", "vehicle_updates": fields}

    settings = _settings_from(text)
    if settings:
        return {"intent": "update_settings", "settings": settings}

    battery_change = _number(text, [r"(?:start|starting|leave)\s*(?:with|at)\s*(\d+(?:\.\d+)?)\s*%", r"what if i (?:start|leave)\s*(?:with|at)\s*(\d+(?:\.\d+)?)\s*%"])
    if battery_change is not None:
        return {"intent": "simulate", "field": "battery_percent", "value": battery_change}

    if re.search(r"\b(?:do i need to charge|can i (?:make|reach|complete)|will (?:my|the) (?:car|ev|battery)|ev (?:trip|plan|planner|feasib)|electric (?:trip|plan)|plan (?:the )?charging|charging (?:plan|stops?)\s*(?:for|on)?\s*(?:this|my|the)?\s*(?:trip|route)?$|enable (?:the )?ev)", lower):
        return {"intent": "ev_plan"}
    return None


def _ev_detail(text: str, context: dict[str, Any]) -> dict[str, Any] | None:
    lower = text.lower()
    has_detail = re.search(r"\b(?:detailed?\s+information|details?|information|info)\b", lower)
    numbered = re.search(
        r"\b(\d+|first|second|third|fourth|fifth|1st|2nd|3rd|4th|5th)\s+(?:actual\s+)?(?:charging|charger)\s+stop\b|"
        r"\b(?:charging|charger)\s+stop\s*(?:number\s*)?(\d+|first|second|third|fourth|fifth|1st|2nd|3rd|4th|5th)\b",
        lower,
    )
    # "details of charging stop 2", "tell me about the second charging stop", "show charging stop 9":
    # a numbered stop with no place category in the sentence is a lookup in the EV plan (which may then say it doesn't exist).
    wants_places = re.search(r"\b(?:restaurants?|cafes?|hotels?|hospitals?|pharmac\w+|fuel|petrol|diesel|supermarkets?|stores?|atms?|banks?|parking|malls?|pubs?|bars?|places?|things)\b", lower)
    if numbered and (has_detail or not wants_places):
        raw = next((g for g in numbered.groups() if g), "1").lower()
        number = _ordinal(raw) or 1
        return {"intent": "show_charging_stop", "index": max(0, number - 1)}
    charging_stop_search = re.search(
        r"(?:find|show|search|give|share)\s+(?:me\s+)?(?:the\s+)?(\d{1,3})?\s*(restaurants?|cafes?|hotels?|hospitals?|pharmacies?|fuel stations?|supermarkets?|convenience stores?|tourist places?|tourist attractions?|pubs?|bars?|nightlife|shopping malls?|banks?|atms?|car repair|car wash)\s+(?:in|near|around|at)\s+(?:the\s+)?(first|1st|second|2nd|third|3rd|fourth|4th|fifth|5th)\s+(?:actual\s+)?(?:charging|charger)\s+stop",
        text, flags=re.IGNORECASE,
    )
    if charging_stop_search:
        raw_category = charging_stop_search.group(2).lower()
        category = canonical_category(raw_category.rstrip("s")) or canonical_category(raw_category) or raw_category.replace(" ", "_")
        return {
            "intent": "find_pois", "category": category, "scope": "charging_stop",
            "index": _index_from(charging_stop_search.group(3)), "radius_km": 5.0,
            "limit": int(charging_stop_search.group(1)) if charging_stop_search.group(1) else 80, "query": raw_category,
        }
    charger_near = re.search(r"(?:show|find|search|give)\s+(?:me\s+)?(?:a\s+|some\s+|the\s+)?(?:ev\s+)?charg(?:er|ers|ing station|ing stations)\s+(?:near|around|by)\s+(.+?)(?:\s+within\s+(\d+(?:\.\d+)?)\s*km)?$", text, flags=re.IGNORECASE)
    if charger_near:
        return {"intent": "search_charger_nearby", "place": charger_near.group(1).strip().rstrip("."), "radius_km": float(charger_near.group(2)) if charger_near.group(2) else 25.0}
    if re.search(r"\b(?:charger|chargers|charging stations?)\b", lower) and re.search(r"\b(?:on|along)\s+(?:(?:the|my|current)\s+)?route\b", lower):
        return {"intent": "find_pois", "category": "ev_charging", "scope": "route", "radius_km": 0.0, "limit": 100}
    if re.search(r"\b(?:recommend|suggest|show)\b.*\b(?:charging|charger)\s+stop\b|\b(?:recommended|best|nearest)\s+(?:charging|charger)\b|\bfind\s+(?:me\s+)?(?:a\s+)?(?:charging|charger)\s+stop\b", lower):
        return {"intent": "recommend_charger"}
    return None


_QUESTION_START = re.compile(r"^(?:what|why|how|who|when|which|where|is|are|can|could|do|does|tell me|explain|describe|compare|should|will)\b", re.IGNORECASE)


# ---------------------------------------------------------------------------------------------
# Named places, "nearest X to Y", and open questions about places
# ---------------------------------------------------------------------------------------------
_WHERE_IS = re.compile(r"^(?:please\s+)?(?:where\s+is|where's|locate|show\s+me\s+where\s+is)\s+(?P<p>.+?)[\s?.!]*$", re.IGNORECASE)
_NOT_A_NAME = re.compile(r"^(?:a|an|any|some|nearest|closest|nearby|good|best|my|this|that|there|here|it)\b|^the\s+(?:nearest|closest|best|cheapest|top|first|second|third|last|next)\b", re.IGNORECASE)


def _where_is(text: str) -> dict[str, Any] | None:
    """"Where is Kempegowda International Airport?" names ONE place. A category word inside the name ("Airport") must
    not turn it into a category search, and with a trip on screen it must never search along that trip."""
    match = _WHERE_IS.match(text)
    if not match:
        return None
    name = match.group("p").strip(" ?.!,")
    if not name or len(name.split()) > 8 or _NOT_A_NAME.match(name) or is_me_place(name):
        return None
    bare = re.sub(r"^the\s+", "", name, flags=re.IGNORECASE)
    if canonical_category(bare) or reference_in(name):
        return None  # "where is the hotel", "where is my destination": not a named place
    return {"intent": "geocode", "place": name, "named": True}


_NEAREST = re.compile(
    r"\b(?:nearest|closest)\s+(?P<what>[a-z][a-z' -]{1,40}?)\s+(?:from|to|near|around|of|in|at)\s+(?P<place>.+?)[\s?.!]*$"
    r"|\b(?P<what2>[a-z][a-z' -]{1,40}?)\s+(?:nearest|closest)\s+(?:to|from)\s+(?P<place2>.+?)[\s?.!]*$",
    re.IGNORECASE,
)
_NOT_A_SEARCH = re.compile(r"\b(?:plan|route|directions?|add|remove|stops?|via|along)\b", re.IGNORECASE)


def _nearest_search(text: str, context: dict[str, Any]) -> dict[str, Any] | None:
    """"nearest airport from Puri": the closest ones to a NAMED place, widening the search until something is found.
    It is a search around that place, never along a trip that happens to be on screen."""
    if _NOT_A_SEARCH.search(text):
        return None
    match = _NEAREST.search(text)
    if not match:
        return None
    what = match.group("what") or match.group("what2") or ""
    place = (match.group("place") or match.group("place2") or "").strip(" ?.!,")
    place = re.split(r"\s+(?:and\s+(?:show|list|give|tell|how|what|which)|please)\b", place, maxsplit=1, flags=re.IGNORECASE)[0].strip(" ?.!,")
    found = _find_category(what)
    if not found or not place or len(place.split()) > 8:
        return None
    category, _phrase = found
    common = {"category": category, "query": None, "radius_km": 5.0, "radius_explicit": False, "limit": 5, "nearest": True}
    # "nearest cafe to me", "nearest hospital to my destination": the centre is the user's location or a stop of the
    # trip, not the trip itself, so the route is never searched along.
    if is_me_place(place):
        return {"intent": "find_pois", "scope": "me", "index": None, **common}
    if is_midpoint_place(place):
        return {"intent": "find_pois", "scope": "midpoint", "index": None, **common}
    reference = reference_in(place)
    if reference and reference["scope"] != "route":
        return {"intent": "find_pois", "scope": reference["scope"], "index": reference["index"], **common}
    return {"intent": "find_pois_near_location", "place": place, **common}


_SUBJECT_CUT = re.compile(r"\s+(?:for|in terms of|based on|with|considering|regarding|during|when|if)\b.*$", re.IGNORECASE)
_ASK_ABOUT = re.compile(
    r"^(?:please\s+)?(?:(?:can|could)\s+you\s+)?(?:tell\s+me|give\s+me|explain|describe)\s+(?:more\s+|a\s+bit\s+more\s+)?(?:about\s+)?(?P<s>.+?)[\s?.!]*$",
    re.IGNORECASE,
)
_ASK_COMPARE = re.compile(r"^(?:please\s+)?(?:compare|contrast)\s+(?P<s>.+?)[\s?.!]*$|^(?P<s2>.+?\s+(?:vs\.?|versus)\s+.+?)[\s?.!]*$", re.IGNORECASE)
_ASK_PROS_CONS = re.compile(r"\b(?:pros\s+and\s+cons|advantages\s+and\s+disadvantages)\s+of\s+(?P<s>.+?)[\s?.!]*$", re.IGNORECASE)
_PRONOUN_SUBJECT = re.compile(r"^(?:it|them|they|these|those|this|that|the\s+(?:first|second|third|fourth|fifth|last|\w+)\s+(?:one|place|result)|(?:the\s+)?(?:first|second|third|last)\s+one)\b", re.IGNORECASE)


_ROUTE_PLAN_WORDS = re.compile(r"\b(?:plan|route|navigate|directions?|drive|take\s+me|get\s+me|trip\s+(?:from|to)|journey\s+(?:from|to))\b|\bfrom\s+.+?\s+to\s+.+", re.IGNORECASE)
_ROUTE_EDIT_WORDS = re.compile(
    r"\b(?:add|include|insert|remove|delete|drop|skip|replace|swap|change|set|make|put)\b.+\b(?:stop|stops|waypoint|route|destination|origin|start|end|trip|journey|way|via)\b"
    r"|\bvia\b|\b(?:go|going|pass|passing)\s+through\b|\b(?:stop|stops)\s+(?:at|in)\b",
    re.IGNORECASE,
)


def explicit_route_request(text: str) -> bool:
    """Did the user actually ask to plan or change a route? Searching for places never counts, so a model that reads a
    search as a route edit is overruled."""
    return bool(_ROUTE_PLAN_WORDS.search(text) or _ROUTE_EDIT_WORDS.search(text))


def _subjects(raw: str) -> list[str]:
    raw = _SUBJECT_CUT.sub("", raw.strip(" ?.!,"))
    raw = re.sub(r"^(?:living|staying|travel(?:l?ing)?|visiting|moving|going|holidaying)\s+(?:in|to|at)\s+", "", raw, flags=re.IGNORECASE)
    parts = [p.strip(" ?.!,") for p in re.split(r"\s+(?:and|vs\.?|versus|or|&)\s+|\s*,\s*", raw, flags=re.IGNORECASE)]
    return [p for p in parts if p and len(p.split()) <= 6][:3]


def _ask_request(text: str) -> dict[str, Any] | None:
    """The rule-based reading of an open question about named places. With the AI connected the model reads these
    itself (and any other wording); this keeps the common forms working, with HERE's verified places, when it is off."""
    for pattern in (_ASK_PROS_CONS, _ASK_COMPARE, _ASK_ABOUT):
        match = pattern.match(text) if pattern is not _ASK_PROS_CONS else pattern.search(text)
        if not match:
            continue
        raw = next((g for g in match.groups() if g), "")
        if _PRONOUN_SUBJECT.match(raw.strip()):
            return None  # a follow-up about what is already on screen
        subjects = _subjects(raw)
        if subjects and not any(is_me_place(x) or reference_in(x) for x in subjects):
            return {"intent": "ask", "subjects": subjects}
    return None


_GUIDE_PLACE_CUT = re.compile(r"\s+(?:in|during|for|with|around|if|when|because|so)\b.*$", re.IGNORECASE)
_GUIDE_NOT_A_GUIDE = re.compile(r"\b(?:plan|route|directions?|add|remove|stops?|via)\b|\bfrom\b.+\bto\b", re.IGNORECASE)
_GUIDE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("best_time", re.compile(
        r"\b(?:best|ideal|perfect|right|good)\s+(?:time|season|month|months|weather)\s+(?:of\s+(?:the\s+)?year\s+)?(?:to|for)\s+"
        r"(?:visit(?:ing)?|go(?:ing)?\s+to|travel(?:l?ing)?\s+to|see|explor(?:e|ing)|tour(?:ing)?)\s+(?P<place>[^?.!]+)", re.IGNORECASE)),
    ("best_time", re.compile(
        r"\bwhen\s+(?:is\s+(?:the\s+best\s+time\s+to|it\s+best\s+to)|should\s+i|do\s+i\s+go|to|can\s+i|is\s+it\s+good\s+to)\s+"
        r"(?:visit(?:ing)?|go(?:ing)?\s+to|travel(?:l?ing)?\s+to)\s+(?P<place>[^?.!]+)", re.IGNORECASE)),
    ("things_to_do", re.compile(
        r"\b(?:things\s+to\s+do|what\s+to\s+(?:do|see)|what\s+can\s+i\s+(?:do|see)|must[- ]see\s+(?:places\s+|sights\s+)?|sightseeing)\s+"
        r"(?:in|at|around|near)\s+(?P<place>[^?.!]+)", re.IGNORECASE)),
    ("overview", re.compile(r"\bis\s+(?P<place>[^?.!]+?)\s+worth\s+(?:a\s+|the\s+)?(?:visit(?:ing)?|going|trip|travel)", re.IGNORECASE)),
    ("overview", re.compile(r"\b(?:travel|trip|tourist|visitor|city)\s+guide\s+(?:to|for|of|on)\s+(?P<place>[^?.!]+)", re.IGNORECASE)),
)


def _destination_guide_request(text: str) -> dict[str, Any] | None:
    """Travel-advice questions about a named place ("best time to visit Ooty", "things to do in Goa").

    These get a guide: the AI writes the advice, HERE supplies the places. A message that is really about planning
    or editing a route is left to the trip rules.
    """
    if _GUIDE_NOT_A_GUIDE.search(text):
        return None
    for topic, pattern in _GUIDE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        place = _GUIDE_PLACE_CUT.sub("", match.group("place").strip(" ?.!,;:"))
        place = re.sub(r"^the\s+", "", place, flags=re.IGNORECASE).strip(" ?.!,;:")
        if not place or len(place.split()) > 5 or re.search(r"\b(?:me|my|here|there|this|that|it|them)\b", place, re.IGNORECASE):
            continue
        return {"intent": "destination_guide", "place": place, "topic": topic}
    return None


# ---------------------------------------------------------------------------------------------
# Trip planning: itineraries and destination ideas
# ---------------------------------------------------------------------------------------------
_MONTHS = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
           "november": 11, "december": 12, "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
           "oct": 10, "nov": 11, "dec": 12}
_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "a": 1, "an": 1}
_DAYS = re.compile(r"\b(?P<n>\d{1,2}|one|two|three|four|five|six|seven)[\s-]*(?:days?|nights?)\b", re.IGNORECASE)
_PARTY_WORD = re.compile(
    r"\b(?:wife|husband|spouse|partner|girlfriend|boyfriend|kids?|child(?:ren)?|baby|babies|infant|toddler|son|daughter|parents?|mother|father|"
    r"mom|dad|grand(?:parents?|mother|father|ma|pa)|family|friends?|couples?|group|colleagues|team|solo|elderly|seniors?)\b", re.IGNORECASE)
_BABY_AGE = re.compile(
    r"\b(?P<age>(?:\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven)[\s-]*(?:months?|yrs?|years?)(?:[\s-]*old)?)\s+(?P<who>baby|infant|toddler|girl|boy|daughter|son|kid|child)(?:\s+(?P<sex>girl|boy))?\b",
    re.IGNORECASE)
_ITINERARY_WORD = re.compile(r"\b(?:itinerary|itineraries|day[\s-]*wise|day[\s-]*by[\s-]*day|full\s+trip\s+plan|trip\s+plan|travel\s+plan)\b", re.IGNORECASE)
_PLAN_VERB = re.compile(r"\b(?:plan|planning|create|make|prepare|build|design|organi[sz]e|arrange|give|share|suggest|need|want|show|help)\b", re.IGNORECASE)
_TRIP_NOUN = r"(?:trip|itinerary|itineraries|tour|holiday|vacation|getaway|break|honeymoon|staycation|outing)"
_DEST_STOP = r"(?:\s+(?:from|with|for|and|on|in|during|starting|via|by|that|where|which|within|if|so|but|please)\b|[,.?!;]|$)"
_DEST_PATTERNS = (
    re.compile(rf"\b{_TRIP_NOUN}(?:\s+plan)?\s+(?:to|in|for|at|around|of|across)\s+(?P<d>[^,.?!;]+?){_DEST_STOP}", re.IGNORECASE),
    re.compile(rf"\b(?:plan|planning|create|make|prepare|build|design|organi[sz]e|arrange|give|share|suggest)\b(?:\s+me)?(?:\s+(?:a|an|my|our|the))?"
               rf"(?:\s+\d{{1,2}}[\s-]*(?:days?|nights?))?(?:\s+(?:fun|family|romantic|short|long|budget|weekend|quick|relaxing|perfect|good|nice|best))*"
               rf"\s+(?P<d>[a-z][a-z .'-]{{2,40}}?)\s+{_TRIP_NOUN}\b", re.IGNORECASE),
    re.compile(rf"\b\d{{1,2}}[\s-]*(?:days?|nights?)\s+(?:in|at|around|to)\s+(?P<d>[^,.?!;]+?){_DEST_STOP}", re.IGNORECASE),
    re.compile(rf"\b\d{{1,2}}[\s-]*(?:days?|nights?)\s+(?P<d>[a-z][a-z .'-]{{2,40}}?)\s+{_TRIP_NOUN}\b", re.IGNORECASE),
    re.compile(rf"\bitinerary\s+(?:for|of|to|in)\s+(?P<d>[^,.?!;]+?){_DEST_STOP}", re.IGNORECASE),
    re.compile(r"\b(?:visit|visiting|travel(?:l?ing)?\s+to|going\s+to|go\s+to)\s+(?P<d>[a-z][a-z .'-]{2,40}?)" + _DEST_STOP, re.IGNORECASE),
)
_ORIGIN = re.compile(r"\bfrom\s+(?P<o>[a-z][a-z .'-]{1,40}?)(?=\s+(?:for|with|to|and|on|in|by|during|via|please)\b|[,.?!;]|$)", re.IGNORECASE)
_NOT_A_PLACE = re.compile(
    r"^(?:a|an|the|my|our|this|that|it|here|there|us|me|trip|day|days|week|weekend|family|baby|wife|husband|kids?|friends?|couple|"
    r"honeymoon|holiday|vacation|plan|itinerary|full|complete|detailed|whole|entire|proper|good|nice|best|perfect|great|amazing|short|long|"
    r"quick|fun|romantic|relaxing|budget|solo|group|road|travel|tour|details?|day[\s-]*wise|"
    # How someone travels is never where they are going: "plan a EV trip Mumbai to Bhubaneswar" has no destination called EV.
    r"ev|evs|electric|car|cars|bike|bikes|motorbike|motorcycle|scooter|suv|vehicle|drive|driving|self[\s-]*drive|cab|taxi|train|bus|flight|"
    r"bicycle|cycle|cycling|ride|charging)$", re.IGNORECASE)
_BUDGET_WORDS = (
    (re.compile(r"\b(?:budget|cheap|affordable|low[\s-]*cost|economical|backpack(?:er|ing)?)\b", re.IGNORECASE), "budget"),
    (re.compile(r"\b(?:luxury|luxurious|premium|5[\s-]*star|five[\s-]*star|high[\s-]*end)\b", re.IGNORECASE), "luxury"),
    (re.compile(r"\b(?:mid[\s-]*range|moderate)\b", re.IGNORECASE), "mid"),
)
_INTEREST_WORDS = re.compile(
    r"\b(?:temples?|heritage|history|food|foodie|street\s+food|shopping|nature|wildlife|adventure|trekking|hiking|beaches?|nightlife|photography|"
    r"spiritual|relaxing|relax|romantic|culture|museums?|waterfalls?|coffee|tea|plantations?|safari|yoga|wellness|spa)\b", re.IGNORECASE)

# What people mean by "somewhere to go", as opposed to a kind of business to find. These go to the destination suggester.
_DESTINATION_KIND = re.compile(
    r"\b(?P<k>hill[\s-]*stations?|hill[\s-]*resorts?|beach(?:es)?(?!\s+(?:resorts?|hotels?|clubs?|shacks?|restaurants?|cafes?|bars?|houses?|villas?|stays?|homes?|road|side|view|front))|waterfalls?|wild[\s-]*life\s+sanctuar(?:y|ies)|national\s+parks?|safaris?|"
    r"trekking(?:\s+(?:spots?|places?|destinations?))?|treks?|camping(?:\s+(?:sites?|spots?|places?))?|weekend\s+(?:getaways?|trips?|destinations?)|"
    r"getaways?|day\s+trips?|short\s+trips?|road\s+trips?|honeymoon(?:\s+(?:destinations?|spots?|places?))?|heritage\s+(?:towns?|cities|places?)|"
    r"temple\s+(?:towns?|cities)|backwaters?|islands?|desert\s+(?:camps?|destinations?)?|adventure\s+(?:spots?|destinations?|places?)|"
    r"spiritual\s+(?:destinations?|places?)|romantic\s+(?:getaways?|places?|destinations?)|family\s+(?:destinations?|getaways?|holidays?)|"
    r"offbeat\s+(?:places?|destinations?)|nature\s+(?:spots?|getaways?|destinations?)|coffee\s+estates?|tea\s+estates?|plantation\s+stays?|"
    r"travel\s+destinations?|holiday\s+destinations?|tourist\s+destinations?|vacation\s+(?:spots?|destinations?))\b", re.IGNORECASE)
# Words that clearly ask for a list of options. A bare "good" or "what" is not enough: "is Goa good for a honeymoon?" and
# "what should I pack for a trek?" are questions about one place or a general question, not requests for ideas.
_IDEAS_ASK = re.compile(
    r"\b(?:suggest(?:ions?)?|recommend(?:ations?)?|show|find|list|give|share|any|some|best|top|popular|ideas?|options?|which|"
    r"(?:good|nice|great|interesting|beautiful|lovely|scenic)\s+(?:places?|spots?|destinations?|getaways?|towns?))\b", re.IGNORECASE)
_WHERE_TO_GO = re.compile(
    r"\b(?:where\s+(?:can|should|could|shall|do)\s+(?:i|we)\s+(?:go|travel|visit|head|drive)|places?\s+to\s+(?:go|visit|travel)\s+(?:for\s+(?:a|the)\s+(?:weekend|day|short)|this\s+weekend|"
    r"over\s+the\s+weekend)|(?:any|some)\s+(?:place|places|destination|destinations)\s+to\s+(?:go|visit))\b", re.IGNORECASE)
_NEAR_PLACE = re.compile(
    r"\b(?P<conn>near(?:by)?|around|close\s+to|from|within\s+[\d.]+\s*(?:km|kms|kilomet(?:er|re)s?|hours?|hrs?)\s+(?:of|from)|in|across|of)\s+(?P<p>[^,.?!;]+?)"
    r"(?=\s+(?:for|with|within|in\s+(?:" + "|".join(k for k in _MONTHS if len(k) > 3) + r")|during|that|which|where|and|on|under|by|this|next|please)\b|[,.?!;]|$)", re.IGNORECASE)
_WITHIN = re.compile(r"\bwithin\s+(?P<n>\d+(?:\.\d+)?)\s*(?P<u>km|kms|kilomet(?:er|re)s?|hours?|hrs?|h|minutes?|mins?)\b", re.IGNORECASE)


def _party_from(text: str) -> list[str]:
    """Who is travelling, in the user's words: ["wife", "8 month baby girl"]. A fixed vocabulary, nothing is guessed."""
    found: list[str] = []
    aged = list(_BABY_AGE.finditer(text))
    for m in aged:
        label = " ".join(x for x in (re.sub(r"\s+", " ", m.group("age").replace("-", " ")).replace(" old", ""), m.group("who"), m.group("sex")) if x)
        if label.lower() not in (f.lower() for f in found):
            found.append(label)
    covered = " ".join(m.group(0).lower() for m in aged)
    for m in _PARTY_WORD.finditer(text):
        word = m.group(0).lower()
        if word in covered or word in {"baby", "babies", "infant", "toddler"} and aged:
            continue
        if word not in (f.lower() for f in found):
            found.append(word)
    return found[:6]


def _days_from(text: str) -> int | None:
    m = _DAYS.search(text)
    if m:
        raw = m.group("n").lower()
        return int(raw) if raw.isdigit() else _NUMBER_WORDS.get(raw)
    if re.search(r"\b(?:long\s+weekend)\b", text, re.IGNORECASE):
        return 3
    if re.search(r"\b(?:weekend|week[\s-]*end)\b", text, re.IGNORECASE):
        return 2
    if re.search(r"\b(?:a|one)\s+week\b|\bweek[\s-]*long\b", text, re.IGNORECASE):
        return 7
    if re.search(r"\bday\s+(?:trip|out|tour)\b|\bone[\s-]*day\b", text, re.IGNORECASE):
        return 1
    return None


def _month_from(text: str) -> int | None:
    m = re.search(r"\b(?:in|during|for|around|by|this|next|visit(?:ing)?)\s+(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\b(?!\s*(?:\d{1,2}\b))", text, re.IGNORECASE)
    return _MONTHS.get(m.group(1).lower()) if m else None


def _budget_from(text: str) -> str | None:
    return next((label for rx, label in _BUDGET_WORDS if rx.search(text)), None)


def _interests_from(text: str) -> list[str]:
    seen: list[str] = []
    for m in _INTEREST_WORDS.finditer(text):
        word = re.sub(r"\s+", " ", m.group(0).lower())
        if word not in seen:
            seen.append(word)
    return seen[:5]


def _clean_destination(raw: str) -> str:
    value = _trim_place(raw)
    value = re.sub(r"^(?:a|an|my|our|the|some|one)\s+", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^(?:\d+[\s-]*(?:days?|nights?)\s+)", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+(?:trip|itinerary|tour|holiday|vacation|getaway)$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value).strip(" .,'-")
    words = value.split()
    if not value or len(words) > 5 or _NOT_A_PLACE.match(value) or all(_NOT_A_PLACE.match(w) for w in words):
        return ""
    return value


def _before_clause(value: str) -> str:
    """"Goa for friends" -> "Goa", "Jaipur in November" -> "Jaipur": the place, without the rest of the sentence."""
    return re.split(r"\s+(?:for|with|during|in|on|and\s+(?:my|our|the|a)|starting)\s+", value.strip(), maxsplit=1, flags=re.IGNORECASE)[0]


def has_itinerary_signals(text: str) -> bool:
    """Does the message give what a bare route cannot answer: a trip length, the word itinerary, or who is travelling?"""
    return bool(_days_from(text) or _ITINERARY_WORD.search(text) or _party_from(text))


def _itinerary_request(text: str, context: dict[str, Any]) -> dict[str, Any] | None:
    """A multi-day plan for one destination: "plan a 2 day trip to Mysuru from Bangalore with my wife and baby".

    A plain "plan a trip from A to B" is a route, not an itinerary. It becomes one only when the user also gives a trip
    length, says "itinerary", or says who is travelling: those are what a route alone cannot answer.
    """
    days, party = _days_from(text), _party_from(text)
    asks_itinerary = bool(_ITINERARY_WORD.search(text))
    if not (_PLAN_VERB.search(text) or asks_itinerary or days):
        return None
    # "A to B" is a journey. It stays a route unless the user wants an itinerary, or gave a trip length / travelling party and is
    # not asking about an EV (an EV trip between two places wants the route and its charging stops).
    if len(extract_places(text)) >= 2 and not asks_itinerary and (_EV_WORDS.search(text) or not (days or party)):
        return None
    has_trip_noun = bool(re.search(rf"\b{_TRIP_NOUN}\b", text, re.IGNORECASE))
    # "plan a trip to Goa" with no starting point, no length and nobody named still wants an itinerary rather than a route.
    bare_plan = bool(_PLAN_VERB.search(text) and has_trip_noun and not _ORIGIN.search(text) and not (context.get("stops") or []))
    if not (days or asks_itinerary or party or bare_plan):
        return None
    destination = ""
    for pattern in _DEST_PATTERNS:
        for m in pattern.finditer(text):
            candidate = _clean_destination(m.group("d"))
            if candidate:
                destination = candidate
                break
        if destination:
            break
    route_places = extract_places(text)
    if not destination and len(route_places) >= 2:
        # "a 2 day trip from Bangalore to Mysuru with my wife": the destination is the end of the journey, the start is where it begins.
        destination = _clean_destination(_before_clause(route_places[-1]))
    if not destination:
        return None
    # Distances ("a trip from A to B") with no length, itinerary word or party stay what they were: a route.
    origin = ""
    m = _ORIGIN.search(text)
    if m:
        origin = _clean_destination(m.group("o"))
    elif len(route_places) >= 2:
        origin = _clean_destination(_before_clause(route_places[0]))
    if origin and origin.lower() == destination.lower():
        origin = ""
    return {
        "intent": "trip_itinerary", "destination": destination, "origin": origin or None,
        "days": days or 2, "party": party, "interests": _interests_from(text), "month": _month_from(text), "budget": _budget_from(text),
    }


def _destination_ideas_request(text: str, context: dict[str, Any]) -> dict[str, Any] | None:
    """"Suggest hill stations near Bangalore", "beaches near Mangalore", "weekend getaway near Pune for a couple".

    These ask where to GO, so they must not become a text search for the word "station" around a city centre.
    """
    kind_match = _DESTINATION_KIND.search(text)
    where_to_go = _WHERE_TO_GO.search(text)
    if not (kind_match or where_to_go):
        return None
    # "restaurants near the beach in Goa" asks for a kind of business; the beach is only where. Not a request for ideas.
    business = _find_category(text)
    if business and business[0] != "tourist_attraction":
        return None
    # A specific place named in the question ("beaches in Goa: which is best for kids?") still wants ideas; but a trip plan
    # ("plan a 2 day trip to Goa") or a question about one place ("best time to visit Ooty") is handled elsewhere.
    if _ITINERARY_WORD.search(text) or (_DAYS.search(text) and re.search(rf"\b{_TRIP_NOUN}\b", text, re.IGNORECASE) and not kind_match):
        return None
    starts_with_kind = bool(kind_match and not text[: kind_match.start()].strip(" ,.-"))
    if not (_IDEAS_ASK.search(text) or where_to_go or starts_with_kind):
        return None
    if re.search(r"\b(?:pack|packing|carry|wear|carrying|bring|prepare|train|gear|equipment|insurance|permit|how\s+(?:to|do|long|much|hard|difficult)|is\s+it\s+(?:safe|worth))\b", text, re.IGNORECASE):
        return None
    origin, region = None, None
    within = _WITHIN.search(text)
    remainder = text[:kind_match.start()] + " " + text[kind_match.end():] if kind_match else text
    for m in _NEAR_PLACE.finditer(remainder):
        place = _clean_destination(m.group("p"))
        if re.fullmatch(r"(?:me|my\s+(?:current\s+)?(?:location|place|position)|here)", m.group("p").strip(), re.IGNORECASE):
            origin = "my location"
            break
        if not place or _DESTINATION_KIND.fullmatch(place):
            continue
        conn = m.group("conn").lower()
        if conn in ("in", "across", "of"):
            region = region or place
        else:
            origin = origin or place
        if origin:
            break
    # With no place to anchor the ideas, only an explicit request for a list counts ("suggest some hill stations").
    if not (origin or region) and not (kind_match and re.search(r"\b(?:suggest(?:ions?)?|recommend(?:ations?)?|list|ideas?|best|top|popular|options?)\b", text, re.IGNORECASE)) and not where_to_go:
        return None
    within_km = within_minutes = None
    if within:
        n, unit = float(within.group("n")), within.group("u").lower()
        if unit.startswith(("km", "kilo")):
            within_km = n
        elif unit.startswith(("h",)):
            within_minutes = n * 60
        else:
            within_minutes = n
    kind = re.sub(r"\s+", " ", kind_match.group("k").lower().replace("-", " ")) if kind_match else None
    return {
        "intent": "suggest_destinations", "origin": origin, "region": region, "kind": kind,
        "within_km": within_km, "within_minutes": within_minutes, "days": _days_from(text), "party": _party_from(text),
        "interests": _interests_from(text), "month": _month_from(text),
    }


_DIRECTIONS = re.compile(
    r"^(?:(?:please\s+)?(?:can|could|would)\s+you\s+)?(?:(?:please\s+)?(?:give|show|get|find|share|send|tell|ask)\s+(?:me\s+)?(?:for\s+)?(?:a\s+|the\s+)?"
    r"(?:route|routes|directions?|way|path|navigation)|(?:route|directions?|navigate|navigation)|take\s+me|"
    r"how\s+(?:do|can|should|would)\s+(?:i|we)\s+(?:get|go|reach|drive))\s+(?:to|for|towards?)\s+(?P<t>.+?)"
    r"(?:\s+from\s+(?P<f>.+?))?[.?!\s]*$", re.IGNORECASE)
_THESE = re.compile(r"^(?:these|those|them|all\s+of\s+(?:them|these|those)|(?:these|those)\s+[a-z ]{3,30})$", re.IGNORECASE)


def _directions_request(text: str, context: dict[str, Any]) -> dict[str, Any] | None:
    """"Route to Hotel Hanumanthu", "directions to the second one", "ask me for a route to these restaurants".

    A trip is "from A to B"; this is "to B" with the start either said, shared (the location button) or still to be asked.
    The target can be a place on screen: by name, number or position ("the first one").
    """
    m = _DIRECTIONS.match(text.strip())
    if not m:
        return None
    target, origin = m.group("t").strip(" .,!?"), (m.group("f") or "").strip(" .,!?") or None
    pois = [str(p.get("name") or "") for p in (context.get("pois") or []) if isinstance(p, dict)]
    if _THESE.match(target):
        return {"intent": "directions", "choose": True, "origin": origin}
    index = pick_index(target, pois) if pois else None
    if index is not None:
        return {"intent": "directions", "index": index, "destination": pois[index] if 0 <= index < len(pois) else None, "origin": origin}
    if _MIDPOINT_WORDS.search(target) or is_me_place(target):
        return None
    cleaned = _clean_destination(target) or target
    return {"intent": "directions", "destination": cleaned, "origin": origin} if cleaned else None


def _parse_rules(message: str, context: dict[str, Any]) -> dict[str, Any]:
    text = re.sub(r"\s+", " ", message.strip())
    lower = text.lower()
    context = context or {}

    directions = _directions_request(text, context)
    if directions:
        return directions

    # Trip planning and destination ideas come first: "plan a 2 day trip to Mysuru with my baby" and "hill stations near
    # Bangalore" are not route requests or text searches, and the checks below would otherwise take them for one.
    itinerary = _itinerary_request(text, context)
    if itinerary:
        return itinerary
    ideas = _destination_ideas_request(text, context)
    if ideas:
        return ideas

    # "near me": never geocode the word "me". With a category it is a search around the location the user shared
    # (the handler asks them to share it if they haven't); without one there is nothing to search for yet.
    me_phrase = re.search(r"\b(?:near|around|close to|next to|nearby to)\s+(?:me|my\s+(?:current\s+)?(?:location|position|place))\b", lower)
    if (me_phrase or re.search(r"\bnear\s*by\s*$|\bnearby\s*[?.!]*$", lower)) and not _route_edit(text, context):
        found_here = _find_category(text) if me_phrase else None
        if found_here:
            radius = _radius_in(text)
            return {"intent": "find_pois", "category": found_here[0], "scope": "me", "index": None, "query": found_here[1],
                    "radius_km": radius if radius is not None else 5.0, "radius_explicit": radius is not None, "limit": _extract_count(text)}
        return {"intent": "clarify", "question": "I can't see your location. Tell me a city or address and I'll search there, for example “restaurants in Pune”."}

    if re.match(r"^(?:help|what can you do|what do you do|how do i use|capabilities|hi|hello|hey)\b[\s!.?]*$", lower):
        return {"intent": "help"}

    guide = _destination_guide_request(text)
    if guide:
        return guide

    named = _where_is(text)
    if named:
        return named

    nearest = _nearest_search(text, context)
    if nearest:
        return nearest

    asking = _ask_request(text)
    if asking:
        return asking

    compound = _compound_trip_request(text, context)
    if compound:
        return compound

    if any(word in lower for word in ["what is my trip", "show context", "what do you know", "trip context", "my trip so far"]):
        return {"intent": "context"}

    detail = _ev_detail(text, context)
    if detail and detail["intent"] == "show_charging_stop":
        return detail

    edit = _route_edit(text, context)
    if edit:
        return edit

    midpoint = _midpoint_request(text, context)
    if midpoint:
        return midpoint

    trip = _trip_request(text, context)
    if trip:
        return trip

    if detail:
        return detail

    if context.get("pois") and re.search(r"\b(?:show|share|list|display|put|plot)\b.*\b(?:here|map|places|them|results)\b", lower):
        return {"intent": "show_pois"}

    options_or_ev = _vehicle_or_ev(text, context)
    if options_or_ev:
        return options_or_ev

    # Searches come before distance questions: a search that merely *mentions* distance or time in a
    # presentation clause ("...and show their name & distance") is still a search.
    anchor = _anchor_search(text, context)
    if anchor:
        return anchor

    along = _along_route_search(text, context)
    if along:
        return along

    near_ref = _near_reference_search(text, context)
    if near_ref:
        return near_ref

    generic = _generic_location_search(text, context)
    if generic:
        return generic

    info = _route_info(text, context)
    if info:
        return info

    nearby_generic = re.search(r"(?:what(?:'s| is)|show|find)\s+(?:is\s+)?(?:nearby|near by)\s+(.+)$", text, flags=re.IGNORECASE)
    if nearby_generic:
        return {"intent": "find_pois_near_location", "category": "place", "query": "places", "place": nearby_generic.group(1).strip().rstrip("."), "radius_km": 5.0, "radius_explicit": False, "limit": 40}

    found = _find_category(text)
    if found and _SEARCH_VERBS.search(text):
        if context.get("stops") and len(context.get("stops") or []) >= 2:
            return {"intent": "find_pois", "category": found[0], "scope": "route", "index": None, "radius_km": _radius_in(text) or 3.0, "limit": _extract_count(text), "query": found[1]}
        return {"intent": "clarify", "question": "Where should I search? Tell me a city or address, for example “restaurants in Pune”."}

    if re.search(r"\bnear\s+me\b|\baround\s+me\b|\bmy\s+(?:current\s+)?location\b", lower):
        return {"intent": "clarify", "question": "I can't see your location. Tell me a city or address and I'll search there."}

    where = re.match(r"^(?:where\s+is|where's|locate|show\s+me|find|take\s+me\s+to|map\s+of|show)\s+(?P<p>.+?)(?:\s+on\s+(?:the\s+)?map)?[?.! ]*$", text, flags=re.IGNORECASE)
    if where and not _find_category(where.group("p")):
        return {"intent": "geocode", "place": where.group("p").strip()}

    if re.search(r"\b(?:which|what)\b.*\b(?:closest|nearest|best|cheapest|highest|top|rated)\b|\b(?:tell me more|more (?:info|details))\b|\b(?:compare)\b", lower) and (context.get("pois") or context.get("stops")):
        return {"intent": "follow_up_question"}

    if _QUESTION_START.match(text) or text.endswith("?"):
        return {"intent": "follow_up_question" if (context.get("pois") or context.get("stops")) else "general_question"}

    if len(text.split()) <= 5:
        return {"intent": "geocode", "place": text.strip(" .?!")}
    return {"intent": "general_question"}


_SEARCH_INTENTS = frozenset({"find_pois_near_location", "find_pois", "find_pois_in_area", "find_pois_near_anchor"})


def parse(message: str, context: dict[str, Any]) -> dict[str, Any]:
    """The rule-based reading of ``message``: one intent dict (not yet validated)."""
    action = _parse_rules(message, context)
    wishes = extract_qualifiers(message)
    if wishes:
        if action.get("intent") in _SEARCH_INTENTS:
            action["qualifiers"] = wishes
        elif action.get("intent") == "compound_trip_request":
            for request in action.get("poi_requests", []):
                request["qualifiers"] = wishes
    return action


def pick_index(message: str, names: list[str]) -> int | None:
    """Which listed result a short reply picks: "2", "the second one", "option 3", "last", or a name. 0-based; -1 = last."""
    text = re.sub(r"\s+", " ", message.strip().lower()).strip(" .!?,")
    if not text or not names:
        return None
    match = re.fullmatch(r"(?:the\s+)?(?:number\s+|no\.?\s*|#\s*|option\s+|result\s+|choice\s+)?(\d{1,3})(?:st|nd|rd|th)?(?:\s+(?:one|hotel|result|option|place|choice))?", text)
    if match:
        index = int(match.group(1)) - 1
        return index if 0 <= index < len(names) else None
    match = re.fullmatch(rf"(?:the\s+)?({_ORD})(?:\s+(?:one|hotel|result|option|place|choice))?", text)
    if match:
        index = _index_from(match.group(1))
        if index == -1:
            return -1
        return index if index is not None and 0 <= index < len(names) else None
    cleaned = re.sub(r"^(?:the\s+|let'?s\s+(?:go\s+with\s+)?|go\s+with\s+|use\s+|pick\s+|choose\s+)+", "", text).strip()
    hits = [i for i, name in enumerate(names) if cleaned and (cleaned == name.lower() or (len(cleaned) >= 4 and cleaned in name.lower()))]
    return hits[0] if len(hits) == 1 else None
