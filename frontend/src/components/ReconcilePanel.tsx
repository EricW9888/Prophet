"use client";

import Link from "next/link";
import { useId, useState } from "react";

import { reconcileFromText, ReconcileResponse } from "@/lib/api";
import { ClipboardCheck } from "lucide-react";

const KIND_LABEL: Record<string, string> = {
  missing_in_book: "Broker has it, book doesn't",
  extra_in_book: "Book has it, broker doesn't",
  quantity_mismatch: "Share count disagrees",
};

type ReconcilePanelProps = {
  id?: string;
  initiallyOpen?: boolean;
};

export default function ReconcilePanel({ id, initiallyOpen = false }: ReconcilePanelProps) {
  const [open, setOpen] = useState(initiallyOpen);
  const panelId = useId();
  const [text, setText] = useState("");
  const [listedOnly, setListedOnly] = useState(false);
  const [busy, setBusy] = useState<"compare" | "queue" | null>(null);
  const [queued, setQueued] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ReconcileResponse | null>(null);

  async function run(queue: boolean) {
    if (!text.trim() || busy) return;
    setBusy(queue ? "queue" : "compare");
    setError(null);
    try {
      setResult(await reconcileFromText(text, queue, listedOnly));
      setQueued(queue);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Reconcile failed.");
    } finally {
      setBusy(null);
    }
  }

  function invalidateResult() {
    setResult(null);
    setQueued(false);
    setError(null);
  }

  const discrepancyCount = (result?.discrepancies.length ?? 0) + Number(Boolean(result?.cash_discrepancy));

  return (
    <section id={id} className="rounded-lg border border-slate-200 bg-white p-5 dark:border-slate-800 dark:bg-[#111]">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-controls={panelId}
        className="flex w-full items-center justify-between text-left"
      >
        <div className="flex items-start gap-3">
          <div className="mt-0.5 rounded-lg bg-sky-50 p-2 text-sky-600 dark:bg-sky-950/30 dark:text-sky-300">
            <ClipboardCheck className="h-4 w-4" />
          </div>
          <div>
          <h2 className="text-sm font-semibold">Compare a broker statement</h2>
          <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
            Paste current holdings or a USD cash balance to check the recorded ledger. You can review the differences before adding them to the queue.
          </p>
          </div>
        </div>
        <span className="ml-4 text-xs text-slate-400">{open ? "Hide" : "Open"}</span>
      </button>

      {open ? (
        <div id={panelId} className="mt-4 space-y-3">
          <label htmlFor={`${panelId}-statement`} className="block text-sm font-medium">Statement rows</label>
          <textarea
            id={`${panelId}-statement`}
            value={text}
            onChange={(e) => { setText(e.target.value); invalidateResult(); }}
            disabled={Boolean(busy)}
            rows={6}
            placeholder={"One holding per line, e.g.\nEXMPL 10\nDEMO 1.5\nCASH 2500\n\n…or paste a CSV with Symbol/Quantity columns."}
            className="w-full resize-y rounded-lg border border-slate-300 bg-white p-3 font-mono text-sm outline-none focus:border-sky-500 focus:ring-2 focus:ring-sky-500 dark:border-slate-700 dark:bg-slate-900 dark:text-white"
          />
          <div className="flex flex-wrap items-center gap-3">
            <button
              type="button"
              onClick={() => void run(false)}
              disabled={Boolean(busy) || !text.trim()}
              className="inline-flex items-center gap-2 rounded-lg bg-sky-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-sky-700 disabled:opacity-50"
            >
              <ClipboardCheck className="h-4 w-4" />
              {busy === "compare" ? "Comparing…" : "Compare statement"}
            </button>
            <label className="flex items-center gap-2 text-xs text-slate-600 dark:text-slate-300">
              <input type="checkbox" checked={listedOnly} disabled={Boolean(busy)} onChange={(e) => { setListedOnly(e.target.checked); invalidateResult(); }} />
              Compare only listed holdings
            </label>
          </div>

          {error ? <p role="alert" className="text-sm text-rose-700 dark:text-rose-300">{error}</p> : null}

          {result ? (
            <div className="space-y-3">
              <div className="flex flex-wrap gap-2 text-xs">
                <span
                  className={`rounded-none px-3 py-1 font-medium ${
                    result.in_sync
                      ? "bg-emerald-50 text-emerald-700 dark:bg-emerald-950/30 dark:text-emerald-300"
                      : "bg-amber-50 text-amber-800 dark:bg-amber-950/30 dark:text-amber-200"
                  }`}
                >
                  {result.in_sync ? "Compared values match" : discrepancyCount ? `${discrepancyCount} ${discrepancyCount === 1 ? "discrepancy" : "discrepancies"}` : "Comparison incomplete"}
                </span>
                {queued ? (
                  <span className="rounded-none bg-sky-50 px-3 py-1 text-sky-700 dark:bg-sky-950/30 dark:text-sky-300">
                    {result.review_items_created} new review items
                  </span>
                ) : null}
              </div>
              <p className="text-sm text-muted" role="status">
                {result.holdings_scope === "complete" ? "All holdings checked." : result.holdings_scope === "listed" ? "Only listed holdings checked." : "Holdings were not compared."}
                {" "}{result.cash_status === "compared" ? "Cash checked." : result.cash_status === "book_unavailable" ? "No book cash balance is available to compare." : "Statement cash was not supplied."}
              </p>
              {discrepancyCount > 0 ? (
                <div className="flex flex-wrap items-center gap-3">
                  {!queued ? <button type="button" className="workspace-control" disabled={Boolean(busy)} onClick={() => void run(true)}>{busy === "queue" ? "Queuing…" : "Queue discrepancies"}</button> : null}
                  <Link href="/verification?domain=portfolio_data" className="workspace-control">Open portfolio data reviews</Link>
                </div>
              ) : null}

              {result.cash_discrepancy ? (
                <div className="rounded-lg border border-amber-200 bg-amber-50/50 p-3 text-sm dark:border-amber-900/40 dark:bg-amber-950/20">
                  Cash: book ${result.cash_discrepancy.book_cash.toLocaleString()} vs statement $
                  {result.cash_discrepancy.broker_cash.toLocaleString()} (Δ ${result.cash_discrepancy.delta.toLocaleString()})
                </div>
              ) : null}

              {result.discrepancies.length > 0 ? (
                <div className="overflow-x-auto border border-line">
                  <table className="w-full min-w-[30rem] text-sm">
                    <thead className="bg-slate-50 text-left text-xs uppercase tracking-wider text-slate-500 dark:bg-slate-900 dark:text-slate-400">
                      <tr>
                        <th className="px-3 py-2">Ticker</th>
                        <th className="px-3 py-2">Issue</th>
                        <th className="px-3 py-2 text-right">Book</th>
                        <th className="px-3 py-2 text-right">Statement</th>
                        <th className="px-3 py-2 text-right">Δ</th>
                      </tr>
                    </thead>
                    <tbody>
                      {result.discrepancies.map((d) => (
                        <tr key={d.ticker} className="border-t border-slate-100 dark:border-slate-800">
                          <td className="px-3 py-2 font-medium">{d.ticker}</td>
                          <td className="px-3 py-2 text-slate-500 dark:text-slate-400">{KIND_LABEL[d.kind] ?? d.kind}</td>
                          <td className="px-3 py-2 text-right tabular-nums">{d.book_quantity}</td>
                          <td className="px-3 py-2 text-right tabular-nums">{d.broker_quantity}</td>
                          <td className={`px-3 py-2 text-right tabular-nums ${d.delta < 0 ? "text-red-500" : "text-emerald-600"}`}>
                            {d.delta > 0 ? "+" : ""}
                            {d.delta}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : null}
            </div>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
