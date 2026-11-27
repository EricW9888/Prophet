import { useSyncExternalStore } from "react";

// The current time for rendering. Calling Date.now() during render gives a
// different answer on every render; this reads one shared clock instead,
// refreshed every 15 seconds while any component is subscribed.
const TICK_MS = 15_000;
const listeners = new Set<() => void>();
let now = Date.now();
let timer: number | null = null;

function tick() {
  now = Date.now();
  listeners.forEach((listener) => listener());
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  if (timer === null) {
    now = Date.now();
    timer = window.setInterval(tick, TICK_MS);
  }
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0 && timer !== null) {
      window.clearInterval(timer);
      timer = null;
    }
  };
}

const read = () => now;

export function useNow(): number {
  return useSyncExternalStore(subscribe, read, read);
}
