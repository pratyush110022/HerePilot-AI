/** Shapes returned by the HerePilot-AI backend. Optional fields are optional on purpose: the UI must cope with absence. */

export type VehicleType = "petrol" | "diesel" | "hybrid" | "electric";
export type TravelMode = "car" | "pedestrian" | "bicycle";
export type EvStatus = "not_requested" | "needs_vehicle" | "calculated" | "failed";

export type Stop = {
  name: string;
  display_name: string;
  latitude: number;
  longitude: number;
  role: string;
  result_type?: string | null;
};

export type Poi = {
  name: string;
  category: string;
  latitude: number;
  longitude: number;
  address?: string | null;
  city?: string | null;
  state?: string | null;
  /** HERE's own name for the place's primary category ("Coffee Shop"), as opposed to the category that was asked for. */
  here_category?: string | null;
  rating?: number | null;
  /** How many reviews the rating is based on, and where to read them (Tripadvisor, supplied by HERE). */
  rating_count?: number | null;
  rating_url?: string | null;
  rating_source?: string | null;
  /** A photo HERE attaches to the place: a 150 px square for lists and a 550 px image for headers. */
  photo?: { small: string; large: string; supplier?: string } | null;
  /** HERE's own open-now flag at the time of the search. */
  open_now?: boolean | null;
  phone?: string | null;
  website?: string | null;
  opening_hours?: string | string[] | null;
  operator?: string | null;
  source?: string | null;
  here_id?: string | null;
  osm_id?: number | string | null;
  distance_from_reference_km?: number | null;
  distance_from_route_km?: number | null;
  route_position_percent?: number | null;
  charger_id?: string | number | null;
  brand_key?: string | null;
  /** Set on places a plan or suggestion put on the map: how to draw them, and where they sit in the plan. */
  marker?: "stop" | "meal" | "hotel" | "destination" | "origin" | null;
  day?: number | null;
  order?: number | null;
  when?: string | null;
};

/** Weather at one place, from Open-Meteo: now, a daily forecast, and what each month is usually like. */
export type WeatherKind = "clear" | "partly" | "cloudy" | "fog" | "drizzle" | "rain" | "snow" | "storm";
export type WeatherNow = { temperature_c: number | null; feels_like_c?: number | null; condition: string; kind: WeatherKind; humidity_percent?: number | null; wind_kmh?: number | null };
export type WeatherDay = {
  date: string;
  condition: string;
  kind: WeatherKind;
  temp_max_c: number | null;
  temp_min_c: number | null;
  rain_chance_percent: number | null;
  rain_mm: number | null;
};
export type ClimateMonth = { month: number; temp_max_c: number | null; temp_min_c: number | null; rain_mm: number | null; rainy_days: number | null };
export type PlaceWeather = {
  place?: string;
  provider?: string;
  current?: WeatherNow | null;
  days?: WeatherDay[];
  climate?: { years: string; months: ClimateMonth[] };
};

/** A position the user chose to share. Kept in memory and sent only with chat messages. */
export type UserLocation = { latitude: number; longitude: number; accuracy_m?: number };

export type RouteLeg = {
  from_stop_index: number;
  to_stop_index: number;
  distance_km: number;
  duration_minutes: number;
};

export type StopPosition = {
  stop_index: number;
  name?: string;
  latitude: number;
  longitude: number;
  distance_from_start_km: number;
  duration_from_start_minutes: number;
};

export type Route = {
  provider?: string;
  distance_km: number;
  duration_minutes: number;
  transport_mode?: string;
  geometry: { type: "LineString"; coordinates: [number, number][] };
  here_flexible_polyline?: string | null;
  here_flexible_polylines?: string[];
  legs?: RouteLeg[];
  stop_positions?: StopPosition[];
};

export type Charger = {
  id?: string | number;
  name?: string;
  address?: string;
  city?: string;
  cpoName?: string;
  latitude: number;
  longitude: number;
  socket_type?: string;
  charging_speed?: string;
  distance_from_route_km?: number;
  route_position_percent?: number;
  brand_key?: string;
  source?: string;
};

export type ChargingStop = {
  charger: Charger;
  battery_arrival_percent?: number;
  battery_departure_percent?: number;
  distance_from_start_km?: number;
  estimated_charging_time_minutes?: number | null;
};

export type EvPlan = {
  trip_possible: boolean;
  requires_charging?: boolean;
  charging_stops: ChargingStop[];
  starting_battery_percent?: number;
  destination_battery_percent?: number;
  failure_reason?: string;
  message?: string;
};

export type Vehicle = {
  make?: string | null;
  model?: string | null;
  max_range_km?: number | null;
  battery_capacity_kwh?: number | null;
  current_battery_percent?: number | null;
};

export type Focus = { name: string; latitude: number; longitude: number; kind: string; index?: number | null };

/** The session snapshot the backend attaches to every response. */
export type Context = {
  session_id: string;
  stops: Stop[];
  route: Route | null;
  pois: Poi[];
  pois_label?: string | null;
  chargers: Charger[];
  vehicle: Vehicle | null;
  vehicle_type: VehicleType | null;
  travel_mode: TravelMode;
  ev_mode: boolean;
  ev_status: EvStatus;
  ev_plan: EvPlan | null;
  reserve_percent: number;
  target_charge_percent: number;
  corridor_km: number;
  focus: Focus | null;
  user_location?: UserLocation | null;
  route_revision: number;
  turn_count: number;
};

export type GuideSection = {
  title: string;
  months: number[];
  months_label: string;
  badge: string | null;
  summary: string;
  bullets: string[];
  /** Positions in the result's `pois` list. */
  place_indexes: number[];
};

/** Travel advice written by the AI (general knowledge). The places it points at come from HERE. */
export type Guide = {
  headline: string;
  summary: string;
  best_months: number[];
  good_months: number[];
  avoid_months: number[];
  best_label: string;
  sections: GuideSection[];
  tips: string[];
  caveat: string | null;
};

/** One slot of a day plan. `place_index` / `meal_index` point into the itinerary's `attractions` / `restaurants`. */
export type ItinerarySlot = { when: string; activity: string; tip: string | null; place_index: number | null; meal_index: number | null };
export type ItineraryDay = { title: string; summary: string; slots: ItinerarySlot[] };
export type ItineraryPlan = {
  summary: string;
  best_months: number[];
  avoid_months: number[];
  best_label: string;
  getting_there: string;
  days: ItineraryDay[];
  stay: Array<{ hotel_index: number; why: string }>;
  tips: string[];
  caveats: string[];
};
export type Itinerary = {
  destination: string;
  region?: string | null;
  origin?: string | null;
  days: number;
  party: string[];
  interests: string[];
  month: number;
  budget?: string | null;
  ai: boolean;
  plan: ItineraryPlan | null;
  attractions: Poi[];
  more_attractions: Poi[];
  hotels: Poi[];
  restaurants: Poi[];
  route: { distance_km?: number; duration_minutes?: number; mode?: string } | null;
};

/** A destination the AI proposed and HERE found: its position, the drive from the start, a photo and the weather. */
export type SuggestedDestination = {
  name: string;
  region?: string | null;
  why: string;
  highlights: string[];
  best_months: number[];
  best_label: string;
  stay?: string | null;
  suits?: string | null;
  latitude: number;
  longitude: number;
  distance_km: number | null;
  duration_minutes: number | null;
  photo?: { small: string; large: string } | null;
  photo_name?: string | null;
  top_places: Array<{ name: string; rating?: number | null; rating_count?: number | null; here_category?: string | null }>;
  weather?: { current?: WeatherNow | null; days?: WeatherDay[] } | null;
  order: number;
};

/** An open-question answer written by the AI. Block types are chosen by the model; place blocks point into `pois`. */
export type AnswerBlockRaw =
  | { type: "text"; title: string; text: string }
  | { type: "list" | "steps"; title: string; items: string[] }
  | { type: "pros_cons"; title: string; pros: string[]; cons: string[] }
  | { type: "compare"; title: string; columns: string[]; rows: Array<{ label: string; values: string[] }> }
  | { type: "itinerary"; title: string; days: Array<{ title: string; items: string[] }> }
  | { type: "facts"; title: string; items: Array<{ label: string; value: string }> }
  | { type: "places"; title: string; place_indexes: number[] };

export type AnswerDoc = { answer: string; blocks: AnswerBlockRaw[]; caveats: string[]; suggestions: string[] };

/** The per-turn result. Loosely typed: only the fields the UI reads are listed. */
export type TurnResult = {
  success?: boolean;
  action?: string;
  question?: string | null;
  category?: string;
  pois?: Poi[];
  pois_label?: string | null;
  source?: string | null;
  source_status?: "verified" | "fallback" | "unavailable";
  source_error?: string | null;
  radius_km?: number;
  scope?: string;
  coverage?: { complete?: boolean; route_km?: number; sampled_points?: number; sample_cap_reached?: boolean; full_pages?: number; failed_points?: number };
  search?: { complete?: boolean; page_full?: boolean; rejected?: number; outside_area?: number; matched?: number; requests?: number; budget_exhausted?: boolean; unresolved_full_tiles?: number };
  area?: { name?: string; kind?: string; state?: string | null };
  center?: { latitude: number; longitude: number; name?: string };
  reference_note?: string | null;
  qualifier_note?: string | null;
  anchor_step?: "choose" | "none_found" | "unavailable";
  needs_location?: boolean;
  guide?: Guide | null;
  guide_source?: string | null;
  answer_doc?: AnswerDoc | null;
  subjects?: Array<{ name: string; kind: string; poi_index?: number | null; alternatives?: number }>;
  ai_available?: boolean;
  itinerary?: Itinerary;
  destinations?: SuggestedDestination[];
  intro?: string;
  origin?: string | null;
  kind?: string | null;
  destinations_note?: string | null;
  ranking_note?: string | null;
  ranked_by?: string | null;
  place_weather?: PlaceWeather;
  nearest?: boolean;
  named_place?: Poi;
  alternatives?: number;
  place?: string;
  region?: string | null;
  poi_groups?: Array<TurnResult>;
  route?: Route;
  change?: string;
  previous_stops?: string[];
  distance_km?: number;
  duration_minutes?: number;
  to?: string;
  stop_number?: number;
  measured_from?: string;
  target?: string;
  results?: Array<{ display_name?: string; latitude: number; longitude: number; state?: string; country?: string }>;
  ambiguous?: string[];
  ev_plan?: EvPlan | null;
  ev_status?: string;
  ev_mode?: boolean;
  needs_vehicle?: boolean;
  message?: string;
  answer?: string | null;
  query?: string;
  changed?: string[];
};

export type ChatResponse = {
  session_id: string;
  message: string;
  reply: string;
  result: TurnResult;
  context: Context;
};

export type ApiErrorBody = {
  detail?: string;
  error?: {
    code: string;
    message: string;
    retryable: boolean;
    request_id?: string;
    data?: { query?: string; options?: string[] };
  };
};

export type Health = {
  status: string;
  version: string;
  here_configured: boolean;
  ai: { provider: string | null; available: boolean; reason?: string | null };
};

// ---- derived, compact "cards" kept with each chat message (and persisted) ----------------------
export type PlaceItem = {
  /** Stable identity: the HERE ID when there is one (so two places at the same spot stay two), else the coordinates. */
  key: string;
  name: string;
  category: string;
  latitude: number;
  longitude: number;
  address?: string;
  distanceKm?: number;
  distanceLabel?: string;
  hereCategory?: string;
  city?: string;
  rating?: number;
  phone?: string;
  website?: string;
  openingHours?: string;
  source?: string;
  /** Tripadvisor review data supplied by HERE. */
  ratingCount?: number;
  ratingUrl?: string;
  photo?: string;
  photoLarge?: string;
  /** HERE's open-now flag and when it was read, so a saved conversation never claims "open now" days later. */
  openNow?: boolean;
  openNowAt?: number;
};

export type PlacesCard = {
  kind: "places";
  title: string;
  items: PlaceItem[];
  total: number;
  source?: string;
  status: "ok" | "fallback" | "unavailable" | "empty";
  /** Where the search looked, e.g. "in Odisha (state)" or "near the halfway point of your trip". */
  scopeLabel?: string;
  /** Plain-language limits of this result: partial coverage, ignored wishes like "quiet", how distances are measured. */
  notes: string[];
  suggestion?: string;
};

export type RouteCard = {
  kind: "route";
  stops: Array<{ name: string; role: string }>;
  distanceKm: number;
  durationMinutes: number;
  mode: string;
  legs: Array<{ from: string; to: string; distanceKm: number; durationMinutes: number }>;
  change?: string;
  fuelNote?: string;
};

export type DistanceCard = {
  kind: "distance";
  to: string;
  from?: string;
  stopNumber?: number;
  distanceKm: number;
  durationMinutes: number;
};

export type GeocodeCard = {
  kind: "geocode";
  query: string;
  items: PlaceItem[];
  others: string[];
};

export type EvCard = {
  kind: "ev";
  status: EvStatus | "ready";
  tripPossible?: boolean;
  stops: Array<{ name: string; arrival?: number; departure?: number }>;
  failureReason?: string;
};

export type GuideSectionCard = {
  title: string;
  months: number[];
  monthsLabel: string;
  badge?: string;
  summary: string;
  bullets: string[];
  items: PlaceItem[];
};

/** A travel guide: AI advice arranged around HERE places. `ai` is false when only the places could be shown. */
export type GuideCard = {
  kind: "guide";
  place: string;
  region?: string;
  ai: boolean;
  headline: string;
  summary: string;
  bestMonths: number[];
  goodMonths: number[];
  avoidMonths: number[];
  bestLabel: string;
  sections: GuideSectionCard[];
  more: PlaceItem[];
  tips: string[];
  caveat?: string;
  heroPhoto?: string;
  heroName?: string;
  /** Live weather and measured month-by-month climate for the destination, from Open-Meteo. */
  weather?: PlaceWeather;
  status: "ok" | "empty" | "unavailable";
  notes: string[];
};

export type AnswerBlock =
  | Exclude<AnswerBlockRaw, { type: "places" }>
  | { type: "places"; title: string; items: PlaceItem[] };

/** An answer to an open question: the AI's reasoning laid out in the blocks it chose, around HERE's verified places. */
export type AnswerCard = {
  kind: "answer";
  ai: boolean;
  /** The specific places the question was about (HERE's own records), shown first. */
  subjects: PlaceItem[];
  blocks: AnswerBlock[];
  caveats: string[];
  suggestions: string[];
  /** Places HERE found that the answer did not point at (all of them when there is no AI answer). */
  more: PlaceItem[];
  status: "ok" | "empty" | "unavailable";
  note?: string;
};

export type WeatherCard = { kind: "weather"; place: string; weather: PlaceWeather };

export type ItineraryStop = { slot: ItinerarySlot; place?: PlaceItem; meal?: PlaceItem };
export type ItineraryDayCard = { title: string; summary: string; day: number; stops: ItineraryStop[] };
export type StayItem = { place: PlaceItem; why?: string; picked: boolean };

/** A day-by-day plan: AI advice arranged around HERE places, with the road route and the weather. `ai` is false when only the places could be shown. */
export type ItineraryCard = {
  kind: "itinerary";
  destination: string;
  region?: string;
  origin?: string;
  days: number;
  party: string[];
  ai: boolean;
  summary: string;
  bestLabel: string;
  bestMonths: number[];
  avoidMonths: number[];
  gettingThere: string;
  route?: { distanceKm: number; durationMinutes: number };
  dayCards: ItineraryDayCard[];
  stay: StayItem[];
  moreToSee: PlaceItem[];
  tips: string[];
  caveats: string[];
  heroPhoto?: string;
  heroName?: string;
  weather?: PlaceWeather;
  month: number;
  status: "ok" | "empty";
};

export type DestinationItem = {
  name: string;
  region?: string;
  why: string;
  highlights: string[];
  bestLabel: string;
  stay?: string;
  suits?: string;
  latitude: number;
  longitude: number;
  distanceKm?: number;
  durationMinutes?: number;
  photo?: string;
  photoName?: string;
  topPlaces: Array<{ name: string; rating?: number; ratingCount?: number }>;
  weather?: { current?: WeatherNow | null; days: WeatherDay[] };
  order: number;
};

/** Where to go: destinations the AI proposed, each checked and measured by HERE. */
export type DestinationsCard = {
  kind: "destinations";
  title: string;
  intro: string;
  origin?: string;
  items: DestinationItem[];
  note?: string;
  status: "ok" | "empty";
};

export type ResultCard = PlacesCard | RouteCard | DistanceCard | GeocodeCard | EvCard | GuideCard | AnswerCard | ItineraryCard | DestinationsCard | WeatherCard;

export type MessageError = {
  code: string;
  retryable: boolean;
  requestId?: string;
  retryText?: string;
  options?: Array<{ label: string; text: string }>;
};

export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  text: string;
  /** Set when the assistant needs the user's location; the message offers a "Share my location" button. */
  askLocation?: { retryText: string };
  cards?: ResultCard[];
  error?: MessageError;
  notice?: boolean;
};

export type Suggestion = { label: string; text: string; mode: "send" | "fill" };
