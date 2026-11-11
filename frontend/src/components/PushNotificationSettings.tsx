"use client";

import { Bell, BellOff, Send, Smartphone } from "lucide-react";
import { type FormEvent, useEffect, useState, useSyncExternalStore } from "react";

import { apiFetch } from "@/lib/api";
import {
  browserNotificationsSupported, disableForegroundNotifications, enableForegroundNotifications,
  foregroundDeliveryHealth, foregroundNotificationsEnabled, subscribeForegroundHealth,
  subscribeForegroundPreference, testForegroundNotification,
} from "@/lib/foreground-notifications";
import {
  currentPushSubscription,
  disablePushNotifications,
  enablePushNotifications,
  pushNotificationsSupported,
  pushSubscriptionMatches,
  PushNotificationServerStatus,
  sendTestPushNotification,
  syncPushSubscription,
} from "@/lib/push-notifications";

function permissionLabel(permission: NotificationPermission | "unsupported"): string {
  if (permission === "granted") return "Allowed on this device";
  if (permission === "denied") return "Blocked in browser settings";
  if (permission === "default") return "Not requested";
  return "Unavailable in this browser context";
}

export default function PushNotificationSettings() {
  const [status, setStatus] = useState<PushNotificationServerStatus | null>(null);
  const [subscription, setSubscription] = useState<PushSubscription | null>(null);
  const [permission, setPermission] = useState<
    NotificationPermission | "unsupported"
  >("unsupported");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [senderContact, setSenderContact] = useState("");
  const foregroundEnabled = useSyncExternalStore(subscribeForegroundPreference, foregroundNotificationsEnabled, () => false);
  const deliveryHealth = useSyncExternalStore(subscribeForegroundHealth, foregroundDeliveryHealth, foregroundDeliveryHealth);

  async function refresh() {
    const supported = browserNotificationsSupported();
    setPermission(supported ? Notification.permission : "unsupported");
    let serverStatus = await apiFetch<PushNotificationServerStatus>(
      "/notifications/status",
    );
    const currentSubscription = supported ? await currentPushSubscription() : null;
    if (
      currentSubscription &&
      Notification.permission === "granted" &&
      serverStatus.ready &&
      pushSubscriptionMatches(currentSubscription, serverStatus.application_server_key)
    ) {
      await syncPushSubscription(currentSubscription);
      serverStatus = await apiFetch<PushNotificationServerStatus>(
        "/notifications/status",
      );
    }
    setStatus(serverStatus);
    setSubscription(currentSubscription);
  }

  useEffect(() => {
    void refresh().catch((reason) => {
      setError(
        reason instanceof Error
          ? reason.message
          : "Notification status could not be loaded.",
      );
    });
  }, []);

  async function enable() {
    if (!status?.enabled) return;
    setBusy(true);
    setError(null);
    setMessage("Allow notifications in your browser’s permission prompt. Dismissing it leaves this device off.");
    try {
      if (status.ready && status.application_server_key && pushNotificationsSupported()) {
        const nextSubscription = await enablePushNotifications(status.application_server_key);
        setSubscription(nextSubscription);
        setMessage("Background delivery enabled for this device. Keep the Prophet host running.");
      } else {
        await enableForegroundNotifications();
        setMessage("Notifications enabled while Prophet is open. Background delivery can be set up below.");
      }
      setPermission(Notification.permission);
      await refresh();
    } catch (reason) {
      setPermission(
        browserNotificationsSupported() ? Notification.permission : "unsupported",
      );
      setError(
        reason instanceof Error ? reason.message : "Notifications could not be enabled.",
      );
    } finally {
      setBusy(false);
    }
  }

  async function saveSenderContact(value: string | null) {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const next = await apiFetch<PushNotificationServerStatus>("/notifications/contact", {
        method: "PUT",
        body: JSON.stringify({ sender_contact: value }),
      });
      setStatus(next);
      setSenderContact("");
      setMessage(value ? "Sender contact saved. Enable this device if needed, then send a test." : "Saved sender contact removed.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The sender contact could not be saved.");
    } finally {
      setBusy(false);
    }
  }

  function submitSenderContact(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (senderContact.trim()) void saveSenderContact(senderContact.trim());
  }

  async function disable() {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      disableForegroundNotifications();
      if (subscription) await disablePushNotifications(subscription);
      setSubscription(null);
      setMessage("Notifications are off on this device.");
      await refresh();
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : "Notifications could not be disabled.",
      );
    } finally {
      setBusy(false);
    }
  }

  async function test() {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      if (!activePush) {
        await testForegroundNotification();
        setMessage("Test notification passed to your browser.");
        return;
      }
      if (!subscription) return;
      const result = await sendTestPushNotification(subscription);
      if (result.sent) {
        setMessage("Test notification sent.");
      } else if (result.status === "retry_scheduled") {
        setMessage(
          "The push service was temporarily unavailable; Prophet scheduled a retry.",
        );
      } else if (result.status === "subscription_retired") {
        setSubscription(null);
        setError("The browser push service retired this subscription. Enable it again.");
      } else if (result.status === "configuration_error") {
        setError(
          status?.configuration_error ??
            "Owner notifications are not configured correctly on the Prophet host.",
        );
      } else {
        setError("The test notification could not be delivered after its retry limit.");
      }
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : "The test notification could not be sent.",
      );
    } finally {
      setBusy(false);
    }
  }

  const supported = permission !== "unsupported";
  const activePush = Boolean(subscription && permission === "granted" && status?.ready && pushSubscriptionMatches(subscription, status.application_server_key));
  const activeForeground = Boolean(foregroundEnabled && permission === "granted" && status?.enabled);
  const active = activePush || activeForeground;
  const canEnable = supported && permission !== "denied" && status?.enabled;
  const contactForm = <form onSubmit={submitSenderContact} className="mt-3 space-y-2">
    <label htmlFor="push-sender-contact" className="block text-sm font-medium">Sender contact</label>
    <p id="push-sender-help" className="text-xs text-muted">Use an email address or an HTTPS site origin you control. Prophet sends this contact to the browser’s push service when it delivers an alert.</p>
    <div className="flex min-w-0 flex-wrap gap-2">
      <input id="push-sender-contact" aria-describedby="push-sender-help" className="min-w-0 flex-[1_1_14rem] border border-line bg-background px-3 py-2 text-sm" type="text" value={senderContact} onChange={(event) => setSenderContact(event.target.value)} placeholder="Email or HTTPS site origin" autoCapitalize="none" spellCheck={false} maxLength={512} required />
      <button type="submit" className="workspace-control disabled:opacity-50" disabled={busy || !senderContact.trim()}>Save sender contact</button>
      {status?.sender_contact_saved && <button type="button" className="workspace-control disabled:opacity-50" disabled={busy} onClick={() => void saveSenderContact(null)}>Remove saved contact</button>}
    </div>
  </form>;

  return (
    <section id="device-notifications" className="rounded-lg border border-line bg-panel p-6">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex items-start gap-3">
          <div className="rounded-md border border-slate-200 p-2 dark:border-slate-700">
            {active ? (
              <Bell className="h-5 w-5 text-emerald-600 dark:text-emerald-400" />
            ) : (
              <BellOff className="h-5 w-5 text-slate-500" />
            )}
          </div>
          <div>
            <h2 className="text-base font-semibold text-slate-950 dark:text-slate-100">
              Device notifications
            </h2>
            <p className="mt-1 max-w-2xl text-sm text-slate-600 dark:text-slate-400">
              Get an optional browser or desktop notification when a watch or reminder changes. The lock screen shows a generic alert; details stay in Prophet.
            </p>
          </div>
        </div>
        <span
          className={`w-fit rounded-md border px-2.5 py-1 text-xs font-medium ${
            active
              ? "border-emerald-200 text-emerald-700 dark:border-emerald-900 dark:text-emerald-300"
              : "border-slate-200 text-slate-500 dark:border-slate-700 dark:text-slate-400"
          }`}
        >
          {activePush ? "Background delivery on" : activeForeground ? "On while Prophet is open" : status && !status.enabled ? "Off on host" : "Off for this device"}
        </span>
      </div>

      <div className="mt-5 grid gap-px overflow-hidden rounded-md border border-slate-200 bg-slate-200 sm:grid-cols-3 dark:border-slate-800 dark:bg-slate-800">
        <div className="bg-slate-50 p-3 dark:bg-slate-900">
          <p className="text-xs text-slate-500">Permission</p>
          <p className="mt-1 text-sm font-medium">{permissionLabel(permission)}</p>
        </div>
        <div className="bg-slate-50 p-3 dark:bg-slate-900">
          <p className="text-xs text-slate-500">This device</p>
          <p className="mt-1 text-sm font-medium">
            {activePush ? "Web Push subscribed" : activeForeground ? "Alerts while open" : "Off"}
          </p>
        </div>
        <div className="bg-slate-50 p-3 dark:bg-slate-900">
          <p className="text-xs text-slate-500">Background subscriptions</p>
          <p className="mt-1 text-sm font-medium tabular-nums">
            {status?.active_subscription_count ?? "-"}
          </p>
        </div>
      </div>

      {!supported && (
        <div className="mt-4 flex items-start gap-3 border-l-2 border-amber-400 pl-3 text-sm text-slate-600 dark:text-slate-300">
          <Smartphone className="mt-0.5 h-4 w-4 shrink-0" />
          <p>
            Notifications are unavailable in this browser context. On iPhone or iPad, add
            Prophet to the Home Screen, open the installed app over HTTPS, then enable
            notifications here.
          </p>
        </div>
      )}

      {permission === "denied" && (
        <p className="mt-4 border-l-2 border-amber-400 pl-3 text-sm text-slate-600 dark:text-slate-300">
          Re-enable Prophet in the device or browser notification settings before
          trying again.
        </p>
      )}

      <p className="mt-4 text-sm text-muted">Alerts while open need only browser permission. Closed-app delivery uses Web Push. Mobile browsers may suspend open pages, so use background delivery for alerts while away.</p>
      {activeForeground && !activePush && <p role="status" className="mt-2 text-sm text-muted">
        {deliveryHealth.state === "error" || deliveryHealth.state === "host_disabled" ? deliveryHealth.message : deliveryHealth.state === "ready" ? "Watch updates checked. The open app will keep checking." : "Checking for watch updates while Prophet is open."}
      </p>}
      {status && <details className="mt-4 text-sm">
        <summary className="cursor-pointer text-muted">Background delivery setup</summary>
        <p className="mt-2 text-muted">Install Prophet to the Home Screen on iPhone or iPad and open it over HTTPS. The Prophet host must stay running.</p>
        {!status.enabled ? <p className="mt-2 text-muted">Notifications are disabled on the Prophet host.</p> : <>
          <p className="mt-2 text-muted">{status.ready ? "Sender setup is ready." : "This host’s Web Push sender needs an email or HTTPS site origin for the push service. Alerts while open work without this setup."}</p>
          {contactForm}
          {status.configuration_error && <details className="mt-2 text-xs text-muted"><summary className="cursor-pointer">Host setup details</summary><p className="mt-2">{status.configuration_error}</p></details>}
          {!activePush && activeForeground && status.ready && pushNotificationsSupported() && <button type="button" disabled={busy} onClick={() => void enable()} className="workspace-control mt-3">Enable background delivery</button>}
        </>}
      </details>}

      {error && <p role="alert" className="mt-4 text-sm text-red-600 dark:text-red-300">{error}</p>}
      {message && (
        <p role="status" className="mt-4 text-sm text-emerald-700 dark:text-emerald-300">{message}</p>
      )}

      {(subscription || foregroundEnabled || canEnable) && <div className="mt-5 flex flex-wrap gap-2">
        {!active && canEnable ? (
          <button
            type="button"
            onClick={() => void enable()}
            disabled={busy}
            className="rounded-md bg-slate-900 px-4 py-2.5 text-sm font-medium text-white disabled:opacity-50 dark:bg-slate-100 dark:text-slate-950"
          >
            {busy ? "Working..." : "Enable on this device"}
          </button>
        ) : null}
        {active && <button
              type="button"
              onClick={() => void test()}
              disabled={busy}
              className="inline-flex items-center gap-2 rounded-md bg-slate-900 px-4 py-2.5 text-sm font-medium text-white disabled:opacity-50 dark:bg-slate-100 dark:text-slate-950"
            >
              <Send className="h-4 w-4" />
              Send test
        </button>}
        {(subscription || foregroundEnabled) && <button
              type="button"
              onClick={() => void disable()}
              disabled={busy}
              className="rounded-md border border-slate-300 px-4 py-2.5 text-sm font-medium text-slate-700 disabled:opacity-50 dark:border-slate-700 dark:text-slate-200"
            >
              Turn off
        </button>}
      </div>}
    </section>
  );
}
