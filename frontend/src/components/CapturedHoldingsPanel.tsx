"use client";

import { useCallback, useEffect, useState } from "react";
import { API_BASE, type CapturedHoldingsValue } from "@/lib/api";
import { safeFormatCurrency, safeFormatSignedCurrency } from "@/lib/formatting";
import { useWorkspaceReads } from "@/lib/workspace-reads";
import CapturedValueChart from "./CapturedValueChart";
import WorkspaceState from "./WorkspaceState";

export default function CapturedHoldingsPanel({ view = "history" }: { view?: "history" | "scenario" }) {
  const { read, status } = useWorkspaceReads<"value">();
  const [captures, setCaptures] = useState<CapturedHoldingsValue[]>([]);
  const [shock, setShock] = useState(-10);
  const load = useCallback(() => read<CapturedHoldingsValue[]>("value", "/risk/portfolio/value-history", setCaptures, false), [read]);
  useEffect(() => { void load(); }, [load]);
  const latest = captures.at(-1);
  const error = status.value?.error;
  const loading = !status.value?.ready && !error;
  const scenario = view === "scenario";
  const title = scenario ? "What-if value" : "Holdings value history";

  return <section className="min-w-0 border border-line bg-panel p-4 sm:p-5" aria-label={title}>
    <div className="mb-3 flex flex-wrap items-start justify-between gap-2">
      <div>
        <h2 className="font-semibold">{title}</h2>
        <p className="mt-1 text-xs text-muted">{scenario ? "Apply a uniform price shock when the latest capture covers every holding. Assumes unchanged positions and excludes cash. No probability or time horizon is implied."
          : "Recorded holdings marked at completed closes. Excludes cash; changes can reflect trades as well as prices. Gaps indicate incomplete or unmatched prices."}</p>
      </div>
      <button type="button" className="workspace-control" disabled={loading} onClick={() => void load()}>Reload captures</button>
    </div>
    {error && <WorkspaceState compact kind={captures.length ? "degraded" : "error"} title="Captures could not be loaded" description={error} actionLabel="Retry" onAction={() => void load()} />}
    {loading ? <p className="text-sm text-muted" role="status">Loading retained captures…</p> : !latest && !error ? (
      <p className="text-sm text-muted">Prophet hasn’t recorded a holdings valuation yet. Captures appear as market data and holdings become available.</p>
    ) : latest && scenario ? (
      latest.value_usd == null ? <p className="text-sm text-muted">The latest capture covered {latest.covered_positions} of {latest.total_positions} holdings. A same-session value for the full book is unavailable.</p> : (
        <ScenarioControls value={latest.value_usd} session={latest.price_session} shock={shock} onChange={setShock} />
      )
    ) : latest ? (
      <>
        <p className="mb-2 text-xs text-muted">{captures.filter((capture) => capture.value_usd != null).length} complete of {captures.length} recent captures · Latest coverage: {latest.covered_positions}/{latest.total_positions}</p>
        <CapturedValueChart label="Holdings value" points={captures.map((capture) => ({
          id: capture.assessment_id, at: capture.captured_at, value: capture.value_usd,
          detail: `${capture.covered_positions}/${capture.total_positions} holdings priced${capture.price_session ? ` · Price session ${capture.price_session}` : " · No common price session"}`,
          href: `${API_BASE}/risk/portfolio/packets/${capture.assessment_id}`,
        }))} />
      </>
    ) : null}
  </section>;
}

function ScenarioControls({ value, session, shock, onChange }: { value: number; session: string | null; shock: number; onChange: (value: number) => void }) {
  const change = value * shock / 100;
  const adjusted = value + change;
  const max = Math.max(value, adjusted);
  return <div className="space-y-3">
    <label className="flex flex-wrap items-center gap-x-4 gap-y-2 text-sm">
      <span>Price shock <strong className="tabular-nums">{shock > 0 ? "+" : ""}{shock}%</strong></span>
      <input type="range" min={-100} max={100} step={1} value={shock} aria-valuetext={`${shock}% price shock`} onChange={(event) => onChange(Number(event.target.value))} className="min-w-24 flex-1 accent-current" />
    </label>
    <div className="flex flex-wrap gap-2">{[-20, -10, 0, 10, 20].map((step) => <button type="button" className="workspace-control aria-pressed:border-action" aria-pressed={shock === step} key={step} onClick={() => onChange(step)}>{step > 0 ? "+" : ""}{step}%</button>)}</div>
    <div className="space-y-2 border border-line bg-background p-3 text-sm tabular-nums">
      <div><p className="flex flex-wrap justify-between gap-2"><span>Captured value {session && <span className="text-xs text-muted">({session})</span>}</span><strong>{safeFormatCurrency(value, 2)}</strong></p><div className="mt-1 h-1.5 bg-action" style={{ width: `${value / max * 100}%` }} /></div>
      <div><p className="flex flex-wrap justify-between gap-2"><span>Under this shock</span><strong>{safeFormatCurrency(adjusted, 2)}</strong></p><div className={`mt-1 h-1.5 ${shock < 0 ? "bg-rose-500" : "bg-emerald-500"}`} style={{ width: `${adjusted / max * 100}%` }} /></div>
      <p className="text-xs text-muted" aria-live="polite">Change in holdings value: {safeFormatSignedCurrency(change)}</p>
    </div>
  </div>;
}
