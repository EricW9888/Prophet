import assert from "node:assert/strict";
import test from "node:test";

import { readableMachineText } from "../src/lib/formatting";

test("key=value pairs and identifiers read as words", () => {
  assert.equal(
    readableMachineText("new evidence ingested: Holding review; triggers=contradiction_pressure; stance=no_view; confidence=very_low; coverage_score=0.7"),
    "new evidence ingested: Holding review · triggers: contradiction pressure · stance: no view · confidence: very low · coverage score: 0.7",
  );
  assert.equal(readableMachineText("risk_off"), "risk off");
  assert.equal(readableMachineText("web_research"), "web research");
});

test("URLs, dates, numbers and ordinary text are left alone", () => {
  assert.equal(readableMachineText("see https://example.com/a_b?x=1_2 now"), "see https://example.com/a_b?x=1_2 now");
  assert.equal(readableMachineText("last=2026-11-27T03:00Z"), "last: 2026-11-27T03:00Z");
  assert.equal(readableMachineText("Revenue rose 12% in Q3."), "Revenue rose 12% in Q3.");
  assert.equal(readableMachineText(null), "");
});
