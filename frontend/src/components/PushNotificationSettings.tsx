"use client";

import { Bell, BellOff, Send, Smartphone } from "lucide-react";
import { useEffect, useState } from "react";

import { apiFetch } from "@/lib/api";
import {
  currentPushSubscription,
  disablePushNotifications,
  enablePushNotifications,
  pushNotificationsSupported,
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

  async function refresh() {
    const supported = pushNotificationsSupported();
    setPermission(supported ? Notification.permission : "unsupported");
    let serverStatus = await apiFetch<PushNotificationServerStatus>(
      "/notifications/status",
    );
    const currentSubscription = supported ? await currentPushSubscription() : null;
    if (
      currentSubscription &&
      Notification.permission === "granted" &&
      serverStatus.ready
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
    if (!status?.application_server_key) return;
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const nextSubscription = await enablePushNotifications(
        status.application_server_key,
      );
      setSubscription(nextSubscription);
      setPermission(Notification.permission);
      setMessage("This device will receive material watch and reminder alerts.");
      await refresh();
    } catch (reason) {
      setPermission(
        pushNotificationsSupported() ? Notification.permission : "unsupported",
      );
      setError(
        reason instanceof Error ? reason.message : "Notifications could not be enabled.",
      );
    } finally {
      setBusy(false);
    }
  }

  async function disable() {
    if (!subscription) return;
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      await disablePushNotifications(subscription);
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
    if (!subscription) return;
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
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
  const active = Boolean(subscription && permission === "granted");

  return (
    <section className="rounded-lg border border-slate-200 bg-white p-6 dark:border-slate-800 dark:bg-slate-950">
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
              Owner notifications
            </h2>
            <p className="mt-1 max-w-2xl text-sm text-slate-600 dark:text-slate-400">
              Receive an advisory alert when a durable watch or reminder changes state.
              Lock-screen messages stay generic; open Prophet to review the evidence and
              adjustment plan.
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
          {active ? "On for this device" : "Off for this device"}
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
            {subscription ? "Subscribed" : "Not subscribed"}
          </p>
        </div>
        <div className="bg-slate-50 p-3 dark:bg-slate-900">
          <p className="text-xs text-slate-500">Active devices</p>
          <p className="mt-1 text-sm font-medium tabular-nums">
            {status?.active_subscription_count ?? "-"}
          </p>
        </div>
      </div>

      {!supported && (
        <div className="mt-4 flex items-start gap-3 border-l-2 border-amber-400 pl-3 text-sm text-slate-600 dark:text-slate-300">
          <Smartphone className="mt-0.5 h-4 w-4 shrink-0" />
          <p>
            Push is unavailable in this browser context. On iPhone or iPad, add
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

      {status?.configuration_error && (
        <p className="mt-4 border-l-2 border-red-500 pl-3 text-sm text-red-600 dark:text-red-300">
          {status.configuration_error}
        </p>
      )}

      {error && <p className="mt-4 text-sm text-red-600 dark:text-red-300">{error}</p>}
      {message && (
        <p className="mt-4 text-sm text-emerald-700 dark:text-emerald-300">{message}</p>
      )}

      <div className="mt-5 flex flex-wrap gap-2">
        {!active ? (
          <button
            type="button"
            onClick={() => void enable()}
            disabled={
              busy ||
              !supported ||
              permission === "denied" ||
              !status?.enabled ||
              !status.ready ||
              !status.application_server_key
            }
            className="rounded-md bg-slate-900 px-4 py-2.5 text-sm font-medium text-white disabled:opacity-50 dark:bg-slate-100 dark:text-slate-950"
          >
            {busy ? "Working..." : "Enable on this device"}
          </button>
        ) : (
          <>
            <button
              type="button"
              onClick={() => void test()}
              disabled={busy}
              className="inline-flex items-center gap-2 rounded-md bg-slate-900 px-4 py-2.5 text-sm font-medium text-white disabled:opacity-50 dark:bg-slate-100 dark:text-slate-950"
            >
              <Send className="h-4 w-4" />
              Send test
            </button>
            <button
              type="button"
              onClick={() => void disable()}
              disabled={busy}
              className="rounded-md border border-slate-300 px-4 py-2.5 text-sm font-medium text-slate-700 disabled:opacity-50 dark:border-slate-700 dark:text-slate-200"
            >
              Turn off
            </button>
          </>
        )}
      </div>
    </section>
  );
}
