# GeoPilot AI V4.2.1 — LLM + EV popup reliability fix

## Fixed

### 1. Natural-language POI references to trip stops
Requests such as `pub & bar near Goa stop` and `Hotels near Goa stop` now resolve `Goa stop` to the existing Goa trip waypoint instead of geocoding the literal phrase.

### 2. HERE Search response safety
The generic HERE Discover parser now handles unexpected JSON-array payloads defensively and ignores malformed items instead of raising `list object has no attribute get`.

### 3. LLM payload safety
A malformed top-level LLM JSON list is rejected before the dictionary-based intent dispatcher. Deterministic heuristic intent is used instead.

### 4. EV marker popups
EV charging markers now use the same popup path as HERE POIs and retry popup creation if the HERE map/UI object is not ready yet.

### 5. EV charging-row Details
Charging-plan rows now resolve their map marker by stable charger ID instead of comparing floating-point latitude/longitude values. Clicking a charging-plan item flies to the charger and opens its popup reliably.

## Validation

`PYTHONPATH=. pytest -q` → 10 passed.

Frontend build could not be executed in this offline environment because `frontend/node_modules` is not present. The TypeScript source was updated against the existing V4.2.0 project structure.
