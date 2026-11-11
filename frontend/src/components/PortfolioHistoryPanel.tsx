"use client";

import { useCallback, useEffect, useState } from "react";
import { differenceInCalendarDays, subMonths } from "date-fns";
import { API_BASE, type PortfolioHistory } from "@/lib/api";
import { safeFormatSignedCurrency } from "@/lib/formatting";
import { useWorkspaceReads } from "@/lib/workspace-reads";
import { PORTFOLIO_HISTORY_REQUEST_TIMEOUT_MS } from "@/lib/request-timeouts";
import CapturedValueChart from "./CapturedValueChart";
import HintMarker from "./HintMarker";
import WorkspaceState from "./WorkspaceState";

const ranges = [["1W", null], ["1M", 1], ["3M", 3], ["6M", 6], ["1Y", 12]] as const;

export default function PortfolioHistoryPanel() {
  const [period, setPeriod] = useState<(typeof ranges)[number][0]>("1M");
  const months = ranges.find(([label]) => label === period)![1];
  const today = new Date();
  const days = months === null ? 7 : differenceInCalendarDays(today, subMonths(today, months));
  const [metric, setMetric] = useState<"value" | "gain">("value");
  const [{ history, warning: refreshWarning }, setDisplay] = useState<{ history: PortfolioHistory | null; warning: string }>({ history: null, warning: "" });
  const [updating, setUpdating] = useState(false);
  const { read, status } = useWorkspaceReads<"history">();
  const load = useCallback((refresh = false) =>
    read<PortfolioHistory>("history", `/risk/portfolio/history?window_days=${days}&refresh=${refresh}`, (result) => {
      setUpdating(false);
      setDisplay((previous) => {
        if (previous.history && previous.history.window_days === result.window_days && result.points.length === 0 && result.reasons.length) {
          return { ...previous, warning: result.reasons.join(" ") };
        }
        return { history: result, warning: "" };
      });
    }, true, PORTFOLIO_HISTORY_REQUEST_TIMEOUT_MS), [days, read]);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    if (history?.cache_hit && history.window_days === days) void load(true);
  }, [history, days, load]);
  const latest = history?.points.at(-1);
  const baseline = history?.points.find((point) => point.value != null);
  const error = status.history?.error;
  const busy = !error && !refreshWarning && (updating || Boolean(history?.cache_hit));
  const date = (at: string) => new Date(at).toLocaleDateString(undefined, { dateStyle: "medium", timeZone: "UTC" });

  return <section className="min-w-0 space-y-3 border border-line bg-panel p-4 sm:p-5" aria-label="Portfolio performance">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div className="flex items-center gap-2">
        <h2 className="font-semibold">Portfolio performance</h2>
        <HintMarker label="About portfolio performance">
          Historical holdings value is reconstructed from recorded trades and daily prices downloaded at the calculation time. Cash is excluded. Gain and loss account for buys, sells and dividends over the displayed period. Missing prices or an unverified share basis create gaps. Open the calculation receipt to inspect its inputs.
        </HintMarker>
      </div>
      <button type="button" disabled={busy} onClick={() => { setUpdating(true); void load(true); }} className="workspace-control">{busy ? "Updating…" : "Refresh prices"}</button>
    </div>
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div className="flex flex-wrap gap-1" role="group" aria-label="History range">
        {ranges.map(([label]) => <button key={label} type="button" className="workspace-control aria-pressed:border-action aria-pressed:text-action" aria-pressed={period === label} onClick={() => { if (period !== label) { setUpdating(true); setPeriod(label); } }}>{label}</button>)}
      </div>
      <div className="flex gap-1" role="group" aria-label="Chart metric">
        <button type="button" className="workspace-control aria-pressed:border-action" aria-pressed={metric === "value"} onClick={() => setMetric("value")}>Holdings value</button>
        <button type="button" className="workspace-control aria-pressed:border-action" aria-pressed={metric === "gain"} onClick={() => setMetric("gain")}>Gain / loss</button>
      </div>
    </div>
    {error && <WorkspaceState compact kind={history ? "degraded" : "error"} title="History could not be refreshed" description={error} actionLabel="Retry" onAction={() => void load(true)} />}
    {refreshWarning && <p role="status" className="text-sm text-amber-400">Showing the previous calculation. {refreshWarning}</p>}
    {!history && !error && <p role="status" className="text-sm text-muted">Loading historical prices and portfolio activity…</p>}
    {history && <>
      <div className="flex flex-wrap items-baseline justify-between gap-x-5 gap-y-1 text-sm">
        <p>{latest?.gain != null ? <><strong className="tabular-nums">{safeFormatSignedCurrency(latest.gain)}</strong>{latest.return_pct != null && <span className="ml-2 tabular-nums">{latest.return_pct > 0 ? "+" : ""}{latest.return_pct.toFixed(2)}%</span>}<span className="ml-2 text-muted">since {baseline ? date(baseline.at) : "the first complete value"}</span></> : "Gain and loss are unavailable for the latest close."}</p>
        <p className="text-muted">{history.points.length > 0 && `${date(history.points[0].at)} – ${date(history.points.at(-1)!.at)} · `}<time dateTime={history.computed_at}>Calculated {new Date(history.computed_at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}</time></p>
      </div>
      {history.reasons.length > 0 && <p className="text-sm text-amber-400">{history.reasons.join(" ")}</p>}
      <CapturedValueChart label={metric === "value" ? "Holdings value" : "Holdings gain and loss"} eventsLabel="Portfolio activity"
        points={history.points.map((point) => ({
          id: point.at, at: point.at, value: metric === "value" ? point.value : point.gain,
          detail: [`${point.covered_positions}/${point.total_positions} holdings priced`, ...point.reasons, point.unavailable_tickers.length ? `Missing ${point.unavailable_tickers.join(", ")}` : ""].filter(Boolean).join(" · "),
          href: `${API_BASE}/risk/portfolio/history/${history.receipt_id}`, receiptLabel: "Open calculation receipt",
        }))}
        events={history.events.map((event) => ({
          ...event, detail: event.detail ?? undefined,
          receiptLabel: event.kind === "research" ? "Open evidence" : event.kind === "alert" ? "Open alert record" : "Open ledger receipt",
          appReceipt: !event.href.startsWith("/api_proxy/"),
        }))} />
      {history.events_truncated && <p className="text-sm text-muted">Showing the latest 250 events of each type for this period.</p>}
    </>}
  </section>;
}
