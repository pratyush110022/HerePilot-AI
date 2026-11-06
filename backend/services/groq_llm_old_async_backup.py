import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv
from groq import Groq

from services.geocoding import search_places
from services.routing import get_route
from services.ev_agent import plan_ev_trip_for_agent


load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")

if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY is not configured.")

client = Groq(api_key=GROQ_API_KEY)

MODEL_NAME = "openai/gpt-oss-20b"


def run_async(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(asyncio.run, coro)
        return future.result()


def geocode_place(query: str):
    results = run_async(
        search_places(query=query, limit=1)
    )

    if not results:
        return {
            "query": query,
            "found": False,
        }

    result = results[0]

    return {
        "query": query,
        "found": True,
        "place": {
            "display_name": result.get("display_name"),
            "latitude": result.get("latitude"),
            "longitude": result.get("longitude"),
        },
    }


def calculate_route(
    start_latitude: float,
    start_longitude: float,
    end_latitude: float,
    end_longitude: float,
):
    result = run_async(
        get_route(
            start_latitude=start_latitude,
            start_longitude=start_longitude,
            end_latitude=end_latitude,
            end_longitude=end_longitude,
        )
    )

    return {
        "distance_km": result.get("distance_km"),
        "duration_minutes": result.get("duration_minutes"),
    }


async def plan_ev_trip(
    start_latitude: float,
    start_longitude: float,
    end_latitude: float,
    end_longitude: float,
    max_range_km: float,
    battery_percent: float,
    reserve_percent: float = 15.0,
    target_charge_percent: float = 80.0,
    corridor_km: float = 5.0,
):
    return await plan_ev_trip_for_agent(
        start_latitude=start_latitude,
        start_longitude=start_longitude,
        end_latitude=end_latitude,
        end_longitude=end_longitude,
        max_range_km=max_range_km,
        battery_percent=battery_percent,
        reserve_percent=reserve_percent,
        target_charge_percent=target_charge_percent,
        corridor_km=corridor_km,
    )


def extract_json(text: str):
    """
    Extract the first JSON object from the model response.
    Handles plain JSON and accidental markdown fences.
    """
    if not text:
        return None

    text = text.strip()

    if text.startswith("```"):
        lines = text.splitlines()

        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]

        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]

        text = "\n".join(lines).strip()

        if text.lower().startswith("json"):
            text = text[4:].strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")

    if start == -1 or end == -1 or end <= start:
        return None

    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def normalize_place_coordinates(
    action: dict,
    start_place: str,
    end_place: str,
):
    """
    Resolve origin/destination names into deterministic coordinates.
    The LLM does not provide or calculate coordinates.
    """

    start_result = geocode_place(start_place)

    if not start_result.get("found"):
        return {
            "error": f"Could not find the starting place: {start_place}"
        }

    end_result = geocode_place(end_place)

    if not end_result.get("found"):
        return {
            "error": f"Could not find the destination: {end_place}"
        }

    start = start_result["place"]
    end = end_result["place"]

    action["start_latitude"] = float(start["latitude"])
    action["start_longitude"] = float(start["longitude"])
    action["end_latitude"] = float(end["latitude"])
    action["end_longitude"] = float(end["longitude"])

    action["resolved_start"] = start["display_name"]
    action["resolved_destination"] = end["display_name"]

    return action


async def execute_action(action: dict):
    """
    Execute deterministic GeoPilot backend logic.

    Supported actions:
      - route
      - ev_trip
      - geocode
    """

    if not isinstance(action, dict):
        return {
            "error": "Model did not return a valid JSON action."
        }

    action_type = action.get("action")

    if action_type == "geocode":
        query = action.get("place")

        if not query:
            return {
                "error": "Missing 'place' for geocode action."
            }

        return geocode_place(query)

    if action_type == "route":
        origin = action.get("origin")
        destination = action.get("destination")

        if not origin or not destination:
            return {
                "error": (
                    "Route action requires 'origin' and 'destination'."
                )
            }

        resolved = normalize_place_coordinates(
            action,
            origin,
            destination,
        )

        if "error" in resolved:
            return resolved

        route_result = calculate_route(
            start_latitude=resolved["start_latitude"],
            start_longitude=resolved["start_longitude"],
            end_latitude=resolved["end_latitude"],
            end_longitude=resolved["end_longitude"],
        )

        return {
            "action": "route",
            "origin": origin,
            "destination": destination,
            "resolved_origin": resolved["resolved_start"],
            "resolved_destination": resolved[
                "resolved_destination"
            ],
            "route": route_result,
        }

    if action_type == "ev_trip":
        origin = action.get("origin")
        destination = action.get("destination")

        if not origin or not destination:
            return {
                "error": (
                    "EV trip action requires 'origin' and "
                    "'destination'."
                )
            }

        max_range_km = action.get("max_range_km")
        battery_percent = action.get("battery_percent")

        if max_range_km is None:
            return {
                "error": "Missing EV maximum range in km."
            }

        if battery_percent is None:
            return {
                "error": "Missing current battery percentage."
            }

        resolved = normalize_place_coordinates(
            action,
            origin,
            destination,
        )

        if "error" in resolved:
            return resolved

        result = plan_ev_trip(
            start_latitude=resolved["start_latitude"],
            start_longitude=resolved["start_longitude"],
            end_latitude=resolved["end_latitude"],
            end_longitude=resolved["end_longitude"],
            max_range_km=float(max_range_km),
            battery_percent=float(battery_percent),
            reserve_percent=float(
                action.get("reserve_percent", 15.0)
            ),
            target_charge_percent=float(
                action.get("target_charge_percent", 80.0)
            ),
            corridor_km=float(
                action.get("corridor_km", 5.0)
            ),
        )

        return {
            "action": "ev_trip",
            "origin": origin,
            "destination": destination,
            "resolved_origin": resolved["resolved_start"],
            "resolved_destination": resolved[
                "resolved_destination"
            ],
            "result": result,
        }

    return {
        "error": (
            f"Unsupported action '{action_type}'. "
            "Supported actions are route, ev_trip, and geocode."
        )
    }


def create_action(user_message: str):
    """
    Ask the LLM only to interpret the user's intent.

    The LLM returns JSON.
    It does NOT call tools.
    It does NOT calculate coordinates, distance, duration,
    battery usage, or charging stops.
    """

    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are the intent parser for GeoPilot AI. "
                    "Return ONLY valid JSON. No markdown. No XML. "
                    "No tool calls. No explanations. "
                    "\n\n"
                    "Choose exactly one action:\n"
                    "1. route - ordinary road distance or travel-time "
                    "questions.\n"
                    "2. ev_trip - EV trip planning, charging stops, "
                    "battery feasibility, or EV route questions.\n"
                    "3. geocode - when the user only wants the location "
                    "or coordinates of a place.\n\n"
                    "For route return:\n"
                    '{"action":"route","origin":"...","destination":"..."}\n\n'
                    "For EV trip return:\n"
                    '{"action":"ev_trip","origin":"...","destination":"...",'
                    '"max_range_km":number,"battery_percent":number,'
                    '"reserve_percent":number,"target_charge_percent":number,'
                    '"corridor_km":number}\n\n'
                    "For geocode return:\n"
                    '{"action":"geocode","place":"..."}\n\n'
                    "Never invent coordinates or geographic measurements. "
                    "Use the place names supplied by the user. "
                    "If EV range or battery percentage is missing, "
                    "still return the ev_trip action with the missing "
                    "value omitted."
                ),
            },
            {
                "role": "user",
                "content": user_message,
            },
        ],
    )

    content = response.choices[0].message.content or ""

    action = extract_json(content)

    if not action:
        return {
            "error": "The LLM did not return valid JSON.",
            "raw_response": content,
        }

    return action


def create_final_answer(
    user_message: str,
    verified_result: dict,
):
    """
    Final LLM call.

    No tools are supplied.
    The model only converts verified backend data
    into a natural-language response.
    """

    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are GeoPilot AI. "
                    "Answer the user's question using ONLY the "
                    "verified GeoPilot backend result below. "
                    "Do not invent or recalculate geographic values. "
                    "Do not call tools. "
                    "Do not output JSON or XML. "
                    "Give a concise, useful natural-language answer."
                ),
            },
            {
                "role": "user",
                "content": user_message,
            },
            {
                "role": "user",
                "content": (
                    "Verified GeoPilot backend result:\n"
                    + json.dumps(
                        verified_result,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                ),
            },
        ],
    )

    return (
        response.choices[0].message.content
        or "I could not generate a response."
    )


async def ask_groq(user_message: str) -> str:
    """
    Complete GeoPilot AI workflow.

    1. LLM interprets intent as JSON.
    2. Python validates and executes deterministic backend logic.
    3. LLM converts the verified result into the final answer.
    """

    action = create_action(user_message)

    if "error" in action and "action" not in action:
        return action["error"]

    verified_result = await execute_action(action)

    if "error" in verified_result:
        return (
            "I could not complete the request: "
            + str(verified_result["error"])
        )

    return create_final_answer(
        user_message=user_message,
        verified_result=verified_result,
    )
