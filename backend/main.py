from __future__ import annotations

import copy
import logging
import os
import uuid
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from dotenv import load_dotenv

load_dotenv()

from pydantic import BaseModel, Field

from services.errors import AppError, internal_error_payload
from services.geocoding import search_places
from services.llm import get_provider
from services.logging_setup import configure_logging, describe_exception, short_id
from services.mcp_tools import list_tools
from services.orchestrator import configure_ev, handle_message, set_options
from services.tata_chargers import (
    get_all_tata_chargers,
    get_tata_chargers_in_bbox,
    get_tata_charger_stats,
)
from services.trip_context import TripContext, TripContextStore, is_valid_session_id

# Runs after load_dotenv() so an optional LOG_LEVEL from backend/.env is honoured.
configure_logging()
logger = logging.getLogger(__name__)

APP_NAME = "HerePilot-AI"
APP_VERSION = "5.0.0"


def _origins() -> list[str]:
    configured = os.getenv("CORS_ALLOW_ORIGINS", "").strip()
    if configured:
        return [o.strip().rstrip("/") for o in configured.split(",") if o.strip()]
    return [
        "http://localhost:5173", "http://127.0.0.1:5173",
        "http://localhost:4173", "http://127.0.0.1:4173",
    ]


app = FastAPI(
    title=APP_NAME,
    description="Conversational map assistant: places, geocoding, routes and optional EV trip planning",
    version=APP_VERSION,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type"],
)

store = TripContextStore()
provider = get_provider()

# Booleans and names only: whether credentials exist is logged, never their values.
logger.info(
    "event=startup version=%s log_level=%s llm_provider=%s llm_configured=%s here_configured=%s",
    app.version, logging.getLevelName(logging.getLogger().level),
    provider.name, provider.available, bool(os.getenv("HERE_API_KEY")),
)


# ---------------------------------------------------------------------------------------------
# Errors: the only place that decides what a client is told
# ---------------------------------------------------------------------------------------------
def _request_id(request: Request | None = None) -> str:
    return (getattr(getattr(request, "state", None), "request_id", None)) or uuid.uuid4().hex[:12]


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    request.state.request_id = uuid.uuid4().hex[:12]
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    return response


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    rid = _request_id(request)
    level = logging.WARNING if exc.status >= 500 or exc.retryable else logging.INFO
    logger.log(level, "event=app_error code=%s status=%s request_id=%s detail=%s", exc.code, exc.status, rid, exc.detail or "")
    return JSONResponse(exc.to_payload(rid), status_code=exc.status)


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    rid = _request_id(request)
    fields = sorted({".".join(str(p) for p in err.get("loc", ())[1:]) or "body" for err in exc.errors()})[:6]
    logger.info("event=validation_error request_id=%s fields=%s", rid, ",".join(fields))
    err = AppError("invalid_request", f"Please check these fields: {', '.join(fields)}." if fields else None)
    return JSONResponse(err.to_payload(rid), status_code=422)


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception):
    rid = _request_id(request)
    logger.exception("event=unhandled_error request_id=%s error=%s", rid, describe_exception(exc))
    return JSONResponse(internal_error_payload(rid), status_code=500)


# ---------------------------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------------------------
@app.get("/")
async def root():
    return {"name": APP_NAME, "status": "running", "version": APP_VERSION}


@app.get("/health")
async def health():
    status = provider.status()
    return {
        "status": "healthy",
        "sessions": store.size(),
        "version": APP_VERSION,
        "routing_provider": "HERE Routing API v8",
        "search_provider": "HERE Search; OpenStreetMap / Overpass fallback",
        "here_configured": bool(os.getenv("HERE_API_KEY")),
        "ai": {"provider": status.get("provider"), "available": provider.available, "reason": status.get("reason")},
    }


@app.get("/geocode/search")
async def geocode_search(q: str = Query(..., min_length=1, max_length=200), limit: int = Query(5, ge=1, le=20)):
    results = await search_places(q, limit)
    return {"query": q, "count": len(results), "results": results}


@app.get("/tata-chargers")
async def tata_chargers():
    chargers = get_all_tata_chargers()
    return {"count": len(chargers), "chargers": chargers}


@app.get("/tata-chargers/bbox")
async def tata_chargers_bbox(west: float, south: float, east: float, north: float):
    chargers = get_tata_chargers_in_bbox(south=south, west=west, north=north, east=east)
    return {"count": len(chargers), "chargers": chargers}


@app.get("/tata-chargers/search")
async def tata_chargers_search_endpoint(
    q: str = Query("", min_length=0, max_length=100),
    limit: int = Query(50, ge=1, le=500),
):
    chargers = get_all_tata_chargers()
    q_norm = q.strip().lower()
    if q_norm:
        chargers = [
            charger for charger in chargers
            if q_norm in " ".join(str(charger.get(k, "")) for k in ["name", "city", "state", "address", "cpoName"]).lower()
        ]
    return {"query": q, "count": min(len(chargers), limit), "chargers": chargers[:limit]}


@app.get("/tata-chargers/stats")
async def tata_chargers_stats():
    return get_tata_charger_stats()


@app.get("/mcp/tools")
async def mcp_tools():
    """Capability registry used by the orchestrator."""
    return {"tools": list_tools()}


# ---------------------------------------------------------------------------------------------
# Conversation
# ---------------------------------------------------------------------------------------------
class LocationBody(BaseModel):
    """A position the user chose to share (browser geolocation). Kept in the session only; never logged."""
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy_m: float | None = Field(default=None, ge=0, le=100_000)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    session_id: str | None = Field(default=None, max_length=64)
    # Omit to leave the stored location alone; send null to forget it; send a position to set it.
    location: LocationBody | None = None


def _load_context(session_id: str | None) -> TripContext:
    """An existing session, or a new one. A malformed or unknown ID never registers itself."""
    if session_id and is_valid_session_id(session_id):
        existing = store.find(session_id)
        if existing is not None:
            return existing
    return store.create()


def _payload(saved: TripContext, message: str, reply: str, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": saved.session_id,
        "message": message,
        "reply": reply,
        "result": result,
        "context": saved.to_dict(),
    }


@app.post("/chat")
async def chat(request: ChatRequest):
    original = _load_context(request.session_id)
    working = copy.deepcopy(original)  # handlers mutate the working copy; a failed turn keeps ``original``
    if "location" in request.model_fields_set:
        working.user_location = request.location.model_dump(exclude_none=True) if request.location else None
    try:
        working, result, reply = await handle_message(working, request.message)
    except AppError as exc:
        # A failed turn leaves the saved session exactly as it was before the message.
        logger.info("event=chat_failed session=%s code=%s", short_id(original.session_id), exc.code)
        store.save(original)
        raise
    except Exception as exc:
        logger.exception("event=chat_failed session=%s error=%s", short_id(original.session_id), describe_exception(exc))
        store.save(original)
        raise
    saved = store.save(working)
    return _payload(saved, request.message, reply, result)


# ---------------------------------------------------------------------------------------------
# Panels (structured controls)
# ---------------------------------------------------------------------------------------------
class OptionsRequest(BaseModel):
    session_id: str | None = Field(default=None, max_length=64)
    vehicle_type: str | None = Field(default=None, max_length=16)
    travel_mode: str | None = Field(default=None, max_length=16)


@app.post("/trip/options")
async def trip_options(request: OptionsRequest):
    original = _load_context(request.session_id)
    working = copy.deepcopy(original)
    try:
        result, reply = await set_options(working, vehicle_type=request.vehicle_type, travel_mode=request.travel_mode)
    except Exception:
        store.save(original)
        raise
    return _payload(store.save(working), "", reply, result)


class VehicleBody(BaseModel):
    make: str | None = Field(default=None, max_length=40)
    model: str | None = Field(default=None, max_length=40)
    max_range_km: float | None = Field(default=None, gt=0, le=2000)
    battery_capacity_kwh: float | None = Field(default=None, gt=0, le=500)
    current_battery_percent: float | None = Field(default=None, ge=0, le=100)


class SettingsBody(BaseModel):
    reserve_percent: float | None = Field(default=None, ge=0, lt=100)
    target_charge_percent: float | None = Field(default=None, gt=0, le=100)
    corridor_km: float | None = Field(default=None, gt=0, le=50)


class EVConfigRequest(BaseModel):
    session_id: str | None = Field(default=None, max_length=64)
    enabled: bool = True
    vehicle: VehicleBody | None = None
    settings: SettingsBody | None = None


@app.post("/ev/configure")
async def ev_configure(request: EVConfigRequest):
    """Switch the optional EV Planner on/off and apply vehicle details. Recomputes only the EV plan."""
    original = _load_context(request.session_id)
    working = copy.deepcopy(original)
    vehicle = request.vehicle.model_dump(exclude_none=True) if request.vehicle else None
    settings = request.settings.model_dump(exclude_none=True) if request.settings else None
    try:
        result, reply = await configure_ev(working, enabled=request.enabled, vehicle=vehicle, settings=settings)
    except Exception:
        store.save(original)
        raise
    return _payload(store.save(working), "", reply, result)


@app.get("/session/{session_id}")
async def get_session(session_id: str):
    if not is_valid_session_id(session_id):
        raise AppError("invalid_request", "That session ID isn't valid.")
    context = store.find(session_id)
    if context is None:
        raise AppError("invalid_request", "That session has expired. Start a new one.")
    return context.to_dict()


@app.delete("/session/{session_id}/location")
async def forget_location(session_id: str):
    """Stop sharing: the position the user chose to share is dropped from the session right away, not at their next message."""
    if not is_valid_session_id(session_id):
        raise AppError("invalid_request", "That session ID isn't valid.")
    context = store.find(session_id)
    if context is None:
        return {"forgotten": False}
    had_location = context.user_location is not None
    context.user_location = None
    store.save(context)
    return {"forgotten": had_location}


@app.delete("/session/{session_id}")
async def delete_session(session_id: str):
    if not is_valid_session_id(session_id):
        raise AppError("invalid_request", "That session ID isn't valid.")
    return {"deleted": store.delete(session_id)}
