import assert from "node:assert/strict";
import test from "node:test";

import { shadowLifecycleLine } from "../src/components/ShadowLifecycleSummary";

const zero = { total: 0, active: 0, queued: 0, running: 0, waiting: 0, due: 0, overdue: 0, completed: 0, failed: 0, superseded: 0, skipped: 0, manual: 0 };

test("names only states that hold runs", () => {
  const line = shadowLifecycleLine({ ...zero, total: 7, active: 5, running: 5, waiting: 5, completed: 2 });
  assert.equal(line.status, "5 active (5 waiting) · 2 completed");
  assert.equal(line.attention, null);
});

test("overdue and failed runs are called out", () => {
  const line = shadowLifecycleLine({ ...zero, active: 2, waiting: 1, overdue: 1, failed: 3 });
  assert.equal(line.status, "2 active (1 waiting)");
  assert.equal(line.attention, "Needs attention: 1 overdue, 3 failed");
});

test("an empty lab says so", () => {
  assert.equal(shadowLifecycleLine(zero).status, "No active experiments");
});
