# GeoPilot AI 4.2.0 — Fix Notes

## What changed

1. **HERE route rendering**
   - The backend now preserves every raw HERE `sections[*].polyline`.
   - The frontend renders those exact HERE Flexible Polylines with HERE Maps JS `H.geo.LineString.fromFlexiblePolyline`.
   - GeoJSON geometry remains available for EV/POI calculations.
   - Backend returns endpoint validation diagnostics so a corrupted decode is visible instead of silently accepted.
   - `flexpolyline==0.1.0` is preferred when installed; the existing local decoder remains as an offline fallback.

2. **EV charger popup/details**
   - EV markers retain their dedicated GeoPilot/Tata charging source and rich popup.
   - Clicking a charging-plan row now flies to the charger and opens its popup.

3. **HERE Places popup/details**
   - HERE POI markers now open a rich popup with category, query, distance, rating when supplied, address, phone, website, opening hours and source.
   - Clicking a POI in the drawer also opens its map popup.

4. **More powerful HERE Places search**
   - Generic HERE Discover queries now accept natural-language search terms.
   - Examples: `5 star hotel`, `pub bar`, `nightlife`, `shopping mall`, `bank`, `ATM`, `car repair`, etc.
   - EV charging remains the only intentionally separate provider path.

5. **Conversation fixes**
   - `Find 10 tourist places near Goa and show their names` → 10 HERE tourist results near Goa.
   - `Find 5 star hotel in Goa` → HERE query `5 star hotel` near Goa.
   - `Where can I find the best pub & bar in Goa?` → HERE query for pub/bar near Goa.
   - `Share me hotels in 2nd charging stop` → HERE hotels around EV-plan charging stop #2.
   - `Share all charger details on my route` → GeoPilot EV charging layer, not generic HERE Places.

## Run

Backend:

```powershell
cd C:\Users\prdas4\GeoPilot-AI\backend
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

Frontend in another terminal:

```powershell
cd C:\Users\prdas4\GeoPilot-AI\frontend
npm install
npm run dev
```

Then open:

`http://localhost:5173`

## Regression validation completed before packaging

- Python compile: PASS
- Flexible Polyline local round-trip test: PASS
- Intent/EV/context/POI tests: **9/9 PASS**

The package does not contain `.env` or any API key. Keep the existing local HERE/Groq keys in your local `.env` files.
