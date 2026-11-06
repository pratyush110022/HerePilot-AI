import csv
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


# ============================================================
# Tata Charging Station Dataset
# ============================================================

DATA_FILE = (
    Path(__file__).resolve().parent.parent
    / "data"
    / "tata_ev_charging_stations.csv"
)


# Cache the dataset in memory.
# 9,683 records is small enough for this portfolio project.
_TATA_CHARGERS = None


def charger_metadata(charger_type: str | None, cpo_name: str | None, app_name: str | None) -> dict:
    """Normalize charger presentation fields without inventing live availability."""
    socket = (charger_type or "").strip().upper()
    cpo = (cpo_name or "").strip().lower()
    app = (app_name or "").strip().lower()

    if socket in {"CCS2", "CCS", "CHADEMO", "GB/T DC", "GB/T"}:
        speed = "DC Fast"
        power_kw = 50.0
    elif socket in {"TYPE2", "TYPE 2", "AC TYPE 2", "AC"}:
        speed = "AC Regular"
        power_kw = 7.2
    else:
        speed = "Fast / DC" if any(token in socket for token in ("DC", "CCS", "CHADEMO")) else "Charger type not specified"
        power_kw = 50.0 if speed == "Fast / DC" else None

    if any(token in cpo or token in app for token in ("bpcl", "hellobpcl")):
        brand_key = "bpcl"
    elif any(token in cpo or token in app for token in ("indianoil", "iocl", "indian oil", "e-charge")):
        brand_key = "iocl"
    elif "zeon" in cpo or "zeon" in app:
        brand_key = "zeon"
    elif "tata power" in cpo or "ez charge" in app or "tata power" in app:
        brand_key = "tata-power-ez-charge"
    elif "shell" in cpo or "shell" in app:
        brand_key = "shell-recharge"
    elif "jio" in cpo or "pulse" in app:
        brand_key = "jiobp-pulse"
    elif "statiq" in cpo or "statiq" in app:
        brand_key = "statiq"
    elif "chargezone" in cpo or "chargezone" in app:
        brand_key = "chargezone"
    else:
        brand_key = "generic-ev"

    return {
        "socket_type": charger_type or "Not specified",
        "charging_speed": speed,
        "estimated_power_kw": power_kw,
        "brand_key": brand_key,
    }


# ============================================================
# Load Tata Charging Stations
# ============================================================

def load_tata_chargers():
    """
    Load Tata EV charging stations from CSV.

    The dataset contains:
    - id
    - uid
    - region
    - city
    - state
    - country
    - name
    - address
    - latitude
    - longitude
    - pincode
    - chargerType
    - appName
    - cpoName
    - customerCategory
    - megaChargers
    """

    global _TATA_CHARGERS

    if _TATA_CHARGERS is not None:
        return _TATA_CHARGERS

    if not DATA_FILE.exists():
        raise FileNotFoundError(
            f"Tata charging station file not found: {DATA_FILE}"
        )

    chargers = []

    with open(
        DATA_FILE,
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as file:

        reader = csv.DictReader(file)

        for row in reader:

            try:
                latitude = float(row["latitude"])
                longitude = float(row["longitude"])
            except (
                TypeError,
                ValueError,
                KeyError,
            ):
                continue

            charger = {
                "id": row.get("id"),
                "uid": row.get("uid"),
                "region": row.get("region"),
                "city": row.get("city"),
                "state": row.get("state"),
                "country": row.get("country"),
                "name": row.get("name"),
                "address": row.get("address"),
                "latitude": latitude,
                "longitude": longitude,
                "pincode": row.get("pincode"),
                "chargerType": row.get("chargerType"),
                "appName": row.get("appName"),
                "cpoName": row.get("cpoName"),
                "customerCategory": row.get(
                    "customerCategory"
                ),
                "megaChargers": row.get(
                    "megaChargers"
                ),
                **charger_metadata(
                    row.get("chargerType"),
                    row.get("cpoName"),
                    row.get("appName"),
                ),
            }

            chargers.append(charger)

    _TATA_CHARGERS = chargers

    logger.info("event=charger_dataset_loaded stations=%d", len(_TATA_CHARGERS))

    return _TATA_CHARGERS


# ============================================================
# Get All Tata Chargers
# ============================================================

def get_all_tata_chargers():
    """
    Return all Tata charging stations.
    """

    return load_tata_chargers()


# ============================================================
# Get Tata Chargers Inside Bounding Box
# ============================================================

def get_tata_chargers_in_bbox(
    south: float,
    west: float,
    north: float,
    east: float,
):
    """
    Return Tata charging stations inside a
    latitude/longitude bounding box.
    """

    chargers = load_tata_chargers()

    results = []

    for charger in chargers:

        latitude = charger["latitude"]
        longitude = charger["longitude"]

        if latitude < south:
            continue

        if latitude > north:
            continue

        if longitude < west:
            continue

        if longitude > east:
            continue

        results.append(charger)

    return results


# ============================================================
# Search Tata Chargers
# ============================================================

def search_tata_chargers(
    city=None,
    state=None,
    charger_type=None,
    cpo_name=None,
    customer_category=None,
):
    """
    Search Tata charging stations using optional filters.
    """

    chargers = load_tata_chargers()

    results = []

    for charger in chargers:

        if city:
            if (
                str(charger.get("city", "")).strip().lower()
                != city.strip().lower()
            ):
                continue

        if state:
            if (
                str(charger.get("state", "")).strip().lower()
                != state.strip().lower()
            ):
                continue

        if charger_type:
            if (
                str(
                    charger.get("chargerType", "")
                ).strip().lower()
                != charger_type.strip().lower()
            ):
                continue

        if cpo_name:
            if (
                str(
                    charger.get("cpoName", "")
                ).strip().lower()
                != cpo_name.strip().lower()
            ):
                continue

        if customer_category:
            if (
                str(
                    charger.get(
                        "customerCategory",
                        "",
                    )
                ).strip().lower()
                != customer_category.strip().lower()
            ):
                continue

        results.append(charger)

    return results


# ============================================================
# Dataset Statistics
# ============================================================

def get_tata_charger_stats():

    chargers = load_tata_chargers()

    states = set()
    cities = set()
    charger_types = set()
    operators = set()

    for charger in chargers:

        if charger.get("state"):
            states.add(charger["state"])

        if charger.get("city"):
            cities.add(charger["city"])

        if charger.get("chargerType"):
            charger_types.add(
                charger["chargerType"]
            )

        if charger.get("cpoName"):
            operators.add(
                charger["cpoName"]
            )

    return {
        "total_chargers": len(chargers),
        "states": len(states),
        "cities": len(cities),
        "charger_types": sorted(
            charger_types
        ),
        "operators": len(operators),
    }

def search_tata_chargers_nearby(latitude: float, longitude: float, radius_km: float = 25.0, limit: int = 50):
    """Return GeoPilot/Tata dataset chargers near an arbitrary location."""
    from services.route_chargers import haversine_distance_km

    results = []
    for charger in load_tata_chargers():
        distance = haversine_distance_km(
            float(latitude),
            float(longitude),
            float(charger["latitude"]),
            float(charger["longitude"]),
        )
        if distance <= radius_km:
            item = dict(charger)
            item["distance_from_reference_km"] = round(distance, 3)
            item["source"] = "GeoPilot EV Charging Dataset"
            results.append(item)
    results.sort(key=lambda item: float(item.get("distance_from_reference_km", 999999)))
    return results[:limit]
