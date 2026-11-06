import asyncio
import json
import os
import re
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


def plan_ev_trip(
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
    return plan_ev_trip_for_agent(
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


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "geocode_place",
            "description": (
                "Find real geographic coordinates for a city, "
                "address, landmark, or other place. "
                "Use this whenever coordinates are required."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Place or address to search for.",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate_route",
            "description": (
                "Calculate a real driving route between two "
                "geographic coordinates. Returns road distance "
                "and travel duration."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_latitude": {"type": "number"},
                    "start_longitude": {"type": "number"},
                    "end_latitude": {"type": "number"},
                    "end_longitude": {"type": "number"},
                },
                "required": [
                    "start_latitude",
                    "start_longitude",
                    "end_latitude",
                    "end_longitude",
                ],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "plan_ev_trip",
            "description": (
                "Plan a real EV road trip between two places using "
                "OSRM routing and route-relevant Tata EV charging "
                "stations. Use this when the user asks for an EV "
                "trip plan, charging stops, whether an EV can reach "
                "a destination, or where to charge. The planner "
                "performs deterministic battery and charger calculations."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_latitude": {
                        "type": "number",
                        "description": "Starting latitude.",
                    },
                    "start_longitude": {
                        "type": "number",
                        "description": "Starting longitude.",
                    },
                    "end_latitude": {
                        "type": "number",
                        "description": "Destination latitude.",
                    },
                    "end_longitude": {
                        "type": "number",
                        "description": "Destination longitude.",
                    },
                    "max_range_km": {
                        "type": "number",
                        "description": (
                            "Vehicle advertised maximum range in km."
                        ),
                    },
                    "battery_percent": {
                        "type": "number",
                        "description": (
                            "Current battery percentage."
                        ),
                    },
                    "reserve_percent": {
                        "type": "number",
                        "description": (
                            "Minimum battery reserve to maintain."
                        ),
                        "default": 15,
                    },
                    "target_charge_percent": {
                        "type": "number",
                        "description": (
                            "Battery percentage to charge to at each stop."
                        ),
                        "default": 80,
                    },
                    "corridor_km": {
                        "type": "number",
                        "description": (
                            "Maximum distance from the route for charging stations."
                        ),
                        "default": 5,
                    },
                },
                "required": [
                    "start_latitude",
                    "start_longitude",
                    "end_latitude",
                    "end_longitude",
                    "max_range_km",
                    "battery_percent",
                ],
            },
        },
    },
]


def execute_tool(function_name: str, arguments: dict):
    if function_name == "geocode_place":
        return geocode_place(arguments["query"])

    if function_name == "calculate_route":
        return calculate_route(
            start_latitude=arguments["start_latitude"],
            start_longitude=arguments["start_longitude"],
            end_latitude=arguments["end_latitude"],
            end_longitude=arguments["end_longitude"],
        )

    if function_name == "plan_ev_trip":
        return plan_ev_trip(
            start_latitude=arguments["start_latitude"],
            start_longitude=arguments["start_longitude"],
            end_latitude=arguments["end_latitude"],
            end_longitude=arguments["end_longitude"],
            max_range_km=arguments["max_range_km"],
            battery_percent=arguments["battery_percent"],
            reserve_percent=arguments.get(
                "reserve_percent", 15.0
            ),
            target_charge_percent=arguments.get(
                "target_charge_percent", 80.0
            ),
            corridor_km=arguments.get(
                "corridor_km", 5.0
            ),
        )

    return {
        "error": f"Unknown tool: {function_name}"
    }


def parse_raw_tool_call(content: str):
    """Parse Qwen's raw <tool_call> text when Groq does not expose tool_calls."""
    if not content or "<tool_call>" not in content:
        return None

    function_match = re.search(r"<function\s*=\s*([A-Za-z0-9_]+)\s*>", content)
    if not function_match:
        return None

    function_name = function_match.group(1)
    arguments = {}

    for match in re.finditer(
        r"<parameter\s*=\s*([A-Za-z0-9_]+)\s*>\s*(.*?)\s*</parameter>",
        content,
        flags=re.DOTALL,
    ):
        key = match.group(1)
        value = match.group(2).strip()
        arguments[key] = value

    # Qwen may use shorter argument names than our JSON schema.
    aliases = {
        "start_lat": "start_latitude",
        "start_lon": "start_longitude",
        "start_lng": "start_longitude",
        "end_lat": "end_latitude",
        "end_lon": "end_longitude",
        "end_lng": "end_longitude",
        "max_range": "max_range_km",
        "battery": "battery_percent",
        "reserve": "reserve_percent",
        "target": "target_charge_percent",
        "corridor": "corridor_km",
    }
    arguments = {aliases.get(k, k): v for k, v in arguments.items()}

    # Qwen may also return route coordinates as origin/destination strings.
    if "origin" in arguments:
        try:
            lat, lng = [float(x.strip()) for x in arguments["origin"].split(",", 1)]
            arguments["start_latitude"] = lat
            arguments["start_longitude"] = lng
        except (TypeError, ValueError):
            pass
        arguments.pop("origin", None)

    if "destination" in arguments:
        try:
            lat, lng = [float(x.strip()) for x in arguments["destination"].split(",", 1)]
            arguments["end_latitude"] = lat
            arguments["end_longitude"] = lng
        except (TypeError, ValueError):
            pass
        arguments.pop("destination", None)

    numeric_keys = {
        "start_latitude", "start_longitude", "end_latitude", "end_longitude",
        "max_range_km", "battery_percent", "reserve_percent",
        "target_charge_percent", "corridor_km",
    }
    for key in numeric_keys:
        if key in arguments:
            try:
                arguments[key] = float(arguments[key])
            except (TypeError, ValueError):
                pass

    if function_name == "calculate_route":
        arguments.pop("mode", None)

    return function_name, arguments

def ask_groq(user_message: str) -> str:
    messages = [
        {
            "role": "system",
            "content": (
                "You are GeoPilot AI, a geospatial travel and EV assistant. "
                "Only use the declared GeoPilot tools: geocode_place, "
                "calculate_route, and plan_ev_trip. "
                "Never invent coordinates, road distances, travel times, "
                "charging stations, or battery calculations. "
                "Use geocode_place when place coordinates are needed. "
                "Use calculate_route for ordinary route questions. "
                "Use plan_ev_trip for EV trip planning and charging stops. "
                "After each tool result, continue the tool workflow if another "
                "tool is required. Only provide the final answer when all "
                "required tools have completed. "
                "Treat tool results as the source of geographic facts. "
                "Do not perform new geographic or battery calculations yourself."
            ),
        },
        {
            "role": "user",
            "content": user_message,
        },
    ]

    allowed_tools = {
        "geocode_place",
        "calculate_route",
        "plan_ev_trip",
    }

    max_tool_rounds = 5

    for _ in range(max_tool_rounds):
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
        )

        assistant_message = response.choices[0].message

        # ------------------------------------------------------------
        # Native structured tool calls
        # ------------------------------------------------------------
        if assistant_message.tool_calls:
            messages.append(
                {
                    "role": "assistant",
                    "content": assistant_message.content or "",
                    "tool_calls": [
                        {
                            "id": tool_call.id,
                            "type": "function",
                            "function": {
                                "name": tool_call.function.name,
                                "arguments": tool_call.function.arguments,
                            },
                        }
                        for tool_call in assistant_message.tool_calls
                    ],
                }
            )

            for tool_call in assistant_message.tool_calls:
                try:
                    function_name = tool_call.function.name

                    if function_name not in allowed_tools:
                        result = {
                            "error": (
                                f"Unsupported tool '{function_name}'. "
                                "Only GeoPilot tools are available."
                            )
                        }
                    else:
                        arguments = json.loads(
                            tool_call.function.arguments
                        )

                        # Some models may provide place names instead of
                        # coordinates for route tools. Resolve them here.
                        if function_name in {
                            "calculate_route",
                            "plan_ev_trip",
                        }:
                            origin = arguments.pop("origin", None)
                            destination = arguments.pop(
                                "destination", None
                            )

                            if origin and (
                                "start_latitude" not in arguments
                                or "start_longitude" not in arguments
                            ):
                                origin_result = geocode_place(origin)

                                if not origin_result.get("found"):
                                    raise ValueError(
                                        f"Could not geocode origin: {origin}"
                                    )

                                place = origin_result["place"]

                                arguments["start_latitude"] = float(
                                    place["latitude"]
                                )
                                arguments["start_longitude"] = float(
                                    place["longitude"]
                                )

                            if destination and (
                                "end_latitude" not in arguments
                                or "end_longitude" not in arguments
                            ):
                                destination_result = geocode_place(
                                    destination
                                )

                                if not destination_result.get("found"):
                                    raise ValueError(
                                        "Could not geocode destination: "
                                        f"{destination}"
                                    )

                                place = destination_result["place"]

                                arguments["end_latitude"] = float(
                                    place["latitude"]
                                )
                                arguments["end_longitude"] = float(
                                    place["longitude"]
                                )

                        arguments.pop("mode", None)
                        arguments.pop("profile", None)

                        result = execute_tool(
                            function_name,
                            arguments,
                        )

                except Exception as e:
                    result = {
                        "error": str(e)
                    }

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(
                            result,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    }
                )

            # IMPORTANT:
            # Continue the loop so the model can request another tool.
            continue

        # ------------------------------------------------------------
        # Qwen-style raw <tool_call> fallback
        # ------------------------------------------------------------
        raw_call = parse_raw_tool_call(
            assistant_message.content or ""
        )

        if raw_call:
            raw_function_name, raw_arguments = raw_call

            if raw_function_name not in allowed_tools:
                result = {
                    "error": (
                        f"Unsupported tool '{raw_function_name}'. "
                        "Only GeoPilot tools are available."
                    )
                }
            else:
                try:
                    result = execute_tool(
                        raw_function_name,
                        raw_arguments,
                    )
                except Exception as e:
                    result = {
                        "error": str(e)
                    }

            messages.append(
                {
                    "role": "assistant",
                    "content": assistant_message.content or "",
                }
            )

            messages.append(
                {
                    "role": "user",
                    "content": (
                        "GeoPilot tool result. Continue the analysis if "
                        "another GeoPilot tool is required. Do not invent "
                        "geographic facts:"
                        + json.dumps(
                            result,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    ),
                }
            )

            # Continue so another tool can be requested.
            continue

        # ------------------------------------------------------------
        # No tool call = final answer
        # ------------------------------------------------------------
        return (
            assistant_message.content
            or "I could not generate a response."
        )

    return (
        "I could not complete the geographic analysis "
        "within the available tool steps."
    )

