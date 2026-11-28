"use client";

import { useMemo, useSyncExternalStore } from "react";

function preferenceStore(storageKey: string) {
  let fallback = "";
  return {
    read() {
      try { return localStorage.getItem(storageKey) ?? fallback; } catch { return fallback; }
    },
    write(value: string) {
      fallback = value;
      try { localStorage.setItem(storageKey, value); } catch { /* Keep the preference for this session. */ }
      window.dispatchEvent(new Event(storageKey));
    },
    subscribe(listener: () => void) {
      const onStorage = (event: StorageEvent) => {
        if (event.key === storageKey || event.key === null) listener();
      };
      window.addEventListener(storageKey, listener);
      window.addEventListener("storage", onStorage);
      return () => {
        window.removeEventListener(storageKey, listener);
        window.removeEventListener("storage", onStorage);
      };
    },
  };
}

export function useWorkspacePreference(key: string) {
  return useStoredPreference(`prophet:workspace:${key}`);
}

/** A browser-stored preference under an exact key; "" until it is read. */
export function useStoredPreference(storageKey: string) {
  const store = useMemo(() => preferenceStore(storageKey), [storageKey]);
  return { value: useSyncExternalStore(store.subscribe, store.read, () => ""), ...store };
}

const noSubscription = () => () => {};

/** False while rendering on the server and hydrating; true once in the browser. */
export function useInBrowser() {
  return useSyncExternalStore(noSubscription, () => true, () => false);
}
