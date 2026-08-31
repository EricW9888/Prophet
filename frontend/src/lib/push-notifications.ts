import { apiFetch } from "@/lib/api";

export type PushNotificationServerStatus = {
  enabled: boolean;
  ready: boolean;
  configuration_error?: string | null;
  application_server_key?: string | null;
  active_subscription_count: number;
};

function applicationServerKey(value: string): Uint8Array<ArrayBuffer> {
  const padding = "=".repeat((4 - (value.length % 4)) % 4);
  const decoded = window.atob((value + padding).replace(/-/g, "+").replace(/_/g, "/"));
  const bytes = Uint8Array.from(decoded, character => character.charCodeAt(0));
  return new Uint8Array(bytes.buffer);
}

function keysMatch(
  current: ArrayBuffer | null,
  expected: Uint8Array<ArrayBuffer>,
): boolean {
  if (!current) return false;
  const currentBytes = new Uint8Array(current);
  return (
    currentBytes.length === expected.length &&
    currentBytes.every((byte, index) => byte === expected[index])
  );
}

export function pushNotificationsSupported(): boolean {
  return (
    typeof window !== "undefined" &&
    "serviceWorker" in navigator &&
    "PushManager" in window &&
    "Notification" in window
  );
}

export async function registerProphetServiceWorker(): Promise<ServiceWorkerRegistration> {
  if (!("serviceWorker" in navigator)) {
    throw new Error("This browser does not support service workers.");
  }
  await navigator.serviceWorker.register("/sw.js", { scope: "/" });
  return navigator.serviceWorker.ready;
}

export async function syncPushSubscription(subscription: PushSubscription): Promise<void> {
  const serialized = subscription.toJSON();
  if (!serialized.endpoint || !serialized.keys?.p256dh || !serialized.keys?.auth) {
    throw new Error("The browser returned an incomplete push subscription.");
  }
  await apiFetch("/notifications/subscriptions", {
    method: "POST",
    body: JSON.stringify({
      endpoint: serialized.endpoint,
      keys: {
        p256dh: serialized.keys.p256dh,
        auth: serialized.keys.auth,
      },
    }),
  });
}

export async function enablePushNotifications(
  publicKey: string,
): Promise<PushSubscription> {
  if (!pushNotificationsSupported()) {
    throw new Error("Push notifications are not available in this browser context.");
  }
  const permission = await Notification.requestPermission();
  if (permission !== "granted") {
    throw new Error(
      permission === "denied"
        ? "Notifications are blocked in browser settings."
        : "Notification permission was not granted.",
    );
  }
  const registration = await registerProphetServiceWorker();
  const key = applicationServerKey(publicKey);
  let existing = await registration.pushManager.getSubscription();
  if (existing && !keysMatch(existing.options.applicationServerKey, key)) {
    await disablePushNotifications(existing);
    existing = null;
  }
  const subscription =
    existing ??
    (await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: key,
    }));
  await syncPushSubscription(subscription);
  return subscription;
}

export async function currentPushSubscription(): Promise<PushSubscription | null> {
  if (!pushNotificationsSupported()) return null;
  const registration = await navigator.serviceWorker.getRegistration("/");
  return registration?.pushManager.getSubscription() ?? null;
}

export async function disablePushNotifications(
  subscription: PushSubscription,
): Promise<void> {
  await apiFetch("/notifications/subscriptions/remove", {
    method: "POST",
    body: JSON.stringify({ endpoint: subscription.endpoint }),
  });
  await subscription.unsubscribe();
}

export async function sendTestPushNotification(
  subscription: PushSubscription,
): Promise<{ status: string; sent: boolean }> {
  return apiFetch("/notifications/test", {
    method: "POST",
    body: JSON.stringify({ endpoint: subscription.endpoint }),
  });
}

export async function syncExistingPushSubscription(): Promise<void> {
  if (!pushNotificationsSupported() || Notification.permission !== "granted") return;
  const subscription = await currentPushSubscription();
  if (subscription) await syncPushSubscription(subscription);
}
