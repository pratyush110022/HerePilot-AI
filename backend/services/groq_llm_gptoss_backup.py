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
                "calculate_route, and plan_ev_trip. Never request or invent "
                "browser.search, web search, news search, or any other tool. "
                "Never invent coordinates, road distances, travel times, "
                "charging stations, or battery calculations. Use geocode_place "
                "when place coordinates are needed. Use calculate_route for "
                "ordinary route questions. Use plan_ev_trip for EV trip planning "
                "and charging stops. Treat tool results as the source of geographic "
                "facts. After the required tools have returned results, provide a "
                "concise final answer. Do not perform new geographic or battery "
                "calculations yourself. The EV planner uses advertised range, "
                "battery percentage, reserve, route-relevant Tata charging stations, "
                "and deterministic battery calculations. It does not provide "
                "real-time charger availability or charging duration."
            ),
        },
        {
            "role": "user",
            "content": user_message,
        },
    ]

    # Tool phase: allow only the tools declared in TOOLS.
    # After tool execution, we deliberately make a separate final call with
    # tools disabled so the model cannot keep generating tool calls forever.
    max_tool_rounds = 3

    for _ in range(max_tool_rounds):
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
        )

        assistant_message = response.choices[0].message

        if not assistant_message.tool_calls:
            raw_call = parse_raw_tool_call(assistant_message.content or "")

            if raw_call:
                raw_function_name, raw_arguments = raw_call

                if raw_function_name not in {
                    "geocode_place",
                    "calculate_route",
                    "plan_ev_trip",
                }:
                    result = {
                        "error": (
                            f"Unsupported tool '{raw_function_name}'. "
                            "Only GeoPilot tools are available."
                        )
                    }
                else:
                    try:
                        # Qwen sometimes sends place names instead of coordinates
                        # for calculate_route or plan_ev_trip. Resolve them first.
                        if raw_function_name in {
                            "calculate_route",
                            "plan_ev_trip",
                        }:
                            origin_text = raw_arguments.pop("origin", None)
                            destination_text = raw_arguments.pop("destination", None)

                            if origin_text and "start_latitude" not in raw_arguments:
                                geo = geocode_place(origin_text)
                                if not geo.get("found"):
                                    result = {
                                        "error": f"Could not geocode origin: {origin_text}"
                                    }
                                else:
                                    place = geo["place"]
                                    raw_arguments["start_latitude"] = float(
                                        place["latitude"]
                                    )
                                    raw_arguments["start_longitude"] = float(
                                        place["longitude"]
                                    )

                            if destination_text and "end_latitude" not in raw_arguments:
                                geo = geocode_place(destination_text)
                                if not geo.get("found"):
                                    result = {
                                        "error": (
                                            f"Could not geocode destination: "
                                            f"{destination_text}"
                                        )
                                    }
                                else:
                                    place = geo["place"]
                                    raw_arguments["end_latitude"] = float(
                                        place["latitude"]
                                    )
                                    raw_arguments["end_longitude"] = float(
                                        place["longitude"]
                                    )

                        # Remove model-generated routing metadata that our tools
                        # do not need.
                        raw_arguments.pop("mode", None)
                        raw_arguments.pop("profile", None)

                        if "result" not in locals():
                            result = execute_tool(
                                raw_function_name,
                                raw_arguments,
                            )

                    except Exception as e:
                        result = {"error": str(e)}

                # Raw Qwen tool calls do not have a Groq tool_call_id, so pass
                # the deterministic result back as context for the final call.
                messages.append({
                    "role": "assistant",
                    "content": assistant_message.content or "",
                })
                messages.append({
                    "role": "user",
                    "content": (
                        "GeoPilot executed the requested tool. Use ONLY this "
                        "result for the final answer; do not call another tool "
                        "and do not recalculate geographic values:\n"
                        + json.dumps(result, ensure_ascii=False, separators=(",", ":"))
                    ),
                })

                final_messages = messages + [{
                    "role": "user",
                    "content": (
                        "Now answer the original user question using ONLY the GeoPilot "
                        "tool result above. Return ONLY a concise natural-language answer. "
                        "Do NOT output XML, <tool_call>, function names, parameters, JSON, "
                        "or request another tool. Do not recalculate the route."
                    ),
                }]

                final_response = client.chat.completions.create(
                    model=MODEL_NAME,
                    messages=final_messages,
                    tool_choice="none",
                )

                final_message = final_response.choices[0].message
                final_content = final_message.content or ""

                if "<tool_call>" in final_content:
                    return (
                        "The route is approximately "
                        + str(result.get("distance_km", "unknown"))
                        + " km by road, with an estimated travel time of "
                        + str(result.get("duration_minutes", "unknown"))
                        + " minutes."
                    )

                return final_content or "I could not generate a response."

            return assistant_message.content or "I could not generate a response."

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
                # Reject anything outside GeoPilot's declared tool set.
                if tool_call.function.name not in {
                    "geocode_place",
                    "calculate_route",
                    "plan_ev_trip",
                }:
                    result = {
                        "error": (
                            f"Unsupported tool '{tool_call.function.name}'. "
                            "Only GeoPilot tools are available."
                        )
                    }
                else:
                    arguments = json.loads(tool_call.function.arguments)
                    result = execute_tool(
                        tool_call.function.name,
                        arguments,
                    )
            except Exception as e:
                result = {"error": str(e)}

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

        # Ask the model for the final natural-language response without tools.
        final_response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            tool_choice="none",
        )

        final_message = final_response.choices[0].message
        return final_message.content or "I could not generate a response."

    return "I could not complete the geographic analysis within the available tool steps."

