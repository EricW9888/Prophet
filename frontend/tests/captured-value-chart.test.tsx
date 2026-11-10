import "./helpers/dom.mjs";
import assert from "node:assert/strict";
import { afterEach, beforeEach, test } from "node:test";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import CapturedHoldingsPanel from "../src/components/CapturedHoldingsPanel";
import CapturedValueChart from "../src/components/CapturedValueChart";
import type { CapturedHoldingsValue } from "../src/lib/api";

let root: Root;
let container: HTMLDivElement;
const originalFetch = globalThis.fetch;
const capture = (day: number, value: number | null): CapturedHoldingsValue => ({
  assessment_id: `receipt-${day}`,
  captured_at: `2025-05-${day}T21:00:00Z`,
  price_session: `2025-05-${day}`,
  covered_positions: value == null ? 2 : 3,
  total_positions: 3,
  value_usd: value,
});

beforeEach(() => {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  globalThis.fetch = originalFetch;
});

function feed(rows: CapturedHoldingsValue[]) {
  globalThis.fetch = async () => new Response(JSON.stringify(rows), {
    status: 200, headers: { "Content-Type": "application/json" },
  });
}

async function click(button: HTMLButtonElement) {
  await act(async () => button.click());
}

test("a partial portfolio capture breaks the line rather than plotting a false loss", async () => {
  feed([capture(20, 3000), capture(21, null), capture(22, 3200)]);
  await act(async () => root.render(<CapturedHoldingsPanel />));
  assert.match(container.textContent!, /2 complete of 3 recent captures/);
  const path = container.querySelector('path[stroke="currentColor"]')?.getAttribute("d") ?? "";
  assert.equal(path.match(/M /g)?.length, 2);
  assert.doesNotMatch(path, /L /);
  assert.equal(container.querySelector("a")?.getAttribute("href"), "/api_proxy/risk/portfolio/packets/receipt-22");
});

test("the what-if applies a user selected shock and refuses an incomplete current basis", async () => {
  feed([capture(20, 3000)]);
  await act(async () => root.render(<CapturedHoldingsPanel view="scenario" />));
  const button = (name: string) => [...container.querySelectorAll("button")].find((item) => item.textContent === name)!;
  await click(button("+20%"));
  assert.match(container.textContent!, /Under this shock\$3,600.00/);
  assert.match(container.textContent!, /Change in holdings value: \+\$600.00/);
  feed([capture(20, 3000), capture(21, null)]);
  await click(button("Reload captures"));
  assert.match(container.textContent!, /latest capture covered 2 of 3 holdings/);
  assert.equal(container.querySelector('input[type="range"]'), null);
});

test("selecting a fill exposes its record without attaching equity to the fill time", async () => {
  await act(async () => root.render(<CapturedValueChart label="Paper equity" points={[
    { id: "start", at: "2025-05-20T18:00:00Z", value: 3000 },
    { id: "end", at: "2025-05-21T18:00:00Z", value: 3250 },
  ]} events={[
    { id: "fill", at: "2025-05-20T19:00:00Z", label: "Buy SYN-A", detail: "Test trade rationale", kind: "fill" },
  ]} />));
  assert.match(container.querySelector('[aria-live="polite"]')!.textContent!, /\$3,250.00/);
  await click(container.querySelector("button")!);
  const readout = container.querySelector('[aria-live="polite"]')!.textContent!;
  assert.match(readout, /Buy SYN-A.*Test trade rationale/);
  assert.doesNotMatch(readout, /\$/);
});
