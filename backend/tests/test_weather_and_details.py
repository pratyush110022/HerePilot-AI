from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from services.heuristic_intent import parse
from services.trip_context import PlaceContext, TripContext, VehicleContext
from services.weather import get_current_weather


def test_numbered_charging_stop_detail_intent_is_not_recommend_charger():
    context = {"stops": [{"name": "Bengaluru"}, {"name": "Goa"}, {"name": "Mumbai"}], "pois": []}
    action = parse("Show me detailed information about charging stop 2", context)
    assert action == {"intent": "show_charging_stop", "index": 1}


def test_ordinal_charging_stop_details():
    action = parse("Show details for the first charging stop", {"stops": [], "pois": []})
    assert action == {"intent": "show_charging_stop", "index": 0}


def test_tourist_places_defaults_to_flexible_radius_but_explicit_radius_is_preserved():
    context = {"stops": [], "pois": []}
    default_action = parse("Find 10 tourist places near Goa and show their names", context)
    explicit_action = parse("Find 10 tourist places near Goa within 5 km", context)
    assert default_action["radius_explicit"] is False
    assert explicit_action["radius_explicit"] is True
    assert explicit_action["radius_km"] == 5.0


@pytest.mark.asyncio
async def test_weather_maps_open_meteo_fields_to_ui_friendly_values():
    payload = {
        "timezone": "Asia/Kolkata",
        "latitude": 15.5,
        "longitude": 73.8,
        "current": {
            "time": "2026-10-14T10:00",
            "temperature_2m": 31.2,
            "relative_humidity_2m": 72,
            "apparent_temperature": 35.0,
            "precipitation": 0.2,
            "rain": 0.2,
            "showers": 0.0,
            "snowfall": 0.0,
            "weather_code": 61,
            "cloud_cover": 65,
            "wind_speed_10m": 12.5,
            "wind_gusts_10m": 18.0,
        },
    }
    mock_response = SimpleNamespace(status_code=200, json=lambda: payload)
    with patch("services.weather.request", new=AsyncMock(return_value=mock_response)):
        result = await get_current_weather(15.5, 73.8)
    assert result["available"] is True
    assert result["condition"] == "Rain"
    assert result["temperature_c"] == 31.2
    assert result["wind_kmh"] == 12.5
    assert result["provider"] == "Open-Meteo"


@pytest.mark.asyncio
async def test_weather_failure_is_non_fatal_and_marked_unavailable():
    with patch("services.weather.request", new=AsyncMock(side_effect=TimeoutError())):
        result = await get_current_weather(15.5, 73.8)
    assert result["available"] is False
    assert result["provider"] == "Open-Meteo"


@pytest.mark.asyncio
async def test_charging_stop_two_details_returns_second_station_and_weather():
    context = TripContext(
        session_id="test-session",
        stops=[
            PlaceContext("Bengaluru", "Bengaluru, India", 12.97, 77.59, "origin"),
            PlaceContext("Mumbai", "Mumbai, India", 19.07, 72.87, "destination"),
        ],
        vehicle=VehicleContext(make="Tata", model="Nexon EV", max_range_km=400, battery_capacity_kwh=50, current_battery_percent=100),
    )
    context.ev_plan = {
        "trip_possible": True,
        "charging_stops": [
            {"charger": {"id": "charger-1", "name": "First Station", "latitude": 15.0, "longitude": 74.0}, "battery_arrival_percent": 40, "battery_departure_percent": 80},
            {"charger": {"id": "charger-2", "name": "Second Station", "latitude": 17.0, "longitude": 73.0, "distance_from_route_km": 0.07, "route_position_percent": 29.0}, "battery_arrival_percent": 16.64, "battery_departure_percent": 80, "energy_to_target_kwh": 31.7, "estimated_charging_time_minutes": 52},
        ],
    }
    context.weather = {
        "provider": "Open-Meteo",
        "charging_stops": {"charger-2": {"available": True, "condition": "Partly cloudy", "temperature_c": 29.0, "wind_kmh": 6.0}},
    }
    from services.orchestrator import handle_message

    updated_context, result, reply = await handle_message(context, "Show me detailed information about charging stop 2")
    assert result["action"] == "show_charging_stop"
    assert result["charging_stop_number"] == 2
    assert result["charger"]["name"] == "Second Station"
    assert result["weather_at_charger"]["temperature_c"] == 29.0
    assert "Second Station" in reply


def test_tourist_search_parses_location_query_and_requested_count_separately():
    action = parse(
        "Find 10 tourist places near Goa and show their name & distance",
        {"stops": [], "pois": []},
    )
    assert action["intent"] == "find_pois_near_location"
    assert action["place"] == "Goa"
    assert action["query"] == "tourist places"
    assert action["limit"] == 10
    assert action["radius_explicit"] is False


@pytest.mark.asyncio
async def test_tourist_place_results_keep_reference_location_and_flexible_default_radius(monkeypatch):
    import services.orchestrator as orchestrator

    from services.trip_context import TripContext

    async def fake_resolve_place(query: str, role: str = "stop"):
        assert query == "Goa"
        return PlaceContext("Goa", "Goa, India", 15.5, 73.8, role)

    async def fake_find_nearby_pois_with_source(points, category, radius_km, limit=80, search_query=None):
        assert points == [{"latitude": 15.5, "longitude": 73.8}]
        assert category == "tourist_attraction"
        assert radius_km == 15.0
        assert limit == 10
        assert search_query == "tourist places"
        return {
            "pois": [{"name": "Example Attraction", "distance_from_reference_km": 2.4}],
            "source_status": "verified",
            "source": "HERE Search API",
            "source_error": None,
        }

    monkeypatch.setattr(orchestrator, "_resolve_place", fake_resolve_place)
    monkeypatch.setattr(orchestrator, "find_nearby_pois_with_source", fake_find_nearby_pois_with_source)

    _, result, reply = await orchestrator.handle_message(
        TripContext(session_id="tourist-search-test"),
        "Find 10 tourist places near Goa and show their name & distance",
    )

    assert result["location_query"] == "Goa"
    assert result["query"] == "Goa"
    assert result["search_query"] == "tourist places"
    assert result["radius_km"] == 15.0
    assert result["limit"] == 10
    # Name and distance are in the results card; the reply no longer repeats them as a numbered list.
    assert result["pois"][0]["name"] == "Example Attraction" and result["pois"][0].get("distance_from_reference_km") is not None
    assert "Example Attraction" not in reply and "Found" in reply


@pytest.mark.asyncio
async def test_unavailable_tourist_search_never_asks_llm_to_invent_places(monkeypatch, fake_llm):
    import services.orchestrator as orchestrator

    async def fake_resolve_place(query: str, role: str = "stop"):
        return PlaceContext("Goa", "Goa, India", 15.5, 73.8, role)

    async def unavailable_search(*args, **kwargs):
        return {
            "pois": [],
            "source_status": "unavailable",
            "source": "HERE Search API + OpenStreetMap / Overpass",
            "source_error": "provider_unavailable",
        }

    def responder(system: str, user: str) -> str:
        if "intent parser" in system:
            return "{}"  # model has nothing useful to say about intent
        raise AssertionError("The model must not phrase or invent place results when providers are unavailable")

    provider = fake_llm(responder)
    monkeypatch.setattr(orchestrator, "_resolve_place", fake_resolve_place)
    monkeypatch.setattr(orchestrator, "find_nearby_pois_with_source", unavailable_search)

    _, result, reply = await orchestrator.handle_message(
        TripContext(session_id="tourist-provider-outage"),
        "Find 10 tourist places near Goa and show their names, distance from Goa, and locations on the map",
    )

    assert result["source_status"] == "unavailable"
    assert result["pois"] == []
    assert "haven't made up any results" in reply
    assert "approx" not in reply.lower()
    assert all("intent parser" in call["system"] for call in provider.calls)
