import { useEffect, useRef, useState } from "react";
import { LngLatBounds, Map as HereMap, Marker, Popup } from "./hereMapAdapter";
import { MAX_MAP_MARKERS, dedupePois, placeKey, poiToItem } from "./cards";
import { dayColour, formatKm } from "./format";
import {
  chargerMarkerElement, destinationMarkerElement, dotMarkerElement, hotelMarkerElement, numberedPoiMarkerElement, originMarkerElement, placePopupNode,
  planMarkerElement, poiMarkerElement, stopMarkerElement, stopPopupNode, userMarkerElement, userPopupNode,
} from "./mapMarkers";
import type { Charger, Context, PlaceItem, UserLocation } from "./types";

export type FocusRequest = { item: PlaceItem; nonce: number };

type Props = {
  context: Context | null;
  focusRequest: FocusRequest | null;
  /** The position the user chose to share; drawn as a dot and centred on whenever `locationNonce` changes. */
  userLocation?: UserLocation | null;
  locationNonce?: number;
};

type MapStatus = "loading" | "ready" | "error" | "nokey";

const HAS_KEY = Boolean(import.meta.env.VITE_HERE_API_KEY);
const DEFAULT_CENTER: [number, number] = [78.9629, 21.0];
const DEFAULT_ZOOM = 5;
/** Results beyond this many are drawn as small dots, so the pins the list leads with stay readable. */
const NUMBERED_RESULTS = 10;

function chargerToItem(charger: Charger): PlaceItem {
  return {
    key: placeKey(charger.latitude, charger.longitude),
    name: charger.name || charger.address || "EV charging station",
    category: "ev_charging",
    latitude: charger.latitude,
    longitude: charger.longitude,
    address: charger.address,
    distanceLabel: charger.distance_from_route_km != null ? `${formatKm(charger.distance_from_route_km)} from route` : undefined,
    source: charger.source,
  };
}

/** What the map should be framed on. An empty string means "nothing to show": reset to the default view. */
function viewKey(context: Context | null): string {
  if (!context || (!context.stops.length && !context.pois.length)) return "";
  const stops = context.stops.map((s) => placeKey(s.latitude, s.longitude)).join(">");
  const first = context.pois[0] ? placeKey(context.pois[0].latitude, context.pois[0].longitude) : "";
  return `${stops}|${context.route?.distance_km ?? ""}|${context.pois.length}|${first}|${context.route_revision}`;
}

export default function MapView({ context, focusRequest, userLocation = null, locationNonce = 0 }: Props) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<HereMap | null>(null);
  // Every marker that is on the map (so a redraw removes all of them, even several at one spot)...
  const allMarkersRef = useRef<Marker[]>([]);
  // ...and the first marker per place key, used to open a popup from a result card.
  const markersRef = useRef<globalThis.Map<string, Marker>>(new globalThis.Map());
  const transientRef = useRef<Marker | null>(null);
  const userMarkerRef = useRef<Marker | null>(null);
  const lastViewKey = useRef<string | null>(null);
  const [status, setStatus] = useState<MapStatus>(HAS_KEY ? "loading" : "nokey");
  const [attempt, setAttempt] = useState(0);

  // ---- create the map ------------------------------------------------------------------------
  useEffect(() => {
    const container = containerRef.current;
    if (!HAS_KEY || !container) return;
    const map = new HereMap({ container, center: DEFAULT_CENTER, zoom: DEFAULT_ZOOM });
    mapRef.current = map;
    markersRef.current = new globalThis.Map();
    allMarkersRef.current = [];
    lastViewKey.current = null;
    map.on("load", () => setStatus("ready"));
    map.on("error", () => setStatus("error"));
    const observer = new ResizeObserver(() => map.resize());
    observer.observe(container);
    return () => {
      observer.disconnect();
      map.remove();
      mapRef.current = null;
      markersRef.current = new globalThis.Map();
      allMarkersRef.current = [];
      transientRef.current = null;
      userMarkerRef.current = null;
    };
  }, [attempt]);

  // ---- draw route, stops and places ----------------------------------------------------------
  useEffect(() => {
    const map = mapRef.current;
    if (!map || status !== "ready") return;

    for (const marker of allMarkersRef.current) marker.remove();
    allMarkersRef.current = [];
    markersRef.current.clear();
    transientRef.current?.remove();
    transientRef.current = null;
    map.closePopups();
    for (const id of ["trip-route-outline", "trip-route"]) map.removeLayer(id);
    map.removeSource("trip-route");

    const add = (key: string, marker: Marker) => {
      marker.addTo(map);
      allMarkersRef.current.push(marker);
      // Places can share coordinates (two businesses in one building); the first one answers "show on map".
      if (!markersRef.current.has(key)) markersRef.current.set(key, marker);
    };

    const route = context?.route ?? null;
    if (route) {
      const polylines = route.here_flexible_polylines?.length
        ? route.here_flexible_polylines
        : route.here_flexible_polyline ? [route.here_flexible_polyline] : [];
      if (polylines.length) {
        map.addFlexiblePolyline("trip-route-outline", polylines, { color: "#ffffff", width: 9, opacity: 0.95, zIndex: 10 });
        map.addFlexiblePolyline("trip-route", polylines, { color: "#0b5fff", width: 5, opacity: 0.9, zIndex: 11 });
      } else if ((route.geometry?.coordinates?.length ?? 0) > 1) {
        map.addSource("trip-route", { type: "geojson", data: { type: "Feature", properties: {}, geometry: route.geometry } });
        map.addLayer({ id: "trip-route-outline", type: "line", source: "trip-route", paint: { "line-color": "#ffffff", "line-width": 9, "line-opacity": 0.95 } });
        map.addLayer({ id: "trip-route", type: "line", source: "trip-route", paint: { "line-color": "#0b5fff", "line-width": 5, "line-opacity": 0.9 } });
      }
    }

    const stops = context?.stops ?? [];
    const positions = route?.stop_positions ?? [];
    let waypointNumber = 0;
    stops.forEach((stop, index) => {
      const label = stop.role === "origin" ? "A" : stop.role === "destination" ? "B" : String(++waypointNumber);
      const popup = new Popup({}).setNode(stopPopupNode(stop.name, stop.display_name, stop.role, positions[index] ? formatKm(positions[index].distance_from_start_km) : undefined));
      const marker = new Marker({ element: stopMarkerElement(label, stop.role, stop.name), zIndex: 40 })
        .setLngLat([stop.longitude, stop.latitude])
        .setPopup(popup);
      add(`stop:${index}`, marker);
    });

    // Each place once, by HERE ID: two entries for one place are one marker, two places at one spot stay two.
    // The kind of marker follows what the place is for: a stop or meal of a day plan, a hotel, a suggested destination, the
    // starting point, or an ordinary search result (numbered like its row in the list for the first few, a small dot after).
    const pois = dedupePois(context?.pois ?? []).slice(0, MAX_MAP_MARKERS);
    const poiSpots = new Set<string>();
    let resultNumber = 0;
    pois.forEach((poi) => {
      const item = poiToItem(poi);
      poiSpots.add(placeKey(poi.latitude, poi.longitude));
      let element: HTMLElement;
      let zIndex = 20;
      if (poi.marker === "origin") {
        element = originMarkerElement(item.name);
        zIndex = 45;
      } else if (poi.marker === "destination") {
        element = destinationMarkerElement(poi.order ?? 0, item.name);
        zIndex = 35;
      } else if (poi.marker === "stop" || poi.marker === "meal") {
        element = planMarkerElement(poi.marker, poi.day ?? 1, poi.order ?? 0, item.name, dayColour(poi.day ?? 1));
        zIndex = poi.marker === "stop" ? 30 : 25;
      } else if (poi.marker === "hotel") {
        element = hotelMarkerElement(item.name);
        zIndex = 22;
      } else {
        resultNumber += 1;
        element = resultNumber <= NUMBERED_RESULTS ? numberedPoiMarkerElement(item, resultNumber) : dotMarkerElement(item);
        zIndex = resultNumber <= NUMBERED_RESULTS ? 24 : 12;
      }
      const marker = new Marker({ element, zIndex })
        .setLngLat([item.longitude, item.latitude])
        .setPopup(new Popup({}).setNode(placePopupNode(item)));
      add(item.key, marker);
    });

    // Charging stations are EV Planner content: drawn only while the EV Planner is on. Planned stops are
    // always drawn; the other stations are thinned evenly along the route so the cap never hides one end of it.
    if (context?.ev_mode) {
      const planned = new globalThis.Map<string, number>();
      (context.ev_plan?.charging_stops ?? []).forEach((stop, i) => planned.set(placeKey(stop.charger.latitude, stop.charger.longitude), i + 1));
      const others = (context.chargers ?? []).filter((c) => !planned.has(placeKey(c.latitude, c.longitude)));
      const room = Math.max(0, MAX_MAP_MARKERS - planned.size);
      const step = Math.max(1, Math.ceil(others.length / Math.max(1, room)));
      const shown = [
        ...(context.ev_plan?.charging_stops ?? []).map((stop) => stop.charger),
        ...others.filter((_, i) => i % step === 0),
      ];
      shown.forEach((charger) => {
        const item = chargerToItem(charger);
        if (poiSpots.has(item.key)) return;
        const marker = new Marker({ element: chargerMarkerElement(item.name, planned.get(item.key)), zIndex: planned.has(item.key) ? 30 : 10 })
          .setLngLat([item.longitude, item.latitude])
          .setPopup(new Popup({}).setNode(placePopupNode(item)));
        add(item.key, marker);
      });
    }

    // ---- frame the view only when what is shown has actually changed ----------------------
    const key = viewKey(context);
    if (key === lastViewKey.current) return;
    lastViewKey.current = key;
    if (!key) {
      map.flyTo({ center: DEFAULT_CENTER, zoom: DEFAULT_ZOOM });
      return;
    }
    const bounds = new LngLatBounds();
    let points = 0;
    const extend = (lon: number, lat: number) => {
      bounds.extend([lon, lat]);
      points += 1;
    };
    const planPois = pois.filter((p) => p.marker === "stop" || p.marker === "hotel" || p.marker === "meal");
    if (planPois.length > 0) {
      // A day plan is about the destination: frame its stops and hotels. The road there stays drawn (zoom out to see it),
      // but it must not shrink the view until every stop is a speck.
      planPois.forEach((p) => extend(p.longitude, p.latitude));
    } else {
      route?.geometry?.coordinates?.forEach(([lon, lat]) => extend(lon, lat));
      stops.forEach((s) => extend(s.longitude, s.latitude));
      // A crowd of results must not push the route off the screen: only the numbered results (the ones the list leads with) count.
      let counted = 0;
      pois.forEach((p) => {
        if (!p.marker && ++counted > NUMBERED_RESULTS) return;
        extend(p.longitude, p.latitude);
      });
    }
    // The point that was searched around stays in view, unless the results are packed into a few hundred metres somewhere else:
    // including it would zoom out until the numbered pins pile up in one corner.
    if (context?.focus && !route) {
      const { west, east, south, north } = bounds.toRect();
      const packed = points > 1 && east - west < 0.005 && north - south < 0.005;
      const apart = Math.abs(context.focus.longitude - (west + east) / 2) > 0.01 || Math.abs(context.focus.latitude - (south + north) / 2) > 0.01;
      if (!(packed && apart)) extend(context.focus.longitude, context.focus.latitude);
    }
    if (points <= 1) {
      const only = stops[0] ?? pois[0];
      if (only) map.flyTo({ center: [only.longitude, only.latitude], zoom: 13 });
      return;
    }
    // Results that sit close together (one street of restaurants) may zoom in further, so their numbered pins do not overlap.
    map.fitBounds(bounds, { maxZoom: route || stops.length ? 14 : 16 });
  }, [context, status]);

  // ---- the user's shared position: its own marker, separate from results so a redraw never removes it ----------
  useEffect(() => {
    const map = mapRef.current;
    if (!map || status !== "ready") return;
    userMarkerRef.current?.remove();
    userMarkerRef.current = null;
    if (!userLocation) return;
    const marker = new Marker({ element: userMarkerElement(), zIndex: 60 })
      .setLngLat([userLocation.longitude, userLocation.latitude])
      .setPopup(new Popup({}).setNode(userPopupNode(userLocation.accuracy_m)))
      .addTo(map);
    userMarkerRef.current = marker;
  }, [userLocation, status]);

  // A fresh GPS fix centres the map on the exact position (also when the position has not changed).
  useEffect(() => {
    const map = mapRef.current;
    if (!map || status !== "ready" || !userLocation || locationNonce === 0) return;
    map.flyTo({ center: [userLocation.longitude, userLocation.latitude], zoom: 16 });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [locationNonce, status]);

  // ---- "show on map" requests from result cards ----------------------------------------------
  useEffect(() => {
    const map = mapRef.current;
    if (!map || status !== "ready" || !focusRequest) return;
    const { item } = focusRequest;
    map.flyTo({ center: [item.longitude, item.latitude], zoom: 14 });
    let marker = markersRef.current.get(item.key);
    if (!marker) {
      // The card belongs to an earlier answer, so the map no longer has its marker: show a temporary one.
      transientRef.current?.remove();
      marker = new Marker({ element: poiMarkerElement(item), zIndex: 50 })
        .setLngLat([item.longitude, item.latitude])
        .setPopup(new Popup({}).setNode(placePopupNode(item)))
        .addTo(map);
      transientRef.current = marker;
    }
    const target = marker;
    const timer = window.setTimeout(() => map.openPopup(target), 250);
    return () => window.clearTimeout(timer);
  }, [focusRequest, status]);

  return (
    <div className="map-wrap">
      <div ref={containerRef} className="map-canvas" role="region" aria-label="Interactive map" />
      {status === "loading" && <div className="map-state" role="status">Loading map…</div>}
      {status === "nokey" && (
        <div className="map-state map-state-error" role="alert">
          <strong>The map can't load.</strong>
          <span>No HERE API key is configured for the web app. Set <code>VITE_HERE_API_KEY</code> in <code>frontend/.env</code> and restart the dev server. Search and chat still work.</span>
        </div>
      )}
      {status === "error" && (
        <div className="map-state map-state-error" role="alert">
          <strong>The map couldn't load.</strong>
          <span>Check your connection and HERE API key, then try again. Chat results still appear in the panel.</span>
          <button type="button" className="btn btn-primary" onClick={() => { setStatus("loading"); setAttempt((n) => n + 1); }}>Retry</button>
        </div>
      )}
    </div>
  );
}
