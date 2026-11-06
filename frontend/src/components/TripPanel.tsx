import { placeKey } from "../cards";
import { formatDuration, formatKm, modeLabel } from "../format";
import type { Context, PlaceItem, TravelMode, VehicleType } from "../types";

type Props = {
  context: Context | null;
  busy: boolean;
  onSend: (text: string) => void;
  onShow: (item: PlaceItem) => void;
  onAddStop: () => void;
  onSetOptions: (options: { vehicle_type?: VehicleType | null; travel_mode?: TravelMode }) => void;
};

const FUEL_OPTIONS: Array<{ value: VehicleType; label: string }> = [
  { value: "petrol", label: "Petrol" },
  { value: "diesel", label: "Diesel" },
  { value: "hybrid", label: "Hybrid" },
  { value: "electric", label: "Electric" },
];

const MODE_OPTIONS: Array<{ value: TravelMode; label: string }> = [
  { value: "car", label: "Driving" },
  { value: "pedestrian", label: "Walking" },
  { value: "bicycle", label: "Cycling" },
];

export default function TripPanel({ context, busy, onSend, onShow, onAddStop, onSetOptions }: Props) {
  const route = context?.route ?? null;
  const stops = context?.stops ?? [];
  const positions = route?.stop_positions ?? [];

  return (
    <div className="panel-body">
      <section aria-labelledby="trip-heading" className="panel-section">
        <h2 id="trip-heading" className="panel-title">Your trip</h2>
        {!route || stops.length < 2 ? (
          <div className="empty">
            <p>No trip yet. Tell me where you're starting and where you're going.</p>
            <button type="button" className="chip" disabled={busy} onClick={() => onSend("Plan a trip from Mumbai to Bhubaneswar")}>
              Plan a trip: Mumbai to Bhubaneswar
            </button>
          </div>
        ) : (
          <>
            <dl className="metrics">
              <div><dt>Distance</dt><dd>{formatKm(route.distance_km)}</dd></div>
              <div><dt>{modeLabel(route.transport_mode || context?.travel_mode)}</dt><dd>{formatDuration(route.duration_minutes)}</dd></div>
              <div><dt>Stops</dt><dd>{stops.length}</dd></div>
            </dl>
            <ol className="stop-list">
              {stops.map((stop, index) => {
                const position = positions[index];
                const removable = stop.role === "waypoint";
                return (
                  <li key={`${stop.name}-${index}`} className={`stop stop-${stop.role}`}>
                    <button
                      type="button"
                      className="stop-main"
                      onClick={() => onShow({ key: placeKey(stop.latitude, stop.longitude), name: stop.name, category: "place", latitude: stop.latitude, longitude: stop.longitude })}
                      aria-label={`${stop.name}, ${stop.role}. Show on map.`}
                    >
                      <span className="stop-badge" aria-hidden="true">{stop.role === "origin" ? "A" : stop.role === "destination" ? "B" : index}</span>
                      <span className="stop-text">
                        <span className="stop-name">{stop.name}</span>
                        <span className="stop-sub">
                          {stop.role === "origin" ? "Start" : stop.role === "destination" ? "Destination" : "Stop"}
                          {position && index > 0 ? ` · ${formatKm(position.distance_from_start_km)} · ${formatDuration(position.duration_from_start_minutes)} from start` : ""}
                        </span>
                      </span>
                    </button>
                    {removable && (
                      <button type="button" className="btn btn-ghost" disabled={busy} onClick={() => onSend(`Remove ${stop.name} from my route`)} aria-label={`Remove ${stop.name} from route`}>
                        Remove
                      </button>
                    )}
                  </li>
                );
              })}
            </ol>
            <button type="button" className="btn btn-secondary" disabled={busy} onClick={onAddStop}>Add a stop…</button>
          </>
        )}
      </section>

      <section aria-labelledby="options-heading" className="panel-section">
        <h2 id="options-heading" className="panel-title">Trip options</h2>
        <div className="field">
          <label htmlFor="travel-mode">Travel mode</label>
          <select
            id="travel-mode"
            value={context?.travel_mode ?? "car"}
            disabled={busy}
            onChange={(event) => onSetOptions({ travel_mode: event.target.value as TravelMode })}
          >
            {MODE_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
        </div>
        <div className="field">
          <label htmlFor="fuel-type">Vehicle fuel type</label>
          <select
            id="fuel-type"
            value={context?.vehicle_type ?? ""}
            disabled={busy}
            onChange={(event) => onSetOptions({ vehicle_type: event.target.value as VehicleType })}
          >
            <option value="" disabled>Not specified</option>
            {FUEL_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
        </div>
        <p className="hint">
          Fuel type is saved with your trip. HerePilot-AI does not calculate fuel use, cost or refuelling stops.
          {context?.vehicle_type === "electric" && !context.ev_mode ? " For charging plans, turn on the optional EV Planner." : ""}
        </p>
      </section>
    </div>
  );
}
