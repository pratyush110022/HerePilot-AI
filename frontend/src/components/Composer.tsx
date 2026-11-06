import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import type { Suggestion } from "../types";

const MAX_LENGTH = 2000;
/** The box starts three lines tall and grows with the text up to this many pixels, then scrolls. */
const MAX_BOX_PX = 240;

type Props = {
  busy: boolean;
  disabled?: boolean;
  suggestions: Suggestion[];
  /** Text to start with, e.g. "Add a stop at ". The parent remounts the composer (via `key`) to change it. */
  initialText?: string;
  onSend: (text: string) => void;
};

export default function Composer({ busy, disabled, suggestions, initialText, onSend }: Props) {
  const [value, setValue] = useState(initialText ?? "");
  const inputRef = useRef<HTMLTextAreaElement | null>(null);

  // Grow with the text (so long, multi-line questions stay visible), and shrink back after sending.
  useEffect(() => {
    const input = inputRef.current;
    if (!input) return;
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight + 2, MAX_BOX_PX)}px`;
  }, [value]);

  // Starting text (from "Add a stop…") puts the cursor at the end so the user can finish the sentence.
  useEffect(() => {
    const input = inputRef.current;
    if (!initialText || !input) return;
    input.focus();
    input.setSelectionRange(initialText.length, initialText.length);
  }, [initialText]);

  const submit = () => {
    const text = value.trim();
    if (!text || busy || disabled) return;
    setValue("");
    onSend(text);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      submit();
    }
  };

  const pick = (suggestion: Suggestion) => {
    if (suggestion.mode === "fill") {
      setValue(suggestion.text);
      inputRef.current?.focus();
    } else if (!busy && !disabled) {
      onSend(suggestion.text);
    }
  };

  return (
    <div className="composer">
      {suggestions.length > 0 && (
        <div className="chip-row" role="group" aria-label="Suggested questions">
          {suggestions.map((s) => (
            <button key={s.label} type="button" className="chip" disabled={busy || disabled} onClick={() => pick(s)}>{s.label}</button>
          ))}
        </div>
      )}
      <form
        className="composer-form"
        onSubmit={(event) => {
          event.preventDefault();
          submit();
        }}
      >
        <label htmlFor="composer-input" className="sr-only">Ask HerePilot-AI</label>
        <textarea
          id="composer-input"
          ref={inputRef}
          value={value}
          rows={3}
          maxLength={MAX_LENGTH}
          placeholder="Ask about places, routes, or anything you're curious about…"
          onChange={(event) => setValue(event.target.value)}
          onKeyDown={onKeyDown}
          disabled={disabled}
        />
        <button type="submit" className="btn btn-primary send" disabled={busy || disabled || !value.trim()} aria-label={busy ? "Sending" : "Send"}>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M22 2 11 13" /><path d="M22 2 15 22l-4-9-9-4 20-7Z" /></svg>
          <span className="send-label">{busy ? "Sending…" : "Send"}</span>
        </button>
      </form>
      <p className="composer-hint">Enter to send · Shift+Enter for a new line</p>
    </div>
  );
}
