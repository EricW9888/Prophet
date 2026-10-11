"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { apiFetch, DashboardSummary, ProviderHealth } from "@/lib/api";

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
  const canProbe = health && ["open", "unobserved"].includes(health.state) && (!health.next_probe_at || Date.parse(health.next_probe_at) <= Date.now());
  return <details className="rounded border border-slate-200 p-3 text-xs dark:border-slate-800">
    <summary className="cursor-pointer font-medium">Work completed in the last 24 hours · {usage.live_analysis_runs_24h} live analyses · {usage.cached_runs_24h} cached</summary>
    <p className="mt-3 text-slate-500">{usage.analysis_runs_24h} analysis records: {usage.live_analysis_runs_24h} live, {usage.cached_runs_24h} cached, {usage.fallback_runs_24h} fallback, {usage.deferred_runs_24h} deferred, {usage.failed_runs_24h} failed, {usage.unclassified_runs_24h} unclassified.</p>
    <p className="mt-2">Independent critiques: {usage.live_critique_runs_24h} live of {usage.critique_runs_24h} attempts. Verification records: {usage.verification_runs_24h}.</p>
    <p className="mt-2 text-slate-500">Analysis activity and accepted investment conclusions are separate outcomes.</p>
    {operations.map(operation => <details key={`${operation.timestamp}:${operation.query}`} className="mt-3 border-t border-slate-100 pt-2 dark:border-slate-800"><summary className="cursor-pointer">{operation.summary}</summary>{operation.phases.map((phase, index) => <p key={index} className="mt-2 text-slate-500">{phase.phase}: {(phase.outcome ?? phase.status).replaceAll("_", " ")}</p>)}</details>)}
    <p className="mt-3">Provider: {health?.provider ?? "unknown"} · {health?.state.replaceAll("_", " ") ?? "checking"}.</p>
    {health?.failure_code ? <p className="mt-1 text-amber-700 dark:text-amber-300">{health.failure_code.replaceAll("_", " ")}. Model-dependent jobs wait; ingestion, market data, paper maintenance and recovery continue.</p> : null}
    {health?.next_probe_at ? <p className="mt-1">Next shared probe after {new Date(health.next_probe_at).toLocaleString()}.</p> : null}
    {health?.state === "unobserved" ? <p className="mt-1 text-slate-500">Structured generation has not been verified for this configuration.</p> : null}
    {health?.state === "waiting_for_config" ? <Link href="/settings" className="mt-2 inline-block underline">Configure the model provider</Link> : null}
    {canProbe ? <button onClick={() => void probe()} disabled={busy} className="mt-3 rounded border border-slate-300 px-3 py-2 dark:border-slate-700">{busy ? "Probing…" : "Run shared provider probe"}</button> : null}
    <p className="mt-2 text-slate-500">The probe requests a small structured response from the configured provider and uses its existing account and rate limits.</p>
    {error ? <p role="status" className="mt-2 text-amber-700 dark:text-amber-300">{error} Last displayed data may be stale.</p> : null}
  </details>;
}
