import { MAX_MAP_MARKERS } from "../cards";
import { formatDuration, formatKm, hasTripadvisorData } from "../format";
import type { DistanceCard, EvCard, GeocodeCard, PlaceItem, PlacesCard, ResultCard, RouteCard, WeatherCard } from "../types";
import AnswerCardView from "./Answer";
import DestinationsCardView from "./Destinations";
import GuideCardView from "./Guide";
import ItineraryCardView from "./Itinerary";
import { PlaceRow, TripadvisorCredit } from "./PlaceRow";
import { WeatherPanel } from "./Weather";

type ShowHandler = (item: PlaceItem) => void;

function PlacesCardView({ card, onShow }: { card: PlacesCard; onShow: ShowHandler }) {
  return (
    <section className="card" aria-label={card.title}>
      <header className="card-head">
        <h3 className="card-title">{card.title.charAt(0).toUpperCase() + card.title.slice(1)}</h3>
        {card.total > 0 && <span className="pill">{card.total} found</span>}
      </header>
      {card.status === "unavailable" && (
        <p className="card-state card-state-warn" role="status">
          Place search is unavailable right now, so there are no results to show. Nothing has been guessed. {card.suggestion}
        </p>
      )}
      {card.status === "empty" && <p className="card-state" role="status">No results found. {card.suggestion}</p>}
      {card.status === "fallback" && <p className="card-state card-state-info">HERE Search had no usable answer, so these come from a fallback source.</p>}
      {card.items.length > 0 && (
        <ol className="place-list">
          {card.items.map((item, i) => <PlaceRow key={`${item.key}-${i}`} item={item} index={i} onShow={onShow} />)}
        </ol>
      )}
      {card.notes.length > 0 && (
        <ul className="card-notes">
          {card.notes.map((note) => <li key={note}>{note}</li>)}
        </ul>
      )}
      {card.total > card.items.length && (
        <p className="card-foot">
          Showing {card.items.length} of {card.total}.{" "}
          {card.total > MAX_MAP_MARKERS ? `The first ${MAX_MAP_MARKERS} are on the map.` : "All are on the map."}
        </p>
      )}
      {card.source && <p className="card-foot">Source: {card.source}</p>}
      {hasTripadvisorData(card.items) && <TripadvisorCredit />}
    </section>
  );
}

function RouteCardView({ card }: { card: RouteCard }) {
  return (
    <section className="card" aria-label="Route summary">
      <header className="card-head">
        <h3 className="card-title">Route · {card.mode}</h3>
      </header>
      {card.change && <p className="card-state card-state-info">{card.change}</p>}
      <ol className="route-stops">
        {card.stops.map((stop, i) => (
          <li key={`${stop.name}-${i}`} className={`route-stop route-stop-${stop.role}`}>
            <span className="route-stop-dot" aria-hidden="true" />
            <span>{stop.name}<span className="sr-only">, {stop.role}</span></span>
          </li>
        ))}
      </ol>
      <dl className="metrics">
        <div><dt>Distance</dt><dd>{formatKm(card.distanceKm)}</dd></div>
        <div><dt>Travel time</dt><dd>{formatDuration(card.durationMinutes)}</dd></div>
        <div><dt>Stops</dt><dd>{card.stops.length}</dd></div>
      </dl>
      {card.legs.length > 1 && (
        <ul className="legs">
          {card.legs.map((leg, i) => (
            <li key={i}><span>{leg.from} → {leg.to}</span><span>{formatKm(leg.distanceKm)} · {formatDuration(leg.durationMinutes)}</span></li>
          ))}
        </ul>
      )}
      {card.fuelNote && <p className="card-foot">{card.fuelNote}</p>}
    </section>
  );
}

function DistanceCardView({ card }: { card: DistanceCard }) {
  return (
    <section className="card" aria-label="Distance">
      <header className="card-head"><h3 className="card-title">To {card.to}</h3>{card.stopNumber ? <span className="pill">Stop {card.stopNumber}</span> : null}</header>
      <dl className="metrics">
        <div><dt>Distance</dt><dd>{formatKm(card.distanceKm)}</dd></div>
        <div><dt>Travel time</dt><dd>{formatDuration(card.durationMinutes)}</dd></div>
      </dl>
      {card.from && <p className="card-foot">Measured along your route from {card.from}.</p>}
    </section>
  );
}

function GeocodeCardView({ card, onShow }: { card: GeocodeCard; onShow: ShowHandler }) {
  return (
    <section className="card" aria-label="Place lookup">
      <header className="card-head"><h3 className="card-title">{card.query || "Place"}</h3></header>
      <ol className="place-list">
        {card.items.map((item, i) => <PlaceRow key={`${item.key}-${i}`} item={item} index={i} onShow={onShow} />)}
      </ol>
      {card.others.length > 0 && <p className="card-foot">Other matches: {card.others.join("; ")}</p>}
    </section>
  );
}

/** EV content. It is only ever built while the optional EV Planner is on. */
export function EvSummary({ card }: { card: EvCard }) {
  if (card.status === "needs_vehicle") {
    return <p className="card-state card-state-info">Charging plan not calculated yet. Add your vehicle's range and current battery in the EV Planner.</p>;
  }
  if (card.status === "failed") {
    return <p className="card-state card-state-warn" role="status">The charging plan couldn't be calculated this time. Your route is unaffected. Try again in a moment.</p>;
  }
  if (card.status === "ready") {
    return <p className="card-state card-state-info">EV Planner is on. Plan a trip to see charging stops.</p>;
  }
  return (
    <div>
      <p className={`card-state ${card.tripPossible ? "card-state-ok" : "card-state-warn"}`} role="status">
        {card.tripPossible
          ? card.stops.length === 0 ? "No charging stop needed for this route." : `${card.stops.length} charging stop${card.stops.length === 1 ? "" : "s"} planned.`
          : `No feasible charging plan with these settings.${card.failureReason ? ` ${card.failureReason}` : ""}`}
      </p>
      {card.stops.length > 0 && (
        <ol className="charge-list">
          {card.stops.map((stop, i) => (
            <li key={i}>
              <span className="place-index" aria-hidden="true">{i + 1}</span>
              <span className="charge-name">{stop.name}</span>
              <span className="charge-battery">
                {stop.arrival != null ? `${Math.round(stop.arrival)}%` : "—"} → {stop.departure != null ? `${Math.round(stop.departure)}%` : "—"}
              </span>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

function EvCardView({ card }: { card: EvCard }) {
  return (
    <section className="card card-ev" aria-label="EV Planner">
      <header className="card-head"><h3 className="card-title">EV Planner</h3><span className="pill pill-ev">Optional</span></header>
      <EvSummary card={card} />
    </section>
  );
}

function WeatherCardView({ card }: { card: WeatherCard }) {
  return (
    <section className="card card-weather" aria-label={`Weather in ${card.place}`}>
      <WeatherPanel weather={card.weather} place={card.place} days={5} />
    </section>
  );
}

export function CardView({ card, onShow, onSend }: { card: ResultCard; onShow: ShowHandler; onSend: (text: string) => void }) {
  switch (card.kind) {
    case "itinerary": return <ItineraryCardView card={card} onShow={onShow} onSend={onSend} />;
    case "destinations": return <DestinationsCardView card={card} onShow={onShow} onSend={onSend} />;
    case "weather": return <WeatherCardView card={card} />;
    case "places": return <PlacesCardView card={card} onShow={onShow} />;
    case "route": return <RouteCardView card={card} />;
    case "distance": return <DistanceCardView card={card} />;
    case "geocode": return <GeocodeCardView card={card} onShow={onShow} />;
    case "ev": return <EvCardView card={card} />;
    case "guide": return <GuideCardView card={card} onShow={onShow} />;
    case "answer": return <AnswerCardView card={card} onShow={onShow} />;
    default: return null;
  }
}
