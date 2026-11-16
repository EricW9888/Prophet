import "./helpers/dom.mjs";
import assert from "node:assert/strict";
import { afterEach, beforeEach, test } from "node:test";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import SourcePassage from "../src/components/SourcePassage";

let root: Root;
let container: HTMLDivElement;
beforeEach(() => {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});
afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
});

test("original quotation keeps correction context and literal markup readable", async () => {
  const exact = "Helix's license has not been granted. <script>invented()</script>";
  await act(async () => root.render(<SourcePassage passage={{ status: "bound", raw_evidence_id: "synthetic-receipt", quote_selector: {type: "TextQuoteSelector", exact, prefix: "Earlier report: approved. Correction: ", suffix: " Operations remain conditional."}, semantic_verification: false }} />));
  const quotation = container.querySelector("blockquote");
  assert.ok(quotation);
  assert.match(quotation.textContent!, /Correction: Helix's license has not been granted/);
  assert.ok(quotation.textContent!.includes(exact));
  assert.equal(container.querySelector("script"), null);
  assert.ok(container.querySelector('[aria-label="What a matched source passage means"]'));
  assert.equal(container.querySelector("a")?.getAttribute("href"), "/sources/evidence/synthetic-receipt#original-source-text");
});

for (const status of ["unknown", "unbound", "source_changed", "assertion_changed"] as const) {
  test(`${status} never displays a supplied quotation as matched source text`, async () => {
    await act(async () => root.render(<SourcePassage passage={{ status, reason: "No usable anchor", quote_selector: {type: "TextQuoteSelector", exact: "fabricated source claim", prefix: "", suffix: ""} }} />));
    assert.equal(container.querySelector("blockquote"), null);
    assert.match(container.textContent!, /No usable anchor/);
    assert.doesNotMatch(container.textContent!, /fabricated source claim/);
  });
}
