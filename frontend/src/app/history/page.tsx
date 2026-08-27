"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import AppNav from "@/components/AppNav";
import PageHeader from "@/components/PageHeader";
import ReconcilePanel from "@/components/ReconcilePanel";
import { apiFetch, AutomationStatus, correctTransaction, PortfolioOverview, Position, Transaction, TransactionCorrectionRecord } from "@/lib/api";
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
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadHistory = useCallback(async () => {
    try {
      setLoading(true);
      const [closedData, overviewData, automationData, correctionsData] = await Promise.all([
        apiFetch<Position[]>("/portfolio/positions?list_type=closed"),
        apiFetch<PortfolioOverview>("/portfolio/overview"),
        apiFetch<AutomationStatus>("/automation/status"),
        apiFetch<TransactionCorrectionRecord[]>("/portfolio/transactions/corrections?limit=40"),
      ]);
      setClosedPositions(
        closedData.sort(
          (a, b) =>
            new Date((b.updated_at || b.added_at) as string).getTime() -
            new Date((a.updated_at || a.added_at) as string).getTime(),
        ),
      );
      setOverview(overviewData);
      setAutomationStatus(automationData);
      setCorrectionHistory(correctionsData);
    } catch (err) {
      console.error("Failed to load history:", err);
      setError(err instanceof Error ? err.message : "Failed to load historical data");
    } finally {
      setLoading(false);
    }
  }, []);

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

      <div className="mx-auto w-full max-w-[1440px] px-4 py-8 sm:px-6 lg:px-8">
        <PageHeader
          className="mb-8"
          eyebrow="Portfolio records"
          title="Portfolio history"
          description="Review closed positions, transactions, corrections, and how the live book changed over time without losing sight of what is still held."
        />

        <div className="mb-8">
          <div className="rounded-lg border border-line bg-panel p-5">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="text-xs font-bold text-slate-400 uppercase tracking-widest">Recent flow</div>
              <div className="flex flex-wrap items-center gap-2">
                <a
                  href="#statement-reconcile"
                  className="inline-flex items-center gap-1.5 rounded-full border border-sky-200 bg-sky-50 px-2.5 py-1 text-[10px] font-bold uppercase tracking-widest text-sky-700 hover:border-sky-300 hover:bg-sky-100 dark:border-sky-900 dark:bg-sky-950/30 dark:text-sky-300 dark:hover:bg-sky-950/50"
                >
                  <ClipboardCheck className="h-3 w-3" />
                  Reconcile
                </a>
                <span className={`rounded-full border px-2.5 py-1 text-[10px] font-bold uppercase tracking-widest ${
                  gmailHealth.tone === "ok"
                    ? "border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-900 dark:bg-emerald-950/30 dark:text-emerald-300"
                    : gmailHealth.tone === "warn"
                      ? "border-rose-200 bg-rose-50 text-rose-700 dark:border-rose-900 dark:bg-rose-950/30 dark:text-rose-300"
                      : "border-slate-200 bg-slate-50 text-slate-500 dark:border-slate-800 dark:bg-slate-950 dark:text-slate-400"
                }`}>
                  {gmailHealth.label}: {gmailHealth.detail}
                </span>
              </div>
            </div>
            <div className="mt-4 grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
              {(overview?.recent_transactions ?? []).slice(0, 6).map((txn) => (
                <div key={txn.id} className="rounded-lg border border-slate-100 dark:border-slate-800 px-4 py-3">
                  <div className="flex items-center justify-between gap-3">
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="text-sm font-black uppercase">{txn.ticker || "CASH"}</span>
                        <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-widest text-slate-500 dark:bg-slate-900 dark:text-slate-300">
                          {txn.action}
                        </span>
                      </div>
                      <div className="mt-1 truncate text-[11px] text-slate-500 dark:text-slate-400">
                        {txn.entity_name || "Portfolio transaction"}
                      </div>
                    </div>
                    <span className="text-xs text-slate-400">
                      {formatLedgerDate(txn.executed_at)}
                    </span>
                  </div>
                  <div className="mt-3 flex items-end justify-between gap-3">
                    <div className="text-sm text-slate-600 dark:text-slate-300">
                      <div className="text-[10px] font-bold uppercase tracking-widest text-slate-400">Fill</div>
                      {formatQuantity(txn.quantity)} @ {formatPrice(txn.price)}
                    </div>
                    <div className="min-w-0 text-right">
                      <div className="text-[10px] font-bold uppercase tracking-widest text-slate-400">Source</div>
                      <div className="truncate text-xs font-bold text-slate-700 dark:text-slate-300">
                        {txn.source_label || "Manual/API"}
                        {txn.source_confidence != null ? ` · ${Math.round(txn.source_confidence * 100)}%` : ""}
                      </div>
                    </div>
                  </div>
                  {txn.source_evidence_id ? (
                    <Link
                      href={`/sources/evidence/${txn.source_evidence_id}`}
                      className="mt-3 block rounded-lg border border-slate-100 bg-slate-50 px-2.5 py-2 text-[10px] font-semibold text-slate-500 hover:border-sky-200 hover:text-sky-600 dark:border-slate-800 dark:bg-slate-950 dark:text-slate-400 dark:hover:border-sky-900 dark:hover:text-sky-300"
                    >
                      Evidence receipt {txn.source_evidence_id.slice(0, 8)}
                    </Link>
                  ) : txn.source_label === "Broker email" ? (
                    <div className="mt-3 rounded-lg border border-slate-100 bg-slate-50 px-2.5 py-2 text-[10px] text-slate-500 dark:border-slate-800 dark:bg-slate-950 dark:text-slate-400">
                      Legacy broker-confirmation note
                    </div>
                  ) : null}
                  <div className="mt-3 flex items-center justify-between gap-2">
                    <button
                      type="button"
                      onClick={() => openCorrection(txn)}
                      className="inline-flex items-center gap-1.5 rounded-full border border-slate-200 px-2.5 py-1 text-[10px] font-bold uppercase tracking-widest text-slate-500 hover:border-sky-200 hover:text-sky-600 dark:border-slate-800 dark:text-slate-400 dark:hover:border-sky-900 dark:hover:text-sky-300"
                    >
                      <PencilLine className="h-3 w-3" />
                      Correct fill
                    </button>
                    {txn.superseded_by_id ? (
                      <span className="text-[10px] font-semibold text-amber-600 dark:text-amber-300">
                        Superseded
                      </span>
                    ) : null}
                  </div>
                  {correctingTxnId === txn.id && correctionForm ? (
                    <div className="mt-3 rounded-lg border border-sky-100 bg-sky-50/40 p-3 dark:border-sky-950 dark:bg-sky-950/20">
                      <div className="mb-2 flex items-center justify-between gap-2">
                        <div className="text-[10px] font-bold uppercase tracking-widest text-sky-700 dark:text-sky-300">
                          Save corrected transaction
                        </div>
                        <button
                          type="button"
                          onClick={() => {
                            setCorrectingTxnId(null);
                            setCorrectionForm(null);
                            setCorrectionError(null);
                          }}
                          className="rounded-full p-1 text-slate-400 hover:bg-white hover:text-slate-700 dark:hover:bg-slate-900 dark:hover:text-slate-200"
                          aria-label="Close correction form"
                        >
                          <X className="h-3 w-3" />
                        </button>
                      </div>
                      <div className="grid grid-cols-2 gap-2">
                        <label className="text-[10px] font-bold uppercase tracking-widest text-slate-400">
                          Quantity
                          <input
                            value={correctionForm.quantity}
                            onChange={(e) => setCorrectionForm((current) => current ? { ...current, quantity: e.target.value } : current)}
                            type="number"
                            step="0.0001"
                            className="mt-1 w-full rounded-lg border border-slate-200 bg-white px-2 py-1.5 text-xs font-semibold text-slate-800 outline-none focus:border-sky-400 dark:border-slate-800 dark:bg-slate-950 dark:text-slate-100"
                          />
                        </label>
                        <label className="text-[10px] font-bold uppercase tracking-widest text-slate-400">
                          Price
                          <input
                            value={correctionForm.price}
                            onChange={(e) => setCorrectionForm((current) => current ? { ...current, price: e.target.value } : current)}
                            type="number"
                            step="0.01"
                            className="mt-1 w-full rounded-lg border border-slate-200 bg-white px-2 py-1.5 text-xs font-semibold text-slate-800 outline-none focus:border-sky-400 dark:border-slate-800 dark:bg-slate-950 dark:text-slate-100"
                          />
                        </label>
                        <label className="col-span-2 text-[10px] font-bold uppercase tracking-widest text-slate-400">
                          Executed
                          <input
                            value={correctionForm.executed_at}
                            onChange={(e) => setCorrectionForm((current) => current ? { ...current, executed_at: e.target.value } : current)}
                            type="datetime-local"
                            className="mt-1 w-full rounded-lg border border-slate-200 bg-white px-2 py-1.5 text-xs font-semibold text-slate-800 outline-none focus:border-sky-400 dark:border-slate-800 dark:bg-slate-950 dark:text-slate-100"
                          />
                        </label>
                        <label className="col-span-2 text-[10px] font-bold uppercase tracking-widest text-slate-400">
                          Reason
                          <input
                            value={correctionForm.reason}
                            onChange={(e) => setCorrectionForm((current) => current ? { ...current, reason: e.target.value } : current)}
                            className="mt-1 w-full rounded-lg border border-slate-200 bg-white px-2 py-1.5 text-xs font-semibold text-slate-800 outline-none focus:border-sky-400 dark:border-slate-800 dark:bg-slate-950 dark:text-slate-100"
                          />
                        </label>
                      </div>
                      {correctionError ? <div className="mt-2 text-xs font-semibold text-rose-600 dark:text-rose-300">{correctionError}</div> : null}
                      <button
                        type="button"
                        onClick={() => submitCorrection(txn)}
                        disabled={correctionBusy}
                        className="mt-3 inline-flex items-center gap-1.5 rounded-full bg-sky-600 px-3 py-1.5 text-[10px] font-bold uppercase tracking-widest text-white hover:bg-sky-700 disabled:opacity-50"
                      >
                        <PencilLine className="h-3 w-3" />
                        {correctionBusy ? "Saving" : "Save correction"}
                      </button>
                    </div>
                  ) : null}
                </div>
              ))}
              {!loading && (overview?.recent_transactions ?? []).length === 0 ? (
                <div className="rounded-lg border border-dashed border-slate-200 px-4 py-6 text-sm italic text-slate-400 dark:border-slate-800 dark:text-slate-500">
                  No recent transactions are in the ledger yet.
                </div>
              ) : null}
            </div>
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
              <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
                Superseded fills stay visible here with their replacement row and reason.
              </p>
            </div>
            <span className="rounded-full border border-slate-200 px-2.5 py-1 text-[10px] font-bold uppercase tracking-widest text-slate-500 dark:border-slate-800 dark:text-slate-400">
              {correctionHistory.length} corrections
            </span>
          </div>
          <div className="mt-4 grid grid-cols-1 gap-3 lg:grid-cols-2">
            {correctionHistory.slice(0, 6).map((item) => (
              <div key={`${item.original.id}:${item.replacement?.id ?? "missing"}`} className="rounded-lg border border-slate-100 px-4 py-3 dark:border-slate-800">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="min-w-0">
                    <div className="text-sm font-black uppercase text-slate-900 dark:text-slate-100">
                      {item.original.ticker || item.replacement?.ticker || "CASH"}
                    </div>
                    <div className="mt-0.5 truncate text-[11px] text-slate-500 dark:text-slate-400">
                      {(item.original.entity_name || item.replacement?.entity_name || "Portfolio transaction")} · {formatLedgerDate(item.corrected_at || item.replacement?.executed_at || item.original.executed_at)}
                    </div>
                  </div>
                  <span className="rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 text-[10px] font-bold uppercase tracking-widest text-amber-700 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-300">
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
            {!loading && correctionHistory.length === 0 ? (
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
              <p className="text-[10px] text-slate-500 mt-1 uppercase font-bold tracking-widest">Net outcome across {closedPositions.length} trades</p>
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
              <div className="mt-2 h-1.5 w-full bg-slate-100 dark:bg-slate-800 rounded-full overflow-hidden">
                <div className="h-full bg-sky-500 transition-all duration-1000" style={{ width: `${winRate}%` }} />
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
              <p className="text-[10px] text-slate-500 mt-1 uppercase font-bold tracking-widest">Total operational span</p>
            </div>
          </div>
        </div>

        {loading ? (
          <div className="flex flex-col items-center justify-center py-20 grayscale opacity-50">
            <div className="w-8 h-8 border-3 border-sky-500/30 border-t-sky-500 rounded-full animate-spin mb-4" />
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
            <p className="text-sm text-slate-500 dark:text-slate-400 mt-2">
              Once positions are opened or closed, they will appear here in one running ledger.
            </p>
          </div>
        ) : (
          <div className="overflow-hidden rounded-lg border border-line bg-panel">
            <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 dark:border-slate-800/50 px-5 py-3">
              <div className="text-sm text-slate-500 dark:text-slate-400">
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
                    className={`rounded-full border px-3 py-1 text-[11px] font-semibold transition-colors ${
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
                  <th className="px-5 py-3 text-[10px] font-bold text-slate-400 uppercase tracking-widest">Asset</th>
                  <th className="px-5 py-3 text-[10px] font-bold text-slate-400 uppercase tracking-widest">Book state</th>
                  <th className="px-5 py-3 text-[10px] font-bold text-slate-400 uppercase tracking-widest text-right">Quantity</th>
                  <th className="px-5 py-3 text-[10px] font-bold text-slate-400 uppercase tracking-widest text-right">Avg Cost</th>
                  <th className="px-5 py-3 text-[10px] font-bold text-slate-400 uppercase tracking-widest text-right">Value / Outcome</th>
                  <th className="px-5 py-3 text-[10px] font-bold text-slate-400 uppercase tracking-widest text-right">Last Activity</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-50 dark:divide-slate-900/50">
                {filteredLedgerRows.map(({ kind, position: pos }) => {
                  const pnl = kind === "live" ? (pos.unrealized_pnl || 0) : (pos.realized_pnl || 0);
                  const isPositive = pnl >= 0;
                  const statusLabel = kind === "live" ? "active holding" : "archived exit";
                  const valueLabel = kind === "live" ? pos.market_value : pnl;

                  return (
                    <tr key={`${kind}:${pos.id}`} className={`group transition-colors ${kind === "live" ? "bg-sky-50/30 dark:bg-sky-950/10 hover:bg-sky-50/50 dark:hover:bg-sky-950/20" : "hover:bg-slate-50/50 dark:hover:bg-slate-950/30"}`}>
                      <td className="px-5 py-3">
                        <div className="flex flex-col">
                          <span className="text-sm font-bold tracking-tight text-slate-900 dark:text-slate-100">{pos.ticker || "UNKNOWN"}</span>
                          <span className="text-[10px] text-slate-500 uppercase tracking-tighter mt-0.5">{pos.entity_name}</span>
                        </div>
                      </td>
                      <td className="px-5 py-3">
                        <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-[9px] font-bold uppercase tracking-widest ${kind === "live" ? "bg-sky-100 text-sky-700 dark:bg-sky-950/40 dark:text-sky-300" : "bg-slate-100 text-slate-600 dark:bg-slate-900 dark:text-slate-300"}`}>
                          {statusLabel}
                        </span>
                      </td>
                      <td className="px-5 py-3 text-right font-mono text-xs text-slate-600 dark:text-slate-400">
                        {Math.abs(pos.quantity).toLocaleString(undefined, { maximumFractionDigits: 3 })}
                      </td>
                      <td className="px-5 py-3 text-right font-mono text-xs text-slate-600 dark:text-slate-400">
                        ${pos.avg_cost_basis.toLocaleString(undefined, { minimumFractionDigits: 2 })}
                      </td>
                      <td className="px-5 py-3 text-right">
                        <div className="flex flex-col items-end gap-1.5">
                          <div className="text-sm font-bold font-mono text-slate-900 dark:text-slate-100">
                            ${valueLabel.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                          </div>
                          <div className={`inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-[11px] font-bold font-mono transition-transform group-hover:scale-105 ${
                          isPositive
                            ? "bg-emerald-50 text-emerald-600 dark:bg-emerald-950/20 dark:text-emerald-400"
                            : "bg-rose-50 text-rose-600 dark:bg-rose-950/20 dark:text-rose-400"
                        }`}>
                          {isPositive ? <ArrowUpRight className="w-3 h-3" /> : <ArrowDownRight className="w-3 h-3" />}
                            {kind === "live" ? "unrealized" : "realized"} {isPositive ? "+" : ""}${pnl.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                          </div>
                        </div>
                      </td>
                      <td className="px-5 py-3 text-right">
                        <span className="text-[10px] font-bold text-slate-400 uppercase tracking-widest">
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
      <div className="text-[10px] font-bold uppercase tracking-widest text-slate-400">{label}</div>
      <div className="mt-1 truncate text-xs font-bold text-slate-800 dark:text-slate-200">
        {txn.action.toUpperCase()} {formatQuantity(txn.quantity)}
      </div>
      <div className="mt-0.5 text-[11px] text-slate-500 dark:text-slate-400">
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
