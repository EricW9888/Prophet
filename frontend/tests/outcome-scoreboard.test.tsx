import assert from "node:assert/strict";
import test from "node:test";

import { outcomeLabel, rateText } from "../src/components/OutcomeScoreboard";

test("rates always show their base and never invent a rate", () => {
  assert.equal(rateText(0.913, 42, 4), "91% · 42 of 46 decided");
  assert.equal(rateText(null, 0, 0), "Nothing decided yet");
});

test("outcomes read in plain language", () => {
  assert.equal(outcomeLabel("validated"), "Held");
  assert.equal(outcomeLabel("partially_correct"), "Partly correct");
  assert.equal(outcomeLabel("some_new_state"), "some new state");
});
