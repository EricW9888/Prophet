import assert from "node:assert/strict";
import test from "node:test";

import { labelText } from "../src/lib/formatting";

test("labels read in sentence case without lowercasing anything", () => {
  assert.equal(labelText("web_research"), "Web research");
  assert.equal(labelText("TSLA · Tesla, Inc."), "TSLA · Tesla, Inc.");
  assert.equal(labelText("scheduled"), "Scheduled");
  assert.equal(labelText(42), 42);
  assert.equal(labelText(null), null);
});
