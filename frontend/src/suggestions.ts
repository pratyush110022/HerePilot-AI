import type { ChatMessage, Context, Suggestion } from "./types";

/** First-run prompts: one for each kind of thing the assistant does, none about EVs. */
export const STARTER_SUGGESTIONS: Suggestion[] = [
  { label: "Best time to visit Ooty", text: "What is the best time to visit Ooty?", mode: "send" },
  { label: "Restaurants near Connaught Place", text: "Find restaurants near Connaught Place, Delhi", mode: "send" },
  { label: "Plan a trip: Mumbai to Bhubaneswar", text: "Plan a trip from Mumbai to Bhubaneswar", mode: "send" },
  { label: "Airports in Odisha", text: "Share the list of airports in Odisha", mode: "send" },
  { label: "Compare Coorg and Mysuru", text: "Compare Coorg and Mysuru for a three-day trip", mode: "send" },
];

const MAX_CHIPS = 4;

const send = (label: string, text = label): Suggestion => ({ label, text, mode: "send" });
/** "Connaught Place, Delhi" -> "Connaught Place": chips are short, the full name still goes in the question. */
const short = (name: string): string => name.split(",")[0].trim();

/** What the assistant last showed, which decides what is worth suggesting next. */
function lastAssistant(messages: ChatMessage[]): ChatMessage | undefined {
  for (let i = messages.length - 1; i >= 0; i -= 1) if (messages[i].role === "assistant" && !messages[i].error) return messages[i];
  return undefined;
}

/**
 * A few follow-ups that fit the conversation right now, never a fixed menu:
 * the AI's own suggested follow-ups when it gave an answer, other things to look for around the place just searched,
 * route actions while a trip is on screen, and nothing at all when there is nothing sensible to add.
 */
export function suggestionsFor(context: Context | null, aiAvailable: boolean, messages: ChatMessage[] = []): Suggestion[] {
  const last = lastAssistant(messages);
  const answer = last?.cards?.find((card) => card.kind === "answer");
  if (answer?.kind === "answer" && answer.suggestions.length > 0) return answer.suggestions.slice(0, MAX_CHIPS - 1).map((s) => send(s));

  const guide = last?.cards?.find((card) => card.kind === "guide");
  if (guide?.kind === "guide") {
    const place = guide.place;
    return [send(`Hotels in ${place}`, `Find hotels in ${place}`), send(`Restaurants in ${place}`, `Find restaurants in ${place}`),
      send(`Cafes near ${place}`, `Find cafes near ${place}`)];
  }

  const hasRoute = Boolean(context && context.stops.length >= 2 && context.route);
  if (hasRoute && context) {
    const out: Suggestion[] = [send("Petrol pumps along my route", "Find petrol pumps along my route"), send("Restaurants along my route", "Find restaurants along my route"),
      send("Coffee at the halfway point", "Find a coffee shop near the halfway point of my trip"), send("Hotels near my destination", "Find hotels near my destination")];
    if (aiAvailable && context.pois.length > 1) out.splice(1, 0, send("Which of these is closest?"));
    return out.slice(0, MAX_CHIPS);
  }

  const around = context?.focus?.name;
  // "Near your location" is only worth suggesting while the location is still shared.
  const stale = around === "your location" && !context?.user_location;
  if (context && context.pois.length > 0 && around && !stale) {
    const out: Suggestion[] = [];
    if (aiAvailable && context.pois.length > 1) out.push(send("Which of these is closest?"));
    const shown = (context.pois_label || "").toLowerCase();
    for (const [label, query] of [["Restaurants", "restaurants"], ["Cafes", "cafes"], ["Hotels", "hotels"], ["Petrol pumps", "petrol pumps"]] as const) {
      if (!shown.includes(query.split(" ")[0])) out.push(send(`${label} near ${short(around)}`, `Find ${query} near ${around}`));
    }
    return out.slice(0, MAX_CHIPS);
  }
  return [];
}
