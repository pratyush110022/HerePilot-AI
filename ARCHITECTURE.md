# HerePilot-AI architecture

## Design objective

HerePilot-AI is a general-purpose conversational map assistant. Every message operates on one active **trip context**
(stops, route, current search results, optional EV state). The optional EV Planner is an add-on to that context, not
the product's purpose: an ordinary trip never computes or shows battery, charging-stop or EV feasibility information.

## Request flow

```text
Browser (React + HERE Maps JS)
  │  POST /chat {message, session_id}
  ▼
FastAPI (main.py)  ── one exception handler, request IDs, CORS allow-list
  ▼
orchestrator.handle_message
  1. interpret   rule parser (always)  +  LLM intent (optional, validated)  → one intent
  2. handler     _h_* function for that intent: deterministic, provider-backed
  3. reply       deterministic wording for every place, distance, time and count; the LLM only answers open-ended
                 follow-ups about results on screen, and a number it states must appear in those results
  ▼
{session_id, reply, result, context}   (context = the full session snapshot used to draw the map)
```

### Interpretation

- `services/intent_parser.py` is the rule parser. Its priority order matters: route edits ("add a stop at Hyderabad")
  are matched before place searches ("restaurants at Hyderabad"), and searches before distance questions.
- `services/intents.py` defines the closed set of intents and `validate_intent`, which clamps radii and limits,
  normalises place text and rejects anything outside the schema. Both the rule parser and the LLM go through it.
- `services/llm_assistant.py` asks the model for a structured intent and, separately, to phrase an answer. It only sees
  allow-listed views of the context (`llm_view`, `result_view`), never raw provider payloads.
- Where rules are exact (route edits, numbered EV details, vehicle numbers) they win over the model. EV intents from
  the model are ignored unless the EV Planner is on.
- `services/references.py` resolves "the second stop", "my destination", "that station" and "charging stop 4" against
  what actually exists in the session. An unresolved reference produces a targeted question, never a guess.

### Handlers and results

Handlers live in `services/orchestrator.py` (`_HANDLERS`). They return a result dict with an `action` name. Route
changes go through `_apply_route`, which recalculates the route and calls `TripContext.invalidate_route_dependents()`
so nothing computed from the old route survives. EV recalculation (`_recompute_ev`) does not re-route and does not
clear search results.

A failed turn leaves the saved session untouched (`/chat` works on a deep copy).

## LLM provider

`services/llm/` defines a small `LLMProvider` protocol, a `NullProvider` (rules mode) and `BedrockProvider`, which uses
the Bedrock Converse API through boto3. There is no default model: the operator sets `BEDROCK_MODEL_ID`, and the
standard AWS credential chain supplies identity (`AWS_PROFILE`, SSO/login session or IAM role). Failures are mapped to
a closed set of `LLMError` categories; AWS messages are never copied into logs or responses.

## Providers

| Capability | Provider | Notes |
| --- | --- | --- |
| Geocoding | HERE Geocoding & Search | City-like queries prefer localities; equal names in different states raise `place_ambiguous` with options. |
| Places | HERE Discover | `in=circle:lat,lon;r=meters` plus an exact distance check. OpenStreetMap / Overpass is a labelled fallback. |
| Routing | HERE Routing v8 | Multi-stop, `car`, `pedestrian` or `bicycle`. Flexible polylines are decoded server-side and also sent to the browser. |
| Route-corridor search | HERE Discover | Samples up to 20 points along the route and reports `coverage.complete`. |
| Weather | Open-Meteo | Advisory only. Never changes a calculation. |
| EV chargers | Bundled Tata EV dataset, HERE EV Charging Search | The dataset drives the deterministic EV plan. |

HERE native EV routing is off by default (`HERE_EV_ENABLED=false`): it needs a vehicle consumption model this app does
not have, so EV results come from the deterministic planner.

## Errors and safety

- `services/errors.py` defines `AppError` with a stable `code`, a fixed client message and a `retryable` flag. Internal
  detail goes only to the redacting log formatter.
- `services/http_utils.py` builds the TLS context from certifi plus an optional extra CA (`GEO_HTTP_CA_BUNDLE`). There
  is no fallback that disables verification.
- Logs go through `services/logging_setup.py`, which redacts keys, tokens and URLs with query strings.
- Session IDs must be UUIDs; unknown IDs never create entries; the in-memory store is bounded (LRU, 500).

## Frontend

```text
src/App.tsx                 layout, tabs (Chat, Trip, EV Planner), focus/prefill state
src/useConversation.ts      messages, session, health polling, retry, panel actions, localStorage persistence
src/api.ts                  fetch wrapper; every failure becomes an ApiError with a safe message
src/cards.ts                turns a /chat result into compact result cards (places, route, distance, EV)
src/MapView.tsx             draws route, stops, results and (EV only) charging stations
src/mapMarkers.ts          marker and popup DOM built with textContent, never provider HTML
src/hereMapAdapter.ts       thin wrapper over the HERE Maps JS SDK
src/components/*            Messages, Cards, Composer, TripPanel, EvPlannerPanel, Welcome, Brand
```

EV content (the EV card, charger markers, battery figures) is rendered only when `context.ev_mode` is true.

## State

The trip context is an in-memory, thread-safe store, so it suits a single-instance deployment. A persistent store
(for example Redis) would be needed for multiple instances. The charging CSV is bundled with the project.

## Legacy modules

Older modules (`groq_llm*.py`, OSRM/Nominatim helpers, `*_backup.py`, `main_before_async_chat_fix.py`) remain in the
repository for reference. None are imported by the active code path. `services/groq_llm.py` is marked historical and
the `groq` package is no longer a dependency.
