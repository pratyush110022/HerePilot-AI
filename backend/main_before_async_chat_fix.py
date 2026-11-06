from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import math
from services.geocoding import search_places

# ============================================================
# Services
# ============================================================

from services.osm import (
    get_charging_stations,
    get_charging_stations_in_bbox,
)

from services.routing import (
    get_route,
)

from services.ev import (
    EVProfile,
    calculate_usable_range,
    calculate_remaining_battery,
    can_reach_destination,
)

from services.route_chargers import (
    filter_chargers_near_route,
)

from services.tata_chargers import (
    get_all_tata_chargers,
    get_tata_chargers_in_bbox,
    search_tata_chargers,
    get_tata_charger_stats,
)

from services.ev_planner import (
    plan_ev_trip,
)

from pydantic import BaseModel
from services.groq_llm import ask_groq


# ============================================================
# FastAPI Application
# ============================================================

app = FastAPI(
    title="GeoPilot AI",
    description="LLM-powered geospatial EV assistant",
    version="0.2.0",
)


# ============================================================
# CORS
# ============================================================

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# Root
# ============================================================

@app.get("/")
async def root():

    return {
        "project": "GeoPilot AI",
        "status": "running",
        "version": "0.2.0",
    }


# ============================================================
# Health
# ============================================================

@app.get("/health")
async def health():

    return {
        "status": "healthy",
    }


# ============================================================
# Geocoding / Place Search
# ============================================================

@app.get("/geocode/search")
async def geocode_search(
    q: str,
    limit: int = 5,
):

    if not q.strip():
        return {
            "query": q,
            "results": [],
        }

    try:

        results = await search_places(
            query=q,
            limit=min(limit, 10),
        )

        return {
            "query": q,
            "results": results,
        }

    except Exception as e:

        raise HTTPException(
            status_code=502,
            detail=f"Geocoding failed: {e}",
        )


# ============================================================
# OSM Charging Stations Around Point
# ============================================================

@app.get("/charging-stations")
async def charging_stations(
    latitude: float,
    longitude: float,
    radius: int = Query(
        default=5000,
        gt=0,
        le=50000,
    ),
):

    try:

        stations = await get_charging_stations(
            latitude=latitude,
            longitude=longitude,
            radius=radius,
        )

        return {
            "count": len(stations),
            "latitude": latitude,
            "longitude": longitude,
            "radius_meters": radius,
            "source": "OpenStreetMap / Overpass",
            "stations": stations,
        }

    except Exception as e:

        raise HTTPException(
            status_code=502,
            detail=str(e),
        )


# ============================================================
# OSM Charging Stations - Bounding Box
# ============================================================

@app.get("/charging-stations/bbox")
async def charging_stations_bbox(
    south: float,
    west: float,
    north: float,
    east: float,
):

    if south >= north:

        raise HTTPException(
            status_code=400,
            detail="south must be less than north",
        )

    if west >= east:

        raise HTTPException(
            status_code=400,
            detail="west must be less than east",
        )

    try:

        stations = (
            await get_charging_stations_in_bbox(
                south=south,
                west=west,
                north=north,
                east=east,
            )
        )

        return {
            "count": len(stations),

            "bbox": {
                "south": south,
                "west": west,
                "north": north,
                "east": east,
            },

            "source": "OpenStreetMap / Overpass",

            "stations": stations,
        }

    except Exception as e:

        raise HTTPException(
            status_code=502,
            detail=str(e),
        )


# ============================================================
# Tata - All Charging Stations
# ============================================================

@app.get("/tata-chargers")
async def tata_chargers(
    limit: int = Query(
        default=100,
        gt=0,
        le=10000,
    ),
):

    try:

        chargers = (
            get_all_tata_chargers()
        )

        return {
            "count": len(chargers),

            "returned": min(
                limit,
                len(chargers),
            ),

            "source": (
                "Tata EV Route Planner"
            ),

            "chargers": chargers[:limit],
        }

    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=str(e),
        )


# ============================================================
# Tata - Bounding Box
# ============================================================

@app.get("/tata-chargers/bbox")
async def tata_chargers_bbox(
    south: float,
    west: float,
    north: float,
    east: float,

    limit: int = Query(
        default=10000,
        gt=0,
        le=10000,
    ),
):

    if south >= north:

        raise HTTPException(
            status_code=400,
            detail="south must be less than north",
        )

    if west >= east:

        raise HTTPException(
            status_code=400,
            detail="west must be less than east",
        )

    try:

        chargers = (
            get_tata_chargers_in_bbox(
                south=south,
                west=west,
                north=north,
                east=east,
            )
        )

        return {
            "count": len(chargers),

            "returned": min(
                limit,
                len(chargers),
            ),

            "source": (
                "Tata EV Route Planner"
            ),

            "bbox": {
                "south": south,
                "west": west,
                "north": north,
                "east": east,
            },

            "chargers": chargers[:limit],
        }

    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=str(e),
        )


# ============================================================
# Tata - Search
# ============================================================

@app.get("/tata-chargers/search")
async def tata_chargers_search(
    city: str | None = None,
    state: str | None = None,
    charger_type: str | None = None,
    cpo_name: str | None = None,
    customer_category: str | None = None,

    limit: int = Query(
        default=100,
        gt=0,
        le=10000,
    ),
):

    try:

        chargers = (
            search_tata_chargers(
                city=city,
                state=state,
                charger_type=charger_type,
                cpo_name=cpo_name,
                customer_category=(
                    customer_category
                ),
            )
        )

        return {
            "count": len(chargers),

            "returned": min(
                limit,
                len(chargers),
            ),

            "source": (
                "Tata EV Route Planner"
            ),

            "filters": {
                "city": city,
                "state": state,
                "charger_type": (
                    charger_type
                ),
                "cpo_name": cpo_name,
                "customer_category": (
                    customer_category
                ),
            },

            "chargers": chargers[:limit],
        }

    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=str(e),
        )


# ============================================================
# Tata - Statistics
# ============================================================

@app.get("/tata-chargers/stats")
async def tata_chargers_stats():

    try:

        return (
            get_tata_charger_stats()
        )

    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=str(e),
        )


# ============================================================
# OSRM Route
# ============================================================

@app.get("/route")
async def route(
    start_latitude: float,
    start_longitude: float,
    end_latitude: float,
    end_longitude: float,
):

    try:

        result = await get_route(
            start_latitude=(
                start_latitude
            ),
            start_longitude=(
                start_longitude
            ),
            end_latitude=(
                end_latitude
            ),
            end_longitude=(
                end_longitude
            ),
        )

        return result

    except Exception as e:

        raise HTTPException(
            status_code=502,
            detail=str(e),
        )


# ============================================================
# Helper:
# Calculate Bounding Box Around Route
# ============================================================

def calculate_route_bbox(
    route_geometry: dict,
    corridor_km: float,
):

    route_coordinates = (
        route_geometry.get(
            "coordinates",
            [],
        )
    )

    if len(route_coordinates) < 2:

        raise ValueError(
            "Route geometry must contain "
            "at least two coordinates."
        )

    longitudes = [
        coordinate[0]
        for coordinate
        in route_coordinates
    ]

    latitudes = [
        coordinate[1]
        for coordinate
        in route_coordinates
    ]

    min_longitude = min(
        longitudes
    )

    max_longitude = max(
        longitudes
    )

    min_latitude = min(
        latitudes
    )

    max_latitude = max(
        latitudes
    )

    center_latitude = (
        min_latitude
        + max_latitude
    ) / 2


    # --------------------------------------------------------
    # Latitude
    #
    # Approximately:
    # 1 degree latitude = 111 km
    # --------------------------------------------------------

    latitude_buffer = (
        corridor_km
        / 111.0
    )


    # --------------------------------------------------------
    # Longitude
    #
    # Longitude distance changes
    # depending on latitude.
    # --------------------------------------------------------

    longitude_factor = (
        111.0
        * math.cos(
            math.radians(
                center_latitude
            )
        )
    )

    if abs(longitude_factor) < 0.000001:

        longitude_factor = 0.000001


    longitude_buffer = (
        corridor_km
        / longitude_factor
    )


    return {
        "south": (
            min_latitude
            - latitude_buffer
        ),

        "west": (
            min_longitude
            - longitude_buffer
        ),

        "north": (
            max_latitude
            + latitude_buffer
        ),

        "east": (
            max_longitude
            + longitude_buffer
        ),
    }


# ============================================================
# Helper:
# Get Tata Chargers Relevant to Route
# ============================================================

def get_route_tata_chargers(
    route_geometry: dict,
    corridor_km: float,
):

    bbox = calculate_route_bbox(
        route_geometry=route_geometry,
        corridor_km=corridor_km,
    )


    # --------------------------------------------------------
    # First:
    # Fast bounding-box filtering
    # --------------------------------------------------------

    candidate_chargers = (
        get_tata_chargers_in_bbox(
            south=bbox["south"],
            west=bbox["west"],
            north=bbox["north"],
            east=bbox["east"],
        )
    )


    # --------------------------------------------------------
    # Second:
    # Precise route-distance filtering
    # --------------------------------------------------------

    route_chargers = (
        filter_chargers_near_route(
            route_geometry=(
                route_geometry
            ),
            stations=(
                candidate_chargers
            ),
            corridor_km=(
                corridor_km
            ),
        )
    )


    return (
        candidate_chargers,
        route_chargers,
        bbox,
    )


# ============================================================
# Route-Aware Tata Charging Stations
# ============================================================

@app.get("/route/chargers")
async def route_chargers(
    start_latitude: float,
    start_longitude: float,
    end_latitude: float,
    end_longitude: float,

    corridor_km: float = Query(
        default=5.0,
        gt=0,
        le=50,
    ),
):

    # --------------------------------------------------------
    # Get OSRM Route
    # --------------------------------------------------------

    try:

        route_result = await get_route(
            start_latitude=(
                start_latitude
            ),
            start_longitude=(
                start_longitude
            ),
            end_latitude=(
                end_latitude
            ),
            end_longitude=(
                end_longitude
            ),
        )

    except Exception as e:

        raise HTTPException(
            status_code=502,
            detail=(
                f"Routing failed: {e}"
            ),
        )


    route_geometry = (
        route_result.get(
            "geometry"
        )
    )


    if not route_geometry:

        raise HTTPException(
            status_code=500,
            detail=(
                "OSRM returned no "
                "route geometry."
            ),
        )


    # --------------------------------------------------------
    # Get Tata Route Chargers
    # --------------------------------------------------------

    try:

        (
            candidate_chargers,
            route_relevant_chargers,
            bbox,
        ) = get_route_tata_chargers(
            route_geometry=(
                route_geometry
            ),
            corridor_km=(
                corridor_km
            ),
        )

    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=(
                f"Charger search failed: {e}"
            ),
        )


    # --------------------------------------------------------
    # Response
    # --------------------------------------------------------

    return {

        "source": (
            "Tata EV Route Planner"
        ),

        "route": {

            "distance_km": (
                route_result[
                    "distance_km"
                ]
            ),

            "duration_minutes": (
                route_result[
                    "duration_minutes"
                ]
            ),

            "geometry": (
                route_geometry
            ),
        },

        "search": {

            "corridor_km": (
                corridor_km
            ),

            "candidate_chargers": len(
                candidate_chargers
            ),

            "route_relevant_chargers": len(
                route_relevant_chargers
            ),

            "bbox": bbox,
        },

        "chargers": (
            route_relevant_chargers
        ),
    }


# ============================================================
# EV Trip Planner
# ============================================================

@app.get("/ev/plan")
async def ev_plan(
    start_latitude: float,
    start_longitude: float,
    end_latitude: float,
    end_longitude: float,

    max_range_km: float = Query(
        ...,
        gt=0,
    ),

    battery_percent: float = Query(
        ...,
        ge=0,
        le=100,
    ),

    reserve_percent: float = Query(
        default=15.0,
        ge=0,
        lt=100,
    ),

    target_charge_percent: float = Query(
        default=80.0,
        gt=0,
        le=100,
    ),

    corridor_km: float = Query(
        default=5.0,
        gt=0,
        le=50,
    ),
):

    # --------------------------------------------------------
    # Validate EV Parameters
    # --------------------------------------------------------

    if (
        target_charge_percent
        <= reserve_percent
    ):

        raise HTTPException(
            status_code=400,
            detail=(
                "target_charge_percent "
                "must be greater than "
                "reserve_percent."
            ),
        )


    if (
        battery_percent
        <= reserve_percent
    ):

        raise HTTPException(
            status_code=400,
            detail=(
                "Starting battery must be "
                "greater than reserve_percent."
            ),
        )


    # --------------------------------------------------------
    # Step 1:
    # Get OSRM Route
    # --------------------------------------------------------

    try:

        route_result = await get_route(
            start_latitude=(
                start_latitude
            ),
            start_longitude=(
                start_longitude
            ),
            end_latitude=(
                end_latitude
            ),
            end_longitude=(
                end_longitude
            ),
        )

    except Exception as e:

        raise HTTPException(
            status_code=502,
            detail=(
                f"Routing failed: {e}"
            ),
        )


    route_geometry = (
        route_result.get(
            "geometry"
        )
    )


    if not route_geometry:

        raise HTTPException(
            status_code=500,
            detail=(
                "OSRM returned no "
                "route geometry."
            ),
        )


    # --------------------------------------------------------
    # Step 2:
    # Get Route-Relevant Tata Chargers
    # --------------------------------------------------------

    try:

        (
            candidate_chargers,
            route_relevant_chargers,
            bbox,
        ) = get_route_tata_chargers(
            route_geometry=(
                route_geometry
            ),
            corridor_km=(
                corridor_km
            ),
        )

    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=(
                f"Charger search failed: {e}"
            ),
        )


    # --------------------------------------------------------
    # Step 3:
    # Run Deterministic EV Planner
    # --------------------------------------------------------

    try:

        plan = plan_ev_trip(

            route_distance_km=(
                route_result[
                    "distance_km"
                ]
            ),

            chargers=(
                route_relevant_chargers
            ),

            max_range_km=(
                max_range_km
            ),

            starting_battery_percent=(
                battery_percent
            ),

            reserve_percent=(
                reserve_percent
            ),

            target_charge_percent=(
                target_charge_percent
            ),
        )

    except ValueError as e:

        raise HTTPException(
            status_code=400,
            detail=str(e),
        )

    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=(
                f"EV planning failed: {e}"
            ),
        )


    # --------------------------------------------------------
    # Step 4:
    # Return Complete Trip Plan
    # --------------------------------------------------------

    return {

        "source": (
            "Tata EV Route Planner"
        ),

        "trip": {

            "start": {
                "latitude": (
                    start_latitude
                ),
                "longitude": (
                    start_longitude
                ),
            },

            "destination": {
                "latitude": (
                    end_latitude
                ),
                "longitude": (
                    end_longitude
                ),
            },
        },


        "vehicle": {

            "max_range_km": (
                max_range_km
            ),

            "starting_battery_percent": (
                battery_percent
            ),

            "reserve_percent": (
                reserve_percent
            ),

            "target_charge_percent": (
                target_charge_percent
            ),
        },


        "route": {

            "distance_km": (
                route_result[
                    "distance_km"
                ]
            ),

            "duration_minutes": (
                route_result[
                    "duration_minutes"
                ]
            ),

            "geometry": (
                route_geometry
            ),
        },


        "charging_search": {

            "corridor_km": (
                corridor_km
            ),

            "candidate_chargers": len(
                candidate_chargers
            ),

            "route_relevant_chargers": len(
                route_relevant_chargers
            ),

            "bbox": bbox,
        },


        "plan": plan,
    }


# ============================================================
# LLM Chat
# ============================================================

class ChatRequest(BaseModel):
    message: str


@app.post("/chat")
async def chat(request: ChatRequest):
    try:
        reply = ask_groq(request.message)

        return {
            "message": request.message,
            "reply": reply,
        }

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"LLM request failed: {str(e)}",
        )


# ============================================================
# EV Battery Check
# ============================================================

@app.get("/ev/check")
async def ev_check(
    max_range_km: float,
    battery_percent: float,
    distance_km: float,

    reserve_percent: float = 15.0,
):

    # --------------------------------------------------------
    # Validation
    # --------------------------------------------------------

    if max_range_km <= 0:

        raise HTTPException(
            status_code=400,
            detail=(
                "max_range_km must be "
                "greater than zero."
            ),
        )


    if not (
        0
        <= battery_percent
        <= 100
    ):

        raise HTTPException(
            status_code=400,
            detail=(
                "battery_percent must be "
                "between 0 and 100."
            ),
        )


    if not (
        0
        <= reserve_percent
        < 100
    ):

        raise HTTPException(
            status_code=400,
            detail=(
                "reserve_percent must be "
                "between 0 and 100."
            ),
        )


    # --------------------------------------------------------
    # EV Profile
    # --------------------------------------------------------

    ev = EVProfile(
        max_range_km=(
            max_range_km
        ),

        battery_percent=(
            battery_percent
        ),

        reserve_percent=(
            reserve_percent
        ),
    )


    # --------------------------------------------------------
    # Calculations
    # --------------------------------------------------------

    usable_range = (
        calculate_usable_range(
            ev
        )
    )


    remaining_battery = (
        calculate_remaining_battery(
            ev,
            distance_km,
        )
    )


    reachable = (
        can_reach_destination(
            ev,
            distance_km,
        )
    )


    # --------------------------------------------------------
    # Response
    # --------------------------------------------------------

    return {

        "vehicle": {

            "max_range_km": (
                max_range_km
            ),

            "battery_percent": (
                battery_percent
            ),

            "reserve_percent": (
                reserve_percent
            ),
        },


        "trip": {

            "distance_km": (
                distance_km
            ),
        },


        "calculation": {

            "usable_range_km": round(
                usable_range,
                2,
            ),

            "remaining_battery_percent": (
                remaining_battery
            ),

            "can_reach_destination": (
                reachable
            ),
        },
    }