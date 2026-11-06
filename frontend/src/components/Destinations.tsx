import { useState } from "react";
import { formatDuration, formatKm } from "../format";
import type { DestinationItem, DestinationsCard } from "../types";
import type { ShowHandler } from "./PlaceRow";
import { WeatherChip } from "./Weather";

function Card({ item, origin, onShow, onSend }: { item: DestinationItem; origin?: string; onShow: ShowHandler; onSend: (text: string) => void }) {
  const [failed, setFailed] = useState(false);
  const showPhoto = item.photo && !failed;
  const place = item.region ? `${item.name}, ${item.region}` : item.name;
  return (
    <li className="dest">
      <button
        type="button"
        className={`dest-hero${showPhoto ? " dest-hero-photo" : ""}`}
        onClick={() => onShow({ key: `dest:${item.name}`, name: item.name, category: "tourist_attraction", latitude: item.latitude, longitude: item.longitude, address: item.region })}
        aria-label={`${item.name}. Show on map.`}
      >
        {showPhoto && <img className="dest-img" src={item.photo} alt="" loading="lazy" referrerPolicy="no-referrer" onError={() => setFailed(true)} />}
        <span className="dest-shade" aria-hidden="true" />
        <span className="dest-num" aria-hidden="true">{item.order}</span>
        <span className="dest-hero-body">
          <span className="dest-name">{item.name}</span>
          {item.region && <span className="dest-region">{item.region}</span>}
        </span>
      </button>
      <div className="dest-body">
        <ul className="dest-facts" aria-label={`Facts about ${item.name}`}>
          {item.durationMinutes != null && (
            <li className="dest-drive"><b>{formatDuration(item.durationMinutes)}</b> by road{origin ? ` from ${origin}` : ""}{item.distanceKm != null ? ` · ${formatKm(item.distanceKm)}` : ""}</li>
          )}
          {item.bestLabel && <li>Best: <b>{item.bestLabel}</b></li>}
          {item.stay && <li>{item.stay}</li>}
        </ul>
        <WeatherChip now={item.weather?.current} days={item.weather?.days} />
        {item.why && <p className="dest-why">{item.why}</p>}
        {item.highlights.length > 0 && <ul className="dest-tags">{item.highlights.map((h) => <li key={h} className="tag">{h}</li>)}</ul>}
        {item.suits && <p className="dest-suits">Suits: {item.suits}</p>}
        {item.topPlaces.length > 0 && <p className="dest-top">On the map here: {item.topPlaces.map((p) => p.name).join(" · ")}</p>}
        <div className="dest-actions">
          <button type="button" className="chip" onClick={() => onSend(`Plan a 2 day trip to ${place}${origin && origin !== "your location" ? ` from ${origin}` : ""}`)}>Plan a trip</button>
          <button type="button" className="chip" onClick={() => onSend(`Best time to visit ${place}`)}>Best time</button>
          <button type="button" className="chip" onClick={() => onSend(`Hotels in ${item.name}`)}>Hotels</button>
        </div>
      </div>
    </li>
  );
}

export default function DestinationsCardView({ card, onShow, onSend }: { card: DestinationsCard; onShow: ShowHandler; onSend: (text: string) => void }) {
  return (
    <section className="card destinations" aria-label={card.title}>
      <header className="card-head">
        <h3 className="card-title">{card.title.charAt(0).toUpperCase() + card.title.slice(1)}</h3>
        {card.items.length > 0 && <span className="pill">{card.items.length} found</span>}
      </header>
      {card.status === "empty" && <p className="card-state" role="status">I couldn't verify any suggestions on the map right now. Try naming a region or a different kind of place.</p>}
      {card.note && <p className="card-state card-state-info" role="status">{card.note}</p>}
      {card.items.length > 0 && (
        <ol className="dest-list">
          {card.items.map((item) => <Card key={item.name} item={item} origin={card.origin} onShow={onShow} onSend={onSend} />)}
        </ol>
      )}
      <ul className="source-chips" aria-label="Where this comes from">
        <li className="source-chip source-ai"><span aria-hidden="true">✦ </span>Suggestions: AI general knowledge</li>
        <li className="source-chip source-here">Locations, drive times: HERE</li>
        <li className="source-chip source-wx">Weather: Open-Meteo</li>
      </ul>
      <p className="card-foot">Every place was found on the map by HERE before it was shown. Anything HERE couldn't find was left out.</p>
    </section>
  );
}
