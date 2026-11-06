import { useState } from "react";
import { dayColour, formatDuration, formatKm, hasTripadvisorData, hotelSearchLinks, monthName, WHEN_LABEL, wegoLink } from "../format";
import type { ItineraryCard, ItineraryStop, PlaceItem, StayItem } from "../types";
import { PlaceRow, TripadvisorCredit, type ShowHandler } from "./PlaceRow";
import { WeatherPanel } from "./Weather";

function Hero({ card }: { card: ItineraryCard }) {
  const [failed, setFailed] = useState(false);
  const showPhoto = card.heroPhoto && !failed;
  return (
    <header className={`guide-hero itin-hero${showPhoto ? " guide-hero-photo" : ""}`}>
      {showPhoto && <img className="guide-hero-img" src={card.heroPhoto} alt="" loading="lazy" referrerPolicy="no-referrer" onError={() => setFailed(true)} />}
      <div className="guide-hero-shade" aria-hidden="true" />
      <div className="guide-hero-body">
        <span className="eyebrow"><span aria-hidden="true">✦ </span>{card.ai ? `${card.days}-day plan` : "Places to plan with"}</span>
        <h3 className="guide-title">{card.destination}</h3>
        {card.region && <p className="guide-region">{card.region}</p>}
        <div className="itin-chips">
          {card.origin && card.route && (
            <span className="window-chip"><span className="window-label">From {card.origin}</span> {formatKm(card.route.distanceKm)} · {formatDuration(card.route.durationMinutes)} by road</span>
          )}
          {card.bestLabel && <span className="window-chip"><span className="window-label">Best time</span> {card.bestLabel}</span>}
        </div>
        {card.party.length > 0 && <p className="itin-party">For: {card.party.join(", ")}</p>}
        {showPhoto && card.heroName && <span className="hero-credit">Photo: {card.heroName} · Tripadvisor via HERE</span>}
      </div>
    </header>
  );
}

function StopBlock({ stop, day, onShow }: { stop: ItineraryStop; day: number; onShow: ShowHandler }) {
  const { slot, place, meal } = stop;
  const items: Array<{ item: PlaceItem; meal: boolean }> = [];
  if (place) items.push({ item: place, meal: false });
  if (meal) items.push({ item: meal, meal: true });
  return (
    <li className="itin-slot" style={{ ["--day" as string]: dayColour(day) }}>
      <div className="itin-when">{WHEN_LABEL[slot.when] || "Time"}</div>
      <div className="itin-slot-body">
        {slot.activity && <p className="itin-activity">{slot.activity}</p>}
        {items.map(({ item, meal: isMeal }, i) => (
          // PlaceRow renders an <li>, so each sits in its own list; a list item inside a list item is invalid HTML.
          <ol key={`${item.key}-${i}`} className="place-list itin-places itin-place">
            {isMeal && <span className="itin-meal-tag">Meal</span>}
            <PlaceRow item={item} onShow={onShow} />
          </ol>
        ))}
        {slot.tip && <p className="itin-tip"><span aria-hidden="true">Tip · </span>{slot.tip}</p>}
      </div>
    </li>
  );
}

function StayRow({ stay, onShow }: { stay: StayItem; onShow: ShowHandler }) {
  const links = hotelSearchLinks(stay.place.name, stay.place.city);
  return (
    <li className={`stay${stay.picked ? " stay-picked" : ""}`}>
      {stay.picked && <span className="tag stay-tag">Suggested</span>}
      <ol className="place-list"><PlaceRow item={stay.place} onShow={onShow} /></ol>
      {stay.why && <p className="stay-why">{stay.why}</p>}
      <div className="stay-links">
        <span className="stay-links-label">Check prices and availability:</span>
        {links.map((l) => (
          <a key={l.label} className="btn-link" href={l.href} target="_blank" rel="noopener noreferrer">{l.label}<span className="sr-only"> for {stay.place.name} (opens in a new tab)</span></a>
        ))}
        <a className="btn-link" href={wegoLink(stay.place.latitude, stay.place.longitude)} target="_blank" rel="noopener noreferrer">HERE WeGo<span className="sr-only"> map of {stay.place.name} (opens in a new tab)</span></a>
      </div>
    </li>
  );
}

export default function ItineraryCardView({ card, onShow, onSend }: { card: ItineraryCard; onShow: ShowHandler; onSend: (text: string) => void }) {
  const allItems = [
    ...card.dayCards.flatMap((d) => d.stops.flatMap((s) => [s.place, s.meal])),
    ...card.stay.map((s) => s.place),
    ...card.moreToSee,
  ].filter((p): p is PlaceItem => Boolean(p));
  return (
    <section className="guide itinerary card" aria-label={`Trip plan: ${card.destination}`}>
      <Hero card={card} />
      <div className="guide-body">
        {!card.ai && (
          <p className="card-state card-state-info" role="status">
            The AI that writes day-by-day plans isn't available right now, so I haven't made one up. These are the best-known places, hotels and restaurants HERE lists.
          </p>
        )}
        {card.status === "empty" && <p className="card-state" role="status">HERE lists no notable places here to plan around.</p>}
        {card.summary && <p className="itin-summary">{card.summary}</p>}
        {card.gettingThere && (
          <p className="itin-getting"><b>Getting there.</b> {card.gettingThere}{card.route ? ` By road it is ${formatKm(card.route.distanceKm)}, about ${formatDuration(card.route.durationMinutes)} (HERE).` : ""}</p>
        )}
        {card.weather && <WeatherPanel weather={card.weather} place={card.destination} days={5} showClimate highlightMonth={card.month} />}

        {card.dayCards.map((day) => (
          <article className="itin-day" key={day.day} style={{ ["--day" as string]: dayColour(day.day) }}>
            <header className="itin-day-head">
              <span className="itin-day-num" aria-hidden="true">{day.day}</span>
              <div>
                <h4 className="itin-day-title"><span className="sr-only">Day {day.day}: </span>{day.title}</h4>
                {day.summary && <p className="itin-day-summary">{day.summary}</p>}
              </div>
            </header>
            <ol className="itin-slots">
              {day.stops.map((stop, i) => <StopBlock key={i} stop={stop} day={day.day} onShow={onShow} />)}
            </ol>
          </article>
        ))}

        {card.stay.length > 0 && (
          <article className="guide-section">
            <header className="guide-section-head">
              <h4 className="guide-section-title">Where to stay</h4>
              <span className="tag">Your choice: I can't book</span>
            </header>
            <ul className="stay-list">{card.stay.map((s) => <StayRow key={s.place.key} stay={s} onShow={onShow} />)}</ul>
            <p className="card-foot">Hotels and ratings come from HERE. Prices and availability aren't in HERE's data, so the links open a search on another site.</p>
          </article>
        )}

        {card.moreToSee.length > 0 && (
          <article className="guide-section">
            <header className="guide-section-head"><h4 className="guide-section-title">More worth a look</h4></header>
            <ol className="place-list guide-places">{card.moreToSee.map((item, i) => <PlaceRow key={`${item.key}-${i}`} item={item} index={i} onShow={onShow} />)}</ol>
          </article>
        )}

        {card.tips.length > 0 && (
          <aside className="guide-tips" aria-label="Good to know">
            <h4 className="guide-section-title">Good to know</h4>
            <ul className="guide-bullets">{card.tips.map((t) => <li key={t}>{t}</li>)}</ul>
          </aside>
        )}

        <div className="itin-actions" role="group" aria-label="Refine this plan">
          {card.ai && <button type="button" className="chip" onClick={() => onSend(`Plan ${card.days + 1} days in ${card.destination}${card.origin ? ` from ${card.origin}` : ""}`)}>Make it {card.days + 1} days</button>}
          <button type="button" className="chip" onClick={() => onSend(`Best restaurants in ${card.destination}`)}>Best restaurants</button>
          <button type="button" className="chip" onClick={() => onSend(`Best time to visit ${card.destination}`)}>Best time to visit</button>
        </div>

        <footer className="guide-sources">
          <ul className="source-chips" aria-label="Where this comes from">
            {card.ai && <li className="source-chip source-ai"><span aria-hidden="true">✦ </span>Plan and advice: AI general knowledge, not live data</li>}
            {allItems.length > 0 && <li className="source-chip source-here">Places, hours, route: HERE</li>}
            {card.weather && <li className="source-chip source-wx">Weather: Open-Meteo</li>}
          </ul>
          {card.caveats.length > 0 && <ul className="card-notes">{card.caveats.map((c) => <li key={c}>{c}</li>)}</ul>}
          {card.bestMonths.length > 0 && <p className="card-foot">Suggested months: {card.bestMonths.map(monthName).join(", ")}. Check the weather chart above, which uses measured data.</p>}
          {hasTripadvisorData(allItems) && <TripadvisorCredit />}
        </footer>
      </div>
    </section>
  );
}
