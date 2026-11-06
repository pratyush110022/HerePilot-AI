import type { ApiErrorBody, ChatResponse, Context, Health, TravelMode, UserLocation, VehicleType } from "./types";

export const BACKEND_URL = (import.meta.env.VITE_BACKEND_URL || "http://localhost:8000").replace(/\/+$/, "");

const REQUEST_TIMEOUT_MS = 90_000;

/**
 * An error that is always safe to show. The backend already sends fixed client messages; anything
 * else (network failure, non-JSON body, unexpected shape) is replaced with wording defined here,
 * so no raw server text or exception detail ever reaches the screen.
 */
export class ApiError extends Error {
  code: string;
  retryable: boolean;
  requestId?: string;
  status: number;
  data?: { query?: string; options?: string[] };

  constructor(code: string, message: string, options: { retryable?: boolean; requestId?: string; status?: number; data?: ApiError["data"] } = {}) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.retryable = options.retryable ?? false;
    this.requestId = options.requestId;
    this.status = options.status ?? 0;
    this.data = options.data;
  }
}

const NETWORK_MESSAGE = "I can't reach the HerePilot-AI server. Check that the backend is running, then try again.";
const TIMEOUT_MESSAGE = "That took too long to answer. Please try again.";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function toApiError(status: number, body: unknown): ApiError {
  const parsed = isRecord(body) ? (body as ApiErrorBody) : {};
  const error = parsed.error;
  if (error && typeof error.message === "string" && typeof error.code === "string") {
    return new ApiError(error.code, error.message, {
      retryable: Boolean(error.retryable),
      requestId: typeof error.request_id === "string" ? error.request_id : undefined,
      status,
      data: error.data,
    });
  }
  if (status >= 500) return new ApiError("internal_error", "Something went wrong on the server. Please try again.", { retryable: true, status });
  return new ApiError("invalid_request", "That request couldn't be completed.", { status });
}

async function request<T>(path: string, init: RequestInit & { signal?: AbortSignal } = {}): Promise<T> {
  const timeout = new AbortController();
  const timer = window.setTimeout(() => timeout.abort(), REQUEST_TIMEOUT_MS);
  const outer = init.signal;
  const onOuterAbort = () => timeout.abort();
  outer?.addEventListener("abort", onOuterAbort, { once: true });
  try {
    const response = await fetch(`${BACKEND_URL}${path}`, {
      ...init,
      signal: timeout.signal,
      headers: { "Content-Type": "application/json", ...(init.headers || {}) },
    });
    let body: unknown = null;
    try {
      body = await response.json();
    } catch {
      body = null;
    }
    if (!response.ok) throw toApiError(response.status, body);
    return body as T;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (outer?.aborted) throw new DOMException("Request cancelled", "AbortError");
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new ApiError("timeout", TIMEOUT_MESSAGE, { retryable: true });
    }
    throw new ApiError("network", NETWORK_MESSAGE, { retryable: true });
  } finally {
    window.clearTimeout(timer);
    outer?.removeEventListener("abort", onOuterAbort);
  }
}

export function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

export function getHealth(signal?: AbortSignal) {
  return request<Health>("/health", { signal });
}

export function getSession(sessionId: string, signal?: AbortSignal) {
  return request<Context>(`/session/${encodeURIComponent(sessionId)}`, { signal });
}

/** Tell the server to forget the position the user shared, right now. */
export function deleteLocation(sessionId: string) {
  return request<{ forgotten: boolean }>(`/session/${encodeURIComponent(sessionId)}/location`, { method: "DELETE" });
}

export function deleteSession(sessionId: string) {
  return request<{ deleted: boolean }>(`/session/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
}

/** ``location``: a position to use, ``null`` to make the server forget it, ``undefined`` to leave it as it is. */
export function postChat(message: string, sessionId: string | null, signal?: AbortSignal, location?: UserLocation | null) {
  const body: Record<string, unknown> = { message, session_id: sessionId || null };
  if (location !== undefined) body.location = location;
  return request<ChatResponse>("/chat", { method: "POST", body: JSON.stringify(body), signal });
}

export function postTripOptions(sessionId: string | null, options: { vehicle_type?: VehicleType | null; travel_mode?: TravelMode }, signal?: AbortSignal) {
  return request<ChatResponse>("/trip/options", {
    method: "POST",
    body: JSON.stringify({ session_id: sessionId || null, ...options }),
    signal,
  });
}

export type EvConfigureBody = {
  enabled: boolean;
  vehicle?: {
    make?: string;
    model?: string;
    max_range_km?: number;
    battery_capacity_kwh?: number;
    current_battery_percent?: number;
  };
  settings?: { reserve_percent?: number; target_charge_percent?: number; corridor_km?: number };
};

export function postEvConfigure(sessionId: string | null, body: EvConfigureBody, signal?: AbortSignal) {
  return request<ChatResponse>("/ev/configure", {
    method: "POST",
    body: JSON.stringify({ session_id: sessionId || null, ...body }),
    signal,
  });
}
