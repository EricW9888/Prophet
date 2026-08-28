"use client";

import { useState } from "react";

import { API_BASE } from "@/lib/api";

export default function AddTransactionModal({
  isOpen,
  onClose,
  onSaved,
}: {
  isOpen: boolean;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [ticker, setTicker] = useState("");
  const [action, setAction] = useState("buy");
  const [executedAt, setExecutedAt] = useState("");
  const [quantity, setQuantity] = useState("");
  const [price, setPrice] = useState("");
  const [notes, setNotes] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!isOpen) return null;

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const response = await fetch(`${API_BASE}/portfolio/transactions/by-ticker`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ticker,
          action,
          quantity: Number(quantity),
          price: Number(price),
          executed_at: new Date(executedAt).toISOString(),
          notes: notes || null,
          list_type: "holding",
          direction: "long",
        }),
      });
      if (!response.ok) {
        throw new Error(await response.text());
      }
      onSaved();
      onClose();
      setTicker("");
      setAction("buy");
      setExecutedAt("");
      setQuantity("");
      setPrice("");
      setNotes("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to save transaction.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50 backdrop-blur-sm animate-in fade-in duration-200">
      <div className="max-h-[calc(100dvh-2rem)] w-full max-w-md overflow-y-auto rounded-lg border border-slate-200 bg-white shadow-xl animate-in zoom-in-95 duration-200 dark:border-slate-800 dark:bg-slate-900">
        <div className="px-6 py-4 border-b border-slate-100 dark:border-slate-800 flex justify-between items-center bg-slate-50/50 dark:bg-slate-800/50">
          <div>
            <h3 className="font-semibold text-lg text-slate-900 dark:text-slate-100">Add Holding Transaction</h3>
            <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
              Use this for manual portfolio bootstrap or corrections. CSV import is available in Setup.
            </p>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-600 dark:hover:text-slate-300 transition-colors">
            ✕
          </button>
        </div>

        <form className="p-6 space-y-4" onSubmit={handleSubmit}>
          <div className="space-y-1">
            <label className="text-sm font-medium text-slate-700 dark:text-slate-300">Ticker</label>
            <input value={ticker} onChange={(e) => setTicker(e.target.value.toUpperCase())} type="text" placeholder="e.g. EXMPL" className="w-full px-3 py-2 border border-slate-300 dark:border-slate-700 rounded-md focus:ring-2 focus:ring-sky-500 focus:border-sky-500 dark:bg-slate-950 dark:text-slate-100" />
          </div>

          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <div className="space-y-1">
              <label className="text-sm font-medium text-slate-700 dark:text-slate-300">Type</label>
              <select value={action} onChange={(e) => setAction(e.target.value)} className="w-full px-3 py-2 border border-slate-300 dark:border-slate-700 rounded-md focus:ring-2 focus:ring-sky-500 focus:border-sky-500 dark:bg-slate-950 dark:text-slate-100">
                <option value="buy">Buy</option>
                <option value="sell">Sell</option>
              </select>
            </div>
            <div className="space-y-1">
              <label className="text-sm font-medium text-slate-700 dark:text-slate-300">Date</label>
              <input value={executedAt} onChange={(e) => setExecutedAt(e.target.value)} type="date" className="w-full px-3 py-2 border border-slate-300 dark:border-slate-700 rounded-md focus:ring-2 focus:ring-sky-500 focus:border-sky-500 dark:bg-slate-950 dark:text-slate-100" />
            </div>
          </div>

          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <div className="space-y-1">
              <label className="text-sm font-medium text-slate-700 dark:text-slate-300">Quantity</label>
              <input value={quantity} onChange={(e) => setQuantity(e.target.value)} type="number" step="0.0001" placeholder="0.00" className="w-full px-3 py-2 border border-slate-300 dark:border-slate-700 rounded-md focus:ring-2 focus:ring-sky-500 focus:border-sky-500 dark:bg-slate-950 dark:text-slate-100" />
            </div>
            <div className="space-y-1">
              <label className="text-sm font-medium text-slate-700 dark:text-slate-300">Price</label>
              <input value={price} onChange={(e) => setPrice(e.target.value)} type="number" step="0.01" placeholder="0.00" className="w-full px-3 py-2 border border-slate-300 dark:border-slate-700 rounded-md focus:ring-2 focus:ring-sky-500 focus:border-sky-500 dark:bg-slate-950 dark:text-slate-100" />
            </div>
          </div>

          <div className="space-y-1">
            <label className="text-sm font-medium text-slate-700 dark:text-slate-300">Notes</label>
            <textarea value={notes} onChange={(e) => setNotes(e.target.value)} rows={3} className="w-full px-3 py-2 border border-slate-300 dark:border-slate-700 rounded-md focus:ring-2 focus:ring-sky-500 focus:border-sky-500 dark:bg-slate-950 dark:text-slate-100" />
          </div>

          {error ? <p className="text-sm text-red-600 dark:text-red-400">{error}</p> : null}

          <div className="pt-4 flex justify-end gap-3">
            <button type="button" onClick={onClose} className="px-4 py-2 text-sm font-medium text-slate-700 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-slate-800 rounded-md transition-colors border border-slate-300 dark:border-slate-700">
              Cancel
            </button>
            <button disabled={submitting || !ticker || !executedAt || !quantity || !price} type="submit" className="px-4 py-2 text-sm font-medium text-white bg-sky-600 hover:bg-sky-700 rounded-md transition-colors disabled:opacity-50">
              {submitting ? "Saving..." : "Save Transaction"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
