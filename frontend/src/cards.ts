import { categoryLabel, formatKm, modeLabel, safeHttpUrl } from "./format";
import type {
  AnswerBlock, AnswerCard, Context, DestinationItem, DestinationsCard, EvCard, GeocodeCard, GuideCard, ItineraryCard, PlaceItem, PlacesCard, Poi,
  ResultCard, RouteCard, TurnResult, WeatherCard,
} from "./types";

const MAX_CARD_ITEMS = 10;
/** The map draws at most this many result markers; cards say so honestly. */
export const MAX_MAP_MARKERS = 60;

/** Identity for a position (stops, chargers, places with no HERE ID). */
export function placeKey(latitude: number, longitude: number): string {
  return `${Number(latitude).toFixed(5)},${Number(longitude).toFixed(5)}`;
}

/** Identity for a place: its HERE ID when it has one, so distinct places at the same coordinates are not merged. */
export function poiKey(poi: Pick<Poi, "here_id" | "latitude" | "longitude" | "name">): string {
  const id = String(poi.here_id ?? "").trim().toLowerCase();
  return id ? `id:${id}` : `${placeKey(poi.latitude, poi.longitude)}:${String(poi.name ?? "").trim().toLowerCase()}`;
}

/** Each place once, in order. Two entries are the same place only if they share an identity. */
export function dedupeByKey<T extends { key: string }>(items: T[]): T[] {
  const seen = new Set<string>();
  return items.filter((item) => (seen.has(item.key) ? false : (seen.add(item.key), true)));
}

export function dedupePois(pois: Poi[]): Poi[] {
  const seen = new Set<string>();
  return pois.filter((poi) => {
    const key = poiKey(poi);
    return seen.has(key) ? false : (seen.add(key), true);
  });
}

function openingHours(value: Poi["opening_hours"]): string | undefined {
  if (!value) return undefined;
  const text = Array.isArray(value) ? value.filter(Boolean).slice(0, 2).join("; ") : String(value);
  return text.trim().slice(0, 120) || undefined;
}

/** An https image URL from HERE, or nothing. */
function photoUrl(value: string | null | undefined): string | undefined {
  const url = safeHttpUrl(value);
  return url?.startsWith("https://") ? url : undefined;
}

export function poiToItem(poi: Poi, scope?: string, readAt: number = Date.now()): PlaceItem {
  const fromReference = poi.distance_from_reference_km;
  const fromRoute = poi.distance_from_route_km;
  const distanceKm = fromReference ?? fromRoute ?? undefined;
  const reference = scope === "midpoint" ? "from the halfway point" : "away";
  let distanceLabel =
    fromReference != null ? `${formatKm(fromReference)} ${reference}` : fromRoute != null ? `${formatKm(fromRoute)} from route` : undefined;
  // Around the halfway point both numbers matter: how far from the point, and how far off the route.
  if (scope === "midpoint" && fromReference != null && fromRoute != null) distanceLabel += ` · ${formatKm(fromRoute)} off route`;
  return {
    key: poiKey(poi),
    name: poi.name || "Place",
    category: poi.category || "place",
    hereCategory: poi.here_category || undefined,
    city: poi.city || undefined,
    latitude: poi.latitude,
    longitude: poi.longitude,
    address: poi.address || undefined,
    distanceKm: distanceKm ?? undefined,
    distanceLabel,
    rating: typeof poi.rating === "number" ? poi.rating : undefined,
    ratingCount: typeof poi.rating_count === "number" && poi.rating_count > 0 ? poi.rating_count : undefined,
    ratingUrl: safeHttpUrl(poi.rating_url),
    photo: photoUrl(poi.photo?.small),
    photoLarge: photoUrl(poi.photo?.large),
    openNow: typeof poi.open_now === "boolean" ? poi.open_now : undefined,
    openNowAt: typeof poi.open_now === "boolean" ? readAt : undefined,
    phone: poi.phone || undefined,
    website: safeHttpUrl(poi.website),
    openingHours: openingHours(poi.opening_hours),
    source: poi.source || undefined,
  };
}

const PLACE_ACTIONS = new Set([
  "geocode", "find_pois_near_location", "find_pois", "find_pois_in_area", "find_pois_near_anchor", "show_pois", "search_charger_nearby", "compound_trip_request",
]);
// A destinations fallback (no AI) reuses the ordinary places card, so a suggestion request never ends in an empty answer.
const ROUTE_ACTIONS = new Set(["plan_trip", "modify_trip", "set_trip_options", "compound_trip_request"]);
const EV_ACTIONS = new Set([
  "ev_plan", "update_vehicle", "update_settings", "simulate", "ev_mode", "show_charging_stop", "recommend_charger", "plan_trip", "modify_trip", "compound_trip_request",
]);

/** Limits worth showing next to the results themselves (the chat text carries the same facts in prose). */
function placeNotes(result: TurnResult): string[] {
  const notes: string[] = [];
  const search = result.search;
  const coverage = result.coverage;
  if (result.reference_note) notes.push(result.reference_note);
  if (result.qualifier_note) notes.push(result.qualifier_note);
  if (result.scope === "area" && search && search.complete === false) {
    notes.push("This list may be incomplete: HERE returns at most 100 places per request. Name a district or city for a fuller list.");
  } else if (result.scope === "area" && search?.complete) {
    notes.push("Everything HERE's data classifies in this area. Places HERE doesn't list won't appear.");
  } else if (result.scope !== "area" && search?.page_full) {
    notes.push("HERE returns at most 100 places per search, so there may be more.");
  }
  if (coverage && coverage.complete === false) {
    notes.push("Parts of a long or busy route may not be covered.");
  }
  if (search?.rejected) notes.push(`${search.rejected} result${search.rejected === 1 ? "" : "s"} left out because HERE doesn't classify ${search.rejected === 1 ? "it" : "them"} as this type of place.`);
  return notes;
}

function scopeLabel(result: TurnResult): string | undefined {
  switch (result.scope) {
    case "area": {
      const kind = { state: "state", district: "district", city: "city area", region: "region" }[String(result.area?.kind)];
      return `in ${result.area?.name ?? "this area"}${kind ? ` (${kind})` : ""}`;
    }
    case "midpoint": return "near the halfway point of your trip";
    case "me": return "near your location";
    case "route": return "along your route";
    default: {
      const where = result.target || result.center?.name;
      return where ? `near ${where}` : undefined;
    }
  }
}

function placesCard(result: TurnResult): PlacesCard[] {
  if (!PLACE_ACTIONS.has(result.action || "")) return [];
  // A plain address lookup has no places of its own; a named-place lookup ("Where is ...?") does.
  if (result.action === "geocode" && !result.pois?.length) return [];
  // A combined request ("plan a trip and find cafes at the halfway point") has one card per search.
  if (result.action === "compound_trip_request" && result.poi_groups?.length) {
    return result.poi_groups.map((group) => singlePlacesCard(group)).filter((c): c is PlacesCard => c !== null);
  }
  const card = singlePlacesCard(result);
  return card ? [card] : [];
}

function singlePlacesCard(result: TurnResult): PlacesCard | null {
  const pois = dedupePois(result.pois || []);
  const status = result.source_status;
  const label = scopeLabel(result);
  const base = result.pois_label || (result.category ? `${categoryLabel(result.category)}${label ? ` ${label}` : ""}` : "Places");
  const title = result.action === "find_pois_near_anchor" && result.anchor_step === "choose" ? `${base} (pick one)` : base;
  const notes = placeNotes(result);
  if (status === "unavailable") {
    return { kind: "places", title, scopeLabel: label, items: [], total: 0, status: "unavailable", notes, source: result.source || undefined, suggestion: "Try again in a moment." };
  }
  if (!pois.length) {
    return { kind: "places", title, scopeLabel: label, items: [], total: 0, status: "empty", notes, source: result.source || undefined, suggestion: "Try a wider radius, like “within 10 km”, or a different search term." };
  }
  return {
    kind: "places",
    title,
    scopeLabel: label,
    items: pois.slice(0, MAX_CARD_ITEMS).map((poi) => poiToItem(poi, result.scope)),
    total: pois.length,
    status: status === "fallback" ? "fallback" : "ok",
    notes,
    source: result.source || pois[0]?.source || undefined,
  };
}

function answerCard(result: TurnResult): AnswerCard | null {
  if (result.action !== "ask") return null;
  const pois = result.pois || [];
  const items = pois.map((poi) => poiToItem(poi, result.scope));
  const doc = result.answer_doc || null;
  const subjects = (result.subjects || [])
    .filter((s) => s.kind === "place" && typeof s.poi_index === "number" && items[s.poi_index])
    .map((s) => items[s.poi_index as number]);
  const subjectKeys = new Set(subjects.map((s) => s.key));
  const pointedAt = new Set<string>(subjectKeys);
  const blocks: AnswerBlock[] = [];
  for (const block of doc?.blocks || []) {
    if (block.type !== "places") {
      blocks.push(block);
      continue;
    }
    const shown = dedupeByKey(block.place_indexes.map((i) => items[i]).filter((item): item is PlaceItem => Boolean(item) && !subjectKeys.has(item.key)));
    shown.forEach((item) => pointedAt.add(item.key));
    if (shown.length) blocks.push({ type: "places", title: block.title, items: shown });
  }
  // With an answer, only the places it points at. Without one (no AI), everything HERE found, so nothing is hidden.
  const more = doc ? [] : dedupeByKey(items).filter((item) => !pointedAt.has(item.key)).slice(0, MAX_CARD_ITEMS);
  const status = result.source_status === "unavailable" ? "unavailable" : items.length ? "ok" : "empty";
  const note = !doc
    ? result.ai_available === false
      ? "The AI service that writes explanations isn't connected, so there is no written answer. These are HERE's verified places."
      : "I couldn't put an answer together this time."
    : undefined;
  return { kind: "answer", ai: Boolean(doc), subjects, blocks, caveats: doc?.caveats || [], suggestions: doc?.suggestions || [], more, status, note };
}

function guideCard(result: TurnResult): GuideCard | null {
  if (result.action !== "destination_guide") return null;
  const pois = result.pois || [];
  const guide = result.guide || null;
  const items = pois.map((poi) => poiToItem(poi, result.scope));
  const used = new Set<number>();
  const sections = (guide?.sections || []).map((section) => {
    const indexes = section.place_indexes.filter((i) => i >= 0 && i < items.length && !used.has(i));
    indexes.forEach((i) => used.add(i));
    return {
      title: section.title,
      months: section.months,
      monthsLabel: section.months_label,
      badge: section.badge || undefined,
      summary: section.summary,
      bullets: section.bullets,
      items: indexes.map((i) => items[i]),
    };
  });
  const hero = items.find((item) => item.photoLarge);
  const status = result.source_status === "unavailable" ? "unavailable" : pois.length ? "ok" : "empty";
  return {
    kind: "guide",
    place: result.place || "this place",
    region: result.region || undefined,
    ai: guide !== null,
    headline: guide?.headline || "",
    summary: guide?.summary || "",
    bestMonths: guide?.best_months || [],
    goodMonths: guide?.good_months || [],
    avoidMonths: guide?.avoid_months || [],
    bestLabel: guide?.best_label || "",
    sections,
    more: items.filter((_, i) => !used.has(i)).slice(0, MAX_CARD_ITEMS),
    tips: guide?.tips || [],
    caveat: guide?.caveat || undefined,
    heroPhoto: hero?.photoLarge,
    heroName: hero?.name,
    weather: result.place_weather ? { ...result.place_weather, place: result.place || result.place_weather.place } : undefined,
    status,
    notes: [],
  };
}

function itineraryCard(result: TurnResult): ItineraryCard | null {
  const it = result.itinerary;
  if (result.action !== "trip_itinerary" || !it) return null;
  const toItems = (pois: Poi[]) => pois.map((poi) => poiToItem(poi));
  const attractions = toItems(it.attractions || []);
  const restaurants = toItems(it.restaurants || []);
  const hotels = toItems(it.hotels || []);
  const plan = it.plan;
  const dayCards = (plan?.days || []).map((day, i) => ({
    title: day.title,
    summary: day.summary,
    day: i + 1,
    stops: day.slots.map((slot) => ({
      slot,
      place: slot.place_index != null ? attractions[slot.place_index] : undefined,
      meal: slot.meal_index != null ? restaurants[slot.meal_index] : undefined,
    })),
  }));
  const picks = new Map((plan?.stay || []).map((s) => [s.hotel_index, s.why]));
  const stay = hotels.map((place, i) => ({ place, why: picks.get(i) || undefined, picked: picks.has(i) }));
  // Without a written plan the places are shown as a list, so nothing HERE found is hidden.
  const moreToSee = plan ? toItems(it.more_attractions || []) : attractions;
  const hero = [...attractions, ...toItems(it.more_attractions || [])].find((p) => p.photoLarge);
  const route = it.route && it.route.distance_km != null ? { distanceKm: it.route.distance_km, durationMinutes: it.route.duration_minutes ?? 0 } : undefined;
  return {
    kind: "itinerary",
    destination: it.destination,
    region: it.region || undefined,
    origin: it.origin || undefined,
    days: it.days,
    party: it.party || [],
    ai: it.ai,
    summary: plan?.summary || "",
    bestLabel: plan?.best_label || "",
    bestMonths: plan?.best_months || [],
    avoidMonths: plan?.avoid_months || [],
    gettingThere: plan?.getting_there || "",
    route,
    dayCards,
    stay,
    moreToSee,
    tips: plan?.tips || [],
    caveats: plan?.caveats || [],
    heroPhoto: hero?.photoLarge,
    heroName: hero?.name,
    weather: result.place_weather ? { ...result.place_weather, place: it.destination } : undefined,
    month: it.month,
    status: attractions.length || hotels.length || moreToSee.length ? "ok" : "empty",
  };
}

function destinationsCard(result: TurnResult): DestinationsCard | null {
  if (result.action !== "suggest_destinations") return null;
  const items: DestinationItem[] = (result.destinations || []).map((d) => ({
    name: d.name,
    region: d.region || undefined,
    why: d.why,
    highlights: d.highlights || [],
    bestLabel: d.best_label || "",
    stay: d.stay || undefined,
    suits: d.suits || undefined,
    latitude: d.latitude,
    longitude: d.longitude,
    distanceKm: d.distance_km ?? undefined,
    durationMinutes: d.duration_minutes ?? undefined,
    photo: photoUrl(d.photo?.small ? d.photo.large || d.photo.small : null),
    photoName: d.photo_name || undefined,
    topPlaces: (d.top_places || []).map((p) => ({ name: p.name, rating: p.rating ?? undefined, ratingCount: p.rating_count ?? undefined })),
    weather: d.weather ? { current: d.weather.current, days: d.weather.days || [] } : undefined,
    order: d.order,
  }));
  const kind = result.kind ? categoryLabel(result.kind) : "Destinations";
  return {
    kind: "destinations",
    title: `${kind}${result.origin ? ` near ${result.origin}` : result.region ? ` in ${result.region}` : ""}`,
    intro: result.intro || "",
    origin: result.origin || undefined,
    items,
    note: result.destinations_note || undefined,
    status: items.length ? "ok" : "empty",
  };
}

function weatherCard(result: TurnResult): WeatherCard | null {
  const weather = result.place_weather;
  if (!weather || ["trip_itinerary", "suggest_destinations", "destination_guide"].includes(result.action || "")) return null;
  if (!weather.current && !weather.days?.length) return null;
  return { kind: "weather", place: weather.place || result.center?.name || "this place", weather };
}

function routeCard(result: TurnResult, context: Context): RouteCard | null {
  const route = result.route;
  if (!route || !ROUTE_ACTIONS.has(result.action || "") || context.stops.length < 2) return null;
  const names = context.stops.map((s) => s.name);
  const legs = (route.legs || [])
    .filter((leg) => leg.from_stop_index < names.length)
    .map((leg) => ({
      from: names[leg.from_stop_index],
      to: names[Math.min(leg.to_stop_index, names.length - 1)],
      distanceKm: leg.distance_km,
      durationMinutes: leg.duration_minutes,
    }));
  const fuel = context.vehicle_type;
  return {
    kind: "route",
    stops: context.stops.map((s) => ({ name: s.name, role: s.role })),
    distanceKm: route.distance_km,
    durationMinutes: route.duration_minutes,
    mode: modeLabel(route.transport_mode || context.travel_mode),
    legs,
    change: result.change,
    fuelNote: !context.ev_mode && (fuel === "petrol" || fuel === "diesel" || fuel === "hybrid")
      ? `Set up for a ${fuel} vehicle. Fuel use and cost aren't calculated.`
      : undefined,
  };
}

function geocodeCard(result: TurnResult): GeocodeCard | null {
  // A named place ("Where is the airport?") already has its own place card; the bare address lookup is for everything else.
  if (result.action !== "geocode" || !result.results?.length || result.pois?.length) return null;
  return {
    kind: "geocode",
    query: result.query || "",
    items: result.results.slice(0, 3).map((r) => ({
      key: placeKey(r.latitude, r.longitude),
      name: r.display_name || result.query || "Place",
      category: "place",
      latitude: r.latitude,
      longitude: r.longitude,
    })),
    others: (result.ambiguous || []).slice(1, 4),
  };
}

/** EV information exists only while the optional EV Planner is on. Ordinary trips never get this card. */
export function evCard(context: Context, planOverride?: Context["ev_plan"]): EvCard | null {
  if (!context.ev_mode) return null;
  const plan = planOverride ?? context.ev_plan;
  const status = context.ev_status;
  const stops = (plan?.charging_stops || []).map((s) => ({
    name: s.charger?.name || s.charger?.address || "EV charging station",
    arrival: s.battery_arrival_percent,
    departure: s.battery_departure_percent,
  }));
  if (status === "calculated" && plan) {
    return { kind: "ev", status: "calculated", tripPossible: plan.trip_possible, stops, failureReason: plan.trip_possible ? undefined : plan.failure_reason };
  }
  if (status === "failed") return { kind: "ev", status: "failed", stops: [] };
  return { kind: "ev", status: status === "needs_vehicle" ? "needs_vehicle" : "ready", stops: [] };
}

export function buildCards(result: TurnResult, context: Context): ResultCard[] {
  const cards: ResultCard[] = [];
  const route = routeCard(result, context);
  if (route) cards.push(route);
  cards.push(...placesCard(result));
  const guide = guideCard(result);
  if (guide) cards.push(guide);
  const itinerary = itineraryCard(result);
  if (itinerary) cards.push(itinerary);
  const destinations = destinationsCard(result);
  if (destinations) cards.push(destinations);
  const answer = answerCard(result);
  if (answer) cards.push(answer);
  const weather = weatherCard(result);
  if (weather) cards.push(weather);
  const geo = geocodeCard(result);
  if (geo) cards.push(geo);
  if (result.action === "route_info" && result.distance_km != null) {
    cards.push({
      kind: "distance",
      to: result.to || "your destination",
      from: result.measured_from,
      stopNumber: result.stop_number,
      distanceKm: result.distance_km,
      durationMinutes: result.duration_minutes ?? 0,
    });
  }
  if (EV_ACTIONS.has(result.action || "")) {
    const ev = evCard(context, result.ev_plan);
    if (ev) cards.push(ev);
  }
  return cards;
}
