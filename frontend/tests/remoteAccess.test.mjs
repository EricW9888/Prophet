import assert from "node:assert/strict";
import test from "node:test";

import { remoteAccessDecision } from "../src/lib/remoteAccess.ts";

const expectedIdentity = "owner@example.com";

test("loopback requests remain available without remote access configuration", () => {
  for (const hostHeader of ["127.0.0.1:3000", "localhost:3000", "[::1]:3000"]) {
    assert.deepEqual(
      remoteAccessDecision({ hostHeader, proxyIdentity: null, expectedIdentity: undefined }),
      { allowed: true, mode: "local" },
    );
  }
});

test("non-loopback requests are denied when private access is not configured", () => {
  assert.deepEqual(
    remoteAccessDecision({
      hostHeader: "prophet.example.ts.net",
      proxyIdentity: expectedIdentity,
      expectedIdentity: undefined,
    }),
    { allowed: false, reason: "remote_disabled" },
  );
});

test("non-loopback requests require the trusted proxy identity header", () => {
  assert.deepEqual(
    remoteAccessDecision({
      hostHeader: "prophet.example.ts.net",
      proxyIdentity: null,
      expectedIdentity,
    }),
    { allowed: false, reason: "identity_missing" },
  );
});

test("private proxy identity must exactly match the configured operator", () => {
  assert.deepEqual(
    remoteAccessDecision({
      hostHeader: "prophet.example.ts.net",
      proxyIdentity: "someone-else@example.com",
      expectedIdentity,
    }),
    { allowed: false, reason: "identity_mismatch" },
  );
  assert.deepEqual(
    remoteAccessDecision({
      hostHeader: "prophet.example.ts.net",
      proxyIdentity: " OWNER@example.com ",
      expectedIdentity,
    }),
    { allowed: true, mode: "private_proxy" },
  );
});

test("proxy identity is still checked when a reverse proxy rewrites Host to loopback", () => {
  assert.deepEqual(
    remoteAccessDecision({
      hostHeader: "127.0.0.1:3000",
      proxyIdentity: "someone-else@example.com",
      expectedIdentity,
    }),
    { allowed: false, reason: "identity_mismatch" },
  );
  assert.deepEqual(
    remoteAccessDecision({
      hostHeader: "localhost:3000",
      proxyIdentity: expectedIdentity,
      expectedIdentity,
    }),
    { allowed: true, mode: "private_proxy" },
  );
});
