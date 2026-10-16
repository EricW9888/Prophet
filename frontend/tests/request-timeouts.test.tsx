import assert from "node:assert/strict";
import test from "node:test";
import nextConfig from "../next.config";
import {
  API_TIMEOUT_MS,
  DISCOVERY_RUN_TIMEOUT_MS,
  TRANSLATION_REQUEST_TIMEOUT_MS,
} from "../src/lib/request-timeouts";

test("the real Next proxy configuration outlives every supported client deadline", () => {
  const proxyTimeout = nextConfig.experimental?.proxyTimeout;
  for (const deadline of [API_TIMEOUT_MS, TRANSLATION_REQUEST_TIMEOUT_MS, DISCOVERY_RUN_TIMEOUT_MS]) {
    assert.ok(Number.isFinite(deadline) && deadline > 0);
    assert.ok(typeof proxyTimeout === "number" && Number.isFinite(proxyTimeout) && proxyTimeout > deadline);
  }
});
