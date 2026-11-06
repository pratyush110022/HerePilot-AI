"""Small, optional weather integration backed by the Open-Meteo forecast API.

Weather is advisory context only: it never changes the EV's declared range or
battery arithmetic. A weather failure is represented as unavailable data and
must not block route planning.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

from services.http_utils import request
from services.logging_setup import describe_exception

logger = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
CURRENT_FIELDS = (
    "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,rain,"
    "showers,snowfall,weather_code,cloud_cover,wind_speed_10m,wind_gusts_10m"
)


def _condition(code: Any) -> str:
    try:
        value = int(code)
    except (TypeError, ValueError):
        return "Conditions unavailable"
    if value == 0:
        return "Clear sky"
    if value in (1, 2):
        return "Mostly clear / partly cloudy"
    if value == 3:
        return "Overcast"
    if value in (45, 48):
        return "Fog"
    if value in (51, 53, 55, 56, 57):
        return "Drizzle"
    if value in (61, 63, 65, 66, 67):
        return "Rain"
    if value in (71, 73, 75, 77):
        return "Snow"
    if value in (80, 81, 82):
        return "Rain showers"
    if value in (85, 86):
        return "Snow showers"
    if value in (95, 96, 99):
        return "Thunderstorm"
    return "Mixed conditions"


async def get_current_weather(latitude: float, longitude: float) -> dict[str, Any]:
    """Return current observed/modelled conditions; failures are non-fatal."""
    try:
        response = await request(
            "GET",
            OPEN_METEO_URL,
            timeout=10.0,
            headers={"Accept": "application/json", "User-Agent": "HerePilot-AI/5.0"},
            params={
                "latitude": round(float(latitude), 5),
                "longitude": round(float(longitude), 5),
                "current": CURRENT_FIELDS,
                "timezone": "auto",
            },
        )
        if response.status_code != 200:
            logger.warning("event=weather_unavailable provider=open-meteo http_status=%s", response.status_code)
            return {
                "available": False,
                "provider": "Open-Meteo",
                "message": f"Weather provider returned HTTP {response.status_code}.",
            }
        payload = response.json()
        current = payload.get("current") if isinstance(payload, dict) else None
        if not isinstance(current, dict):
            logger.warning("event=weather_unavailable provider=open-meteo reason=no_current_conditions")
            return {"available": False, "provider": "Open-Meteo", "message": "No current conditions were returned."}
        return {
            "available": True,
            "provider": "Open-Meteo",
            "condition": _condition(current.get("weather_code")),
            "weather_code": current.get("weather_code"),
            "temperature_c": current.get("temperature_2m"),
            "feels_like_c": current.get("apparent_temperature"),
            "humidity_percent": current.get("relative_humidity_2m"),
            "precipitation_mm": current.get("precipitation"),
            "rain_mm": current.get("rain"),
            "showers_mm": current.get("showers"),
            "snowfall_cm": current.get("snowfall"),
            "cloud_cover_percent": current.get("cloud_cover"),
            "wind_kmh": current.get("wind_speed_10m"),
            "wind_gust_kmh": current.get("wind_gusts_10m"),
            "observed_at": current.get("time"),
            "timezone": payload.get("timezone"),
            "latitude": payload.get("latitude", latitude),
            "longitude": payload.get("longitude", longitude),
        }
    except Exception as exc:
        logger.warning("event=weather_unavailable provider=open-meteo error=%s", describe_exception(exc))
        # Weather is optional. Keep error detail concise, without breaking the
        # trip or leaking provider payloads into the public response.
        return {
            "available": False,
            "provider": "Open-Meteo",
            "message": f"Weather is temporarily unavailable ({type(exc).__name__}).",
        }



# ---------------------------------------------------------------------------------------------
# Forecast and climate (shown on place searches, guides, itineraries and destination suggestions)
# ---------------------------------------------------------------------------------------------
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
DAILY_FIELDS = "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum"
CLIMATE_YEARS = 5
RAINY_DAY_MM = 2.5
_FORECAST_TTL_S = 600.0
_CLIMATE_TTL_S = 30 * 24 * 3600.0
_CACHE_MAX = 400
_BREAKER_FAILURES = 3
_BREAKER_PAUSE_S = 300.0
_fail_streak = 0
_blocked_until = 0.0
_forecast_cache: dict[tuple[float, float, int], tuple[float, dict[str, Any]]] = {}
_climate_cache: dict[tuple[float, float], tuple[float, dict[str, Any]]] = {}


def condition_kind(code: Any) -> str:
    """A small, stable vocabulary for the weather icon: clear, partly, cloudy, fog, drizzle, rain, snow or storm."""
    try:
        value = int(code)
    except (TypeError, ValueError):
        return "cloudy"
    if value == 0:
        return "clear"
    if value in (1, 2):
        return "partly"
    if value == 3:
        return "cloudy"
    if value in (45, 48):
        return "fog"
    if value in (51, 53, 55, 56, 57):
        return "drizzle"
    if value in (61, 63, 65, 66, 67, 80, 81, 82):
        return "rain"
    if value in (71, 73, 75, 77, 85, 86):
        return "snow"
    if value in (95, 96, 99):
        return "storm"
    return "cloudy"


def _round_or_none(value: Any, digits: int = 0) -> float | int | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, digits) if digits else int(round(number))


def _trim(cache: dict, limit: int = _CACHE_MAX) -> None:
    while len(cache) > limit:
        cache.pop(next(iter(cache)))


async def get_forecast(latitude: float, longitude: float, days: int = 7) -> dict[str, Any]:
    """Current conditions plus a daily forecast. Never raises: ``available`` is False when Open-Meteo cannot answer."""
    days = max(1, min(int(days), 16))
    key = (round(float(latitude), 1), round(float(longitude), 1), days)
    now = time.monotonic()
    hit = _forecast_cache.get(key)
    if hit and now - hit[0] < _FORECAST_TTL_S:
        return hit[1]
    try:
        response = await request(
            "GET", OPEN_METEO_URL, timeout=8.0,
            headers={"Accept": "application/json", "User-Agent": "HerePilot-AI/5.0"},
            params={"latitude": round(float(latitude), 4), "longitude": round(float(longitude), 4), "current": CURRENT_FIELDS,
                    "daily": DAILY_FIELDS, "forecast_days": days, "timezone": "auto"},
        )
        if response.status_code != 200:
            logger.warning("event=weather_unavailable provider=open-meteo kind=forecast http_status=%s", response.status_code)
            return {"available": False, "provider": "Open-Meteo"}
        payload = response.json()
        current = payload.get("current") if isinstance(payload, dict) else None
        daily = payload.get("daily") if isinstance(payload, dict) else None
        if not isinstance(daily, dict) or not isinstance(daily.get("time"), list):
            return {"available": False, "provider": "Open-Meteo"}
        rows = []
        for i, day in enumerate(daily["time"]):
            def at(name: str) -> Any:
                values = daily.get(name)
                return values[i] if isinstance(values, list) and i < len(values) else None
            code = at("weather_code")
            rows.append({
                "date": day, "weather_code": code, "condition": _condition(code), "kind": condition_kind(code),
                "temp_max_c": _round_or_none(at("temperature_2m_max")), "temp_min_c": _round_or_none(at("temperature_2m_min")),
                "rain_chance_percent": _round_or_none(at("precipitation_probability_max")), "rain_mm": _round_or_none(at("precipitation_sum"), 1),
            })
        now_block = None
        if isinstance(current, dict):
            now_block = {
                "temperature_c": _round_or_none(current.get("temperature_2m")), "feels_like_c": _round_or_none(current.get("apparent_temperature")),
                "condition": _condition(current.get("weather_code")), "kind": condition_kind(current.get("weather_code")),
                "humidity_percent": _round_or_none(current.get("relative_humidity_2m")), "wind_kmh": _round_or_none(current.get("wind_speed_10m")),
                "observed_at": current.get("time"),
            }
        result = {"available": True, "provider": "Open-Meteo", "timezone": payload.get("timezone"), "current": now_block, "days": rows}
        _forecast_cache[key] = (now, result)
        _trim(_forecast_cache)
        return result
    except Exception as exc:
        logger.warning("event=weather_unavailable provider=open-meteo kind=forecast error=%s", describe_exception(exc))
        return {"available": False, "provider": "Open-Meteo"}


async def get_climate(latitude: float, longitude: float) -> dict[str, Any]:
    """Typical weather for each month, averaged over the last five full years (Open-Meteo's historical archive)."""
    key = (round(float(latitude) * 4) / 4, round(float(longitude) * 4) / 4)
    now = time.monotonic()
    hit = _climate_cache.get(key)
    if hit and now - hit[0] < _CLIMATE_TTL_S:
        return hit[1]
    this_year = datetime.now(timezone.utc).year
    first, last = this_year - CLIMATE_YEARS, this_year - 1
    try:
        response = await request(
            "GET", ARCHIVE_URL, timeout=12.0,
            headers={"Accept": "application/json", "User-Agent": "HerePilot-AI/5.0"},
            params={"latitude": round(float(latitude), 3), "longitude": round(float(longitude), 3), "start_date": f"{first}-01-01",
                    "end_date": f"{last}-12-31", "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum", "timezone": "auto"},
        )
        if response.status_code != 200:
            logger.warning("event=weather_unavailable provider=open-meteo kind=climate http_status=%s", response.status_code)
            return {"available": False, "provider": "Open-Meteo"}
        daily = (response.json() or {}).get("daily") or {}
        times, tmax, tmin, rain = (daily.get(k) or [] for k in ("time", "temperature_2m_max", "temperature_2m_min", "precipitation_sum"))
        buckets: dict[int, dict[str, list[float]]] = {m: {"max": [], "min": [], "rain": [], "rainy": []} for m in range(1, 13)}
        per_year: dict[tuple[int, int], float] = {}
        rainy_by_year: dict[tuple[int, int], int] = {}
        for i, day in enumerate(times):
            try:
                year, month = int(day[:4]), int(day[5:7])
            except (TypeError, ValueError):
                continue
            if i < len(tmax) and tmax[i] is not None:
                buckets[month]["max"].append(float(tmax[i]))
            if i < len(tmin) and tmin[i] is not None:
                buckets[month]["min"].append(float(tmin[i]))
            if i < len(rain) and rain[i] is not None:
                per_year[(year, month)] = per_year.get((year, month), 0.0) + float(rain[i])
                if float(rain[i]) >= RAINY_DAY_MM:
                    rainy_by_year[(year, month)] = rainy_by_year.get((year, month), 0) + 1
        months = []
        for month in range(1, 13):
            b = buckets[month]
            totals = [v for (y, m), v in per_year.items() if m == month]
            rainy = [rainy_by_year.get((y, m), 0) for (y, m) in per_year if m == month]
            if not b["max"] or not b["min"]:
                continue
            months.append({
                "month": month, "temp_max_c": _round_or_none(sum(b["max"]) / len(b["max"])), "temp_min_c": _round_or_none(sum(b["min"]) / len(b["min"])),
                "rain_mm": _round_or_none(sum(totals) / len(totals)) if totals else None,
                "rainy_days": _round_or_none(sum(rainy) / len(rainy)) if rainy else None,
            })
        if len(months) < 6:
            return {"available": False, "provider": "Open-Meteo"}
        result = {"available": True, "provider": "Open-Meteo", "years": f"{first}-{last}", "months": months}
        _climate_cache[key] = (now, result)
        _trim(_climate_cache)
        return result
    except Exception as exc:
        logger.warning("event=weather_unavailable provider=open-meteo kind=climate error=%s", describe_exception(exc))
        return {"available": False, "provider": "Open-Meteo"}


def climate_words(climate: dict[str, Any] | None, month: int) -> dict[str, str] | None:
    """A month's typical weather in plain words ("warm", "some rain") so a model can write with it without quoting numbers."""
    if not climate or not climate.get("months"):
        return None
    row = next((m for m in climate.get("months", []) if m.get("month") == month), None)
    if not row or row.get("temp_max_c") is None:
        return None
    high = row["temp_max_c"]
    heat = "very hot" if high >= 37 else "hot" if high >= 32 else "warm" if high >= 27 else "mild" if high >= 21 else "cool" if high >= 14 else "cold"
    low = row.get("temp_min_c")
    nights = None if low is None else ("cold nights" if low <= 8 else "cool nights" if low <= 16 else "warm nights")
    rainy = row.get("rainy_days")
    wet = None if rainy is None else ("very wet" if rainy >= 15 else "wet" if rainy >= 8 else "some rain" if rainy >= 3 else "mostly dry")
    out = {"days": heat}
    if nights:
        out["nights"] = nights
    if wet:
        out["rain"] = wet
    return out


async def weather_bundle(
    latitude: float, longitude: float, *, forecast_days: int = 7, climate: bool = False, timeout: float = 9.0,
) -> dict[str, Any] | None:
    """Everything a result card needs about the weather at one point. ``None`` when nothing could be fetched in time."""
    global _fail_streak, _blocked_until
    if time.monotonic() < _blocked_until:
        return None  # Open-Meteo has been failing: skip weather for a few minutes rather than make every answer wait for it
    jobs = [get_forecast(latitude, longitude, forecast_days)]
    if climate:
        jobs.append(get_climate(latitude, longitude))
    try:
        results = await asyncio.wait_for(asyncio.gather(*jobs), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("event=weather_unavailable provider=open-meteo error=timeout")
        _fail_streak += 1
        if _fail_streak >= _BREAKER_FAILURES:
            _blocked_until = time.monotonic() + _BREAKER_PAUSE_S
        return None
    forecast = results[0]
    clim = results[1] if climate else None
    if not (forecast.get("available") or (clim and clim.get("available"))):
        _fail_streak += 1
        if _fail_streak >= _BREAKER_FAILURES:
            _blocked_until = time.monotonic() + _BREAKER_PAUSE_S
            logger.warning("event=weather_paused seconds=%d", int(_BREAKER_PAUSE_S))
        return None
    _fail_streak = 0
    bundle: dict[str, Any] = {"provider": "Open-Meteo", "latitude": round(float(latitude), 4), "longitude": round(float(longitude), 4)}
    if forecast.get("available"):
        bundle["current"] = forecast.get("current")
        bundle["days"] = forecast.get("days")
    if clim and clim.get("available"):
        bundle["climate"] = {"years": clim["years"], "months": clim["months"]}
    return bundle


MONTH_FULL = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")
CONFLICT_RAINY_DAYS = 14
CONFLICT_HOT_C = 37


def climate_conflicts(months: list[int], climate: dict[str, Any] | None) -> str | None:
    """Where an AI's "best months" disagree with measured weather, as one sentence. The model's memory of a place can be wrong
    (it called October "cool and dry" for a city with about 18 rainy days that month); the measurement is shown alongside, not
    hidden behind a silent correction, and the reader decides."""
    if not months or not climate or not climate.get("months"):
        return None
    by_month = {m["month"]: m for m in climate["months"]}
    found: list[str] = []
    for month in months:
        row = by_month.get(month)
        if not row:
            continue
        if (row.get("rainy_days") or 0) >= CONFLICT_RAINY_DAYS:
            found.append(f"{MONTH_FULL[month - 1]} (about {row['rainy_days']} rainy days)")
        elif (row.get("temp_max_c") or 0) >= CONFLICT_HOT_C:
            found.append(f"{MONTH_FULL[month - 1]} (daytime highs around {row['temp_max_c']}°C)")
    if not found:
        return None
    years = climate.get("years")
    where = f", measured {years}" if years else ""
    return f"The AI's suggested months include {', '.join(found)}{where}. Check the weather chart before you decide."


def weather_sentence(bundle: dict[str, Any] | None, name: str) -> str:
    """One plain sentence of weather for a reply: today's conditions and whether rain is likely in the days ahead."""
    if not bundle:
        return ""
    current = bundle.get("current") or {}
    days = bundle.get("days") or []
    parts: list[str] = []
    if current.get("temperature_c") is not None:
        parts.append(f"Right now in {name}: {current['temperature_c']}°C, {str(current.get('condition') or '').lower()}".rstrip(", "))
    elif days and days[0].get("temp_max_c") is not None:
        parts.append(f"Today in {name}: {days[0]['temp_min_c']} to {days[0]['temp_max_c']}°C, {str(days[0].get('condition') or '').lower()}")
    ahead = days[:5]
    wet = [d for d in ahead if (d.get("rain_chance_percent") or 0) >= 50]
    if ahead and parts:
        parts[-1] += f"; rain looks likely on {len(wet)} of the next {len(ahead)} days" if wet else "; little rain expected in the next few days"
    return (parts[0] + ".") if parts else ""


def weather_enabled() -> bool:
    """Weather can be switched off with HERE_WEATHER=0 (tests do, so they never touch the network)."""
    return os.getenv("HERE_WEATHER", "1").strip().lower() not in {"0", "false", "off", "no"}


def weather_snapshot_metadata() -> dict[str, str]:
    return {
        "provider": "Open-Meteo",
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "type": "current_conditions",
        "note": "Current conditions at the selected coordinates; not a prediction for the vehicle's arrival time.",
    }
