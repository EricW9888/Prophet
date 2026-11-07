"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";

import { API_BASE, apiFetch, PortfolioRiskState } from "@/lib/api";
import PortfolioDataReviewNotice from "@/components/PortfolioDataReviewNotice";

const money = (value: number | null | undefined) => value == null ? "Unavailable" : new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(value);
const date = (value: string | null | undefined) => value ? new Date(value).toLocaleString() : "Not yet checked";

export default function PortfolioRiskPanel() {
  const [state, setState] = useState<PortfolioRiskState | null>(null);
  const [budget, setBudget] = useState("");
  const [age, setAge] = useState("96");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestSequence = useRef(0);
  const mutationPending = useRef(false);

  async function load() {
    if (mutationPending.current) return;
    const request = ++requestSequence.current;
    try {
      const next = await apiFetch<PortfolioRiskState>("/risk/portfolio");
      if (request === requestSequence.current) { setState(next); setError(null); }
    } catch (err) {
      if (request === requestSequence.current) setError(err instanceof Error ? err.message : "Unable to load portfolio risk.");
    }
  }

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => { void load(); }, 20000);
    return () => window.clearInterval(timer);
  }, []);

  async function mutate(path: string, method: string, body?: object) {
    mutationPending.current = true;
    ++requestSequence.current;
    setBusy(true);
    setError(null);
    try {
      const response = await fetch(`${API_BASE}${path}`, {
        method, headers: { "Content-Type": "application/json" },
        ...(body ? { body: JSON.stringify(body) } : {}),
      });
      if (!response.ok) throw new Error(await response.text());
      setState(await response.json());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to update portfolio risk.");
    } finally {
      mutationPending.current = false;
      setBusy(false);
    }
  }

  const assessment = state?.assessment;
  const result = assessment?.result;
  const watch = state?.watch;
  const stale = !!state && (!state.last_refresh_at || new Date(state.checked_at).getTime() - new Date(state.last_refresh_at).getTime() > state.refresh_interval_seconds * 3000);

  return (
    <section aria-labelledby="portfolio-risk-title" className="rounded-xl border border-line bg-surface p-5 sm:p-6 space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="max-w-3xl">
          <p className="text-xs uppercase tracking-wider text-muted">Your holdings · Daily risk review</p>
          <h2 id="portfolio-risk-title" className="mt-1 text-xl font-semibold">Could a bad day exceed your loss budget?</h2>
          <p className="mt-2 text-sm text-muted">Replay simultaneous daily moves across your current holdings to reveal shared price risk. A calm benchmark does not establish that your portfolio is safe.</p>
        </div>
        <button type="button" disabled={busy} onClick={() => { void mutate("/risk/portfolio/refresh", "POST"); }} className="rounded border border-line-strong px-3 py-2 text-sm disabled:opacity-50">{busy ? "Working…" : "Check portfolio risk"}</button>
      </div>

      {error && <p role="alert" className="text-sm text-red-500">{error}</p>}
      {!state && !error && <p role="status" className="text-sm text-muted">Loading portfolio risk…</p>}
      {state && (
        <>
          <PortfolioDataReviewNotice count={state.portfolio_data_review_count ?? 0} />
          <div role="status" className={`rounded-lg border p-3 text-sm ${stale || !state.automation_enabled || !state.market_data_enabled ? "border-amber-500/50" : "border-line"}`}>
            <p>{!state.automation_enabled || !state.market_data_enabled ? "Background monitoring is paused in settings." : stale ? "No recent successful risk check. Refresh or inspect automation before relying on monitoring." : "Background daily-data checks are running."}</p>
            <p className="mt-1 text-muted">Last check: {date(state.last_refresh_at)}. Data capture: {date(result?.cutoff)}. Latest daily bar: {result?.latest_session_date ?? "Mixed or unavailable"}.</p>
          </div>

          {result ? (
            <>
              <div className="grid gap-4 sm:grid-cols-3">
                <Metric label={result.status === "ready" ? "Historical daily tail stress" : "Covered holdings daily tail stress"} value={money(result.historical_stress_loss ?? result.covered_stress_loss)} detail="Mean of the worst 5% of matching sample moves" />
                <Metric label={result.status === "ready" ? "Worst sampled move" : "Covered holdings worst sampled move"} value={money(result.worst_observed_loss ?? result.covered_worst_observed_loss)} detail={result.worst_session?.join(" → ") ?? "Insufficient matching history"} />
                <Metric label="Coverage" value={`${result.covered_positions}/${result.total_positions} holdings`} detail={`${result.matched_returns} matching returns · ${money(result.covered_value)} covered · cash excluded`} />
              </div>
              {result.status !== "ready" && <p className="rounded border border-amber-500/50 p-3 text-sm">Coverage is insufficient. Missing exposure is not zero risk; the budget cannot be evaluated.</p>}
              {result.issues.map(issue => <p key={issue} className="text-sm text-muted">{issue}</p>)}
              {result.excluded.length > 0 && <ul className="text-sm text-muted space-y-1">{result.excluded.map((item, index) => <li key={`${item.ticker}-${index}`}>{item.ticker}: {item.reason}</li>)}</ul>}
              {result.excluded.length > 0 && <Link href="/positions" className="inline-block text-sm underline">Review instrument identity and price coverage in Portfolio</Link>}
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <caption className="text-left text-muted pb-2">Covered holdings under the same worst sampled session. Median pair correlation: {result.median_pair_correlation ?? "Unavailable"}.</caption>
                  <thead><tr className="border-b border-line text-left"><th className="py-2 pr-4">Holding</th><th className="pr-4">Weight of covered book</th><th className="pr-4">1-session return</th><th className="pr-4">Volume / prior average</th><th>Scenario impact</th></tr></thead>
                  <tbody>{result.positions.map((position, index) => <tr key={`${position.security_id}-${index}`} className="border-b border-line"><td className="py-2 pr-4 font-medium">{position.ticker}</td><td className="pr-4">{position.weight_pct.toFixed(1)}%</td><td className="pr-4">{position.return_1d_pct == null ? "Unavailable" : `${position.return_1d_pct.toFixed(2)}%`}</td><td className="pr-4">{position.volume_ratio == null ? "Unavailable" : `${position.volume_ratio.toFixed(2)}×`}</td><td>{money(position.worst_session_impact)}</td></tr>)}</tbody>
                </table>
              </div>
              <details className="text-sm text-muted"><summary className="cursor-pointer">Assumptions, limitations and frozen inputs</summary><ul className="mt-3 list-disc pl-5 space-y-1">{result.limitations.map(item => <li key={item}>{item}</li>)}</ul><a href={`${API_BASE}/risk/portfolio/packets/${assessment!.id}`} target="_blank" rel="noreferrer" className="inline-block mt-3 underline">Inspect this assessment’s frozen packet and hash</a></details>
            </>
          ) : <p className="text-sm text-muted">No portfolio assessment yet. Check portfolio risk to collect completed daily observations.</p>}

          <div className="border-t border-line pt-5 space-y-3">
            <h3 className="font-semibold">Advisory loss-budget watch</h3>
            <p className="text-sm text-muted">Choose a USD loss budget for the invested holdings above. The watch alerts once when the historical stress exceeds it. It stays disarmed after triggering until you explicitly arm it again. No orders are placed.</p>
            {watch && <div className="rounded border border-line p-3 text-sm space-y-1"><p className="font-medium">{watch.enabled ? "Armed" : watch.status === "triggered" ? "Triggered · rearm after review" : "Disarmed"} · {money(watch.policy.loss_budget)} budget</p><p>{watch.detail ?? "Waiting for a completed risk check."}</p><p className="text-muted">Evaluated: {date(watch.last_checked_at)}. Triggered: {date(watch.triggered_at)}.</p>{watch.assessment?.assessment_id && watch.assessment.assessment_id !== assessment?.id && <a href={`${API_BASE}/risk/portfolio/packets/${watch.assessment.assessment_id}`} className="underline">Inspect the packet used for this watch decision</a>}<p className="text-muted">{state.delivery.length ? state.delivery.map(item => `${item.status}${item.delivered_at ? ` ${date(item.delivered_at)}` : ""}`).join(" · ") : "No push delivery recorded. Device notifications require opt-in in Settings."}</p></div>}
            <form onSubmit={event => { event.preventDefault(); void mutate("/risk/portfolio/watch", "PUT", { enabled: true, loss_budget: Number(budget), max_age_hours: Number(age) }); }} className="flex flex-wrap items-end gap-3">
              <label className="text-sm">Loss budget (USD)<input aria-label="Loss budget (USD)" required type="number" min="0.01" step="0.01" value={budget} onChange={event => setBudget(event.target.value)} placeholder={watch ? String(watch.policy.loss_budget) : "Choose your budget"} className="block mt-1 w-48 rounded border border-line bg-background px-3 py-2" /></label>
              <label className="text-sm">Maximum close age (hours)<input aria-label="Maximum close age (hours)" required type="number" min="1" max="168" value={age} onChange={event => setAge(event.target.value)} className="block mt-1 w-48 rounded border border-line bg-background px-3 py-2" /></label>
              <button disabled={busy || !Number.isFinite(Number(budget)) || Number(budget) <= 0} className="rounded bg-foreground text-background px-4 py-2 text-sm disabled:opacity-50">{watch?.status === "triggered" ? "Rearm advisory watch" : "Save and arm watch"}</button>
              {watch?.enabled && <button type="button" disabled={busy} onClick={() => { void mutate("/risk/portfolio/watch", "PUT", { ...watch.policy, enabled: false }); }} className="rounded border border-line px-4 py-2 text-sm disabled:opacity-50">Disarm</button>}
            </form>
            <p className="text-xs text-muted">Freshness is measured in elapsed hours, including weekends. Unsupported or stale holdings prevent a budget verdict. A historical sample cannot bound tomorrow’s loss.</p>
          </div>
        </>
      )}
    </section>
  );
}

function Metric({ label, value, detail }: { label: string; value: string; detail: string }) {
  return <div className="rounded-lg border border-line p-4"><p className="text-sm text-muted">{label}</p><p className="mt-1 text-xl font-semibold">{value}</p><p className="mt-1 text-xs text-muted">{detail}</p></div>;
}
