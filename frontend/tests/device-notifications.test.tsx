import "./helpers/dom.mjs";
import assert from "node:assert/strict";
import { test } from "node:test";
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import PushNotificationSettings from "../src/components/PushNotificationSettings";

test("saving sender setup does not ask for device permission or subscribe", async () => {
  const originalFetch = globalThis.fetch;
  const mutations: string[] = [];
  let permissionsRequested = 0;
  let configured = false;
  let subscription: { options: { applicationServerKey: ArrayBuffer }; toJSON: () => object } | null = null;
  const notification = {
    permission: "default",
    async requestPermission() { permissionsRequested++; this.permission = "granted"; return this.permission; },
  };
  const registration = { pushManager: {
    async getSubscription() { return subscription; },
    async subscribe(options: { applicationServerKey: Uint8Array<ArrayBuffer> }) {
      subscription = { options: { applicationServerKey: options.applicationServerKey.buffer },
        toJSON: () => ({ endpoint: "https://push.example.test/one", keys: { p256dh: "fixture", auth: "fixture" } }) };
      return subscription;
    },
  } };
  const install = (object: object, key: string, value: unknown) => {
    const previous = Object.getOwnPropertyDescriptor(object, key);
    Object.defineProperty(object, key, { configurable: true, value });
    return () => { if (previous) Object.defineProperty(object, key, previous); else Reflect.deleteProperty(object, key); };
  };
  const restore = [install(window, "PushManager", {}), install(window, "Notification", notification), install(globalThis, "Notification", notification),
    install(navigator, "serviceWorker", { getRegistration: async () => registration, register: async () => registration, ready: Promise.resolve(registration) })];
  globalThis.fetch = async (input, options) => {
    const url = String(input);
    if (options?.method && options.method !== "GET") mutations.push(url);
    if (url.endsWith("/contact")) {
      assert.deepEqual(JSON.parse(String(options?.body)), { sender_contact: "fixture@example.test" });
      configured = true;
    }
    const data = url.endsWith("/test") ? { status: "sent", sent: true } : url.endsWith("/subscriptions") ? {} : {
      enabled: true, ready: configured, sender_contact_saved: configured,
      active_subscription_count: subscription ? 1 : 0, application_server_key: configured ? "AQID" : null,
    };
    return new Response(JSON.stringify(data), { status: 200, headers: { "Content-Type": "application/json" } });
  };
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const button = (name: string) => [...container.querySelectorAll("button")].find((item) => item.textContent === name);
  try {
    await act(async () => root.render(<PushNotificationSettings />));
    assert.ok(button("Enable on this device"));
    assert.equal(permissionsRequested, 0);
    const contact = container.querySelector("input")!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")!.set!.call(contact, "fixture@example.test");
      contact.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => button("Save sender contact")!.click());
    assert.equal(permissionsRequested, 0);
    assert.deepEqual(mutations, ["/api_proxy/notifications/contact"]);

    await act(async () => button("Enable on this device")!.click());
    assert.equal(permissionsRequested, 1);
    assert.ok(mutations.includes("/api_proxy/notifications/subscriptions"));
    await act(async () => button("Send test")!.click());
    assert.match(container.textContent!, /Test notification sent\./);
  } finally {
    await act(async () => root.unmount());
    container.remove();
    for (const reset of restore.reverse()) reset();
    globalThis.fetch = originalFetch;
  }
});
