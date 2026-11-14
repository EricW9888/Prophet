import "./helpers/dom.mjs";
import assert from "node:assert/strict";
import { test } from "node:test";
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import ResearchReceipt from "../src/components/ResearchReceipt";

test("research receipts distinguish searched holdings, candidate misses, and skipped checks", async () => {
  const container = document.createElement("div"); document.body.append(container);
  const root = createRoot(container);
  try {
    await act(async () => root.render(<ResearchReceipt receipt={{
      evidence_prepared_at: "2026-11-14T06:00:00Z", holdings_count: 5, holdings_with_linked_evidence: ["AAA"],
      holding_search_coverage: [
        {ticker: "AAA", status: "candidate_found", searched: true, candidate_count: 2, candidate_urls: []},
        {ticker: "BBB", status: "no_matching_candidate", searched: true, candidate_count: 0, candidate_urls: []},
        {ticker: "CCC", status: "request_limit", searched: false, candidate_count: 0, candidate_urls: []},
        {ticker: "DDD", status: "budget_exhausted", searched: false, candidate_count: 0, candidate_urls: []},
        {ticker: "EEE", status: "research_failed", searched: true, candidate_count: 0, candidate_urls: []},
      ],
      checks: [{lane: "Holdings", query: "AAA BBB", status: "matched", result_count: 2}, {lane: "Holdings", query: "DDD", status: "budget_exhausted", result_count: 0}],
      candidate_sources: [{title: "Blocked URL", url: "javascript:alert(1)"}],
    }} />));
    assert.match(container.querySelector("summary")!.textContent!, /2 of 5 holding checks complete · 3 incomplete/);
    const rows = [...container.querySelectorAll('ul[aria-label="Holding search coverage"] li')].map((row) => row.textContent);
    assert.match(rows[0]!, /AAA2 candidates/);
    assert.match(rows[1]!, /BBBNo matching candidate/);
    assert.match(rows[2]!, /CCCNot searched · request limit/);
    assert.match(rows[3]!, /DDDNot searched · Budget Exhausted/);
    assert.match(rows[4]!, /EEEIncomplete · Research Failed/);
    assert.match(container.textContent!, /Stored evidence explicitly linked to 1 of 5/);
    assert.equal(container.querySelectorAll("a").length, 0);
    assert.equal(container.querySelector("time")!.dateTime, "2026-11-14T06:00:00Z");
  } finally {await act(async () => root.unmount()); container.remove();}
});
