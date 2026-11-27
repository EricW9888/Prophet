"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { apiFetch, ConnectionHealth, DashboardSummary, ProviderHealth } from "@/lib/api";

/** "Recent responses took about 48s (fastest tenth 31s), over the last 50 successful requests." */
export function responseTimes(seconds: number[]): string {
  const sorted = [...seconds].sort((a, b) => a - b);
  const at = (q: number) => sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * q))];
  const n = sorted.length;
  return `Recent responses took about ${Math.round(at(0.5))}s (fastest tenth ${Math.round(at(0.1))}s), over the last ${n} successful request${n === 1 ? "" : "s"}.`;
}

export default function ProviderWorkSummary({ usage, operations }: { usage: DashboardSummary["llm_usage"]; operations: DashboardSummary["recent_research_actions"] }) {
  const [health, setHealth] = useState<ProviderHealth | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function refresh() {
    try { setHealth(await apiFetch<ProviderHealth>("/automation/provider-health")); setError(null); }
    catch { setError("Provider health could not be refreshed."); }
  }
  useEffect(() => { void refresh(); const timer = setInterval(() => void refresh(), 15000); return () => clearInterval(timer); }, []);
  async function probe() {
    setBusy(true);
    try { setHealth(await apiFetch<ProviderHealth>("/automation/provider-health/probe", { method: "POST" })); setError(null); }
    catch { setError("The shared provider probe could not complete."); }
    finally { setBusy(false); }
  }
  const [testing, setTesting] = useState<string | null>(null);
  async function testConnection(name: string) {
    setTesting(name);
    try { await apiFetch<ConnectionHealth>(`/automation/provider-health/connections/${encodeURIComponent(name)}/probe`, { method: "POST" }); await refresh(); }
    catch { setError(`The connection "${name}" could not be tested.`); }
    finally { setTesting(null); }
  }
  const canProbe = health && ["open", "unobserved"].includes(health.state) && (!health.next_probe_at || Date.parse(health.next_probe_at) <= Date.now());
  return <details className="rounded border border-slate-200 p-3 text-xs dark:border-slate-800">
    <summary className="cursor-pointer font-medium">Work completed in the last 24 hours · {usage.live_analysis_runs_24h} live analyses · {usage.cached_runs_24h} cached</summary>
    <p className="mt-3 text-slate-500">{usage.analysis_runs_24h} analysis records: {usage.live_analysis_runs_24h} live, {usage.cached_runs_24h} cached, {usage.fallback_runs_24h} fallback, {usage.deferred_runs_24h} deferred, {usage.failed_runs_24h} failed, {usage.unclassified_runs_24h} unclassified.</p>
    <p className="mt-2">Independent critiques: {usage.live_critique_runs_24h} live of {usage.critique_runs_24h} attempts. Verification records: {usage.verification_runs_24h}.</p>
    <p className="mt-2 text-slate-500">Analysis activity and accepted investment conclusions are separate outcomes.</p>
    {operations.map(operation => <details key={`${operation.timestamp}:${operation.query}`} className="mt-3 border-t border-slate-100 pt-2 dark:border-slate-800"><summary className="cursor-pointer">{operation.summary}</summary>{operation.phases.map((phase, index) => <p key={index} className="mt-2 text-slate-500">{phase.phase}: {(phase.outcome ?? phase.status).replaceAll("_", " ")}</p>)}</details>)}
    <p className="mt-3">Provider: {health?.provider ?? "unknown"} · {health?.state.replaceAll("_", " ") ?? "checking"}.</p>
    {health?.admission_timeout_count ? <details className="mt-2">
      <summary className="cursor-pointer">Requests that timed out waiting for a slot</summary>
      <p className="mt-2">Prophet recorded {health.admission_timeout_count} timeouts while waiting for local request capacity.</p>
      {health.last_admission_timeout_at ? <p className="mt-1">Latest at {new Date(health.last_admission_timeout_at).toLocaleString()}.</p> : null}
    </details> : null}
    {health?.caller_timeout_count ? <details className="mt-2"><summary className="cursor-pointer">Requests that reached shorter time limits</summary><p className="mt-2">{health.caller_timeout_count} recorded across retained history. These requests failed at their own time limit; their errors remain in Activity.</p>{health.last_caller_timeout_at ? <p className="mt-1">Latest: {new Date(health.last_caller_timeout_at).toLocaleString()}{health.last_caller_timeout_seconds != null ? ` · ${health.last_caller_timeout_seconds}s request limit` : ""}.</p> : null}</details> : null}
    {health?.connections?.length ? <div className="mt-2 space-y-1">
      <p>Extra connections:</p>
      {health.connections.map((c) => <div key={c.name} className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="font-medium">{c.name}</span>
        <span className="text-muted">{c.model} · {(c.state ?? "unknown").replaceAll("_", " ")}{c.median_latency_seconds != null ? ` · about ${Math.round(c.median_latency_seconds)}s per response` : ""}{c.failure_code ? ` · ${c.failure_code.replaceAll("_", " ")}` : ""}</span>
        <button type="button" className="workspace-control" disabled={testing === c.name} onClick={() => void testConnection(c.name)}>{testing === c.name ? "Testing…" : "Test"}</button>
      </div>)}
    </div> : null}
    {health?.recent_latency_seconds?.length ? <p className="mt-2">{responseTimes(health.recent_latency_seconds)}</p> : null}
    {health?.short_limit_skip_count ? <p className="mt-1 text-muted">{health.short_limit_skip_count} quick requests were not sent because the provider has recently been slower than their time limit; they used their fallback at once instead of waiting and failing.</p> : null}
    {health?.failure_code ? <p className="mt-1 text-caution">{health.failure_code.replaceAll("_", " ")}. Model-dependent jobs wait; ingestion, market data, paper maintenance and recovery continue.</p> : null}
    {health?.failure_detail && <details className="mt-2 text-slate-500"><summary className="cursor-pointer">Structured response diagnostic</summary><p className="mt-2">{health.failure_detail.kind?.replaceAll("_", " ")}{health.failure_detail.finish_reason ? ` · finish: ${health.failure_detail.finish_reason}` : ""}{health.failure_detail.content_chars != null ? ` · content: ${health.failure_detail.content_chars} characters` : ""}{health.failure_detail.completion_tokens != null ? ` · generated: ${health.failure_detail.completion_tokens} tokens` : ""}{health.failure_detail.max_tokens != null ? ` · limit: ${health.failure_detail.max_tokens} tokens` : ""}.</p></details>}
    {health?.next_probe_at ? <p className="mt-1">Next shared probe after {new Date(health.next_probe_at).toLocaleString()}.</p> : null}
    {health?.state === "unobserved" ? <p className="mt-1 text-slate-500">Structured generation has not been verified for this configuration.</p> : null}
    {health?.state === "waiting_for_config" ? <Link href="/settings" className="mt-2 inline-block underline">Configure the model provider</Link> : null}
    {canProbe ? <button onClick={() => void probe()} disabled={busy} className="mt-3 rounded border border-slate-300 px-3 py-2 dark:border-slate-700">{busy ? "Probing…" : "Run shared provider probe"}</button> : null}
    <p className="mt-2 text-slate-500">The probe requests a small structured response from the configured provider and uses its existing account and rate limits.</p>
    {error ? <p role="status" className="mt-2 text-caution">{error} Last displayed data may be stale.</p> : null}
  </details>;
}
