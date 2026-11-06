import { useCallback, useMemo, useRef, useState, type KeyboardEvent } from "react";
import "./App.css";
import { BACKEND_URL } from "./api";
import { Header } from "./components/Brand";
import Composer from "./components/Composer";
import LocateControl from "./components/LocateControl";
import EvPlannerPanel from "./components/EvPlannerPanel";
import Messages from "./components/Messages";
import TripPanel from "./components/TripPanel";
import Welcome from "./components/Welcome";
import MapView, { type FocusRequest } from "./MapView";
import { suggestionsFor } from "./suggestions";
import type { PlaceItem } from "./types";
import { useConversation, type ActionOutcome } from "./useConversation";

type Tab = "chat" | "trip" | "ev";

const TABS: Array<{ id: Tab; label: string; badge?: string }> = [
  { id: "chat", label: "Chat" },
  { id: "trip", label: "Trip" },
  { id: "ev", label: "EV Planner", badge: "optional" },
];

export default function App() {
  const convo = useConversation();
  const { messages, context, busy, health, serverReachable, restoring, send: sendMessage, retry, newChat, setTripOptions, configureEv, location, locationNonce, locating, locationError, shareLocation, clearLocation, dismissLocationError, shareLocationAndRetry } = convo;
  const [tab, setTab] = useState<Tab>("chat");
  const [focus, setFocus] = useState<FocusRequest | null>(null);
  const [prefill, setPrefill] = useState<{ text: string; nonce: number } | null>(null);
  const [note, setNote] = useState<ActionOutcome | null>(null);
  const tabRefs = useRef<Record<Tab, HTMLButtonElement | null>>({ chat: null, trip: null, ev: null });

  const showOnMap = useCallback((item: PlaceItem) => setFocus({ item, nonce: Date.now() }), []);

  const send = useCallback((text: string) => {
    setNote(null);
    setTab("chat");
    void sendMessage(text);
  }, [sendMessage]);

  const aiAvailable = Boolean(health?.ai.available);
  const suggestions = useMemo(
    () => (messages.length === 0 ? [] : suggestionsFor(context, aiAvailable, messages)),
    [messages, context, aiAvailable],
  );

  const runPanelAction = useCallback(async (action: Promise<ActionOutcome | null>) => {
    setNote(null);
    const outcome = await action;
    if (outcome) setNote(outcome);
  }, []);

  const onTabKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    const move = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
    if (!move && event.key !== "Home" && event.key !== "End") return;
    event.preventDefault();
    const next = event.key === "Home" ? 0 : event.key === "End" ? TABS.length - 1 : (index + move + TABS.length) % TABS.length;
    setTab(TABS[next].id);
    tabRefs.current[TABS[next].id]?.focus();
  };

  return (
    <div className="app">
      <a className="skip-link" href="#composer-input" onClick={() => setTab("chat")}>Skip to the message box</a>

      <aside className="panel" aria-label="HerePilot-AI assistant">
        <Header health={health} serverReachable={serverReachable} onNewChat={() => { setNote(null); setFocus(null); void newChat(); }} disabled={restoring} />

        {!serverReachable && (
          <div className="banner banner-bad" role="alert">
            Can't reach the HerePilot-AI server at {BACKEND_URL}. Start the backend and try again.
          </div>
        )}

        <div className="tabs" role="tablist" aria-label="Assistant sections">
          {TABS.map((t, index) => (
            <button
              key={t.id}
              ref={(node) => { tabRefs.current[t.id] = node; }}
              id={`tab-${t.id}`}
              role="tab"
              type="button"
              aria-selected={tab === t.id}
              aria-controls={`panel-${t.id}`}
              tabIndex={tab === t.id ? 0 : -1}
              className={`tab${tab === t.id ? " tab-active" : ""}`}
              onClick={() => setTab(t.id)}
              onKeyDown={(event) => onTabKeyDown(event, index)}
            >
              {t.label}
              {t.badge && <span className="tab-badge">{t.badge}</span>}
            </button>
          ))}
        </div>

        <div id="panel-chat" role="tabpanel" aria-labelledby="tab-chat" hidden={tab !== "chat"} className="tab-panel tab-panel-chat">
          <div className="scroll">
            {messages.length === 0 && !busy ? (
              <Welcome busy={busy || restoring} onSend={send} onOpenEv={() => setTab("ev")} />
            ) : (
              <Messages messages={messages} busy={busy} onShow={showOnMap} onRetry={(id) => { void retry(id); }} onSend={send} onShareLocation={(id) => { void shareLocationAndRetry(id); }} />
            )}
          </div>
          <Composer key={prefill?.nonce ?? 0} busy={busy} disabled={restoring} suggestions={suggestions} initialText={prefill?.text} onSend={send} />
        </div>

        <div id="panel-trip" role="tabpanel" aria-labelledby="tab-trip" hidden={tab !== "trip"} className="tab-panel">
          <div className="scroll">
            <TripPanel
              context={context}
              busy={busy}
              onSend={send}
              onShow={showOnMap}
              onAddStop={() => { setTab("chat"); setPrefill({ text: "Add a stop at ", nonce: Date.now() }); }}
              onSetOptions={(options) => { void runPanelAction(setTripOptions(options)); }}
            />
            {note && <p className={`panel-note ${note.ok ? "" : "panel-note-bad"}`} role="status">{note.text}</p>}
          </div>
        </div>

        <div id="panel-ev" role="tabpanel" aria-labelledby="tab-ev" hidden={tab !== "ev"} className="tab-panel">
          <div className="scroll">
            <EvPlannerPanel context={context} busy={busy} onConfigure={(body) => { void runPanelAction(configureEv(body)); }} />
            {note && <p className={`panel-note ${note.ok ? "" : "panel-note-bad"}`} role="status">{note.text}</p>}
          </div>
        </div>
      </aside>

      <main className="map-area" aria-label="Map">
        <MapView context={context} focusRequest={focus} userLocation={location} locationNonce={locationNonce} />
        <LocateControl
          locating={locating}
          active={Boolean(location)}
          error={locationError}
          onLocate={() => { void shareLocation(); }}
          onStop={clearLocation}
          onDismissError={dismissLocationError}
        />
        {!context?.stops.length && !context?.pois.length && (
          <div className="map-hint" aria-hidden="true">Search results and routes appear here</div>
        )}
      </main>
    </div>
  );
}
