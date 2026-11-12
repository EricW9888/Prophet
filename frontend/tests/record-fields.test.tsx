import "./helpers/dom.mjs";
import assert from "node:assert/strict";
import { test } from "node:test";
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import RecordFields from "../src/components/RecordFields";

test("structured receipts retain nested values and provenance without a JSON-only presentation", async () => {
  const data = {max_subjects: 4, directory_frontier: {pending_frontier: 0, enabled: false},
    source_versions: [{url: "https://example.test/source_version?key_name=value_1", hash: "abc_123_DEF"}], missing: null};
  const container = document.createElement("div"); document.body.append(container);
  const root = createRoot(container);
  try {
    await act(async () => root.render(<RecordFields value={data} raw />));
    assert.ok(container.querySelector("dl dl"), "Nested records use semantic fields");
    assert.match(container.textContent!, /Directory Frontier/);
    assert.match(container.textContent!, /Pending Frontier0/);
    assert.match(container.textContent!, /EnabledNo/);
    assert.match(container.textContent!, /Not recorded/);
    assert.ok(container.textContent!.includes(data.source_versions[0].url));
    assert.ok(container.textContent!.includes(data.source_versions[0].hash));
    assert.deepEqual(JSON.parse(container.querySelector("pre")!.textContent!), data);
    assert.equal(container.querySelector("details")!.open, false);
  } finally {await act(async () => root.unmount()); container.remove();}
});
