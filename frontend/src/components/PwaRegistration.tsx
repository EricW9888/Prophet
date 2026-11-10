"use client";

import { createContext, type ReactNode, useContext, useEffect, useRef, useState, useSyncExternalStore } from "react";

import { syncExistingPushSubscription } from "@/lib/push-notifications";

type InstallPrompt = Event & {
  prompt: () => Promise<void>;
  userChoice: Promise<{ outcome: "accepted" | "dismissed" }>;
};
type InstallState = "idle" | "accepted" | "installed" | "failed";
const standaloneQuery = "(display-mode: standalone)";
const InstallContext = createContext({
  standalone: false,
  canInstall: false,
  state: "idle" as InstallState,
  install: async () => {},
});

function subscribeToDisplayMode(onChange: () => void) {
  const query = window.matchMedia(standaloneQuery);
  query.addEventListener("change", onChange);
  return () => query.removeEventListener("change", onChange);
}

export function useProphetInstall() {
  return useContext(InstallContext);
}

export default function PwaRegistration({ children }: { children: ReactNode }) {
  const eventRef = useRef<InstallPrompt | null>(null);
  const [canInstall, setCanInstall] = useState(false);
  const [state, setState] = useState<InstallState>("idle");
  const standalone = useSyncExternalStore(subscribeToDisplayMode, () => window.matchMedia(standaloneQuery).matches, () => false);

  useEffect(() => {
    function offerInstall(event: Event) {
      const prompt = event as InstallPrompt;
      if (typeof prompt.prompt !== "function") return;
      event.preventDefault();
      eventRef.current = prompt;
      setCanInstall(true);
    }
    function installed() {
      eventRef.current = null;
      setCanInstall(false);
      setState("installed");
    }
    window.addEventListener("beforeinstallprompt", offerInstall);
    window.addEventListener("appinstalled", installed);
    return () => {
      window.removeEventListener("beforeinstallprompt", offerInstall);
      window.removeEventListener("appinstalled", installed);
    };
  }, []);

  useEffect(() => {
    if (process.env.NODE_ENV !== "production" || !("serviceWorker" in navigator)) {
      return;
    }

    void navigator.serviceWorker
      .register("/sw.js", { scope: "/" })
      .then(() => syncExistingPushSubscription())
      .catch(() => {
        // Installability and notifications are progressive enhancements.
      });
  }, []);

  async function install() {
    const prompt = eventRef.current;
    if (!prompt) return;
    eventRef.current = null;
    setCanInstall(false);
    try {
      await prompt.prompt();
      const choice = await prompt.userChoice;
      if (choice.outcome === "accepted") setState((previous) => previous === "installed" ? previous : "accepted");
    } catch {
      setState("failed");
    }
  }

  return <InstallContext.Provider value={{ standalone, canInstall, state, install }}>{children}</InstallContext.Provider>;
}
