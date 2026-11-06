"""Deterministic stand-ins for every external provider. Importable from any test (tests/ is on sys.path)."""
from __future__ import annotations

from typing import Any

from services.errors import AppError

COORDS: dict[str, tuple[float, float, str, str]] = {
    # name: (lat, lon, HERE resultType, state)
    "Mumbai": (19.0760, 72.8777, "locality", "Maharashtra"),
    "Pune": (18.5204, 73.8567, "locality", "Maharashtra"),
    "Hyderabad": (17.3850, 78.4867, "locality", "Telangana"),
    "Bhubaneswar": (20.2961, 85.8245, "locality", "Odisha"),
    "Nagpur": (21.1458, 79.0882, "locality", "Maharashtra"),
    "Bengaluru": (12.9716, 77.5946, "locality", "Karnataka"),
    "Mysuru": (12.2958, 76.6394, "locality", "Karnataka"),
    "Goa": (15.2993, 74.1240, "administrativeArea", "Goa"),
    "Odisha": (20.9517, 85.0985, "administrativeArea", "Odisha"),
    "Baga Beach": (15.5553, 73.7517, "place", "Goa"),
    "Ooty": (11.40556, 76.70051, "locality", "Tamil Nadu"),
    "Puri": (19.8080, 85.8340, "locality", "Odisha"),
}
AREAS = {
    "Odisha": {"name": "Odisha", "kind": "state", "state": "Odisha", "state_code": "OD", "display_name": "Odisha, India",
               "bbox": {"west": 81.39167, "south": 17.81122, "east": 87.48681, "north": 22.56729},
               "center": {"latitude": 20.2, "longitude": 84.4}},
    "Ooty": {"name": "Ooty", "kind": "city", "state": "Tamil Nadu", "state_code": "TN", "display_name": "Ooty, Tamil Nadu, India",
             "bbox": {"west": 76.65, "south": 11.35, "east": 76.75, "north": 11.45},
             "center": {"latitude": 11.40556, "longitude": 76.70051}},
    "Bhubaneswar": {"name": "Bhubaneswar", "kind": "city", "state": "Odisha", "state_code": "OD", "display_name": "Bhubaneswar, Odisha, India",
                    "bbox": {"west": 85.7189, "south": 20.19797, "east": 85.89395, "north": 20.37955},
                    "center": {"latitude": 20.2961, "longitude": 85.8245}},
}
AMBIGUOUS = {
    "Aurangabad": [
        ("Aurangabad, Maharashtra, India", 19.8762, 75.3433, "Maharashtra"),
        ("Aurangabad, Bihar, India", 24.7521, 84.3742, "Bihar"),
    ]
}


def candidate(name: str) -> dict[str, Any]:
    lat, lon, kind, state = COORDS[name]
    return {"display_name": f"{name}, {state}, India", "latitude": lat, "longitude": lon, "result_type": kind,
            "state": state, "country": "India", "query_score": 1.0}


async def resolve_candidates(query: str):
    """Mirror of services.mcp_tools.resolve_place_candidates."""
    if query in AMBIGUOUS:
        cands = [{"display_name": d, "latitude": la, "longitude": lo, "result_type": "locality", "state": st,
                  "country": "India", "query_score": 1.0} for d, la, lo, st in AMBIGUOUS[query]]
        return cands, cands
    if query in COORDS:
        return [candidate(query)], []
    return [], []


async def resolve_area(query):
    """Mirror of services.mcp_tools.resolve_area_candidate: states/cities resolve to an area, anything else does not."""
    area = AREAS.get(query)
    return (dict(area), []) if area else (None, [])


AREA_CALLS: list[dict[str, Any]] = []


async def area_pois(area, category, limit=80, search_query=None):
    AREA_CALLS.append({"area": area["name"], "category": category, "limit": limit, "query": search_query})
    pois = [{"name": f"{category.title()} {i}", "category": category, "latitude": 20.0 + i, "longitude": 85.0, "city": "Bhubaneswar",
             "state": area.get("state"), "here_category": category.title(), "primary_category_id": "400-4000-4581"} for i in (1, 2)]
    return {"pois": pois, "source_status": "verified", "source": "HERE Search API", "source_error": None,
            "search": {"method": "browse", "category_filtered": True, "rejected": 0, "outside_area": 3, "requests": 6, "tiles": 6,
                       "complete": True, "matched": 2, "unresolved_full_tiles": 0, "budget_exhausted": False}}


# Specific places HERE knows by name (what /discover returns), for "Where is ...?" and "Tell me about ...".
NAMED_PLACES: dict[str, dict[str, Any]] = {
    "Kempegowda International Airport": {
        "name": "Kempegowda International Airport", "category": "airport", "here_category": "Airport", "latitude": 13.1986, "longitude": 77.7066,
        "address": "Kempegowda International Airport, Devanahalli, Bengaluru 560300, India", "city": "Bengaluru", "state": "Karnataka",
        "here_id": "here:pds:place:kia", "source": "HERE Search API", "primary_category_id": "400-4000-4581"},
    "Puri Jagannath Temple": {
        "name": "Shri Jagannath Temple", "category": "place", "here_category": "Temple", "latitude": 19.8049, "longitude": 85.8180,
        "address": "Grand Road, Puri 752001, India", "city": "Puri", "state": "Odisha", "here_id": "here:pds:place:jagannath",
        "rating": 4.6, "rating_count": 5200, "rating_url": "https://www.tripadvisor.com/Attraction_Review-puri", "rating_source": "tripadvisor",
        "photo": {"small": "https://media-cdn.tripadvisor.com/media/photo-l/jag.jpg", "large": "https://media-cdn.tripadvisor.com/media/photo-s/jag.jpg", "supplier": "tripadvisor"},
        "source": "HERE Search API"},
}
SUBJECT_CALLS: list[str] = []


async def resolve_subject(query, near=None):
    """Mirror of services.mcp_tools.resolve_subject: a named place, a city/state area, or nothing."""
    SUBJECT_CALLS.append(query)
    if query in NAMED_PLACES:
        return {"kind": "place", "place": dict(NAMED_PLACES[query]), "alternatives": []}
    area = AREAS.get(query)
    return {"kind": "area", "area": dict(area), "ambiguous": []} if area else None


HIGHLIGHT_CALLS: list[dict[str, Any]] = []


def _photo(code: str) -> dict[str, str]:
    return {"small": f"https://media-cdn.tripadvisor.com/media/photo-l/{code}.jpg",
            "large": f"https://media-cdn.tripadvisor.com/media/photo-s/{code}.jpg", "supplier": "tripadvisor"}


def highlight_places() -> list[dict[str, Any]]:
    """Places shaped like HERE's Discover results with Tripadvisor attributes (rating, review count, photo)."""
    base = {"category": "tourist_attraction", "source": "HERE Search API", "category_validated": True, "city": "Ooty", "state": "Tamil Nadu"}
    return [
        {**base, "name": "Government Rose Garden", "here_category": "Garden", "latitude": 11.4064, "longitude": 76.7116, "rating": 4.0,
         "rating_count": 1320, "rating_url": "https://www.tripadvisor.com/Attraction_Review-1", "rating_source": "tripadvisor",
         "photo": _photo("rose"), "open_now": False, "opening_hours": "Mon-Sun: 07:00 - 18:30", "distance_from_reference_km": 1.1, "here_id": "here:pds:place:rose"},
        {**base, "name": "Otty Lake", "here_category": "Tourist Attraction", "latitude": 11.4101, "longitude": 76.6904, "rating": 4.1,
         "rating_count": 800, "rating_url": "https://www.tripadvisor.com/Attraction_Review-2", "rating_source": "tripadvisor",
         "photo": _photo("lake"), "open_now": True, "opening_hours": "Mon-Sun: 09:00 - 18:00", "distance_from_reference_km": 0.8, "here_id": "here:pds:place:lake"},
        {**base, "name": "Doddabetta Peak", "here_category": "Scenic Point", "latitude": 11.3993, "longitude": 76.7364, "rating": 4.4,
         "rating_count": 640, "rating_url": "https://www.tripadvisor.com/Attraction_Review-3", "rating_source": "tripadvisor",
         "photo": _photo("peak"), "open_now": None, "distance_from_reference_km": 9.3, "here_id": "here:pds:place:peak"},
        {**base, "name": "Heritage Museum", "here_category": "History Museum", "latitude": 11.4085, "longitude": 76.6942,
         "distance_from_reference_km": 0.4, "here_id": "here:pds:place:museum"},
    ]


async def highlights(latitude, longitude, radius_km=25.0, limit=40):
    HIGHLIGHT_CALLS.append({"latitude": latitude, "longitude": longitude, "radius_km": radius_km, "limit": limit})
    return {"pois": highlight_places()[:limit], "source_status": "verified", "source": "HERE Search API", "source_error": None,
            "search": {"method": "discover", "queries": 6, "requests": 6, "matched": 4, "with_photos": 3, "with_ratings": 3, "radius_km": radius_km}}


async def route(stops, ev=None, transport_mode="car"):
    """500 km per leg at 1.2 min/km: easy to assert on."""
    legs, total_km, total_min = [], 0.0, 0.0
    for i in range(len(stops) - 1):
        km = 500.0
        legs.append({"from_stop_index": i, "to_stop_index": i + 1, "distance_km": km, "duration_minutes": round(km * 1.2, 1)})
        total_km += km
        total_min += km * 1.2
    positions, so_far, minutes = [], 0.0, 0.0
    for i, stop in enumerate(stops):
        if i:
            so_far += legs[i - 1]["distance_km"]
            minutes += legs[i - 1]["duration_minutes"]
        positions.append({"stop_index": i, "name": stop["name"], "latitude": stop["latitude"], "longitude": stop["longitude"],
                          "distance_from_start_km": so_far, "duration_from_start_minutes": minutes,
                          "route_position_percent": round(so_far / total_km * 100, 2) if total_km else 0})
    return {"distance_km": total_km, "duration_minutes": round(total_min, 1), "transport_mode": transport_mode,
            "geometry": {"type": "LineString", "coordinates": [[s["longitude"], s["latitude"]] for s in stops]},
            "legs": legs, "stop_positions": positions, "waypoint_count": len(stops), "provider": "fake"}


async def nearby(points, category, radius_km, limit=80, search_query=None):
    lat, lon = points[0]["latitude"], points[0]["longitude"]
    return {"pois": [{"name": f"{category.title()} {i}", "category": category, "latitude": lat + 0.005 * i, "longitude": lon,
                      "distance_from_reference_km": round(0.5 * i, 2), "address": f"{i} Test Road"} for i in (1, 2, 3)],
            "source_status": "verified", "source": "HERE Search API", "source_error": None}


async def route_pois(route_geometry, category, corridor_km, limit=60, search_query=None):
    return {"pois": [{"name": f"Route {category.title()}", "category": category, "latitude": 19.0, "longitude": 75.0,
                      "distance_from_route_km": 0.8, "route_position_percent": 30.0}],
            "source_status": "verified", "source": "HERE Search API", "source_error": None, "coverage": {"complete": True, "route_km": 1000}}


async def weather(latitude, longitude):
    return {"available": True, "provider": "Open-Meteo", "condition": "Clear sky", "temperature_c": 25.0,
            "wind_kmh": 8.0, "latitude": latitude, "longitude": longitude}


def chargers(route_, corridor):
    return [{"id": "c1", "name": "Charge One", "latitude": 19.9, "longitude": 76.5, "chargerType": "CCS2",
             "route_position_percent": 30.0, "distance_from_route_km": 0.5, "estimated_power_kw": 50.0},
            {"id": "c2", "name": "Charge Two", "latitude": 18.0, "longitude": 80.0, "chargerType": "CCS2",
             "route_position_percent": 60.0, "distance_from_route_km": 0.7, "estimated_power_kw": 50.0},
            {"id": "c3", "name": "Charge Three", "latitude": 18.5, "longitude": 83.0, "chargerType": "CCS2",
             "route_position_percent": 85.0, "distance_from_route_km": 0.9, "estimated_power_kw": 50.0}]


def install(monkeypatch, **overrides) -> None:
    """Replace every provider the orchestrator uses. ``overrides`` swap individual fakes."""
    import services.orchestrator as orchestrator

    parts = {
        "resolve_place_candidates": resolve_candidates, "calculate_route": route,
        "resolve_area_candidate": resolve_area, "find_area_pois_with_source": area_pois,
        "find_destination_highlights_with_source": highlights, "resolve_subject": resolve_subject,
        "find_nearby_pois_with_source": nearby, "find_route_pois_with_source": route_pois,
        "get_current_weather": weather, "_route_chargers": chargers,
    }
    parts.update(overrides)
    for name, fake in parts.items():
        monkeypatch.setattr(orchestrator, name, fake)


async def failing_route(stops, ev=None, transport_mode="car"):
    raise AppError("provider_unavailable", detail="HERE Routing returned HTTP 503")
