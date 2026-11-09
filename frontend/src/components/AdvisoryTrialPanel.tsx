"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { API_BASE, apiFetch } from "@/lib/api";

const path = "/risk/portfolio/advisory-trial";

type Protocol = {
  benchmark_ticker: string;
  duration_days: number;
  outcome_grace_days: number;
  source_max_age_hours: number;
  minimum_weak_holdings: number;
  minimum_weak_weight_pct: number;
  weak_return_pct: number;
  adverse_outcome_pct: number;
  response_delay_minutes: number;
  reduction_fraction: number;
  cost_bps_each_way: number;
};

type Comparison = {
  alerts: number;
  paper_responses: number;
  missed_warnings: number;
  unnecessary_alerts: number;
  mean_advantage_bps: number | null;
  lost_upside_bps: number;
  turnover_pct: number;
};

type Observation = {
  id: string;
  detected_at: string;
  decision: {
    session: string;
    reason: string;
    candidate_alert: boolean;
    benchmark_alert: boolean;
    coverage: { covered_positions: number; total_positions: number };
    weak_weight_pct: number | null;
  };
  evidence: { id: string; title: string | null } | null;
  outcome: { status: "scored" | "unavailable"; reason?: string; hold_return_pct?: number } | null;
};

type TrialState = {
  protocol: Protocol;
  trial: {
    id: string;
    phase: "collecting" | "settling" | "finished" | "stopped";
    declaration: { declared_at: string; collect_until: string; policy: Protocol };
    declaration_hash: string;
    last_capture_detail?: string;
    last_checked_at?: string;
    missed_sessions?: string[];
    observations: Observation[];
    scored_sessions: number;
    adverse_sessions: number;
    comparisons: Record<"candidate" | "benchmark", Comparison>;
  } | null;
};

export default function AdvisoryTrialPanel() {
  const [state, setState] = useState<TrialState | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function request(endpoint: string, method: "GET" | "POST" = "GET") {
    setBusy(true);
    setError(null);
    try {
      setState(await apiFetch<TrialState>(endpoint, { method }));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to load the paper trial.");
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    void request(path);
  }, []);

  const trial = state?.trial;
  const policy = trial?.declaration.policy ?? state?.protocol;
  const active = trial?.phase === "collecting" || trial?.phase === "settling";

  return (
    <section aria-labelledby="advisory-trial-title" className="space-y-4 rounded-xl border border-line bg-surface p-5 sm:p-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 id="advisory-trial-title" className="text-xl font-semibold">Early warning paper trial</h2>
          <p className="mt-2 text-sm text-muted">Compare a source-backed warning with holding and the existing market trend rule. Each response is simulated using later session prices.</p>
        </div>
        <button type="button" disabled={busy} onClick={() => void request(path)} className="rounded border border-line-strong px-3 py-2 text-sm disabled:opacity-50">Refresh trial</button>
      </div>
      {error && <p role="alert" className="text-sm text-red-500">{error}</p>}
      {!state && !error && <p role="status" className="text-sm text-muted">Loading paper trial…</p>}
      {policy && (
        <details className="rounded border border-line p-3 text-sm">
          <summary className="cursor-pointer font-medium">Trial rules and assumptions</summary>
          <div className="mt-3 space-y-2 text-muted">
            <p>Collect for {policy.duration_days} days using {policy.benchmark_ticker || "the configured benchmark"}. A dated adverse source within {policy.source_max_age_hours} hours must link to at least {policy.minimum_weak_holdings} issuers down {Math.abs(policy.weak_return_pct)}% or more in the same session, representing at least {policy.minimum_weak_weight_pct}% of invested value.</p>
            <p>The simulation models a {100 * policy.reduction_fraction}% trim at the next regular open after a {policy.response_delay_minutes}-minute response window, with re-entry at that close and {policy.cost_bps_each_way} basis points of cost each way. A closing loss of {Math.abs(policy.adverse_outcome_pct)}% or more is the scoring threshold.</p>
          </div>
        </details>
      )}
      {state && !active && (
        <button type="button" disabled={busy || !state.protocol.benchmark_ticker} onClick={() => void request(path, "POST")} className="rounded border border-line-strong px-3 py-2 text-sm disabled:opacity-50">
          Start {state.protocol.duration_days}-day paper trial
        </button>
      )}
      {trial && (
        <>
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <p className="font-medium capitalize">{trial.phase}</p>
            <p className="text-muted">Observations through {new Date(trial.declaration.collect_until).toLocaleString()}</p>
            {active && <button type="button" disabled={busy} onClick={() => void request(path + "/" + trial.id + "/stop", "POST")} className="rounded border border-line-strong px-3 py-2 text-sm disabled:opacity-50">Stop trial</button>}
          </div>
          <p className="text-sm text-muted">{trial.last_capture_detail ?? "Waiting for the next risk capture."} {trial.last_checked_at && "Last check " + new Date(trial.last_checked_at).toLocaleString() + "."}</p>
          <p className="text-sm text-muted">{trial.observations.length} captured sessions, {trial.scored_sessions} scored, {trial.adverse_sessions} adverse outcomes, {trial.missed_sessions?.length ?? 0} observed benchmark sessions without a capture.</p>
          {trial.scored_sessions > 0 && (
            <div className="grid gap-3 sm:grid-cols-2">
              {(["candidate", "benchmark"] as const).map((name) => {
                const stats = trial.comparisons[name];
                return (
                  <div key={name} className="space-y-1 rounded border border-line p-3 text-sm">
                    <h3 className="font-medium">{name === "candidate" ? "Evidence and shared weakness" : "Existing benchmark rule"}</h3>
                    <p>{stats.mean_advantage_bps?.toFixed(2)} basis points mean advantage over holding</p>
                    <p className="text-muted">{stats.alerts} alerts, {stats.paper_responses} simulated responses, {stats.missed_warnings} missed warnings, {stats.unnecessary_alerts} unnecessary alerts</p>
                    <p className="text-muted">{stats.lost_upside_bps} basis points forfeited and {stats.turnover_pct}% turnover across scored cases</p>
                  </div>
                );
              })}
            </div>
          )}
          {trial.observations.length > 0 && (
            <ul className="space-y-2">
              {[...trial.observations].reverse().map((caseRow) => (
                <li key={caseRow.id} className="space-y-1 rounded border border-line p-3 text-sm">
                  <p className="font-medium">{caseRow.decision.session} · {caseRow.decision.candidate_alert ? "Paper warning recorded" : caseRow.decision.benchmark_alert ? "Benchmark warning" : "Session observed"}</p>
                  <p className="text-muted">{caseRow.decision.reason} Coverage {caseRow.decision.coverage.covered_positions}/{caseRow.decision.coverage.total_positions}.</p>
                  {caseRow.decision.weak_weight_pct !== null && <p>{caseRow.decision.weak_weight_pct}% of invested value met the weakness rule.</p>}
                  <p className="text-muted">{caseRow.outcome?.status === "scored" ? "Holding returned " + caseRow.outcome.hold_return_pct + "% through the next close." : caseRow.outcome?.reason ?? (active ? "Waiting for the next session outcome." : "Outcome unavailable when collection ended.")}</p>
                  <div className="flex flex-wrap gap-3">
                    {caseRow.evidence && <Link className="underline" href={"/sources/evidence/" + caseRow.evidence.id}>{caseRow.evidence.title ?? "Original evidence"}</Link>}
                    <a className="underline" href={API_BASE + path + "/receipts/" + caseRow.id} target="_blank" rel="noreferrer">Open frozen observation</a>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  );
}
