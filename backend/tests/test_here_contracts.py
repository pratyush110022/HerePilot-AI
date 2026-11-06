"""HERE geocoding, places and routing contracts, against recorded-shape payloads (no network)."""
from __future__ import annotations

import httpx
import pytest

from services import here_places, routing
from services.errors import AppError


def response(status, payload=None, text=None):
    request = httpx.Request("GET", "https://example.invalid/")
    if payload is not None:
        return httpx.Response(status, json=payload, request=request)
    return httpx.Response(status, text=text or "", request=request)


def patch_request(monkeypatch, module, handler):
    calls = []

    async def fake_request(method, url, **kwargs):
        calls.append({"url": url, **kwargs})
        result = handler(len(calls), kwargs.get("params") or {})
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(module, "request", fake_request)
    monkeypatch.setattr(module, "HERE_API_KEY", "TEST-KEY-NOT-REAL")
    return calls


def geocode_item(title, lat, lng, kind="locality", state="Telangana", score=1.0):
    return {"id": f"here:{title}", "title": title, "resultType": kind, "position": {"lat": lat, "lng": lng},
            "address": {"label": f"{title}, {state}, India", "city": title, "state": state, "countryName": "India"},
            "scoring": {"queryScore": score}}


# ---- geocoding ----------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_geocode_normalises_hera_items_into_the_internal_location_shape(monkeypatch):
    patch_request(monkeypatch, here_places, lambda n, p: response(200, {"items": [geocode_item("Hyderabad", 17.385, 78.4867)]}))
    [place] = await here_places.search_places("Hyderabad")
    assert place["latitude"] == 17.385 and place["longitude"] == 78.4867
    assert place["result_type"] == "locality" and place["state"] == "Telangana" and place["display_name"].startswith("Hyderabad")
    assert place["source"] == "HERE Geocoding & Search API"


@pytest.mark.asyncio
async def test_city_query_prefers_the_locality_over_a_business_with_a_similar_name(monkeypatch):
    items = [geocode_item("Hyderabad Biryani House", 17.40, 78.48, kind="place"), geocode_item("Hyderabad", 17.385, 78.4867, kind="locality")]
    patch_request(monkeypatch, here_places, lambda n, p: response(200, {"items": items}))
    results = await here_places.search_places("Hyderabad")
    assert results[0]["result_type"] == "locality", "never pick an arbitrary business when the user means a city"


@pytest.mark.asyncio
async def test_street_style_queries_keep_the_providers_own_order(monkeypatch):
    items = [geocode_item("12 MG Road", 12.97, 77.6, kind="houseNumber"), geocode_item("MG Road", 12.97, 77.61, kind="street")]
    patch_request(monkeypatch, here_places, lambda n, p: response(200, {"items": items}))
    results = await here_places.search_places("12 MG Road Bengaluru")
    assert [r["result_type"] for r in results] == ["houseNumber", "street"]


@pytest.mark.asyncio
async def test_empty_results_are_empty_not_an_error_and_retry_without_country_filter(monkeypatch):
    seen = []

    def handler(n, params):
        seen.append(dict(params))
        return response(200, {"items": []})

    patch_request(monkeypatch, here_places, handler)
    assert await here_places.search_places("zzzzqqqq") == []
    assert seen[0].get("in") == "countryCode:IND" and "in" not in seen[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("status, code", [(401, "provider_not_configured"), (403, "provider_not_configured"), (429, "provider_rate_limited"),
                                          (500, "provider_unavailable"), (503, "provider_unavailable")])
async def test_provider_http_failures_are_errors_with_safe_codes_and_never_copy_the_body(monkeypatch, status, code):
    patch_request(monkeypatch, here_places, lambda n, p: response(status, text="LEAK apiKey=SUPER-SECRET internal stack"))
    with pytest.raises(AppError) as caught:
        await here_places.search_places("Pune")
    assert caught.value.code == code
    assert "SUPER-SECRET" not in caught.value.message and "LEAK" not in caught.value.message
    assert "SUPER-SECRET" not in repr(caught.value.detail)


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [httpx.ConnectTimeout("t"), httpx.ReadTimeout("t"), httpx.ConnectError("c")])
async def test_network_failures_are_provider_unavailable(monkeypatch, exc):
    patch_request(monkeypatch, here_places, lambda n, p: exc)
    with pytest.raises(AppError) as caught:
        await here_places.search_places("Pune")
    assert caught.value.code == "provider_unavailable" and caught.value.retryable is True


@pytest.mark.asyncio
async def test_invalid_json_and_unusable_coordinates_are_handled(monkeypatch):
    patch_request(monkeypatch, here_places, lambda n, p: response(200, text="<html>not json</html>"))
    with pytest.raises(AppError) as caught:
        await here_places.search_places("Pune")
    assert caught.value.code == "provider_unavailable"

    # httpx's JSON encoder rejects NaN, so send the payload as raw text the way a misbehaving server could.
    bad = ('{"items": [{"title": "Nowhere", "position": {"lat": "x", "lng": 5}}, '
           '{"title": "Off", "position": {"lat": 95, "lng": 5}}, {"title": "NaN", "position": {"lat": NaN, "lng": 5}}]}')
    patch_request(monkeypatch, here_places, lambda n, p: response(200, text=bad))
    assert await here_places.search_places("Pune") == []


@pytest.mark.asyncio
async def test_missing_api_key_is_a_configuration_error(monkeypatch):
    monkeypatch.setattr(here_places, "HERE_API_KEY", None)
    with pytest.raises(AppError) as caught:
        await here_places.search_places("Pune")
    assert caught.value.code == "provider_not_configured"


def test_ambiguity_is_detected_for_equal_cities_in_different_states_only():
    a = {**{"display_name": "Aurangabad, Maharashtra", "latitude": 19.87, "longitude": 75.34, "result_type": "locality", "state": "Maharashtra", "country": "India", "query_score": 1.0}}
    b = {**a, "display_name": "Aurangabad, Bihar", "latitude": 24.75, "longitude": 84.37, "state": "Bihar"}
    assert len(here_places.detect_ambiguity([a, b], "Aurangabad")) == 2
    assert here_places.detect_ambiguity([a, b], "Aurangabad, Bihar") == [], "naming a state resolves it"
    assert here_places.detect_ambiguity([a, b], "Aurangabad Maharashtra") == []
    near = {**b, "latitude": 19.9, "longitude": 75.4, "state": "Maharashtra"}
    assert here_places.detect_ambiguity([a, near], "Aurangabad") == []
    assert here_places.detect_ambiguity([a], "Aurangabad") == []
    weaker = {**b, "query_score": 0.6}
    assert here_places.detect_ambiguity([a, weaker], "Aurangabad") == []


# ---- places -------------------------------------------------------------------------------------
def poi_item(item_id, name, lat, lng, category="Hotel", category_id="500-5000-0053"):
    """A HERE place as the real API returns it: categories carry HERE category IDs (500-5000-0053 = Hotel)."""
    categories = [{"id": category_id, "name": category, "primary": True}] if category_id else [{"name": category}]
    return {"id": item_id, "title": name, "position": {"lat": lat, "lng": lng},
            "address": {"label": f"{name}, Pune", "city": "Pune", "state": "Maharashtra", "countryName": "India"},
            "categories": categories, "contacts": [{"phone": [{"value": "+91 20 0000 0000"}], "www": [{"value": "https://example.test"}]}],
            "openingHours": {"text": ["Mon-Sun: 00:00 - 24:00"]}, "rating": {"value": 4.2}}


@pytest.mark.asyncio
async def test_a_known_category_is_searched_by_category_id_in_a_circle_not_by_free_text(monkeypatch):
    calls = patch_request(monkeypatch, here_places, lambda n, p: response(200, {"items": [poi_item("1", "Hotel A", 18.52, 73.86)]}))
    await here_places.find_nearby_places([{"latitude": 18.5204, "longitude": 73.8567}], "hotel", radius_km=3, limit=5)
    params = calls[0]["params"]
    assert calls[0]["url"] == here_places.HERE_BROWSE_URL
    assert params["in"] == "circle:18.5204,73.8567;r=3000" and params["at"] == "18.5204,73.8567"
    assert params["categories"] == "500-5000,500-5100,!500-5100-0056,!500-5100-0060", "Hotel-Motel + Lodging, without campgrounds"
    assert "q" not in params and params["limit"] <= 100


@pytest.mark.asyncio
async def test_descriptive_text_keeps_the_text_search_but_results_are_still_category_checked(monkeypatch):
    items = [poi_item("1", "Pizza Palace", 18.521, 73.857, "Restaurant", "100-1000-0000"),
             poi_item("2", "Pizza Palace Inn", 18.522, 73.857, "Hotel", "500-5000-0053")]
    calls = patch_request(monkeypatch, here_places, lambda n, p: response(200, {"items": items}))
    stats: dict = {}
    results = await here_places.find_nearby_places([{"latitude": 18.5204, "longitude": 73.8567}], "restaurant", 3, 10, "pizza restaurants", stats=stats)
    assert calls[0]["url"] == here_places.HERE_DISCOVER_URL and calls[0]["params"]["q"] == "pizza restaurants"
    assert [r["name"] for r in results] == ["Pizza Palace"] and stats["rejected"] == 1, "the hotel only shares a word with the query"


@pytest.mark.asyncio
async def test_places_are_normalised_sorted_by_distance_and_inside_the_radius(monkeypatch):
    items = [poi_item("far", "Far Hotel", 18.60, 73.86), poi_item("near", "Near Hotel", 18.525, 73.857), poi_item("mid", "Mid Hotel", 18.54, 73.857)]
    patch_request(monkeypatch, here_places, lambda n, p: response(200, {"items": items}))
    results = await here_places.find_nearby_places([{"latitude": 18.5204, "longitude": 73.8567}], "hotel", radius_km=5, limit=10)
    assert [r["name"] for r in results] == ["Near Hotel", "Mid Hotel"], "the 9 km result must be dropped by the exact radius check"
    first = results[0]
    assert first["distance_from_reference_km"] < results[1]["distance_from_reference_km"] <= 5
    assert first["phone"] == "+91 20 0000 0000" and first["website"] == "https://example.test"
    assert first["opening_hours"] and first["rating"] == 4.2 and first["source"] == "HERE Search API"
    assert first["category"] == "hotel" and first["category_validated"] is True
    assert first["here_category"] == "Hotel" and first["here_category_ids"] == ["500-5000-0053"] and first["primary_category_id"] == "500-5000-0053"


@pytest.mark.asyncio
async def test_real_here_opening_hours_shape_is_a_list_of_objects(monkeypatch):
    """Found live: HERE returns ``openingHours`` as a LIST of objects. The earlier object-shaped fixture hid a crash."""
    item = poi_item("1", "Hotel A", 18.521, 73.857)
    item["openingHours"] = [
        {"categories": [{"id": "100-1000"}], "text": ["Mon-Fri: 09:00 - 18:00", "Sat: 10:00 - 14:00"], "isOpen": True, "structured": []},
        {"text": "Sun: closed"},
    ]
    patch_request(monkeypatch, here_places, lambda n, p: response(200, {"items": [item]}))
    results = await here_places.find_nearby_places([{"latitude": 18.5204, "longitude": 73.8567}], "hotel", 3, 5)
    assert results[0]["opening_hours"] == "Mon-Fri: 09:00 - 18:00; Sat: 10:00 - 14:00; Sun: closed"


@pytest.mark.asyncio
async def test_malformed_items_are_skipped_instead_of_failing_the_whole_search(monkeypatch):
    good = poi_item("1", "Hotel A", 18.521, 73.857)
    odd = poi_item("2", "Odd Hotel", 18.522, 73.858)
    odd.update({"address": "not-a-dict", "contacts": "nope", "openingHours": "always", "rating": [4]})
    unclassified = poi_item("3", "No Categories Hotel", 18.523, 73.858)
    unclassified["categories"] = "x"
    items = [{"id": "no-position", "title": "Nowhere"}, {"id": "bad", "title": "Bad", "position": {"lat": None, "lng": "x"}}, "garbage", good, odd, unclassified]
    patch_request(monkeypatch, here_places, lambda n, p: response(200, {"items": items}))
    stats: dict = {}
    results = await here_places.find_nearby_places([{"latitude": 18.5204, "longitude": 73.8567}], "hotel", 3, 10, stats=stats)
    assert {r["name"] for r in results} == {"Hotel A", "Odd Hotel"}
    odd_result = next(r for r in results if r["name"] == "Odd Hotel")
    assert odd_result["opening_hours"] is None and odd_result["phone"] is None and odd_result["rating"] is None
    assert stats["rejected"] == 1, "a place HERE does not classify as a hotel is not presented as one, whatever its name says"


@pytest.mark.asyncio
async def test_zero_results_is_an_empty_list(monkeypatch):
    patch_request(monkeypatch, here_places, lambda n, p: response(200, {"items": []}))
    assert await here_places.find_nearby_places([{"latitude": 18.5, "longitude": 73.8}], "hotel", 5, 10) == []


@pytest.mark.asyncio
async def test_a_rejected_circle_filter_retries_biased_by_at_and_still_enforces_the_radius(monkeypatch):
    """Text (Discover) search for a category with no verified HERE IDs: HERE rejects `at` together with `in=circle`."""
    def handler(n, params):
        if str(params.get("in", "")).startswith("circle:"):
            return response(400, text="bad in")
        return response(200, {"items": [poi_item("1", "Mart A", 18.521, 73.857, "Supermarket", None), poi_item("2", "Too Far", 19.5, 73.86, "Supermarket", None)]})

    calls = patch_request(monkeypatch, here_places, handler)
    results = await here_places.find_nearby_places([{"latitude": 18.5204, "longitude": 73.8567}], "supermarket", 3, 10)
    assert len(calls) == 2 and "at" in calls[1]["params"] and "in" not in calls[1]["params"] and [r["name"] for r in results] == ["Mart A"]
    assert results[0]["category_validated"] is False, "no verified category IDs for this type, so nothing claims it was checked"


@pytest.mark.asyncio
async def test_duplicate_here_ids_collapse_to_the_nearest_copy(monkeypatch):
    items = [poi_item("dup", "Hotel Dup", 18.53, 73.857), poi_item("dup", "Hotel Dup", 18.5206, 73.857)]
    patch_request(monkeypatch, here_places, lambda n, p: response(200, {"items": items}))
    results = await here_places.find_nearby_places([{"latitude": 18.5204, "longitude": 73.8567}], "hotel", 5, 10)
    assert len(results) == 1 and results[0]["distance_from_reference_km"] < 0.1


def test_route_sampling_is_spaced_by_distance_and_bounded():
    line = [[72.0 + i * 0.1, 19.0] for i in range(400)]             # roughly 4,000 km
    samples = here_places.sample_route_points(line, spacing_km=50, max_points=20)
    assert len(samples) == 20 and samples[0] == line[0] and samples[-1] == pytest.approx(line[-1], abs=1e-6)
    short = here_places.sample_route_points([[72.0, 19.0], [72.1, 19.0]], spacing_km=50)
    assert len(short) == 2


@pytest.mark.asyncio
async def test_route_search_reports_when_a_long_route_was_only_partly_covered(monkeypatch):
    async def fake_browse(lat, lon, spec, radius_km=None, bbox=None, limit=100):
        return [poi_item(f"{lat:.2f}", "Pump", lat + 0.001, lon, "Petrol Station", "700-7600-0116")]

    monkeypatch.setattr(here_places, "_browse", fake_browse)
    geometry = {"coordinates": [[72.0 + i * 0.1, 19.0] for i in range(400)]}
    coverage: dict = {}
    results = await here_places.find_route_places(geometry, "fuel", 5, 50, coverage=coverage)
    assert coverage["complete"] is False and coverage["sampled_points"] <= here_places.MAX_ROUTE_SAMPLES
    assert results and all(r["distance_from_route_km"] <= 5 for r in results)
    short = {"coordinates": [[72.0, 19.0], [72.3, 19.0]]}
    coverage2: dict = {}
    await here_places.find_route_places(short, "fuel", 5, 50, coverage=coverage2)
    assert coverage2["complete"] is True


@pytest.mark.asyncio
async def test_route_search_fails_only_when_every_sample_fails(monkeypatch):
    state = {"n": 0}

    async def flaky(lat, lon, spec, radius_km=None, bbox=None, limit=100):
        state["n"] += 1
        if state["n"] % 2 == 0:
            raise AppError("provider_unavailable")
        return [poi_item(f"{state['n']}", "Pump", lat + 0.001, lon, "Petrol Station", "700-7600-0116")]

    monkeypatch.setattr(here_places, "_browse", flaky)
    geometry = {"coordinates": [[72.0 + i * 0.05, 19.0] for i in range(120)]}
    coverage: dict = {}
    assert await here_places.find_route_places(geometry, "fuel", 5, 50, coverage=coverage)
    assert coverage["complete"] is False and coverage["failed_points"] >= 1

    async def always(lat, lon, spec, radius_km=None, bbox=None, limit=100):
        raise AppError("provider_unavailable")

    monkeypatch.setattr(here_places, "_browse", always)
    with pytest.raises(AppError):
        await here_places.find_route_places(geometry, "fuel", 5, 50)


# ---- routing ------------------------------------------------------------------------------------
HERE_VECTOR = "BFoz5xJ67i1B1B7PzIhaxL7Y"        # HERE's published flexible-polyline example (4 points)
HERE_VECTOR_POINTS = [(50.10228, 8.69821), (50.10201, 8.69567), (50.10063, 8.69150), (50.09878, 8.68752)]


def assert_same_points(actual, expected, tolerance=1e-9):
    """Compare two coordinate lists pairwise (pytest.approx does not accept nested sequences)."""
    assert len(actual) == len(expected)
    for a, e in zip(actual, expected):
        assert a[0] == pytest.approx(e[0], abs=tolerance) and a[1] == pytest.approx(e[1], abs=tolerance), (a, e)


def test_both_polyline_decoders_agree_with_the_published_here_vector():
    reference = routing.decode_flexible_polyline_reference(HERE_VECTOR)
    assert len(reference) == 4
    for (lon, lat), (elat, elon) in zip(reference, HERE_VECTOR_POINTS):
        assert (lat, lon) == pytest.approx((elat, elon), abs=1e-9)
    assert_same_points(routing.decode_flexible_polyline(HERE_VECTOR), reference)


def test_signed_value_decoding_follows_the_spec_not_a_negation():
    """Regression: `-(n >> 1)` was off by one for every negative delta, so routes drifted ~1 m per step."""
    assert [routing._decode_signed(n) for n in range(6)] == [0, -1, 1, -2, 2, -3]


def test_fallback_decoder_matches_the_official_package_on_random_routes():
    """Regression for polyline drift. The fallback exists for offline environments; it must not disagree."""
    flexpolyline = pytest.importorskip("flexpolyline")
    import random

    rng = random.Random(7)
    for _ in range(60):
        lat, lon = rng.uniform(8, 35), rng.uniform(68, 90)
        points = []
        for _ in range(rng.randint(2, 300)):
            lat += rng.uniform(-0.01, 0.01)
            lon += rng.uniform(-0.01, 0.01)
            points.append((round(lat, 5), round(lon, 5)))
        encoded = flexpolyline.encode(points)
        ours = [(la, lo) for lo, la in routing.decode_flexible_polyline_reference(encoded)]
        assert_same_points(ours, list(flexpolyline.decode(encoded)))


@pytest.mark.parametrize("bad", ["!!!", "BFoz5xJ67i1B1B7PzIhaxL7", "A"])
def test_corrupt_polylines_raise_instead_of_returning_garbage(bad):
    with pytest.raises(ValueError):
        routing.decode_flexible_polyline_reference(bad)


def section(polyline, length, duration):
    return {"polyline": polyline, "summary": {"length": length, "duration": duration}}


STOPS = [{"name": "A", "latitude": 50.10228, "longitude": 8.69821}, {"name": "B", "latitude": 50.09878, "longitude": 8.68752}]


def test_route_parsing_builds_distance_duration_legs_and_stop_positions():
    data = {"routes": [{"sections": [section(HERE_VECTOR, 120000, 3600)]}]}
    route = routing._parse_route(data, STOPS)
    assert route["distance_km"] == 120.0 and route["duration_minutes"] == 60.0
    assert len(route["geometry"]["coordinates"]) == 4 and route["geometry_validation"]["endpoint_check_passed"] is True
    assert route["stop_positions"][1]["distance_from_start_km"] == 120.0 and route["stop_positions"][1]["duration_from_start_minutes"] == 60.0
    assert route["transport_mode"] == "car" and route["native_ev_routing"] is False


def test_multi_section_routes_join_without_duplicating_the_shared_point():
    flexpolyline = pytest.importorskip("flexpolyline")
    a = [(50.0, 8.0), (50.1, 8.1), (50.2, 8.2)]
    b = [(50.2, 8.2), (50.3, 8.3), (50.4, 8.4)]           # starts exactly where `a` ends, as HERE sections do
    data = {"routes": [{"sections": [section(flexpolyline.encode(a), 1000, 60), section(flexpolyline.encode(b), 2000, 120)]}]}
    stops = [{"name": "A", "latitude": 50.0, "longitude": 8.0}, {"name": "V", "latitude": 50.2, "longitude": 8.2},
             {"name": "B", "latitude": 50.4, "longitude": 8.4}]
    route = routing._parse_route(data, stops)
    assert len(route["legs"]) == 2 and route["distance_km"] == 3.0
    assert len(route["geometry"]["coordinates"]) == 5, "the shared junction point must not be repeated"
    assert route["geometry"]["coordinates"][2] == [8.2, 50.2]
    assert route["here_flexible_polyline"] is None and len(route["here_flexible_polylines"]) == 2
    assert [p["distance_from_start_km"] for p in route["stop_positions"]] == [0, 1.0, 3.0]


@pytest.mark.parametrize("data", [{}, {"routes": []}, {"routes": [{"sections": []}]}, {"routes": [{"sections": [{"summary": {}}]}]}, []])
def test_unusable_route_responses_are_safe_errors(data):
    with pytest.raises(AppError) as caught:
        routing._parse_route(data, STOPS)
    assert caught.value.code in {"no_route", "provider_unavailable"}


def test_zero_length_route_is_not_reported_as_a_route():
    with pytest.raises(AppError):
        routing._parse_route({"routes": [{"sections": [section(HERE_VECTOR, 0, 0)]}]}, STOPS)


def test_endpoint_sanity_check_catches_a_misplaced_route():
    far = [{"name": "A", "latitude": 10.0, "longitude": 10.0}, {"name": "B", "latitude": 11.0, "longitude": 11.0}]
    route = routing._parse_route({"routes": [{"sections": [section(HERE_VECTOR, 1000, 60)]}]}, far)
    assert route["geometry_validation"]["endpoint_check_passed"] is False


@pytest.mark.parametrize("stops", [[], [STOPS[0]], STOPS * 6])
def test_stop_count_is_validated(monkeypatch, stops):
    monkeypatch.setattr(routing, "HERE_API_KEY", "x")
    with pytest.raises(AppError):
        routing._validate_stops(stops)


def test_coordinates_and_mode_are_validated(monkeypatch):
    monkeypatch.setattr(routing, "HERE_API_KEY", "x")
    for bad in ({"latitude": 95, "longitude": 0}, {"latitude": "a", "longitude": 0}, {"longitude": 0}, {"latitude": float("nan"), "longitude": 0}):
        with pytest.raises(AppError):
            routing._validate_stops([STOPS[0], {"name": "bad", **bad}])
    with pytest.raises(AppError):
        routing._validate_stops(STOPS, "helicopter")


def test_via_waypoints_keep_order_and_travel_mode_is_forwarded(monkeypatch):
    monkeypatch.setattr(routing, "HERE_API_KEY", "x")
    stops = [{"latitude": 1, "longitude": 1}, {"latitude": 2, "longitude": 2}, {"latitude": 3, "longitude": 3}, {"latitude": 4, "longitude": 4}]
    params = routing._build_params(stops, None, "pedestrian")
    assert [v for k, v in params if k == "via"] == ["2.0,2.0", "3.0,3.0"]
    assert dict(params)["origin"] == "1.0,1.0" and dict(params)["destination"] == "4.0,4.0" and dict(params)["transportMode"] == "pedestrian"


def test_ev_routing_parameters_use_herees_documented_names_and_are_off_by_default(monkeypatch):
    monkeypatch.setattr(routing, "HERE_API_KEY", "x")
    ev = {"battery_capacity_kwh": 50, "current_battery_percent": 80}
    monkeypatch.setattr(routing, "HERE_EV_ENABLED", False)
    assert not any(k.startswith("ev[") for k, _ in routing._build_params(STOPS, ev))
    monkeypatch.setattr(routing, "HERE_EV_ENABLED", True)
    names = [k for k, _ in routing._build_params(STOPS, ev)]
    assert "ev[initialCharge]" in names and "ev[maxCharge]" in names and "ev[makeReachable]" in names
    assert "ev[initialChargeInKWh]" not in names, "the pre-fix parameter names HERE rejected with HTTP 400"
    assert not any(k.startswith("ev[") for k, _ in routing._build_params(STOPS, ev, "pedestrian")), "EV routing only applies to cars"


@pytest.mark.asyncio
async def test_routing_http_errors_never_expose_the_response_body(monkeypatch):
    patch_request(monkeypatch, routing, lambda n, p: response(400, text="title: Invalid request cause: key=LEAKED-SECRET"))
    with pytest.raises(AppError) as caught:
        await routing.get_multi_stop_route(STOPS)
    assert caught.value.code == "provider_unavailable" and "LEAKED-SECRET" not in caught.value.message + repr(caught.value.detail)


@pytest.mark.asyncio
async def test_routing_success_end_to_end_with_a_mocked_response(monkeypatch):
    payload = {"routes": [{"sections": [section(HERE_VECTOR, 120000, 3600)]}]}
    calls = patch_request(monkeypatch, routing, lambda n, p: response(200, payload))
    route = await routing.get_multi_stop_route(STOPS)
    assert route["distance_km"] == 120.0 and calls[0]["params"]
