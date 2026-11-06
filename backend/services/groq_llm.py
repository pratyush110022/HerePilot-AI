"""HISTORICAL. NOT USED BY THE APPLICATION.

This was the Groq LLM integration. The active assistant now uses the provider-neutral ``services.llm`` package
(AWS Bedrock). Nothing imports this module, ``groq`` is no longer a requirement, and ``GROQ_*`` settings are
ignored. The file is kept only as a reference; it may be removed in a later cleanup.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from dotenv import load_dotenv

load_dotenv()

MODEL_NAME = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

logger = logging.getLogger(__name__)


def _client():
    if not GROQ_API_KEY:
        return None
    try:
        import httpx
        from groq import Groq
        verify = os.getenv("GROQ_VERIFY_SSL", "true").lower() not in {"0", "false", "no"}
        http_client = httpx.Client(verify=verify, timeout=60.0)
        return Groq(api_key=GROQ_API_KEY, http_client=http_client)
    except Exception as exc:
        logger.warning("event=groq_client_init_failed error=%s", type(exc).__name__)
        return None


def extract_json(text: str) -> dict[str, Any]:
    if not text:
        raise ValueError("LLM returned an empty response.")
    cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        value = json.loads(cleaned)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if not match:
        raise ValueError("LLM did not return a JSON object.")
    value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("LLM JSON is not an object.")
    return value


def parse_intent(user_message: str, context: dict[str, Any]) -> dict[str, Any] | None:
    client = _client()
    if client is None:
        return None

    system_prompt = """
You are GeoPilot AI's structured trip-intent parser.
Return ONLY one JSON object. Never invent coordinates or facts.
The current trip context is authoritative for already-known values.
Choose exactly one intent:

1) plan_trip: start a new trip or replace the trip. places is an ordered list.
2) modify_trip: add_stop, remove_stop, set_origin, set_destination. Use place/index.
3) update_vehicle: change only fields explicitly supplied: make, model, max_range_km, battery_capacity_kwh, current_battery_percent.
4) update_settings: change only explicitly supplied reserve_percent, target_charge_percent, corridor_km.
5) find_pois: category (restaurant, cafe, hotel, hospital, pharmacy, fuel, supermarket, convenience, tourist_attraction, parking, pub_bar, nightlife, shopping_mall, bank, atm, car_repair, car_wash, ev_charging), scope (route, stop, charging_stop), index (0-based when relevant), radius_km, limit, and optional natural-language query.
5a) find_pois_near_location: category plus a natural-language query such as "5 star hotel" or "pub bar", place, radius_km and limit. Use this for any category explicitly requested near/in/around a named location, including categories not in the fixed list.
6) simulate: change only one EV field for a what-if: battery_percent/reserve_percent/target_charge_percent, then rerun the EV plan.
7) route: answer a route request using the current trip or supplied places.
8) geocode: locate a place only.
9) context: show current trip/vehicle context without changing it.
10) compound_trip_request: when one message explicitly asks to plan a trip and also asks for multiple POI/EV sub-requests. Return places, ev_required, vehicle_updates, settings, recommend_charger, and poi_requests.

For an EV plan request, set "ev_required": true. For an ordinary driving route, false.
For first-time EV planning with missing range/battery, keep those fields null; the server will ask the user for the missing vehicle information.
Vehicle values are user-declared; do not replace them with manufacturer specifications.
"""
    compact_context = {
        "stops": context.get("stops", []),
        "vehicle": context.get("vehicle"),
        "reserve_percent": context.get("reserve_percent"),
        "target_charge_percent": context.get("target_charge_percent"),
        "corridor_km": context.get("corridor_km"),
        "has_route": bool(context.get("route")),
    }

    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": f"CURRENT CONTEXT:\n{json.dumps(compact_context, ensure_ascii=False)}\n\nUSER:\n{user_message}",
            },
        ],
        max_tokens=450,
    )
    return extract_json(response.choices[0].message.content or "")


def final_answer(user_message: str, context: dict[str, Any], result: dict[str, Any]) -> str | None:
    client = _client()
    if client is None:
        return None
    compact = {
        "context": {
            "stops": context.get("stops", []),
            "vehicle": context.get("vehicle"),
            "reserve_percent": context.get("reserve_percent"),
            "target_charge_percent": context.get("target_charge_percent"),
            "corridor_km": context.get("corridor_km"),
        },
        "result": result,
    }
    system_prompt = """
You are GeoPilot AI. Explain the verified result in a concise, conversational way.
Use only supplied backend values. Never invent geography, distances, charger availability, opening hours, battery math or specifications.
When the user asks a follow-up, clearly state what changed and what was recalculated.
When the server needs vehicle information, ask only for the missing fields.
For general POIs, mention HERE Search API when relevant. The HERE Places query may be natural-language; do not narrow a verified result to the fixed category label if the query contains a modifier such as "5 star", "best", "family", "beachside", "pub", "bar", or "nightlife".
EV trip charging candidates are first-party GeoPilot/Tata charging data; independent charger discovery may also include HERE EV Charging Search.
Keep trip waypoints (for example Goa) separate from EV charging stops unless the verified result explicitly identifies the waypoint as a charging stop.
For a compound trip request, answer all verified sub-requests in the same response instead of asking the user to repeat information.
If estimated charging time or power is supplied by the backend, clearly call it an estimate/reference value rather than live charger availability.
When Open-Meteo weather data is supplied, include the current conditions near the destination and/or selected charging station when relevant. Clearly label these as current conditions and do not present them as arrival-time forecasts. If weather is unavailable, say so; never fabricate weather values. Weather is advisory and must not silently modify the vehicle's declared range or battery calculations.
For a numbered charging-stop detail request, use the exact selected charging_stop_number and station in the supplied result. Do not substitute the first charging stop when a later stop was requested.
If a POI result has source_status=unavailable or fallback_available=true, do not invent live listings, coordinates, distances, availability, prices, opening hours, or map results. Instead, explicitly say that HERE Search is temporarily unavailable and provide useful GENERAL, UNVERIFIED guidance from your language-model knowledge when appropriate. Label it as unverified and keep it separate from verified map data.
When the user asks to show/share/map previously found POIs, use the supplied result/context and do not claim new live results unless they are present in the backend result.
"""
    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"USER:\n{user_message}\n\nVERIFIED:\n{json.dumps(compact, ensure_ascii=False, separators=(',', ':'))}"},
        ],
        max_tokens=600,
    )
    return response.choices[0].message.content or None
