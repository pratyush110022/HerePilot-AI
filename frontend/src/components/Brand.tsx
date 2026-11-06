import type { Health } from "../types";

export function BrandMark({ size = 32 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 48 48" role="img" aria-label="HerePilot-AI logo" focusable="false">
      <rect width="48" height="48" rx="12" fill="#0b5fff" />
      <path d="M24 9c-6.1 0-11 4.7-11 10.6 0 7.7 9 17.4 10.2 18.7a1.1 1.1 0 0 0 1.6 0C26 37 35 27.3 35 19.6 35 13.7 30.1 9 24 9Z" fill="#fff" />
      <path d="m24 14.500 4.600 10.400-4.600-2.300-4.600 2.300L24 14.500Z" fill="#0b5fff" />
    </svg>
  );
}

type Props = {
  health: Health | null;
  serverReachable: boolean;
  onNewChat: () => void;
  disabled: boolean;
};

export function Header({ health, serverReachable, onNewChat, disabled }: Props) {
  const ai = health?.ai;
  return (
    <header className="brand-bar">
      <div className="brand">
        {/* HERE's own logo file, unmodified, at its native square proportions. */}
        <img className="brand-here" src="/here-logo.svg" alt="HERE" width={30} height={30} />
        <div>
          <h1 className="brand-name">HerePilot-AI</h1>
          <p className="brand-tag">Ask your map anything</p>
        </div>
      </div>
      <div className="status-row" aria-label="Service status">
        {!serverReachable ? (
          <span className="status status-bad" title="The web app can't reach the HerePilot-AI server.">Server offline</span>
        ) : health ? (
          <>
            <span
              className={`status ${health.here_configured ? "status-ok" : "status-bad"}`}
              title={health.here_configured ? "HERE search, geocoding and routing are configured on the server." : "No HERE API key is configured on the server."}
            >
              {health.here_configured ? "HERE ready" : "HERE not configured"}
            </span>
            <span
              className={`status ${ai?.available ? "status-ok" : "status-info"}`}
              title={ai?.available ? "An AI model is connected for understanding and phrasing." : "No AI model is connected. Requests are understood with built-in rules; general knowledge questions are limited."}
            >
              {ai?.available ? `AI: ${ai.provider}` : "Rules mode"}
            </span>
          </>
        ) : (
          <span className="status status-info">Connecting…</span>
        )}
        <button type="button" className="btn btn-ghost" onClick={onNewChat} disabled={disabled} title="Clear the conversation and trip">
          New chat
        </button>
      </div>
    </header>
  );
}
