import { useEffect, useRef } from "react";
import type { ChatMessage, PlaceItem } from "../types";
import { CardView } from "./Cards";

type Props = {
  messages: ChatMessage[];
  busy: boolean;
  onShow: (item: PlaceItem) => void;
  onRetry: (messageId: string) => void;
  onSend: (text: string) => void;
  onShareLocation: (messageId: string) => void;
};

function MessageItem({ message, busy, isLast, onShow, onRetry, onSend, onShareLocation }: { message: ChatMessage; busy: boolean; isLast: boolean } & Omit<Props, "messages" | "busy">) {
  // Retry and "which one did you mean" choices only make sense for the latest message.
  const error = message.error;
  return (
    <li className={`msg msg-${message.role}${error ? " msg-error" : ""}${message.notice ? " msg-notice" : ""}`}>
      <div className="msg-who">{message.role === "user" ? "You" : "HerePilot-AI"}</div>
      <div className={`bubble${error ? " bubble-error" : ""}`}>{message.text}</div>
      {message.cards?.map((card, i) => <CardView key={i} card={card} onShow={onShow} onSend={onSend} />)}
      {message.askLocation && isLast && (
        <div className="msg-actions">
          <button type="button" className="btn btn-secondary" disabled={busy} onClick={() => onShareLocation(message.id)}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" aria-hidden="true"><circle cx="12" cy="12" r="6.500" /><path d="M12 2v3M12 19v3M2 12h3M19 12h3" /></svg>
            Share my location
          </button>
        </div>
      )}
      {error && isLast && (
        <div className="msg-actions">
          {error.options?.map((option) => (
            <button key={option.label} type="button" className="chip" disabled={busy} onClick={() => onSend(option.text)}>{option.label}</button>
          ))}
          {error.retryable && (
            <button type="button" className="btn btn-secondary" disabled={busy} onClick={() => onRetry(message.id)}>Try again</button>
          )}
          {error.requestId && <span className="msg-ref">Reference {error.requestId}</span>}
        </div>
      )}
    </li>
  );
}

export default function Messages({ messages, busy, onShow, onRetry, onSend, onShareLocation }: Props) {
  const endRef = useRef<HTMLLIElement | null>(null);

  useEffect(() => {
    const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    endRef.current?.scrollIntoView({ block: "end", behavior: reduce ? "auto" : "smooth" });
  }, [messages.length, busy]);

  return (
    <ol className="messages" role="log" aria-live="polite" aria-relevant="additions" aria-label="Conversation">
      {messages.map((message, index) => (
        <MessageItem key={message.id} message={message} busy={busy} isLast={index === messages.length - 1} onShow={onShow} onRetry={onRetry} onSend={onSend} onShareLocation={onShareLocation} />
      ))}
      {busy && (
        <li className="msg msg-assistant" role="status">
          <div className="msg-who">HerePilot-AI</div>
          <div className="bubble bubble-thinking"><span className="dots" aria-hidden="true"><i /><i /><i /></span> Working on it…</div>
        </li>
      )}
      <li ref={endRef} aria-hidden="true" className="messages-end" />
    </ol>
  );
}
