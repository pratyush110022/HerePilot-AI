"""Pure geometry helpers: route midpoints and bounding-box tiling. No I/O, no provider calls."""
from __future__ import annotations

import math
from typing import Any

from services.route_chargers import haversine_distance_km

# A /browse request is limited to a 250 km radius around its ``at`` point (HERE docs), so a tile is kept well inside
# that: with a 300 km side its farthest corner is ~212 km from the centre.
MAX_TILE_SIDE_KM = 300.0

BBox = dict[str, float]  # {"west", "south", "east", "north"}


def route_length_km(coordinates: list[list[float]]) -> float:
    """Length of a ``[[lon, lat], ...]`` polyline in km."""
    return sum(haversine_distance_km(a[1], a[0], b[1], b[0]) for a, b in zip(coordinates, coordinates[1:]))


def point_along_route(coordinates: list[list[float]], fraction: float = 0.5) -> dict[str, Any] | None:
    """The point ``fraction`` (0..1) of the way along the route *by distance*, or ``None`` for a degenerate route.

    The route geometry is walked segment by segment, so a route that bends or doubles back is handled correctly
    (this is not the average of the end points).
    """
    points = [c for c in coordinates if isinstance(c, (list, tuple)) and len(c) >= 2]
    if len(points) < 2:
        return None
    fraction = min(max(float(fraction), 0.0), 1.0)
    total = route_length_km(points)
    if total <= 0:
        return None
    target = total * fraction
    travelled = 0.0
    for a, b in zip(points, points[1:]):
        segment = haversine_distance_km(a[1], a[0], b[1], b[0])
        if segment > 0 and travelled + segment >= target - 1e-9:
            t = (target - travelled) / segment
            return {
                "latitude": a[1] + (b[1] - a[1]) * t,
                "longitude": a[0] + (b[0] - a[0]) * t,
                "distance_from_start_km": round(target, 2),
                "route_km": round(total, 2),
                "fraction": fraction,
            }
        travelled += segment
    last = points[-1]
    return {"latitude": last[1], "longitude": last[0], "distance_from_start_km": round(total, 2), "route_km": round(total, 2), "fraction": fraction}


def bbox_center(bbox: BBox) -> tuple[float, float]:
    return (bbox["north"] + bbox["south"]) / 2.0, (bbox["east"] + bbox["west"]) / 2.0


def bbox_size_km(bbox: BBox) -> tuple[float, float]:
    """(width, height) in km, measured at the box's mid latitude."""
    mid_lat = (bbox["north"] + bbox["south"]) / 2.0
    width = haversine_distance_km(mid_lat, bbox["west"], mid_lat, bbox["east"])
    height = haversine_distance_km(bbox["south"], bbox["west"], bbox["north"], bbox["west"])
    return width, height


def valid_bbox(value: Any) -> BBox | None:
    if not isinstance(value, dict):
        return None
    try:
        box = {k: float(value[k]) for k in ("west", "south", "east", "north")}
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in box.values()):
        return None
    if not (-90 <= box["south"] < box["north"] <= 90 and -180 <= box["west"] < box["east"] <= 180):
        return None
    return box


def tile_bbox(bbox: BBox, max_side_km: float = MAX_TILE_SIDE_KM) -> list[BBox]:
    """Cut ``bbox`` into a grid whose tiles are at most ``max_side_km`` on each side."""
    width, height = bbox_size_km(bbox)
    columns = max(1, math.ceil(width / max_side_km))
    rows = max(1, math.ceil(height / max_side_km))
    lon_step = (bbox["east"] - bbox["west"]) / columns
    lat_step = (bbox["north"] - bbox["south"]) / rows
    tiles: list[BBox] = []
    for r in range(rows):
        for c in range(columns):
            tiles.append({
                "west": bbox["west"] + c * lon_step, "east": bbox["west"] + (c + 1) * lon_step,
                "south": bbox["south"] + r * lat_step, "north": bbox["south"] + (r + 1) * lat_step,
            })
    return tiles


def split_bbox(bbox: BBox) -> list[BBox]:
    """Four equal quadrants, used to dig deeper into a tile that returned a full page of results."""
    mid_lat, mid_lon = bbox_center(bbox)
    return [
        {"west": bbox["west"], "east": mid_lon, "south": bbox["south"], "north": mid_lat},
        {"west": mid_lon, "east": bbox["east"], "south": bbox["south"], "north": mid_lat},
        {"west": bbox["west"], "east": mid_lon, "south": mid_lat, "north": bbox["north"]},
        {"west": mid_lon, "east": bbox["east"], "south": mid_lat, "north": bbox["north"]},
    ]


def bbox_param(bbox: BBox) -> str:
    """HERE's ``in=bbox:west,south,east,north`` value."""
    return f"bbox:{bbox['west']:.6f},{bbox['south']:.6f},{bbox['east']:.6f},{bbox['north']:.6f}"


def place_identity(place: dict[str, Any]) -> str:
    """A stable identity for a place: its HERE ID, else its name and position (to ~1 m)."""
    here_id = str(place.get("here_id") or "").strip().lower()
    if here_id:
        return f"id:{here_id}"
    try:
        return f"pos:{float(place['latitude']):.5f},{float(place['longitude']):.5f}:{str(place.get('name') or '').casefold().strip()}"
    except (KeyError, TypeError, ValueError):
        return f"name:{str(place.get('name') or '').casefold().strip()}"


def dedupe_places(places: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each place once, in the order given. Two records are the same place only if they share a HERE ID (or, with no ID,
    a name and a position), so distinct places that merely sit at the same coordinates are all kept."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for place in places:
        if not isinstance(place, dict):
            continue
        key = place_identity(place)
        if key not in seen:
            seen.add(key)
            out.append(place)
    return out
