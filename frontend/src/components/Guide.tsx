import { useState } from "react";
import type { GuideCard, PlaceItem } from "../types";
import { hasTripadvisorData } from "../format";
import { useClock } from "../useClock";
import { PlaceRow, TripadvisorCredit, type ShowHandler } from "./PlaceRow";
import { WeatherPanel } from "./Weather";

const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
const LEVEL_WORDS = { best: "best time", good: "good time", avoid: "harder time", none: "no advice" } as const;

type Level = keyof typeof LEVEL_WORDS;

function MonthStrip({ card }: { card: GuideCard }) {
  const now = new Date(useClock()).getMonth() + 1;
  const level = (month: number): Level =>
    card.bestMonths.includes(month) ? "best" : card.goodMonths.includes(month) ? "good" : card.avoidMonths.includes(month) ? "avoid" : "none";
  return (
    <div className="months">
      <ol className="month-strip" aria-label="Months of the year">
        {MONTHS.map((name, i) => {
          const month = i + 1;
          const l = level(month);
          return (
            <li key={name} className={`month month-${l}${month === now ? " month-now" : ""}`}>
              <span aria-hidden="true">{name[0]}</span>
              <span className="sr-only">{name}: {LEVEL_WORDS[l]}{month === now ? ", this month" : ""}</span>
            </li>
          );
        })}
      </ol>
      <ul className="month-legend" aria-hidden="true">
        {card.bestMonths.length > 0 && <li><i className="swatch swatch-best" />Best</li>}
        {card.goodMonths.length > 0 && <li><i className="swatch swatch-good" />Good</li>}
        {card.avoidMonths.length > 0 && <li><i className="swatch swatch-avoid" />Harder</li>}
        <li><i className="swatch swatch-now" />This month</li>
      </ul>
    </div>
  );
}

function Hero({ card }: { card: GuideCard }) {
  const [failed, setFailed] = useState(false);
  const showPhoto = card.heroPhoto && !failed;
  return (
    <header className={`guide-hero${showPhoto ? " guide-hero-photo" : ""}`}>
      {showPhoto && <img className="guide-hero-img" src={card.heroPhoto} alt="" loading="lazy" referrerPolicy="no-referrer" onError={() => setFailed(true)} />}
      <div className="guide-hero-shade" aria-hidden="true" />
      <div className="guide-hero-body">
        <span className="eyebrow"><span aria-hidden="true">✦ </span>{card.ai ? "AI travel guide" : "Top places"}</span>
        <h3 className="guide-title">{card.place}</h3>
        {card.region && <p className="guide-region">{card.region}</p>}
        {card.bestLabel && <span className="window-chip"><span className="window-label">Best time</span> {card.bestLabel}</span>}
        {showPhoto && card.heroName && <span className="hero-credit">Photo: {card.heroName} · Tripadvisor via HERE</span>}
      </div>
    </header>
  );
}

function PlaceList({ items, onShow, start = 0 }: { items: PlaceItem[]; onShow: ShowHandler; start?: number }) {
  if (!items.length) return null;
  return (
    <ol className="place-list guide-places">
      {items.map((item, i) => <PlaceRow key={`${item.key}-${i}`} item={item} index={start + i} onShow={onShow} />)}
    </ol>
  );
}

export default function GuideCardView({ card, onShow }: { card: GuideCard; onShow: ShowHandler }) {
  const thisMonth = new Date(useClock()).getMonth() + 1;
  const allItems = [...card.sections.flatMap((s) => s.items), ...card.more];
  const hasStrip = card.bestMonths.length + card.goodMonths.length + card.avoidMonths.length > 0;
  // Where each section's numbering starts, so place numbers run on from one section to the next.
  const starts: number[] = [];
  let running = 0;
  for (const section of card.sections) {
    starts.push(running);
    running += section.items.length;
  }
  return (
    <section className="guide card" aria-label={`Travel guide: ${card.place}`}>
      <Hero card={card} />
      <div className="guide-body">
        {!card.ai && (
          <p className="card-state card-state-info" role="status">
            The AI service that writes travel advice isn't available right now, so I haven't guessed at when to go. These are the best-known places HERE lists.
          </p>
        )}
        {card.status === "unavailable" && (
          <p className="card-state card-state-warn" role="status">Place search is unavailable right now, so this guide has no places.</p>
        )}
        {card.status === "empty" && card.ai && <p className="card-state" role="status">HERE lists no notable places here.</p>}
        {hasStrip && <MonthStrip card={card} />}
        {card.weather && <WeatherPanel weather={card.weather} place={card.place} days={5} showClimate highlightMonth={thisMonth} />}

        {card.sections.map((section, sectionIndex) => {
          const start = starts[sectionIndex];
          return (
            <article className="guide-section" key={section.title}>
              <header className="guide-section-head">
                <h4 className="guide-section-title">{section.title}</h4>
                {section.monthsLabel && <span className="months-badge">{section.monthsLabel}</span>}
                {section.badge && <span className="tag">{section.badge}</span>}
              </header>
              {section.summary && <p className="guide-section-copy">{section.summary}</p>}
              {section.bullets.length > 0 && (
                <ul className="guide-bullets">{section.bullets.map((b) => <li key={b}>{b}</li>)}</ul>
              )}
              <PlaceList items={section.items} onShow={onShow} start={start} />
            </article>
          );
        })}

        {card.more.length > 0 && (
          <article className="guide-section">
            <header className="guide-section-head">
              <h4 className="guide-section-title">{card.sections.length ? "More worth a look" : `Top places in ${card.place}`}</h4>
            </header>
            <PlaceList items={card.more} onShow={onShow} start={running} />
          </article>
        )}

        {card.tips.length > 0 && (
          <aside className="guide-tips" aria-label="Good to know">
            <h4 className="guide-section-title">Good to know</h4>
            <ul className="guide-bullets">{card.tips.map((t) => <li key={t}>{t}</li>)}</ul>
          </aside>
        )}

        <footer className="guide-sources">
          <ul className="source-chips" aria-label="Where this comes from">
            {card.ai && <li className="source-chip source-ai"><span aria-hidden="true">✦ </span>Advice: AI general knowledge, not live data</li>}
            {allItems.length > 0 && <li className="source-chip source-here">Places, hours: HERE</li>}
          </ul>
          {card.caveat && <p className="card-foot">{card.caveat}</p>}
          {hasTripadvisorData(allItems) && <TripadvisorCredit />}
        </footer>
      </div>
    </section>
  );
}
