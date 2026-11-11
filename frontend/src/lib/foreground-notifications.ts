import { apiFetch } from "./api";
import { registerProphetServiceWorker } from "./push-notifications";

const ENABLED_KEY = "prophet:foreground-notifications";
const CURSOR_KEY = "prophet:notification-cursor";
const CHANGE_EVENT = "prophet:notification-preference";
const HEALTH_EVENT = "prophet:notification-delivery";
const POLL_MS = 15_000;

type EventPage = {
  enabled: boolean;
  items: Array<{ title: string; body: string; url: string; tag: string }>;
  next_cursor: string | null;
  has_more: boolean;
};
type DeliveryHealth = { state: "idle" | "checking" | "ready" | "error" | "host_disabled"; message: string | null };
let health: DeliveryHealth = { state: "idle", message: null };

export function browserNotificationsSupported(): boolean {
  return typeof window !== "undefined" && "Notification" in window && "serviceWorker" in navigator;
}

export function foregroundNotificationsEnabled(): boolean {
  try { return window.localStorage.getItem(ENABLED_KEY) === "true"; } catch { return false; }
}

export function subscribeForegroundPreference(callback: () => void) {
  window.addEventListener("storage", callback);
  window.addEventListener(CHANGE_EVENT, callback);
  return () => { window.removeEventListener("storage", callback); window.removeEventListener(CHANGE_EVENT, callback); };
}

export function foregroundDeliveryHealth() { return health; }
export function subscribeForegroundHealth(callback: () => void) {
  window.addEventListener(HEALTH_EVENT, callback);
  return () => window.removeEventListener(HEALTH_EVENT, callback);
}
function report(state: DeliveryHealth["state"], message: string | null = null) {
  health = { state, message };
  window.dispatchEvent(new Event(HEALTH_EVENT));
}

export async function enableForegroundNotifications(): Promise<void> {
  if (!browserNotificationsSupported()) throw new Error("Notifications are unavailable in this browser context.");
  // Permission must be requested directly from the user's button click.
  const permission = await Notification.requestPermission();
  if (permission !== "granted") throw new Error(permission === "denied" ? "Notifications are blocked in browser settings." : "Notification permission was not granted.");
  await registerProphetServiceWorker();
  const baseline = await apiFetch<EventPage>("/notifications/events");
  if (!baseline.enabled || !baseline.next_cursor) throw new Error("Notifications are disabled on the Prophet host.");
  window.localStorage.setItem(CURSOR_KEY, baseline.next_cursor);
  window.localStorage.setItem(ENABLED_KEY, "true");
  window.dispatchEvent(new Event(CHANGE_EVENT));
}

export function disableForegroundNotifications() {
  window.localStorage.removeItem(ENABLED_KEY);
  window.localStorage.removeItem(CURSOR_KEY);
  window.dispatchEvent(new Event(CHANGE_EVENT));
  report("idle");
}

export async function testForegroundNotification() {
  if (!foregroundNotificationsEnabled() || Notification.permission !== "granted") throw new Error("Enable notifications on this device first.");
  const registration = await registerProphetServiceWorker();
  await registration.showNotification("Prophet notifications are ready", {
    body: "This device can show alerts while Prophet is open.", icon: "/icon-192.png", tag: "prophet-device-test", data: { url: "/settings#device-notifications" },
  });
}

export async function pollForegroundNotifications(signal: AbortSignal): Promise<boolean> {
  const active = () => !signal.aborted && foregroundNotificationsEnabled() && Notification.permission === "granted";
  if (signal.aborted || !foregroundNotificationsEnabled()) return false;
  if (Notification.permission !== "granted") { report("error", "Notifications are blocked or permission is no longer granted. Check browser settings."); return false; }
  const cursor = window.localStorage.getItem(CURSOR_KEY);
  const page = await apiFetch<EventPage>(`/notifications/events${cursor ? `?${new URLSearchParams({ cursor })}` : ""}`, { signal });
  if (!active()) return false;
  if (!page.enabled) { report("host_disabled", "Notifications are disabled on the Prophet host."); return false; }
  const registration = await registerProphetServiceWorker();
  for (const item of page.items) {
    if (!active()) return false;
    // The same tags are used by Web Push. Do not alert twice for a notification
    // already visible, including when another Prophet tab is open.
    const existing = await registration.getNotifications({ tag: item.tag });
    if (!active()) return false;
    if (!existing.length) {
      const target = new URL(item.url, window.location.origin);
      if (target.origin !== window.location.origin) throw new Error("Notification destination must stay inside Prophet.");
      await registration.showNotification(item.title, {
        body: item.body, icon: "/icon-192.png", badge: "/icon-192.png", tag: item.tag,
        data: { url: target.pathname + target.search + target.hash },
      });
    }
  }
  if (!active()) return false;
  if (page.next_cursor) window.localStorage.setItem(CURSOR_KEY, page.next_cursor);
  report("ready");
  return page.has_more;
}

export function startForegroundNotifications(): () => void {
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  async function tick() {
    if (controller.signal.aborted) return;
    report("checking");
    let more = false;
    try {
      const poll = () => pollForegroundNotifications(controller.signal);
      // Standard browser coordination: one active poll across same-origin tabs.
      more = "locks" in navigator
        ? await navigator.locks.request("prophet-notification-poll", { signal: controller.signal }, poll)
        : await poll();
    } catch (error) {
      if (!controller.signal.aborted) report("error", error instanceof Error ? error.message : "Alerts could not be checked. Open Monitor to review watch updates.");
    }
    if (!controller.signal.aborted) timer = setTimeout(() => void tick(), more ? 0 : POLL_MS);
  }
  void tick();
  return () => { controller.abort(); clearTimeout(timer); };
}
