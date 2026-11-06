"""Category-aware, scope-aware place search. Everything external (HERE, AWS) is faked; nothing here is billable.

Category IDs used below are the ones documented by HERE's Places category system and confirmed against live
responses: 400-4000-4581 Airport, 400-4000-4582 Airport Terminal, 500-5000-0053 Hotel, 100-1100-0010 Coffee Shop,
100-1000-0000 Restaurant, 700-7600-0116 Petrol/Gas Station, 700-7600-0322 EV Charging Station, 800-8000-0159 Hospital.
"""
from __future__ import annotations

import json

import pytest

import fakes
import services.orchestrator as orchestrator
from services import here_places, mcp_tools
from services.errors import AppError
from services.geo_utils import MAX_TILE_SIDE_KM, bbox_size_km, point_along_route, split_bbox, tile_bbox
from services.intent_parser import forbids_route_change, parse
from services.intents import validate_intent
from services.place_categories import SPECS, browse_categories_param, classify, extract_qualifiers, is_generic_query, spec_for
from services.trip_context import TripContext


def new_context():
    return TripContext(session_id="00000000-0000-4000-8000-000000000042")


async def say(context, text):
    return await orchestrator.handle_message(context, text)


def here_place(item_id, name, lat, lon, cat_id, cat_name, state="Odisha", city="Bhubaneswar", extra_cats=()):
    cats = [{"id": cat_id, "name": cat_name, "primary": True}, *[{"id": c, "name": n} for c, n in extra_cats]]
    return {"id": item_id, "title": name, "position": {"lat": lat, "lng": lon}, "categories": cats,
            "address": {"label": f"{name}, {city}, {state}", "city": city, "state": state, "stateCode": state[:2].upper(), "countryName": "India"}}


# ======================================================================================================
# Category catalogue: real HERE IDs, not invented ones
# ======================================================================================================
def test_required_categories_use_the_documented_here_category_ids():
    assert SPECS["airport"].include == ("400-4000-4581", "400-4000-4580")            # Airport, Public Sports Airport
    assert SPECS["hospital"].include == ("800-8000-0159",)                           # Hospital
    assert SPECS["medical"].include == ("800-8000",) and "800-8000-0162" in SPECS["medical"].exclude  # not vets
    assert SPECS["cafe"].include == ("100-1100",)                                    # Coffee-Tea
    assert SPECS["restaurant"].include == ("100-1000",)
    assert SPECS["hotel"].include == ("500-5000", "500-5100") and "500-5100-0056" in SPECS["hotel"].exclude  # not campgrounds
    assert SPECS["fuel"].include == ("700-7600",) and set(SPECS["fuel"].exclude) == {"700-7600-0322", "700-7600-0325"}
    assert SPECS["ev_charging"].include == ("700-7600-0322",)


def test_browse_categories_param_puts_excludes_after_includes_with_a_bang():
    assert browse_categories_param(SPECS["fuel"]) == "700-7600,!700-7600-0322,!700-7600-0325"
    assert browse_categories_param(SPECS["airport"]) == "400-4000-4581,400-4000-4580"
    assert browse_categories_param(SPECS["cafe"]) == "100-1100"


def test_unverified_categories_have_no_ids_and_so_are_never_claimed_to_be_checked():
    for key in ("supermarket", "car_wash", "tourist_attraction", "place"):
        assert spec_for(key) is None


@pytest.mark.parametrize("category,ids,primary,expected", [
    ("airport", ["400-4000-4581"], "400-4000-4581", True),
    ("airport", ["400-4000-4582"], "400-4000-4582", False),            # "Airport-Arrival" terminal entries are not airports
    ("airport", ["500-5000-0053"], "500-5000-0053", False),            # "Airport Hotel" is a hotel
    ("airport", [], None, False),                                       # no category data: cannot be called an airport
    ("hospital", ["800-8000-0159"], "800-8000-0159", True),
    ("hospital", ["800-8000-0325"], "800-8000-0325", False),           # the "-ER" duplicate entry
    ("hospital", ["800-8000-0158"], "800-8000-0158", False),           # a clinic is not a hospital
    ("medical", ["800-8000-0158"], "800-8000-0158", True),
    ("medical", ["800-8000-0162"], "800-8000-0162", False),            # veterinarian
    ("cafe", ["100-1100-0010"], "100-1100-0010", True),
    ("cafe", ["100-1000-0000"], "100-1000-0000", False),               # a restaurant named "Cafe ..." is still a restaurant
    ("hotel", ["500-5100-0056"], "500-5100-0056", False),              # campground
    ("hotel", ["500-5100-0057"], "500-5100-0057", True),               # guest house is lodging
    ("fuel", ["700-7600-0116"], "700-7600-0116", True),
    ("fuel", ["700-7600-0322"], "700-7600-0322", False),               # EV charging is not a petrol pump
    ("ev_charging", ["700-7600-0322"], "700-7600-0322", True),
])
def test_a_place_is_only_the_requested_category_if_here_classifies_it_that_way(category, ids, primary, expected):
    assert (classify(SPECS[category], ids, primary) is not None) is expected


def test_primary_category_matches_rank_ahead_and_preferred_cafe_types_come_first():
    cafe = SPECS["cafe"]
    coffee_shop = classify(cafe, ["100-1100-0010"], "100-1100-0010")
    tea_house = classify(cafe, ["100-1100-0331"], "100-1100-0331")
    assert coffee_shop < tea_house
    hotel = SPECS["hotel"]
    real_hotel = classify(hotel, ["500-5000-0053"], "500-5000-0053")
    restaurant_with_rooms = classify(hotel, ["100-1000-0000", "500-5000-0053"], "100-1000-0000")
    assert real_hotel < restaurant_with_rooms, "a restaurant that merely carries a hotel tag ranks after real hotels"


def test_original_query_is_kept_and_generic_wording_is_recognised():
    assert is_generic_query("airport", "the list of airports") and is_generic_query("fuel", "petrol pumps")
    assert is_generic_query("cafe", "a quiet place for a coffee break")
    assert not is_generic_query("restaurant", "pizza restaurants") and not is_generic_query("cafe", "Starbucks coffee")
    assert extract_qualifiers("a quiet, family-friendly cafe") == ["quiet", "family-friendly"]
    assert extract_qualifiers("my family and I want lunch") == []


# ======================================================================================================
# Geometry helpers
# ======================================================================================================
def test_route_midpoint_follows_the_route_not_the_end_points():
    straight = [[0.0, 0.0], [0.0, 2.0]]
    mid = point_along_route(straight, 0.5)
    assert mid["latitude"] == pytest.approx(1.0, abs=0.01) and mid["longitude"] == pytest.approx(0.0, abs=0.01)

    # An L-shaped route with two (almost) equal legs: the halfway point is the corner, not the average of the ends.
    bent = [[0.0, 0.0], [0.0, 1.0], [1.0, 1.0]]
    corner = point_along_route(bent, 0.5)
    assert corner["latitude"] == pytest.approx(1.0, abs=0.03) and corner["longitude"] == pytest.approx(0.0, abs=0.03)
    average_of_ends = (0.5, 0.5)
    assert abs(corner["latitude"] - average_of_ends[0]) > 0.3

    # Distance is cumulative along the line: a short first leg and a long second leg.
    uneven = [[0.0, 0.0], [0.0, 0.1], [0.0, 3.1]]
    third = point_along_route(uneven, 0.5)
    assert third["latitude"] == pytest.approx(1.55, abs=0.02)
    assert third["distance_from_start_km"] == pytest.approx(third["route_km"] / 2, rel=0.01)
    assert point_along_route([[1.0, 1.0]], 0.5) is None and point_along_route([[1.0, 1.0], [1.0, 1.0]], 0.5) is None


def test_bbox_tiles_stay_within_what_a_browse_request_can_serve():
    odisha = fakes.AREAS["Odisha"]["bbox"]
    tiles = tile_bbox(odisha)
    assert len(tiles) > 1
    for tile in tiles:
        width, height = bbox_size_km(tile)
        assert width <= MAX_TILE_SIDE_KM + 1 and height <= MAX_TILE_SIDE_KM + 1
    assert min(t["west"] for t in tiles) == odisha["west"] and max(t["east"] for t in tiles) == pytest.approx(odisha["east"])
    assert len(split_bbox(tiles[0])) == 4


# ======================================================================================================
# HERE layer: area search (fake /browse)
# ======================================================================================================
ODISHA = fakes.AREAS["Odisha"]


def inside(tile, item):
    p = item["position"]
    return tile["west"] <= p["lng"] <= tile["east"] and tile["south"] <= p["lat"] <= tile["north"]


@pytest.mark.asyncio
async def test_airports_in_odisha_are_category_filtered_state_checked_and_complete(monkeypatch):
    catalogue = [
        here_place("a1", "Biju Patnaik International Airport", 20.2444, 85.8178, "400-4000-4581", "Airport"),
        here_place("a1t", "Biju Patnaik International Airport-Arrival", 20.2440, 85.8180, "400-4000-4582", "Airport Terminal"),
        here_place("a2", "Veer Surendra Sai Airport", 21.9142, 84.0505, "400-4000-4581", "Airport", city="Jharsuguda"),
        here_place("a3", "Swami Vivekananda International Airport", 21.1800, 81.7400, "400-4000-4581", "Airport", state="Chhattisgarh", city="Naya Raipur"),
        here_place("h1", "Airport Hotel", 20.2500, 85.8200, "500-5000-0053", "Hotel"),
    ]
    seen = []

    async def fake_browse(lat, lon, spec, radius_km=None, bbox=None, limit=100):
        seen.append({"spec": spec.key, "bbox": bbox, "radius": radius_km, "center": (lat, lon)})
        return [item for item in catalogue if inside(bbox, item)]

    monkeypatch.setattr(here_places, "_browse", fake_browse)
    stats: dict = {}
    results = await here_places.find_area_places(ODISHA, "airport", 80, "the list of airports", stats=stats)

    assert {r["name"] for r in results} == {"Biju Patnaik International Airport", "Veer Surendra Sai Airport"}
    assert all(r["category"] == "airport" and r["category_validated"] and r["primary_category_id"] == "400-4000-4581" for r in results)
    assert all(call["radius"] is None and call["bbox"] is not None for call in seen), "a state is an area, never a point with a radius"
    assert stats["tiles"] == len(tile_bbox(ODISHA["bbox"])) == stats["requests"]
    assert stats["outside_area"] == 1, "the Chhattisgarh airport lies inside Odisha's bounding box but is not in Odisha"
    assert stats["rejected"] == 2, "the terminal entry and 'Airport Hotel' are not airports"
    assert stats["complete"] is True and stats["unresolved_full_tiles"] == 0


@pytest.mark.asyncio
async def test_a_full_page_splits_the_tile_and_overlapping_tiles_are_deduplicated_by_here_id(monkeypatch):
    calls = []

    async def fake_browse(lat, lon, spec, radius_km=None, bbox=None, limit=100):
        calls.append(bbox)
        width, _ = bbox_size_km(bbox)
        if width > 150:  # a big tile comes back full
            return [here_place(f"p{i}-{round(lat, 1)}", f"Place {i}", lat + i * 1e-4, lon, "100-1100-0010", "Coffee Shop") for i in range(100)]
        # small tiles all return the same shared place (an item on a tile edge)
        return [here_place("shared", "Edge Cafe", lat, lon, "100-1100-0010", "Coffee Shop")]

    monkeypatch.setattr(here_places, "_browse", fake_browse)
    medium = {"name": "Midland", "kind": "region", "bbox": {"west": 80.0, "south": 20.0, "east": 82.2, "north": 22.2}}
    stats: dict = {}
    results = await here_places.find_area_places(medium, "cafe", 100, stats=stats)
    assert stats["tiles"] == 1 and stats["requests"] == 5, "the one tile came back full, so it was split into four more requests"
    assert len({r["here_id"] for r in results}) == len(results), "results are merged by stable HERE ID"
    assert sum(1 for r in results if r["here_id"] == "shared") <= 1


@pytest.mark.asyncio
async def test_a_search_that_cannot_finish_reports_that_it_is_incomplete(monkeypatch):
    counter = {"n": 0}

    async def always_full(lat, lon, spec, radius_km=None, bbox=None, limit=100):
        counter["n"] += 1
        return [here_place(f"x{counter['n']}-{i}", f"Cafe {i}", lat, lon, "100-1100-0010", "Coffee Shop") for i in range(100)]

    monkeypatch.setattr(here_places, "_browse", always_full)
    stats: dict = {}
    await here_places.find_area_places({**fakes.AREAS["Bhubaneswar"], "kind": "city"}, "cafe", 50, stats=stats)
    assert stats["complete"] is False
    assert stats["requests"] <= here_places.MAX_AREA_REQUESTS and (stats["budget_exhausted"] or stats["unresolved_full_tiles"])


@pytest.mark.asyncio
async def test_a_failed_tile_makes_the_result_incomplete_and_only_total_failure_is_an_error(monkeypatch):
    state = {"n": 0}

    async def flaky(lat, lon, spec, radius_km=None, bbox=None, limit=100):
        state["n"] += 1
        if state["n"] == 2:
            raise AppError("provider_unavailable")
        return [here_place(f"ok{state['n']}", "Biju Patnaik International Airport", lat, lon, "400-4000-4581", "Airport")]

    monkeypatch.setattr(here_places, "_browse", flaky)
    stats: dict = {}
    assert await here_places.find_area_places({**ODISHA, "state": None, "kind": "region"}, "airport", 10, stats=stats)
    assert stats["complete"] is False and stats["failed_requests"] == 1

    async def dead(lat, lon, spec, radius_km=None, bbox=None, limit=100):
        raise AppError("provider_unavailable")

    monkeypatch.setattr(here_places, "_browse", dead)
    with pytest.raises(AppError):
        await here_places.find_area_places(ODISHA, "airport", 10)


@pytest.mark.asyncio
async def test_places_without_a_matching_state_are_not_counted_as_inside_it(monkeypatch):
    async def fake_browse(lat, lon, spec, radius_km=None, bbox=None, limit=100):
        no_state = here_place("n1", "Mystery Airport", lat, lon, "400-4000-4581", "Airport")
        no_state["address"].pop("state")
        no_state["address"].pop("stateCode")
        return [no_state]

    monkeypatch.setattr(here_places, "_browse", fake_browse)
    stats: dict = {}
    assert await here_places.find_area_places(ODISHA, "airport", 10, stats=stats) == []
    assert stats["outside_area"] >= 1


@pytest.mark.asyncio
async def test_area_search_refuses_kinds_of_place_it_cannot_check_and_areas_that_are_too_large():
    with pytest.raises(AppError) as unknown_category:
        await here_places.find_area_places(ODISHA, "supermarket", 10)
    assert unknown_category.value.code == "invalid_request"
    huge = {"name": "Everything", "kind": "region", "bbox": {"west": 60.0, "south": 5.0, "east": 100.0, "north": 40.0}}
    with pytest.raises(AppError) as too_large:
        await here_places.find_area_places(huge, "airport", 10)
    assert "too large" in too_large.value.message


@pytest.mark.asyncio
async def test_resolving_an_area_uses_hereS_boundary_box_and_refuses_a_whole_country(monkeypatch):
    def geo(name, kind, admin=None, state="Odisha", county=None, box=True):
        return {"display_name": f"{name}, {state}, India", "latitude": 20.0, "longitude": 85.0, "result_type": kind, "admin_type": admin,
                "map_view": {"west": 81.0, "south": 17.0, "east": 87.0, "north": 22.0} if box else None, "state": state,
                "state_code": "OD", "county": county, "query_score": 1.0}

    async def fake_geocode(query):
        return {"Odisha": [geo("Odisha", "administrativeArea", "state")], "North Goa": [geo("North Goa", "administrativeArea", "county", "Goa", "North Goa")],
                "Pune": [geo("Pune", "locality", state="Maharashtra")], "India": [geo("India", "administrativeArea", "country", state=None)],
                "Baga Beach": [geo("Baga Beach", "place", state="Goa")], "Nowhere": [geo("Nowhere", "locality", box=False)]}[query]

    monkeypatch.setattr(mcp_tools, "geocode_place", fake_geocode)
    state, _ = await mcp_tools.resolve_area_candidate("Odisha")
    assert state["kind"] == "state" and state["state"] == "Odisha" and state["bbox"]["west"] == 81.0
    district, _ = await mcp_tools.resolve_area_candidate("North Goa")
    assert district["kind"] == "district" and district["county"] == "North Goa"
    city, _ = await mcp_tools.resolve_area_candidate("Pune")
    assert city["kind"] == "city"
    assert (await mcp_tools.resolve_area_candidate("Baga Beach"))[0] is None, "a beach/business is not an area"
    assert (await mcp_tools.resolve_area_candidate("Nowhere"))[0] is None, "no boundary box, no area"
    with pytest.raises(AppError) as country:
        await mcp_tools.resolve_area_candidate("India")
    assert "whole country" in country.value.message


# ======================================================================================================
# Parser: in / near / within / along / me / midpoint / anchor
# ======================================================================================================
ROUTE_CONTEXT = {"stops": [{"name": "Bengaluru", "role": "origin"}, {"name": "Mysuru", "role": "destination"}], "route": {"distance_km": 144}, "pois": []}
EMPTY = {"stops": [], "pois": []}


@pytest.mark.parametrize("text,context,expected", [
    ("Share me the list of airports in Odisha", EMPTY, {"intent": "find_pois_in_area", "category": "airport", "area": "Odisha"}),
    ("airports in Odisha", EMPTY, {"intent": "find_pois_in_area", "category": "airport", "area": "Odisha"}),
    ("hospitals in Khordha district", EMPTY, {"intent": "find_pois_in_area", "category": "hospital", "area": "Khordha"}),
    ("list all airports across Odisha", EMPTY, {"intent": "find_pois_in_area", "category": "airport", "area": "Odisha"}),
    ("airports near Bhubaneswar", EMPTY, {"intent": "find_pois_near_location", "category": "airport", "place": "Bhubaneswar", "radius_explicit": False}),
    ("hospitals within 5 km of Pune", EMPTY, {"intent": "find_pois_near_location", "category": "hospital", "place": "Pune", "radius_km": 5.0, "radius_explicit": True}),
    ("cafes within 300 metres of Pune station", EMPTY, {"intent": "find_pois_near_location", "category": "cafe", "radius_km": pytest.approx(0.3), "radius_explicit": True}),
    ("airports along my route", ROUTE_CONTEXT, {"intent": "find_pois", "category": "airport", "scope": "route"}),
    ("petrol pumps within 5 km of me", EMPTY, {"intent": "find_pois", "category": "fuel", "scope": "me", "radius_km": 5.0, "radius_explicit": True}),
    ("find cafes near me", EMPTY, {"intent": "find_pois", "category": "cafe", "scope": "me"}),
    ("restaurants near my destination", ROUTE_CONTEXT, {"intent": "find_pois", "category": "restaurant", "scope": "destination"}),
    ("Find a suitable lunch stop within 10 km of the halfway point of my trip", ROUTE_CONTEXT,
     {"intent": "find_pois", "category": "restaurant", "scope": "midpoint", "radius_km": 10.0, "radius_explicit": True}),
    ("coffee shops near the midpoint of my route", ROUTE_CONTEXT, {"intent": "find_pois", "category": "cafe", "scope": "midpoint"}),
    ("Find restaurants within 500 metres of a hotel near Baga Beach", EMPTY,
     {"intent": "find_pois_near_anchor", "category": "restaurant", "anchor_category": "hotel", "place": "Baga Beach", "radius_km": 0.5}),
    ("show cafes near a petrol pump in Pune", EMPTY, {"intent": "find_pois_near_anchor", "category": "cafe", "anchor_category": "fuel", "place": "Pune"}),
    ("restaurants in Goa", EMPTY, {"intent": "find_pois_in_area", "category": "restaurant", "area": "Goa"}),
    ("tourist places in Goa", EMPTY, {"intent": "find_pois_near_location", "category": "tourist_attraction", "place": "Goa"}),  # no verified IDs: point search
])
def test_scope_words_decide_between_area_point_route_midpoint_and_user(text, context, expected):
    action = parse(text, context)
    for key, value in expected.items():
        assert action.get(key) == value, (text, key, action)


def test_the_five_reported_requests_are_read_as_intended():
    assert parse("Share me the list of airports in Odisha", EMPTY)["intent"] == "find_pois_in_area"                     # A
    b = parse("Find a quiet place for a coffee break about halfway between Bengaluru and Mysuru", EMPTY)                      # B
    assert b["intent"] == "compound_trip_request" and b["places"] == ["Bengaluru", "Mysuru"]
    assert b["poi_requests"][0]["category"] == "cafe" and b["poi_requests"][0]["scope"] == "midpoint" and b["poi_requests"][0]["qualifiers"] == ["quiet"]
    c = parse("Find a family-friendly restaurant near my destination, but don't change my route", ROUTE_CONTEXT)               # C
    assert c["intent"] == "find_pois" and c["scope"] == "destination" and c["qualifiers"] == ["family-friendly"]
    assert parse("Find restaurants within 500 metres of a hotel near Baga Beach", EMPTY)["intent"] == "find_pois_near_anchor"   # D
    e = parse("Find a suitable lunch stop within 10 km of the halfway point of my trip", ROUTE_CONTEXT)                       # E
    assert e["scope"] == "midpoint" and e["radius_km"] == 10.0


def test_route_change_prohibitions_are_recognised():
    for text in ("Find restaurants near my destination, but don't change my route", "do not modify my trip", "keep my route as is",
                 "show hotels without changing my route", "don't touch the stops"):
        assert forbids_route_change(text), text
    for text in ("Add Hyderabad to my route", "change my route to avoid tolls", "I don't like tolls"):
        assert not forbids_route_change(text), text


def test_validation_keeps_the_new_scope_fields_and_rejects_nonsense():
    assert validate_intent({"intent": "find_pois_in_area", "category": "airports", "area": "Odisha"})["category"] == "airport"
    assert validate_intent({"intent": "find_pois_in_area", "category": "airports"}) is None
    assert validate_intent({"intent": "find_pois_near_anchor", "category": "restaurant", "anchor_category": "not-a-category", "place": "X"}) is None
    midpoint = validate_intent({"intent": "find_pois", "scope": "midpoint", "category": "cafe", "fraction": 7, "qualifiers": ["Quiet", "quiet", "x" * 90]})
    assert midpoint["fraction"] == 0.95 and midpoint["qualifiers"][0] == "quiet" and len(midpoint["qualifiers"][1]) <= 30


# ======================================================================================================
# Orchestrator: handlers with faked providers
# ======================================================================================================
@pytest.fixture
def world(monkeypatch):
    fakes.AREA_CALLS.clear()
    fakes.install(monkeypatch)
    return monkeypatch


@pytest.mark.asyncio
async def test_airports_in_odisha_searches_the_state_not_a_point_and_says_what_it_covered(world):
    context, result, reply = await say(new_context(), "Share me the list of airports in Odisha")
    assert result["action"] == "find_pois_in_area" and result["scope"] == "area" and result["area"]["name"] == "Odisha"
    assert fakes.AREA_CALLS == [{"area": "Odisha", "category": "airport", "limit": 80, "query": "the list of airports"}]
    assert result["search_query"] == "the list of airports", "the original wording is kept"
    assert "in Odisha (state)" in reply and "everything HERE's data classifies as airports" in reply
    assert "3 more fell outside Odisha" in reply
    assert context.pois_label == "airports in Odisha" and context.stops == [] and context.route is None


@pytest.mark.asyncio
async def test_in_near_within_and_along_reach_different_providers_with_different_geometry(world):
    calls = {"near": [], "route": []}

    async def spy_near(points, category, radius_km, limit=80, search_query=None):
        calls["near"].append((points, category, radius_km))
        return await fakes.nearby(points, category, radius_km, limit, search_query)

    async def spy_route(geometry, category, corridor_km, limit=60, search_query=None):
        calls["route"].append((category, corridor_km))
        return await fakes.route_pois(geometry, category, corridor_km, limit, search_query)

    fakes.install(world, find_nearby_pois_with_source=spy_near, find_route_pois_with_source=spy_route)
    context, _, _ = await say(new_context(), "Plan a trip from Bengaluru to Mysuru")

    _, result, _ = await say(context, "airports in Odisha")
    assert result["action"] == "find_pois_in_area" and not calls["near"] and not calls["route"]

    _, result, _ = await say(context, "airports near Bhubaneswar")
    points, category, radius = calls["near"][-1]
    assert result["action"] == "find_pois_near_location" and category == "airport" and radius == 30.0, "airports are sparse: 30 km default, not 5"
    assert points == [{"latitude": fakes.COORDS["Bhubaneswar"][0], "longitude": fakes.COORDS["Bhubaneswar"][1]}]

    _, result, _ = await say(context, "hospitals within 3 km of Pune")
    assert calls["near"][-1][1:] == ("hospital", 3.0) and result["radius_km"] == 3.0

    _, result, _ = await say(context, "airports along my route")
    assert calls["route"] == [("airport", 25.0)] and result["scope"] == "route"


@pytest.mark.asyncio
async def test_a_place_that_is_not_an_area_falls_back_to_a_point_search_and_says_so(world):
    context, result, reply = await say(new_context(), "restaurants in Baga Beach")
    assert result["action"] == "find_pois_near_location" and result["area_fallback"] == "Baga Beach"
    assert "couldn't find a state, district or city called “Baga Beach”" in reply


@pytest.mark.asyncio
async def test_unmapped_categories_in_a_named_place_keep_the_point_search(world):
    context, result, _ = await say(new_context(), "tourist places in Goa")
    assert result["action"] == "find_pois_near_location" and result["radius_km"] == 15.0 and not fakes.AREA_CALLS


@pytest.mark.asyncio
async def test_midpoint_search_is_centred_on_the_computed_route_midpoint_not_an_end_point(world):
    seen = {}

    async def spy(points, category, radius_km, limit=80, search_query=None):
        seen.update(points=points, category=category, radius=radius_km)
        return await fakes.nearby(points, category, radius_km, limit, search_query)

    fakes.install(world, find_nearby_pois_with_source=spy)
    context, _, _ = await say(new_context(), "Plan a trip from Mumbai to Bhubaneswar via Hyderabad")
    coords = context.route["geometry"]["coordinates"]
    expected = point_along_route(coords, 0.5)
    origin, destination = context.stops[0], context.stops[-1]

    context, result, reply = await say(context, "Find a suitable lunch stop within 10 km of the halfway point of my trip")
    centre = seen["points"][0]
    assert centre["latitude"] == pytest.approx(expected["latitude"]) and centre["longitude"] == pytest.approx(expected["longitude"])
    assert (centre["latitude"], centre["longitude"]) not in {(origin.latitude, origin.longitude), (destination.latitude, destination.longitude)}
    assert seen["category"] == "restaurant" and seen["radius"] == 10.0
    assert result["scope"] == "midpoint" and result["midpoint"]["distance_from_start_km"] == pytest.approx(expected["distance_from_start_km"])
    assert "from the halfway point" in reply and "Distances are measured from the halfway point" in reply
    assert all(p.get("distance_from_route_km") is not None for p in result["pois"]), "each result also reports its distance from the route"
    assert context.trip_names() == ["Mumbai", "Hyderabad", "Bhubaneswar"]


@pytest.mark.asyncio
async def test_coffee_break_halfway_between_two_cities_plans_the_trip_then_searches_cafes_at_its_midpoint(world):
    seen = {}

    async def spy(points, category, radius_km, limit=80, search_query=None):
        seen.update(points=points, category=category)
        return await fakes.nearby(points, category, radius_km, limit, search_query)

    fakes.install(world, find_nearby_pois_with_source=spy)
    context, result, reply = await say(new_context(), "Find a quiet place for a coffee break about halfway between Bengaluru and Mysuru")
    assert context.trip_names() == ["Bengaluru", "Mysuru"]
    assert seen["category"] == "cafe"
    lat = seen["points"][0]["latitude"]
    assert min(fakes.COORDS["Mysuru"][0], fakes.COORDS["Bengaluru"][0]) < lat < max(fakes.COORDS["Mysuru"][0], fakes.COORDS["Bengaluru"][0])
    assert result["poi_groups"][0]["scope"] == "midpoint"
    assert "halfway point" in reply and "HERE has no data on whether a place is quiet" in reply
    assert "quiet cafe" not in reply.lower(), "never claim a venue is quiet"


@pytest.mark.asyncio
async def test_an_already_planned_trip_is_reused_not_re_routed_for_a_halfway_request(world):
    routed = {"n": 0}

    async def counting_route(stops, ev=None, transport_mode="car"):
        routed["n"] += 1
        return await fakes.route(stops, ev, transport_mode)

    fakes.install(world, calculate_route=counting_route)
    context, _, _ = await say(new_context(), "Plan a trip from Bengaluru to Mysuru")
    revision = context.route_revision
    context, _, _ = await say(context, "Find a coffee break about halfway between Bengaluru and Mysuru")
    assert routed["n"] == 1 and context.route_revision == revision


@pytest.mark.asyncio
async def test_destination_follow_up_does_not_touch_the_route(world, fake_llm):
    routed = {"n": 0}

    async def counting_route(stops, ev=None, transport_mode="car"):
        routed["n"] += 1
        return await fakes.route(stops, ev, transport_mode)

    fakes.install(world, calculate_route=counting_route)
    # A model that wrongly reads the message as a route edit must not be obeyed.
    fake_llm(lambda system, user: json.dumps({"intent": "modify_trip", "operation": "add_stop", "place": "Pune"}) if "intent parser" in system else "ok")
    context, _, _ = await say(new_context(), "Plan a trip from Bengaluru to Mysuru")
    before = (context.trip_names(), context.route_revision, context.route["distance_km"])
    context, result, reply = await say(context, "Find a family-friendly restaurant near my destination, but don't change my route")
    assert (context.trip_names(), context.route_revision, context.route["distance_km"]) == before and routed["n"] == 1
    assert result["action"] == "find_pois" and result["scope"] == "destination" and result["target"] == "Mysuru"
    assert "HERE has no data on whether a place is family-friendly" in reply


@pytest.mark.asyncio
async def test_a_route_rewrite_is_blocked_whenever_the_user_forbids_it(world):
    context, _, _ = await say(new_context(), "Plan a trip from Bengaluru to Mysuru")
    context, result, reply = await say(context, "add a stop at Pune but don't change my route")
    assert result["action"] == "clarify" and "not to change your route" in reply
    assert context.trip_names() == ["Bengaluru", "Mysuru"]


@pytest.mark.asyncio
async def test_the_model_can_choose_the_area_tool_but_here_still_supplies_the_places(world, fake_llm):
    fake_llm(lambda system, user: json.dumps({"intent": "find_pois_in_area", "category": "airport", "area": "Odisha"}) if "intent parser" in system else "ok")
    context, result, reply = await say(new_context(), "Could you enumerate all the aerodromes that exist within the territory of Odisha?")
    assert result["action"] == "find_pois_in_area" and fakes.AREA_CALLS and fakes.AREA_CALLS[0]["category"] == "airport"
    assert [p["name"] for p in result["pois"]] == ["Airport 1", "Airport 2"], "place names come from the HERE-backed provider, not from the model"
    assert "Airport 1" not in reply, "the places are in the card; the reply does not repeat them as a list"


# ---- "me" -------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_near_me_needs_a_shared_location_and_then_searches_around_it(world):
    seen = {}

    async def spy(points, category, radius_km, limit=80, search_query=None):
        seen.update(points=points, category=category, radius=radius_km)
        return await fakes.nearby(points, category, radius_km, limit, search_query)

    fakes.install(world, find_nearby_pois_with_source=spy)
    context = new_context()
    context, result, reply = await say(context, "petrol pumps within 5 km of me")
    assert result["action"] == "clarify" and result["needs_location"] is True and "location icon on the map" in reply and not seen, "asks; never geocodes the word 'me'"
    context.user_location = {"latitude": 20.2961, "longitude": 85.8245, "accuracy_m": 25}
    context, result, reply = await say(context, "petrol pumps within 5 km of me")
    assert seen["points"] == [{"latitude": 20.2961, "longitude": 85.8245}] and seen["radius"] == 5.0 and seen["category"] == "fuel"
    assert result["scope"] == "me" and "near you" in reply


@pytest.mark.asyncio
async def test_a_model_that_puts_me_or_the_halfway_point_in_a_place_name_is_still_resolved_correctly(world, fake_llm):
    fake_llm(lambda system, user: json.dumps({"intent": "find_pois_near_location", "category": "fuel", "place": "my location"}) if "intent parser" in system else "ok")
    context = new_context()
    context.user_location = {"latitude": 19.0, "longitude": 73.0}
    seen = {}

    async def spy(points, category, radius_km, limit=80, search_query=None):
        seen["points"] = points
        return await fakes.nearby(points, category, radius_km, limit, search_query)

    fakes.install(world, find_nearby_pois_with_source=spy)
    await say(context, "gas stations around the place I am standing at right now please")
    assert seen["points"] == [{"latitude": 19.0, "longitude": 73.0}], "never geocoded as a place called 'my location'"


# ---- anchors ------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_hotel_anchor_is_not_silently_replaced_by_the_beach(world):
    near_calls = []

    async def spy(points, category, radius_km, limit=80, search_query=None):
        near_calls.append((category, radius_km, points[0]))
        return await fakes.nearby(points, category, radius_km, limit, search_query)

    fakes.install(world, find_nearby_pois_with_source=spy)
    context, result, reply = await say(new_context(), "Find restaurants within 500 metres of a hotel near Baga Beach")
    hotels = [dict(p) for p in context.pois]
    assert [c[0] for c in near_calls] == ["hotel"], "only the hotels were looked up; no restaurant search yet"
    assert near_calls[0][1] == 2.0
    assert result["anchor_step"] == "choose" and "Which one should I search around" in reply and "500 m" in reply
    assert len(context.pois) == 3 and context.pois_label == "hotels near Baga Beach"
    assert context.last_action["pending"]["scope"] == "result" and context.last_action["pending"]["radius_km"] == 0.5

    context, result, reply = await say(context, "the second one")
    category, radius, centre = near_calls[-1]
    assert category == "restaurant" and radius == 0.5, "restaurants within 500 m, not 5 km"
    assert (centre["latitude"], centre["longitude"]) == (hotels[1]["latitude"], hotels[1]["longitude"]), "centred on the hotel the user picked"
    assert result["scope"] == "result" and result["target"] == "Hotel 2" and "within 500 m" in reply and "near Hotel 2" in reply


@pytest.mark.asyncio
async def test_choosing_an_anchor_by_number_name_or_something_unrelated(world):
    context, _, _ = await say(new_context(), "Find restaurants within 500 metres of a hotel near Baga Beach")
    # An unrelated request is not mistaken for a pick, and the pending question is dropped.
    context, result, _ = await say(context, "Plan a trip from Mumbai to Pune")
    assert result["action"] == "plan_trip"

    context, _, _ = await say(new_context(), "Find restaurants within 500 metres of a hotel near Baga Beach")
    context, result, _ = await say(context, "3")
    assert result["target"] == "Hotel 3"


@pytest.mark.asyncio
async def test_a_single_anchor_candidate_is_used_and_named(world):
    async def one_hotel(points, category, radius_km, limit=80, search_query=None):
        full = await fakes.nearby(points, category, radius_km, limit, search_query)
        if category == "hotel":
            full["pois"] = full["pois"][:1]
        return full

    fakes.install(world, find_nearby_pois_with_source=one_hotel)
    _, result, reply = await say(new_context(), "Find restaurants within 500 metres of a hotel near Baga Beach")
    assert result["action"] == "find_pois" and result["target"] == "Hotel 1" and result["anchor"]["only_candidate"]
    assert "The only hotel I found within 2 km of Baga Beach is Hotel 1" in reply


@pytest.mark.asyncio
async def test_no_anchor_means_no_search_and_an_honest_message(world):
    async def none_found(points, category, radius_km, limit=80, search_query=None):
        return {"pois": [], "source_status": "empty", "source": "HERE Search API", "source_error": None}

    fakes.install(world, find_nearby_pois_with_source=none_found)
    _, result, reply = await say(new_context(), "Find restaurants within 500 metres of a hotel near Baga Beach")
    assert result["anchor_step"] == "none_found" and "I haven't searched for restaurants" in reply


@pytest.mark.asyncio
async def test_a_truncated_anchor_list_says_these_are_only_the_nearest(world):
    async def full_page(points, category, radius_km, limit=80, search_query=None):
        result = await fakes.nearby(points, category, radius_km, limit, search_query)
        result["search"] = {"method": "browse", "category_filtered": True, "rejected": 0, "page_full": True, "matched": 100}
        return result

    fakes.install(world, find_nearby_pois_with_source=full_page)
    _, result, reply = await say(new_context(), "Find restaurants within 500 metres of a hotel near Baga Beach")
    assert result["anchor_step"] == "choose"
    assert "Here are the nearest 3 hotels within 2 km of Baga Beach (there are more than I can list)" in reply
    assert "I found 3 hotels" not in reply, "a capped list is not presented as everything that exists"


# ---- honesty about completeness and limits --------------------------------------------------------------
@pytest.mark.asyncio
async def test_incomplete_area_results_are_labelled_incomplete(world):
    async def partial(area, category, limit=80, search_query=None):
        full = await fakes.area_pois(area, category, limit, search_query)
        full["search"].update({"complete": False, "budget_exhausted": True, "unresolved_full_tiles": 3, "requests": 24})
        return full

    fakes.install(world, find_area_pois_with_source=partial)
    _, _, reply = await say(new_context(), "restaurants in Odisha")
    assert "may be incomplete" in reply and "at most 100 places per request" in reply and "everything HERE's data classifies" not in reply


@pytest.mark.asyncio
async def test_a_full_provider_page_is_reported_as_a_limit_not_hidden(world):
    async def full_page(points, category, radius_km, limit=80, search_query=None):
        result = await fakes.nearby(points, category, radius_km, limit, search_query)
        result["search"] = {"method": "browse", "category_filtered": True, "rejected": 2, "page_full": True, "matched": 100}
        return result

    fakes.install(world, find_nearby_pois_with_source=full_page)
    _, _, reply = await say(new_context(), "restaurants near Pune")
    assert "HERE returns at most 100 places per search" in reply and "left out 2 results that HERE doesn't classify as restaurants" in reply


@pytest.mark.asyncio
async def test_empty_area_results_are_reported_as_empty_not_invented(world):
    async def nothing(area, category, limit=80, search_query=None):
        return {"pois": [], "source_status": "empty", "source": "HERE Search API", "source_error": None, "search": {"complete": True, "requests": 6}}

    fakes.install(world, find_area_pois_with_source=nothing)
    _, result, reply = await say(new_context(), "airports in Odisha")
    assert result["pois"] == [] and "I found no airports in Odisha" in reply


@pytest.mark.asyncio
async def test_non_ev_search_results_never_mention_ev(world):
    context, _, _ = await say(new_context(), "Plan a trip from Bengaluru to Mysuru")
    _, _, reply = await say(context, "petrol pumps along my route")
    assert "charging" not in reply.lower() and "battery" not in reply.lower()


# ---- API ---------------------------------------------------------------------------------------------------
fastapi_testclient = pytest.importorskip("fastapi.testclient")


@pytest.fixture
def client(world):
    import main

    main.store._contexts.clear()
    return fastapi_testclient.TestClient(main.app, raise_server_exceptions=False)


def test_chat_accepts_a_shared_location_validates_it_and_can_forget_it(client):
    me = {"latitude": 20.2961, "longitude": 85.8245, "accuracy_m": 30}
    ok = client.post("/chat", json={"message": "petrol pumps within 5 km of me", "location": me})
    assert ok.status_code == 200 and ok.json()["result"]["scope"] == "me"
    session = ok.json()["session_id"]
    again = client.post("/chat", json={"message": "cafes near me", "session_id": session})   # location persisted in the session
    assert again.status_code == 200 and again.json()["result"]["scope"] == "me" and again.json()["context"]["user_location"]["latitude"] == 20.2961

    forgotten = client.post("/chat", json={"message": "cafes near me", "session_id": session, "location": None})
    assert forgotten.status_code == 200 and forgotten.json()["result"]["needs_location"] is True
    assert forgotten.json()["context"]["user_location"] is None

    for bad in ({"latitude": 91, "longitude": 0}, {"latitude": 0, "longitude": 181}, {"latitude": "x", "longitude": 1}):
        assert client.post("/chat", json={"message": "cafes near me", "location": bad}).status_code == 422


def test_the_model_only_ever_sees_whether_a_location_exists_never_the_coordinates():
    from services.llm_assistant import llm_view

    view = llm_view({"user_location": {"latitude": 20.123456, "longitude": 85.654321}, "stops": []})
    assert view["has_shared_location"] is True and "20.123456" not in json.dumps(view) and "85.654321" not in json.dumps(view)


def test_the_intent_prompt_describes_the_scope_vocabulary_the_validator_accepts():
    from services.llm_assistant import INTENT_SYSTEM_PROMPT

    for needle in ("find_pois_in_area", "find_pois_near_anchor", "midpoint", "scope \"me\"", "qualifiers", "airport", "medical", "don't change my route"):
        assert needle in INTENT_SYSTEM_PROMPT, needle


# ---- the model is asked about every message; exact rules keep the final say ----------------------------------
def _intent_calls(provider):
    return [c for c in provider.calls if "intent parser" in c["system"]]


@pytest.mark.asyncio
async def test_the_model_is_asked_even_when_exact_rules_decide_and_the_rules_still_win(world, fake_llm):
    # The model proposes a nearby petrol search. The rules read "in Odisha" as an area search and must win.
    provider = fake_llm(lambda system, user: json.dumps({"intent": "find_pois_near_location", "category": "fuel", "place": "Pune"}) if "intent parser" in system else "ok")
    _, result, _ = await say(new_context(), "airports in Odisha")
    assert len(_intent_calls(provider)) == 1, "consulted even though the rules were certain"
    assert result["action"] == "find_pois_in_area" and fakes.AREA_CALLS[0]["category"] == "airport"


@pytest.mark.asyncio
async def test_a_route_edit_is_consulted_with_the_model_but_never_taken_from_it(world, fake_llm):
    provider = fake_llm(lambda system, user: json.dumps({"intent": "find_pois_near_location", "category": "cafe", "place": "Pune"}) if "intent parser" in system else "ok")
    context, _, _ = await say(new_context(), "Plan a trip from Bengaluru to Mysuru")
    before = len(_intent_calls(provider))
    context, result, _ = await say(context, "add a stop at Pune")
    assert len(_intent_calls(provider)) == before + 1, "the edit message was also put to the model"
    assert result["action"] == "modify_trip" and "Pune" in context.trip_names()


@pytest.mark.asyncio
async def test_the_source_label_says_whether_the_model_agreed_with_the_rules(world, fake_llm):
    fake_llm(lambda system, user: json.dumps({"intent": "find_pois_in_area", "category": "airport", "area": "Odisha"}) if "intent parser" in system else "ok")
    action, source = await orchestrator._interpret_message("airports in Odisha", new_context())
    assert action["intent"] == "find_pois_in_area" and source == "rules_llm_agree"

    fake_llm(lambda system, user: json.dumps({"intent": "find_pois_near_location", "category": "fuel", "place": "Pune"}) if "intent parser" in system else "ok")
    action, source = await orchestrator._interpret_message("airports in Odisha", new_context())
    assert action["intent"] == "find_pois_in_area" and source == "rules_llm_differ"


@pytest.mark.asyncio
async def test_if_the_model_fails_the_rules_answer_alone_and_nothing_breaks(world, fake_llm):
    from services.llm.provider import LLMModelUnavailable

    fake_llm(lambda system, user: LLMModelUnavailable("down"))
    action, source = await orchestrator._interpret_message("airports in Odisha", new_context())
    assert action["intent"] == "find_pois_in_area" and source == "rules"
    _, result, _ = await say(new_context(), "airports in Odisha")
    assert result["action"] == "find_pois_in_area"
