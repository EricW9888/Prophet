import "./helpers/dom.mjs";
import assert from "node:assert/strict";
import { test } from "node:test";
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import RecordFields from "../src/components/RecordFields";

test("structured receipts retain nested values and provenance without a JSON-only presentation", async () => {
  const data = {max_subjects: 4, tiny_measurement: 1e-30, directory_frontier: {pending_frontier: 0, enabled: false},
    source_versions: [{url: "https://example.test/source_version?key_name=value_1", hash: "abc_123_DEF"}], missing: null};
  const container = document.createElement("div"); document.body.append(container);
  const root = createRoot(container);
  try {
    await act(async () => root.render(<RecordFields value={data} raw />));
    assert.equal(container.querySelector("dl dl"), null, "Nested records wait for inspection");
    assert.equal(container.querySelector("pre"), null, "Raw serialization waits for inspection");
    assert.equal(container.querySelector("details")!.open, false);
    // Follow the actual disclosure path through nested records and arrays.
    for (let depth = 0; depth < 3; depth++) {
      for (const details of [...container.querySelectorAll("details")].filter(item => !item.open)) {
        await act(async () => { details.open = true; details.dispatchEvent(new Event("toggle")); });
      }
    }
    assert.ok(container.querySelector("dl dl"), "Nested records use semantic fields");
    assert.match(container.textContent!, /Directory Frontier/);
    assert.match(container.textContent!, /Pending Frontier0/);
    assert.match(container.textContent!, /EnabledNo/);
    assert.match(container.textContent!, /Tiny Measurement1e-30/);
    assert.match(container.textContent!, /Not recorded/);
    assert.ok(container.textContent!.includes(data.source_versions[0].url));
    assert.ok(container.textContent!.includes(data.source_versions[0].hash));
    assert.deepEqual(JSON.parse(container.querySelector("pre")!.textContent!), data);
  } finally {await act(async () => root.unmount()); container.remove();}
});
