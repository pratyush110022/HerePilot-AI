import pytest


@pytest.mark.asyncio
async def test_nearby_places_falls_back_to_osm_when_here_unavailable(monkeypatch):
    import services.mcp_tools as tools

    async def unavailable_here(*args, **kwargs):
        raise RuntimeError("HERE returned 503")

    async def working_osm(points, category, radius_km, limit):
        return [{
            "name": "Named beach",
            "category": category,
            "latitude": 15.51,
            "longitude": 73.76,
            "distance_from_reference_km": 4.2,
            "source": "OpenStreetMap / Overpass",
        }]

    monkeypatch.setattr(tools, "find_nearby_places", unavailable_here)
    monkeypatch.setattr(tools, "find_osm_pois_near_points", working_osm)

    result = await tools.find_nearby_pois_with_source(
        [{"latitude": 15.5, "longitude": 73.8}], "tourist_attraction", 15, 10, "tourist places"
    )

    assert result["source_status"] == "fallback"
    assert result["source"] == "OpenStreetMap / Overpass"
    assert result["pois"][0]["name"] == "Named beach"
    assert result["pois"][0]["latitude"] == 15.51
    assert abs(result["pois"][0]["distance_from_reference_km"] - 4.43) < 0.02


@pytest.mark.asyncio
async def test_nearby_places_reports_unavailable_when_both_providers_fail(monkeypatch):
    import services.mcp_tools as tools

    async def unavailable_here(*args, **kwargs):
        raise RuntimeError("HERE returned 503")

    async def unavailable_osm(*args, **kwargs):
        raise RuntimeError("Overpass timed out")

    monkeypatch.setattr(tools, "find_nearby_places", unavailable_here)
    monkeypatch.setattr(tools, "find_osm_pois_near_points", unavailable_osm)

    result = await tools.find_nearby_pois_with_source(
        [{"latitude": 15.5, "longitude": 73.8}], "tourist_attraction", 15, 10, "tourist places"
    )

    assert result["source_status"] == "unavailable"
    assert result["pois"] == []
    assert "HERE" in result["source_error"]
    assert "OSM" in result["source_error"]
