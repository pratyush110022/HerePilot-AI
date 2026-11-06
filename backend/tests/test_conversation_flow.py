"""End-to-end conversations through the orchestrator with every provider faked and AI disabled."""
from __future__ import annotations

import pytest

import fakes
import services.orchestrator as orchestrator
from services.errors import AppError
from services.trip_context import TripContext


@pytest.fixture
def world(monkeypatch):
    fakes.install(monkeypatch)
    return monkeypatch


async def say(context, text):
    return await orchestrator.handle_message(context, text)


def new_context():
    return TripContext(session_id="00000000-0000-4000-8000-000000000001")


# ---- ordinary journeys never touch the EV planner ---------------------------------------------
@pytest.mark.asyncio
async def test_general_trip_needs_no_vehicle_and_runs_no_ev_planner(world):
    context, result, reply = await say(new_context(), "Plan a trip from Mumbai to Bhubaneswar")
    assert context.trip_names() == ["Mumbai", "Bhubaneswar"]
    assert result["route"]["distance_km"] == 500.0
    assert context.ev_mode is False and context.ev_plan is None and context.chargers == []
    assert context.ev_status == "not_requested"
    assert "needs_vehicle" not in result and "ev_plan" not in result
    assert "battery" not in reply.lower() and "charging" not in reply.lower() and "EV" not in reply
    assert "Mumbai → Bhubaneswar" in reply and "500" in reply


@pytest.mark.asyncio
@pytest.mark.parametrize("fuel", ["petrol", "diesel", "hybrid"])
async def test_non_electric_journey_does_not_run_ev_planner_or_claim_fuel_calculations(world, fuel):
    context, result, reply = await say(new_context(), f"Plan a {fuel}-car journey from Mumbai to Bhubaneswar via Hyderabad")
    assert context.vehicle_type == fuel
    assert context.trip_names() == ["Mumbai", "Hyderabad", "Bhubaneswar"]
    assert context.ev_mode is False and context.ev_plan is None
    assert result["route"]["distance_km"] == 1000.0 and len(result["route"]["legs"]) == 2
    # It may say fuel isn't calculated, but must never state a fuel quantity or price.
    assert "aren't calculated" in reply
    import re
    assert not re.search(r"\d[\d,.]*\s*(?:l\b|litres?|liters?|gallons?|₹|rs\b|inr|\$)", reply.lower())
    assert "refuel" not in reply.lower() and "result" not in result.get("fuel", {})


# ---- "near X" is a search; "add X" changes the route -----------------------------------------
@pytest.mark.asyncio
async def test_add_stop_updates_route_waypoints_distance_and_eta(world):
    context, _, _ = await say(new_context(), "Plan a trip from Mumbai to Bhubaneswar")
    context, result, reply = await say(context, "add a stop at Hyderabad")
    assert context.trip_names() == ["Mumbai", "Hyderabad", "Bhubaneswar"]
    assert [s.role for s in context.stops] == ["origin", "waypoint", "destination"]
    assert result["route"]["distance_km"] == 1000.0
    assert result["route"]["duration_minutes"] == 1200.0
    assert result["action"] == "modify_trip" and "Added Hyderabad" in reply and "20h" in reply


@pytest.mark.asyncio
@pytest.mark.parametrize("phrase", ["Add Hyderabad to my route", "add a stop at Hyderabad on my route and recalculate the route",
                                    "include Hyderabad", "go via Hyderabad", "add Hyderabad to my current route and recalculate"])
async def test_phrasings_of_adding_a_waypoint_all_change_the_route(world, phrase):
    context, _, _ = await say(new_context(), "Plan a trip from Mumbai to Bhubaneswar")
    context, result, _ = await say(context, phrase)
    assert context.trip_names() == ["Mumbai", "Hyderabad", "Bhubaneswar"], phrase
    assert result["action"] == "modify_trip"


@pytest.mark.asyncio
@pytest.mark.parametrize("phrase", ["find restaurants near Hyderabad", "find places near Hyderabad", "hospitals within 5 km of Hyderabad"])
async def test_searching_near_a_city_never_edits_the_route(world, phrase):
    context, _, _ = await say(new_context(), "Plan a trip from Mumbai to Bhubaneswar")
    revision = context.route_revision
    context, result, _ = await say(context, phrase)
    assert context.trip_names() == ["Mumbai", "Bhubaneswar"], phrase
    assert result["action"] == "find_pois_near_location"
    assert context.route_revision == revision, "a search must not invalidate or recompute the route"
    assert context.route["distance_km"] == 500.0


@pytest.mark.asyncio
async def test_existing_stops_are_preserved_when_adding_and_removing(world):
    context, _, _ = await say(new_context(), "Plan a trip from Mumbai to Bhubaneswar via Pune")
    assert context.trip_names() == ["Mumbai", "Pune", "Bhubaneswar"]
    context, _, _ = await say(context, "add Nagpur")
    assert context.trip_names() == ["Mumbai", "Pune", "Nagpur", "Bhubaneswar"]
    context, result, _ = await say(context, "remove Pune")
    assert context.trip_names() == ["Mumbai", "Nagpur", "Bhubaneswar"]
    assert result["route"]["distance_km"] == 1000.0


@pytest.mark.asyncio
async def test_add_stop_after_a_named_stop_inserts_in_that_position(world):
    context, _, _ = await say(new_context(), "Plan a trip from Mumbai to Bhubaneswar via Pune")
    context, _, _ = await say(context, "add Nagpur after Mumbai")
    assert context.trip_names() == ["Mumbai", "Nagpur", "Pune", "Bhubaneswar"]


@pytest.mark.asyncio
async def test_a_stop_between_two_cities_asks_which_place_then_inserts_it(world):
    context, _, _ = await say(new_context(), "Plan a trip from Mumbai to Bhubaneswar via Pune")
    context, result, reply = await say(context, "Add a stop between Mumbai and Pune")
    assert result["action"] == "clarify" and "Which place" in reply
    assert context.trip_names() == ["Mumbai", "Pune", "Bhubaneswar"], "nothing changes until the user answers"
    context, result, _ = await say(context, "Nagpur")
    assert context.trip_names() == ["Mumbai", "Nagpur", "Pune", "Bhubaneswar"]


@pytest.mark.asyncio
async def test_route_changes_invalidate_results_computed_from_the_old_route(world):
    context, _, _ = await say(new_context(), "Plan a trip from Mumbai to Bhubaneswar")
    context, _, _ = await say(context, "find petrol pumps along my route")
    assert context.pois and context.pois_label
    context, _, _ = await say(context, "add Hyderabad")
    assert context.pois == [] and context.pois_label is None and context.focus is None


@pytest.mark.asyncio
async def test_failed_reroute_restores_the_previous_stops(monkeypatch):
    fakes.install(monkeypatch)
    context, _, _ = await say(new_context(), "Plan a trip from Mumbai to Bhubaneswar")
    before = context.trip_names()
    fakes.install(monkeypatch, calculate_route=fakes.failing_route)
    with pytest.raises(AppError) as caught:
        await say(context, "add Hyderabad")
    assert caught.value.code == "provider_unavailable"
    assert context.trip_names() == before, "a failed edit must not leave stops that have no route behind them"


@pytest.mark.asyncio
async def test_adding_a_stop_with_no_trip_explains_instead_of_searching(world):
    with pytest.raises(AppError) as caught:
        await say(new_context(), "add a stop at Hyderabad")
    assert caught.value.code == "needs_trip"


# ---- contextual follow-ups --------------------------------------------------------------------
@pytest.mark.asyncio
async def test_distance_and_eta_questions_use_the_active_route(world):
    context, _, _ = await say(new_context(), "Plan a trip from Mumbai to Bhubaneswar via Hyderabad")
    _, result, reply = await say(context, "What is the distance to the second stop?")
    assert result["action"] == "route_info" and result["distance_km"] == 500.0 and result["to"] == "Hyderabad"
    assert "Hyderabad" in reply and "500" in reply and "10h" in reply
    _, result, reply = await say(context, "how long will the whole trip take")
    assert result["distance_km"] == 1000.0 and "20h" in reply


@pytest.mark.asyncio
async def test_find_near_my_destination_searches_around_the_destination_coordinates(monkeypatch):
    seen = {}

    async def spy(points, category, radius_km, limit=80, search_query=None):
        seen["points"], seen["category"] = points, category
        return await fakes.nearby(points, category, radius_km, limit, search_query)

    fakes.install(monkeypatch, find_nearby_pois_with_source=spy)
    context, _, _ = await say(new_context(), "Plan a trip from Mumbai to Bhubaneswar")
    context, result, _ = await say(context, "Find a petrol pump near my destination")
    assert seen["category"] == "fuel"
    assert seen["points"] == [{"latitude": 20.2961, "longitude": 85.8245}]
    assert result["target"] == "Bhubaneswar"


@pytest.mark.asyncio
async def test_that_place_refers_to_the_last_search_centre_or_chosen_result(world):
    context, _, _ = await say(new_context(), "find hotels near Pune")
    context, result, _ = await say(context, "Find restaurants near that place")
    assert result["action"] == "find_pois" and result["scope"] == "focus"
    assert result["target"] == "Pune"


@pytest.mark.asyncio
async def test_numbered_result_reference_resolves_to_that_results_coordinates(monkeypatch):
    seen = {}

    async def spy(points, category, radius_km, limit=80, search_query=None):
        seen.setdefault("calls", []).append(points)
        return await fakes.nearby(points, category, radius_km, limit, search_query)

    fakes.install(monkeypatch, find_nearby_pois_with_source=spy)
    context, _, _ = await say(new_context(), "find hotels near Pune")
    second = context.pois[1]
    context, result, _ = await say(context, "find restaurants near the second result")
    assert seen["calls"][-1] == [{"latitude": second["latitude"], "longitude": second["longitude"]}]
    assert result["target"] == second["name"]


@pytest.mark.asyncio
async def test_unresolvable_reference_is_a_targeted_clarification_not_a_geocoder_query(world):
    context, _, _ = await say(new_context(), "Plan a trip from Mumbai to Bhubaneswar")
    with pytest.raises(AppError) as caught:
        await say(context, "find hotels near the seventh stop")
    assert caught.value.code == "reference_unresolved"
    assert "only 2 stops" in caught.value.message


@pytest.mark.asyncio
async def test_near_me_asks_for_a_location_instead_of_geocoding_the_word_me(world):
    _, result, reply = await say(new_context(), "find restaurants near me")
    assert result["action"] == "clarify" and "city or address" in reply


# ---- geocoding behaviour ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ambiguous_city_asks_which_one_and_does_not_guess(world):
    with pytest.raises(AppError) as caught:
        await say(new_context(), "Plan a trip from Aurangabad to Pune")
    assert caught.value.code == "place_ambiguous"
    assert "Maharashtra" in caught.value.message and "Bihar" in caught.value.message


@pytest.mark.asyncio
async def test_unknown_place_is_not_found_not_a_provider_error(world):
    with pytest.raises(AppError) as caught:
        await say(new_context(), "Plan a trip from Mumbai to Atlantis")
    assert caught.value.code == "place_not_found" and "Atlantis" in caught.value.message


@pytest.mark.asyncio
async def test_where_is_returns_a_location_and_sets_the_focus(world):
    context, result, reply = await say(new_context(), "Where is Pune?")
    assert result["action"] == "geocode" and result["results"][0]["latitude"] == 18.5204
    assert context.focus["name"].startswith("Pune") and "18.5204" in reply


# ---- optional EV Planner ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ev_planner_is_opt_in_and_asks_for_the_vehicle_when_missing(world):
    context, result, reply = await say(new_context(), "Plan an EV trip from Mumbai to Bhubaneswar")
    assert context.ev_mode is True and context.ev_status == "needs_vehicle"
    assert result["needs_vehicle"] is True and context.ev_plan is None
    assert "range" in reply and "battery" in reply


@pytest.mark.asyncio
async def test_ev_plan_runs_with_a_valid_vehicle_and_recomputes_without_rerouting(monkeypatch):
    calls = {"route": 0}

    async def counting_route(stops, ev=None, transport_mode="car"):
        calls["route"] += 1
        return await fakes.route(stops, ev, transport_mode)

    fakes.install(monkeypatch, calculate_route=counting_route)
    context, _, _ = await say(new_context(), "Plan a trip from Mumbai to Bhubaneswar")
    context, _, _ = await say(context, "find hotels near Pune")
    pois_before = list(context.pois)
    context, result, _ = await say(context, "My car has 400km range with 50 kWh battery currently at 90%")
    assert context.ev_mode and context.ev_status == "calculated" and context.ev_plan is not None
    assert calls["route"] == 1, "adding a vehicle must recompute the EV plan only, not re-route"
    assert context.pois == pois_before, "EV recomputation must not wipe the user's search results"
    context, result, _ = await say(context, "What if I start with 30%")
    assert context.vehicle.current_battery_percent == 30 and calls["route"] == 1
    assert result["ev_status"] == "calculated"


@pytest.mark.asyncio
async def test_zero_charging_stops_is_a_real_result_distinct_from_not_calculated(world):
    context, _, _ = await say(new_context(), "Plan a trip from Mumbai to Pune")
    assert context.ev_status == "not_requested"
    context, result, reply = await say(context, "My vehicle has 900km range with 60 kWh battery currently at 100%")
    assert context.ev_status == "calculated"
    assert context.ev_plan["trip_possible"] is True and context.ev_plan["charging_stops"] == []
    assert "no charging stop" in reply.lower()


@pytest.mark.asyncio
async def test_ev_failure_is_reported_as_failed_not_as_zero_stops(monkeypatch):
    fakes.install(monkeypatch)
    context, _, _ = await say(new_context(), "Plan a trip from Mumbai to Bhubaneswar")

    def boom(*args, **kwargs):
        raise ValueError("planner exploded")

    monkeypatch.setattr(orchestrator, "plan_ev_trip", boom)
    context, result, reply = await say(context, "My vehicle has 400km range with 50 kWh battery currently at 90%")
    assert context.ev_status == "failed" and context.ev_plan is None
    assert "couldn't calculate" in reply


@pytest.mark.asyncio
async def test_charging_stop_reference_resolves_only_against_the_active_ev_plan(world):
    context, _, _ = await say(new_context(), "Plan a trip from Mumbai to Bhubaneswar")
    with pytest.raises(AppError) as caught:
        await say(context, "find hotels near charging stop 2")
    assert caught.value.code == "reference_unresolved" and "EV Planner isn't active" in caught.value.message

    context, _, _ = await say(context, "My vehicle has 250km range with 50 kWh battery currently at 100%")
    assert len(context.ev_plan["charging_stops"]) >= 2
    chosen = context.ev_plan["charging_stops"][1]["charger"]
    context, result, _ = await say(context, "find hotels near charging stop 2")
    assert result["target"] == chosen["name"] and result["scope"] == "charging_stop"
    with pytest.raises(AppError) as caught:
        await say(context, "find hotels near charging stop 40")
    assert caught.value.code == "reference_unresolved"


@pytest.mark.asyncio
async def test_numbered_charging_stop_lookups_never_fall_through_to_a_recommendation(world):
    """Found in the browser: "Show charging stop 9" answered with a recommended stop instead of saying it doesn't exist."""
    context, _, _ = await say(new_context(), "Plan a trip from Mumbai to Bhubaneswar")
    context, _, _ = await say(context, "My vehicle has 250km range with 50 kWh battery currently at 100%")
    total = len(context.ev_plan["charging_stops"])
    assert total >= 2
    chosen = context.ev_plan["charging_stops"][1]["charger"]

    for phrase in ("Tell me about the second charging stop", "show charging stop 2", "what is charging stop number 2"):
        _, result, _ = await say(context, phrase)
        assert result["action"] == "show_charging_stop" and result["charging_stop_number"] == 2, phrase
        assert result["charger"]["name"] == chosen["name"]

    with pytest.raises(AppError) as caught:
        await say(context, "Show charging stop 9")
    assert caught.value.code == "reference_unresolved" and f"only {total}" in caught.value.message

    # A search around a numbered stop is still a search, not a lookup.
    _, result, _ = await say(context, "find hotels near charging stop 2")
    assert result["action"] == "find_pois" and result["scope"] == "charging_stop"


@pytest.mark.asyncio
async def test_hotels_near_a_charging_station_search_around_that_stations_coordinates(monkeypatch):
    seen = {}

    async def spy(points, category, radius_km, limit=80, search_query=None):
        seen["points"] = points
        return await fakes.nearby(points, category, radius_km, limit, search_query)

    fakes.install(monkeypatch, find_nearby_pois_with_source=spy)
    context, _, _ = await say(new_context(), "Plan a trip from Mumbai to Bhubaneswar")
    context, _, _ = await say(context, "My vehicle has 250km range with 50 kWh battery currently at 100%")
    station = context.ev_plan["charging_stops"][0]["charger"]
    await say(context, f"share hotels near {station['name']}")
    assert seen["points"] == [{"latitude": station["latitude"], "longitude": station["longitude"]}]


@pytest.mark.asyncio
async def test_ev_charging_search_is_refused_while_the_ev_planner_is_off(world):
    context, _, _ = await say(new_context(), "Plan a trip from Mumbai to Bhubaneswar")
    with pytest.raises(AppError) as caught:
        await say(context, "share all charger details on my route")
    assert caught.value.code == "invalid_request" and "EV Planner" in caught.value.message


@pytest.mark.asyncio
async def test_turning_ev_off_removes_every_ev_result_but_keeps_the_route(world):
    context, _, _ = await say(new_context(), "Plan a trip from Mumbai to Bhubaneswar")
    result, reply = await orchestrator.configure_ev(context, enabled=True, vehicle={"max_range_km": 250, "current_battery_percent": 100})
    assert context.ev_status == "calculated" and context.chargers
    result, reply = await orchestrator.configure_ev(context, enabled=False)
    assert context.ev_mode is False and context.ev_plan is None and context.chargers == []
    assert context.route and context.trip_names() == ["Mumbai", "Bhubaneswar"]
    assert "battery" not in reply.lower().replace("no charging information", "")


# ---- general questions / help -----------------------------------------------------------------
@pytest.mark.asyncio
async def test_help_describes_the_assistant_and_calls_ev_optional(world):
    _, result, reply = await say(new_context(), "hello")
    assert result["action"] == "help" and "optional" in reply.lower() and "restaurants" in reply


@pytest.mark.asyncio
async def test_general_question_without_ai_says_so_honestly(world):
    _, result, reply = await say(new_context(), "What is the capital of Karnataka?")
    assert result["action"] == "general_answer" and result["answer"] is None
    assert "AI service" in reply


@pytest.mark.asyncio
async def test_general_question_with_ai_is_labelled_as_general_knowledge(world, fake_llm):
    fake_llm(lambda system, user: "Bengaluru is the capital of Karnataka." if "geography" in system else "{}")
    _, result, reply = await say(new_context(), "What is the capital of Karnataka?")
    assert result["answer_source"] == "ai_general_knowledge" and "Bengaluru" in reply


# ---- travel mode ------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_walking_directions_use_pedestrian_mode_and_say_walking(monkeypatch):
    modes = []

    async def spy(stops, ev=None, transport_mode="car"):
        modes.append(transport_mode)
        return await fakes.route(stops, ev, transport_mode)

    fakes.install(monkeypatch, calculate_route=spy)
    context, _, reply = await say(new_context(), "walking directions from Mumbai to Pune")
    assert modes == ["pedestrian"] and context.travel_mode == "pedestrian" and "walking" in reply


# ---- wording ----------------------------------------------------------------------------------
def test_durations_round_half_up_like_the_web_ui():
    """Python's round() is banker's rounding: 1348.5 -> 1348, the browser's Math.round gives 1349."""
    assert orchestrator._fmt_minutes(1348.5) == "22h 29m"
    assert orchestrator._fmt_minutes(0.4) == "0 min" and orchestrator._fmt_minutes(59.5) == "1h"
    assert orchestrator._fmt_minutes(None) == "—"


def test_category_nouns_read_naturally():
    assert orchestrator._category_noun("fuel", 3) == "petrol pumps"
    assert orchestrator._category_noun("hotel", 1) == "hotel" and orchestrator._category_noun("hotel", 5) == "hotels"
    assert orchestrator._category_noun("pharmacy", 2) == "pharmacies"
    assert orchestrator._category_noun("tourist_attraction", 2) == "tourist attractions"


# ---- the model never states geography: HERE results are authoritative ----------------------------
@pytest.mark.asyncio
async def test_route_and_search_replies_never_come_from_the_model(world, fake_llm):
    """With AI on, a model that tries to state its own distance must not change what the user is told."""
    def responder(system, user):
        if "intent parser" in system:
            return "{}"  # no usable intent: the rules decide
        return "It is exactly 9999 km and takes 3 hours, with 77 great hotels."  # would be an invented answer

    provider = fake_llm(responder)
    context, result, reply = await say(new_context(), "Plan a trip from Mumbai to Bhubaneswar")
    assert "9999" not in reply and "500" in reply and result["route"]["distance_km"] == 500.0
    context, _, reply = await say(context, "add a stop at Hyderabad")
    assert "9999" not in reply
    context, _, reply = await say(context, "How far is my destination?")
    assert "9999" not in reply
    asked_for_prose = [c for c in provider.calls if "Explain the VERIFIED RESULT" in c["system"]]
    assert asked_for_prose == [], "no route/search/answer text may be delegated to the model"


@pytest.mark.asyncio
async def test_followup_answer_is_used_only_when_its_numbers_come_from_the_results(world, fake_llm):
    state = {"answer": ""}

    def responder(system, user):
        return "{}" if "intent parser" in system else state["answer"]

    fake_llm(responder)
    context, result, _ = await say(new_context(), "Find hotels near Pune")
    assert result["pois"], "the fakes return places"
    nearest = result["pois"][0]["name"]
    km = result["pois"][0].get("distance_from_reference_km")

    state["answer"] = f"The closest is {nearest}, {km} km away."
    _, result, reply = await say(context, "Which of these is closest?")
    assert result["action"] == "follow_up" and nearest in reply and str(km) in reply

    state["answer"] = f"The closest is {nearest}, only 7777.7 km away."
    _, _, reply = await say(context, "Which of these is closest?")
    assert "7777" not in reply, "an invented distance must be dropped in favour of the deterministic reply"
    assert "couldn't answer that" in reply


def test_numbers_grounded_accepts_roundings_and_duration_parts_and_rejects_inventions():
    from services.llm_assistant import numbers_grounded

    view = {"route": {"distance_km": 1644.89, "duration_minutes": 1345.6}, "places": [{"n": 1, "name": "Hotel 24", "km_away": 0.512}]}
    assert numbers_grounded("About 1,645 km, or 1644.9 km.", view)
    assert numbers_grounded("Around 22 hours 26 minutes (22.4 hours).", view)
    assert numbers_grounded("1. Hotel 24 is 0.5 km away.", view)
    assert numbers_grounded("You asked for 5 km.", view, "restaurants within 5 km")
    assert not numbers_grounded("It is 2,000 km.", view)
    assert not numbers_grounded("Hotel 24 is 0.9 km away.", view)
    assert not numbers_grounded("About 30 hours.", view)
    assert numbers_grounded("No figures here.", view)
