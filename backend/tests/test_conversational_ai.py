"""Open questions, named places, "nearest", follow-ups, and a route that stays put.

Everything external (HERE, AWS) is faked: no Bedrock call is made and nothing here is billable. The scripted model
below stands in for Nova Pro and is allowed to misbehave, because the point is what the app does with its output.
"""
from __future__ import annotations

import json

import pytest

import fakes
import services.orchestrator as orchestrator
from services import conversation, text_safety
from services.errors import AppError
from services.geo_utils import dedupe_places, place_identity
from services.intent_parser import explicit_route_request, parse
from services.intents import validate_intent
from services.trip_context import TripContext

fastapi_testclient = pytest.importorskip("fastapi.testclient")


@pytest.fixture
def world(monkeypatch):
    fakes.install(monkeypatch)
    fakes.HIGHLIGHT_CALLS.clear()
    fakes.SUBJECT_CALLS.clear()
    return monkeypatch


def new_context():
    return TripContext(session_id="00000000-0000-4000-8000-000000000099")


async def say(context, text):
    return await orchestrator.handle_message(context, text)


def scripted(intent=None, doc=None, guide=None):
    """A stand-in for the model: ``intent`` for the intent prompt, ``doc`` for the answer prompt, ``guide`` for guides."""
    def respond(system, user):
        if "intent parser" in system:
            return json.dumps(intent) if intent is not None else "{}"
        if "reasoning engine" in system:
            return json.dumps(doc) if doc is not None else "{}"
        if "travel guidance" in system:
            return json.dumps(guide) if guide is not None else "{}"
        return "ok"
    return respond


def answer_calls(provider):
    return [c for c in provider.calls if "reasoning engine" in c["system"]]


async def trip(context=None):
    context = context or new_context()
    context, _, _ = await say(context, "Plan a trip from Mumbai to Bhubaneswar")
    return context


def route_state(context):
    return (context.trip_names(), context.route_revision, context.route["distance_km"], len(context.route["geometry"]["coordinates"]))


# ======================================================================================================
# What the rules read
# ======================================================================================================
@pytest.mark.parametrize("text, intent, key, value", [
    ("Where is Kempegowda International Airport?", "geocode", "place", "Kempegowda International Airport"),
    ("where's the Taj Mahal", "geocode", "place", "the Taj Mahal"),
    ("nearest airport from Puri", "find_pois_near_location", "place", "Puri"),
    ("Which is the closest airport to Puri?", "find_pois_near_location", "place", "Puri"),
    ("nearest cafe to me", "find_pois", "scope", "me"),
    ("nearest hospital to my destination", "find_pois", "scope", "destination"),
    ("Tell me more about Puri Jagannath Temple", "ask", "subjects", ["Puri Jagannath Temple"]),
    ("Compare Coorg and Mysuru for a three-day trip", "ask", "subjects", ["Coorg", "Mysuru"]),
    ("Coorg vs Mysuru", "ask", "subjects", ["Coorg", "Mysuru"]),
    ("pros and cons of living in Pune", "ask", "subjects", ["Pune"]),
])
@pytest.mark.parametrize("with_trip", [False, True])
def test_names_and_comparisons_are_read_the_same_with_or_without_a_trip_on_screen(text, intent, key, value, with_trip):
    context = {"stops": [{"name": "Mumbai"}, {"name": "Bhubaneswar"}], "route": {"distance_km": 1500}, "pois": []} if with_trip else {}
    action = parse(text, context)
    assert action["intent"] == intent and action[key] == value, "a trip on screen must not turn these into a search along the trip"


@pytest.mark.parametrize("text", [
    "where is the nearest petrol pump", "where is my destination", "tell me more about it", "tell me more about the second one",
    "What is the capital of Karnataka?", "airport near Puri",
])
def test_phrases_that_are_not_asking_about_a_named_place_are_left_alone(text):
    assert parse(text, {"pois": [{"name": "A"}]})["intent"] not in {"ask"} and parse(text, {})["intent"] != "ask"


@pytest.mark.parametrize("text, explicit", [
    ("Plan a trip from Pune to Goa", True), ("Add a stop at Hyderabad", True), ("remove Pune from my route", True), ("go via Nagpur", True),
    ("Find coffee near Pune", False), ("Compare Coorg and Mysuru for a three-day trip", False), ("airports in Odisha", False), ("Tell me about the Konark temple", False),
])
def test_only_an_explicit_request_counts_as_asking_to_change_a_route(text, explicit):
    assert explicit_route_request(text) is explicit


def test_the_ask_intent_is_validated_like_every_other_intent():
    assert validate_intent({"intent": "ask", "subjects": ["  Coorg ", "coorg", "Mysuru", "A", "B", "C"]}) == {"intent": "ask", "subjects": ["Coorg", "Mysuru", "A"]}
    assert validate_intent({"intent": "ask", "subjects": "Coorg"}) == {"intent": "ask", "subjects": []}
    assert validate_intent({"intent": "ask"})["subjects"] == []


# ======================================================================================================
# Named places and "nearest" (the airport defects)
# ======================================================================================================
@pytest.mark.asyncio
async def test_where_is_a_named_place_finds_that_place_without_asking_where_to_search(world):
    context, result, reply = await say(new_context(), "Where is Kempegowda International Airport?")
    assert result["action"] == "geocode" and result["named_place"]["name"] == "Kempegowda International Airport"
    assert "Devanahalli, Bengaluru" in reply and "Where should I search" not in reply
    assert context.pois == result["pois"] and context.focus["name"] == "Kempegowda International Airport"


@pytest.mark.asyncio
async def test_where_is_a_named_place_never_reuses_or_changes_the_trip_on_screen(world):
    searched = {"route": 0}

    async def route_spy(*args, **kwargs):
        searched["route"] += 1
        return await fakes.route_pois(*args, **kwargs)

    fakes.install(world, find_route_pois_with_source=route_spy)
    context = await trip()
    before = route_state(context)
    context, result, _ = await say(context, "Where is Kempegowda International Airport?")
    assert result["action"] == "geocode" and [p["name"] for p in result["pois"]] == ["Kempegowda International Airport"]
    assert searched["route"] == 0, "no search along the Mumbai - Bhubaneswar route"
    assert route_state(context) == before


@pytest.mark.asyncio
async def test_a_model_cannot_turn_a_named_place_question_into_a_route_search(world, fake_llm):
    fake_llm(scripted(intent={"intent": "find_pois", "category": "airport", "scope": "route"}))
    context = await trip()
    action, source = await orchestrator._interpret_message("Where is Kempegowda International Airport?", context)
    assert action["intent"] == "geocode" and source == "rules_llm_differ"


@pytest.mark.asyncio
async def test_the_nearest_airport_widens_the_search_until_one_is_found(world):
    radii = []

    async def sparse(points, category, radius_km, limit=80, search_query=None):
        radii.append(radius_km)
        if radius_km < 50:
            return {"pois": [], "source_status": "empty", "source": "HERE Search API", "source_error": None}
        return {"pois": [{"name": "Biju Patnaik International Airport", "category": "airport", "latitude": 20.244, "longitude": 85.818, "here_id": "here:bp",
                          "distance_from_reference_km": 49.7}], "source_status": "verified", "source": "HERE Search API", "source_error": None}

    fakes.install(world, find_nearby_pois_with_source=sparse)
    context = await trip()
    before = route_state(context)
    context, result, reply = await say(context, "nearest airport from Puri")
    assert radii == [30.0, 60.0], "the airport default (30 km), then doubled"
    assert result["action"] == "find_pois_near_location" and result["scope"] == "location" and result["nearest"] is True
    assert "Biju Patnaik International Airport" in reply and "49.7 km" in reply and "straight line" in reply and "not a driving distance" in reply
    assert route_state(context) == before, "a search near Puri leaves the Mumbai - Bhubaneswar route alone"


@pytest.mark.asyncio
async def test_nearest_stops_widening_at_a_ceiling_and_says_nothing_was_found(world):
    radii = []

    async def none_found(points, category, radius_km, limit=80, search_query=None):
        radii.append(radius_km)
        return {"pois": [], "source_status": "empty", "source": "HERE Search API", "source_error": None}

    fakes.install(world, find_nearby_pois_with_source=none_found)
    _, result, reply = await say(new_context(), "nearest airport from Puri")
    assert radii[-1] == orchestrator.NEAREST_MAX_KM and len(radii) <= orchestrator.NEAREST_MAX_STEPS
    assert result["pois"] == [] and "I found no airports" in reply


# ======================================================================================================
# A route changes only when it is asked to
# ======================================================================================================
@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "nearest airport from Puri", "Where is Kempegowda International Airport?", "Tell me more about Puri Jagannath Temple",
    "Compare Ooty and Bhubaneswar for a three-day trip", "Find restaurants near Pune", "best time to visit Ooty?", "airports in Odisha",
    "Find a coffee shop near the halfway point of my trip", "What is the capital of Karnataka?",
])
async def test_searches_and_questions_never_change_the_route(world, text):
    context = await trip()
    before = route_state(context)
    context, _, _ = await say(context, text)
    assert route_state(context) == before


@pytest.mark.asyncio
async def test_a_model_that_edits_the_route_when_asked_to_search_is_overruled(world, fake_llm):
    fake_llm(scripted(intent={"intent": "modify_trip", "operation": "add_stop", "place": "Pune"}))
    context = await trip()
    before = route_state(context)
    context, result, _ = await say(context, "Find coffee shops around Pune")
    assert result["action"] == "find_pois_near_location" and route_state(context) == before and "Pune" not in context.trip_names()


@pytest.mark.asyncio
async def test_a_model_that_plans_a_trip_when_asked_an_open_question_is_overruled(world, fake_llm):
    fake_llm(scripted(intent={"intent": "plan_trip", "places": ["Coorg", "Mysuru"]}))
    context = await trip()
    before = route_state(context)
    context, result, _ = await say(context, "Compare Ooty and Bhubaneswar for a three-day trip")
    assert result["action"] == "ask" and route_state(context) == before


@pytest.mark.asyncio
async def test_an_explicit_route_edit_still_works(world, fake_llm):
    fake_llm(scripted(intent={"intent": "modify_trip", "operation": "add_stop", "place": "Hyderabad"}))
    context = await trip()
    context, result, _ = await say(context, "add Hyderabad to my route")
    assert result["action"] == "modify_trip" and "Hyderabad" in context.trip_names()


# ======================================================================================================
# Open questions: the model reasons and picks the format, HERE supplies the places
# ======================================================================================================
DOC = {
    "answer": "Coorg suits a slow, scenic break; Mysuru suits a short, culture-heavy one.",
    "blocks": [
        {"type": "pros_cons", "title": "Coorg", "pros": ["Quiet coffee estates", "Cool weather for much of the year"], "cons": ["Roads can be slow in the rains", "Fewer big sights"]},
        {"type": "compare", "columns": ["Ooty", "Bhubaneswar"], "rows": [{"label": "Best for", "values": ["Hill scenery", "Temples and culture"]}]},
        {"type": "places", "title": "Worth seeing", "place_ids": ["p1", "p2", "p99", "p1"]},
        {"type": "itinerary", "days": [{"title": "Day 1", "items": ["Settle in", "Walk the market"]}]},
        {"type": "mystery", "text": "an unknown block type"},
    ],
    "caveats": ["Crowds and weather change by week, so check before you go."],
    "suggestions": ["Which is better for kids?", "What should I pack?"],
}


@pytest.mark.asyncio
async def test_an_open_question_gets_a_reasoned_answer_in_a_format_the_model_chose(world, fake_llm):
    provider = fake_llm(scripted(intent={"intent": "ask", "subjects": ["Ooty", "Bhubaneswar"]}, doc=DOC))
    context, result, reply = await say(new_context(), "Compare Ooty and Bhubaneswar for a three-day trip")
    doc = result["answer_doc"]
    assert result["action"] == "ask" and reply == DOC["answer"]
    assert [b["type"] for b in doc["blocks"]] == ["pros_cons", "compare", "places", "itinerary"], "an unknown block type is dropped, the rest keep their order"
    assert doc["caveats"] and doc["suggestions"] == DOC["suggestions"]
    assert [s["name"] for s in result["subjects"]] == ["Ooty", "Bhubaneswar"] and result["answer_source"] == "ai_general_knowledge"
    assert len(answer_calls(provider)) == 1, "one reasoning call per question"


@pytest.mark.asyncio
async def test_the_answer_can_only_point_at_places_here_returned(world, fake_llm):
    fake_llm(scripted(intent={"intent": "ask", "subjects": ["Ooty"]}, doc=DOC))
    context, result, _ = await say(new_context(), "Compare Ooty and Bhubaneswar for a three-day trip")
    block = next(b for b in result["answer_doc"]["blocks"] if b["type"] == "places")
    names = [p["name"] for p in result["pois"]]
    assert block["place_indexes"] == [0, 1], "p99 was never offered and p1 repeats"
    assert all(i < len(names) for i in block["place_indexes"]) and names[:2] == ["Government Rose Garden", "Otty Lake"]
    assert context.pois == result["pois"], "the places shown are the places on the map and the ones follow-ups refer to"


@pytest.mark.asyncio
async def test_a_named_place_is_pinned_first_and_comes_from_here_not_the_model(world, fake_llm):
    doc = {"answer": "It is one of the four dhams, dedicated to Jagannath.", "blocks": [
        {"type": "facts", "items": [{"label": "Dedicated to", "value": "Jagannath"}, {"label": "Open", "value": "6 am to 9 pm daily"}, {"label": "Rated", "value": "4.9 by 8000 reviews"}]}]}
    fake_llm(scripted(intent={"intent": "ask", "subjects": ["Puri Jagannath Temple"]}, doc=doc))
    _, result, reply = await say(new_context(), "Tell me more about Puri Jagannath Temple")
    assert result["pois"][0]["name"] == "Shri Jagannath Temple" and result["subjects"][0] == {"name": "Puri Jagannath Temple", "kind": "place", "poi_index": 0}
    assert result["pois"][0]["rating"] == 4.6 and result["pois"][0]["rating_count"] == 5200, "rating and reviews are HERE's, never the model's"
    facts = result["answer_doc"]["blocks"][0]["items"]
    assert facts == [{"label": "Dedicated to", "value": "Jagannath"}], "opening hours and invented ratings are removed"
    assert "4.9" not in json.dumps(result["answer_doc"]) and "8000" not in json.dumps(result["answer_doc"])


@pytest.mark.asyncio
async def test_figures_the_model_cannot_know_are_removed_but_grounded_ones_stay(world, fake_llm):
    doc = {"answer": "Ooty is cool and green. It is 25°C in May. Rooms cost ₹4000 a night. Government Rose Garden is 0.9 km from the centre.",
           "blocks": [{"type": "list", "items": ["Mysuru is 160 km away", "The lake is 0.8 km from the centre", "Built in the 1820s", "Rated 4.5 by travellers", "Open 9:00 to 18:00"]}]}
    fake_llm(scripted(intent={"intent": "ask", "subjects": ["Ooty"]}, doc=doc))
    _, result, reply = await say(new_context(), "Tell me about Ooty")
    assert "°" not in reply and "₹" not in reply and "Ooty is cool and green." in reply
    assert result["answer_doc"]["blocks"][0]["items"] == ["Built in the 1820s"] or "Built in the 1820s" in result["answer_doc"]["blocks"][0]["items"]
    items = result["answer_doc"]["blocks"][0]["items"]
    assert "Mysuru is 160 km away" not in items, "a distance the map service never supplied"
    assert not any("Rated" in i or "Open 9" in i for i in items)


@pytest.mark.asyncio
async def test_the_model_sees_names_and_ids_but_never_coordinates_links_or_review_counts(world, fake_llm):
    provider = fake_llm(scripted(intent={"intent": "ask", "subjects": ["Puri Jagannath Temple"]}, doc={"answer": "ok"}))
    await say(new_context(), "Tell me more about Puri Jagannath Temple")
    [call] = answer_calls(provider)
    payload = json.loads(call["user"])
    assert payload["PLACES"][0]["id"] == "p1" and payload["PLACES"][0]["name"] == "Shri Jagannath Temple" and payload["PLACES"][0]["reviews"] == "many"
    sent = call["user"]
    for secret in ("19.8049", "85.818", "tripadvisor", "http", "5200", "jag.jpg"):
        assert secret not in sent
    assert "untrusted" in call["system"] and "NO live data" in call["system"]


@pytest.mark.asyncio
async def test_a_general_question_with_no_places_still_gets_a_structured_answer(world, fake_llm):
    doc = {"answer": "Banking and legal rules differ by state.", "blocks": [{"type": "list", "items": ["Check local rules"]}], "caveats": ["Rules change; verify with an official source."]}
    fake_llm(scripted(intent={"intent": "general_question"}, doc=doc))
    context, result, reply = await say(new_context(), "Why do mountain towns get cooler at night?")
    assert result["action"] == "ask" and result["pois"] == [] and reply == doc["answer"] and result["answer_doc"]["caveats"]
    assert context.pois == [], "an answer with no places leaves the map alone"


@pytest.mark.asyncio
async def test_an_unusable_model_answer_falls_back_to_the_plain_general_answer(world, fake_llm):
    fake_llm(lambda system, user: "Bengaluru is the capital of Karnataka." if "geography" in system else json.dumps({"intent": "general_question"}) if "intent parser" in system else "not json")
    _, result, reply = await say(new_context(), "What is the capital of Karnataka?")
    assert result["action"] == "general_answer" and "Bengaluru" in reply and result["answer_source"] == "ai_general_knowledge"


@pytest.mark.asyncio
async def test_without_the_ai_an_open_question_shows_what_here_verified_and_does_not_guess(world):
    context, result, reply = await say(new_context(), "Tell me more about Puri Jagannath Temple")
    assert result["action"] == "ask" and result["answer_doc"] is None and result["ai_available"] is False
    assert result["pois"][0]["name"] == "Shri Jagannath Temple" and result["source_status"] == "verified"
    assert "Here is what HERE lists for Puri Jagannath Temple" in reply and "haven't guessed" in reply


@pytest.mark.asyncio
async def test_a_name_here_does_not_know_is_reported_not_invented(world):
    _, result, reply = await say(new_context(), "Tell me more about Zzyzx Palace")
    assert result["subjects"] == [{"name": "Zzyzx Palace", "kind": "unknown"}] and result["pois"] == []
    assert "couldn't find “Zzyzx Palace”" in reply


@pytest.mark.asyncio
async def test_an_ambiguous_single_subject_is_asked_about(world, monkeypatch):
    async def ambiguous(query, near=None):
        return {"kind": "area", "area": {"name": "Aurangabad", "center": {"latitude": 19.9, "longitude": 75.3}},
                "ambiguous": [{"display_name": "Aurangabad, Maharashtra, India"}, {"display_name": "Aurangabad, Bihar, India"}]}

    fakes.install(world, resolve_subject=ambiguous)
    with pytest.raises(AppError) as caught:
        await say(new_context(), "Tell me about Aurangabad")
    assert caught.value.code == "place_ambiguous"


@pytest.mark.asyncio
async def test_a_here_outage_does_not_stop_the_model_answering(world, fake_llm):
    async def down(query, near=None):
        raise AppError("provider_unavailable")

    fakes.install(world, resolve_subject=down)
    fake_llm(scripted(intent={"intent": "ask", "subjects": ["Coorg"]}, doc={"answer": "Coorg is a hill district known for coffee.", "caveats": ["I could not load places right now."]}))
    _, result, reply = await say(new_context(), "Tell me about Coorg")
    assert reply.startswith("Coorg is a hill district") and result["pois"] == [] and result["source_status"] == "unavailable"


@pytest.mark.asyncio
async def test_the_model_reading_an_open_question_overrides_the_guide_template_but_not_the_place(world, fake_llm):
    fake_llm(scripted(intent={"intent": "ask", "subjects": ["Ooty"]}, doc={"answer": "Ooty is worth it for hill scenery.", "blocks": [{"type": "pros_cons", "pros": ["Cool"], "cons": ["Crowded in May"]}]}))
    _, result, _ = await say(new_context(), "Is Ooty worth visiting? Explain the pros and cons.")
    assert result["action"] == "ask" and result["answer_doc"]["blocks"][0]["type"] == "pros_cons"

    fake_llm(scripted(intent={"intent": "destination_guide", "place": "Ooty", "topic": "best_time"}))
    action, source = await orchestrator._interpret_message("things to do in Odisha", new_context())
    assert action["intent"] == "destination_guide" and action["place"] == "Odisha", "a model naming another place never overrides the rules"


# ======================================================================================================
# Follow-ups
# ======================================================================================================
@pytest.mark.asyncio
async def test_a_follow_up_is_answered_from_the_conversation_and_the_places_on_screen(world, fake_llm):
    doc = {"answer": "The first one is the better fit for a short stop.", "blocks": [{"type": "places", "title": "Best fit", "place_ids": ["p1"]}]}
    provider = fake_llm(scripted(intent={"intent": "follow_up_question"}, doc=doc))
    context, first, _ = await say(new_context(), "airports in Odisha")
    on_screen = [dict(p) for p in context.pois]
    context, result, reply = await say(context, "Which of those is better for a short stop?")
    assert result["action"] == "ask" and reply == doc["answer"]
    assert result["pois"] == on_screen == context.pois, "a follow-up never reorders or replaces the places the user is looking at"
    assert result["answer_doc"]["blocks"][0]["place_indexes"] == [0]
    payload = json.loads(answer_calls(provider)[0]["user"])
    assert payload["MEMORY"][0]["user"] == "airports in Odisha" and payload["MEMORY"][0]["shown"] == ["Airport 1", "Airport 2"]
    assert [p["name"] for p in payload["PLACES"]] == ["Airport 1", "Airport 2"]


@pytest.mark.asyncio
async def test_the_conversation_memory_is_bounded_private_and_never_sent_to_the_browser(world):
    context = new_context()
    for i in range(10):
        context, _, _ = await say(context, f"find {i + 1} hotels near Pune")
    assert len(context.memory) == orchestrator.MAX_MEMORY_TURNS
    assert context.memory[-1]["user"] == "find 10 hotels near Pune" and "memory" not in context.to_dict()


@pytest.mark.asyncio
async def test_a_follow_up_with_nothing_to_refer_to_does_not_pretend(world):
    _, result, reply = await say(new_context(), "Tell me more about the second one")
    assert result["action"] == "general_answer" and "AI service" in reply


# ======================================================================================================
# Each place once, and replies that do not repeat the cards
# ======================================================================================================
def test_places_are_identified_by_here_id_and_distinct_places_at_one_spot_are_kept():
    a = {"name": "Hotel A", "latitude": 15.5, "longitude": 73.7, "here_id": "here:1"}
    b = {"name": "Hotel B", "latitude": 15.5, "longitude": 73.7, "here_id": "here:2"}   # same coordinates, different place
    a_again = {"name": "Hotel A (second copy)", "latitude": 15.6, "longitude": 73.8, "here_id": "HERE:1"}
    no_id_1 = {"name": "Cafe", "latitude": 15.50001, "longitude": 73.70001}
    no_id_2 = {"name": "cafe ", "latitude": 15.50001, "longitude": 73.70001}
    assert dedupe_places([a, b, a_again, no_id_1, no_id_2, "junk"]) == [a, b, no_id_1]
    assert place_identity(a) != place_identity(b) and place_identity(a) == place_identity(a_again)


@pytest.mark.asyncio
async def test_a_provider_that_repeats_a_place_does_not_show_it_twice(world):
    async def repeated(points, category, radius_km, limit=80, search_query=None):
        base = await fakes.nearby(points, category, radius_km, limit, search_query)
        pois = base["pois"]
        pois[1]["here_id"] = pois[0]["here_id"] = "here:same"
        pois[2]["here_id"] = "here:other"
        return base

    fakes.install(world, find_nearby_pois_with_source=repeated)
    context, result, reply = await say(new_context(), "find hotels near Pune")
    assert [p["name"] for p in result["pois"]] == ["Hotel 1", "Hotel 3"] and context.pois == result["pois"]
    assert "Found 2 hotels" in reply


@pytest.mark.asyncio
async def test_replies_summarise_and_the_cards_carry_the_places(world):
    for text in ("find restaurants near Pune", "airports in Odisha", "hospitals within 5 km of Pune"):
        _, result, reply = await say(new_context(), text)
        assert result["pois"] and not any(p["name"] in reply for p in result["pois"]), text
        assert "\n1." not in reply and "1. " not in reply, "no numbered list"


# ======================================================================================================
# Location the user chose to share
# ======================================================================================================
@pytest.fixture
def client(monkeypatch):
    import main

    main.store._contexts.clear()
    fakes.install(monkeypatch)
    return fastapi_testclient.TestClient(main.app, raise_server_exceptions=False)


def test_near_me_asks_for_permission_then_works_then_stops_when_sharing_stops(client):
    ask = client.post("/chat", json={"message": "Find coffee shops near me"}).json()
    session = ask["session_id"]
    assert ask["result"]["needs_location"] is True and ask["context"].get("user_location") is None

    here = {"latitude": 12.9716, "longitude": 77.5946, "accuracy_m": 12}
    found = client.post("/chat", json={"message": "Find coffee shops near me", "session_id": session, "location": here}).json()
    assert found["result"]["scope"] == "me" and found["result"]["pois"] and found["context"]["user_location"]["latitude"] == 12.9716

    # Stopping takes effect on the server immediately, not at the next message.
    assert client.delete(f"/session/{session}/location").json() == {"forgotten": True}
    assert client.get(f"/session/{session}").json().get("user_location") is None
    again = client.post("/chat", json={"message": "Find coffee shops near me", "session_id": session}).json()
    assert again["result"]["needs_location"] is True


def test_stopping_location_sharing_is_safe_for_unknown_or_invalid_sessions(client):
    assert client.delete("/session/not-a-session/location").status_code == 400
    assert client.delete("/session/00000000-0000-4000-8000-0000000000aa/location").json() == {"forgotten": False}


def test_a_bad_location_is_rejected_and_the_private_memory_never_leaks(client):
    assert client.post("/chat", json={"message": "hi", "location": {"latitude": 123, "longitude": 5}}).status_code == 422
    body = client.post("/chat", json={"message": "find hotels near Pune"}).json()
    assert "memory" not in body["context"] and "memory" not in client.get(f"/session/{body['session_id']}").json()


# ======================================================================================================
# Text safety helpers
# ======================================================================================================
def test_only_figures_from_the_map_service_survive_in_model_text():
    known = {"places": [{"km": 0.9}], "q": "within 5 km of Pune"}
    assert text_safety.drop_unsupported("Pune is 5 km away. It is 160 km to Mysuru. It is a 2-hour drive.", known) == "Pune is 5 km away."
    assert text_safety.drop_unsupported("It opens at 9:00 am. Built in 1820. About 3 temples are nearby.") == "Built in 1820. About 3 temples are nearby."
    assert text_safety.is_supported("A calm hill town.") and not text_safety.is_supported("Costs ₹500.")


def test_a_document_with_nothing_usable_is_nothing():
    for raw in (None, [], "x", {}, {"answer": "", "blocks": [{"type": "mystery"}]}, {"answer": "Costs ₹500 a night.", "blocks": []}):
        assert conversation.sanitize_answer(raw, 0) is None
    lead_only = conversation.sanitize_answer({"blocks": [{"type": "text", "text": "A short lead paragraph."}]}, 0)
    assert lead_only["answer"] == "A short lead paragraph." and lead_only["blocks"] == []


def test_comparison_tables_need_two_columns_and_blank_cells_become_dashes():
    raw = {"answer": "x", "blocks": [
        {"type": "compare", "columns": ["A"], "rows": [{"label": "L", "values": ["1"]}]},
        {"type": "compare", "columns": ["A", "B"], "rows": [{"label": "Cost", "values": ["₹10"]}, {"label": "Mood", "values": ["calm"]}]}]}
    [table] = conversation.sanitize_answer(raw, 0)["blocks"]
    assert table["columns"] == ["A", "B"] and table["rows"] == [{"label": "Mood", "values": ["calm", "—"]}]


def test_markup_ids_and_control_characters_never_reach_the_user():
    raw = {"answer": "**Visit** the <b>garden</b> (p3)\x00 first.", "blocks": [{"type": "list", "items": ["See p2 early", "ok"]}]}
    doc = conversation.sanitize_answer(raw, 3)
    assert doc["answer"] == "Visit the garden first." and doc["blocks"][0]["items"] == ["See early", "ok"]
