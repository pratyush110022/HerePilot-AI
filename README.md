# HerePilot-AI

HerePilot-AI is a conversational map assistant. Ask it to find places, plan a journey, add or remove stops, or work out
distances and travel times, and the answers show up on an interactive HERE map. An **EV Planner** is available as an
optional extra for electric vehicles; ordinary trips never show battery or charging information.

> The project was previously called GeoPilot AI. The repository and local folder keep their old names; the app,
> API title and UI are HerePilot-AI.

## What you can ask

```text
Find restaurants near Connaught Place, Delhi
Hotels within 3 km of Bandra, Mumbai
Plan a trip from Mumbai to Bhubaneswar
Add Hyderabad to my route            -> the route, waypoints, distance and ETA update
Remove Hyderabad from my route
Find petrol pumps along my route
Find hotels near my destination
What is the distance to the second stop?
How far is my destination?
```

"Find restaurants near Hyderabad" is a **search**. "Add Hyderabad to my route" **edits the route**. The two are never
confused, and a search never changes the trip.

## Features

- Map-first UI with a HERE map, a chat panel, place-result cards, route summaries, loading, empty, error and retry
  states, keyboard and screen-reader support, and a layout that works from phone to desktop.
- Persistent conversation and trip context (server-side session, with the transcript kept in the browser).
- Structured intent handling with reference resolution ("the second stop", "my destination", "that station") and
  targeted clarification questions when a request is ambiguous.
- HERE Geocoding & Search, Discover (places and radius searches), and Routing v8 (multi-stop, driving, walking,
  cycling). Place search falls back to OpenStreetMap / Overpass and says so when it does.
- Fuel type (petrol, diesel, hybrid, electric) is stored with the trip. **Fuel use, cost and refuelling are not
  calculated.**
- Optional EV Planner: vehicle range, battery level, reserve, charging target, charger search along the route and a
  deterministic charging plan. It reports "not calculated", "failed" or "no stop needed" honestly. Chargers come from
  the bundled Tata EV Charging Dataset (a static list, not live availability).
- AI is optional. Without it the assistant understands requests with built-in rules. With AWS Bedrock it also
  interprets free-form requests into structured intents and answers open-ended follow-ups about the results on
  screen. HERE stays authoritative: every place, distance and travel time in a reply comes from HERE results, and a
  model answer that contains a number not found in those results is discarded.

## Architecture

```text
React + TypeScript (Vite)  ──  HERE Maps JavaScript SDK
            │
            ▼
   FastAPI  /chat  /trip/options  /ev/configure  /session/{id}
            │
            ▼
   Orchestrator ── validated intent ──► handlers ──► verified results
        │  ▲                                  │
        │  └── rule parser (always on)        ├── HERE Geocoding / Discover / Routing
        └── LLM provider (optional, Bedrock)  ├── OpenStreetMap / Overpass (place fallback)
                                              ├── Open-Meteo (advisory weather)
                                              └── EV planner + bundled charging dataset (optional)
```

Key rules the backend enforces:

- The model only ever proposes a **structured intent**, which is validated before use. It never supplies coordinates,
  distances or place facts.
- Provider errors are mapped to fixed, safe messages. Raw provider text, URLs, keys and exception details are never
  sent to the browser or into model prompts.
- A change to the route clears everything computed from the old route (search results, EV plan, weather).
- TLS verification is always on.

See `ARCHITECTURE.md` for details.

## Run locally

### Backend

macOS / Linux:

```bash
cd backend
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env        # then edit .env; never commit it
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

Windows PowerShell:

```powershell
cd backend
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

### Frontend

```bash
cd frontend
npm install
cp .env.example .env        # set VITE_HERE_API_KEY (browser map key) and VITE_BACKEND_URL
npm run dev
```

Open `http://localhost:5173/`. (Vite may listen on IPv6 only, so use `localhost` rather than `127.0.0.1`.)

## Configuration

`backend/.env` (see `backend/.env.example` for every option):

| Variable | Purpose |
| --- | --- |
| `HERE_API_KEY` | Required for geocoding, place search and routing. |
| `LLM_PROVIDER` | `bedrock` or `none`. Without a working Bedrock setup the app runs in rules mode. |
| `BEDROCK_MODEL_ID` | A model or inference-profile ID your AWS account can use. There is no default. |
| `BEDROCK_REGION` | Optional. Defaults to the AWS region of your environment or profile. |
| `AWS_PROFILE` | Optional. Selects a shared AWS profile. The app never chooses one for you. |
| `GEO_HTTP_CA_BUNDLE` | Optional. Extra root CA (PEM) for a TLS-inspecting corporate proxy. |
| `CORS_ALLOW_ORIGINS` | Optional. Comma-separated origins; defaults to the local Vite ports. |

`frontend/.env`:

| Variable | Purpose |
| --- | --- |
| `VITE_BACKEND_URL` | Where the API runs, for example `http://localhost:8000`. |
| `VITE_HERE_API_KEY` | HERE key used by the browser to draw the map. |

### AWS Bedrock setup

1. In the AWS console, open Amazon Bedrock in your chosen region and make sure your account has access to the model
   you want to use. Some models must be called through an inference profile; use that profile's ID.
2. Sign in with the standard AWS tooling (SSO, `aws login`, an IAM role or a shared profile). Do not put AWS keys in
   `.env`. Then choose the profile explicitly, either in your shell or in `.env`:

   ```bash
   export AWS_PROFILE=your-profile-name
   ```

3. Set `LLM_PROVIDER=bedrock` and `BEDROCK_MODEL_ID=<your model or inference-profile ID>` (and `BEDROCK_REGION` if
   it differs from your profile's region). Models that support on-demand use take their plain ID, for example
   `amazon.nova-pro-v1:0`. Most current Anthropic models are inference-profile only and need the profile ID, for
   example `eu.anthropic.claude-sonnet-5-5`. Set the variables in the terminal that launches the backend rather than
   in a file:

   ```bash
   cd backend
   env LLM_PROVIDER=bedrock BEDROCK_MODEL_ID=amazon.nova-pro-v1:0 BEDROCK_REGION=eu-west-2 \
       AWS_PROFILE=your-profile-name .venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8000
   ```
4. Start the backend. `GET /health` reports `ai.available` and the provider name (never credentials). The header in
   the UI shows "AI: bedrock" when it is ready, or "Rules mode" when it is not.

The app uses the Bedrock Converse API
([documentation](https://docs.aws.amazon.com/bedrock/latest/userguide/conversation-inference.html)). Bedrock calls are
billable. Nothing in this repository's tests calls AWS; they use a fake provider.

To check which identity your profile uses without invoking a model:

```bash
aws sts get-caller-identity --profile your-profile-name
```

### Behind a corporate TLS-inspecting proxy

If requests to AWS or other providers fail with `CERTIFICATE_VERIFY_FAILED`, add your organisation's root CA to the
trust store instead of disabling verification:

```bash
GEO_HTTP_CA_BUNDLE=/path/to/corporate-root-ca.pem python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

The bundle is added to the default (certifi) trust store, not used instead of it.

## Tests and checks

Backend (offline: fakes only, no network, no credentials):

```bash
cd backend
python -m pip install -r requirements-dev.txt
env HERE_API_KEY= LLM_PROVIDER=none python -m pytest
PYTHONPATH=. python tests/smoke_orchestrator.py
```

Frontend:

```bash
cd frontend
npm run lint
npm run build        # runs tsc -b, then vite build
```

CI (`.github/workflows/ci.yml`) runs the same checks on pushes and pull requests. The portfolio deployment workflow is
separate and unchanged.

## API

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | Status, `here_configured`, and `ai` provider status (no secrets). |
| `GET /geocode/search?q=...` | Place lookup. |
| `POST /chat` | `{message, session_id?}` -> `{session_id, reply, result, context}`. |
| `POST /trip/options` | Set fuel type and travel mode (driving, walking, cycling). |
| `POST /ev/configure` | Turn the optional EV Planner on or off and set vehicle details. |
| `GET /session/{id}`, `DELETE /session/{id}` | Read or end a session. |
| `GET /tata-chargers*` | Bundled charging dataset queries (used by the EV Planner). |
| `GET /mcp/tools` | Internal capability registry. |

Errors use one shape: `{"detail": "...", "error": {"code", "message", "retryable", "request_id"}}`. Messages are fixed
and safe to show to users.

## Limitations

- Sessions are held in memory, so a backend restart clears them. The UI explains this when it happens.
- Fuel use, fuel cost and refuelling stops are not calculated.
- Charger data is a bundled static dataset, not live availability or pricing.
- Corridor searches along very long routes sample up to 20 points; the UI notes when coverage may be partial.
- A general-knowledge answer needs the optional AI provider.

## Demo video

[Open or play the demo video](assets/Recording-2.mp4) (recorded before the HerePilot-AI redesign).

Earlier release notes are kept in `FIX_NOTES_V4.*.md`.
