"use client";

import { useMemo, useSyncExternalStore } from "react";

function preferenceStore(key: string) {
  const storageKey = `prophet:workspace:${key}`;
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
  const store = useMemo(() => preferenceStore(key), [key]);
  return { value: useSyncExternalStore(store.subscribe, store.read, () => ""), ...store };
}
