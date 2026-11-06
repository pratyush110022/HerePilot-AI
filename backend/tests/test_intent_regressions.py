from services.heuristic_intent import parse


def test_tourist_nearby_limit_and_tail():
    action = parse("Find 10 tourist places near Goa and show their names", {"stops": [], "pois": []})
    assert action["intent"] == "find_pois_near_location"
    assert action["category"] == "tourist_attraction"
    assert action["query"] == "tourist places"
    assert action["place"].lower() == "goa"
    assert action["limit"] == 10


def test_natural_language_hotel_query():
    """"in Goa" names an area, so hotels are listed across Goa (the old reading searched 5 km around its centre point)."""
    action = parse("find 5 star hotel in Goa", {"stops": [], "pois": []})
    assert action["intent"] == "find_pois_in_area"
    assert action["category"] == "hotel"
    assert action["area"] == "Goa"
    assert action["query"] == "5 star hotel"
    assert action["qualifiers"] == ["luxury"], "a star rating is not something HERE data supports, so it is reported rather than applied"
    near = parse("find 5 star hotel near Goa", {"stops": [], "pois": []})
    assert near["intent"] == "find_pois_near_location" and near["place"] == "Goa"


def test_pub_bar_query():
    action = parse("where can i found best pub & Bar in Goa?", {"stops": [], "pois": []})
    assert action["intent"] == "find_pois_in_area"
    assert action["category"] == "pub_bar"
    assert action["area"].lower() == "goa"


def test_hotels_at_second_charging_stop():
    action = parse("share me hotels in 2nd charging stop", {"stops": [{"name": "Bengaluru"}, {"name": "Mumbai"}], "pois": []})
    assert action["intent"] == "find_pois"
    assert action["category"] == "hotel"
    assert action["scope"] == "charging_stop"
    assert action["index"] == 1


def test_route_chargers_stay_on_ev_layer():
    action = parse("share all charger details on my route", {"stops": [{"name": "Bengaluru"}, {"name": "Mumbai"}], "pois": []})
    assert action["intent"] == "find_pois"
    assert action["category"] == "ev_charging"
    assert action["scope"] == "route"


def test_generic_poi_reference_resolves_named_trip_stop():
    context = {
        "stops": [
            {"name": "Bengaluru", "display_name": "Bengaluru"},
            {"name": "Goa", "display_name": "Goa, India"},
            {"name": "Mumbai", "display_name": "Mumbai, India"},
        ],
        "vehicle": None,
        "pois": [],
    }
    action = parse("share me pub & bar near Goa stop", context)
    assert action["intent"] == "find_pois_near_location"
    assert action["category"] == "pub_bar"
    assert action["place"] == "Goa"
    assert action["query"] == "pub & bar"
