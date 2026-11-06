"""Offline smoke test: one realistic conversation through the whole orchestrator.

Providers are stubbed and AI is disabled, so it needs no network and no credentials. Run it as a script
(``PYTHONPATH=. python tests/smoke_orchestrator.py``); the same flow is also exercised by pytest in
``test_conversation_flow.py``.
"""
import asyncio

import services.orchestrator as orchestrator
from services.llm import NullProvider, set_provider
from services.trip_context import TripContextStore

COORDS = {
    "Mumbai": (19.0760, 72.8777, "locality"),
    "Hyderabad": (17.3850, 78.4867, "locality"),
    "Bhubaneswar": (20.2961, 85.8245, "locality"),
}


async def fake_resolve_candidates(query):
    lat, lon, kind = COORDS.get(query, (12.0, 77.0, "locality"))
    return [{"display_name": f"{query}, India", "latitude": lat, "longitude": lon, "result_type": kind}], []


async def fake_route(stops, ev=None, transport_mode="car"):
    legs, total_km, total_min = [], 0.0, 0.0
    for i in range(len(stops) - 1):
        km = 500.0 + 40 * i
        legs.append({"from_stop_index": i, "to_stop_index": i + 1, "distance_km": km, "duration_minutes": km * 1.2})
        total_km += km
        total_min += km * 1.2
    positions, so_far, minutes = [], 0.0, 0.0
    for i, stop in enumerate(stops):
        if i:
            so_far += legs[i - 1]["distance_km"]
            minutes += legs[i - 1]["duration_minutes"]
        positions.append({"stop_index": i, "name": stop["name"], "latitude": stop["latitude"], "longitude": stop["longitude"],
                          "distance_from_start_km": so_far, "duration_from_start_minutes": minutes})
    return {
        "distance_km": total_km, "duration_minutes": total_min, "transport_mode": transport_mode,
        "geometry": {"type": "LineString", "coordinates": [[s["longitude"], s["latitude"]] for s in stops]},
        "legs": legs, "stop_positions": positions, "waypoint_count": len(stops),
    }


async def fake_nearby(points, category, radius_km, limit=80, search_query=None):
    return {"pois": [{"name": "Test Place", "category": category, "latitude": points[0]["latitude"] + 0.01,
                      "longitude": points[0]["longitude"], "distance_from_reference_km": 1.2}],
            "source_status": "verified", "source": "HERE Search API", "source_error": None}


async def fake_route_pois(route_geometry, category, corridor_km, limit=60, search_query=None):
    return {"pois": [{"name": "Route Pump", "category": category, "latitude": 19.5, "longitude": 75.0, "distance_from_route_km": 0.8}],
            "source_status": "verified", "source": "HERE Search API", "source_error": None, "coverage": {"complete": True}}


async def fake_weather(latitude, longitude):
    # Mirrors the "available" shape of services.weather.get_current_weather so no test contacts Open-Meteo.
    return {"available": True, "provider": "Open-Meteo", "condition": "Clear sky", "weather_code": 0, "temperature_c": 25.0,
            "feels_like_c": 26.0, "humidity_percent": 50, "precipitation_mm": 0.0, "rain_mm": 0.0, "showers_mm": 0.0,
            "snowfall_cm": 0.0, "cloud_cover_percent": 10, "wind_kmh": 8.0, "wind_gust_kmh": 14.0,
            "observed_at": "2026-01-01T12:00", "timezone": "Asia/Kolkata", "latitude": latitude, "longitude": longitude}


def fake_chargers(route, corridor):
    return [{"id": "c1", "name": "Charge 1", "latitude": 19.9, "longitude": 76.5, "chargerType": "50 kW", "route_position_percent": 30.0},
            {"id": "c2", "name": "Charge 2", "latitude": 18.0, "longitude": 80.0, "chargerType": "50 kW", "route_position_percent": 70.0}]


async def main():
    set_provider(NullProvider("smoke test"))
    orchestrator.resolve_place_candidates = fake_resolve_candidates
    orchestrator.calculate_route = fake_route
    orchestrator.find_nearby_pois_with_source = fake_nearby
    orchestrator.find_route_pois_with_source = fake_route_pois
    orchestrator.get_current_weather = fake_weather
    orchestrator._route_chargers = fake_chargers

    context = TripContextStore().create()

    context, result, _ = await orchestrator.handle_message(context, "Plan a trip from Mumbai to Bhubaneswar")
    assert context.trip_names() == ["Mumbai", "Bhubaneswar"]
    assert result["route"]["distance_km"] == 500.0
    assert not context.ev_mode and context.ev_plan is None and context.chargers == [], "a general trip must not touch the EV planner"

    context, result, _ = await orchestrator.handle_message(context, "add a stop at Hyderabad")
    assert context.trip_names() == ["Mumbai", "Hyderabad", "Bhubaneswar"], context.trip_names()
    assert result["route"]["distance_km"] == 1040.0 and len(result["route"]["legs"]) == 2
    assert not context.ev_mode

    context, result, _ = await orchestrator.handle_message(context, "find hotels near Hyderabad")
    assert context.trip_names() == ["Mumbai", "Hyderabad", "Bhubaneswar"], "a place search must not change the route"
    assert result["action"] == "find_pois_near_location" and context.pois[0]["category"] == "hotel"

    context, result, reply = await orchestrator.handle_message(context, "how far is the second stop")
    assert result["action"] == "route_info" and result["distance_km"] == 500.0, result

    context, result, _ = await orchestrator.handle_message(context, "find petrol pumps along my route")
    assert result["scope"] == "route" and context.pois[0]["category"] == "fuel"

    context, result, _ = await orchestrator.handle_message(context, "My vehicle is Tata Nexon EV with 400km range with 50 kWh battery pack currently having 90% charge")
    assert context.ev_mode and context.vehicle.max_range_km == 400 and context.ev_status == "calculated", context.ev_status

    context, result, _ = await orchestrator.handle_message(context, "What if I start with 30%")
    assert context.vehicle.current_battery_percent == 30 and context.ev_plan is not None

    print("HerePilot orchestrator smoke test: PASS")


if __name__ == "__main__":
    asyncio.run(main())
