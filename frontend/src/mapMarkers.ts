import { categoryIconPath, categoryLabel, formatCount, safeHttpUrl } from "./format";
import type { PlaceItem } from "./types";

/**
 * Marker and popup elements are built with DOM APIs and `textContent`, never by concatenating
 * provider data into HTML. The map adapter serialises the marker element for HERE's DomIcon, which
 * escapes attribute values and text.
 */
function el<K extends keyof HTMLElementTagNameMap>(tag: K, className?: string, text?: string): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

export function stopMarkerElement(label: string, role: string, name: string): HTMLElement {
  const root = el("div", `hp-marker hp-marker-stop hp-marker-${role}`);
  root.setAttribute("aria-label", `${role === "origin" ? "Start" : role === "destination" ? "Destination" : "Stop"}: ${name}`);
  root.appendChild(el("span", "hp-marker-label", label));
  return root;
}

export function poiMarkerElement(item: PlaceItem): HTMLElement {
  const root = el("div", "hp-marker hp-marker-poi");
  root.setAttribute("aria-label", `${categoryLabel(item.category)}: ${item.name}`);
  const img = el("img");
  img.src = categoryIconPath(item.category);
  img.alt = "";
  root.appendChild(img);
  return root;
}

/** A result pin carrying the same number as its row in the list, so the card and the map can be read together. */
export function numberedPoiMarkerElement(item: PlaceItem, number: number): HTMLElement {
  const root = poiMarkerElement(item);
  root.classList.add("hp-marker-numbered");
  root.setAttribute("aria-label", `${number}. ${categoryLabel(item.category)}: ${item.name}`);
  root.appendChild(el("span", "hp-marker-num", String(number)));
  return root;
}

/** A small dot for results beyond the first few: it marks where they are without hiding the ones that matter. */
export function dotMarkerElement(item: PlaceItem): HTMLElement {
  const root = el("div", "hp-marker hp-marker-dot");
  root.setAttribute("aria-label", `${categoryLabel(item.category)}: ${item.name}`);
  return root;
}

/** A stop (or meal) of a day plan: the day's colour, the stop's order within that day. Meals are drawn as a ring. */
export function planMarkerElement(kind: "stop" | "meal", day: number, order: number, name: string, colour: string): HTMLElement {
  const root = el("div", `hp-marker hp-marker-plan hp-marker-plan-${kind}`);
  root.style.setProperty("--day", colour);
  root.setAttribute("aria-label", `Day ${day}, ${kind === "meal" ? "meal" : "stop"} ${order}: ${name}`);
  root.appendChild(el("span", "hp-marker-label", String(order)));
  return root;
}

export function hotelMarkerElement(name: string): HTMLElement {
  const root = el("div", "hp-marker hp-marker-hotel");
  root.setAttribute("aria-label", `Hotel: ${name}`);
  const img = el("img");
  img.src = "/category-icons/hotel.svg";
  img.alt = "";
  root.appendChild(img);
  return root;
}

/** A suggested destination, numbered like its card. */
export function destinationMarkerElement(order: number, name: string): HTMLElement {
  const root = el("div", "hp-marker hp-marker-destination-pin");
  root.setAttribute("aria-label", `Suggestion ${order}: ${name}`);
  root.appendChild(el("span", "hp-marker-label", String(order)));
  return root;
}

/** The starting point that the drive times of a destination list are measured from. */
export function originMarkerElement(name: string): HTMLElement {
  const root = el("div", "hp-marker hp-marker-origin-pin");
  root.setAttribute("aria-label", `Starting point: ${name}`);
  root.appendChild(el("span", "hp-marker-label", "A"));
  return root;
}

/** "You are here": a blue dot with a white ring. The accuracy is in the popup, so the dot never claims more than it knows. */
export function userMarkerElement(): HTMLElement {
  const root = el("div", "hp-marker hp-marker-me");
  root.setAttribute("aria-label", "Your location");
  root.appendChild(el("span", "hp-marker-me-pulse"));
  root.appendChild(el("span", "hp-marker-me-dot"));
  return root;
}

export function userPopupNode(accuracyM?: number): HTMLElement {
  const root = el("div", "hp-popup");
  root.appendChild(el("strong", "hp-popup-title", "Your location"));
  root.appendChild(el("div", "hp-popup-meta", accuracyM ? `From your device's GPS, accurate to about ${accuracyM} m` : "From your device's GPS"));
  return root;
}

export function chargerMarkerElement(name: string, order?: number): HTMLElement {
  const root = el("div", `hp-marker hp-marker-charger${order ? " planned" : ""}`);
  root.setAttribute("aria-label", order ? `Planned charging stop ${order}: ${name}` : `EV charging station: ${name}`);
  const img = el("img");
  img.src = "/ev-marker.svg";
  img.alt = "";
  root.appendChild(img);
  if (order) root.appendChild(el("span", "hp-marker-badge", String(order)));
  return root;
}

function row(label: string, value: string): HTMLElement {
  const wrapper = el("div", "hp-popup-row");
  wrapper.appendChild(el("span", "hp-popup-label", label));
  wrapper.appendChild(el("span", "hp-popup-value", value));
  return wrapper;
}

export function placePopupNode(item: PlaceItem): HTMLElement {
  const root = el("div", "hp-popup");
  if (item.photo) {
    const photo = el("img", "hp-popup-photo");
    photo.src = item.photo;
    photo.alt = "";
    photo.loading = "lazy";
    photo.referrerPolicy = "no-referrer";
    photo.addEventListener("error", () => photo.remove());
    root.appendChild(photo);
  }
  root.appendChild(el("strong", "hp-popup-title", item.name));
  const rating = item.rating != null ? `${Number(item.rating).toFixed(1)}/5${item.ratingCount !== undefined ? ` · ${formatCount(item.ratingCount)} reviews` : ""} on Tripadvisor` : "";
  const meta = [item.hereCategory || categoryLabel(item.category), rating].filter(Boolean).join(" · ");
  if (meta) root.appendChild(el("div", "hp-popup-meta", meta));
  if (item.address) root.appendChild(row("Address", item.address));
  if (item.distanceLabel) root.appendChild(row("Distance", item.distanceLabel));
  if (item.openingHours) root.appendChild(row("Hours", item.openingHours));
  if (item.phone) root.appendChild(row("Phone", item.phone));
  const href = safeHttpUrl(item.website);
  if (href) {
    const link = el("a", "hp-popup-link", "Website");
    link.href = href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    root.appendChild(link);
  }
  if (item.source) root.appendChild(el("div", "hp-popup-source", `Source: ${item.source}`));
  return root;
}

export function stopPopupNode(name: string, displayName: string, role: string, distanceLabel?: string): HTMLElement {
  const root = el("div", "hp-popup");
  root.appendChild(el("strong", "hp-popup-title", name));
  root.appendChild(el("div", "hp-popup-meta", role === "origin" ? "Start" : role === "destination" ? "Destination" : "Stop on your route"));
  if (displayName && displayName !== name) root.appendChild(row("Place", displayName));
  if (distanceLabel) root.appendChild(row("From start", distanceLabel));
  return root;
}
