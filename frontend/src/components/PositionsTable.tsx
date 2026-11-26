"use client";

import { type ReactNode, useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";

import AddResearchObjectModal from "./AddResearchObjectModal";
import AddTransactionModal from "./AddTransactionModal";
import PortfolioWatchlist from "./PortfolioWatchlist";
import ValuationCoverage from "./ValuationCoverage";
import HintMarker from "./HintMarker";
import WorkspaceState, { WorkspaceRead, WorkspaceSkeleton } from "./WorkspaceState";
import type { PortfolioOverview, Position } from "@/lib/api";
import { buildPositionRows, latestQuoteTime, sortPositionRows, type PositionSortKey, type SortDirection } from "@/lib/positions-table";
import { useWorkspaceReads } from "@/lib/workspace-reads";
import { safeFormatCurrency, safeFormatPct, safeFormatSignedCurrency, safeFormatQuantity, formatUserLabel } from "@/lib/formatting";
import { researchHref } from "@/lib/workflows";

export default function PositionsTable() {
  const [overview, setOverview] = useState<PortfolioOverview | null>(null);
  const [isTransactionModalOpen, setIsTransactionModalOpen] = useState(false);
  const [isResearchModalOpen, setIsResearchModalOpen] = useState(false);
  const [showRecentChanges, setShowRecentChanges] = useState(false);

  const { read, status } = useWorkspaceReads<"ledger">();
  const loadOverview = useCallback((refresh = true, replace = true) =>
    read<PortfolioOverview>("ledger", `/portfolio/overview?refresh=${refresh}`, setOverview, replace), [read]);

  useEffect(() => {
    let active = true;
    // Show retained holdings before waiting for the optional upstream quote refresh.
    void loadOverview(false).then(() => { if (active) void loadOverview(true, false); });
    const interval = window.setInterval(() => {
      if (!document.hidden) void loadOverview(true, false);
    }, 20000);
    return () => {
      active = false;
      window.clearInterval(interval);
    };
  }, [loadOverview]);

  function handleResearchSaved() {
    void loadOverview(false);
  }

  const holdings = overview?.holdings ?? [];
  const watchlist = overview?.watchlist ?? [];
  const considering = overview?.considering ?? [];

  if (!overview) {
    return <WorkspaceRead name="portfolio ledger" state={status.ledger} retry={() => void loadOverview(false)} placeholder={
      <section className="border border-line bg-panel p-4 sm:p-6">
        <h2 className="mb-4 text-2xl font-semibold tracking-tight">Live holdings</h2>
        <WorkspaceSkeleton label="Loading holdings" rows={4} />
      </section>
    }>{null}</WorkspaceRead>;
  }

  return (
    <>
      <AddTransactionModal isOpen={isTransactionModalOpen} onClose={() => setIsTransactionModalOpen(false)} onSaved={() => loadOverview(false)} />
      <AddResearchObjectModal isOpen={isResearchModalOpen} onClose={() => setIsResearchModalOpen(false)} onSaved={handleResearchSaved} />

      <section className="space-y-6">
        {status.ledger?.error ? (
          <WorkspaceState
            kind="degraded"
            title="Portfolio refresh is delayed"
            description={`${status.ledger.error} The last successfully loaded ledger remains visible.`}
            actionLabel={status.ledger.pending ? "Retrying…" : "Retry refresh"}
            actionDisabled={status.ledger.pending}
            onAction={() => void loadOverview()}
            compact
          />
        ) : null}
        <div className="grid min-w-0 gap-6 2xl:grid-cols-[1.35fr_0.65fr]">
          <section className="min-w-0 rounded-lg border border-line bg-panel p-4 sm:p-6">
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div>
                <p className="text-sm font-medium text-muted">Live holdings</p>
                <h2 className="mt-1 text-2xl font-semibold tracking-tight">
                  {`${holdings.length} active holdings`}
                </h2>
                <p className="mt-2 text-sm text-muted">
                  Review your holdings and use them to see where new research changes your exposure.
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

            <div className="mt-4"><ValuationCoverage coverage={overview.valuation_coverage} /></div>
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
              <HoldingsTable holdings={holdings} />
            )}
          </section>

          <section className="space-y-6">
            <PortfolioWatchlist holdings={holdings} tracked={watchlist} />
            <SidebarList
              title="Considering"
              description="Ideas under active consideration before they become positions."
              items={considering}
              emptyState="Nothing in active consideration yet."
            />
          </section>
        </div>

        <section className="grid gap-6">
          <CollapsiblePanel
            eyebrow="Recent book changes"
            title={`${(overview?.recent_transactions ?? []).length} recent transactions`}
            summary="Expand to inspect the latest trades without leaving the live book."
            open={showRecentChanges}
            onToggle={() => setShowRecentChanges((current) => !current)}
          >
            <div className="mt-4 space-y-3">
              {(overview?.recent_transactions ?? []).length === 0 ? (
                <p className="text-sm text-muted">No transactions yet.</p>
              ) : (
                overview?.recent_transactions.map((txn) => (
                  <div key={txn.id} className="rounded-lg border border-slate-200 p-4 text-sm dark:border-slate-800">
                    <div className="flex items-center justify-between gap-4">
                      <p className="font-medium uppercase">{formatUserLabel(txn.action)}</p>
                      <span className="text-xs text-slate-400">
                        {new Date(txn.executed_at).toLocaleDateString()}
                      </span>
                    </div>
                    <p className="mt-1 text-foreground">
                      {(txn.quantity || 0).toFixed(2)} shares
                      {txn.price != null ? ` @ ${safeFormatCurrency(txn.price, 2)}` : ""}
                    </p>
                    {txn.notes ? (
                      <p className="mt-2 text-muted">{txn.notes}</p>
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

const HOLDING_COLUMNS: ReadonlyArray<{ key: PositionSortKey; label: string; numeric: boolean }> = [
  { key: "ticker", label: "Holding", numeric: false },
  { key: "quantity", label: "Shares", numeric: true },
  { key: "cost", label: "Avg cost", numeric: true },
  { key: "price", label: "Price", numeric: true },
  { key: "value", label: "Value", numeric: true },
  { key: "weight", label: "% of holdings", numeric: true },
  { key: "gain", label: "Total gain/loss", numeric: true },
];

function gainTone(value: number | null | undefined) {
  if (value == null) return "text-muted";
  return value >= 0 ? "text-positive" : "text-negative";
}

/** One line per holding, sortable, with one quote time for the table and a marker only on stale prices. */
function HoldingsTable({ holdings }: { holdings: Position[] }) {
  const [sort, setSort] = useState<{ key: PositionSortKey; direction: SortDirection }>({ key: "value", direction: "desc" });
  const rows = useMemo(() => sortPositionRows(buildPositionRows(holdings), sort.key, sort.direction), [holdings, sort]);
  const asOf = latestQuoteTime(holdings);
  const staleCount = rows.filter((row) => row.stale).length;

  function toggle(key: PositionSortKey) {
    setSort((current) => current.key === key
      ? { key, direction: current.direction === "desc" ? "asc" : "desc" }
      : { key, direction: key === "ticker" ? "asc" : "desc" });
  }

  return (
    <div className="mt-6">
      <p className="text-xs text-muted">
        {asOf ? `Prices as of ${asOf.toLocaleString()}` : "No current prices recorded"}
        {staleCount ? ` · ${staleCount} marked * are not from today` : ""}
      </p>
      <div className="mt-2 max-w-full overflow-x-auto">
        <table className="w-full min-w-[720px] text-sm">
          <thead className="border-b border-line text-xs text-muted">
            <tr>
              {HOLDING_COLUMNS.map((column) => {
                const active = sort.key === column.key;
                return (
                  <th
                    key={column.key}
                    scope="col"
                    aria-sort={active ? (sort.direction === "asc" ? "ascending" : "descending") : "none"}
                    className={`py-2 font-medium ${column.numeric ? "text-right" : "sticky left-0 z-10 bg-panel pr-4 text-left"}`}
                  >
                    <button type="button" onClick={() => toggle(column.key)} className={`inline-flex items-center gap-1 hover:text-foreground ${active ? "text-foreground" : ""}`}>
                      {column.label}
                      <span aria-hidden="true">{active ? (sort.direction === "asc" ? "▲" : "▼") : ""}</span>
                    </button>
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {rows.map(({ position: pos, gainPct, weightPct, stale }) => (
              <tr key={pos.id} className="hover:bg-panel-muted">
                <td className="sticky left-0 z-10 max-w-[18rem] bg-panel py-2 pr-4">
                  <span className="flex min-w-0 items-baseline gap-2">
                    <Link href={researchHref({ subject_id: pos.id, subject_type: "position", subject_name: pos.ticker ?? pos.entity_name ?? "Holding" })} aria-label={`Research ${pos.ticker ?? pos.entity_name ?? "holding"}`} className="font-semibold text-action hover:underline">{pos.ticker ?? pos.security_id}</Link>
                    {pos.entity_name ? <span className="hidden truncate text-xs text-muted sm:inline" title={pos.entity_name}>{pos.entity_name}</span> : null}
                  </span>
                </td>
                <td className="py-2 text-right tabular-nums">{safeFormatQuantity(pos.quantity)}</td>
                <td className="py-2 text-right tabular-nums text-muted">{safeFormatCurrency(pos.avg_cost_basis, 2)}</td>
                <td className="py-2 text-right tabular-nums">
                  {safeFormatCurrency(pos.current_price, 2)}
                  {stale ? (
                    <span className="ml-1 inline-flex items-center align-middle">
                      <span aria-hidden="true" className="text-amber-500">*</span>
                      <HintMarker label={`Price note for ${pos.ticker ?? "holding"}`}>
                        <p>{pos.valuation?.issues.join(" ") || `${pos.valuation?.status === "ready" ? "Last recorded" : "No current"} quote${pos.valuation?.quoted_at ? ` from ${new Date(pos.valuation.quoted_at).toLocaleString()}` : ""}${pos.valuation?.provider ? ` · ${formatUserLabel(pos.valuation.provider)}` : ""}.`}</p>
                      </HintMarker>
                    </span>
                  ) : null}
                </td>
                <td className="py-2 text-right tabular-nums">{safeFormatCurrency(pos.market_value, 2)}</td>
                <td className="py-2 text-right tabular-nums text-muted">{weightPct == null ? "—" : `${weightPct.toFixed(1)}%`}</td>
                <td className={`py-2 text-right tabular-nums ${gainTone(pos.unrealized_pnl)}`}>
                  {safeFormatSignedCurrency(pos.unrealized_pnl)}
                  <span className="ml-2 text-xs">{safeFormatPct(gainPct)}</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="mt-2 text-xs text-muted">Value uses recorded shares and quotes. Gain/loss uses recorded cost basis (average cost {"×"} shares).</p>
    </div>
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
    <section className="rounded-lg border border-line bg-panel p-6">
      <button type="button" onClick={onToggle} className="flex w-full items-start justify-between gap-4 text-left">
        <div>
          <p className="text-sm font-medium text-muted">{eyebrow}</p>
          <h3 className="mt-1 text-xl font-semibold tracking-tight">{title}</h3>
          <p className="mt-2 text-sm text-muted">{summary}</p>
        </div>
        <span className="rounded-none border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-600 dark:border-slate-700 dark:text-slate-300">
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
    <section className="rounded-lg border border-line bg-panel p-6">
      <p className="text-sm font-medium text-muted">{title}</p>
      <p className="mt-2 text-sm text-muted">{description}</p>
      <div className="mt-4 space-y-3">
        {items.length === 0 ? (
          <p className="text-sm text-muted">{emptyState}</p>
        ) : (
          items.map((item) => (
            <div key={item.id} className="rounded-lg border border-slate-200 px-4 py-3 dark:border-slate-800">
              <Link href={researchHref({ subject_id: item.id, subject_type: "position", subject_name: item.ticker ?? item.entity_name ?? "Tracked name" })} aria-label={`Research ${item.ticker ?? item.entity_name ?? "tracked name"}`} className="font-medium text-action underline decoration-dotted underline-offset-4">{item.ticker ?? item.security_id}</Link>
              {item.entity_name ? (
                <p className="mt-1 text-sm text-muted">{item.entity_name}</p>
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


