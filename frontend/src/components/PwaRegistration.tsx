"use client";

import { useEffect } from "react";

import { syncExistingPushSubscription } from "@/lib/push-notifications";

export default function PwaRegistration() {
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

  return null;
}
