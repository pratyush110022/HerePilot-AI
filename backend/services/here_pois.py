from __future__ import annotations

import asyncio
import math
import os
from typing import Any

import httpx

from services.http_utils import request

HERE_DISCOVER_URL = os.getenv("HERE_DISCOVER_URL", "https://discover.search.hereapi.com/v1/discover")
HERE_API_KEY = os.getenv("HERE_API_KEY")

CATEGORY_QUERIES = {
    "restaurant": "restaurant",
    "cafe": "cafe",
    "hotel": "hotel",
    "hospital": "hospital",
    "pharmacy": "pharmacy",
    "fuel": "petrol station",
    "supermarket": "supermarket",
    "convenience": "convenience store",
    "tourist_attraction": "tourist attraction",
    "parking": "parking",
    "bank": "bank",
    "atm": "ATM",
    "toilet": "public toilet",
}

CATEGORY_ALIASES = {
    "restaurants": "restaurant", "food": "restaurant",
    "hotels": "hotel", "lodging": "hotel", "stays": "hotel",
    "attractions": "tourist_attraction", "tourism": "tourist_attraction",
    "tourist": "tourist_attraction", "tourist places": "tourist_attraction",
    "tourist attractions": "tourist_attraction", "tourist spots": "tourist_attraction",
    "fuel station": "fuel", "petrol": "fuel", "petrol pump": "fuel",
    "supermarkets": "supermarket", "shops": "convenience",
}


def normalize_category(value: str) -> str:
    value = (value or "").strip().lower().replace("_", " ")
    return CATEGORY_ALIASES.get(value, value.replace(" ", "_"))


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return r * (2 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1 - a))))


def _distance_to_route_km(coordinates: list[list[float]], lat: float, lon: float) -> float:
    if not coordinates:
        return float("inf")
    # Equirectangular approximation is accurate enough for short POI-to-route segments.
    best = float("inf")
    for point in coordinates:
        best = min(best, _haversine_km(point[1], point[0], lat, lon))
    return best


def _route_position_percent(coordinates: list[list[float]], lat: float, lon: float) -> float:
    if not coordinates:
        return 0.0
    distances = [_haversine_km(p[1], p[0], lat, lon) for p in coordinates]
    idx = min(range(len(distances)), key=distances.__getitem__)
    return round((idx / max(len(coordinates) - 1, 1)) * 100.0, 2)


def _parse_item(item: dict[str, Any], category: str, reference: dict[str, float] | None = None) -> dict[str, Any] | None:
    position = item.get("position") or {}
    if position.get("lat") is None or position.get("lng") is None:
        return None
    address = item.get("address") or {}
    contacts = item.get("contacts") or []
    phone = None
    website = None
    if contacts:
        phone = next((x.get("value") for x in (contacts[0].get("phone") or []) if x.get("value")), None)
        website = next((x.get("value") for x in (contacts[0].get("www") or []) if x.get("value")), None)
    categories = item.get("categories") or []
    category_name = categories[0].get("name") if categories else None
    result = {
        "here_id": item.get("id"),
        "name": item.get("title") or address.get("label") or "Place",
        "category": category,
        "here_category": category_name,
        "latitude": float(position["lat"]),
        "longitude": float(position["lng"]),
        "operator": item.get("chains", [{}])[0].get("name") if item.get("chains") else None,
        "phone": phone,
        "website": website,
        "address": address.get("label"),
        "opening_hours": (item.get("openingHours") or [{}])[0].get("text") if item.get("openingHours") else None,
        "source": "HERE Places / Discover",
    }
    if reference:
        result["distance_from_reference_km"] = round(_haversine_km(reference["latitude"], reference["longitude"], result["latitude"], result["longitude"]), 2)
    return result


async def _discover_at_point(point: dict[str, float], category: str, radius_km: float, limit: int) -> list[dict[str, Any]]:
    if not HERE_API_KEY:
        raise RuntimeError("HERE_API_KEY is not configured. Add it to backend/.env.")
    query = CATEGORY_QUERIES.get(category)
    if not query:
        raise ValueError(f"Unsupported HERE POI category '{category}'.")
    params = {
        "at": f"{point['latitude']},{point['longitude']}",
        "q": query,
        "in": f"circle:{point['latitude']},{point['longitude']};r={int(max(100, min(radius_km * 1000, 50000)))}",
        "limit": max(1, min(limit, 100)),
        "apiKey": HERE_API_KEY,
    }
    response = await request(
        "GET", HERE_DISCOVER_URL,
        timeout=httpx.Timeout(connect=10.0, read=45.0, write=20.0, pool=10.0),
        headers={"Accept": "application/json", "User-Agent": "GeoPilot-AI/3.0"},
        params=params,
    )
    if response.status_code != 200:
        raise RuntimeError(f"HERE Discover returned {response.status_code}: {response.text[:500]}")
    items = response.json().get("items", [])
    parsed = []
    for item in items:
        poi = _parse_item(item, category, point)
        if poi:
            parsed.append(poi)
    return parsed


def _sample_route(points: list[list[float]], max_points: int = 24) -> list[dict[str, float]]:
    if not points:
        return []
    if len(points) <= max_points:
        indexes = range(len(points))
    else:
        indexes = [round(i * (len(points) - 1) / (max_points - 1)) for i in range(max_points)]
    return [{"latitude": points[i][1], "longitude": points[i][0]} for i in indexes]


async def find_pois_near_points(points: list[dict[str, float]], category: str, radius_km: float = 3.0, limit: int = 80) -> list[dict[str, Any]]:
    category = normalize_category(category)
    if category == "ev_charging":
        raise ValueError("EV charging uses the GeoPilot EV Charging Dataset, not HERE Places.")
    if not points:
        return []
    tasks = [_discover_at_point(point, category, radius_km, max(10, min(limit, 50))) for point in points]
    results = await asyncio.gather(*tasks)
    seen: set[str] = set()
    combined: list[dict[str, Any]] = []
    for batch in results:
        for poi in batch:
            key = str(poi.get("here_id") or f"{poi['name']}|{poi['latitude']:.6f}|{poi['longitude']:.6f}")
            if key not in seen:
                seen.add(key)
                combined.append(poi)
    combined.sort(key=lambda p: p.get("distance_from_reference_km", float("inf")))
    return combined[:limit]


async def find_pois_along_route(route_geometry: dict[str, Any], category: str = "restaurant", corridor_km: float = 5.0, limit: int = 60) -> list[dict[str, Any]]:
    category = normalize_category(category)
    if category == "ev_charging":
        raise ValueError("EV charging uses the GeoPilot EV Charging Dataset, not HERE Places.")
    coordinates = route_geometry.get("coordinates", [])
    sample_points = _sample_route(coordinates, max_points=24)
    results = await find_pois_near_points(sample_points, category, max(corridor_km, 3.0), limit * 2)
    for poi in results:
        poi["distance_from_route_km"] = round(_distance_to_route_km(coordinates, poi["latitude"], poi["longitude"]), 2)
        poi["route_position_percent"] = _route_position_percent(coordinates, poi["latitude"], poi["longitude"])
    results = [p for p in results if p["distance_from_route_km"] <= corridor_km]
    results.sort(key=lambda p: (p["distance_from_route_km"], p.get("route_position_percent", 0)))
    return results[:limit]
