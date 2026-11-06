"""Destination guides ("best time to visit Ooty"): the AI writes the advice, HERE supplies every place, photo and rating.

Everything external (HERE, AWS) is faked. Nothing here is billable and nothing reaches the network.
"""
from __future__ import annotations

import json

import pytest

import fakes
import services.orchestrator as orchestrator
from services import destination_guide, here_places
from services.errors import AppError
from services.intent_parser import parse
from services.intents import validate_intent
from services.trip_context import TripContext


@pytest.fixture
def world(monkeypatch):
    fakes.install(monkeypatch)
    fakes.HIGHLIGHT_CALLS.clear()
    return monkeypatch


def new_context():
    return TripContext(session_id="00000000-0000-4000-8000-000000000077")


async def say(context, text):
    return await orchestrator.handle_message(context, text)


GUIDE = {
    "headline": "The best time to visit Ooty is March to June, or October to February for cooler weather.",
    "summary": "Spring and early summer bring flower displays and mild days. Winter is crisp and misty. The monsoon is quiet and green.",
    "best_months": [3, 4, 5, 6], "good_months": [10, 11, 12, 1, 2], "avoid_months": [7, 8, 9],
    "sections": [
        {"title": "Spring and early summer", "months": [3, 4, 5, 6], "badge": "Most popular", "summary": "Gardens are at their best and the days are mild.",
         "bullets": ["The annual flower show is usually held in May.", "Expect crowds in school holidays."], "place_ids": ["p1", "p2"]},
        {"title": "Crisp winter", "months": [10, 11, 12, 1, 2], "summary": "Cold nights and misty mornings.", "bullets": ["Pack warm clothes."], "place_ids": ["p3"]},
        {"title": "Monsoon", "months": [7, 8, 9], "badge": "Fewer crowds", "summary": "Heavy rain turns the hills green.", "bullets": ["Landslides can disrupt roads."], "place_ids": []},
    ],
    "tips": ["Book stays early for May."], "caveat": "Check current local conditions before you travel.",
}


def guide_model(payload=GUIDE):
    def respond(system, user):
        if "travel guidance" in system:
            return json.dumps(payload)
        return json.dumps({"intent": "destination_guide", "place": "Ooty", "topic": "best_time"}) if "intent parser" in system else "ok"
    return respond


# ======================================================================================================
# Recognising a travel-advice question
# ======================================================================================================
@pytest.mark.parametrize("text, place, topic", [
    ("best time to visit Ooty?", "Ooty", "best_time"),
    ("What is the best time to visit Ooty in winter?", "Ooty", "best_time"),
    ("when should I visit Munnar", "Munnar", "best_time"),
    ("When to visit Goa with family?", "Goa", "best_time"),
    ("things to do in Jaipur", "Jaipur", "things_to_do"),
    ("what to see in Hampi", "Hampi", "things_to_do"),
    ("Is Coorg worth visiting?", "Coorg", "overview"),
    ("travel guide to Kerala", "Kerala", "overview"),
])
def test_trip_advice_questions_become_a_destination_guide(text, place, topic):
    action = validate_intent(parse(text, {}))
    assert action == {"intent": "destination_guide", "place": place, "topic": topic}


@pytest.mark.parametrize("text, intent", [
    ("best time to leave for Pune on my route", "general_question"),
    ("plan a trip from Delhi to Ooty and tell me the best time to visit", "plan_trip"),
    ("best time to visit my destination", "general_question"),
    ("places to visit in Goa", "find_pois_near_location"),
    ("tourist attractions in Ooty", "find_pois_near_location"),
    ("airports in Odisha", "find_pois_in_area"),
    ("add a stop at Ooty", "modify_trip"),
])
def test_other_requests_are_not_mistaken_for_a_guide(text, intent):
    assert parse(text, {})["intent"] == intent


def test_the_guide_intent_is_validated_like_every_other_intent():
    assert validate_intent({"intent": "destination_guide", "place": "  Ooty  ", "topic": "Best Time"}) == {"intent": "destination_guide", "place": "Ooty", "topic": "best_time"}
    assert validate_intent({"intent": "destination_guide", "place": "Ooty", "topic": "drop table"})["topic"] == "overview"
    assert validate_intent({"intent": "destination_guide", "place": ""}) is None


# ======================================================================================================
# Month labels
# ======================================================================================================
@pytest.mark.parametrize("months, label", [
    ([3, 4, 5, 6], "March – June"),
    ([10, 11, 12, 1, 2], "October – February"),
    ([1, 2, 3, 4, 5, 6, 10, 11, 12], "October – June"),
    ([3, 4, 9, 10], "March – April, September – October"),
    ([7], "July"),
    (list(range(1, 13)), "All year"),
    ([], ""),
])
def test_month_labels_join_runs_and_wrap_the_year(months, label):
    assert destination_guide.months_label(months) == label


# ======================================================================================================
# What the model's JSON is allowed to become
# ======================================================================================================
def test_only_places_the_model_was_shown_can_be_referenced_and_each_at_most_once():
    raw = {"headline": "Go in spring.", "sections": [
        {"title": "A", "summary": "x", "place_ids": ["p1", "p2", "p99", "px", "p0", "p1"]},
        {"title": "B", "summary": "y", "place_ids": ["p2", "p3"]},
    ]}
    guide = destination_guide.sanitize_guide(raw, place_count=3, destination="Ooty")
    assert [s["place_indexes"] for s in guide["sections"]] == [[0, 1], [2]], "p99/px/p0 were never offered and p1/p2 repeat"


def test_figures_the_model_cannot_know_are_removed():
    raw = {
        "headline": "Expect 25°C days in spring.",
        "summary": "Spring is lovely. Rooms cost ₹5,000 a night. Winter is cold.",
        "best_months": [3, 4],
        "sections": [{"title": "Spring", "summary": "Mild days. It rains 200 mm in July.",
                      "bullets": ["Daytime highs reach 25 degrees.", "Pack a light jacket.", "Hotels charge Rs. 4000."], "place_ids": []}],
        "tips": ["Budget $50 a day.", "Book early."], "caveat": "Rain can reach 80% of days.",
    }
    guide = destination_guide.sanitize_guide(raw, 0, "Ooty")
    assert guide["headline"] == "The best time to visit Ooty is March – April.", "an unusable headline is rebuilt from the best months"
    assert guide["summary"] == "Spring is lovely. Winter is cold."
    assert guide["sections"][0]["summary"] == "Mild days."
    assert guide["sections"][0]["bullets"] == ["Pack a light jacket."]
    assert guide["tips"] == ["Book early."] and guide["caveat"] is None


def test_markup_control_characters_and_long_text_are_cleaned():
    raw = {"headline": "**Go** in <b>spring</b>\x00 now", "summary": "word " * 200, "best_months": ["3", 3, 13, 0, "x", 4.0]}
    guide = destination_guide.sanitize_guide(raw, 0, "Ooty")
    assert guide["headline"] == "Go in spring now"
    assert len(guide["summary"]) <= 461 and guide["summary"].endswith("…")
    assert guide["best_months"] == [3, 4], "numbers 1-12 only, no duplicates"


def test_place_ids_written_into_text_are_removed():
    raw = {"headline": "Go in spring.", "sections": [{"title": "Spring", "summary": "Gardens bloom (p3).",
                                                      "bullets": ["Visit the Rose Garden (p1) and the lake (p2) first.", "p4 is quiet"]}]}
    section = destination_guide.sanitize_guide(raw, 4, "Ooty")["sections"][0]
    assert section["summary"] == "Gardens bloom." and section["bullets"] == ["Visit the Rose Garden and the lake first.", "is quiet"]


def test_a_summary_that_only_repeats_the_headline_is_trimmed():
    raw = {"headline": "The best time to visit Ooty is March to June.",
           "summary": "The best time to visit Ooty is from March to June. Winter nights are cold and misty."}
    assert destination_guide.sanitize_guide(raw, 0, "Ooty")["summary"] == "Winter nights are cold and misty."


def test_best_for_most_of_the_year_is_not_a_recommendation():
    raw = {"headline": "h", "best_months": [1, 2, 3, 4, 5, 6, 7, 8, 9], "good_months": [10, 11]}
    guide = destination_guide.sanitize_guide(raw, 0, "Ooty")
    assert guide["best_months"] == [] and guide["good_months"] == [10, 11]


def test_the_model_sees_how_well_reviewed_a_place_is_but_never_the_count():
    places = [{"name": "A", "here_category": "Garden", "rating_count": 3660}, {"name": "B", "category": "tourist_attraction", "rating_count": 60},
              {"name": "C", "rating_count": 3}, {"name": "D"}]
    shown = destination_guide.prompt_places(places)
    assert [p["reviews"] for p in shown] == ["many", "some", "few", "none"] and "3660" not in json.dumps(shown)


def test_places_are_arranged_around_the_guide_and_its_indexes_follow_them():
    places = [{"name": n} for n in "ABCDEF"]
    guide = {"sections": [{"place_indexes": [4, 2]}, {"place_indexes": [5]}, {"place_indexes": []}]}
    shown = destination_guide.arrange_places(guide, places, limit=5)
    assert [p["name"] for p in shown] == ["E", "C", "F", "A", "B"], "chosen places first, in section order, then the best of the rest"
    assert [s["place_indexes"] for s in guide["sections"]] == [[0, 1], [2], []]
    assert destination_guide.arrange_places(None, places, limit=3) == places[:3]


def test_a_place_cut_by_the_display_limit_is_dropped_from_its_section():
    places = [{"name": n} for n in "ABCD"]
    guide = {"sections": [{"place_indexes": [3, 2, 1]}]}
    shown = destination_guide.arrange_places(guide, places, limit=2)
    assert [p["name"] for p in shown] == ["D", "C"] and guide["sections"][0]["place_indexes"] == [0, 1]


def test_unusable_model_output_is_nothing_not_a_broken_guide():
    for raw in (None, [], "text", {}, {"sections": "nope"}, {"headline": "", "summary": "", "sections": [{"title": "", "summary": "x"}]}):
        assert destination_guide.sanitize_guide(raw, 3, "Ooty") is None


def test_a_guide_has_at_most_four_sections_three_bullets_and_three_tips():
    raw = {"headline": "h", "sections": [{"title": f"S{i}", "summary": "s", "bullets": ["a", "b", "c", "d", "e"]} for i in range(9)],
           "tips": ["1", "2", "3", "4", "5"]}
    guide = destination_guide.sanitize_guide(raw, 0, "Ooty")
    assert len(guide["sections"]) == 4 and all(len(s["bullets"]) == 3 for s in guide["sections"]) and len(guide["tips"]) == 3


# ======================================================================================================
# HERE photos, ratings and opening status
# ======================================================================================================
def here_item(**overrides):
    item = {"id": "here:pds:place:1", "title": "Government Rose Garden", "position": {"lat": 11.4064, "lng": 76.7116},
            "categories": [{"id": "550-5510-0204", "name": "Garden", "primary": True}], "address": {"label": "Rose Garden, Ooty", "city": "Ooty"},
            "openingHours": [{"text": ["Mon-Sun: 07:00 - 18:30"], "isOpen": False}],
            "media": {
                "images": {"items": [{"href": "https://media-cdn.tripadvisor.com/media/photo-f/1/a.jpg", "supplier": {"id": "tripadvisor"}, "variants": {
                    "thumbnail": {"href": "https://media-cdn.tripadvisor.com/media/photo-t/1/a.jpg"},
                    "small": {"href": "https://media-cdn.tripadvisor.com/media/photo-l/1/a.jpg"},
                    "large": {"href": "https://media-cdn.tripadvisor.com/media/photo-s/1/a.jpg"},
                    "original": {"href": "https://media-cdn.tripadvisor.com/media/photo-o/1/a.jpg"}}}]},
                "ratings": {"items": [{"count": 1320, "average": 4.04, "href": "https://www.tripadvisor.com/Attraction_Review-g1", "supplier": {"id": "tripadvisor"}}]}}}
    item.update(overrides)
    return item


def test_photos_ratings_and_open_now_are_read_from_here_and_the_huge_original_is_never_used():
    place = here_places._normalize_item(here_item())
    assert place["photo"] == {"small": "https://media-cdn.tripadvisor.com/media/photo-l/1/a.jpg",
                              "large": "https://media-cdn.tripadvisor.com/media/photo-s/1/a.jpg", "supplier": "tripadvisor"}
    assert "photo-o" not in json.dumps(place)
    assert place["rating"] == 4.0 and place["rating_count"] == 1320 and place["rating_source"] == "tripadvisor"
    assert place["rating_url"] == "https://www.tripadvisor.com/Attraction_Review-g1"
    assert place["open_now"] is False


def test_unsafe_or_malformed_media_is_ignored_not_trusted():
    bad = here_item(media={
        "images": {"items": [{"href": "javascript:alert(1)"}, {"href": "http://media-cdn.tripadvisor.com/x.jpg"}, {"href": "https://a b/c.jpg"}, "junk"]},
        "ratings": {"items": [{"count": "x", "average": "y"}, {"count": 5, "average": 9}, {"count": 5, "average": 0}]}})
    place = here_places._normalize_item(bad)
    assert place["photo"] is None and place["rating"] is None and place["rating_count"] is None
    assert here_places._normalize_item(here_item(media="oops"))["photo"] is None
    assert here_places._normalize_item(here_item(media=None, openingHours=None))["open_now"] is None


def test_a_rating_link_must_be_https():
    place = here_places._normalize_item(here_item(media={"ratings": {"items": [{"count": 3, "average": 4, "href": "javascript:alert(1)"}]}}))
    assert place["rating"] == 4.0 and place["rating_url"] is None


@pytest.mark.asyncio
async def test_photos_are_requested_with_one_extra_parameter_and_dropped_if_here_rejects_it(monkeypatch):
    monkeypatch.setenv("HERE_RICH_PLACES", "1")
    monkeypatch.setattr(here_places, "_rich_blocked_until", 0.0)
    seen = []

    async def fake_get_json(url, params, *, timeout, provider):
        seen.append(dict(params))
        if "show" in params:
            raise AppError("provider_unavailable", detail="HTTP 400")
        return {"items": [here_item()]}

    monkeypatch.setattr(here_places, "_get_json", fake_get_json)
    first = await here_places._get_places_json("https://x", {"q": "a"}, rich=True)
    assert [("show" in p) for p in seen] == [True, False] and first["items"], "asked with photos, then without"
    second = await here_places._get_places_json("https://x", {"q": "a"}, rich=True)
    assert [("show" in p) for p in seen] == [True, False, False] and second["items"], "not asked again during the cool-down"


@pytest.mark.asyncio
async def test_photos_are_not_requested_when_switched_off_or_not_wanted(monkeypatch):
    monkeypatch.setattr(here_places, "_rich_blocked_until", 0.0)
    seen = []

    async def fake_get_json(url, params, *, timeout, provider):
        seen.append(dict(params))
        return {"items": []}

    monkeypatch.setattr(here_places, "_get_json", fake_get_json)
    monkeypatch.setenv("HERE_RICH_PLACES", "0")
    await here_places._get_places_json("https://x", {}, rich=True)
    monkeypatch.setenv("HERE_RICH_PLACES", "1")
    await here_places._get_places_json("https://x", {}, rich=False)
    assert seen == [{}, {}]
    await here_places._get_places_json("https://x", {}, rich=True)
    assert seen[-1] == {"show": here_places.RICH_SHOW}


@pytest.mark.asyncio
async def test_a_genuine_error_is_not_retried_without_photos(monkeypatch):
    monkeypatch.setenv("HERE_RICH_PLACES", "1")
    monkeypatch.setattr(here_places, "_rich_blocked_until", 0.0)
    calls = []

    async def fake_get_json(url, params, *, timeout, provider):
        calls.append(params)
        raise AppError("provider_rate_limited")

    monkeypatch.setattr(here_places, "_get_json", fake_get_json)
    with pytest.raises(AppError):
        await here_places._get_places_json("https://x", {}, rich=True)
    assert len(calls) == 1


# ======================================================================================================
# Finding the notable places around a destination
# ======================================================================================================
def attraction(item_id, title, lat, lon, primary, name, rating=None, count=None, extra_title=None):
    item = {"id": item_id, "title": title, "position": {"lat": lat, "lng": lon},
            "categories": [{"id": primary, "name": name, "primary": True}], "address": {"city": "Ooty"}}
    if rating is not None:
        item["media"] = {"ratings": {"items": [{"count": count, "average": rating, "href": "https://www.tripadvisor.com/r", "supplier": {"id": "tripadvisor"}}]}}
    return item


@pytest.mark.asyncio
async def test_highlights_blend_reviews_with_heres_own_ranking_and_keep_only_attractions(monkeypatch):
    by_query = {
        "tourist attraction": [
            attraction("a", "Neighbourhood Park", 11.4060, 76.7010, "550-5510-0202", "Park-Recreation Area"),
            attraction("b", "Famous Lake", 11.4100, 76.6900, "300-3000-0023", "Tourist Attraction", 4.1, 800),
            attraction("c", "Eucalyptus Oil Shop", 11.4050, 76.7000, "600-6900-0096", "Specialty Store", 5.0, 5000),
            attraction("g", "Hilltop Viewpoint", 11.3993, 76.7364, "300-3000-0023", "Tourist Attraction"),
        ],
        "garden": [attraction("d", "Rose Garden", 11.4064, 76.7116, "550-5510-0204", "Garden", 4.0, 1320),
                   attraction("b", "Famous Lake", 11.4100, 76.6900, "300-3000-0023", "Tourist Attraction", 4.1, 800)],
        "viewpoint": [attraction("e", "Far Peak", 12.9, 77.9, "550-5510-0242", "Scenic Point", 4.5, 9000)],
        "lake": [attraction("f", "Famous Lake", 11.4101, 76.6901, "350-3500-0304", "Lake", 4.1, 20)],
    }

    async def fake_discover(lat, lon, query, limit=50, restrict_to_india=True, radius_km=None, rich=False):
        assert rich is True and radius_km == 25.0
        return by_query.get(query, [])

    monkeypatch.setattr(here_places, "_discover_at", fake_discover)
    stats: dict = {}
    places = await here_places.find_destination_highlights(11.40556, 76.70051, 25.0, 10, stats=stats)
    names = [p["name"] for p in places]
    assert set(names[:2]) == {"Rose Garden", "Famous Lake"}, "the two well-reviewed places lead"
    assert names.index("Famous Lake") < names.index("Neighbourhood Park")
    assert "Hilltop Viewpoint" in names, "a well-placed attraction with no reviews is still on the shortlist"
    assert names.index("Hilltop Viewpoint") < names.index("Neighbourhood Park"), "HERE's own rank and category count, not only reviews"
    assert "Eucalyptus Oil Shop" not in names, "HERE classifies it as a specialty store, whatever it is tagged elsewhere"
    assert "Far Peak" not in names, "outside the radius"
    assert names.count("Famous Lake") == 1, "two HERE records for one lake collapse to one"
    assert all(p["category_validated"] and p["distance_from_reference_km"] <= 25 and "_ranks" not in p for p in places)
    assert stats["rejected"] >= 1 and stats["matched"] == 4 and stats["with_ratings"] == 2


@pytest.mark.asyncio
async def test_of_two_records_for_one_place_the_one_with_photo_and_rating_wins(monkeypatch):
    bare = attraction("x1", "Rose Garden", 11.4064, 76.7116, "550-5510-0204", "Garden")
    full = attraction("x2", "Rose Garden", 11.4065, 76.7117, "550-5510-0204", "Garden", 4.0, 1320)

    async def fake_discover(lat, lon, query, limit=50, restrict_to_india=True, radius_km=None, rich=False):
        return {"tourist attraction": [bare], "garden": [full]}.get(query, [])

    monkeypatch.setattr(here_places, "_discover_at", fake_discover)
    [place] = await here_places.find_destination_highlights(11.40556, 76.70051)
    assert place["rating_count"] == 1320 and place["here_id"] == "x2"


@pytest.mark.asyncio
async def test_highlights_survive_one_failed_query_but_not_all_of_them(monkeypatch):
    async def flaky(lat, lon, query, limit=50, restrict_to_india=True, radius_km=None, rich=False):
        if query == "lake":
            raise AppError("provider_unavailable")
        return [attraction("a", "Rose Garden", 11.4064, 76.7116, "550-5510-0204", "Garden", 4.0, 10)]

    monkeypatch.setattr(here_places, "_discover_at", flaky)
    stats: dict = {}
    assert [p["name"] for p in await here_places.find_destination_highlights(11.4, 76.7, stats=stats)] == ["Rose Garden"]
    assert stats["failed_requests"] == 1

    async def down(*args, **kwargs):
        raise AppError("provider_unavailable")

    monkeypatch.setattr(here_places, "_discover_at", down)
    with pytest.raises(AppError):
        await here_places.find_destination_highlights(11.4, 76.7)


# ======================================================================================================
# The guide, end to end through the orchestrator
# ======================================================================================================
@pytest.mark.asyncio
async def test_the_model_writes_the_advice_and_here_supplies_the_places(world, fake_llm):
    provider = fake_llm(guide_model())
    context, result, reply = await say(new_context(), "best time to visit Ooty?")
    guide = result["guide"]
    assert result["action"] == "destination_guide" and result["place"] == "Ooty" and result["region"] == "Tamil Nadu"
    assert guide["best_label"] == "March – June" and [s["months_label"] for s in guide["sections"]] == ["March – June", "October – February", "July – September"]
    assert [s["place_indexes"] for s in guide["sections"]] == [[0, 1], [2], []]
    assert [p["name"] for p in result["pois"][:3]] == ["Government Rose Garden", "Otty Lake", "Doddabetta Peak"], "indexes point into HERE's list"
    assert context.pois == result["pois"] and context.pois_label == "top places in Ooty", "the places are on the map and usable in follow-ups"
    assert reply.startswith("The best time to visit Ooty is March to June") and "come from HERE" in reply and "AI's general knowledge" in reply
    assert result["guide_source"] == "ai_general_knowledge"
    assert fakes.HIGHLIGHT_CALLS[0]["radius_km"] == 15.0, "a town is searched within 15 km of its centre"
    assert len([c for c in provider.calls if "travel guidance" in c["system"]]) == 1


@pytest.mark.asyncio
async def test_a_place_the_model_invents_never_reaches_the_user(world, fake_llm):
    hostile = json.loads(json.dumps(GUIDE))
    hostile["sections"][0]["place_ids"] = ["p1", "p42"]
    hostile["sections"][0]["bullets"] = ["Visit the Secret Palace of Ooty, open 24 hours."]
    fake_llm(guide_model(hostile))
    _, result, _ = await say(new_context(), "best time to visit Ooty?")
    assert result["guide"]["sections"][0]["place_indexes"] == [0]
    assert len(result["pois"]) == 4 and "Secret Palace" not in [p["name"] for p in result["pois"]]


@pytest.mark.asyncio
async def test_the_model_is_given_ids_names_and_types_only(world, fake_llm):
    provider = fake_llm(guide_model())
    await say(new_context(), "best time to visit Ooty?")
    [call] = [c for c in provider.calls if "travel guidance" in c["system"]]
    payload = json.loads(call["user"])
    assert payload["destination"] == "Ooty" and payload["region"] == "Tamil Nadu" and payload["topic"] == "best_time"
    assert payload["PLACES"][0] == {"id": "p1", "name": "Government Rose Garden", "type": "Garden", "reviews": "many"}
    sent = call["user"]
    assert "latitude" not in sent and "tripadvisor" not in sent and "http" not in sent and "11.40" not in sent
    assert "untrusted" in call["system"] and "NO live data" in call["system"]


@pytest.mark.asyncio
async def test_without_ai_the_guide_is_the_here_places_alone_and_says_why(world):
    context, result, reply = await say(new_context(), "best time to visit Ooty?")
    assert result["guide"] is None and result["guide_source"] is None and len(result["pois"]) == 4
    assert "needs the AI service" in reply and "haven't guessed" in reply
    assert context.pois_label == "top places in Ooty"


@pytest.mark.asyncio
async def test_unusable_model_output_falls_back_to_the_places(world, fake_llm):
    fake_llm(lambda system, user: "not json at all" if "travel guidance" in system else json.dumps({"intent": "destination_guide", "place": "Ooty"}))
    _, result, reply = await say(new_context(), "best time to visit Ooty?")
    assert result["guide"] is None and result["pois"] and "needs the AI service" in reply


@pytest.mark.asyncio
async def test_a_failing_model_does_not_break_the_turn(world, fake_llm):
    from services.llm.provider import LLMThrottled

    fake_llm(lambda system, user: LLMThrottled("slow down") if "travel guidance" in system else "{}")
    _, result, _ = await say(new_context(), "best time to visit Ooty?")
    assert result["action"] == "destination_guide" and result["guide"] is None and result["pois"]


@pytest.mark.asyncio
async def test_the_model_can_choose_the_guide_for_wording_the_rules_do_not_know(world, fake_llm):
    fake_llm(guide_model())
    action, source = await orchestrator._interpret_message("Which season would be lovely for a holiday in Ooty?", new_context())
    assert action["intent"] == "destination_guide" and action["place"] == "Ooty" and source == "llm"


@pytest.mark.asyncio
async def test_when_here_places_fail_the_guide_still_answers_and_says_it_has_no_places(world, fake_llm):
    async def down(latitude, longitude, radius_km=25.0, limit=12):
        return {"pois": [], "source_status": "unavailable", "source": "HERE Search API", "source_error": "provider_unavailable", "search": {}}

    fakes.install(world, find_destination_highlights_with_source=down)
    fake_llm(guide_model())
    _, result, reply = await say(new_context(), "best time to visit Ooty?")
    assert result["guide"] and result["pois"] == [] and "couldn't load places from HERE" in reply


@pytest.mark.asyncio
async def test_an_ambiguous_destination_is_asked_about_not_guessed(world):
    with pytest.raises(AppError) as caught:
        await say(new_context(), "best time to visit Aurangabad")
    assert caught.value.code == "place_ambiguous"


@pytest.mark.asyncio
async def test_a_state_is_searched_over_a_wider_area_than_a_town(world, fake_llm):
    fake_llm(guide_model())
    await say(new_context(), "things to do in Odisha")
    assert fakes.HIGHLIGHT_CALLS[-1]["radius_km"] == 60.0, "capped at 60 km however large the state is"


@pytest.mark.asyncio
async def test_a_destination_that_is_not_an_area_is_still_found_by_name(world, fake_llm):
    fake_llm(guide_model())
    _, result, _ = await say(new_context(), "is Baga Beach worth visiting")
    assert result["action"] == "destination_guide" and result["place"] == "Baga Beach" and fakes.HIGHLIGHT_CALLS[-1]["radius_km"] == 15.0


@pytest.mark.asyncio
async def test_a_whole_country_gets_a_general_answer_not_an_attraction_list(world, fake_llm):
    async def country(query):
        raise AppError("invalid_request", "India is a whole country: too large to list in one search.")

    fakes.install(world, resolve_area_candidate=country)
    fake_llm(lambda system, user: "India has many seasons." if "geography" in system else json.dumps({"intent": "destination_guide", "place": "India"}))
    _, result, reply = await say(new_context(), "best time to visit India")
    assert result["action"] == "general_answer" and "India has many seasons" in reply
