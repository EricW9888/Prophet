"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import type { ReactNode } from "react";

import AppNav from "@/components/AppNav";
import { API_BASE, apiFetch, BenchmarkRecord, PerformanceAttribution, RiskSummary } from "@/lib/api";

export default function RiskPage() {
  const [summary, setSummary] = useState<RiskSummary | null>(null);
  const [attribution, setAttribution] = useState<PerformanceAttribution | null>(null);
  const [benchmarks, setBenchmarks] = useState<BenchmarkRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [attributionLoading, setAttributionLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [attributionError, setAttributionError] = useState<string | null>(null);
  const [windowDays, setWindowDays] = useState("21");
  const [form, setForm] = useState({
    ticker: "",
    name: "",
    description: "",
    benchmark_type: "broad_market",
  });

  async function loadState(refresh = false) {
    if (refresh) {
      setRefreshing(true);
    } else {
      setLoading(true);
    }
    try {
      const [riskSummary, benchmarkRows] = await Promise.all([
        apiFetch<RiskSummary>(`/risk/summary?refresh=${refresh ? "true" : "false"}`),
        apiFetch<BenchmarkRecord[]>("/benchmarks/"),
      ]);
      setSummary(riskSummary);
      setBenchmarks(benchmarkRows);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to load risk context.");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }

  async function loadAttribution(days: number) {
    setAttributionLoading(true);
    try {
      const result = await apiFetch<PerformanceAttribution>(
        `/risk/performance-attribution?window_days=${days}`,
      );
      setAttribution(result);
      setAttributionError(null);
    } catch (err) {
      setAttributionError(err instanceof Error ? err.message : "Unable to calculate performance attribution.");
    } finally {
      setAttributionLoading(false);
    }
  }

  useEffect(() => {
    void loadState(false);
    void loadAttribution(21);
    const interval = window.setInterval(() => {
      void loadState(false);
    }, 20000);
    return () => window.clearInterval(interval);
  }, []);

  async function createBenchmark(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      const response = await fetch(`${API_BASE}/benchmarks/`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ticker: form.ticker,
          name: form.name || null,
          description: form.description || null,
          benchmark_type: form.benchmark_type,
        }),
      });
      if (!response.ok) {
        throw new Error(await response.text());
      }
      setForm({
        ticker: "",
        name: "",
        description: "",
        benchmark_type: "broad_market",
      });
      await loadState(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to create benchmark.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="min-h-screen bg-background text-foreground font-sans">
      <AppNav active="risk" />
      <main className="mx-auto w-full max-w-[1440px] space-y-8 px-4 py-8 sm:px-6 lg:px-8">
        <header className="flex flex-col gap-2 md:flex-row md:items-end md:justify-between">
          <div>
            <h1 className="text-3xl font-bold tracking-tight">Risk + Benchmark</h1>
            <p className="text-slate-500 dark:text-slate-400">
              Benchmark-relative context, concentration, regime, and scenario impact for the live portfolio.
            </p>
          </div>
          <button onClick={() => { void loadState(true); void loadAttribution(Number(windowDays) || 21); }} disabled={refreshing} className="rounded-lg border border-slate-300 dark:border-slate-700 px-4 py-3 text-sm disabled:opacity-50">
            {refreshing ? "Refreshing..." : "Refresh risk snapshot"}
          </button>
        </header>

        {error ? (
          <div className="rounded-lg border border-red-200 bg-white px-4 py-3 text-sm text-red-600 dark:border-red-900 dark:bg-slate-950 dark:text-red-400">
            {error}
          </div>
        ) : null}

        {loading || !summary ? (
          <div className="rounded-lg border border-slate-200 bg-white p-6 dark:border-slate-800 dark:bg-slate-950 text-sm text-slate-500 animate-pulse">
            Loading benchmark context...
          </div>
        ) : (
          <>
            <section className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-5 gap-4">
              <MetricCard label="Benchmark" value={summary.active_benchmark?.ticker ?? "n/a"} detail={summary.active_benchmark?.name ?? "No benchmark set"} />
              <MetricCard label="Open Cost-Basis Return" value={formatPct(summary.portfolio_return_pct)} detail="Unrealized return on current holdings" />
              <MetricCard label="Top Holding" value={summary.top_holding ?? "n/a"} detail={`${summary.top_holding_weight_pct.toFixed(1)}% weight`} />
              <MetricCard label="Top Sector" value={summary.top_sector ?? "n/a"} detail={`${summary.top_sector_weight_pct.toFixed(1)}% weight`} />
              <MetricCard label="Regime" value={summary.current_regime?.regime_type ?? "pending"} detail={summary.current_regime ? `${(summary.current_regime.confidence * 100).toFixed(0)}% confidence` : "Refresh to compute"} />
            </section>

            <PerformanceAttributionPanel
              attribution={attribution}
              loading={attributionLoading}
              error={attributionError}
              windowDays={windowDays}
              onWindowDaysChange={setWindowDays}
              onAnalyze={() => {
                const days = Math.max(1, Math.min(1825, Number(windowDays) || 21));
                setWindowDays(String(days));
                void loadAttribution(days);
              }}
            />

            <section className="grid grid-cols-1 xl:grid-cols-[1.05fr_0.95fr] gap-6">
              <Panel title="Exposure Map" eyebrow="Current concentration">
                <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                  <ExposureList title="Top positions" items={summary.top_positions} />
                  <ExposureList title="Sector exposures" items={summary.sector_exposures} />
                  <ExposureList title="Asset classes" items={summary.asset_class_exposures} />
                </div>
                <div className="mt-4 rounded-lg border border-slate-200 dark:border-slate-800 p-4">
                  <p className="text-xs uppercase tracking-wider text-slate-400">Concentration stats</p>
                  <p className="mt-2 text-sm text-slate-700 dark:text-slate-300">
                    Top sector {summary.top_sector ?? "n/a"}{summary.top_sector ? ` · ${summary.top_sector_weight_pct.toFixed(1)}%` : ""}
                  </p>
                  <p className="mt-1 text-sm text-slate-700 dark:text-slate-300">
                    HHI {summary.concentration_hhi.toFixed(0)}
                  </p>
                </div>
              </Panel>

              <Panel title="Regime + Scenarios" eyebrow="Portfolio impact framing">
                <div className="rounded-lg border border-slate-200 dark:border-slate-800 p-4">
                  <p className="text-xs uppercase tracking-wider text-slate-400">Current regime</p>
                  <p className="mt-2 text-lg font-semibold tracking-tight">
                    {summary.current_regime?.regime_type ?? "Not computed"}
                  </p>
                  <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
                    {summary.current_regime
                      ? `${(summary.current_regime.confidence * 100).toFixed(0)}% confidence · ${summary.current_regime.signal_source}`
                      : "Refresh the risk snapshot to infer regime state from the benchmark."}
                  </p>
                </div>
                <div className="mt-4 space-y-3">
                  {summary.scenarios.map((scenario) => (
                    <div key={scenario.name} className="rounded-lg border border-slate-200 dark:border-slate-800 p-4">
                      <div className="flex items-center justify-between gap-4">
                        <p className="font-medium">{scenario.name.replace("AUTO: ", "")}</p>
                        <span className={`text-sm ${scenario.total_portfolio_impact < 0 ? "text-red-500" : "text-emerald-500"}`}>
                          {formatCurrency(scenario.total_portfolio_impact)}
                        </span>
                      </div>
                      <p className="mt-2 text-sm text-slate-500 dark:text-slate-400">{scenario.scenario_description}</p>
                    </div>
                  ))}
                </div>
              </Panel>
            </section>
          </>
        )}

        <section className="grid grid-cols-1 xl:grid-cols-[0.9fr_1.1fr] gap-6">
          <Panel title="Benchmark Catalog" eyebrow="Managed comparison set">
            <div className="mb-4 flex items-start justify-between gap-4">
              <p className="text-sm text-slate-500 dark:text-slate-400">
                Add or reuse the comparison baselines you want the system to reason against.
              </p>
            </div>
            <form onSubmit={createBenchmark} className="space-y-4">
              <input value={form.ticker} onChange={(e) => setForm((current) => ({ ...current, ticker: e.target.value.toUpperCase() }))} placeholder="Ticker" className={inputClass} />
              <input value={form.name} onChange={(e) => setForm((current) => ({ ...current, name: e.target.value }))} placeholder="Name" className={inputClass} />
              <select value={form.benchmark_type} onChange={(e) => setForm((current) => ({ ...current, benchmark_type: e.target.value }))} className={inputClass}>
                <option value="broad_market">Broad market</option>
                <option value="sector">Sector</option>
                <option value="factor">Factor</option>
                <option value="custom">Custom</option>
              </select>
              <textarea value={form.description} onChange={(e) => setForm((current) => ({ ...current, description: e.target.value }))} rows={3} placeholder="Why this benchmark matters" className={inputClass} />
              <button disabled={saving || !form.ticker.trim()} className="rounded-lg bg-sky-600 px-4 py-3 text-white disabled:opacity-50">
                {saving ? "Saving..." : "Add benchmark"}
              </button>
            </form>
          </Panel>

          <Panel title="Known Benchmarks" eyebrow="Reference universe">
            <div className="space-y-3">
              {benchmarks.length === 0 ? (
                <p className="text-sm text-slate-500 dark:text-slate-400">No benchmarks saved yet.</p>
              ) : (
                benchmarks.map((benchmark) => (
                  <div key={benchmark.id} className="rounded-lg border border-slate-200 dark:border-slate-800 p-4">
                    <div className="flex items-center justify-between gap-4">
                      <p className="font-medium">{benchmark.name}</p>
                      <span className="text-xs uppercase tracking-wider text-slate-400">{benchmark.benchmark_type}</span>
                    </div>
                    <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
                      {benchmark.ticker ?? "custom"} · {new Date(benchmark.created_at).toLocaleDateString()}
                    </p>
                    {benchmark.description ? (
                      <p className="mt-2 text-sm text-slate-700 dark:text-slate-300">{benchmark.description}</p>
                    ) : null}
                  </div>
                ))
              )}
            </div>
            <p className="mt-4 text-sm text-slate-500 dark:text-slate-400">
              Select the active benchmark in <Link href="/setup/integrations" className="text-sky-600 dark:text-sky-400">Settings</Link>.
            </p>
          </Panel>
        </section>
      </main>
    </div>
  );
}

function PerformanceAttributionPanel({
  attribution,
  loading,
  error,
  windowDays,
  onWindowDaysChange,
  onAnalyze,
}: {
  attribution: PerformanceAttribution | null;
  loading: boolean;
  error: string | null;
  windowDays: string;
  onWindowDaysChange: (value: string) => void;
  onAnalyze: () => void;
}) {
  const losses = attribution?.items.filter((item) => item.gain < 0).slice(0, 6) ?? [];
  const gains = attribution?.items.filter((item) => item.gain > 0).slice(-6).reverse() ?? [];
  const sectors = summarizeSectors(attribution?.items ?? []);

  return (
    <section className="border-y border-slate-200 py-6 dark:border-slate-800">
      <div className="flex flex-col gap-4 md:flex-row md:items-end md:justify-between">
        <div>
          <p className="text-sm font-medium text-slate-500 dark:text-slate-400">Measured performance</p>
          <h2 className="mt-1 text-xl font-semibold">What moved the invested book</h2>
          <p className="mt-1 max-w-3xl text-sm text-slate-500 dark:text-slate-400">
            Dated price changes reconciled with settled buys, sells, dividends, and splits. This is separate from the open-position cost-basis figure above.
          </p>
        </div>
        <div className="flex items-end gap-2">
          <label className="block">
            <span className="text-xs font-medium uppercase text-slate-500">Calendar days</span>
            <input
              type="number"
              min={1}
              max={1825}
              value={windowDays}
              onChange={(event) => onWindowDaysChange(event.target.value)}
              className="mt-1 w-28 rounded-md border border-slate-300 bg-white px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-900"
            />
          </label>
          <button onClick={onAnalyze} disabled={loading} className="rounded-md bg-sky-600 px-4 py-2 text-sm font-medium text-white disabled:opacity-50">
            {loading ? "Calculating..." : "Analyze"}
          </button>
        </div>
      </div>

      {error ? <p className="mt-4 text-sm text-red-600 dark:text-red-400">{error}</p> : null}
      {loading && !attribution ? <p className="mt-6 text-sm text-slate-500">Calculating cash-flow-aware attribution...</p> : null}

      {attribution ? (
        <>
          <div className="mt-6 grid grid-cols-2 gap-x-6 gap-y-4 border-y border-slate-200 py-5 md:grid-cols-5 dark:border-slate-800">
            <AttributionStat label="Invested return" value={formatPct(attribution.return_pct)} />
            <AttributionStat label="Gain / loss" value={formatCurrency(attribution.gain)} tone={attribution.gain} />
            <AttributionStat label="Net flows" value={formatCurrency(attribution.net_flow)} />
            <AttributionStat label={`Versus ${attribution.benchmark_ticker ?? "benchmark"}`} value={formatPct(attribution.active_return_pct)} tone={attribution.active_return_pct} />
            <AttributionStat label="Coverage" value={`${attribution.coverage_pct.toFixed(0)}%`} detail={`${attribution.covered_positions}/${attribution.total_positions} securities`} />
          </div>

          <div className="mt-6 grid grid-cols-1 gap-8 lg:grid-cols-3">
            <AttributionList title="Largest drags" items={losses} empty="No measured losses in this window." />
            <AttributionList title="Largest gains" items={gains} empty="No measured gains in this window." />
            <div>
              <h3 className="text-sm font-semibold">Sector contribution</h3>
              <div className="mt-3 divide-y divide-slate-200 border-y border-slate-200 dark:divide-slate-800 dark:border-slate-800">
                {sectors.map((sector) => (
                  <div key={sector.label} className="flex items-center justify-between gap-4 py-3 text-sm">
                    <span>{sector.label}</span>
                    <span className={sector.gain < 0 ? "text-red-600 dark:text-red-400" : "text-emerald-600 dark:text-emerald-400"}>
                      {formatCurrency(sector.gain)}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          </div>

          <div className="mt-5 text-xs leading-5 text-slate-500 dark:text-slate-400">
            <p>
              Window {new Date(attribution.period_start).toLocaleDateString()} through {new Date(attribution.as_of).toLocaleDateString()}. {attribution.method}
            </p>
            {attribution.unavailable_tickers.length > 0 ? (
              <p className="mt-1 text-amber-700 dark:text-amber-400">
                Excluded pending complete price or corporate-action history: {attribution.unavailable_tickers.join(", ")}.
              </p>
            ) : null}
          </div>
        </>
      ) : null}
    </section>
  );
}

function AttributionStat({ label, value, detail, tone }: { label: string; value: string; detail?: string; tone?: number | null }) {
  const toneClass = tone == null ? "" : tone < 0 ? "text-red-600 dark:text-red-400" : tone > 0 ? "text-emerald-600 dark:text-emerald-400" : "";
  return (
    <div>
      <p className="text-xs uppercase text-slate-500">{label}</p>
      <p className={`mt-1 text-xl font-semibold ${toneClass}`}>{value}</p>
      {detail ? <p className="mt-1 text-xs text-slate-500">{detail}</p> : null}
    </div>
  );
}

function AttributionList({ title, items, empty }: { title: string; items: PerformanceAttribution["items"]; empty: string }) {
  return (
    <div>
      <h3 className="text-sm font-semibold">{title}</h3>
      <div className="mt-3 divide-y divide-slate-200 border-y border-slate-200 dark:divide-slate-800 dark:border-slate-800">
        {items.length === 0 ? <p className="py-3 text-sm text-slate-500">{empty}</p> : items.map((item) => (
          <div key={item.ticker} className="flex items-center justify-between gap-4 py-3">
            <div className="min-w-0">
              <p className="text-sm font-medium">{item.ticker}</p>
              <p className="truncate text-xs text-slate-500">{item.name} · {item.contribution_pct.toFixed(2)} pts</p>
            </div>
            <div className="text-right">
              <p className={`text-sm font-medium ${item.gain < 0 ? "text-red-600 dark:text-red-400" : "text-emerald-600 dark:text-emerald-400"}`}>
                {formatCurrency(item.gain)}
              </p>
              <p className="text-xs text-slate-500">{formatPct(item.capital_return_pct)} on capital</p>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function summarizeSectors(items: PerformanceAttribution["items"]) {
  const totals = new Map<string, number>();
  for (const item of items) {
    totals.set(item.sector, (totals.get(item.sector) ?? 0) + item.gain);
  }
  return Array.from(totals, ([label, gain]) => ({ label, gain })).sort((left, right) => left.gain - right.gain);
}

function Panel({
  title,
  eyebrow,
  children,
}: {
  title: string;
  eyebrow: string;
  children: ReactNode;
}) {
  return (
    <section className="rounded-lg border border-slate-200 bg-white p-6 dark:border-slate-800 dark:bg-slate-950">
      <p className="text-sm font-medium text-slate-500 dark:text-slate-400">{eyebrow}</p>
      <h2 className="mt-1 text-xl font-semibold tracking-tight">{title}</h2>
      <div className="mt-4">{children}</div>
    </section>
  );
}

function MetricCard({ label, value, detail }: { label: string; value: string; detail: string }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-5 dark:border-slate-800 dark:bg-slate-950">
      <p className="text-sm font-medium text-slate-500 dark:text-slate-400">{label}</p>
      <p className="mt-2 text-3xl font-semibold tracking-tight">{value}</p>
      <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">{detail}</p>
    </div>
  );
}

function ExposureList({ title, items }: { title: string; items: RiskSummary["sector_exposures"] }) {
  return (
    <div className="rounded-lg border border-slate-200 dark:border-slate-800 p-4">
      <p className="text-xs uppercase tracking-wider text-slate-400">{title}</p>
      <div className="mt-3 space-y-2">
        {items.length === 0 ? (
          <p className="text-sm text-slate-500 dark:text-slate-400">No exposure data yet.</p>
        ) : (
          items.map((item) => (
            <div key={item.label} className="flex items-center justify-between gap-4 text-sm">
              <span className="text-slate-700 dark:text-slate-300">{item.label}</span>
              <span className="font-medium">{item.weight_pct.toFixed(1)}%</span>
            </div>
          ))
        )}
      </div>
    </div>
  );
}

function formatPct(value?: number | null) {
  if (value == null) {
    return "n/a";
  }
  const prefix = value > 0 ? "+" : "";
  return `${prefix}${value.toFixed(2)}%`;
}

function formatCurrency(value: number) {
  const prefix = value > 0 ? "+" : value < 0 ? "-" : "";
  return `${prefix}$${Math.abs(value).toFixed(2)}`;
}

const inputClass =
  "w-full rounded-lg border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-900 px-4 py-3";
