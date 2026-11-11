import "./helpers/dom.mjs";
import assert from "node:assert/strict";
import { afterEach, beforeEach, test } from "node:test";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import PushNotificationSettings from "../src/components/PushNotificationSettings";
import { disableForegroundNotifications, enableForegroundNotifications, foregroundDeliveryHealth, foregroundNotificationsEnabled, pollForegroundNotifications, startForegroundNotifications } from "../src/lib/foreground-notifications";

let root: Root;
let container: HTMLDivElement;
const originalFetch = globalThis.fetch;
let restore: Array<() => void>;
let permissionRequests: number;
let shown: Array<{ title: string; options: NotificationOptions }>;
let notification: { permission: NotificationPermission; requestPermission: () => Promise<NotificationPermission> };
let visibleTags: Set<string>;
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
const page = (items: unknown[] = [], next_cursor = "baseline") => ({ enabled: true, items, next_cursor, has_more: false });
const event = { title: "Watch changed", body: "Open Prophet to review.", tag: "watcher:synthetic:triggered", url: "/timeline" };
function install(object: object, key: string, value: unknown) {
  const previous = Object.getOwnPropertyDescriptor(object, key);
  Object.defineProperty(object, key, { configurable: true, value });
  restore.push(() => { if (previous) Object.defineProperty(object, key, previous); else Reflect.deleteProperty(object, key); });
}
const button = (name: string) => [...container.querySelectorAll("button")].find((item) => item.textContent === name)!;

beforeEach(() => {
  restore = [];
  shown = [];
  permissionRequests = 0;
  visibleTags = new Set();
  window.localStorage.clear();
  notification = { permission: "default", async requestPermission() { permissionRequests++; this.permission = "granted"; return this.permission; } };
  const registration = {
    async getNotifications({ tag }: { tag: string }) { return visibleTags.has(tag) ? [{}] : []; },
    async showNotification(title: string, options: NotificationOptions) { shown.push({ title, options }); visibleTags.add(options.tag!); },
  };
  install(window, "Notification", notification);
  install(globalThis, "Notification", notification);
  install(navigator, "serviceWorker", { getRegistration: async () => registration, register: async () => registration, ready: Promise.resolve(registration) });
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});
afterEach(async () => {
  await act(async () => { root.unmount(); disableForegroundNotifications(); });
  container.remove();
  for (const reset of restore.reverse()) reset();
  window.localStorage.clear();
  globalThis.fetch = originalFetch;
});

test("ordinary alerts enable without sender setup or a push subscription and can be tested and turned off", async () => {
  const requests: string[] = [];
  globalThis.fetch = async (input, options) => {
    const url = String(input);
    requests.push(url);
    assert.ok(!options?.method || options.method === "GET", "foreground opt-in must not create a push subscription");
    return response(url.endsWith("/status") ? { enabled: true, ready: false, active_subscription_count: 0 } : page());
  };
  await act(async () => root.render(<PushNotificationSettings />));
  assert.equal(permissionRequests, 0);
  assert.ok(button("Enable on this device"));
  assert.equal(container.querySelector("details")?.open, false);
  await act(async () => button("Enable on this device").click());
  assert.equal(permissionRequests, 1);
  assert.equal(foregroundNotificationsEnabled(), true);
  assert.match(container.textContent!, /On while Prophet is open/);
  assert.equal(shown.length, 0, "opt-in establishes a baseline without replaying old alerts");
  await act(async () => button("Send test").click());
  assert.equal(shown.length, 1);
  assert.equal(shown[0].options.data.url, "/settings#device-notifications");
  await act(async () => button("Turn off").click());
  assert.equal(foregroundNotificationsEnabled(), false);
  assert.equal(requests.some(url => url.includes("/subscriptions")), false);
});

test("watch polling uses the shared tag, advances after display, and preserves its cursor on failure", async () => {
  globalThis.fetch = async () => response(page());
  await enableForegroundNotifications();
  globalThis.fetch = async () => response(page([event], "after-watch"));
  assert.equal(await pollForegroundNotifications(new AbortController().signal), false);
  assert.equal(shown.length, 1);
  assert.equal(shown[0].options.tag, event.tag);
  assert.equal(foregroundDeliveryHealth().state, "ready");
  await pollForegroundNotifications(new AbortController().signal);
  assert.equal(shown.length, 1, "an existing push or foreground notification must not be shown again");
  globalThis.fetch = async (input) => {
    assert.equal(new URL(String(input), "http://localhost").searchParams.get("cursor"), "after-watch");
    return response({ detail: "Synthetic host outage" }, 503);
  };
  await assert.rejects(pollForegroundNotifications(new AbortController().signal), /Synthetic host outage/);
  globalThis.fetch = async (input) => {
    assert.equal(new URL(String(input), "http://localhost").searchParams.get("cursor"), "after-watch");
    return response(page([], "after-watch"));
  };
  await pollForegroundNotifications(new AbortController().signal);
});

test("turning off stops a pending poll and cannot show a late alert", async () => {
  globalThis.fetch = async () => response(page());
  await enableForegroundNotifications();
  let release!: (value: Response) => void;
  const pending = new Promise<Response>(resolve => { release = resolve; });
  globalThis.fetch = async () => pending;
  const stop = startForegroundNotifications();
  disableForegroundNotifications();
  stop();
  release(response(page([event], "too-late")));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(shown.length, 0);
  assert.equal(foregroundNotificationsEnabled(), false);
  assert.equal(foregroundDeliveryHealth().state, "idle");
});

test("denied permission never arms alerts or fetches a baseline", async () => {
  notification.requestPermission = async () => { permissionRequests++; notification.permission = "denied"; return "denied"; };
  let requests = 0;
  globalThis.fetch = async () => { requests++; return response(page()); };
  await assert.rejects(enableForegroundNotifications(), /blocked in browser settings/);
  assert.equal(foregroundNotificationsEnabled(), false);
  assert.equal(requests, 0);
  assert.equal(shown.length, 0);
});
