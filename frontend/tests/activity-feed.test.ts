import assert from "node:assert/strict";
import test from "node:test";

import { activityStatus, activityTrigger, groupRepeats } from "../src/lib/activity-feed";

test("statuses read as words with a tone; unknown codes stay readable", () => {
  assert.deepEqual(activityStatus("ok"), { label: "Done", tone: "positive" });
  assert.deepEqual(activityStatus("retained_source_assessed"), { label: "Checked, not answered yet", tone: "neutral" });
  assert.deepEqual(activityStatus("brand_new_state"), { label: "Brand new state", tone: "neutral" });
  assert.equal(activityStatus("upload_failed").tone, "negative");
});

test("triggers finish the sentence 'Ran because ...'", () => {
  assert.equal(activityTrigger("signal:provider_recovered"), "the AI provider came back");
  assert.equal(activityTrigger("interval"), "on schedule");
  assert.equal(activityTrigger(undefined), null);
});

test("identical events collapse to the latest, with a count and the earliest time", () => {
  const items = [
    { id: "a", timestamp: "2026-11-27T05:29:00Z", kind: "news", summary: "Checked" },
    { id: "b", timestamp: "2026-11-27T05:28:00Z", kind: "evidence", summary: "Waiting" },
    { id: "c", timestamp: "2026-11-27T05:10:00Z", kind: "news", summary: "Checked" },
    { id: "d", timestamp: "2026-11-27T04:50:00Z", kind: "news", summary: "Checked" },
  ];
  const grouped = groupRepeats(items, (item) => `${item.kind}|${item.summary}`);
  assert.deepEqual(grouped.map((g) => [g.item.id, g.count, g.earliest]), [
    ["a", 3, "2026-11-27T04:50:00Z"],
    ["b", 1, "2026-11-27T05:28:00Z"],
  ]);
});
