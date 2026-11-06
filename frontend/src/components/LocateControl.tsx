import { useEffect, useLayoutEffect, useRef, useState } from "react";

type Props = {
  /** The position is being read right now. */
  locating: boolean;
  /** A position has been shared (the marker is on the map). */
  active: boolean;
  error: string | null;
  onLocate: () => void;
  onStop: () => void;
  onDismissError: () => void;
};

/** How far above HERE's zoom buttons the control sits, so it reads as part of the same column. */
const GAP_PX = 10;

/**
 * "Go to my exact location", in the map's own control column (above the zoom buttons). One click asks the browser for a
 * fresh GPS fix, centres the map on it and lets "near me" searches use it. While a position is shared a small ✕ appears
 * above it to stop sharing. Nothing else is shown permanently; problems appear as a dismissible message.
 */
export default function LocateControl({ locating, active, error, onLocate, onStop, onDismissError }: Props) {
  const stackRef = useRef<HTMLDivElement | null>(null);
  const [offset, setOffset] = useState<{ right: number; bottom: number } | null>(null);

  // Line up with HERE's zoom buttons wherever they are on this screen size, and follow them if the layout changes.
  useLayoutEffect(() => {
    const stack = stackRef.current;
    const area = stack?.parentElement;
    if (!stack || !area) return;
    let frame = 0;
    const place = () => {
      const zoom = area.querySelector<HTMLElement>(".H_zoom");
      if (!zoom) return false;
      const a = area.getBoundingClientRect();
      const z = zoom.getBoundingClientRect();
      if (!z.width) return false;
      setOffset({ right: Math.round(a.right - z.right), bottom: Math.round(a.bottom - z.top + GAP_PX) });
      return true;
    };
    // HERE draws its controls a moment after the map exists, so look for them until they appear.
    let tries = 0;
    const poll = () => {
      if (!place() && tries++ < 60) frame = window.setTimeout(poll, 250);
    };
    poll();
    const observer = new ResizeObserver(() => { place(); });
    observer.observe(area);
    window.addEventListener("resize", place);
    return () => {
      window.clearTimeout(frame);
      observer.disconnect();
      window.removeEventListener("resize", place);
    };
  }, []);

  // A problem message goes away on its own.
  useEffect(() => {
    if (!error) return;
    const timer = window.setTimeout(onDismissError, 12_000);
    return () => window.clearTimeout(timer);
  }, [error, onDismissError]);

  const label = locating ? "Finding your location…" : active ? "Centre the map on my location" : "Show my location";
  return (
    <>
      <div
        ref={stackRef}
        className="locate-stack"
        style={offset ? { right: offset.right, bottom: offset.bottom } : { visibility: "hidden" }}
      >
        {active && !locating && (
          <button type="button" className="map-btn map-btn-small" onClick={onStop} aria-label="Stop sharing my location" title="Stop sharing my location">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.6" strokeLinecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18" /></svg>
          </button>
        )}
        <button
          type="button"
          className={`map-btn${active ? " map-btn-active" : ""}${locating ? " map-btn-busy" : ""}`}
          onClick={onLocate}
          disabled={locating}
          aria-label={label}
          aria-pressed={active}
          title={label}
        >
          {locating ? (
            <span className="map-spinner" aria-hidden="true" />
          ) : (
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
              <circle cx="12" cy="12" r="6.5" />
              <path d="M12 2v3M12 19v3M2 12h3M19 12h3" />
              <circle cx="12" cy="12" r="2.2" fill="currentColor" stroke="none" />
            </svg>
          )}
        </button>
      </div>
      {error && (
        <div className="map-toast" role="alert">
          <span>{error}</span>
          <button type="button" className="map-toast-close" onClick={onDismissError} aria-label="Dismiss">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18" /></svg>
          </button>
        </div>
      )}
    </>
  );
}
