import { useState } from "react";
import { categoryIconPath, categoryLabel, formatCount } from "../format";
import type { PlaceItem } from "../types";
import { useClock } from "../useClock";

export type ShowHandler = (item: PlaceItem) => void;

/** HERE's open-now flag describes the moment of the search, so it is only shown while that is recent. */
const OPEN_FLAG_MAX_AGE_MS = 30 * 60 * 1000;

function openStatus(item: PlaceItem, now: number): "open" | "closed" | null {
  if (item.openNow === undefined || item.openNowAt === undefined) return null;
  if (now - item.openNowAt > OPEN_FLAG_MAX_AGE_MS) return null;
  return item.openNow ? "open" : "closed";
}

export function TripadvisorCredit() {
  return <p className="card-foot card-credit">Photos and ratings: Tripadvisor, supplied by HERE.</p>;
}

function PlaceThumb({ item, index }: { item: PlaceItem; index?: number }) {
  const [failed, setFailed] = useState(false);
  return (
    <span className="place-thumb">
      {item.photo && !failed ? (
        <img className="place-photo" src={item.photo} alt="" width={64} height={64} loading="lazy" referrerPolicy="no-referrer" onError={() => setFailed(true)} />
      ) : (
        <img className="place-icon" src={categoryIconPath(item.category)} alt="" width={28} height={28} />
      )}
      {index !== undefined && <span className="place-index" aria-hidden="true">{index + 1}</span>}
    </span>
  );
}

export function PlaceRow({ item, index, onShow }: { item: PlaceItem; index?: number; onShow: ShowHandler }) {
  const status = openStatus(item, useClock());
  const rated = item.rating !== undefined && item.rating !== null;
  return (
    <li className="place">
      <button type="button" className="place-main" onClick={() => onShow(item)} aria-label={`${item.name}. Show on map.`}>
        <PlaceThumb item={item} index={index} />
        <span className="place-body">
          <span className="place-name">{item.name}</span>
          <span className="place-sub">{item.hereCategory || categoryLabel(item.category)}{item.address ? ` · ${item.address}` : item.city ? ` · ${item.city}` : ""}</span>
          <span className="place-meta">
            {rated && (
              <span className="pill pill-rating">
                {/* Plain text on purpose: Tripadvisor's rules do not allow our own star or bubble graphics for its ratings. */}
                {Number(item.rating).toFixed(1)}<span className="pill-count">/5{item.ratingCount !== undefined ? ` · ${formatCount(item.ratingCount)} reviews` : ""}</span>
                <span className="sr-only"> on Tripadvisor</span>
              </span>
            )}
            {status && <span className={`pill pill-${status}`}>{status === "open" ? "Open now" : "Closed now"}</span>}
            {item.distanceLabel && <span className="pill">{item.distanceLabel}</span>}
            {item.openingHours && <span className="pill pill-muted">{item.openingHours}</span>}
          </span>
        </span>
        <span className="place-go" aria-hidden="true">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 21s7-6.2 7-11.5A7 7 0 0 0 5 9.5C5 14.800 12 21 12 21Z" /><circle cx="12" cy="9.500" r="2.500" /></svg>
        </span>
      </button>
      {(item.website || item.phone || item.ratingUrl) && (
        <span className="place-links">
          {item.website && <a href={item.website} target="_blank" rel="noopener noreferrer">Website<span className="sr-only"> for {item.name} (opens in a new tab)</span></a>}
          {item.ratingUrl && <a href={item.ratingUrl} target="_blank" rel="noopener noreferrer">Reviews on Tripadvisor<span className="sr-only"> for {item.name} (opens in a new tab)</span></a>}
          {item.phone && <span className="place-phone">{item.phone}</span>}
        </span>
      )}
    </li>
  );
}
