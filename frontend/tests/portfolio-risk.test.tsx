import "./helpers/dom.mjs";
import assert from "node:assert/strict";
import { afterEach, beforeEach, test } from "node:test";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import PortfolioRiskPanel from "../src/components/PortfolioRiskPanel";
import type { PortfolioRiskState } from "../src/lib/api";

let root: Root;
let container: HTMLDivElement;
let handle: (url: string, options?: RequestInit) => unknown | Promise<unknown>;
const originalFetch = globalThis.fetch;

function state(): PortfolioRiskState {
  return { checked_at: "2025-05-21T22:00:00Z", last_refresh_at: "2025-05-21T21:59:00Z", refresh_interval_seconds: 300, automation_enabled: true, market_data_enabled: true,
    assessment: { id: "synthetic-assessment", detected_at: "2025-05-21T21:59:00Z", packet_hash: "synthetic-hash", result: {
      status: "ready", cutoff: "2025-05-21T21:59:00Z", currency: "USD", covered_positions: 3, total_positions: 3, covered_value: 3000,
      matched_returns: 60, historical_stress_loss: 150, worst_observed_loss: 200, worst_session: ["2025-05-01", "2025-05-02"], median_pair_correlation: 0.9,
      excluded: [], issues: [], limitations: ["Synthetic scenario, not a forecast"],
      positions: [{ ticker: "SYN-A", security_id: "synthetic-security", weight_pct: 50, signal_ref: "market:SYN-A", return_1d_pct: 5, volume_ratio: 3, worst_session_impact: -100 }],
    } }, watch: null, delivery: [] };
}

beforeEach(() => {
  container = document.createElement("div"); document.body.append(container); root = createRoot(container);
  handle = () => state();
  globalThis.fetch = async (input, options) => new Response(JSON.stringify(await handle(String(input), options)), { status: 200, headers: { "Content-Type": "application/json" } });
});

afterEach(async () => { await act(async () => { root.unmount(); }); container.remove(); globalThis.fetch = originalFetch; });

async function render() { await act(async () => { root.render(<PortfolioRiskPanel />); }); }
async function click(label: string) {
  const button = [...container.querySelectorAll("button")].find(item => item.textContent === label);
  assert.ok(button); await act(async () => { button.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
}

test("risk review shows matching price and volume plus a frozen input receipt", async () => {
  await render();
  assert.match(container.textContent!, /\$150.00/);
  assert.match(container.textContent!, /5.00%/);
  assert.match(container.textContent!, /3.00×/);
  assert.match(container.textContent!, /cash excluded/);
  assert.match(container.querySelector("a")!.getAttribute("href")!, /\/risk\/portfolio\/packets\/synthetic-assessment$/);
  const arm = [...container.querySelectorAll("button")].find(item => item.textContent === "Save and arm watch")!;
  assert.equal(arm.disabled, true);
});

test("missing exposure cannot render a zero-dollar risk or healthy monitoring", async () => {
  const data = state(); data.last_refresh_at = "2025-05-20T00:00:00Z";
  data.assessment!.result.status = "insufficient_data";
  data.assessment!.result.historical_stress_loss = null;
  data.assessment!.result.worst_observed_loss = null;
  data.assessment!.result.excluded = [{ ticker: "SYN-B", reason: "missing quote currency" }];
  handle = () => data;
  await render();
  assert.match(container.textContent!, /No recent successful risk check/);
  assert.match(container.textContent!, /Missing exposure is not zero risk/);
  assert.match(container.textContent!, /SYN-B: missing quote currency/);
  assert.match(container.textContent!, /Unavailable/);
});

test("a triggered watch displays delivery separately and requires explicit rearm", async () => {
  const data = state();
  data.watch = { id: "synthetic-watch", status: "triggered", enabled: false, policy: { loss_budget: 100, currency: "USD", max_age_hours: 96 }, last_checked_at: data.checked_at, triggered_at: data.checked_at, detail: "Synthetic budget breach", assessment: { assessment_id: "older-packet", decision: { state: "breached" } } };
  handle = () => data;
  await render();
  assert.match(container.textContent!, /Triggered · rearm after review/);
  assert.match(container.textContent!, /No push delivery recorded/);
  assert.equal([...container.querySelectorAll("button")].filter(item => item.textContent === "Rearm advisory watch").length, 1);
  assert.ok([...container.querySelectorAll("a")].some(item => item.href.endsWith("/older-packet")));
});

test("a refresh error preserves the previous assessment and shows the failure", async () => {
  await render();
  globalThis.fetch = async () => new Response("Synthetic provider failure", { status: 503 });
  await click("Check portfolio risk");
  assert.match(container.querySelector('[role="alert"]')!.textContent!, /Synthetic provider failure/);
  assert.match(container.textContent!, /\$150.00/);
});

test("a late poll cannot replace a newly refreshed risk assessment", async () => {
  let release!: (value: unknown) => void;
  handle = () => new Promise(resolve => { release = resolve; });
  await render();
  handle = () => { const data = state(); data.assessment!.result.historical_stress_loss = 250; return data; };
  await click("Check portfolio risk");
  await act(async () => { release(state()); });
  assert.match(container.textContent!, /\$250.00/);
  assert.doesNotMatch(container.textContent!, /\$150.00/);
});
