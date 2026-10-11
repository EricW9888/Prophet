import assert from "node:assert/strict";
import test from "node:test";
import { loadMonitorSnapshot } from "../src/lib/monitor.ts";

test("a secondary summary failure preserves the available evidence feed and its times", async () => {
  const items = [{ id: "synthetic", display_time: "2026-10-10T12:00:00Z", created_at: "2026-10-11T12:00:00Z" }];
  const snapshot = await loadMonitorSnapshot(async () => items, async () => { throw new Error("summary unavailable"); });
  assert.equal(snapshot.items, items);
  assert.equal(snapshot.items[0].display_time, items[0].display_time);
  assert.equal(snapshot.changes, null);
  assert.ok(snapshot.warning);
});

test("a feed failure is a failed refresh, never a successful empty feed", async () => {
  await assert.rejects(loadMonitorSnapshot(async () => { throw new Error("feed unavailable"); }, async () => ({ changes: [] })), /feed unavailable/);
});

test("an observed empty feed remains a successful snapshot", async () => {
  const snapshot = await loadMonitorSnapshot(async () => [], async () => ({ changes: [] }));
  assert.deepEqual(snapshot.items, []);
  assert.equal(snapshot.warning, null);
  assert.deepEqual(snapshot.changes, { changes: [] });
});
