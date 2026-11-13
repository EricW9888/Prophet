import "./helpers/dom.mjs";
import assert from "node:assert/strict";
import {test} from "node:test";
import React, {act} from "react";
import {createRoot} from "react-dom/client";
import QuantityEvidence from "../src/components/QuantityEvidence";

test("numerical receipts distinguish unverified history from matched dimensions and preserve exact values", async () => {
  const container = document.createElement("div"); document.body.append(container);
  const root = createRoot(container);
  try {
    await act(async () => root.render(<QuantityEvidence check={null}/>));
    assert.match(container.textContent!, /No numerical verification was saved/);
    assert.doesNotMatch(container.textContent!, /measurement matched/);
    const check = {verified: true, measurements: [{measurement_ref: "quantity:synthetic", value: "10000.000000000000001", unit: "USD", basis: "captured_close_holdings_value_excluding_cash", as_of: "2026-01-02T21:00:00Z", period_start: "2026-01-01", period_end: "2026-01-02", subject_ref: "portfolio", snapshot_ref: "snapshot-a", source_ref: "capture-a", notes: ["Excludes cash."]}]};
    await act(async () => root.render(<QuantityEvidence check={check}/>));
    assert.match(container.textContent!, /1 recorded numerical measurement matched/);
    assert.match(container.textContent!, /10000\.000000000000001 USD/);
    assert.match(container.textContent!, /Excluding Cash/i);
    assert.match(container.textContent!, /2026-01-01/);
    assert.match(container.textContent!, /snapshot-a/);
    assert.equal(container.querySelector("details")!.open, false);
    await act(async () => root.render(<QuantityEvidence check={{verified: false, issues: ["value_mismatch", "measurement_partial"]}}/>));
    assert.match(container.textContent!, /needs review/);
    assert.match(container.textContent!, /Value Mismatch/i);
  } finally {await act(async () => root.unmount()); container.remove();}
});
