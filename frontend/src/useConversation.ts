import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  deleteSession,
  getHealth,
  getSession,
  isAbort,
  postChat,
  postEvConfigure,
  postTripOptions,
  deleteLocation,
  type EvConfigureBody,
} from "./api";
import { buildCards } from "./cards";
import { newId } from "./format";
import { clearState, loadState, saveState } from "./storage";
import type { ChatMessage, ChatResponse, Context, Health, MessageError, TravelMode, UserLocation, VehicleType } from "./types";

const HEALTH_POLL_MS = 30_000;

function errorMessage(error: ApiError, retryText?: string): ChatMessage {
  const options = error.code === "place_ambiguous" && retryText && error.data?.query && error.data.options
    ? error.data.options.slice(0, 4).map((label) => ({
        label,
        // Re-ask the same thing with the chosen, fully qualified place name instead of the ambiguous one.
        text: replaceFirst(retryText, error.data!.query!, label),
      }))
    : undefined;
  const info: MessageError = {
    code: error.code,
    retryable: error.retryable && Boolean(retryText),
    requestId: error.requestId,
    retryText,
    options,
  };
  return { id: newId(), role: "assistant", text: error.message, error: info };
}

function replaceFirst(text: string, needle: string, replacement: string): string {
  const index = text.toLowerCase().indexOf(needle.toLowerCase());
  if (index < 0) return `${text} (${replacement})`;
  return text.slice(0, index) + replacement + text.slice(index + needle.length);
}

/** What a panel action (trip options, EV Planner) reports back so the panel can show it in place. */
export type ActionOutcome = { ok: boolean; text: string };

export type Conversation = {
  messages: ChatMessage[];
  context: Context | null;
  busy: boolean;
  restoring: boolean;
  health: Health | null;
  serverReachable: boolean;
  send: (text: string) => Promise<void>;
  retry: (messageId: string) => Promise<void>;
  /** The user's opt-in location. Held in memory only; sent to the server with chat messages while shared. */
  location: UserLocation | null;
  /** Bumps each time a fresh position is read, so the map can centre on it even when the position is unchanged. */
  locationNonce: number;
  locating: boolean;
  locationError: string | null;
  shareLocation: () => Promise<boolean>;
  clearLocation: () => void;
  dismissLocationError: () => void;
  shareLocationAndRetry: (messageId: string) => Promise<void>;
  newChat: () => Promise<void>;
  setTripOptions: (options: { vehicle_type?: VehicleType | null; travel_mode?: TravelMode }) => Promise<ActionOutcome | null>;
  configureEv: (body: EvConfigureBody) => Promise<ActionOutcome | null>;
};

export function useConversation(): Conversation {
  const [initial] = useState(loadState);
  const [messages, setMessages] = useState<ChatMessage[]>(initial.messages);
  const [sessionId, setSessionId] = useState<string | null>(initial.sessionId);
  const [context, setContext] = useState<Context | null>(null);
  const [busy, setBusy] = useState(false);
  const [restoring, setRestoring] = useState(Boolean(initial.sessionId));
  const [health, setHealth] = useState<Health | null>(null);
  const [serverReachable, setServerReachable] = useState(true);

  const sessionRef = useRef<string | null>(initial.sessionId);
  const locationRef = useRef<UserLocation | null>(null);
  const forgetLocationRef = useRef(false);
  const [location, setLocation] = useState<UserLocation | null>(null);
  const [locationNonce, setLocationNonce] = useState(0);
  const [locating, setLocating] = useState(false);
  const [locationError, setLocationError] = useState<string | null>(null);
  const busyRef = useRef(false);
  const abortRef = useRef<AbortController | null>(null);
  const messagesRef = useRef(messages);

  useEffect(() => {
    messagesRef.current = messages;
  }, [messages]);

  useEffect(() => {
    saveState({ sessionId, messages });
  }, [sessionId, messages]);

  const push = useCallback((...added: ChatMessage[]) => {
    setMessages((previous) => [...previous, ...added]);
  }, []);

  // --- server health (drives the status chips and the offline banner) -------------------------
  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    const check = async () => {
      try {
        const value = await getHealth(controller.signal);
        if (!cancelled) {
          setHealth(value);
          setServerReachable(true);
        }
      } catch (error) {
        if (!cancelled && !isAbort(error)) setServerReachable(false);
      }
    };
    void check();
    const timer = window.setInterval(check, HEALTH_POLL_MS);
    return () => {
      cancelled = true;
      controller.abort();
      window.clearInterval(timer);
    };
  }, []);

  // --- restore a saved session once on load ---------------------------------------------------
  useEffect(() => {
    const saved = sessionRef.current;
    if (!saved) return;
    const controller = new AbortController();
    (async () => {
      try {
        const restored = await getSession(saved, controller.signal);
        setContext(restored);
      } catch (error) {
        if (isAbort(error)) return;
        if (error instanceof ApiError && (error.code === "network" || error.code === "timeout")) {
          // Keep the session id: the server may simply not be running yet.
          return;
        }
        sessionRef.current = null;
        setSessionId(null);
        setMessages((previous) =>
          previous.length
            ? [...previous, { id: newId(), role: "assistant", notice: true, text: "Your previous trip session has expired, so the trip and map results were cleared. Your earlier messages are kept above." }]
            : previous,
        );
      } finally {
        if (!controller.signal.aborted) setRestoring(false);
      }
    })();
    return () => controller.abort();
  }, []);

  const adopt = useCallback((response: ChatResponse, sentSession: string | null) => {
    if (sentSession && response.session_id !== sentSession) {
      push({ id: newId(), role: "assistant", notice: true, text: "The server started a new session (it may have restarted), so earlier trip details were reset." });
    }
    sessionRef.current = response.session_id;
    setSessionId(response.session_id);
    setContext(response.context);
  }, [push]);

  const run = useCallback(async (work: (signal: AbortSignal, sent: string | null) => Promise<ChatResponse>, failure: (error: ApiError) => ChatMessage, userText?: string): Promise<ActionOutcome | null> => {
    if (busyRef.current) return null;
    busyRef.current = true;
    setBusy(true);
    const controller = new AbortController();
    abortRef.current = controller;
    const sent = sessionRef.current;
    try {
      const response = await work(controller.signal, sent);
      adopt(response, sent);
      setServerReachable(true);
      push({
        id: newId(), role: "assistant", text: response.reply, cards: buildCards(response.result, response.context),
        askLocation: response.result.needs_location && userText ? { retryText: userText } : undefined,
      });
      return { ok: true, text: response.reply };
    } catch (error) {
      if (isAbort(error)) return null;
      const apiError = error instanceof ApiError ? error : new ApiError("internal_error", "Something went wrong. Please try again.", { retryable: true });
      if (apiError.code === "network") setServerReachable(false);
      const failed = failure(apiError);
      push(failed);
      return { ok: false, text: failed.text };
    } finally {
      // A cancelled request (New chat) must not clear the flag of a request started after it.
      if (abortRef.current === controller) {
        busyRef.current = false;
        abortRef.current = null;
        setBusy(false);
      }
    }
  }, [adopt, push]);

  const send = useCallback(async (raw: string) => {
    const text = raw.trim();
    if (!text || busyRef.current) return;
    push({ id: newId(), role: "user", text });
    await run((signal, sent) => {
      const location = locationRef.current ?? (forgetLocationRef.current ? null : undefined);
      const request = postChat(text, sent, signal, location);
      if (location === null) request.then(() => { forgetLocationRef.current = false; }, () => undefined);
      return request;
    }, (error) => errorMessage(error, text), text);
  }, [push, run]);

  const shareLocation = useCallback((): Promise<boolean> => new Promise((resolve) => {
    if (typeof navigator === "undefined" || !("geolocation" in navigator)) {
      setLocationError("This browser can't share a location. You can still name a place.");
      resolve(false);
      return;
    }
    if (!window.isSecureContext) {
      setLocationError("Your browser only shares a location on a secure (https or localhost) page. You can still name a place.");
      resolve(false);
      return;
    }
    setLocationError(null);
    setLocating(true);
    // The exact GPS fix, never a cached one: the user clicked to be located now.
    navigator.geolocation.getCurrentPosition(
      (position) => {
        const here: UserLocation = {
          latitude: position.coords.latitude,
          longitude: position.coords.longitude,
          accuracy_m: Math.round(position.coords.accuracy),
        };
        locationRef.current = here;
        forgetLocationRef.current = false;
        setLocation(here);
        setLocationNonce((n) => n + 1);
        setLocating(false);
        resolve(true);
      },
      (error) => {
        setLocating(false);
        setLocationError(
          error.code === error.PERMISSION_DENIED
            ? "Location access is blocked for this site. Allow it in your browser's site settings, then try again."
            : error.code === error.TIMEOUT
              ? "Locating you took too long. Check that GPS or Wi-Fi location is on, then try again."
              : "Your device couldn't work out where you are. Check that location is turned on, then try again.",
        );
        resolve(false);
      },
      { enableHighAccuracy: true, timeout: 15_000, maximumAge: 0 },
    );
  }), []);

  const clearLocation = useCallback(() => {
    locationRef.current = null;
    forgetLocationRef.current = true; // belt and braces: the next message also tells the server to forget it
    setLocation(null);
    setLocating(false);
    setLocationError(null);
    const session = sessionRef.current;
    if (session) deleteLocation(session).catch(() => undefined); // the server drops it now, not at the next message
  }, []);

  const dismissLocationError = useCallback(() => setLocationError(null), []);

  const shareLocationAndRetry = useCallback(async (messageId: string) => {
    const text = messagesRef.current.find((m) => m.id === messageId)?.askLocation?.retryText;
    if (!text || busyRef.current) return;
    if (await shareLocation()) await send(text);
  }, [send, shareLocation]);

  const retry = useCallback(async (messageId: string) => {
    const target = messagesRef.current.find((m) => m.id === messageId);
    const text = target?.error?.retryText;
    if (!text || busyRef.current) return;
    // Drop the failed attempt (the error and the user message just before it) so the transcript stays clean.
    setMessages((previous) => {
      const index = previous.findIndex((m) => m.id === messageId);
      if (index < 0) return previous;
      const before = previous[index - 1];
      const dropFrom = before && before.role === "user" && before.text === text ? index - 1 : index;
      return [...previous.slice(0, dropFrom), ...previous.slice(index + 1)];
    });
    await send(text);
  }, [send]);

  const newChat = useCallback(async () => {
    abortRef.current?.abort();
    abortRef.current = null;
    const previous = sessionRef.current;
    sessionRef.current = null;
    busyRef.current = false;
    setBusy(false);
    setSessionId(null);
    setContext(null);
    setMessages([]);
    clearState();
    if (previous) {
      try {
        await deleteSession(previous);
      } catch {
        // Best effort: an expired or unreachable session needs no cleanup.
      }
    }
  }, []);

  const setTripOptions = useCallback((options: { vehicle_type?: VehicleType | null; travel_mode?: TravelMode }) => {
    return run((signal, sent) => postTripOptions(sent, options, signal), (error) => errorMessage(error));
  }, [run]);

  const configureEv = useCallback((body: EvConfigureBody) => {
    return run((signal, sent) => postEvConfigure(sent, body, signal), (error) => errorMessage(error));
  }, [run]);

  return {
    messages, context, busy, restoring, health, serverReachable, send, retry, newChat, setTripOptions, configureEv,
    location, locationNonce, locating, locationError, shareLocation, clearLocation, dismissLocationError, shareLocationAndRetry,
  };
}
