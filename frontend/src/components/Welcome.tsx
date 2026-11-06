import { STARTER_SUGGESTIONS } from "../suggestions";
import { BrandMark } from "./Brand";

export default function Welcome({ busy, onSend, onOpenEv }: { busy: boolean; onSend: (text: string) => void; onOpenEv: () => void }) {
  return (
    <div className="welcome">
      <BrandMark size={36} />
      <h2 className="welcome-title">Hi, I'm HerePilot-AI</h2>
      <p className="welcome-copy">
        Ask me anything about places and travel: find somewhere nearby, plan a journey, compare destinations or get advice.
        Places and routes show up on the map.
      </p>
      <ul className="starter-list" aria-label="Try asking">
        {STARTER_SUGGESTIONS.map((s) => (
          <li key={s.label}>
            <button type="button" className="starter" disabled={busy} onClick={() => onSend(s.text)}>{s.label}</button>
          </li>
        ))}
      </ul>
      <p className="hint">
        Driving an electric vehicle? <button type="button" className="link-btn" onClick={onOpenEv}>Open the optional EV Planner</button> for charging stops.
      </p>
    </div>
  );
}
