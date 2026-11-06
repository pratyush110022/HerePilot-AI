const HERE_CORE = "https://js.api.here.com/v3/3.2/mapsjs-core.js";
const HERE_SERVICE = "https://js.api.here.com/v3/3.2/mapsjs-service.js";
const HERE_EVENTS = "https://js.api.here.com/v3/3.2/mapsjs-mapevents.js";
const HERE_UI = "https://js.api.here.com/v3/3.2/mapsjs-ui.js";
const HERE_UI_CSS = "https://js.api.here.com/v3/3.2/mapsjs-ui.css";

/** Map label language. HERE accepts ISO 639-1/639-2 codes; a name with no English form keeps its local spelling. */
const MAP_LANGUAGE = "en";

declare global {
  interface Window {
    H: any;
  }
}

let hereLoadPromise: Promise<void> | null = null;

function loadScript(src: string) {
  return new Promise<void>((resolve, reject) => {
    const existing = document.querySelector(`script[src="${src}"]`) as HTMLScriptElement | null;
    if (existing) {
      if (window.H) resolve();
      else existing.addEventListener("load", () => resolve(), { once: true });
      existing.addEventListener("error", () => reject(new Error(`Failed to load HERE Maps SDK: ${src}`)), { once: true });
      return;
    }
    const script = document.createElement("script");
    script.src = src;
    script.async = false;
    script.onload = () => resolve();
    script.onerror = () => reject(new Error(`Failed to load HERE Maps SDK: ${src}`));
    document.head.appendChild(script);
  });
}

function loadCss() {
  if (document.querySelector(`link[href="${HERE_UI_CSS}"]`)) return;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = HERE_UI_CSS;
  document.head.appendChild(link);
}

export function loadHereMaps(): Promise<void> {
  if (hereLoadPromise) return hereLoadPromise;
  hereLoadPromise = (async () => {
    await loadScript(HERE_CORE);
    await loadScript(HERE_SERVICE);
    await loadScript(HERE_EVENTS);
    await loadScript(HERE_UI);
    loadCss();
  })();
  return hereLoadPromise;
}

export class NavigationControl {
  // Marker class retained so the existing UI code can stay simple.
}

export class Popup {
  private popup: any = null;
  private html = "";
  private node: Node | null = null;

  constructor(_options: { offset?: number; maxWidth?: string } = {}) {}

  setHTML(html: string) {
    this.html = html;
    this.node = null;
    return this;
  }

  /** Preferred: content built with DOM APIs, so no data is ever parsed as HTML. */
  setNode(node: Node) {
    this.node = node;
    this.html = "";
    return this;
  }

  attach(marker: any) {
    if (!window.H?.ui || !marker) return;
    if (!this.popup) {
      this.popup = new window.H.ui.InfoBubble(marker.getGeometry(), {
        content: this.node ?? this.html,
        minWidth: 220,
        maxWidth: 340,
      });
    } else {
      this.popup.setPosition(marker.getGeometry());
      this.popup.setContent(this.node ?? this.html);
    }
    return this.popup;
  }
}

export class Marker {
  private marker: any = null;
  private element: HTMLElement;
  private lng = 0;
  private lat = 0;
  private popup: Popup | null = null;
  private map: Map | null = null;
  private onTap: (() => void) | undefined;
  private zIndex: number;

  constructor(options: { element: HTMLElement; onTap?: () => void; zIndex?: number }) {
    this.element = options.element;
    this.onTap = options.onTap;
    this.zIndex = options.zIndex ?? 0;
  }

  getZIndex() {
    return this.zIndex;
  }

  setLngLat([longitude, latitude]: [number, number]) {
    this.lng = longitude;
    this.lat = latitude;
    if (this.marker && window.H) {
      this.marker.setGeometry({ lat: latitude, lng: longitude });
    }
    return this;
  }

  setPopup(popup: Popup) {
    this.popup = popup;
    return this;
  }

  addTo(map: Map) {
    this.map = map;
    map.addMarker(this);
    return this;
  }

  remove() {
    if (this.map) this.map.removeMarker(this);
    this.map = null;
    this.marker = null;
  }

  getPosition() {
    return { lat: this.lat, lng: this.lng };
  }

  getElement() {
    return this.element;
  }

  getPopup() {
    return this.popup;
  }

  getOnTap() {
    return this.onTap;
  }

  setNativeMarker(marker: any) {
    this.marker = marker;
  }

  getNativeMarker() {
    return this.marker;
  }
}

export class LngLatBounds {
  private minLon = Infinity;
  private minLat = Infinity;
  private maxLon = -Infinity;
  private maxLat = -Infinity;

  extend(coordinate: [number, number]) {
    const [lon, lat] = coordinate;
    this.minLon = Math.min(this.minLon, lon);
    this.maxLon = Math.max(this.maxLon, lon);
    this.minLat = Math.min(this.minLat, lat);
    this.maxLat = Math.max(this.maxLat, lat);
    return this;
  }

  toRect() {
    return {
      west: this.minLon,
      south: this.minLat,
      east: this.maxLon,
      north: this.maxLat,
    };
  }
}

type MapOptions = {
  container: HTMLElement;
  center: [number, number];
  zoom: number;
};

type Layer = {
  id: string;
  type: string;
  source: string;
  paint?: Record<string, unknown>;
};

export class Map {
  private nativeMap: any = null;
  private container: HTMLElement;
  private center: [number, number];
  private zoom: number;
  private ready = false;
  private failed = false;
  private listeners = new globalThis.Map<string, Array<() => void>>();
  private onceListeners = new globalThis.Map<string, Array<() => void>>();
  private sources = new globalThis.Map<string, {
    type: string;
    data: {
      type?: string;
      properties?: Record<string, unknown>;
      geometry?: { type?: string; coordinates?: [number, number][] };
    };
  }>();
  private layers = new globalThis.Map<string, any>();
  private markers = new globalThis.Set<Marker>();
  private markerIds = new globalThis.Map<Marker, string>();
  private markerClickHandlers = new globalThis.Map<string, () => void>();
  private markerCounter = 0;
  private removed = false;
  private handleDelegatedMarkerClick = (event: MouseEvent) => {
    const target = event.target;
    if (!(target instanceof Element)) return;
    const markerElement = target.closest("[data-marker-id]");
    if (!markerElement || !this.container.contains(markerElement)) return;
    const markerId = markerElement.getAttribute("data-marker-id");
    const handler = markerId ? this.markerClickHandlers.get(markerId) : undefined;
    if (!handler) return;
    event.preventDefault();
    event.stopPropagation();
    handler();
  };

  constructor(options: MapOptions) {
    this.container = options.container;
    this.center = options.center;
    this.zoom = options.zoom;
    // Capture-phase delegation is independent of HERE's cloned DOM marker
    // listener lifecycle, and therefore remains a reliable click path.
    this.container.addEventListener("click", this.handleDelegatedMarkerClick, true);
    void this.initialize();
  }

  private async initialize() {
    try {
      await loadHereMaps();
      if (this.removed) return;
      if (!window.H) throw new Error("HERE Maps SDK is not available");
      this.createNativeMap();
    } catch (error) {
      if (this.removed) return;
      // Surface a failed SDK load or map construction to the UI instead of leaving a blank map.
      hereLoadPromise = null;
      console.warn("Map failed to initialise", error instanceof Error ? error.message : "unknown error");
      this.failed = true;
      this.emit("error");
    }
  }

  private createNativeMap() {
    const platform = new window.H.service.Platform({ apikey: import.meta.env.VITE_HERE_API_KEY });
    // Labels (cities, districts, states) in English everywhere, in every base layer, whatever the local script is.
    const defaultLayers = platform.createDefaultLayers({ lg: MAP_LANGUAGE, lg2: MAP_LANGUAGE });
    const layer = defaultLayers.vector.normal.map;
    this.nativeMap = new window.H.Map(
      this.container,
      layer,
      {
        pixelRatio: window.devicePixelRatio || 1,
        center: { lat: this.center[1], lng: this.center[0] },
        zoom: this.zoom,
      },
    );

    // HERE fits bounds inside the viewport padding (setLookAtData has no padding option of its own).
    // A symmetric padding keeps the map centre where it is, so flyTo is unaffected.
    const pad = Math.max(24, Math.min(70, Math.round(Math.min(this.container.clientWidth, this.container.clientHeight) * 0.1)));
    this.nativeMap.getViewPort().setPadding(pad, pad, pad, pad);

    new window.H.mapevents.Behavior(new window.H.mapevents.MapEvents(this.nativeMap));
    if (window.H.ui) {
      const ui = window.H.ui.UI.createDefault(this.nativeMap, defaultLayers);
      this.nativeMap.__hereUi = ui;
    }

    this.ready = true;
    this.emit("load");
  }

  hasFailed() {
    return this.failed;
  }

  /** Call when the container changes size (window resize, layout change). */
  resize() {
    if (this.nativeMap) this.nativeMap.getViewPort().resize();
  }

  closePopups() {
    const ui = this.nativeMap?.__hereUi;
    if (ui) ui.getBubbles().forEach((bubble: any) => ui.removeBubble(bubble));
  }

  private emit(event: string) {
    for (const callback of this.listeners.get(event) || []) callback();
    const once = this.onceListeners.get(event) || [];
    this.onceListeners.delete(event);
    for (const callback of once) callback();
  }

  on(event: string, callback: () => void) {
    const list = this.listeners.get(event) || [];
    list.push(callback);
    this.listeners.set(event, list);
    if (event === "load" && this.ready) callback();
    if (event === "error" && this.failed) callback();
    return this;
  }

  once(event: string, callback: () => void) {
    if (event === "load" && this.ready) {
      callback();
      return this;
    }
    const list = this.onceListeners.get(event) || [];
    list.push(callback);
    this.onceListeners.set(event, list);
    return this;
  }

  loaded() {
    return this.ready;
  }

  addControl(_control: NavigationControl, _position?: string) {
    // HERE UI already creates zoom controls during initialization.
    return this;
  }

  addMarker(marker: Marker) {
    if (!this.nativeMap || !window.H) {
      this.once("load", () => this.addMarker(marker));
      return;
    }
    const popup = marker.getPopup();
    let lastOpenAt = 0;
    const openMarkerPopup = (event?: any) => {
      // HERE map events and browser DOM events expose different event shapes.
      // Stop propagation only when the relevant method exists.
      if (typeof event?.stopPropagation === "function") event.stopPropagation();
      // The same physical action can surface as pointerup, HERE tap, and DOM
      // click. Collapse those into one visible selection/popup operation.
      const now = Date.now();
      if (now - lastOpenAt < 220) return;
      lastOpenAt = now;
      marker.getOnTap()?.();
      if (popup) this.openPopup(marker, 0);
    };
    // Add a stable identity to the HTML copied by HERE's DomIcon. A capture-
    // phase listener on the map container uses it to dispatch clicks even if
    // HERE does not forward a click/tap event for this particular marker.
    const markerId = `marker-${++this.markerCounter}`;
    marker.getElement().setAttribute("data-marker-id", markerId);
    this.markerIds.set(marker, markerId);
    this.markerClickHandlers.set(markerId, () => openMarkerPopup());
    const icon = new window.H.map.DomIcon(marker.getElement().outerHTML, {
      onAttach: (clonedElement: HTMLElement) => {
        clonedElement.style.cursor = "pointer";
        clonedElement.style.pointerEvents = "auto";
        clonedElement.setAttribute("role", "button");
        clonedElement.setAttribute("tabindex", "0");
        clonedElement.addEventListener("keydown", (event: KeyboardEvent) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            openMarkerPopup(event);
          }
        });
        // Child logo/badge clicks should bubble to the delegated marker target.
        clonedElement.querySelectorAll<HTMLElement>("img, span, svg").forEach((child) => {
          child.style.pointerEvents = "none";
        });
      },
    });
    const native = new window.H.map.DomMarker(
      { lat: marker.getPosition().lat, lng: marker.getPosition().lng },
      { icon, zIndex: marker.getZIndex() },
    );
    native.addEventListener("tap", openMarkerPopup);
    native.addEventListener("pointerup", openMarkerPopup);
    marker.setNativeMarker(native);
    this.nativeMap.addObject(native);
    this.markers.add(marker);
  }

  removeMarker(marker: Marker) {
    const native = marker.getNativeMarker();
    if (native && this.nativeMap) this.nativeMap.removeObject(native);
    const markerId = this.markerIds.get(marker);
    if (markerId) this.markerClickHandlers.delete(markerId);
    this.markerIds.delete(marker);
    this.markers.delete(marker);
  }

  openPopup(marker: Marker, attempt = 0) {
    if (!this.nativeMap || !window.H) return this;
    const native = marker.getNativeMarker();
    const popup = marker.getPopup();
    const ui = this.nativeMap.__hereUi;
    if (!native || !popup || !ui) {
      if (attempt < 12) {
        window.setTimeout(() => this.openPopup(marker, attempt + 1), 100);
      }
      return this;
    }
    try {
      ui.getBubbles().forEach((bubble: any) => ui.removeBubble(bubble));
      const bubble = popup.attach(native);
      if (bubble) ui.addBubble(bubble);
    } catch (error) {
      console.warn("Map marker popup failed", error);
      if (attempt < 12) {
        window.setTimeout(() => this.openPopup(marker, attempt + 1), 100);
      }
    }
    return this;
  }

  getLayer(id: string) {
    return this.layers.has(id) ? this.layers.get(id) : undefined;
  }

  getSource(id: string) {
    return this.sources.has(id) ? this.sources.get(id) : undefined;
  }

  addSource(id: string, source: {
    type: string;
    data: {
      type?: string;
      properties?: Record<string, unknown>;
      geometry?: { type?: string; coordinates?: [number, number][] };
    };
  }) {
    this.sources.set(id, source);
    return this;
  }

  removeLayer(id: string) {
    const object = this.layers.get(id);
    if (object && this.nativeMap) {
      if (Array.isArray(object)) object.forEach((entry) => this.nativeMap.removeObject(entry));
      else this.nativeMap.removeObject(object);
    }
    this.layers.delete(id);
  }

  removeSource(id: string) {
    this.sources.delete(id);
  }

  addLayer(layer: Layer) {
    const source = this.sources.get(layer.source);
    const coordinates = source?.data?.geometry?.coordinates || [];
    if (!window.H || !this.nativeMap || coordinates.length < 2) return this;

    const lineString = new window.H.geo.LineString();
    coordinates.forEach(([lon, lat]) => lineString.pushLatLngAlt(lat, lon, 0));
    const color = String(layer.paint?.["line-color"] || "#2563eb");
    const width = Number(layer.paint?.["line-width"] || 5);
    const opacity = Number(layer.paint?.["line-opacity"] || 1);
    const polyline = new window.H.map.Polyline(lineString, {
      style: { strokeColor: color, lineWidth: width, lineOpacity: opacity },
      zIndex: layer.id === "trip-route-outline" ? 10 : 11,
    });
    this.nativeMap.addObject(polyline);
    this.layers.set(layer.id, polyline);
    return this;
  }

  addFlexiblePolyline(
    id: string,
    encodedPolylines: string[],
    options: { color?: string; width?: number; opacity?: number; zIndex?: number } = {},
  ) {
    if (!window.H || !this.nativeMap || !encodedPolylines.length) return this;

    const objects: any[] = [];
    for (const encoded of encodedPolylines) {
      if (!encoded) continue;
      const factory = window.H.geo?.LineString?.fromFlexiblePolyline;
      if (typeof factory !== "function") return this;
      const lineString = factory(encoded);
      const polyline = new window.H.map.Polyline(lineString, {
        style: {
          strokeColor: options.color || "#2563eb",
          lineWidth: options.width || 5,
          lineOpacity: options.opacity ?? 0.86,
        },
        zIndex: options.zIndex || 11,
      });
      this.nativeMap.addObject(polyline);
      objects.push(polyline);
    }
    this.layers.set(id, objects);
    return this;
  }

  fitBounds(bounds: LngLatBounds, options: { maxZoom?: number } = {}) {
    if (!this.nativeMap || !window.H) {
      this.once("load", () => this.fitBounds(bounds, options));
      return this;
    }
    const b = bounds.toRect();
    if (!Number.isFinite(b.west) || !Number.isFinite(b.east)) return;
    const rect = new window.H.geo.Rect(b.north, b.west, b.south, b.east);
    // Not animated: the zoom cap below has to act on the final zoom, not on the one the animation starts from.
    this.nativeMap.getViewModel().setLookAtData({ bounds: rect }, false);
    if (options.maxZoom != null && this.nativeMap.getZoom() > options.maxZoom) this.nativeMap.setZoom(options.maxZoom);
  }

  flyTo(options: { center: [number, number]; zoom?: number; duration?: number }) {
    if (!this.nativeMap) {
      this.once("load", () => this.flyTo(options));
      return;
    }
    this.nativeMap.getViewModel().setLookAtData({
      position: { lat: options.center[1], lng: options.center[0] },
      zoom: options.zoom ?? 15,
    }, true);
  }

  remove() {
    this.removed = true;
    this.markers.forEach((marker) => this.removeMarker(marker));
    this.container.removeEventListener("click", this.handleDelegatedMarkerClick, true);
    if (this.nativeMap) this.nativeMap.dispose();
    this.nativeMap = null;
    this.ready = false;
  }
}
