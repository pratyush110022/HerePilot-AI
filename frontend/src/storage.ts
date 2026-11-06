import { safeHttpUrl } from "./format";
import type { AnswerBlock, ChatMessage, GuideCard, MessageError, PlaceItem, ResultCard } from "./types";

const STATE_KEY = "herepilot-state-v1";
const LEGACY_SESSION_KEY = "geopilot-session-id";
const MAX_MESSAGES = 40;

export type PersistedState = { sessionId: string | null; messages: ChatMessage[] };

type Obj = Record<string, unknown>;
const isObj = (value: unknown): value is Obj => typeof value === "object" && value !== null && !Array.isArray(value);
const list = (value: unknown): unknown[] => (Array.isArray(value) ? value : []);
const text = (value: unknown, fallback = ""): string => (typeof value === "string" ? value : fallback);
const optText = (value: unknown): string | undefined => (typeof value === "string" && value ? value : undefined);
const num = (value: unknown, fallback = 0): number => (typeof value === "number" && Number.isFinite(value) ? value : fallback);
const optNum = (value: unknown): number | undefined => (typeof value === "number" && Number.isFinite(value) ? value : undefined);
const strings = (value: unknown): string[] => list(value).filter((s): s is string => typeof s === "string");
const httpsUrl = (value: unknown): string | undefined => {
  const url = typeof value === "string" ? safeHttpUrl(value) : undefined;
  return url?.startsWith("https://") ? url : undefined;
};
const monthList = (value: unknown): number[] => list(value).filter((m): m is number => Number.isInteger(m) && (m as number) >= 1 && (m as number) <= 12);

function sanitizeItem(value: unknown): PlaceItem | null {
  if (!isObj(value) || typeof value.name !== "string") return null;
  const latitude = optNum(value.latitude);
  const longitude = optNum(value.longitude);
  if (latitude === undefined || longitude === undefined) return null;
  return {
    key: text(value.key, `${latitude.toFixed(5)},${longitude.toFixed(5)}`),
    name: value.name,
    category: text(value.category, "place"),
    latitude,
    longitude,
    address: optText(value.address),
    distanceKm: optNum(value.distanceKm),
    distanceLabel: optText(value.distanceLabel),
    hereCategory: optText(value.hereCategory),
    city: optText(value.city),
    rating: optNum(value.rating),
    phone: optText(value.phone),
    website: safeHttpUrl(optText(value.website)),
    openingHours: optText(value.openingHours),
    source: optText(value.source),
    ratingCount: optNum(value.ratingCount),
    ratingUrl: safeHttpUrl(optText(value.ratingUrl)),
    photo: httpsUrl(value.photo),
    photoLarge: httpsUrl(value.photoLarge),
    openNow: typeof value.openNow === "boolean" ? value.openNow : undefined,
    openNowAt: typeof value.openNowAt === "number" ? optNum(value.openNowAt) : undefined,
  };
}

const items = (value: unknown): PlaceItem[] => list(value).map(sanitizeItem).filter((item): item is PlaceItem => item !== null);

/**
 * Rebuilds a saved card in the current shape. Cards saved by an older build can lack newer fields (for example
 * `notes`); rendering one directly used to crash React and leave a blank page. Unusable cards are dropped.
 */
function sanitizeCard(value: unknown): ResultCard | null {
  if (!isObj(value)) return null;
  switch (value.kind) {
    case "places": {
      const placeItems = items(value.items);
      const status = ["ok", "fallback", "unavailable", "empty"].includes(String(value.status)) ? (value.status as "ok" | "fallback" | "unavailable" | "empty") : "ok";
      return {
        kind: "places", title: text(value.title, "Places"), items: placeItems, total: num(value.total, placeItems.length), source: optText(value.source),
        status, scopeLabel: optText(value.scopeLabel), notes: strings(value.notes), suggestion: optText(value.suggestion),
      };
    }
    case "route": {
      const stops = list(value.stops).filter(isObj).map((s) => ({ name: text(s.name, "Stop"), role: text(s.role, "stop") }));
      const legs = list(value.legs).filter(isObj).map((l) => ({
        from: text(l.from), to: text(l.to), distanceKm: num(l.distanceKm), durationMinutes: num(l.durationMinutes),
      }));
      return {
        kind: "route", stops, distanceKm: num(value.distanceKm), durationMinutes: num(value.durationMinutes), mode: text(value.mode, "drive"),
        legs, change: optText(value.change), fuelNote: optText(value.fuelNote),
      };
    }
    case "distance":
      return {
        kind: "distance", to: text(value.to, "your destination"), from: optText(value.from), stopNumber: optNum(value.stopNumber),
        distanceKm: num(value.distanceKm), durationMinutes: num(value.durationMinutes),
      };
    case "geocode":
      return { kind: "geocode", query: text(value.query), items: items(value.items), others: strings(value.others) };
    case "ev": {
      const status = text(value.status, "ready") as Extract<ResultCard, { kind: "ev" }>["status"];
      const stops = list(value.stops).filter(isObj).map((s) => ({ name: text(s.name, "Charger"), arrival: optNum(s.arrival), departure: optNum(s.departure) }));
      return { kind: "ev", status, tripPossible: typeof value.tripPossible === "boolean" ? value.tripPossible : undefined, stops, failureReason: optText(value.failureReason) };
    }
    case "answer": {
      const status = ["ok", "empty", "unavailable"].includes(String(value.status)) ? (value.status as "ok" | "empty" | "unavailable") : "ok";
      const blocks: AnswerBlock[] = [];
      for (const raw of list(value.blocks).filter(isObj)) {
        const title = text(raw.title);
        switch (raw.type) {
          case "text": blocks.push({ type: "text", title, text: text(raw.text) }); break;
          case "list": case "steps": blocks.push({ type: raw.type, title, items: strings(raw.items) }); break;
          case "pros_cons": blocks.push({ type: "pros_cons", title, pros: strings(raw.pros), cons: strings(raw.cons) }); break;
          case "compare":
            blocks.push({ type: "compare", title, columns: strings(raw.columns), rows: list(raw.rows).filter(isObj).map((r) => ({ label: text(r.label), values: strings(r.values) })) });
            break;
          case "itinerary": blocks.push({ type: "itinerary", title, days: list(raw.days).filter(isObj).map((d) => ({ title: text(d.title), items: strings(d.items) })) }); break;
          case "facts": blocks.push({ type: "facts", title, items: list(raw.items).filter(isObj).map((f) => ({ label: text(f.label), value: text(f.value) })) }); break;
          case "places": blocks.push({ type: "places", title, items: items(raw.items) }); break;
          default: break;
        }
      }
      return {
        kind: "answer", ai: value.ai === true, subjects: items(value.subjects), blocks, caveats: strings(value.caveats),
        suggestions: strings(value.suggestions), more: items(value.more), status, note: optText(value.note),
      };
    }
    case "guide": {
      const status = ["ok", "empty", "unavailable"].includes(String(value.status)) ? (value.status as GuideCard["status"]) : "ok";
      const sections = list(value.sections).filter(isObj).map((section) => ({
        title: text(section.title, "Section"), months: monthList(section.months), monthsLabel: text(section.monthsLabel),
        badge: optText(section.badge), summary: text(section.summary), bullets: strings(section.bullets), items: items(section.items),
      }));
      return {
        kind: "guide", place: text(value.place, "this place"), region: optText(value.region), ai: value.ai === true,
        headline: text(value.headline), summary: text(value.summary), bestMonths: monthList(value.bestMonths), goodMonths: monthList(value.goodMonths),
        avoidMonths: monthList(value.avoidMonths), bestLabel: text(value.bestLabel), sections, more: items(value.more), tips: strings(value.tips),
        caveat: optText(value.caveat), heroPhoto: httpsUrl(value.heroPhoto), heroName: optText(value.heroName), status, notes: strings(value.notes),
      };
    }
    case "itinerary": {
      const placeItem = (v: unknown): PlaceItem | undefined => (isObj(v) ? sanitizeItem(v) ?? undefined : undefined);
      const dayCards = list(value.dayCards).filter(isObj).map((d, i) => ({
        title: text(d.title, `Day ${i + 1}`), summary: text(d.summary), day: num(d.day, i + 1),
        stops: list(d.stops).filter(isObj).map((s) => {
          const slot = isObj(s.slot) ? s.slot : {};
          return {
            slot: { when: text(slot.when), activity: text(slot.activity), tip: optText(slot.tip) ?? null, place_index: null, meal_index: null },
            place: placeItem(s.place), meal: placeItem(s.meal),
          };
        }),
      }));
      const stay = list(value.stay).filter(isObj).map((s) => ({ place: placeItem(s.place), why: optText(s.why), picked: s.picked === true }))
        .filter((s): s is { place: PlaceItem; why: string | undefined; picked: boolean } => s.place !== undefined);
      const route = isObj(value.route) ? { distanceKm: num(value.route.distanceKm), durationMinutes: num(value.route.durationMinutes) } : undefined;
      return {
        kind: "itinerary", destination: text(value.destination, "this place"), region: optText(value.region), origin: optText(value.origin),
        days: num(value.days, dayCards.length || 2), party: strings(value.party), ai: value.ai === true, summary: text(value.summary), bestLabel: text(value.bestLabel),
        bestMonths: monthList(value.bestMonths), avoidMonths: monthList(value.avoidMonths), gettingThere: text(value.gettingThere), route, dayCards, stay,
        moreToSee: items(value.moreToSee), tips: strings(value.tips), caveats: strings(value.caveats), month: num(value.month, 1),
        status: value.status === "empty" ? "empty" : "ok",
      };
    }
    case "destinations": {
      const destItems = list(value.items).filter(isObj).filter((d) => typeof d.name === "string" && optNum(d.latitude) !== undefined && optNum(d.longitude) !== undefined).map((d) => ({
        name: String(d.name), region: optText(d.region), why: text(d.why), highlights: strings(d.highlights), bestLabel: text(d.bestLabel), stay: optText(d.stay),
        suits: optText(d.suits), latitude: num(d.latitude), longitude: num(d.longitude), distanceKm: optNum(d.distanceKm), durationMinutes: optNum(d.durationMinutes),
        photo: httpsUrl(d.photo), photoName: optText(d.photoName), topPlaces: list(d.topPlaces).filter(isObj).map((p) => ({ name: text(p.name), rating: optNum(p.rating), ratingCount: optNum(p.ratingCount) })),
        order: num(d.order, 1),
      }));
      return { kind: "destinations", title: text(value.title, "Destinations"), intro: text(value.intro), origin: optText(value.origin), items: destItems, note: optText(value.note), status: destItems.length ? "ok" : "empty" };
    }
    default:
      return null;
  }
}

function sanitizeError(value: unknown): MessageError | undefined {
  if (!isObj(value) || typeof value.code !== "string") return undefined;
  const options = list(value.options).filter(isObj).filter((o) => typeof o.label === "string" && typeof o.text === "string")
    .map((o) => ({ label: String(o.label), text: String(o.text) }));
  return {
    code: value.code, retryable: value.retryable === true, requestId: optText(value.requestId), retryText: optText(value.retryText),
    options: options.length ? options : undefined,
  };
}

function sanitizeMessage(value: unknown): ChatMessage | null {
  if (!isObj(value)) return null;
  if (typeof value.id !== "string" || (value.role !== "user" && value.role !== "assistant") || typeof value.text !== "string") return null;
  const cards = list(value.cards).map(sanitizeCard).filter((card): card is ResultCard => card !== null);
  const retryText = isObj(value.askLocation) ? optText(value.askLocation.retryText) : undefined;
  return {
    id: value.id, role: value.role, text: value.text,
    cards: cards.length ? cards : undefined,
    error: sanitizeError(value.error),
    notice: value.notice === true ? true : undefined,
    askLocation: retryText ? { retryText } : undefined,
  };
}

/** Reads saved state defensively: anything malformed is ignored rather than trusted. */
export function loadState(): PersistedState {
  try {
    const raw = localStorage.getItem(STATE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as { sessionId?: unknown; messages?: unknown };
      const messages = list(parsed?.messages).map(sanitizeMessage).filter((m): m is ChatMessage => m !== null).slice(-MAX_MESSAGES);
      const sessionId = typeof parsed?.sessionId === "string" && parsed.sessionId ? parsed.sessionId : null;
      return { sessionId, messages };
    }
    // One-time migration from the previous app's session key. The conversation text is not carried over.
    const legacy = localStorage.getItem(LEGACY_SESSION_KEY);
    if (legacy) {
      localStorage.removeItem(LEGACY_SESSION_KEY);
      return { sessionId: legacy, messages: [] };
    }
  } catch {
    // Storage blocked or corrupt: start clean.
  }
  return { sessionId: null, messages: [] };
}

/**
 * Tripadvisor's Caching Policy does not allow its photos, ratings or review counts to be stored by an app (HERE supplies
 * them as Tripadvisor content). They are shown from the live response only, so they are left out of the saved copy.
 */
function withoutThirdPartyContent(item: PlaceItem): PlaceItem {
  return { ...item, photo: undefined, photoLarge: undefined, rating: undefined, ratingCount: undefined, ratingUrl: undefined };
}

function stripCard(card: ResultCard): ResultCard {
  switch (card.kind) {
    case "places": case "geocode": return { ...card, items: card.items.map(withoutThirdPartyContent) };
    case "guide":
      return { ...card, heroPhoto: undefined, heroName: undefined, more: card.more.map(withoutThirdPartyContent),
        sections: card.sections.map((s) => ({ ...s, items: s.items.map(withoutThirdPartyContent) })) };
    case "answer":
      return { ...card, subjects: card.subjects.map(withoutThirdPartyContent), more: card.more.map(withoutThirdPartyContent),
        blocks: card.blocks.map((b) => (b.type === "places" ? { ...b, items: b.items.map(withoutThirdPartyContent) } : b)) };
    case "itinerary":
      return { ...card, heroPhoto: undefined, heroName: undefined, moreToSee: card.moreToSee.map(withoutThirdPartyContent),
        stay: card.stay.map((s) => ({ ...s, place: withoutThirdPartyContent(s.place) })),
        dayCards: card.dayCards.map((d) => ({ ...d, stops: d.stops.map((s) => ({
          ...s, place: s.place ? withoutThirdPartyContent(s.place) : undefined, meal: s.meal ? withoutThirdPartyContent(s.meal) : undefined })) })) };
    case "destinations":
      return { ...card, items: card.items.map((d) => ({ ...d, photo: undefined, photoName: undefined, topPlaces: d.topPlaces.map((p) => ({ name: p.name })) })) };
    default: return card;
  }
}

export function saveState(state: PersistedState): void {
  try {
    const messages = state.messages.slice(-MAX_MESSAGES).map((m) => (m.cards ? { ...m, cards: m.cards.map(stripCard) } : m));
    localStorage.setItem(STATE_KEY, JSON.stringify({ sessionId: state.sessionId, messages }));
  } catch {
    // Quota or privacy mode: persistence is best-effort.
  }
}

export function clearState(): void {
  try {
    localStorage.removeItem(STATE_KEY);
    localStorage.removeItem(LEGACY_SESSION_KEY);
  } catch {
    // ignore
  }
}
