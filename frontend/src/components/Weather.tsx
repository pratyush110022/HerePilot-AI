import { monthName, shortDate } from "../format";
import type { ClimateMonth, PlaceWeather, WeatherDay, WeatherKind, WeatherNow } from "../types";

/** Small inline icons (no image files, no emoji, so they look the same everywhere and scale with the text). */
export function WeatherIcon({ kind, size = 22 }: { kind: WeatherKind; size?: number }) {
  const common = { width: size, height: size, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 1.8, strokeLinecap: "round" as const, strokeLinejoin: "round" as const, "aria-hidden": true };
  const cloud = <path d="M7 18h9.5a3.5 3.5 0 0 0 .5-6.96A5 5 0 0 0 7.4 9.6 4.2 4.2 0 0 0 7 18Z" />;
  switch (kind) {
    case "clear":
      return <svg {...common} className="wx wx-clear"><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" /></svg>;
    case "partly":
      return <svg {...common} className="wx wx-partly"><circle cx="8" cy="8" r="2.8" /><path d="M8 2v1.2M2 8h1.2M3.8 3.8l.9.9M12.2 3.8l-.9.9" /><path d="M9 20h8.5a3 3 0 0 0 .4-5.97A4.4 4.4 0 0 0 9.8 12.8 3.6 3.6 0 0 0 9 20Z" /></svg>;
    case "rain":
      return <svg {...common} className="wx wx-rain">{cloud}<path d="M8 20l-1 2M12 20l-1 2M16 20l-1 2" /></svg>;
    case "drizzle":
      return <svg {...common} className="wx wx-rain">{cloud}<path d="M9 21v.01M13 21v.01M17 21v.01" /></svg>;
    case "storm":
      return <svg {...common} className="wx wx-storm">{cloud}<path d="M12 14l-2.5 4h3L11 22" /></svg>;
    case "snow":
      return <svg {...common} className="wx wx-snow">{cloud}<path d="M8 20h.01M12 21h.01M16 20h.01" /></svg>;
    case "fog":
      return <svg {...common} className="wx wx-fog"><path d="M4 9h16M3 13h18M6 17h12" /></svg>;
    default:
      return <svg {...common} className="wx wx-cloudy">{cloud}</svg>;
  }
}

const deg = (value: number | null | undefined): string => (value == null ? "–" : `${Math.round(value)}°`);

function Now({ now }: { now: WeatherNow }) {
  return (
    <div className="wx-now">
      <WeatherIcon kind={now.kind} size={34} />
      <div>
        <div className="wx-now-temp">{deg(now.temperature_c)}<span className="wx-unit">C</span></div>
        <div className="wx-now-cond">{now.condition}</div>
      </div>
      <ul className="wx-now-facts" aria-label="Current conditions">
        {now.feels_like_c != null && <li>Feels like {deg(now.feels_like_c)}</li>}
        {now.humidity_percent != null && <li>Humidity {Math.round(now.humidity_percent)}%</li>}
        {now.wind_kmh != null && <li>Wind {Math.round(now.wind_kmh)} km/h</li>}
      </ul>
    </div>
  );
}

function Days({ days, limit }: { days: WeatherDay[]; limit: number }) {
  return (
    <ol className="wx-days" aria-label="Forecast">
      {days.slice(0, limit).map((day, i) => (
        <li key={day.date} className="wx-day">
          <span className="wx-day-name">{i === 0 ? "Today" : shortDate(day.date).split(" ")[0]}</span>
          <WeatherIcon kind={day.kind} size={20} />
          <span className="wx-day-temp"><b>{deg(day.temp_max_c)}</b> <span>{deg(day.temp_min_c)}</span></span>
          {day.rain_chance_percent != null && day.rain_chance_percent >= 20 && <span className="wx-day-rain">{Math.round(day.rain_chance_percent)}% rain</span>}
          <span className="sr-only">{day.condition}</span>
        </li>
      ))}
    </ol>
  );
}

/** Typical weather for each month of the year, with the travel month marked. Bars show the usual daily high; drops show rainy days. */
export function ClimateChart({ months, highlight, years }: { months: ClimateMonth[]; highlight?: number; years?: string }) {
  const highs = months.map((m) => m.temp_max_c).filter((v): v is number => v != null);
  if (!highs.length) return null;
  const lo = Math.min(...highs) - 4;
  const hi = Math.max(...highs) + 1;
  const maxRain = Math.max(1, ...months.map((m) => m.rainy_days ?? 0));
  return (
    <figure className="climate">
      <figcaption className="climate-title">Typical weather by month{years ? <span> · average of {years}</span> : null}</figcaption>
      <ol className="climate-bars">
        {months.map((m) => {
          const h = m.temp_max_c == null ? 0 : Math.max(8, Math.round(((m.temp_max_c - lo) / (hi - lo)) * 100));
          const wet = (m.rainy_days ?? 0) / maxRain;
          return (
            <li key={m.month} className={`climate-col${m.month === highlight ? " climate-now" : ""}`}>
              <span className="climate-temp">{deg(m.temp_max_c)}</span>
              <span className="climate-bar" style={{ height: `${h}%` }} aria-hidden="true">
                <i className="climate-rain" style={{ height: `${Math.round(wet * 100)}%` }} />
              </span>
              <span className="climate-m" aria-hidden="true">{monthName(m.month)[0]}</span>
              <span className="sr-only">
                {monthName(m.month)}: usually {deg(m.temp_max_c)} by day and {deg(m.temp_min_c)} at night, about {m.rainy_days ?? "no"} rainy days
              </span>
            </li>
          );
        })}
      </ol>
      <p className="climate-legend"><i className="swatch swatch-hot" />Daytime high <i className="swatch swatch-wet" />Rainy days</p>
    </figure>
  );
}

/** Weather for a result: today and the days ahead, optionally with the month-by-month climate. */
export function WeatherPanel({ weather, place, days = 5, showClimate = false, highlightMonth }: { weather: PlaceWeather; place?: string; days?: number; showClimate?: boolean; highlightMonth?: number }) {
  const hasNow = Boolean(weather.current && weather.current.temperature_c != null);
  const hasDays = Boolean(weather.days?.length);
  const months = weather.climate?.months ?? [];
  if (!hasNow && !hasDays && !(showClimate && months.length)) return null;
  return (
    <section className="weather" aria-label={`Weather${place ? ` in ${place}` : ""}`}>
      <header className="weather-head">
        <h4 className="weather-title">Weather{place ? <span> in {place}</span> : null}</h4>
        <span className="source-chip source-wx">Open-Meteo · live</span>
      </header>
      {hasNow && weather.current && <Now now={weather.current} />}
      {hasDays && weather.days && <Days days={weather.days} limit={days} />}
      {showClimate && months.length > 0 && <ClimateChart months={months} highlight={highlightMonth} years={weather.climate?.years} />}
    </section>
  );
}

/** A one-line weather summary for a destination card. */
export function WeatherChip({ now, days }: { now?: WeatherNow | null; days?: WeatherDay[] }) {
  if (!now || now.temperature_c == null) return null;
  const wet = (days ?? []).slice(0, 3).filter((d) => (d.rain_chance_percent ?? 0) >= 50).length;
  return (
    <span className="wx-chip" title={now.condition}>
      <WeatherIcon kind={now.kind} size={16} />
      <b>{deg(now.temperature_c)}</b> now{wet > 0 ? ` · rain likely ${wet} of next ${Math.min(3, days?.length ?? 3)} days` : ""}
    </span>
  );
}
