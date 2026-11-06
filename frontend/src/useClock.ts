import { useSyncExternalStore } from "react";

/**
 * "Now" for components that show time-dependent facts (an open-now flag, the current month). One shared timer ticks
 * once a minute while anything is subscribed, so reading the time never happens as a side effect of rendering and
 * a long-open conversation does not keep claiming a place is open.
 */
let clock = Date.now();
const listeners = new Set<() => void>();
let timer: number | undefined;

function subscribe(listener: () => void): () => void {
  if (listeners.size === 0) {
    clock = Date.now();
    timer = window.setInterval(() => {
      clock = Date.now();
      listeners.forEach((notify) => notify());
    }, 60_000);
  }
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0 && timer !== undefined) {
      window.clearInterval(timer);
      timer = undefined;
    }
  };
}

const getSnapshot = () => clock;

/** Milliseconds since the epoch, refreshed once a minute. */
export function useClock(): number {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}
