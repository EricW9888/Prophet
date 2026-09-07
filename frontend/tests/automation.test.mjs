import assert from "node:assert/strict";
import test from "node:test";
import { automationJobHealth, automationJobOutcome } from "../src/lib/automation.ts";

const job = (last_status) => ({
  name: "gmail_sync", enabled: true, last_status,
  last_run_at: new Date().toISOString(), detail: "Synthetic status",
});

for (const status of ["warning", "partial", "busy", "waiting_for_config", "error"]) {
  test(`${status} does not become healthy merely because it has a run time`, () => {
    assert.equal(automationJobHealth([job(status)], "gmail_sync", "Inbox").tone, "warn");
  });
}

for (const status of ["running", "cancelled", "idle", "unexpected", ""]) {
  test(`${status || "empty status"} does not imply a completed run`, () => {
    assert.notEqual(automationJobOutcome(job(status)), "ok");
    assert.equal(automationJobHealth([job(status)], "gmail_sync", "Inbox").tone, "idle");
  });
}

test("only a known successful result is healthy", () => {
  assert.equal(automationJobHealth([job("ok")], "gmail_sync", "Inbox").tone, "ok");
});

test("disabled jobs remain disabled even after an earlier success", () => {
  const disabled = { ...job("ok"), enabled: false };
  assert.equal(automationJobOutcome(disabled), "disabled");
  assert.equal(automationJobHealth([disabled], "gmail_sync", "Inbox").tone, "idle");
});
