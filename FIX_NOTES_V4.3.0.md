# GeoPilot AI 4.3.0 — integrated EV, weather, HERE Places, and UI fixes

## Implemented

### Weather context
- Added an optional Open-Meteo adapter that fetches current conditions without requiring a weather API key.
- Includes current temperature, feels-like temperature, condition, precipitation, humidity, wind, and observation time where available.
- Adds current weather at trip stops, destination, and planned charging stations to backend context and result payloads.
- Displays weather in destination summaries, charging-station popups, and structured charging-stop detail cards.
- Keeps weather advisory and non-blocking. Unavailable weather is shown honestly, and it never changes the declared EV range or battery/battery-reserve calculation.

### Numbered charging-stop follow-ups
- Explicitly recognizes queries such as `Show me detailed information about charging stop 2` and ordinal variants.
- Uses the exact selected planned stop, preserving its battery arrival/departure numbers and attached weather in the structured response.
- Avoids routing these queries to the generic “recommend charger” intent, which otherwise returns stop 1.

### HERE Places / tourist search
- Adds relevant tourist-attraction fallback terms including places to visit, landmarks, beaches, historic sites, museums, and viewpoints.
- Applies exact great-circle distance filtering, sorts near-to-far, deduplicates results, and respects caller-specified radii.
- Uses a 15 km default for tourist attractions only when the radius is implicit; explicit radii remain explicit.
- Uses a non-country-restricted retry for tourist searches only when the India-constrained searches produce no nearby results, still retaining the requested coordinate-radius filter.
- Keeps location/reference place separate from the natural-language search query and distinguishes provider-unavailable from a successful search with no matches.
- Rejects invalid/non-finite provider coordinates defensively.

### Map and chat UI
- Improves HERE DOM-marker click handling by binding to the rendered/cloned marker and the native HERE tap event.
- Adds structured chat result cards for EV station/charging-stop details, battery percentages, route/charging estimates, weather, nearby POI distances, and trip metrics.
- Preserves structured API result data on the assistant chat message to support the cards.
- Keeps weather timestamps and current-condition disclaimers visible.

## Validation performed

- Backend tests in `backend/tests`: **20 passed**.
- Python bytecode compilation for services and `main.py`: passed.
- TypeScript/TSX transpilation syntax checks: passed.
- A strict TypeScript check using local temporary React ambient stubs: passed.
- Full `npm run build` could not be run because frontend packages were not installed and package-registry access/cache was unavailable in this environment. Run `npm ci` and `npm run build` locally to complete the production build check.
- The legacy standalone `backend/test_groq.py` requires the `groq` package. Run `pip install -r backend/requirements.txt` before running that file or the full backend test collection.

## Local update note

Keep your existing, private `backend/.env` and `frontend/.env` files when applying these source changes. The packaged project intentionally excludes secret-bearing `.env` files, `.git`, virtual environments, caches, and `node_modules`.
