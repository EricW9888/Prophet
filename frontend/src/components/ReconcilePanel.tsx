"use client";

import { useState } from "react";

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
  const [text, setText] = useState("");
  const [createReviewItems, setCreateReviewItems] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ReconcileResponse | null>(null);

  async function run() {
    if (!text.trim() || busy) return;
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      setResult(await reconcileFromText(text, createReviewItems));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Reconcile failed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section id={id} className="rounded-lg border border-slate-200 bg-white p-5 dark:border-slate-800 dark:bg-[#111]">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between text-left"
      >
        <div className="flex items-start gap-3">
          <div className="mt-0.5 rounded-lg bg-sky-50 p-2 text-sky-600 dark:bg-sky-950/30 dark:text-sky-300">
            <ClipboardCheck className="h-4 w-4" />
          </div>
          <div>
          <h2 className="text-sm font-semibold">Reconcile broker truth</h2>
          <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
            Paste a current statement when an email-parsed trade looks wrong, a fill is missing, or the reconstructed book needs an authoritative check.
          </p>
          </div>
        </div>
        <span className="ml-4 text-xs text-slate-400">{open ? "Hide" : "Open"}</span>
      </button>

      {open ? (
        <div className="mt-4 space-y-3">
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            rows={6}
            placeholder={"One holding per line, e.g.\nEXMPL 10\nDEMO 1.5\nCASH 2500\n\n…or paste a CSV with Symbol/Quantity columns."}
            className="w-full resize-y rounded-lg border border-slate-300 bg-white p-3 font-mono text-sm outline-none focus:border-sky-500 focus:ring-2 focus:ring-sky-500 dark:border-slate-700 dark:bg-slate-900 dark:text-white"
          />
          <div className="flex flex-wrap items-center gap-3">
            <button
              type="button"
              onClick={run}
              disabled={busy || !text.trim()}
              className="inline-flex items-center gap-2 rounded-lg bg-sky-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-sky-700 disabled:opacity-50"
            >
              <ClipboardCheck className="h-4 w-4" />
              {busy ? "Comparing…" : "Compare statement"}
            </button>
            <label className="flex items-center gap-2 text-xs text-slate-600 dark:text-slate-300">
              <input type="checkbox" checked={createReviewItems} onChange={(e) => setCreateReviewItems(e.target.checked)} />
              Queue discrepancies for review
            </label>
          </div>

          {error ? <p className="text-sm text-red-500">{error}</p> : null}

          {result ? (
            <div className="space-y-3">
              <div className="flex flex-wrap gap-2 text-xs">
                <span
                  className={`rounded-full px-3 py-1 font-medium ${
                    result.in_sync
                      ? "bg-emerald-50 text-emerald-700 dark:bg-emerald-950/30 dark:text-emerald-300"
                      : "bg-amber-50 text-amber-800 dark:bg-amber-950/30 dark:text-amber-200"
                  }`}
                >
                  {result.in_sync ? "In sync with the statement" : `${result.discrepancies.length} discrepancy(ies)`}
                </span>
                {result.review_items_created > 0 ? (
                  <span className="rounded-full bg-sky-50 px-3 py-1 text-sky-700 dark:bg-sky-950/30 dark:text-sky-300">
                    {result.review_items_created} review item(s) created
                  </span>
                ) : null}
              </div>

              {result.cash_discrepancy ? (
                <div className="rounded-lg border border-amber-200 bg-amber-50/50 p-3 text-sm dark:border-amber-900/40 dark:bg-amber-950/20">
                  Cash: book ${result.cash_discrepancy.book_cash.toLocaleString()} vs statement $
                  {result.cash_discrepancy.broker_cash.toLocaleString()} (Δ ${result.cash_discrepancy.delta.toLocaleString()})
                </div>
              ) : null}

              {result.discrepancies.length > 0 ? (
                <div className="overflow-hidden rounded-lg border border-slate-200 dark:border-slate-800">
                  <table className="w-full text-sm">
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
