export function formatDuration(minutes: number | null | undefined): string {
  if (minutes == null || !Number.isFinite(Number(minutes))) return "—";
  const total = Math.round(Number(minutes));
  const hours = Math.floor(total / 60);
  const mins = total % 60;
  if (hours && mins) return `${hours} h ${mins} min`;
  if (hours) return `${hours} h`;
  return `${mins} min`;
}

export function formatKm(km: number | null | undefined): string {
  if (km == null || !Number.isFinite(Number(km))) return "—";
  const value = Number(km);
  if (value < 1) return `${Math.round(value * 1000)} m`;
  if (value < 100) return `${value.toFixed(1).replace(/\.0$/, "")} km`;
  return `${Math.round(value).toLocaleString("en-IN")} km`;
}

/** True when any of the places carries Tripadvisor photos or ratings (which come with an attribution requirement). */
export function hasTripadvisorData(items: Array<{ photo?: string; photoLarge?: string; ratingCount?: number; ratingUrl?: string }>): boolean {
  return items.some((item) => item.photo || item.photoLarge || item.ratingCount !== undefined || item.ratingUrl);
}

export function formatCount(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(Number(value))) return "—";
  return Math.round(Number(value)).toLocaleString("en-IN");
}

export function modeLabel(mode: string | null | undefined): string {
  if (mode === "pedestrian") return "Walking";
  if (mode === "bicycle") return "Cycling";
  return "Driving";
}

export function categoryLabel(category: string): string {
  return category.replaceAll("_", " ").replace(/^\w/, (c) => c.toUpperCase());
}

const CATEGORY_ICONS: Record<string, string> = {
  restaurant: "restaurant",
  cafe: "cafe",
  hotel: "hotel",
  hospital: "hospital",
  pharmacy: "pharmacy",
  fuel: "fuel",
  supermarket: "supermarket",
  convenience: "convenience",
  tourist_attraction: "tourist",
  parking: "parking",
};

export function categoryIconPath(category: string): string {
  const key = category.replaceAll(" ", "_").toLowerCase();
  return `/category-icons/${CATEGORY_ICONS[key] || "place"}.svg`;
}

/** Only http(s) links are ever rendered as links. Anything else from a provider is dropped. */
export function safeHttpUrl(value: string | null | undefined): string | undefined {
  if (!value) return undefined;
  try {
    const url = new URL(value.includes("://") ? value : `https://${value}`);
    return url.protocol === "https:" || url.protocol === "http:" ? url.toString() : undefined;
  } catch {
    return undefined;
  }
}

/**
 * Search links for checking prices and availability elsewhere. HerePilot-AI cannot book a room or see a price: these only open a
 * search on another site for the hotel's name and town. Nothing about the traveller is put in the link.
 */
export function hotelSearchLinks(name: string, city?: string): Array<{ label: string; href: string }> {
  const query = encodeURIComponent([name, city].filter(Boolean).join(" "));
  return [
    { label: "Booking.com", href: `https://www.booking.com/searchresults.html?ss=${query}` },
    { label: "Google Hotels", href: `https://www.google.com/travel/search?q=${query}` },
  ];
}

const DAY_COLOURS = ["#0b5fff", "#12805c", "#c2410c", "#7c3aed", "#0e7490", "#be185d", "#4d7c0f"];
/** One colour per day of a plan, used on the day's card and on its markers so the two can be matched at a glance. */
export const dayColour = (day: number): string => DAY_COLOURS[(Math.max(1, day) - 1) % DAY_COLOURS.length];

/** HERE WeGo opens the place in HERE's own consumer map. */
export function wegoLink(latitude: number, longitude: number): string {
  return `https://wego.here.com/?map=${latitude.toFixed(5)},${longitude.toFixed(5)},16,normal`;
}

const MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
export const monthName = (month: number): string => MONTH_NAMES[Math.max(1, Math.min(12, month)) - 1];

/** "Sat 8 Nov" from an ISO date such as 2026-11-08, read as a calendar date (no time-zone shift). */
export function shortDate(iso: string): string {
  const [y, m, d] = iso.split("-").map(Number);
  if (!y || !m || !d) return iso;
  const date = new Date(Date.UTC(y, m - 1, d));
  return `${date.toLocaleDateString("en-GB", { weekday: "short", timeZone: "UTC" })} ${d} ${MONTH_NAMES[m - 1].slice(0, 3)}`;
}

export const WHEN_LABEL: Record<string, string> = { morning: "Morning", afternoon: "Afternoon", evening: "Evening" };

export function newId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) return crypto.randomUUID();
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}
