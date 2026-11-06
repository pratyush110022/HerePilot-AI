import math

from services.routing import get_route
from services.tata_chargers import get_tata_chargers_in_bbox
from services.route_chargers import filter_chargers_near_route
from services.ev_planner import plan_ev_trip


def calculate_route_bbox(route_geometry: dict, corridor_km: float):
    coordinates = route_geometry.get("coordinates", [])

    if not coordinates:
        raise ValueError("Route geometry contains no coordinates.")

    longitudes = [point[0] for point in coordinates]
    latitudes = [point[1] for point in coordinates]

    min_longitude = min(longitudes)
    max_longitude = max(longitudes)
    min_latitude = min(latitudes)
    max_latitude = max(latitudes)

    center_latitude = (
        min_latitude + max_latitude
    ) / 2

    latitude_buffer = corridor_km / 111.0

    longitude_factor = (
        111.0
        * math.cos(math.radians(center_latitude))
    )

    if abs(longitude_factor) < 0.000001:
        longitude_factor = 0.000001

    longitude_buffer = corridor_km / longitude_factor

    return {
        "south": min_latitude - latitude_buffer,
        "west": min_longitude - longitude_buffer,
        "north": max_latitude + latitude_buffer,
        "east": max_longitude + longitude_buffer,
    }


def get_route_tata_chargers_for_agent(
    route_geometry: dict,
    corridor_km: float,
):
    bbox = calculate_route_bbox(
        route_geometry=route_geometry,
        corridor_km=corridor_km,
    )

    candidate_chargers = get_tata_chargers_in_bbox(
        south=bbox["south"],
        west=bbox["west"],
        north=bbox["north"],
        east=bbox["east"],
    )

    route_chargers = filter_chargers_near_route(
        route_geometry=route_geometry,
        stations=candidate_chargers,
        corridor_km=corridor_km,
    )

    return candidate_chargers, route_chargers


def compact_charger(charger: dict):
    return {
        "name": charger.get("name"),
        "city": charger.get("city"),
        "state": charger.get("state"),
        "latitude": charger.get("latitude"),
        "longitude": charger.get("longitude"),
        "charger_type": charger.get("chargerType"),
        "operator": charger.get("cpoName"),
        "app": charger.get("appName"),
    }


async def plan_ev_trip_for_agent(
    start_latitude: float,
    start_longitude: float,
    end_latitude: float,
    end_longitude: float,
    max_range_km: float,
    battery_percent: float,
    reserve_percent: float = 15.0,
    target_charge_percent: float = 80.0,
    corridor_km: float = 5.0,
):
    if target_charge_percent <= reserve_percent:
        raise ValueError(
            "target_charge_percent must be greater than reserve_percent."
        )

    if battery_percent <= reserve_percent:
        raise ValueError(
            "Starting battery must be greater than reserve_percent."
        )

    route_result = await get_route(
        start_latitude=start_latitude,
        start_longitude=start_longitude,
        end_latitude=end_latitude,
        end_longitude=end_longitude,
    )

    route_geometry = route_result.get("geometry")

    if not route_geometry:
        raise ValueError("OSRM returned no route geometry.")

    candidate_chargers, route_chargers = (
        get_route_tata_chargers_for_agent(
            route_geometry=route_geometry,
            corridor_km=corridor_km,
        )
    )

    plan = plan_ev_trip(
        route_distance_km=route_result["distance_km"],
        chargers=route_chargers,
        max_range_km=max_range_km,
        starting_battery_percent=battery_percent,
        reserve_percent=reserve_percent,
        target_charge_percent=target_charge_percent,
    )

    compact_stops = []

    for stop in plan.get("charging_stops", []):
        compact_stops.append(
            {
                "charger": compact_charger(
                    stop.get("charger", {})
                ),
                "distance_from_previous_stop_km": stop.get(
                    "distance_from_previous_stop_km"
                ),
                "distance_from_start_km": stop.get(
                    "distance_from_start_km"
                ),
                "route_position_percent": stop.get(
                    "route_position_percent"
                ),
                "battery_arrival_percent": stop.get(
                    "battery_arrival_percent"
                ),
                "battery_departure_percent": stop.get(
                    "battery_departure_percent"
                ),
                "charge_added_percent": stop.get(
                    "charge_added_percent"
                ),
            }
        )

    return {
        "route": {
            "distance_km": route_result["distance_km"],
            "duration_minutes": route_result["duration_minutes"],
        },
        "vehicle": {
            "max_range_km": max_range_km,
            "starting_battery_percent": battery_percent,
            "reserve_percent": reserve_percent,
            "target_charge_percent": target_charge_percent,
        },
        "charging_search": {
            "corridor_km": corridor_km,
            "candidate_chargers": len(candidate_chargers),
            "route_relevant_chargers": len(route_chargers),
        },
        "plan": {
            "trip_possible": plan.get("trip_possible"),
            "requires_charging": plan.get("requires_charging"),
            "route_distance_km": plan.get("route_distance_km"),
            "total_battery_required_percent": plan.get(
                "total_battery_required_percent"
            ),
            "starting_battery_percent": plan.get(
                "starting_battery_percent"
            ),
            "destination_battery_percent": plan.get(
                "destination_battery_percent"
            ),
            "charging_stops": compact_stops,
            "message": plan.get("message"),
            "failure_reason": plan.get("failure_reason"),
        },
    }
