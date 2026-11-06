from __future__ import annotations

import pytest

from services import here_places


def _here_item(item_id: str, name: str, latitude: float, longitude: float) -> dict:
    return {
        "id": item_id,
        "title": name,
        "position": {"lat": latitude, "lng": longitude},
        "address": {"label": f"{name}, Goa", "city": "Panaji", "state": "Goa", "countryName": "India"},
        "categories": [{"name": "Tourist attraction"}],
    }


@pytest.mark.asyncio
async def test_tourist_query_fallback_fills_results_and_filters_to_radius(monkeypatch):
    queries: list[tuple[str, bool]] = []

    async def fake_discover(lat, lon, query, limit=50, restrict_to_india=True, radius_km=None, rich=False):
        queries.append((query, restrict_to_india))
        if query == "tourist places":
            return [
                _here_item("beach-1", "Example Beach", 15.51, 73.80),
                # About 22 km from the reference, outside the requested 15 km.
                _here_item("far-1", "Far Away Site", 15.70, 73.80),
            ]
        if query in {"tourist attractions", "places to visit"}:
            return [
                # Duplicate HERE ID should not create a duplicate result.
                _here_item("beach-1", "Example Beach", 15.51, 73.80),
                _here_item("fort-1", "Example Fort", 15.52, 73.80),
            ]
        return []

    monkeypatch.setattr(here_places, "_discover_at", fake_discover)
    result = await here_places.find_nearby_places(
        [{"latitude": 15.5, "longitude": 73.8}],
        "tourist_attraction",
        radius_km=15,
        limit=2,
        search_query="tourist places",
    )

    assert [item["name"] for item in result] == ["Example Beach", "Example Fort"]
    assert all(item["distance_from_reference_km"] <= 15 for item in result)
    assert queries[:2] == [("tourist places", True), ("tourist attractions", True)]


@pytest.mark.asyncio
async def test_search_forwards_radius_as_a_hard_filter_and_drops_results_outside_it(monkeypatch):
    seen_radius: list[float | None] = []

    async def fake_discover(lat, lon, query, limit=50, restrict_to_india=True, radius_km=None, rich=False):
        seen_radius.append(radius_km)
        return [
            _here_item("near-1", "Near Museum", 15.51, 73.80),   # ~1 km
            _here_item("far-1", "Far Museum", 15.70, 73.80),     # ~22 km: HERE should not return it, but we verify anyway
        ]

    monkeypatch.setattr(here_places, "_discover_at", fake_discover)
    result = await here_places.find_nearby_places(
        [{"latitude": 15.5, "longitude": 73.8}], "museum", radius_km=5, limit=10, search_query="museums",
    )

    assert seen_radius and all(radius == 5 for radius in seen_radius)
    assert [item["name"] for item in result] == ["Near Museum"]
    assert result[0]["distance_from_reference_km"] <= 5
