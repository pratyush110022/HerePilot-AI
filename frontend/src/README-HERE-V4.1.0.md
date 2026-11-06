# GeoPilot AI — HERE Frontend V4.1.0

This patch replaces the stale MapLibre/OSM frontend map implementation with HERE Maps JavaScript API v3.1 while preserving the existing GeoPilot React UI and backend calls.

## Files

Copy these two files into:

`C:\Users\prdas4\GeoPilot-AI\frontend\src`

- `App.tsx`
- `hereMapAdapter.ts`

## What changed

- Removed the active MapLibre map import.
- Removed the OSM raster base map.
- HERE Maps vector base map is initialized using `VITE_HERE_API_KEY`.
- Existing GeoPilot marker, popup, route and fit-to-route calls are translated to HERE Maps through `hereMapAdapter.ts`.
- Backend URL now correctly uses `VITE_BACKEND_URL`.
- Existing `/health`, `/chat`, `/geocode/search`, `/tata-chargers/bbox`, and route/EV endpoints remain unchanged.
- No npm package installation is required for HERE Maps; the HERE JS SDK is loaded at runtime.

## Frontend .env

Keep exactly one backend URL line:

`VITE_BACKEND_URL=http://127.0.0.1:8000`

and your HERE key:

`VITE_HERE_API_KEY=YOUR_NEW_HERE_KEY`

The HERE key previously exposed in chat should be rotated before production use.

## Run

PowerShell 1 — backend:

`cd C:\Users\prdas4\GeoPilot-AI\backend`

`.\.venv\Scripts\Activate.ps1`

`python -m uvicorn main:app --host 127.0.0.1 --port 8000`

PowerShell 2 — frontend:

`cd C:\Users\prdas4\GeoPilot-AI\frontend`

`npm run dev`

Open the Vite URL and hard-refresh the browser (`Ctrl+Shift+R`).

Expected first checkpoint:

- Backend: Online/healthy
- HERE base map: visible
- No OSM raster tiles
- No MapLibre runtime dependency in App.tsx
