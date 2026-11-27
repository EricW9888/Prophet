import assert from "node:assert/strict";
import test from "node:test";

import { responseTimes } from "../src/components/ProviderWorkSummary";

test("response times read as a median and the fastest tenth", () => {
  const seconds = Array.from({ length: 20 }, (_, i) => 30 + i * 2);
  assert.equal(responseTimes(seconds), "Recent responses took about 50s (fastest tenth 34s), over the last 20 successful requests.");
  assert.equal(responseTimes([12]), "Recent responses took about 12s (fastest tenth 12s), over the last 1 successful request.");
});
