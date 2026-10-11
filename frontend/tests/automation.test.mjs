import assert from "node:assert/strict";
import test from "node:test";
import { automationJobHealth, automationJobOutcome, automationOverallHealth } from "../src/lib/automation.ts";

const job = (last_status) => ({
  name: "gmail_sync", enabled: true, last_status,
  last_run_at: new Date().toISOString(), detail: "Synthetic status",
});

for (const status of ["warning", "partial", "busy", "waiting_for_config", "deferred", "rejected", "error"]) {
  test(`${status} does not become healthy merely because it has a run time`, () => {
    assert.equal(automationJobHealth([job(status)], "gmail_sync", "Inbox").tone, "warn");
  });
}

for (const status of ["running", "cancelled", "idle", "skipped", "no_new_evidence", "no_new_knowledge", "unexpected", ""]) {
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


test("overall health distinguishes disabled, unobserved and deferred automation", () => {
  assert.equal(automationOverallHealth(false, []), "Disabled");
  assert.equal(automationOverallHealth(true, []), "Starting");
  assert.equal(automationOverallHealth(true, [job("deferred")]), "Needs review");
  assert.equal(automationOverallHealth(true, [job("no_new_evidence")]), "Operational");
});
