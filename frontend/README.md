# HerePilot-AI frontend

React + TypeScript + Vite single-page app for HerePilot-AI, a conversational map assistant. The map is the HERE Maps
JavaScript SDK 3.1, loaded from HERE's hosted CDN.

## Environment

Create `.env` (see `.env.example`):

```text
VITE_BACKEND_URL=http://localhost:8000
VITE_HERE_API_KEY=your_here_api_key
```

`VITE_HERE_API_KEY` is used only by the browser to draw the map. Without it the app shows a clear message in place of
the map and chat still works. Restrict the key to your origins in the HERE portal.

## Scripts

```bash
npm install
npm run dev        # http://localhost:5173/  (use localhost; Vite may listen on IPv6 only)
npm run lint       # oxlint
npm run build      # tsc -b, then vite build
```

## Layout

- `src/App.tsx` — page layout and tabs: **Chat**, **Trip**, and the optional **EV Planner**.
- `src/useConversation.ts` — conversation state, retry, health polling and persistence. The session ID and the last 40
  messages are kept in `localStorage` under `herepilot-state-v1`.
- `src/api.ts` — API client. Failures are turned into fixed, user-safe messages.
- `src/cards.ts` — builds result cards from API responses.
- `src/MapView.tsx`, `src/mapMarkers.ts`, `src/hereMapAdapter.ts` — map drawing. Marker and popup content is built with
  DOM APIs and `textContent`, never by inserting provider data as HTML.

EV content is shown only while the EV Planner is switched on.
