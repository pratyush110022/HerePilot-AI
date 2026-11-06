from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import time
from typing import Any

import httpx

from services.http_utils import request
from services.logging_setup import describe_exception, url_host

from services.route_chargers import (
    calculate_distance_to_route_km,
    calculate_route_position_percent,
)


logger = logging.getLogger(__name__)

OVERPASS_URLS = [
    os.getenv("OVERPASS_URL", "https://overpass-api.de/api/interpreter"),
    "https://overpass.kumi.systems/api/interpreter",
]
HEADERS = {
    "User-Agent": "HerePilot-AI/5.0 (OSM POI exploration)",
    "Accept": "application/json",
}

CATEGORY_DEFINITIONS: dict[str, list[tuple[str, str]]] = {
    "restaurant": [("amenity", "restaurant"), ("amenity", "fast_food"), ("amenity", "food_court")],
    "cafe": [("amenity", "cafe")],
    "hotel": [("tourism", "hotel"), ("tourism", "motel"), ("tourism", "guest_house"), ("tourism", "hostel")],
    "hospital": [("amenity", "hospital"), ("healthcare", "hospital")],
    "pharmacy": [("amenity", "pharmacy"), ("healthcare", "pharmacy")],
    "fuel": [("amenity", "fuel")],
    "supermarket": [("shop", "supermarket")],
    "convenience": [("shop", "convenience")],
    "tourist_attraction": [
        ("tourism", "attraction"), ("tourism", "museum"),
        ("tourism", "viewpoint"), ("tourism", "artwork"),
        ("tourism", "gallery"), ("tourism", "theme_park"),
        ("tourism", "zoo"), ("historic", "monument"),
        ("historic", "castle"), ("historic", "ruins"),
        ("historic", "archaeological_site"), ("natural", "beach"),
    ],
    "parking": [("amenity", "parking")],
    "ev_charging": [("amenity", "charging_station")],
    "bank": [("amenity", "bank")],
    "atm": [("amenity", "atm")],
    "pub_bar": [("amenity", "pub"), ("amenity", "bar")],
    "nightlife": [("amenity", "nightclub"), ("amenity", "music_venue"), ("amenity", "pub"), ("amenity", "bar")],
    "shopping_mall": [("shop", "mall")],
    "car_repair": [("shop", "car_repair"), ("craft", "car_repair")],
    "car_wash": [("amenity", "car_wash")],
    "toilet": [("amenity", "toilets")],
}

ALIAS_TO_CATEGORY = {
    "restaurants": "restaurant",
    "food": "restaurant",
    "eat": "restaurant",
    "eating": "restaurant",
    "hotels": "hotel",
    "lodging": "hotel",
    "stays": "hotel",
    "hospitals": "hospital",
    "medical": "hospital",
    "pharmacies": "pharmacy",
    "petrol": "fuel",
    "petrol pump": "fuel",
    "gas": "fuel",
    "fuel station": "fuel",
    "supermarkets": "supermarket",
    "shops": "convenience",
    "attractions": "tourist_attraction",
    "tourism": "tourist_attraction",
    "charging": "ev_charging",
    "chargers": "ev_charging",
    "pubs": "pub_bar",
    "bars": "pub_bar",
    "nightclubs": "nightlife",
    "malls": "shopping_mall",
    "car repair": "car_repair",
    "car wash": "car_wash",
}

_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_TTL_SECONDS = 300
_SEMAPHORE = asyncio.Semaphore(2)


def normalize_category(value: str) -> str:
    normalized = (value or "").strip().lower()
    return ALIAS_TO_CATEGORY.get(normalized, normalized)


def _bbox_query(south: float, west: float, north: float, east: float, category: str) -> str:
    tags = CATEGORY_DEFINITIONS.get(category)
    if not tags:
        raise ValueError(
            f"Unsupported POI category '{category}'. Supported: {', '.join(sorted(CATEGORY_DEFINITIONS))}"
        )
    selectors = "\n".join(
        f'  nwr["{key}"="{value}"]({south},{west},{north},{east});'
        for key, value in tags
    )
    return f"""
[out:json][timeout:45];
(
{selectors}
);
out center tags;
"""


def _around_points_query(points: list[dict[str, float]], radius_m: int, categories: list[str]) -> str:
    clauses: list[str] = []
    for point in points:
        for category in categories:
            for key, value in CATEGORY_DEFINITIONS[category]:
                clauses.append(
                    f'  nwr["{key}"="{value}"](around:{radius_m},{point["latitude"]},{point["longitude"]});'
                )
    joined = "\n".join(clauses)
    return f"""
[out:json][timeout:45];
(
{joined}
);
out center tags;
"""


def _cache_key(query: str) -> str:
    return hashlib.sha1(query.encode("utf-8")).hexdigest()


async def query_overpass(query: str) -> dict[str, Any]:
    key = _cache_key(query)
    cached = _CACHE.get(key)
    if cached and (time.monotonic() - cached[0]) < _CACHE_TTL_SECONDS:
        return cached[1]

    last_error: Exception | None = None
    async with _SEMAPHORE:
        for endpoint in OVERPASS_URLS:
            for attempt in range(2):
                try:
                    timeout = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)
                    response = await request(
                        "POST",
                        endpoint,
                        timeout=timeout,
                        headers=HEADERS,
                        data={"data": query},
                    )
                    if response.status_code == 429:
                        logger.warning(
                            "event=overpass_rate_limited endpoint_host=%s attempt=%d", url_host(endpoint), attempt + 1
                        )
                        await asyncio.sleep(2.0 * (attempt + 1))
                        last_error = RuntimeError("Overpass rate limited request (429).")
                        continue
                    if response.status_code != 200:
                        logger.warning(
                            "event=overpass_http_error endpoint_host=%s http_status=%s attempt=%d",
                            url_host(endpoint), response.status_code, attempt + 1,
                        )
                        # The body can echo the query; keep only the status code.
                        raise RuntimeError(f"Overpass returned HTTP {response.status_code}")
                    data = response.json()
                    _CACHE[key] = (time.monotonic(), data)
                    return data
                except Exception as exc:
                    last_error = exc
                    logger.warning(
                        "event=overpass_attempt_failed endpoint_host=%s attempt=%d error=%s",
                        url_host(endpoint), attempt + 1, describe_exception(exc),
                    )
                    await asyncio.sleep(0.6 * (attempt + 1))

    raise RuntimeError(f"All Overpass endpoints failed ({type(last_error).__name__ if last_error else 'unknown'})")


def parse_pois(data: dict[str, Any], category: str | None = None) -> list[dict[str, Any]]:
    pois: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for element in data.get("elements", []):
        tags = element.get("tags", {}) or {}
        lat = element.get("lat")
        lon = element.get("lon")
        if lat is None or lon is None:
            center = element.get("center") or {}
            lat = center.get("lat")
            lon = center.get("lon")
        if lat is None or lon is None:
            continue

        osm_type = str(element.get("type", ""))
        osm_id = str(element.get("id", ""))
        key = (osm_type, osm_id)
        if key in seen:
            continue
        seen.add(key)

        inferred_category = category or infer_category(tags)
        address = ", ".join(
            part
            for part in [
                tags.get("addr:housenumber"),
                tags.get("addr:street"),
                tags.get("addr:city"),
            ]
            if part
        )
        pois.append(
            {
                "osm_id": element.get("id"),
                "osm_type": element.get("type"),
                "name": tags.get("name") or "Unnamed place",
                "category": inferred_category,
                "latitude": float(lat),
                "longitude": float(lon),
                "operator": tags.get("operator"),
                "cuisine": tags.get("cuisine"),
                "website": tags.get("website") or tags.get("contact:website"),
                "phone": tags.get("phone") or tags.get("contact:phone"),
                "opening_hours": tags.get("opening_hours"),
                "address": address or None,
                "source": "OpenStreetMap / Overpass",
            }
        )

    return pois


def infer_category(tags: dict[str, Any]) -> str:
    for category, selectors in CATEGORY_DEFINITIONS.items():
        for key, value in selectors:
            if tags.get(key) == value:
                return category
    return "other"


def route_bbox(route_coordinates: list[list[float]], padding_km: float) -> dict[str, float]:
    if len(route_coordinates) < 2:
        raise ValueError("Route geometry must contain at least two coordinates.")
    min_lon = min(point[0] for point in route_coordinates)
    max_lon = max(point[0] for point in route_coordinates)
    min_lat = min(point[1] for point in route_coordinates)
    max_lat = max(point[1] for point in route_coordinates)
    lat_pad = padding_km / 111.0
    center_lat = (min_lat + max_lat) / 2.0
    lon_pad = padding_km / (111.0 * max(abs(math.cos(math.radians(center_lat))), 0.1))
    return {
        "south": min_lat - lat_pad,
        "west": min_lon - lon_pad,
        "north": max_lat + lat_pad,
        "east": max_lon + lon_pad,
    }


async def find_pois_along_route(
    route_geometry: dict[str, Any],
    category: str = "restaurant",
    corridor_km: float = 5.0,
    limit: int = 60,
) -> list[dict[str, Any]]:
    category = normalize_category(category)
    coords = route_geometry.get("coordinates", [])
    bbox = route_bbox(coords, corridor_km)
    query = _bbox_query(category=category, **bbox)
    data = await query_overpass(query)
    pois = parse_pois(data, category=category)

    for poi in pois:
        poi["distance_from_route_km"] = calculate_distance_to_route_km(
            coords, poi["latitude"], poi["longitude"]
        )
        poi["route_position_percent"] = calculate_route_position_percent(
            coords, poi["latitude"], poi["longitude"]
        )
    pois = [p for p in pois if p["distance_from_route_km"] is not None and p["distance_from_route_km"] <= corridor_km]
    pois.sort(key=lambda p: (p["distance_from_route_km"], p["route_position_percent"] or 0))
    return pois[:limit]


async def find_pois_near_points(
    points: list[dict[str, float]],
    category: str = "restaurant",
    radius_km: float = 3.0,
    limit: int = 80,
) -> list[dict[str, Any]]:
    category = normalize_category(category)
    if not points:
        return []
    radius_m = int(max(100, min(radius_km * 1000.0, 50_000)))
    query = _around_points_query(points, radius_m, [category])
    data = await query_overpass(query)
    pois = parse_pois(data, category=category)

    for poi in pois:
        nearest = min(
            haversine_km(poi["latitude"], poi["longitude"], p["latitude"], p["longitude"])
            for p in points
        )
        poi["distance_from_reference_km"] = round(nearest, 2)
    pois.sort(key=lambda p: p["distance_from_reference_km"])
    return pois[:limit]


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return r * (2 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1 - a))))
