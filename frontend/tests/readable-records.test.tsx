import "./helpers/dom.mjs";
import assert from "node:assert/strict";
import { test } from "node:test";
import React, { act, useEffect } from "react";
import { createRoot } from "react-dom/client";
import RecordFields from "../src/components/RecordFields";
import DeferredDisclosure from "../src/components/DeferredDisclosure";

async function toggle(details: HTMLDetailsElement, open: boolean) {
  await act(async () => { details.open = open; details.dispatchEvent(new Event("toggle")); });
}

test("secondary content waits for opening and keeps entered state when closed", async () => {
  let mounts = 0; let unmounts = 0;
  function Detail() {
    useEffect(() => { mounts++; return () => { unmounts++; }; }, []);
    return <input aria-label="Research draft" defaultValue="Initial draft" />;
  }
  const host = document.createElement("div"); document.body.append(host);
  const root = createRoot(host);
  try {
    await act(async () => root.render(<DeferredDisclosure title="Research details"><Detail /></DeferredDisclosure>));
    assert.equal(mounts, 0);
    assert.equal(host.querySelector("input"), null);
    const details = host.querySelector("details")!;
    await toggle(details, true);
    assert.equal(mounts, 1);
    host.querySelector("input")!.value = "Owner's unsent draft";
    await toggle(details, false);
    assert.equal(unmounts, 0);
    await toggle(details, true);
    assert.equal(mounts, 1);
    assert.equal(host.querySelector("input")!.value, "Owner's unsent draft");
  } finally { await act(async () => root.unmount()); host.remove(); }
  assert.equal(unmounts, 1);
});

test("record display retains exact identifiers, multiline text, zero, false and timestamp identity", async () => {
  const host = document.createElement("div"); document.body.append(host);
  const root = createRoot(host);
  const timestamp = "2026-11-15T14:00:00-06:00";
  const value = {reference_id: "keep_this_ID_exact", amount: 0, verified: false, note: "First line\nSecond line", captured_at: timestamp};
  try {
    await act(async () => root.render(<RecordFields value={value} raw />));
    assert.match(host.textContent!, /keep_this_ID_exact/);
    assert.match(host.textContent!, /First line\nSecond line/);
    assert.match(host.textContent!, /Amount0/);
    assert.match(host.textContent!, /VerifiedNo/);
    assert.equal(host.querySelector("time")!.dateTime, timestamp);
    assert.equal(host.querySelector("pre"), null);
    const raw = [...host.querySelectorAll("details")].find(item => item.querySelector("summary")?.textContent === "Raw record")!;
    await toggle(raw, true);
    assert.deepEqual(JSON.parse(host.querySelector("pre")!.textContent!), value);
  } finally { await act(async () => root.unmount()); host.remove(); }
});

test("nested records and raw serialization stay deferred until explicitly opened", async () => {
  const host = document.createElement("div"); document.body.append(host);
  const root = createRoot(host);
  let reads = 0;
  const nested = {get source_text() { reads++; return "Captured source content"; }};
  try {
    await act(async () => root.render(<RecordFields value={{metadata: nested}} raw />));
    assert.equal(reads, 0);
    assert.doesNotMatch(host.textContent!, /Captured source content/);
    assert.equal(host.querySelector("pre"), null);
    const detail = [...host.querySelectorAll("details")].find(item => item.querySelector("summary")?.textContent === "1 field")!;
    await toggle(detail, true);
    assert.match(host.textContent!, /Captured source content/);
    assert.ok(reads > 0);
  } finally { await act(async () => root.unmount()); host.remove(); }
});
