from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from services.errors import AppError, provider_error_from_status
from services.http_utils import request

try:
    import flexpolyline as _here_flexpolyline  # type: ignore
except ImportError:
    _here_flexpolyline = None

logger = logging.getLogger(__name__)

HERE_ROUTING_URL = os.getenv("HERE_ROUTING_URL", "https://router.hereapi.com/v8/routes")
HERE_API_KEY = os.getenv("HERE_API_KEY")
# Native HERE EV routing needs a vehicle consumption model (speed tables, ascent/descent factors,
# charging curve) that this app does not have, so it is OFF unless explicitly enabled. EV results
# come from the deterministic planner, which uses the bundled charging dataset.
HERE_EV_ENABLED = os.getenv("HERE_EV_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}

MAX_ROUTE_STOPS = 10
TRANSPORT_MODES = ("car", "pedestrian", "bicycle")

_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
_DEC = {char: index for index, char in enumerate(_ALPHABET)}


def _decode_unsigned_values(encoded: str) -> list[int]:
    values: list[int] = []
    result = 0
    shift = 0
    for char in encoded:
        if char not in _DEC:
            raise ValueError("Invalid HERE flexible polyline character.")
        value = _DEC[char]
        result |= (value & 0x1F) << shift
        if value & 0x20:
            shift += 5
        else:
            values.append(result)
            result = 0
            shift = 0
    if shift:
        raise ValueError("Invalid HERE flexible polyline encoding.")
    return values


def _decode_signed(value: int) -> int:
    """Zig-zag-free signed decode used by HERE flexible polyline: odd values are ``~(value >> 1)``.

    (``-(value >> 1)`` is off by one for every negative delta, which accumulates into route drift.)
    """
    return ~(value >> 1) if value & 1 else value >> 1


def decode_flexible_polyline(encoded: str) -> list[list[float]]:
    """Decode HERE Flexible Polyline into GeoJSON [longitude, latitude] coordinates."""
    encoded = str(encoded or "").strip()
    if not encoded:
        return []

    # Prefer HERE's maintained reference implementation when installed. The
    # local implementation below remains available so the project can still
    # run in offline/dev environments where dependencies cannot be downloaded.
    if _here_flexpolyline is not None:
        decoded = _here_flexpolyline.decode(encoded)
        return [[float(lon), float(lat)] for lat, lon, *_ in decoded]

    return decode_flexible_polyline_reference(encoded)


def decode_flexible_polyline_reference(encoded: str) -> list[list[float]]:
    """Pure-Python decoder (no third-party dependency). Exposed so tests can cross-check it."""
    encoded = str(encoded or "").strip()
    if not encoded:
        return []
    values = _decode_unsigned_values(encoded)
    if not values:
        return []

    # HERE Flexible Polyline stores two separate header varints:
    #   1) version
    #   2) format header (precision + third-dimension metadata)
    if len(values) < 2:
        raise ValueError("Incomplete HERE flexible polyline header.")

    version = values[0]
    header = values[1]
    if version != 1:
        raise ValueError(f"Unsupported HERE flexible polyline version: {version}")

    precision = header & 0x0F
    third_dim = (header >> 4) & 0x07
    third_dim_precision = (header >> 7) & 0x0F

    factor = 10 ** precision
    z_factor = 10 ** third_dim_precision
    lat = 0
    lon = 0
    z = 0
    index = 2
    coordinates: list[list[float]] = []

    coordinate_values = len(values) - index
    values_per_coordinate = 3 if third_dim else 2
    if coordinate_values % values_per_coordinate != 0:
        raise ValueError(
            "Malformed HERE flexible polyline: "
            f"header={header}, version={version}, precision={precision}, "
            f"third_dim={third_dim}, third_dim_precision={third_dim_precision}, "
            f"coordinate_values={coordinate_values}, "
            f"values_per_coordinate={values_per_coordinate}."
        )

    while index < len(values):
        if index + 1 >= len(values):
            raise ValueError(
                "Incomplete HERE flexible polyline coordinate pair: "
                f"remaining_values={len(values) - index}."
            )

        lat += _decode_signed(values[index])
        index += 1
        lon += _decode_signed(values[index])
        index += 1

        if third_dim:
            if index >= len(values):
                raise ValueError("Incomplete HERE flexible polyline third dimension.")
            z += _decode_signed(values[index])
            index += 1
            coordinates.append([lon / factor, lat / factor, z / z_factor])
        else:
            coordinates.append([lon / factor, lat / factor])

    return coordinates


def _validate_stops(stops: list[dict[str, Any]], transport_mode: str = "car") -> None:
    if len(stops) < 2:
        raise AppError("needs_trip", "A route needs at least an origin and a destination.")
    if len(stops) > MAX_ROUTE_STOPS:
        raise AppError("invalid_request", f"A route can have at most {MAX_ROUTE_STOPS} stops.")
    if transport_mode not in TRANSPORT_MODES:
        raise AppError("invalid_request", "That travel mode isn't supported. Use driving, walking or cycling.")
    if not HERE_API_KEY:
        raise AppError("provider_not_configured", detail="HERE_API_KEY is not set")
    for index, stop in enumerate(stops):
        try:
            lat = float(stop["latitude"])
            lon = float(stop["longitude"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AppError("invalid_request", f"Stop {index + 1} has no valid coordinates.") from exc
        if not (-90 <= lat <= 90 and -180 <= lon <= 180) or lat != lat or lon != lon:
            raise AppError("invalid_request", f"Stop {index + 1} has no valid coordinates.")


def _build_params(stops: list[dict[str, Any]], ev: dict[str, Any] | None = None, transport_mode: str = "car") -> list[tuple[str, str]]:
    params: list[tuple[str, str]] = [
        ("origin", f"{float(stops[0]['latitude'])},{float(stops[0]['longitude'])}"),
        ("destination", f"{float(stops[-1]['latitude'])},{float(stops[-1]['longitude'])}"),
        ("transportMode", transport_mode),
        ("routingMode", "fast"),
        ("return", "polyline,summary"),
        ("apiKey", str(HERE_API_KEY)),
    ]
    for stop in stops[1:-1]:
        params.append(("via", f"{float(stop['latitude'])},{float(stop['longitude'])}"))

    if ev and HERE_EV_ENABLED and transport_mode == "car":
        params.append(("ev[makeReachable]", "true"))
        capacity = ev.get("battery_capacity_kwh")
        battery_percent = ev.get("current_battery_percent")
        if capacity is not None and battery_percent is not None:
            try:
                initial_kwh = max(0.0, float(capacity) * float(battery_percent) / 100.0)
                # HERE's documented required names (kWh): ev[initialCharge] and ev[maxCharge].
                params.append(("ev[initialCharge]", f"{initial_kwh:.4f}"))
                params.append(("ev[maxCharge]", f"{float(capacity):.4f}"))
            except (TypeError, ValueError):
                pass
    return params


def _validate_decoded_geometry(
    coordinates: list[list[float]],
    stops: list[dict[str, Any]],
    max_endpoint_error_km: float = 5.0,
) -> tuple[float, float]:
    """Measure decoded-polyline endpoint agreement with the requested route."""
    if len(coordinates) < 2:
        raise AppError("no_route", detail="decoded geometry has fewer than two coordinates")
    for coordinate in coordinates:
        if len(coordinate) < 2:
            raise AppError("no_route", detail="decoded an invalid coordinate")
        lon, lat = float(coordinate[0]), float(coordinate[1])
        if not -180 <= lon <= 180 or not -90 <= lat <= 90:
            raise AppError("no_route", detail=f"decoded an out-of-bounds coordinate: {coordinate!r}")

    # A correct HERE flexible-polyline decoder must begin/end near the requested
    # origin/destination. This catches silent decoder corruption before it is
    # used for charger corridor filtering or displayed on the map.
    from services.route_chargers import haversine_distance_km

    first_lon, first_lat = coordinates[0][:2]
    last_lon, last_lat = coordinates[-1][:2]
    origin = stops[0]
    destination = stops[-1]
    start_error = haversine_distance_km(
        float(origin["latitude"]), float(origin["longitude"]),
        float(first_lat), float(first_lon),
    )
    end_error = haversine_distance_km(
        float(destination["latitude"]), float(destination["longitude"]),
        float(last_lat), float(last_lon),
    )
    return start_error, end_error


def _parse_route(data: dict[str, Any], stops: list[dict[str, Any]], provider_suffix: str = "", transport_mode: str = "car") -> dict[str, Any]:
    if not isinstance(data, dict):
        raise AppError("provider_unavailable", detail="routing response was not an object")
    routes = data.get("routes") or []
    if not routes:
        raise AppError("no_route", detail="HERE returned no routes")

    route = routes[0]
    sections = route.get("sections") or []
    if not sections:
        raise AppError("no_route", detail="HERE returned a route with no sections")

    all_coordinates: list[list[float]] = []
    flexible_polylines: list[str] = []
    legs: list[dict[str, Any]] = []
    cumulative_distance = 0.0
    cumulative_duration = 0.0

    for index, section in enumerate(sections):
        polyline = section.get("polyline")
        if not isinstance(polyline, str) or not polyline:
            raise AppError("provider_unavailable", detail=f"section {index + 1} has no polyline")
        flexible_polylines.append(polyline)
        try:
            coords = decode_flexible_polyline(polyline)
        except ValueError as exc:
            raise AppError("provider_unavailable", detail=f"section {index + 1}: undecodable polyline ({type(exc).__name__})") from exc
        if len(coords) < 2:
            raise AppError("provider_unavailable", detail=f"section {index + 1} has invalid geometry")
        if all_coordinates and coords[0] == all_coordinates[-1]:
            coords = coords[1:]
        all_coordinates.extend(coords)

        summary = section.get("summary") or {}
        distance_km = max(0.0, float(summary.get("length", 0.0)) / 1000.0)
        duration_minutes = max(0.0, float(summary.get("duration", 0.0)) / 60.0)
        legs.append({
            "from_stop_index": index,
            "to_stop_index": min(index + 1, len(stops) - 1),
            "distance_km": round(distance_km, 2),
            "duration_minutes": round(duration_minutes, 1),
        })
        cumulative_distance += distance_km
        cumulative_duration += duration_minutes

    start_error_km, end_error_km = _validate_decoded_geometry(all_coordinates, stops)

    route_summary = route.get("summary") or {}
    total_distance_km = cumulative_distance or float(route_summary.get("length", 0.0)) / 1000.0
    total_duration_minutes = cumulative_duration or float(route_summary.get("duration", 0.0)) / 60.0
    if total_distance_km <= 0 or total_duration_minutes <= 0:
        raise AppError("no_route", detail="HERE returned an empty route summary")

    stop_positions: list[dict[str, Any]] = []
    distance_so_far = 0.0
    duration_so_far = 0.0
    for index, stop in enumerate(stops):
        if index > 0 and index - 1 < len(legs):
            distance_so_far += legs[index - 1]["distance_km"]
            duration_so_far += legs[index - 1]["duration_minutes"]
        stop_positions.append({
            "stop_index": index,
            "name": stop.get("name") or stop.get("display_name") or f"Stop {index + 1}",
            "latitude": float(stop["latitude"]),
            "longitude": float(stop["longitude"]),
            "distance_from_start_km": round(distance_so_far, 2),
            "duration_from_start_minutes": round(duration_so_far, 1),
            "route_position_percent": round(distance_so_far / max(total_distance_km, 0.001) * 100.0, 2),
        })

    return {
        "provider": f"HERE Routing API v8{provider_suffix}",
        "distance_meters": round(total_distance_km * 1000.0, 1),
        "distance_km": round(total_distance_km, 2),
        "duration_seconds": round(total_duration_minutes * 60.0, 1),
        "duration_minutes": round(total_duration_minutes, 1),
        "geometry": {"type": "LineString", "coordinates": all_coordinates},
        "here_flexible_polyline": flexible_polylines[0] if len(flexible_polylines) == 1 else None,
        "here_flexible_polylines": flexible_polylines,
        "sections": [
            {"section_index": i, "polyline": section.get("polyline"), "summary": section.get("summary", {})}
            for i, section in enumerate(sections)
        ],
        "legs": legs,
        "stop_positions": stop_positions,
        "waypoint_count": len(stops),
        "routing_mode": "fast",
        "transport_mode": transport_mode,
        "native_ev_routing": bool(provider_suffix),
        "geometry_validation": {
            "start_error_km": round(start_error_km, 4),
            "end_error_km": round(end_error_km, 4),
            "decoder": "flexpolyline package" if _here_flexpolyline is not None else "reference implementation",
            "endpoint_check_passed": start_error_km <= 5.0 and end_error_km <= 5.0,
        },
    }


async def get_multi_stop_route(
    stops: list[dict[str, Any]],
    ev: dict[str, Any] | None = None,
    transport_mode: str = "car",
) -> dict[str, Any]:
    """Calculate an ordered route through ``stops`` with HERE Routing API v8.

    Provider failures are raised as :class:`AppError`; the response body is logged (redacted) but
    never placed in the exception message.
    """
    _validate_stops(stops, transport_mode)
    params = _build_params(stops, ev, transport_mode)
    try:
        response = await request(
            "GET",
            HERE_ROUTING_URL,
            timeout=httpx.Timeout(connect=10.0, read=60.0, write=20.0, pool=10.0),
            params=params,
            headers={"Accept": "application/json", "User-Agent": "HerePilot-AI/5.0"},
        )
    except httpx.HTTPError as exc:
        raise AppError("provider_unavailable", detail=f"HERE Routing request failed: {type(exc).__name__}") from exc
    if response.status_code != 200:
        error = provider_error_from_status(response.status_code, provider="HERE Routing")
        logger.warning("event=here_routing_error http_status=%s", response.status_code)
        raise error
    try:
        data = response.json()
    except ValueError as exc:
        raise AppError("provider_unavailable", detail="HERE Routing returned invalid JSON") from exc
    return _parse_route(data, stops, " · Native EV routing" if ev and HERE_EV_ENABLED and transport_mode == "car" else "", transport_mode)


async def get_route(start_latitude: float, start_longitude: float, end_latitude: float, end_longitude: float):
    return await get_multi_stop_route([
        {"name": "Origin", "latitude": start_latitude, "longitude": start_longitude},
        {"name": "Destination", "latitude": end_latitude, "longitude": end_longitude},
    ])
