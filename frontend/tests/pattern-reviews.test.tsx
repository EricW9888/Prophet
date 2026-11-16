import "./helpers/dom.mjs";
import assert from "node:assert/strict";
import { test } from "node:test";
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { PatternReviewDetail } from "../src/components/PatternReviews";
import { patternInvestigationHref } from "../src/lib/pattern-reviews";
import { readResearchEntry } from "../src/lib/workflows";
import type { ReasoningTrace } from "../src/lib/api";

const id = "70b318be-2360-4c27-841c-129967d0db17";
const trace: ReasoningTrace = {
  id, run_type: "pattern_discovery", model_used: "synthetic-provider", model_version: "synthetic-model", created_at: "2026-11-15T18:00:00Z",
  structured_output_json: {
    snapshot: { captured_at: "2026-11-15T18:00:00Z", validation_policy: { minimum_confidence: 0.78 }, input: {source_dated_signals: [
      {ref: "setup:one", signal_name: "Synthetic supply report", source_name: "Publisher one", public_time: null, signal_created_at: "2026-11-14T18:00:00Z", evidence_url: "https://one.example.test/report", lineage_key: "publisher:one.example.test", raw_evidence_id: id, setup_context: "Signal text"},
      {ref: "setup:two", signal_name: "Unsafe source URL", evidence_url: "javascript:alert(1)", raw_evidence_id: "not-an-id"},
    ]}},
    proposal: {reasoning_summary: "A proposed connection still needs checking.", hypothesis: {label: "Synthetic memory transmission", proposed_mechanism: "Capacity cuts may improve margins.", falsifier: "Independent data shows rising supply.", next_test: "Check capacity and contract pricing.", why_now: "Before the next report.", confidence: 0.1, evidence_refs: ["setup:one", "setup:missing"]}},
    review: {status: "rejected", detail: "pattern_confidence_below_threshold"},
  },
};

test("pattern review separates rejection, source attribution and uncalibrated confidence", async () => {
  const container = document.createElement("div"); document.body.append(container);
  const root = createRoot(container);
  try {
    await act(async () => root.render(<PatternReviewDetail trace={trace} />));
    assert.match(container.textContent!, /Not admitted/);
    assert.match(container.textContent!, /What would disprove it/);
    assert.match(container.textContent!, /Confidence is uncalibrated/);
    assert.match(container.textContent!, /Published Unknown/);
    assert.match(container.textContent!, /Unmatched model references: setup:missing/);
    assert.match(container.textContent!, /Synthetic supply report · Cited/);
    assert.equal(container.querySelector('a[href="javascript:alert(1)"]'), null);
    assert.equal(container.querySelectorAll('a[href^="/sources/evidence/"]').length, 1);
    assert.doesNotMatch(container.textContent!, /Unsafe source URL/);
    const otherSignals = [...container.querySelectorAll("details")].find(item => item.querySelector("summary")?.textContent === "Other captured signals · 1")!;
    await act(async () => { otherSignals.open = true; otherSignals.dispatchEvent(new Event("toggle")); });
    assert.match(container.textContent!, /Unsafe source URL/);
    assert.equal(container.querySelector('a[href="javascript:alert(1)"]'), null);
    // Expensive exact packet rendering is deferred until requested.
    assert.equal(container.querySelector("pre"), null);
    const draft = container.querySelector('a[href^="/chat?"]')!;
    assert.equal(draft.textContent, "Draft investigation");
    assert.match(container.textContent!, /unsent question/);
  } finally { await act(async () => root.unmount()); container.remove(); }
});

test("investigation draft retains disposition, falsifier and cited context within the chat entry limit", () => {
  const large = structuredClone(trace);
  const hypothesis = (large.structured_output_json.proposal as {hypothesis: Record<string, unknown>}).hypothesis;
  for (const field of ["label", "observation", "proposed_mechanism", "why_now", "falsifier", "next_test"]) hypothesis[field] = field.repeat(2000);
  const href = patternInvestigationHref(large);
  const params = new URL(href, "http://localhost:3000").searchParams;
  const raw = params.get("q")!;
  assert.ok(raw.length < 6000);
  const entry = readResearchEntry(params);
  assert.equal(entry.question, raw);
  assert.equal(entry.sessionId, null);
  assert.match(raw, /UNVERIFIED model proposal/);
  assert.match(raw, /pattern_confidence_below_threshold/);
  assert.ok(raw.includes(id));
  assert.match(raw, /https:\/\/one.example.test\/report/);
  assert.match(raw, /publication: unknown/);
  assert.match(raw, /bounded excerpts only/);
  assert.ok(!raw.includes("javascript:"));
});
