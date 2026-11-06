"""The HTTP layer: stable error shape, nothing internal in responses, session handling, input validation."""
from __future__ import annotations

import json
import logging

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")

import fakes
from services.errors import AppError

SESSION = "11111111-1111-4111-8111-111111111111"
SECRET = "SENTINEL-HERE-KEY-0123456789"


@pytest.fixture
def client(monkeypatch):
    import main

    main.store._contexts.clear()
    fakes.install(monkeypatch)
    return fastapi_testclient.TestClient(main.app, raise_server_exceptions=False)


def error_of(response):
    body = response.json()
    assert set(body) >= {"detail", "error"}, body
    assert set(body["error"]) >= {"code", "message", "retryable", "request_id"}
    assert body["detail"] == body["error"]["message"]
    return body["error"]


def test_chat_success_shape_and_request_id_header(client):
    response = client.post("/chat", json={"message": "Plan a trip from Mumbai to Pune"})
    assert response.status_code == 200
    body = response.json()
    assert {"session_id", "reply", "result", "context", "message"} <= set(body)
    assert body["context"]["ev_mode"] is False and body["context"]["ev_plan"] is None
    assert response.headers["X-Request-ID"]


def test_unexpected_exception_returns_a_generic_message_and_hides_internals(client, monkeypatch):
    import main

    async def boom(context, message):
        raise RuntimeError(f"Traceback internal path /Users/dev/secret.py key={SECRET} https://x.example/?apiKey={SECRET}")

    monkeypatch.setattr(main, "handle_message", boom)
    response = client.post("/chat", json={"message": "hello there"})
    assert response.status_code == 500
    text = response.text
    assert SECRET not in text and "/Users/dev" not in text and "Traceback" not in text and "RuntimeError" not in text
    assert error_of(response)["code"] == "internal_error"


def test_provider_failures_surface_as_fixed_messages_not_provider_text(client, monkeypatch):
    monkeypatch.setattr(fakes, "route", fakes.failing_route)
    fakes.install(monkeypatch, calculate_route=fakes.failing_route)
    response = client.post("/chat", json={"message": "Plan a trip from Mumbai to Pune"})
    assert response.status_code == 502
    error = error_of(response)
    assert error["code"] == "provider_unavailable" and error["retryable"] is True
    assert "503" not in response.text and "HERE" not in response.text


@pytest.mark.parametrize("path", ["/geocode/search?q=Pune", "/route"])
def test_geocode_endpoint_provider_errors_are_safe(client, monkeypatch, path):
    import main

    async def leaky(*args, **kwargs):
        raise AppError("provider_unavailable", detail=f"HERE returned 500 body with key={SECRET}")

    monkeypatch.setattr(main, "search_places", leaky)
    if path.startswith("/geocode"):
        response = client.get(path)
        assert response.status_code == 502 and SECRET not in response.text and "HERE" not in response.text


def test_expected_user_errors_use_their_own_status_and_message(client):
    response = client.post("/chat", json={"message": "Plan a trip from Mumbai to Atlantis"})
    assert response.status_code == 404
    error = error_of(response)
    assert error["code"] == "place_not_found" and "Atlantis" in error["message"] and error["retryable"] is False


def test_ambiguous_place_returns_options_for_the_ui(client):
    response = client.post("/chat", json={"message": "Plan a trip from Aurangabad to Pune"})
    error = error_of(response)
    assert response.status_code == 409 and error["code"] == "place_ambiguous"
    assert len(error["data"]["options"]) == 2


def test_failed_turn_leaves_the_saved_session_unchanged(client):
    first = client.post("/chat", json={"message": "Plan a trip from Mumbai to Pune"}).json()
    sid = first["session_id"]
    failed = client.post("/chat", json={"session_id": sid, "message": "add Atlantis"})
    assert failed.status_code == 404
    after = client.get(f"/session/{sid}").json()
    assert [s["name"] for s in after["stops"]] == ["Mumbai", "Pune"]
    assert after["route"]["distance_km"] == first["context"]["route"]["distance_km"]


@pytest.mark.parametrize("bad", ["", "not-a-uuid", "../../etc/passwd", "1" * 200])
def test_malformed_session_ids_never_register_or_leak(client, bad):
    import main

    before = main.store.size()
    assert client.get(f"/session/{bad}").status_code in (400, 404, 422)
    response = client.post("/chat", json={"message": "hello", "session_id": bad})
    assert response.status_code in (200, 422)
    if response.status_code == 200:
        assert response.json()["session_id"] != bad
    assert all(k == k.lower() and len(k) <= 36 for k in main.store._contexts)
    assert main.store.size() <= before + 1


def test_unknown_but_wellformed_session_id_is_not_silently_created(client):
    import main

    assert client.get(f"/session/{SESSION}").status_code == 400
    assert SESSION not in main.store._contexts
    body = client.post("/chat", json={"message": "hello", "session_id": SESSION}).json()
    assert body["session_id"] != SESSION


def test_message_length_and_empty_message_are_validated(client):
    assert client.post("/chat", json={"message": ""}).status_code == 422
    assert client.post("/chat", json={"message": "x" * 2001}).status_code == 422
    assert client.post("/chat", json={}).status_code == 422
    assert error_of(client.post("/chat", json={"message": ""}))["code"] == "invalid_request"


def test_trip_with_too_many_stops_is_rejected(client):
    places = ", ".join(["Pune"] + ["Nagpur"] * 11)
    response = client.post("/chat", json={"message": f"Plan a trip from Mumbai to Hyderabad via {places}"})
    assert response.status_code == 400
    error = error_of(response)
    assert error["code"] == "invalid_request" and "at most 10 stops" in error["message"]


def test_radius_and_limit_are_clamped_not_trusted():
    from services.intents import validate_intent

    intent = validate_intent({"intent": "find_pois_near_location", "place": "Pune", "category": "hotel", "radius_km": 99999, "limit": 99999})
    assert intent["radius_km"] == 50.0 and intent["limit"] == 100
    intent = validate_intent({"intent": "find_pois_near_location", "place": "Pune", "category": "hotel", "radius_km": -5, "limit": 0})
    assert intent["radius_km"] == 5.0 and intent["limit"] == 80
    assert validate_intent({"intent": "find_pois", "scope": "the-moon"}) is None
    assert validate_intent({"intent": "drop_database"}) is None


def test_ev_panel_validates_inputs_and_recomputes(client):
    sid = client.post("/chat", json={"message": "Plan a trip from Mumbai to Bhubaneswar"}).json()["session_id"]
    assert client.post("/ev/configure", json={"session_id": sid, "vehicle": {"current_battery_percent": 140}}).status_code == 422
    assert client.post("/ev/configure", json={"session_id": sid, "vehicle": {"max_range_km": -3}}).status_code == 422
    ok = client.post("/ev/configure", json={"session_id": sid, "enabled": True, "vehicle": {"max_range_km": 250, "current_battery_percent": 100}})
    assert ok.status_code == 200
    ctx = ok.json()["context"]
    assert ctx["ev_mode"] and ctx["ev_status"] == "calculated" and len(ctx["ev_plan"]["charging_stops"]) >= 2
    off = client.post("/ev/configure", json={"session_id": sid, "enabled": False}).json()["context"]
    assert off["ev_mode"] is False and off["ev_plan"] is None and off["chargers"] == []


def test_trip_options_set_fuel_type_and_reject_unknown_values(client):
    sid = client.post("/chat", json={"message": "Plan a trip from Mumbai to Pune"}).json()["session_id"]
    ok = client.post("/trip/options", json={"session_id": sid, "vehicle_type": "diesel"})
    assert ok.status_code == 200 and ok.json()["context"]["vehicle_type"] == "diesel"
    assert ok.json()["context"]["ev_mode"] is False
    assert client.post("/trip/options", json={"session_id": sid, "vehicle_type": "steam"}).status_code == 400


def test_health_reports_ai_status_without_secrets(client):
    body = client.get("/health").json()
    assert set(body["ai"]) == {"provider", "available", "reason"}
    text = json.dumps(body)
    assert "arn:" not in text and "AKIA" not in text


def test_cors_allows_only_the_configured_origins(client):
    allowed = client.options("/chat", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"})
    assert allowed.headers.get("access-control-allow-origin") == "http://localhost:5173"
    denied = client.options("/chat", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in denied.headers


def test_error_logs_carry_codes_not_secrets(client, monkeypatch, caplog):
    import main

    async def boom(context, message):
        raise AppError("provider_unavailable", detail="HERE returned 500")

    monkeypatch.setattr(main, "handle_message", boom)
    with caplog.at_level(logging.INFO):
        client.post("/chat", json={"message": "hello there"})
    assert "provider_unavailable" in caplog.text and "hello there" not in caplog.text
