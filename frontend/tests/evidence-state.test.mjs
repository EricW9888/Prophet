import assert from "node:assert/strict";
import test from "node:test";
import { evidenceProcessingTone } from "../src/lib/evidence-state.ts";

test("retention and intentional skips never imply completed extraction", () => {
  for (const status of ["quarantined", "not_required", "not_applicable", "pending", "running", "unknown", ""]) {
    assert.notEqual(evidenceProcessingTone(status), "good");
  }
  assert.equal(evidenceProcessingTone("quarantined"), "warning");
});

test("deferral and blocked extraction remain visible separately from completion", () => {
  assert.equal(evidenceProcessingTone("provider_deferred"), "warning");
  assert.equal(evidenceProcessingTone("retry_scheduled"), "warning");
  assert.equal(evidenceProcessingTone("retry_exhausted"), "bad");
  assert.equal(evidenceProcessingTone("blocked_missing_content"), "bad");
  assert.equal(evidenceProcessingTone("completed"), "good");
});
