from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from services.errors import AppError, provider_error_from_status
from services.geo_utils import (
    MAX_TILE_SIDE_KM,
    bbox_center,
    bbox_param,
    bbox_size_km,
    split_bbox,
    tile_bbox,
    valid_bbox,
)
from services.http_utils import request
from services.place_categories import CategorySpec, browse_categories_param, classify, is_generic_query, spec_for
from services.route_chargers import (
    calculate_distance_to_route_km,
    calculate_route_position_percent,
    haversine_distance_km,
)

logger = logging.getLogger(__name__)

HERE_GEOCODE_URL = os.getenv("HERE_GEOCODE_URL", "https://geocode.search.hereapi.com/v1/geocode")
HERE_DISCOVER_URL = os.getenv("HERE_DISCOVER_URL", "https://discover.search.hereapi.com/v1/discover")
HERE_BROWSE_URL = os.getenv("HERE_BROWSE_URL", "https://browse.search.hereapi.com/v1/browse")
HERE_API_KEY = os.getenv("HERE_API_KEY")
USER_AGENT = "HerePilot-AI/5.0"

# EV charging deliberately stays outside this generic HERE Places path.
# Everything else can use a provider query so the LLM/heuristics are not
# restricted to a tiny hard-coded category list.
CATEGORY_QUERY = {
    "restaurant": "restaurants",
    "cafe": "cafes",
    "hotel": "hotels",
    "hospital": "hospitals",
    "pharmacy": "pharmacies",
    "fuel": "fuel stations",
    "supermarket": "supermarkets",
    "convenience": "convenience stores",
    "tourist_attraction": "tourist attractions",
    "parking": "parking",
    "pub_bar": "pubs bars",
    "nightlife": "nightlife",
    "shopping_mall": "shopping malls",
    "bank": "banks",
    "atm": "ATMs",
    "car_repair": "car repair",
    "car_wash": "car wash",
}

CATEGORY_QUERY_FALLBACKS = {
    # HERE Discover's natural-language interpretation varies across regions.
    # Use alternate terms only for the category where this was observed to
    # under-return results, then apply an exact geodesic radius filter.
    "tourist_attraction": [
        "tourist attractions", "places to visit", "landmarks",
        "beaches", "historic sites", "museums", "viewpoints",
    ],
}

# Route-corridor search: bounded provider calls, honest about coverage.
MAX_ROUTE_SAMPLES = 20
ROUTE_SEARCH_CONCURRENCY = 4

# HERE returns at most 100 places per request and offset does not reach beyond that cap (HERE developer guidance:
# cover a large area by querying smaller areas and merging by place ID). Area search therefore tiles the area and
# splits any tile that comes back full, within a request budget; if the budget runs out the result says so.
BROWSE_PAGE_LIMIT = 100
MAX_AREA_REQUESTS = 24
MAX_AREA_SPLIT_DEPTH = 3
MAX_AREA_TILES = 12
AREA_CONCURRENCY = 4

# HERE geocode result types, most specific "place the user means by a city name" first.
_AREA_TYPES = {"locality", "administrativeArea"}
_AREA_RANK = {"locality": 0, "administrativeArea": 0, "place": 2, "street": 3, "intersection": 3,
              "houseNumber": 4, "addressBlock": 4, "postalCodePoint": 4, "chargingPoint": 5}


def _require_key() -> str:
    if not HERE_API_KEY:
        raise AppError("provider_not_configured", detail="HERE_API_KEY is not set")
    return HERE_API_KEY


async def _get_json(url: str, params: dict[str, Any], *, timeout: float, provider: str) -> dict[str, Any] | list[Any]:
    """GET + parse JSON. Failures become safe AppErrors; bodies are never copied into messages."""
    try:
        response = await request(
            "GET", url, timeout=timeout,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT}, params=params,
        )
    except httpx.TimeoutException as exc:
        raise AppError("provider_unavailable", detail=f"{provider} timed out ({type(exc).__name__})") from exc
    except httpx.HTTPError as exc:
        raise AppError("provider_unavailable", detail=f"{provider} request failed ({type(exc).__name__})") from exc
    if response.status_code != 200:
        logger.warning("event=here_http_error provider=%s http_status=%s", provider, response.status_code)
        raise provider_error_from_status(response.status_code, provider=provider)
    try:
        return response.json()
    except ValueError as exc:
        raise AppError("provider_unavailable", detail=f"{provider} returned invalid JSON") from exc


# Photos and ratings come from HERE's Tripadvisor attributes (`show=tripadvisor`), a restricted beta that only some API
# keys may use. They are requested for the searches whose results are shown as cards. If HERE rejects the request the
# search is repeated without them, and they are not asked for again for a few minutes, so a missing entitlement can
# never break place search. HERE_RICH_PLACES=0 turns them off.
RICH_SHOW = "tripadvisor,tripadvisorImageVariants"
_RICH_COOLDOWN_S = 600.0
_rich_blocked_until = 0.0


def _rich_wanted(rich: bool) -> bool:
    if not rich or os.getenv("HERE_RICH_PLACES", "1").strip().lower() in {"0", "false", "off", "no"}:
        return False
    return time.monotonic() >= _rich_blocked_until


async def _get_places_json(url: str, params: dict[str, Any], *, rich: bool, timeout: float = 20.0) -> dict[str, Any] | list[Any]:
    global _rich_blocked_until
    if _rich_wanted(rich):
        try:
            return await _get_json(url, {**params, "show": RICH_SHOW}, timeout=timeout, provider="HERE Search")
        except AppError as exc:
            if exc.code not in {"provider_unavailable", "provider_not_configured"}:
                raise
            logger.warning("event=here_rich_attributes_failed code=%s", exc.code)
            data = await _get_json(url, params, timeout=timeout, provider="HERE Search")
            _rich_blocked_until = time.monotonic() + _RICH_COOLDOWN_S  # the plain request worked: it was the extras
            return data
    return await _get_json(url, params, timeout=timeout, provider="HERE Search")


def _first_contact(item: dict[str, Any], key: str) -> str | None:
    contacts = item.get("contacts")
    for contact in contacts if isinstance(contacts, list) else []:
        values = contact.get(key) if isinstance(contact, dict) else None
        for value in values if isinstance(values, list) else []:
            candidate = value.get("value") if isinstance(value, dict) else value
            if candidate:
                return str(candidate)
    return None


def _opening_hours(item: dict[str, Any]) -> str | None:
    """HERE returns ``openingHours`` as a list of objects, each with a ``text`` list. A single object is tolerated."""
    opening = item.get("openingHours")
    entries = opening if isinstance(opening, list) else [opening] if isinstance(opening, dict) else []
    lines: list[str] = []
    for entry in entries:
        text = entry.get("text") if isinstance(entry, dict) else None
        if isinstance(text, str):
            lines.append(text)
        elif isinstance(text, list):
            lines.extend(str(value) for value in text if value)
    return "; ".join(lines[:7]) or None


def _https_url(value: Any) -> str | None:
    """A plain https URL, or ``None``. Provider links are never rendered as anything else."""
    if not isinstance(value, str) or len(value) > 600 or re.search(r"\s|[\x00-\x1f]", value):
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    return value if parts.scheme == "https" and parts.netloc else None


def _media_photo(item: dict[str, Any]) -> dict[str, str] | None:
    """The first usable photo HERE attaches to a place: a 150 px square for lists and a 550 px image for heroes.

    The multi-megabyte ``original`` variant is deliberately never used.
    """
    media = item.get("media")
    images = (media.get("images") or {}).get("items") if isinstance(media, dict) and isinstance(media.get("images"), dict) else None
    for entry in images if isinstance(images, list) else []:
        if not isinstance(entry, dict):
            continue
        variants = entry.get("variants") if isinstance(entry.get("variants"), dict) else {}

        def variant(name: str) -> str | None:
            found = variants.get(name)
            return _https_url(found.get("href")) if isinstance(found, dict) else None

        small = variant("small") or variant("medium") or _https_url(entry.get("href"))
        large = variant("large") or variant("medium") or _https_url(entry.get("href"))
        if small or large:
            supplier = entry.get("supplier")
            return {"small": small or large or "", "large": large or small or "",
                    "supplier": str(supplier.get("id") if isinstance(supplier, dict) else "") or "tripadvisor"}
    return None


def _media_rating(item: dict[str, Any]) -> dict[str, Any] | None:
    media = item.get("media")
    ratings = (media.get("ratings") or {}).get("items") if isinstance(media, dict) and isinstance(media.get("ratings"), dict) else None
    for entry in ratings if isinstance(ratings, list) else []:
        if not isinstance(entry, dict):
            continue
        try:
            average = float(entry.get("average"))
            count = int(entry.get("count") or 0)
        except (TypeError, ValueError):
            continue
        if math.isfinite(average) and 0 < average <= 5 and count >= 0:
            supplier = entry.get("supplier")
            return {"average": round(average, 1), "count": count, "url": _https_url(entry.get("href")),
                    "supplier": str(supplier.get("id") if isinstance(supplier, dict) else "") or "tripadvisor"}
    return None


def _open_now(item: dict[str, Any]) -> bool | None:
    """HERE's own open-now flag (computed in the place's time zone), when it supplies one."""
    opening = item.get("openingHours")
    entries = opening if isinstance(opening, list) else [opening] if isinstance(opening, dict) else []
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("isOpen"), bool):
            return entry["isOpen"]
    return None


def _valid_position(item: Any) -> tuple[float, float] | None:
    if not isinstance(item, dict):
        return None
    position = item.get("position") or {}
    if not isinstance(position, dict):
        return None
    try:
        latitude, longitude = float(position.get("lat")), float(position.get("lng"))
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(latitude) and math.isfinite(longitude) and -90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    return latitude, longitude


def _normalize_item(item: Any, category: str | None = None, search_query: str | None = None) -> dict[str, Any] | None:
    """One HERE place in the app's shape, or ``None`` if it has no usable position. Odd field types are tolerated."""
    point = _valid_position(item)
    if point is None:
        return None
    address = item.get("address") if isinstance(item.get("address"), dict) else {}
    categories_raw = item.get("categories") or []
    categories = categories_raw if isinstance(categories_raw, list) else []
    category_name = category
    first_category = categories[0] if categories else None
    if not category_name and isinstance(first_category, dict):
        category_name = str(first_category.get("name") or "place").lower().replace(" ", "_")

    rating = item.get("rating")
    if isinstance(rating, dict):
        rating = rating.get("value") or rating.get("score")
    elif isinstance(rating, list):
        rating = None
    media_rating = _media_rating(item)
    if media_rating:
        rating = media_rating["average"]

    category_ids = [str(c["id"]) for c in categories if isinstance(c, dict) and c.get("id")]
    primary = next((c for c in categories if isinstance(c, dict) and c.get("primary")), first_category if isinstance(first_category, dict) else None)

    return {
        "osm_id": None,
        "osm_type": "here",
        "here_id": item.get("id"),
        "name": item.get("title") or address.get("label") or "Place",
        "category": category_name or "place",
        "latitude": point[0],
        "longitude": point[1],
        "operator": None,
        "cuisine": None,
        "website": _first_contact(item, "www"),
        "phone": _first_contact(item, "phone"),
        "opening_hours": _opening_hours(item),
        "address": address.get("label") or address.get("street") or address.get("district"),
        "city": address.get("city"),
        "county": address.get("county"),
        "state": address.get("state"),
        "state_code": address.get("stateCode"),
        "country": address.get("countryName"),
        "rating": rating,
        "rating_count": media_rating["count"] if media_rating else None,
        "rating_url": media_rating["url"] if media_rating else None,
        "rating_source": media_rating["supplier"] if media_rating else None,
        "photo": _media_photo(item),
        "open_now": _open_now(item),
        "here_categories": [str(c.get("name")) for c in categories if isinstance(c, dict) and c.get("name")],
        "here_category_ids": category_ids,
        "primary_category_id": str(primary["id"]) if isinstance(primary, dict) and primary.get("id") else None,
        "here_category": str(primary["name"]) if isinstance(primary, dict) and primary.get("name") else None,
        "category_validated": False,
        "search_query": search_query,
        "source": "HERE Search API",
    }


# ---------------------------------------------------------------------------------------------
# Geocoding
# ---------------------------------------------------------------------------------------------
def _normalize_geocode_item(item: dict[str, Any], query: str) -> dict[str, Any] | None:
    point = _valid_position(item)
    if point is None:
        return None
    address = item.get("address") or {}
    scoring = item.get("scoring") or {}
    query_score = scoring.get("queryScore")
    return {
        "display_name": address.get("label") or item.get("title") or query,
        "latitude": point[0],
        "longitude": point[1],
        "type": item.get("resultType"),
        "result_type": item.get("resultType"),
        "locality_type": item.get("localityType"),
        "admin_type": item.get("administrativeAreaType"),
        "map_view": valid_bbox(item.get("mapView")),
        "query_score": float(query_score) if isinstance(query_score, (int, float)) else None,
        "city": address.get("city") or address.get("district"),
        "county": address.get("county"),
        "state": address.get("state"),
        "state_code": address.get("stateCode"),
        "country": address.get("countryName"),
        "here_id": item.get("id"),
        "source": "HERE Geocoding & Search API",
    }


def _looks_like_area_query(query: str) -> bool:
    """A short name with no street/number is a city or region request, not a business lookup."""
    text = query.strip()
    if any(ch.isdigit() for ch in text) or len(text.split()) > 4:
        return False
    lowered = text.lower()
    return not any(word in lowered.split() for word in ("road", "rd", "street", "st", "nagar", "colony", "mall", "hotel", "station", "airport", "temple"))


def rank_geocode_results(results: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """Prefer cities/areas for city-like queries. HERE's own order is the tie-breaker."""
    if not results:
        return results
    indexed = list(enumerate(results))
    if _looks_like_area_query(query):
        indexed.sort(key=lambda pair: (_AREA_RANK.get(str(pair[1].get("result_type")), 6), pair[0]))
    return [item for _, item in indexed]


async def search_places(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """Geocode ``query``. Returns ``[]`` when HERE finds nothing; raises AppError when HERE fails."""
    key = _require_key()
    query = query.strip()
    if not query:
        return []
    limit = max(1, min(int(limit), 20))
    base = {"q": query, "limit": max(limit, 5), "apiKey": key, "in": "countryCode:IND"}

    def parse(data: Any) -> list[dict[str, Any]]:
        items = data.get("items") if isinstance(data, dict) else None
        normalized = [_normalize_geocode_item(item, query) for item in (items or []) if isinstance(item, dict)]
        return [item for item in normalized if item]

    results = parse(await _get_json(HERE_GEOCODE_URL, base, timeout=15.0, provider="HERE Geocoding"))
    if not results:
        fallback = dict(base)
        fallback.pop("in", None)
        results = parse(await _get_json(HERE_GEOCODE_URL, fallback, timeout=15.0, provider="HERE Geocoding"))
    return rank_geocode_results(results, query)[:limit]


# ---------------------------------------------------------------------------------------------
# Named places ("Kempegowda International Airport", "Puri Jagannath Temple")
# ---------------------------------------------------------------------------------------------
# HERE's address geocoder answers a place name with streets that contain it (four "Kempegowda International Airport
# Road" entries) or with unrelated towns that share a word. /discover is the POI search: it returns the place itself.
_NAME_STOPWORDS = frozenset("the a an of in at on and near to from by for".split())
INDIA_CENTRE = (20.5937, 78.9629)
MIN_NAME_MATCH = 0.6


def _name_tokens(text: Any) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", str(text or "").casefold()) if t not in _NAME_STOPWORDS}


def name_match_score(query: str, title: str, *context: Any) -> float:
    """How much of ``query`` is accounted for by a result: its title first, then where it is (city, district, state).

    "Puri Jagannath Temple" scores 1.0 against a "Shri Jagannath Temple" in Puri (the title covers two words, the
    address the third) and 0.67 against the same temple name in another town.
    """
    wanted = _name_tokens(query)
    if not wanted:
        return 0.0
    in_title = _name_tokens(title)
    around = set().union(*(_name_tokens(c) for c in context)) if context else set()
    return len(wanted & (in_title | around)) / len(wanted)


async def find_named_places(query: str, near: tuple[float, float] | None = None, limit: int = 6) -> list[dict[str, Any]]:
    """HERE /discover for one specific place, best match first. Cities and regions are returned as ``kind='area'``.

    Each result carries ``match`` (see :func:`name_match_score`). Places come with Tripadvisor photo and rating
    attributes when HERE supplies them. Nothing is guessed: no result means HERE has no such place.
    """
    query = query.strip()
    if not query:
        return []
    key = _require_key()
    lat, lon = near if near else INDIA_CENTRE
    params: dict[str, Any] = {"q": query, "at": f"{lat},{lon}", "limit": max(1, min(int(limit), 10)), "apiKey": key}
    if not near:
        params["in"] = "countryCode:IND"
    data = await _get_places_json(HERE_DISCOVER_URL, params, rich=True)
    items = data.get("items") if isinstance(data, dict) else data if isinstance(data, list) else []
    results: list[dict[str, Any]] = []
    for order, item in enumerate(items or []):
        if not isinstance(item, dict) or _valid_position(item) is None:
            continue
        address = item.get("address") if isinstance(item.get("address"), dict) else {}
        where = (address.get("city"), address.get("district"), address.get("county"), address.get("state"))
        score = name_match_score(query, item.get("title") or "", *where)
        kind = item.get("resultType")
        if kind in _AREA_TYPES:
            candidate = _normalize_geocode_item(item, query)
            if candidate:
                results.append({"kind": "area", "match": score, "order": order, "area_candidate": candidate})
        elif kind == "place":
            place = _normalize_item(item, None, query)
            if place:
                place["category_validated"] = False
                results.append({"kind": "place", "match": score, "order": order, "place": place})
    return results


def detect_ambiguity(candidates: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """Candidates the user should choose between, or ``[]`` when the best match is clear.

    Ambiguous only when the top results are equally good cities/areas in different states that are
    far apart, and the query does not already name a state or country.
    """
    if len(candidates) < 2 or "," in query:
        return []
    areas = [c for c in candidates if c.get("result_type") in _AREA_TYPES]
    if len(areas) < 2:
        return []
    first = areas[0]
    first_score = first.get("query_score")
    lowered = query.lower()
    if first.get("state") and str(first["state"]).lower() in lowered:
        return []
    close = [first]
    for other in areas[1:4]:
        if other.get("state") == first.get("state") or other.get("country") != first.get("country"):
            continue
        other_score = other.get("query_score")
        if first_score is None or other_score is None or other_score < max(first_score - 0.02, 0.9):
            continue
        if haversine_distance_km(first["latitude"], first["longitude"], other["latitude"], other["longitude"]) < 50:
            continue
        close.append(other)
    return close if len(close) > 1 else []


# ---------------------------------------------------------------------------------------------
# Places / Discover
# ---------------------------------------------------------------------------------------------
async def _discover_at(
    lat: float,
    lon: float,
    query: str,
    limit: int = 50,
    restrict_to_india: bool = True,
    radius_km: float | None = None,
    rich: bool = False,
) -> list[dict[str, Any]]:
    """Discover places. With ``radius_km`` the search is a hard ``in=circle`` filter around the point."""
    key = _require_key()
    params: dict[str, Any] = {"q": query, "limit": max(1, min(limit, 100)), "apiKey": key}
    if radius_km is not None and radius_km > 0:
        meters = int(min(max(radius_km, 0.1), 100) * 1000)
        params["in"] = f"circle:{lat},{lon};r={meters}"
    else:
        params["at"] = f"{lat},{lon}"
        if restrict_to_india:
            params["in"] = "countryCode:IND"
    try:
        data = await _get_places_json(HERE_DISCOVER_URL, params, rich=rich)
    except AppError as exc:
        # A rejected circle filter must not take the whole feature down: retry once biased by `at`.
        if "in" in params and str(params["in"]).startswith("circle:") and exc.code == "provider_unavailable":
            logger.warning("event=here_circle_retry_with_at")
            params.pop("in")
            params["at"] = f"{lat},{lon}"
            data = await _get_places_json(HERE_DISCOVER_URL, params, rich=rich)
        else:
            raise
    # HERE normally returns an object with an `items` array. Keep the integration defensive.
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("items") or []
    else:
        items = []
    return [item for item in items if _valid_position(item) is not None]


async def _browse(
    lat: float,
    lon: float,
    spec: CategorySpec,
    *,
    radius_km: float | None = None,
    bbox: dict[str, float] | None = None,
    limit: int = BROWSE_PAGE_LIMIT,
    rich: bool = False,
) -> list[dict[str, Any]]:
    """HERE /browse: places carrying one of ``spec``'s category IDs, nearest to ``(lat, lon)`` first.

    ``in`` is either a circle around the point or a bounding box; HERE applies it as a hard filter, so the
    results can be trusted to lie inside it (the callers still re-check the distance themselves).
    """
    key = _require_key()
    params: dict[str, Any] = {
        "at": f"{lat},{lon}", "categories": browse_categories_param(spec),
        "limit": max(1, min(int(limit), BROWSE_PAGE_LIMIT)), "apiKey": key,
    }
    if bbox is not None:
        params["in"] = bbox_param(bbox)
    elif radius_km is not None and radius_km > 0:
        params["in"] = f"circle:{lat},{lon};r={int(min(max(radius_km, 0.05), 250) * 1000)}"
    data = await _get_places_json(HERE_BROWSE_URL, params, rich=rich)
    items = data if isinstance(data, list) else data.get("items") if isinstance(data, dict) else []
    return [item for item in items or [] if _valid_position(item) is not None]


def validate_places(raw_items: list[Any], spec: CategorySpec, search_query: str | None = None) -> tuple[list[dict[str, Any]], int]:
    """Normalise HERE places and keep only those HERE itself classifies under ``spec``.

    Returns (kept, rejected). A place is never relabelled as the requested category because its *name* contains a
    matching word: only its HERE category IDs count.
    """
    kept: list[dict[str, Any]] = []
    rejected = 0
    for item in raw_items:
        place = _normalize_item(item, spec.key, search_query)
        if place is None:
            continue
        tier = classify(spec, place["here_category_ids"], place["primary_category_id"])
        if tier is None:
            rejected += 1
            continue
        place["category_validated"] = True
        place["relevance"] = tier
        kept.append(place)
    return kept, rejected


def _rank_key(place: dict[str, Any], distance_field: str) -> tuple[int, float]:
    raw = place.get(distance_field)
    return int(place.get("relevance", 0)), float(raw) if raw is not None else 999999.0


def _dedupe_places(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate HERE results, preserving the copy nearest to its reference."""
    selected: dict[str, dict[str, Any]] = {}
    for item in items:
        key = str(item.get("here_id") or f"{item.get('latitude', 0):.5f}:{item.get('longitude', 0):.5f}:{item.get('name', '')}").lower()
        existing = selected.get(key)
        current_raw_distance = item.get("distance_from_reference_km", item.get("distance_from_route_km"))
        existing_raw_distance = (existing or {}).get("distance_from_reference_km", (existing or {}).get("distance_from_route_km"))
        current_distance = float(current_raw_distance) if current_raw_distance is not None else 999999.0
        existing_distance = float(existing_raw_distance) if existing_raw_distance is not None else 999999.0
        if existing is None or current_distance < existing_distance:
            selected[key] = item
    return list(selected.values())


def _query_for(category: str, search_query: str | None) -> str:
    if search_query and search_query.strip():
        return search_query.strip()
    return CATEGORY_QUERY.get(category, category.replace("_", " "))


async def find_nearby_places(
    points: list[dict[str, float]],
    category: str,
    radius_km: float,
    limit: int = 80,
    search_query: str | None = None,
    stats: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Places within ``radius_km`` of each point, best match first, then nearest.

    For a category with verified HERE IDs and a query that names nothing but the category, HERE /browse is used with
    the category filter. If the query says more ("pizza", a brand) the text search is used and its results are still
    checked against the category. The radius is enforced twice: by HERE's circle filter and by an exact check.
    """
    stats = stats if stats is not None else {}
    spec = spec_for(category)
    generic = spec is not None and is_generic_query(category, search_query)
    stats.update({
        "method": "browse" if generic else "discover", "category_filtered": spec is not None,
        "category_ids": browse_categories_param(spec) if spec else None, "rejected": 0, "requests": 0, "page_full": False,
    })
    requested_limit = max(1, min(int(limit), 100))
    all_items: list[dict[str, Any]] = []

    for point in points:
        latitude = float(point["latitude"])
        longitude = float(point["longitude"])
        point_items: list[dict[str, Any]] = []

        def absorb(candidates: list[dict[str, Any]]) -> None:
            for place in candidates:
                distance = haversine_distance_km(latitude, longitude, place["latitude"], place["longitude"])
                if distance <= radius_km:
                    place["distance_from_reference_km"] = round(distance, 3)
                    point_items.append(place)

        if generic:
            raw = await _browse(latitude, longitude, spec, radius_km=radius_km, rich=True)
            stats["requests"] += 1
            stats["page_full"] = stats["page_full"] or len(raw) >= BROWSE_PAGE_LIMIT
            candidates, rejected = validate_places(raw, spec, None)
            stats["rejected"] += rejected
            absorb(candidates)
        else:
            query = _query_for(category, search_query)
            terms = [query]
            for fallback in CATEGORY_QUERY_FALLBACKS.get(category, []):
                if fallback.casefold() not in {term.casefold() for term in terms}:
                    terms.append(fallback)
            for term in terms:
                raw = await _discover_at(latitude, longitude, term, min(requested_limit * 3, 100), radius_km=radius_km, rich=True)
                stats["requests"] += 1
                if spec is not None:
                    candidates, rejected = validate_places(raw, spec, term)
                    stats["rejected"] += rejected
                else:
                    candidates = [n for n in (_normalize_item(item, category, term) for item in raw) if n is not None]
                absorb(candidates)
                if len(_dedupe_places(point_items)) >= requested_limit:
                    break
        all_items.extend(point_items)

    all_items = _dedupe_places(all_items)
    all_items.sort(key=lambda item: _rank_key(item, "distance_from_reference_km"))
    stats["matched"] = len(all_items)
    return all_items[:requested_limit]


# ---------------------------------------------------------------------------------------------
# Destination highlights (travel guides)
# ---------------------------------------------------------------------------------------------
# Browse lists the *nearest* places, which in a town centre means neighbourhood parks, so a guide uses Discover, which
# ranks by relevance, over a few attraction-flavoured queries and merges them. Primary category groups below were read
# from live HERE responses: 300-3000 Landmark-Attraction, 300-3100 Museum, 300-3200 Religious Place, 350-3500 Lake,
# 550-5510 Park-Garden-Scenic Point, 550-5520 Amusement Park.
GUIDE_QUERIES = ("tourist attraction", "garden", "viewpoint", "lake", "museum", "temple")
GUIDE_PRIMARY_PREFIXES = ("300-3000", "300-3100", "300-3200", "350-3500", "550-5510", "550-5520")
GUIDE_DEFAULT_RADIUS_KM = 25.0
GUIDE_CONCURRENCY = 4


def _name_key(name: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name or "").casefold())


# How much each signal counts when choosing which places a guide may talk about. None of them is "popularity" on its own:
# Tripadvisor review counts are missing for many well-known places (a hill-top viewpoint can have none), so HERE's own
# relevance order for the query and HERE's category carry weight too. The model later picks from this shortlist.
_QUERY_WEIGHT = {"tourist attraction": 1.0, "viewpoint": 0.8, "garden": 0.6, "lake": 0.6, "museum": 0.5, "temple": 0.4}
_CATEGORY_BONUS = (("300-3000-0023", 0.7), ("300-3000-0025", 0.4), ("550-5510-0242", 0.5), ("550-5510-0204", 0.3),
                   ("550-5520", 0.3), ("350-3500", 0.3), ("300-3100", 0.2))
_RANK_SPAN = 30.0


def _highlight_score(place: dict[str, Any]) -> float:
    reviews = min(1.0, math.log10(1 + int(place.get("rating_count") or 0)) / 3.5) * 3.0
    ranked = max((_QUERY_WEIGHT.get(term, 0.4) * max(0.0, (_RANK_SPAN - rank) / _RANK_SPAN) for term, rank in place.get("_ranks", {}).items()), default=0.0) * 2.0
    primary = str(place.get("primary_category_id") or "")
    bonus = next((value for prefix, value in _CATEGORY_BONUS if primary.startswith(prefix)), 0.0)
    return reviews + ranked + bonus


async def find_destination_highlights(
    latitude: float,
    longitude: float,
    radius_km: float = GUIDE_DEFAULT_RADIUS_KM,
    limit: int = 40,
    stats: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """A shortlist of the notable places around a destination: HERE attractions, parks, lakes, museums and temples.

    Only places HERE itself classifies in an attraction group are kept. They are scored by Tripadvisor review count,
    by where HERE ranked them for each query, and by HERE category (see ``_highlight_score``), best first.
    """
    stats = stats if stats is not None else {}
    stats.update({"method": "discover", "queries": len(GUIDE_QUERIES), "requests": 0, "failed_requests": 0, "rejected": 0})
    radius_km = max(2.0, min(float(radius_km), 100.0))
    gate = asyncio.Semaphore(GUIDE_CONCURRENCY)

    async def run(term: str) -> list[dict[str, Any]] | AppError:
        async with gate:
            try:
                return await _discover_at(latitude, longitude, term, 30, restrict_to_india=False, radius_km=radius_km, rich=True)
            except AppError as exc:
                return exc

    batches = await asyncio.gather(*(run(term) for term in GUIDE_QUERIES))
    stats["requests"] = len(batches)
    failures = [b for b in batches if isinstance(b, AppError)]
    stats["failed_requests"] = len(failures)
    if failures and len(failures) == len(batches):
        raise failures[0]

    unique: list[dict[str, Any]] = []
    for term, batch in zip(GUIDE_QUERIES, batches):
        if isinstance(batch, AppError):
            continue
        for rank, item in enumerate(batch):
            place = _normalize_item(item, "tourist_attraction", term)
            if place is None:
                continue
            if not str(place["primary_category_id"] or "").startswith(GUIDE_PRIMARY_PREFIXES):
                stats["rejected"] += 1
                continue
            distance = haversine_distance_km(latitude, longitude, place["latitude"], place["longitude"])
            if distance > radius_km:
                continue
            place["category_validated"] = True
            place["distance_from_reference_km"] = round(distance, 3)
            place["_ranks"] = {term: rank}
            # One record per real place: by HERE ID, then by name within 200 m (HERE can hold two records for one garden).
            twin = next((u for u in unique if (u["here_id"] and u["here_id"] == place["here_id"]) or (
                _name_key(u["name"]) == _name_key(place["name"])
                and haversine_distance_km(u["latitude"], u["longitude"], place["latitude"], place["longitude"]) < 0.2)), None)
            if twin is None:
                unique.append(place)
                continue
            twin["_ranks"][term] = min(rank, twin["_ranks"].get(term, rank))
            if int(place.get("rating_count") or 0) > int(twin.get("rating_count") or 0):
                ranks = twin["_ranks"]
                twin.update(place)  # the richer record (photo, rating) replaces the bare one
                twin["_ranks"] = ranks

    for place in unique:
        place["relevance"] = 0
    unique.sort(key=lambda p: (-_highlight_score(p), p["distance_from_reference_km"]))
    for place in unique:
        place.pop("_ranks", None)
    stats.update({"matched": len(unique), "with_photos": sum(1 for p in unique if p.get("photo")),
                  "with_ratings": sum(1 for p in unique if p.get("rating") is not None), "radius_km": radius_km})
    return unique[: max(1, min(int(limit), 40))]


def _route_length_km(coordinates: list[list[float]]) -> float:
    total = 0.0
    for a, b in zip(coordinates, coordinates[1:]):
        total += haversine_distance_km(a[1], a[0], b[1], b[0])
    return total


def sample_route_points(coordinates: list[list[float]], spacing_km: float, max_points: int = MAX_ROUTE_SAMPLES) -> list[list[float]]:
    """Points spaced evenly *by distance* along the route (always including both ends)."""
    if len(coordinates) < 2:
        return list(coordinates)
    total = _route_length_km(coordinates)
    if total <= 0:
        return [coordinates[0]]
    count = max(2, min(max_points, int(math.ceil(total / max(spacing_km, 1.0))) + 1))
    targets = [total * i / (count - 1) for i in range(count)]
    samples: list[list[float]] = []
    travelled, ti = 0.0, 0
    for a, b in zip(coordinates, coordinates[1:]):
        segment = haversine_distance_km(a[1], a[0], b[1], b[0])
        while ti < len(targets) and travelled + segment >= targets[ti] - 1e-9:
            fraction = 0.0 if segment == 0 else (targets[ti] - travelled) / segment
            samples.append([a[0] + (b[0] - a[0]) * fraction, a[1] + (b[1] - a[1]) * fraction])
            ti += 1
        travelled += segment
    while len(samples) < count:
        samples.append(list(coordinates[-1]))
    return samples


async def find_route_places(
    route_geometry: dict[str, Any],
    category: str,
    corridor_km: float,
    limit: int = 80,
    search_query: str | None = None,
    coverage: dict[str, Any] | None = None,
    stats: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Places within ``corridor_km`` of the route line.

    HERE is queried in circles centred on points spaced along the route (category-filtered /browse where the category
    has verified IDs). Provider calls are capped at ``MAX_ROUTE_SAMPLES``; if a long route needs more, or a circle
    comes back full, ``coverage['complete']`` is False so the caller says the search was partial.
    Best category matches are kept first and then shown in route order.
    """
    coordinates = route_geometry.get("coordinates") or []
    if len(coordinates) < 2:
        raise AppError("needs_trip", "There is no route to search along yet.")
    stats = stats if stats is not None else {}
    spec = spec_for(category)
    generic = spec is not None and is_generic_query(category, search_query)
    query = _query_for(category, search_query)
    radius_km = min(max(corridor_km * 3.0, 5.0), 25.0)
    total_km = _route_length_km(coordinates)
    samples = sample_route_points(coordinates, spacing_km=radius_km * 1.6)
    needed = int(math.ceil(total_km / (radius_km * 1.6))) + 1
    stats.update({
        "method": "browse" if generic else "discover", "category_filtered": spec is not None,
        "category_ids": browse_categories_param(spec) if spec else None, "rejected": 0, "requests": len(samples), "page_full": False,
    })
    if coverage is not None:
        coverage.update({
            "route_km": round(total_km, 1), "sampled_points": len(samples),
            "search_radius_km": round(radius_km, 1), "complete": needed <= MAX_ROUTE_SAMPLES,
            "sample_cap_reached": needed > MAX_ROUTE_SAMPLES,
        })

    semaphore = asyncio.Semaphore(ROUTE_SEARCH_CONCURRENCY)

    async def one(sample: list[float]) -> list[dict[str, Any]] | AppError:
        async with semaphore:
            try:
                if generic:
                    return await _browse(float(sample[1]), float(sample[0]), spec, radius_km=radius_km)
                return await _discover_at(float(sample[1]), float(sample[0]), query, min(100, max(limit, 50)), radius_km=radius_km)
            except AppError as exc:
                return exc

    batches = await asyncio.gather(*(one(sample) for sample in samples))
    failures = [b for b in batches if isinstance(b, AppError)]
    if failures and len(failures) == len(batches):
        raise failures[0]
    if failures and coverage is not None:
        coverage["complete"] = False
        coverage["failed_points"] = len(failures)
    good = [batch for batch in batches if not isinstance(batch, AppError)]
    full = sum(1 for batch in good if len(batch) >= BROWSE_PAGE_LIMIT)
    if full:
        stats["page_full"] = True
        if coverage is not None:
            coverage["complete"] = False
            coverage["full_pages"] = full
    raw_items = [item for batch in good for item in batch]

    if spec is not None:
        normalized, rejected = validate_places(raw_items, spec, query if not generic else None)
        stats["rejected"] = rejected
    else:
        normalized = [n for n in (_normalize_item(item, category, query) for item in raw_items) if n is not None]
    normalized = _dedupe_places(normalized)
    route_coords = [list(pair[:2]) for pair in coordinates]
    filtered: list[dict[str, Any]] = []
    for item in normalized:
        distance = calculate_distance_to_route_km(route_coords, item["latitude"], item["longitude"])
        if distance is None or distance > corridor_km:
            continue
        item["distance_from_route_km"] = distance
        item["route_position_percent"] = calculate_route_position_percent(route_coords, item["latitude"], item["longitude"])
        filtered.append(item)
    stats["matched"] = len(filtered)
    best = sorted(filtered, key=lambda item: _rank_key(item, "distance_from_route_km"))[:limit]
    best.sort(key=lambda item: float(item.get("route_position_percent") or 0))
    return best


def _same(a: Any, b: Any) -> bool:
    return bool(a) and bool(b) and str(a).strip().casefold() == str(b).strip().casefold()


def place_in_area(place: dict[str, Any], area: dict[str, Any]) -> bool:
    """Is ``place`` inside the administrative area? HERE gives a bounding box, not a boundary, so the box is only
    a way to find candidates: states and districts are decided by the place's own address fields.
    Cities have no such field to compare, so for them the (approximate) box is the definition."""
    kind = area.get("kind")
    if kind == "state":
        return _same(place.get("state"), area.get("state")) or (bool(area.get("state_code")) and place.get("state_code") == area.get("state_code"))
    if kind == "district":
        return _same(place.get("county"), area.get("county")) and _same(place.get("state"), area.get("state"))
    return True


async def find_area_places(
    area: dict[str, Any],
    category: str,
    limit: int = 80,
    search_query: str | None = None,
    stats: dict[str, Any] | None = None,
    rich: bool = False,
) -> list[dict[str, Any]]:
    """Places of ``category`` inside a state, district or city, for "airports in Odisha".

    Strategy: take the area's HERE bounding box, cut it into tiles HERE can serve (<= 250 km from the tile centre),
    run a category-filtered /browse per tile, split any tile that returns a full page of 100, merge by HERE place ID,
    then keep only places whose own address is in the area (the box of a state also holds its neighbours' places).
    ``stats['complete']`` is False if the request budget ran out, a tile stayed full at the finest split, or a request
    failed, so callers never present a possibly partial list as exhaustive.
    """
    stats = stats if stats is not None else {}
    spec = spec_for(category)
    if spec is None:
        raise AppError("invalid_request", "I can list a whole area only for types of place that HERE classifies precisely, such as airports, hospitals, hotels, restaurants, cafes and petrol pumps. Try “near <place>” instead.")
    bbox = valid_bbox(area.get("bbox"))
    if bbox is None:
        raise AppError("place_not_found", f"I couldn't work out the extent of {area.get('name') or 'that area'}.")
    tiles = tile_bbox(bbox, MAX_TILE_SIDE_KM)
    if len(tiles) > MAX_AREA_TILES:
        raise AppError("invalid_request", f"{area.get('name') or 'That area'} is too large to list in one search. Try a state, a district or a city.")

    semaphore = asyncio.Semaphore(AREA_CONCURRENCY)

    async def one(tile: dict[str, float]) -> list[dict[str, Any]]:
        latitude, longitude = bbox_center(tile)
        async with semaphore:
            if rich:  # only a ranked search asks for ratings; every other area search is exactly as before
                return await _browse(latitude, longitude, spec, bbox=tile, limit=BROWSE_PAGE_LIMIT, rich=True)
            return await _browse(latitude, longitude, spec, bbox=tile, limit=BROWSE_PAGE_LIMIT)

    pending: list[tuple[dict[str, float], int]] = [(tile, 0) for tile in tiles]
    raw_items: list[dict[str, Any]] = []
    requests = failed = unresolved_full = 0
    first_error: Exception | None = None
    while pending and requests < MAX_AREA_REQUESTS:
        room = MAX_AREA_REQUESTS - requests
        batch, pending = pending[:room], pending[room:]
        outcomes = await asyncio.gather(*(one(tile) for tile, _ in batch), return_exceptions=True)
        requests += len(batch)
        for (tile, depth), outcome in zip(batch, outcomes):
            if isinstance(outcome, BaseException):
                failed += 1
                first_error = first_error or outcome
                continue
            raw_items.extend(outcome)
            if len(outcome) >= BROWSE_PAGE_LIMIT:
                if depth < MAX_AREA_SPLIT_DEPTH:
                    pending.extend((quadrant, depth + 1) for quadrant in split_bbox(tile))
                else:
                    unresolved_full += 1
    budget_exhausted = bool(pending)
    if failed and failed == requests and first_error is not None:
        raise first_error if isinstance(first_error, AppError) else AppError("provider_unavailable", detail=type(first_error).__name__)

    candidates, rejected = validate_places(raw_items, spec, None)
    candidates = _dedupe_places(candidates)
    inside = [place for place in candidates if place_in_area(place, area)]
    center_lat, center_lon = bbox_center(bbox)
    inside.sort(key=lambda p: (int(p.get("relevance", 0)), round(haversine_distance_km(center_lat, center_lon, p["latitude"], p["longitude"]), 1),
                               str(p.get("name") or "").casefold()))
    stats.update({
        "method": "browse", "category_filtered": True, "category_ids": browse_categories_param(spec),
        "rejected": rejected, "outside_area": len(candidates) - len(inside), "matched": len(inside),
        "requests": requests, "tiles": len(tiles), "failed_requests": failed,
        "unresolved_full_tiles": unresolved_full + len(pending), "budget_exhausted": budget_exhausted,
        "complete": not (failed or unresolved_full or budget_exhausted),
        "area_width_km": round(bbox_size_km(bbox)[0]), "area_height_km": round(bbox_size_km(bbox)[1]),
    })
    return inside[: max(1, min(int(limit), 100))]


async def search_here_ev_chargers(lat: float, lon: float, radius_km: float = 25.0, limit: int = 50) -> list[dict[str, Any]]:
    # Keep EV charging discovery explicitly separate from generic Places: HERE category 700-7600-0322 only.
    spec = spec_for("ev_charging")
    assert spec is not None
    raw = await _browse(lat, lon, spec, radius_km=radius_km, limit=min(limit, BROWSE_PAGE_LIMIT))
    kept, _ = validate_places(raw, spec, "EV charging station")
    results: list[dict[str, Any]] = []
    for normalized in kept:
        distance = haversine_distance_km(lat, lon, normalized["latitude"], normalized["longitude"])
        if distance > radius_km:
            continue
        normalized.update({
            "id": f"HERE:{normalized.get('here_id') or normalized['latitude']}_{normalized['longitude']}",
            "uid": normalized.get("here_id"),
            "chargerType": "HERE EV charging station",
            "cpoName": None,
            "socket_type": "See HERE station details",
            "charging_speed": "See HERE station details",
            "estimated_power_kw": None,
            "source": "HERE EV Charging Search",
            "distance_from_reference_km": round(distance, 3),
        })
        results.append(normalized)
    results.sort(key=lambda item: float(item.get("distance_from_reference_km") or 999999))
    return results[:limit]
