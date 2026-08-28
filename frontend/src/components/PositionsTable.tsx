"use client";

import { type ReactNode, useCallback, useEffect, useState } from "react";

import AddResearchObjectModal from "./AddResearchObjectModal";
import AddTransactionModal from "./AddTransactionModal";
import WorkspaceState from "./WorkspaceState";
import { apiFetch, PortfolioBuildPoint, PortfolioOverview } from "@/lib/api";
import { safeFormatCurrency, safeFormatSignedCurrency, formatUserLabel } from "@/lib/formatting";

export default function PositionsTable() {
  const [overview, setOverview] = useState<PortfolioOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [isTransactionModalOpen, setIsTransactionModalOpen] = useState(false);
  const [isResearchModalOpen, setIsResearchModalOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showBuildHistory, setShowBuildHistory] = useState(false);
  const [showRecentChanges, setShowRecentChanges] = useState(false);

  const loadOverview = useCallback(async () => {
    try {
      const data = await apiFetch<PortfolioOverview>("/portfolio/overview");
      setOverview(data);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to load portfolio workspace.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadOverview();
    const interval = window.setInterval(() => {
      void loadOverview();
    }, 20000);
    return () => window.clearInterval(interval);
  }, [loadOverview]);

  function handleResearchSaved() {
    void loadOverview();
  }

  const holdings = overview?.holdings ?? [];
  const watchlist = overview?.watchlist ?? [];
  const considering = overview?.considering ?? [];

  if (loading && !overview) {
    return (
      <WorkspaceState
        kind="loading"
        title="Loading the portfolio ledger"
        description="Prophet is reading current holdings, tracked names, and recent transactions."
        className="min-h-64"
      />
    );
  }

  if (!overview) {
    return (
      <WorkspaceState
        kind="error"
        title="Portfolio ledger unavailable"
        description={error ?? "Prophet could not load the current portfolio ledger."}
        actionLabel="Retry"
        onAction={() => {
          setLoading(true);
          void loadOverview();
        }}
        className="min-h-64"
      />
    );
  }

  return (
    <>
      <AddTransactionModal isOpen={isTransactionModalOpen} onClose={() => setIsTransactionModalOpen(false)} onSaved={loadOverview} />
      <AddResearchObjectModal isOpen={isResearchModalOpen} onClose={() => setIsResearchModalOpen(false)} onSaved={handleResearchSaved} />

      <section className="space-y-6">
        {error ? (
          <WorkspaceState
            kind="degraded"
            title="Portfolio refresh is delayed"
            description={`${error} The last successfully loaded ledger remains visible.`}
            actionLabel="Retry refresh"
            onAction={() => void loadOverview()}
            compact
          />
        ) : null}
        <div className="grid min-w-0 gap-6 2xl:grid-cols-[1.35fr_0.65fr]">
          <section className="min-w-0 rounded-lg border border-slate-200 bg-white p-4 sm:p-6 dark:border-slate-800 dark:bg-slate-950">
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div>
                <p className="text-sm font-medium text-slate-500 dark:text-slate-400">Live holdings</p>
                <h2 className="mt-1 text-2xl font-semibold tracking-tight">
                  {`${holdings.length} active holdings`}
                </h2>
                <p className="mt-2 text-sm text-slate-500 dark:text-slate-400">
                  The book comes first. Track holdings here, then let research objects orbit around the real portfolio.
                </p>
              </div>
              <div className="flex flex-wrap gap-2">
                <button onClick={() => setIsResearchModalOpen(true)} className="rounded-md border border-slate-300 px-3 py-2 text-sm font-medium transition-colors hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-900">
                  Track name
                </button>
                <button onClick={() => setIsTransactionModalOpen(true)} className="rounded-md bg-sky-600 px-3 py-2 text-sm font-medium text-white transition-colors hover:bg-sky-700">
                  Add holding
                </button>
              </div>
            </div>

            {holdings.length === 0 ? (
              <WorkspaceState
                kind="empty"
                title="No active holdings"
                description="Add a transaction here or import transaction history from Settings to establish the live book."
                actionLabel="Add holding"
                onAction={() => setIsTransactionModalOpen(true)}
                compact
                className="mt-6"
              />
            ) : (
              <div className="mt-6 max-w-full overflow-x-auto">
                <table className="min-w-[620px] w-full text-sm text-left">
                  <thead className="text-xs uppercase text-slate-500 dark:text-slate-400">
                    <tr>
                      <th className="pb-3 font-medium">Holding</th>
                      <th className="pb-3 font-medium text-right">Shares</th>
                      <th className="pb-3 font-medium text-right">Avg Cost</th>
                      <th className="pb-3 font-medium text-right">Last</th>
                      <th className="pb-3 font-medium text-right">Market Value</th>
                      <th className="pb-3 font-medium text-right">P&L</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-200 dark:divide-slate-800">
                    {holdings.map((pos) => (
                      <tr key={pos.id}>
                        <td className="py-4">
                          <div className="font-semibold text-slate-900 dark:text-slate-100">{pos.ticker ?? pos.security_id}</div>
                          {pos.entity_name ? (
                            <div className="text-xs text-slate-500 dark:text-slate-400">{pos.entity_name}</div>
                          ) : null}
                        </td>
                        <td className="py-4 text-right tabular-nums">{(pos.quantity || 0).toFixed(2)}</td>
                        <td className="py-4 text-right tabular-nums">{safeFormatCurrency(pos.avg_cost_basis, 2)}</td>
                        <td className="py-4 text-right tabular-nums">{safeFormatCurrency(pos.current_price, 2)}</td>
                        <td className="py-4 text-right tabular-nums">{safeFormatCurrency(pos.market_value, 2)}</td>
                        <td className={`py-4 text-right tabular-nums ${(pos.unrealized_pnl || 0) >= 0 ? "text-emerald-600" : "text-rose-500"}`}>
                          {safeFormatSignedCurrency(pos.unrealized_pnl)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section className="space-y-6">
            <SidebarList
              title="Watchlist"
              description="Tracked names with profiles, questions, and evidence collection."
              items={watchlist}
              emptyState="No watchlist names yet."
            />
            <SidebarList
              title="Considering"
              description="Ideas under active consideration before they become positions."
              items={considering}
              emptyState="Nothing in active consideration yet."
            />
          </section>
        </div>

        <section className="grid gap-6 2xl:grid-cols-[1fr_0.8fr]">
          <CollapsiblePanel
            eyebrow="Portfolio build"
            title="Transaction-based history"
            summary={
              overview?.build_series?.length
                ? `${overview.build_series[overview.build_series.length - 1]?.transaction_count ?? 0} recorded transactions across ${(overview?.build_series ?? []).length} portfolio snapshots.`
                : "No transaction history yet."
            }
            open={showBuildHistory}
            onToggle={() => setShowBuildHistory((current) => !current)}
          >
            <div className="mt-5">
              <PortfolioBuildChart points={overview?.build_series ?? []} />
            </div>
          </CollapsiblePanel>

          <CollapsiblePanel
            eyebrow="Recent book changes"
            title={`${(overview?.recent_transactions ?? []).length} recent transactions`}
            summary="Expand to inspect the latest trades without leaving the live book."
            open={showRecentChanges}
            onToggle={() => setShowRecentChanges((current) => !current)}
          >
            <div className="mt-4 space-y-3">
              {(overview?.recent_transactions ?? []).length === 0 ? (
                <p className="text-sm text-slate-500 dark:text-slate-400">No transactions yet.</p>
              ) : (
                overview?.recent_transactions.map((txn) => (
                  <div key={txn.id} className="rounded-lg border border-slate-200 p-4 text-sm dark:border-slate-800">
                    <div className="flex items-center justify-between gap-4">
                      <p className="font-medium uppercase">{formatUserLabel(txn.action)}</p>
                      <span className="text-xs text-slate-400">
                        {new Date(txn.executed_at).toLocaleDateString()}
                      </span>
                    </div>
                    <p className="mt-1 text-slate-700 dark:text-slate-300">
                      {(txn.quantity || 0).toFixed(2)} shares
                      {txn.price != null ? ` @ ${safeFormatCurrency(txn.price, 2)}` : ""}
                    </p>
                    {txn.notes ? (
                      <p className="mt-2 text-slate-500 dark:text-slate-400">{txn.notes}</p>
                    ) : null}
                  </div>
                ))
              )}
            </div>
          </CollapsiblePanel>
        </section>
      </section>
    </>
  );
}

function CollapsiblePanel({
  eyebrow,
  title,
  summary,
  open,
  onToggle,
  children,
}: {
  eyebrow: string;
  title: string;
  summary: string;
  open: boolean;
  onToggle: () => void;
  children: ReactNode;
}) {
  return (
    <section className="rounded-lg border border-slate-200 bg-white p-6 dark:border-slate-800 dark:bg-slate-950">
      <button type="button" onClick={onToggle} className="flex w-full items-start justify-between gap-4 text-left">
        <div>
          <p className="text-sm font-medium text-slate-500 dark:text-slate-400">{eyebrow}</p>
          <h3 className="mt-1 text-xl font-semibold tracking-tight">{title}</h3>
          <p className="mt-2 text-sm text-slate-500 dark:text-slate-400">{summary}</p>
        </div>
        <span className="rounded-full border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-600 dark:border-slate-700 dark:text-slate-300">
          {open ? "Hide" : "Expand"}
        </span>
      </button>
      {open ? children : null}
    </section>
  );
}

function SidebarList({
  title,
  description,
  items,
  emptyState,
}: {
  title: string;
  description: string;
  items: PortfolioOverview["watchlist"];
  emptyState: string;
}) {
  return (
    <section className="rounded-lg border border-slate-200 bg-white p-6 dark:border-slate-800 dark:bg-slate-950">
      <p className="text-sm font-medium text-slate-500 dark:text-slate-400">{title}</p>
      <p className="mt-2 text-sm text-slate-500 dark:text-slate-400">{description}</p>
      <div className="mt-4 space-y-3">
        {items.length === 0 ? (
          <p className="text-sm text-slate-500 dark:text-slate-400">{emptyState}</p>
        ) : (
          items.slice(0, 8).map((item) => (
            <div key={item.id} className="rounded-lg border border-slate-200 px-4 py-3 dark:border-slate-800">
              <p className="font-medium text-slate-900 dark:text-slate-100">{item.ticker ?? item.security_id}</p>
              {item.entity_name ? (
                <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">{item.entity_name}</p>
              ) : null}
              {item.conviction != null ? (
                <p className="mt-1 text-xs uppercase tracking-wider text-slate-400">conviction {item.conviction}/5</p>
              ) : null}
            </div>
          ))
        )}
      </div>
    </section>
  );
}

function PortfolioBuildChart({ points }: { points: PortfolioBuildPoint[] }) {
  if (points.length === 0) {
    return <p className="text-sm text-slate-500 dark:text-slate-400">No transaction history yet.</p>;
  }

  const maxNet = Math.max(...points.map((point) => point.net_capital_deployed), 1);
  const width = 640;
  const height = 220;
  const padding = 24;
  const chartWidth = width - padding * 2;
  const chartHeight = height - padding * 2;
  const path = points
    .map((point, index) => {
      const x = padding + (index / Math.max(points.length - 1, 1)) * chartWidth;
      const y = height - padding - (point.net_capital_deployed / maxNet) * chartHeight;
      return `${index === 0 ? "M" : "L"} ${x} ${y}`;
    })
    .join(" ");
  const latest = points[points.length - 1];

  return (
    <div className="space-y-4">
      <svg viewBox={`0 0 ${width} ${height}`} className="w-full rounded-lg border border-slate-200 bg-slate-50 dark:border-slate-800 dark:bg-slate-900">
        <path d={path} fill="none" stroke="currentColor" strokeWidth="3" className="text-sky-600 dark:text-sky-400" />
      </svg>
      <div className="grid gap-4 md:grid-cols-3">
        <Metric label="Net capital deployed" value={safeFormatCurrency(latest.net_capital_deployed, 2)} />
        <Metric label="Gross traded notional" value={safeFormatCurrency(latest.gross_trade_notional, 2)} />
        <Metric label="Recorded transactions" value={`${latest.transaction_count}`} />
      </div>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-slate-200 p-4 dark:border-slate-800">
      <p className="text-xs uppercase tracking-wider text-slate-400">{label}</p>
      <p className="mt-1 text-xl font-semibold tracking-tight">{value}</p>
    </div>
  );
}
