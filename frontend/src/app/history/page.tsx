"use client";

import { Fragment, useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useWorkspaceReads } from "@/lib/workspace-reads";
import WorkspaceState from "@/components/WorkspaceState";
import AppNav from "@/components/AppNav";
import PageHeader from "@/components/PageHeader";
import ReconcilePanel from "@/components/ReconcilePanel";
import { AutomationStatus, correctTransaction, PortfolioOverview, Position, Transaction, TransactionCorrectionRecord } from "@/lib/api";
import { automationJobHealth } from "@/lib/automation";
import { ArrowUpRight, ArrowDownRight, ArrowRight, History, Calendar, DollarSign, TrendingUp, ClipboardCheck, PencilLine, X } from "lucide-react";

type CorrectionFormState = {
  quantity: string;
  price: string;
  executed_at: string;
  notes: string;
  reason: string;
};

export default function HistoryPage() {
  const [closedPositions, setClosedPositions] = useState<Position[]>([]);
  const [overview, setOverview] = useState<PortfolioOverview | null>(null);
  const [correctionHistory, setCorrectionHistory] = useState<TransactionCorrectionRecord[]>([]);
  const [automationStatus, setAutomationStatus] = useState<AutomationStatus | null>(null);
  const [ledgerFilter, setLedgerFilter] = useState<"all" | "active" | "archived">("all");
  const [correctingTxnId, setCorrectingTxnId] = useState<string | null>(null);
  const [correctionForm, setCorrectionForm] = useState<CorrectionFormState | null>(null);
  const [correctionBusy, setCorrectionBusy] = useState(false);
  const [correctionError, setCorrectionError] = useState<string | null>(null);
  const [correctionNotice, setCorrectionNotice] = useState<string | null>(null);

  const { read, status } = useWorkspaceReads<"closed" | "ledger" | "automation" | "corrections">();
  const loading = !status.closed?.ready && !status.closed?.error;
  const error = status.closed?.ready ? null : status.closed?.error;

  const loadHistory = useCallback(async () => {
    await Promise.all([
      read<Position[]>("closed", "/portfolio/positions?list_type=closed", data =>
        setClosedPositions(data.sort((a, b) =>
          new Date((b.updated_at || b.added_at) as string).getTime() -
          new Date((a.updated_at || a.added_at) as string).getTime()
        ))),
      read<PortfolioOverview>("ledger", "/portfolio/overview?refresh=false", setOverview),
      read<AutomationStatus>("automation", "/automation/status", setAutomationStatus),
      read<TransactionCorrectionRecord[]>("corrections", "/portfolio/transactions/corrections?limit=40", setCorrectionHistory),
    ]);
  }, [read]);

  useEffect(() => {
    void loadHistory();
  }, [loadHistory]);

  function openCorrection(txn: Transaction) {
    setCorrectionError(null);
    setCorrectionNotice(null);
    setCorrectingTxnId(txn.id);
    setCorrectionForm({
      quantity: String(txn.quantity ?? ""),
      price: txn.price == null ? "" : String(txn.price),
      executed_at: toDatetimeLocal(txn.executed_at),
      notes: txn.notes ?? "",
      reason: "",
    });
  }

  async function submitCorrection(txn: Transaction) {
    if (!correctionForm || correctionBusy) return;
    const quantity = Number(correctionForm.quantity);
    const price = correctionForm.price.trim() ? Number(correctionForm.price) : null;
    if (!Number.isFinite(quantity) || quantity <= 0) {
      setCorrectionError("Quantity must be a positive number.");
      return;
    }
    if (price != null && (!Number.isFinite(price) || price < 0)) {
      setCorrectionError("Price must be zero or greater.");
      return;
    }
    setCorrectionBusy(true);
    setCorrectionError(null);
    try {
      await correctTransaction(txn.id, {
        quantity,
        price,
        executed_at: fromDatetimeLocal(correctionForm.executed_at),
        notes: correctionForm.notes.trim() || null,
        reason: correctionForm.reason.trim() || "Manual correction from History recent flow",
      });
      setCorrectionNotice("Correction saved; portfolio replay refreshed.");
      setCorrectingTxnId(null);
      setCorrectionForm(null);
      await loadHistory();
    } catch (err) {
      setCorrectionError(err instanceof Error ? err.message : "Correction failed.");
    } finally {
      setCorrectionBusy(false);
    }
  }

  const totalRealizedPnl = closedPositions.reduce((sum, p) => sum + (p.realized_pnl || 0), 0);
  const winRate = closedPositions.length > 0
    ? (closedPositions.filter(p => (p.realized_pnl || 0) > 0).length / closedPositions.length) * 100
    : 0;
  const liveHoldings = overview?.holdings ?? [];
  const gmailHealth = automationJobHealth(automationStatus?.jobs, "gmail_sync", "Gmail sync");
  const ledgerRows = [
    ...liveHoldings.map((position) => ({
      kind: "live" as const,
      position,
      sortDate: new Date((position.updated_at || position.added_at) as string).getTime(),
    })),
    ...closedPositions.map((position) => ({
      kind: "closed" as const,
      position,
      sortDate: new Date((position.updated_at || position.added_at) as string).getTime(),
    })),
  ].sort((a, b) => b.sortDate - a.sortDate);
  const filteredLedgerRows = ledgerRows.filter((entry) => {
    if (ledgerFilter === "active") return entry.kind === "live";
    if (ledgerFilter === "archived") return entry.kind === "closed";
    return true;
  });
  const earliestLedgerTimestamp = ledgerRows.reduce<number | null>((earliest, entry) => {
    const candidate = new Date(entry.position.added_at).getTime();
    if (Number.isNaN(candidate)) return earliest;
    return earliest == null ? candidate : Math.min(earliest, candidate);
  }, null);
  const activeDaysTracked =
    earliestLedgerTimestamp == null
      ? 0
      : Math.max(1, Math.ceil((Date.now() - earliestLedgerTimestamp) / (1000 * 60 * 60 * 24)));

  return (
    <main className="min-h-screen bg-background text-foreground font-sans">
      <AppNav active="history" />

      <div className="workspace-frame py-8">
        {(["ledger", "automation", "corrections", "closed"] as const).some(key => status[key]?.error) ? (
          <WorkspaceState compact kind="degraded" title="Some history data could not be loaded"
            description={"Unavailable: " + (["ledger", "automation", "corrections", "closed"] as const).filter(key => status[key]?.error).join(", ")}
            actionLabel="Retry" onAction={() => void loadHistory()} />
        ) : null}

        <PageHeader
          className="mb-8"
          eyebrow="Portfolio records"
          title="Portfolio history"
          description="Review closed positions, transactions, corrections, and how the live book changed over time without losing sight of what is still held."
        />

        <div className="mb-8 min-w-0">
          <div className="min-w-0 border border-line bg-panel p-5">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <h2 className="text-sm font-semibold">Recent transactions</h2>
              <div className="flex flex-wrap items-center gap-3 text-xs">
                <span className={gmailHealth.tone === "ok" ? "text-positive" : gmailHealth.tone === "warn" ? "text-negative" : "text-muted"}>
                  {gmailHealth.label}: {gmailHealth.detail}
                </span>
                <a href="#statement-reconcile" className="inline-flex items-center gap-1.5 border border-line px-2.5 py-1 font-medium text-action hover:border-line-strong">
                  <ClipboardCheck className="h-3 w-3" />
                  Reconcile with a statement
                </a>
              </div>
            </div>
            {status.ledger?.ready && (overview?.recent_transactions ?? []).length === 0 ? (
              <p className="mt-4 border border-dashed border-line px-4 py-6 text-sm text-muted">No recent transactions are in the ledger yet.</p>
            ) : (
              <div className="relative mt-4 max-w-full overflow-x-auto">
                <table className="w-full min-w-[760px] text-sm">
                  <thead className="border-b border-line text-xs text-muted">
                    <tr>
                      <th scope="col" className="py-2 pr-4 text-left font-medium">Date</th>
                      <th scope="col" className="py-2 pr-4 text-left font-medium">Holding</th>
                      <th scope="col" className="py-2 pr-4 text-left font-medium">Action</th>
                      <th scope="col" className="py-2 pr-4 text-right font-medium">Shares</th>
                      <th scope="col" className="py-2 pr-4 text-right font-medium">Price</th>
                      <th scope="col" className="py-2 pr-4 text-left font-medium">Source</th>
                      <th scope="col" className="py-2 text-right font-medium"><span className="sr-only">Correct</span></th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-line">
                    {(overview?.recent_transactions ?? []).slice(0, 10).map((txn) => (
                      <Fragment key={txn.id}>
                        <tr className="align-top hover:bg-panel-muted">
                          <td className="whitespace-nowrap py-2 pr-4 tabular-nums text-muted">{formatLedgerDate(txn.executed_at)}</td>
                          <td className="py-2 pr-4">
                            <span className="font-semibold">{txn.ticker || "Cash"}</span>
                            {txn.entity_name ? <span className="ml-2 hidden text-xs text-muted sm:inline">{txn.entity_name}</span> : null}
                            {txn.superseded_by_id ? <span className="ml-2 text-xs text-caution">Superseded</span> : null}
                          </td>
                          <td className="py-2 pr-4 first-letter:uppercase">{txn.action}</td>
                          <td className="py-2 pr-4 text-right tabular-nums">{formatQuantity(txn.quantity)}</td>
                          <td className="py-2 pr-4 text-right tabular-nums">{formatPrice(txn.price)}</td>
                          <td className="py-2 pr-4 text-xs">
                            <span>{txn.source_label || "Manual/API"}{txn.source_confidence != null ? ` · ${Math.round(txn.source_confidence * 100)}%` : ""}</span>
                            {txn.source_evidence_id ? (
                              <Link href={`/sources/evidence/${txn.source_evidence_id}`} className="ml-2 text-action hover:underline">Receipt</Link>
                            ) : txn.source_label === "Broker email" ? (
                              <span className="ml-2 text-muted">legacy note</span>
                            ) : null}
                          </td>
                          <td className="py-2 text-right">
                            <button type="button" onClick={() => openCorrection(txn)} className="inline-flex items-center gap-1 text-xs text-action hover:underline">
                              <PencilLine className="h-3 w-3" />
                              Correct
                            </button>
                          </td>
                        </tr>
                        {correctingTxnId === txn.id && correctionForm ? (
                          <tr>
                            <td colSpan={7} className="bg-panel-muted p-3">
                              <div className="mb-2 flex items-center justify-between gap-2">
                                <p className="text-sm font-semibold">Correct this {txn.action}{txn.ticker ? ` of ${txn.ticker}` : ""}</p>
                                <button type="button" onClick={() => { setCorrectingTxnId(null); setCorrectionForm(null); setCorrectionError(null); }} className="p-1 text-muted hover:text-foreground" aria-label="Close correction form">
                                  <X className="h-3 w-3" />
                                </button>
                              </div>
                              <div className="grid gap-2 sm:grid-cols-4">
                                <label className="text-xs text-muted">
                                  Shares
                                  <input value={correctionForm.quantity} onChange={(e) => setCorrectionForm((current) => current ? { ...current, quantity: e.target.value } : current)} type="number" step="0.0001" className="workspace-control mt-1 w-full" />
                                </label>
                                <label className="text-xs text-muted">
                                  Price
                                  <input value={correctionForm.price} onChange={(e) => setCorrectionForm((current) => current ? { ...current, price: e.target.value } : current)} type="number" step="0.01" className="workspace-control mt-1 w-full" />
                                </label>
                                <label className="text-xs text-muted sm:col-span-2">
                                  Executed
                                  <input value={correctionForm.executed_at} onChange={(e) => setCorrectionForm((current) => current ? { ...current, executed_at: e.target.value } : current)} type="datetime-local" className="workspace-control mt-1 w-full" />
                                </label>
                                <label className="text-xs text-muted sm:col-span-4">
                                  Reason
                                  <input value={correctionForm.reason} onChange={(e) => setCorrectionForm((current) => current ? { ...current, reason: e.target.value } : current)} className="workspace-control mt-1 w-full" />
                                </label>
                              </div>
                              {correctionError ? <p role="alert" className="mt-2 text-xs text-negative">{correctionError}</p> : null}
                              <button type="button" onClick={() => submitCorrection(txn)} disabled={correctionBusy} className="workspace-control mt-3 inline-flex items-center gap-1.5 disabled:opacity-50">
                                <PencilLine className="h-3 w-3" />
                                {correctionBusy ? "Saving…" : "Save correction"}
                              </button>
                            </td>
                          </tr>
                        ) : null}
                      </Fragment>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            {correctionNotice ? (
              <div className="mt-3 rounded-lg border border-emerald-100 bg-emerald-50 px-4 py-3 text-xs font-semibold text-emerald-700 dark:border-emerald-950 dark:bg-emerald-950/20 dark:text-emerald-300">
                {correctionNotice}
              </div>
            ) : null}
          </div>
        </div>

        <div className="mb-8">
          <ReconcilePanel id="statement-reconcile" />
        </div>

        <section className="mb-8 rounded-lg border border-line bg-panel p-5">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <div className="text-xs font-bold uppercase tracking-widest text-slate-400">Correction history</div>
              <p className="mt-1 text-sm text-muted">
                Superseded fills stay visible here with their replacement row and reason.
              </p>
            </div>
            <span className="rounded-none border border-slate-200 px-2.5 py-1 text-xs font-bold uppercase tracking-widest text-slate-500 dark:border-slate-800 dark:text-slate-400">
              {correctionHistory.length} corrections
            </span>
          </div>
          <div className="mt-4 grid grid-cols-1 gap-3 lg:grid-cols-2">
            {correctionHistory.slice(0, 6).map((item) => (
              <div key={`${item.original.id}:${item.replacement?.id ?? "missing"}`} className="rounded-lg border border-slate-100 px-4 py-3 dark:border-slate-800">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="min-w-0">
                    <div className="text-sm font-black uppercase text-foreground">
                      {item.original.ticker || item.replacement?.ticker || "CASH"}
                    </div>
                    <div className="mt-0.5 truncate text-xs text-muted">
                      {(item.original.entity_name || item.replacement?.entity_name || "Portfolio transaction")} · {formatLedgerDate(item.corrected_at || item.replacement?.executed_at || item.original.executed_at)}
                    </div>
                  </div>
                  <span className="rounded-none border border-amber-200 bg-amber-50 px-2 py-0.5 text-xs font-bold uppercase tracking-widest text-amber-700 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-300">
                    superseded
                  </span>
                </div>
                <div className="mt-3 grid grid-cols-[1fr_auto_1fr] items-center gap-2 text-xs">
                  <CorrectionTxnMini label="Original" txn={item.original} />
                  <ArrowRight className="h-4 w-4 text-slate-300 dark:text-slate-700" />
                  {item.replacement ? (
                    <CorrectionTxnMini label="Replacement" txn={item.replacement} />
                  ) : (
                    <div className="rounded-lg border border-dashed border-slate-200 px-3 py-2 text-slate-400 dark:border-slate-800">Missing replacement row</div>
                  )}
                </div>
                {item.reason ? (
                  <div className="mt-3 rounded-lg border border-slate-100 bg-slate-50 px-3 py-2 text-xs text-slate-600 dark:border-slate-800 dark:bg-slate-950 dark:text-slate-300">
                    {item.reason}
                  </div>
                ) : null}
              </div>
            ))}
            {status.corrections?.ready && correctionHistory.length === 0 ? (
              <div className="rounded-lg border border-dashed border-slate-200 px-4 py-6 text-sm italic text-slate-400 dark:border-slate-800 dark:text-slate-500">
                No transaction corrections have been recorded yet.
              </div>
            ) : null}
          </div>
        </section>

        {/* Stats Grid */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-6 mb-12">
          <div className="flex flex-col justify-between rounded-lg border border-line bg-panel p-6">
            <div className="flex items-center justify-between mb-4">
              <span className="text-xs font-bold text-slate-400 uppercase tracking-widest">Total Realized P&L</span>
              <DollarSign className="w-4 h-4 text-slate-300" />
            </div>
            <div>
              <div className={`text-3xl font-bold tracking-tighter ${totalRealizedPnl >= 0 ? "text-emerald-500" : "text-rose-500"}`}>
                {totalRealizedPnl >= 0 ? "+" : ""}${totalRealizedPnl.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
              </div>
              <p className="text-xs text-slate-500 mt-1 uppercase font-bold tracking-widest">Net outcome across {closedPositions.length} trades</p>
            </div>
          </div>

          <div className="flex flex-col justify-between rounded-lg border border-line bg-panel p-6">
            <div className="flex items-center justify-between mb-4">
              <span className="text-xs font-bold text-slate-400 uppercase tracking-widest">Success Rate</span>
              <TrendingUp className="w-4 h-4 text-slate-300" />
            </div>
            <div>
              <div className="text-3xl font-bold tracking-tighter">
                {winRate.toFixed(1)}%
              </div>
              <div className="mt-2 h-1.5 w-full bg-slate-100 dark:bg-slate-800 rounded-none overflow-hidden">
                <div className="h-full bg-muted transition-all duration-1000" style={{ width: `${winRate}%` }} />
              </div>
            </div>
          </div>

          <div className="flex flex-col justify-between rounded-lg border border-line bg-panel p-6">
            <div className="flex items-center justify-between mb-4">
              <span className="text-xs font-bold text-slate-400 uppercase tracking-widest">Active Days tracked</span>
              <Calendar className="w-4 h-4 text-slate-300" />
            </div>
            <div>
              <div className="text-3xl font-bold tracking-tighter">
                {activeDaysTracked}
              </div>
              <p className="text-xs text-slate-500 mt-1 uppercase font-bold tracking-widest">Total operational span</p>
            </div>
          </div>
        </div>

        {loading ? (
          <div className="flex flex-col items-center justify-center py-20 grayscale opacity-50">
            <div className="w-8 h-8 border-3 border-line border-t-sky-500 rounded-full animate-spin mb-4" />
            <p className="text-xs font-bold uppercase tracking-widest animate-pulse">Retrieving Audit logs...</p>
          </div>
        ) : error ? (
          <div className="rounded-lg border border-rose-100 bg-rose-50/50 p-12 text-center dark:border-rose-900/40 dark:bg-rose-950/10">
            <p className="text-sm font-semibold text-rose-600 dark:text-rose-400">{error}</p>
          </div>
        ) : filteredLedgerRows.length === 0 ? (
          <div className="rounded-lg border border-dashed border-line bg-panel p-20 text-center">
            <History className="w-12 h-12 text-slate-300 mx-auto mb-4" />
            <h3 className="text-lg font-bold">No portfolio ledger entries yet</h3>
            <p className="text-sm text-muted mt-2">
              Once positions are opened or closed, they will appear here in one running ledger.
            </p>
          </div>
        ) : (
          <div className="overflow-hidden rounded-lg border border-line bg-panel">
            <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 dark:border-slate-800/50 px-5 py-3">
              <div className="text-sm text-muted">
                Mixed ledger sorted by latest activity. Active rows stay highlighted in-place.
              </div>
              <div className="flex flex-wrap items-center gap-2">
                {[
                  { key: "all", label: `All (${ledgerRows.length})` },
                  { key: "active", label: `Active (${liveHoldings.length})` },
                  { key: "archived", label: `Archived (${closedPositions.length})` },
                ].map((filter) => (
                  <button
                    key={filter.key}
                    type="button"
                    onClick={() => setLedgerFilter(filter.key as "all" | "active" | "archived")}
                    className={`rounded-none border px-3 py-1 text-xs font-semibold transition-colors ${
                      ledgerFilter === filter.key
                        ? "border-sky-300 bg-sky-50 text-sky-700 dark:border-sky-800 dark:bg-sky-950/30 dark:text-sky-300"
                        : "border-slate-300 text-slate-600 hover:border-slate-400 dark:border-slate-700 dark:text-slate-300"
                    }`}
                  >
                    {filter.label}
                  </button>
                ))}
              </div>
            </div>
            <table className="w-full text-left border-collapse">
              <thead>
                <tr className="border-b border-slate-100 dark:border-slate-800/50">
                  <th className="px-5 py-3 text-xs font-bold text-slate-400 uppercase tracking-widest">Asset</th>
                  <th className="px-5 py-3 text-xs font-bold text-slate-400 uppercase tracking-widest">Book state</th>
                  <th className="px-5 py-3 text-xs font-bold text-slate-400 uppercase tracking-widest text-right">Quantity</th>
                  <th className="px-5 py-3 text-xs font-bold text-slate-400 uppercase tracking-widest text-right">Avg Cost</th>
                  <th className="px-5 py-3 text-xs font-bold text-slate-400 uppercase tracking-widest text-right">Value / Outcome</th>
                  <th className="px-5 py-3 text-xs font-bold text-slate-400 uppercase tracking-widest text-right">Last Activity</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-50 dark:divide-slate-900/50">
                {filteredLedgerRows.map(({ kind, position: pos }) => {
                  const pnl = kind === "live" ? pos.unrealized_pnl : pos.realized_pnl;
                  const isPositive = pnl != null && pnl >= 0;
                  const statusLabel = kind === "live" ? "active holding" : "archived exit";
                  const valueLabel = kind === "live" ? pos.market_value : pnl;

                  return (
                    <tr key={`${kind}:${pos.id}`} className={`group transition-colors ${kind === "live" ? "bg-sky-50/30 dark:bg-sky-950/10 hover:bg-sky-50/50 dark:hover:bg-sky-950/20" : "hover:bg-slate-50/50 dark:hover:bg-slate-950/30"}`}>
                      <td className="px-5 py-3">
                        <div className="flex flex-col">
                          <span className="text-sm font-bold tracking-tight text-foreground">{pos.ticker || "UNKNOWN"}</span>
                          <span className="text-xs text-slate-500 uppercase tracking-tighter mt-0.5">{pos.entity_name}</span>
                        </div>
                      </td>
                      <td className="px-5 py-3">
                        <span className={`inline-flex items-center rounded-none px-2 py-0.5 text-xs font-bold uppercase tracking-widest ${kind === "live" ? "bg-sky-100 text-sky-700 dark:bg-sky-950/40 dark:text-sky-300" : "bg-slate-100 text-slate-600 dark:bg-slate-900 dark:text-slate-300"}`}>
                          {statusLabel}
                        </span>
                      </td>
                      <td className="px-5 py-3 text-right font-mono text-xs text-muted">
                        {Math.abs(pos.quantity).toLocaleString(undefined, { maximumFractionDigits: 3 })}
                      </td>
                      <td className="px-5 py-3 text-right font-mono text-xs text-muted">
                        ${pos.avg_cost_basis.toLocaleString(undefined, { minimumFractionDigits: 2 })}
                      </td>
                      <td className="px-5 py-3 text-right">
                        <div className="flex flex-col items-end gap-1.5">
                          <div className="text-sm font-bold font-mono text-foreground">
                            {valueLabel == null ? "Unavailable" : `$${valueLabel.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`}
                          </div>
                          <div className={`inline-flex items-center gap-1 px-2.5 py-1 rounded-none text-xs font-bold font-mono transition-transform group-hover:scale-105 ${
                          pnl == null ? "text-muted" : isPositive
                            ? "bg-emerald-50 text-emerald-600 dark:bg-emerald-950/20 dark:text-emerald-400"
                            : "bg-rose-50 text-rose-600 dark:bg-rose-950/20 dark:text-rose-400"
                        }`}>
                          {pnl == null ? null : isPositive ? <ArrowUpRight className="w-3 h-3" /> : <ArrowDownRight className="w-3 h-3" />}
                            {kind === "live" ? "unrealized" : "realized"} {pnl == null ? "unavailable" : `${isPositive ? "+" : ""}$${pnl.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`}
                          </div>
                        </div>
                      </td>
                      <td className="px-5 py-3 text-right">
                        <span className="text-xs font-bold text-slate-400 uppercase tracking-widest">
                          {new Date((pos.updated_at || pos.added_at) as string).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })}
                        </span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </main>
  );
}

function CorrectionTxnMini({ label, txn }: { label: string; txn: Transaction }) {
  return (
    <div className="min-w-0 rounded-lg border border-slate-100 bg-slate-50 px-3 py-2 dark:border-slate-800 dark:bg-slate-950">
      <div className="text-xs font-bold uppercase tracking-widest text-slate-400">{label}</div>
      <div className="mt-1 truncate text-xs font-bold text-foreground">
        {txn.action.toUpperCase()} {formatQuantity(txn.quantity)}
      </div>
      <div className="mt-0.5 text-xs text-muted">
        {formatPrice(txn.price)} · {formatLedgerDate(txn.executed_at)}
      </div>
    </div>
  );
}

function formatLedgerDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Unknown";
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function formatQuantity(value: number): string {
  if (!Number.isFinite(value)) return "0";
  return new Intl.NumberFormat("en-US", {
    maximumFractionDigits: value < 1 ? 4 : 2,
  }).format(value);
}

function formatPrice(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "n/a";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  }).format(value);
}

function toDatetimeLocal(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 16);
}

function fromDatetimeLocal(value: string): string {
  if (!value) return new Date().toISOString();
  return new Date(value).toISOString();
}
