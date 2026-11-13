import "./helpers/dom.mjs";
import assert from "node:assert/strict";
import {test} from "node:test";
import React, {act} from "react";
import {createRoot} from "react-dom/client";
import ProviderWorkSummary from "../src/components/ProviderWorkSummary";
import type {DashboardSummary} from "../src/lib/api";

test("short request failures remain visible without being counted as completed work or a shared outage", async () => {
  const usage: DashboardSummary["llm_usage"] = {analysis_runs_24h:0,cached_runs_24h:0,verification_runs_24h:0,total_input_tokens_24h:0,total_output_tokens_24h:0,avg_duration_ms:0,live_analysis_runs_24h:0,fallback_runs_24h:0,deferred_runs_24h:0,failed_runs_24h:0,unclassified_runs_24h:0,critique_runs_24h:0,live_critique_runs_24h:0};
  const oldFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify({provider:"nvidia_nim",state:"healthy",caller_timeout_count:2,last_caller_timeout_seconds:8,last_caller_timeout_at:"2026-01-02T21:00:00Z"}), {headers:{"Content-Type":"application/json"}});
  const container=document.createElement("div"); document.body.append(container); const root=createRoot(container);
  try {
    await act(async () => {root.render(<ProviderWorkSummary usage={usage} operations={[]}/>);});
    assert.match(container.textContent!, /0 live analyses/);
    assert.match(container.textContent!, /Provider: nvidia_nim · healthy/);
    assert.match(container.textContent!, /2 recorded across retained history/);
    assert.match(container.textContent!, /8s request limit/);
    assert.doesNotMatch(container.textContent!, /Model-dependent jobs wait/);
  } finally {await act(async () => root.unmount()); container.remove(); globalThis.fetch=oldFetch;}
});
