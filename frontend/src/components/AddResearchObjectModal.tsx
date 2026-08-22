"use client";

import { useState } from "react";

import { API_BASE, ResearchObjectResult } from "@/lib/api";

export default function AddResearchObjectModal({
  isOpen,
  onClose,
  onSaved,
}: {
  isOpen: boolean;
  onClose: () => void;
  onSaved: (result: ResearchObjectResult) => void;
}) {
  const [ticker, setTicker] = useState("");
  const [entityName, setEntityName] = useState("");
  const [listType, setListType] = useState("watchlist");
  const [conviction, setConviction] = useState("");
  const [summary, setSummary] = useState("");
  const [bullCase, setBullCase] = useState("");
  const [bearCase, setBearCase] = useState("");
  const [openQuestions, setOpenQuestions] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!isOpen) return null;

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const response = await fetch(`${API_BASE}/portfolio/research-objects`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ticker,
          entity_name: entityName || null,
          list_type: listType,
          conviction: conviction ? Number(conviction) : null,
          summary: summary || null,
          bull_case: bullCase || null,
          bear_case: bearCase || null,
          open_questions: openQuestions
            .split("\n")
            .map((item) => item.trim())
            .filter(Boolean),
        }),
      });
      if (!response.ok) {
        throw new Error(await response.text());
      }
      const result = (await response.json()) as ResearchObjectResult;
      onSaved(result);
      onClose();
      setTicker("");
      setEntityName("");
      setListType("watchlist");
      setConviction("");
      setSummary("");
      setBullCase("");
      setBearCase("");
      setOpenQuestions("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to create research object.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4 backdrop-blur-sm">
      <div className="w-full max-w-2xl overflow-hidden rounded-2xl border border-gray-200 bg-white shadow-xl dark:border-gray-800 dark:bg-gray-900">
        <div className="flex items-center justify-between border-b border-gray-100 bg-gray-50/60 px-6 py-4 dark:border-gray-800 dark:bg-gray-800/50">
          <div>
            <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100">Track Name</h3>
            <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
              Start tracking a watchlist or considering name. Advanced thesis fields are optional.
            </p>
          </div>
          <button onClick={onClose} className="text-gray-400 transition-colors hover:text-gray-600 dark:hover:text-gray-300">
            ✕
          </button>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4 p-6">
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            <Field label="Ticker">
              <input value={ticker} onChange={(e) => setTicker(e.target.value.toUpperCase())} placeholder="e.g. EXMPL" className={inputClass} />
            </Field>
            <Field label="Entity name">
              <input value={entityName} onChange={(e) => setEntityName(e.target.value)} placeholder="Optional company name" className={inputClass} />
            </Field>
          </div>

          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            <Field label="List type">
              <select value={listType} onChange={(e) => setListType(e.target.value)} className={inputClass}>
                <option value="watchlist">Watchlist</option>
                <option value="considering">Considering</option>
                <option value="theme_basket">Theme basket</option>
              </select>
            </Field>
            <Field label="Conviction">
              <input value={conviction} onChange={(e) => setConviction(e.target.value)} type="number" min="1" max="5" placeholder="1-5" className={inputClass} />
            </Field>
          </div>

          <details className="rounded-xl border border-gray-200 p-4 dark:border-gray-800">
            <summary className="cursor-pointer text-sm font-medium text-gray-700 dark:text-gray-300">
              Optional thesis detail
            </summary>
            <div className="mt-4 space-y-4">
              <Field label="Current summary">
                <textarea value={summary} onChange={(e) => setSummary(e.target.value)} rows={3} placeholder="Why is this name on the list?" className={inputClass} />
              </Field>

              <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
                <Field label="Bull case">
                  <textarea value={bullCase} onChange={(e) => setBullCase(e.target.value)} rows={3} placeholder="What would make it attractive?" className={inputClass} />
                </Field>
                <Field label="Bear case">
                  <textarea value={bearCase} onChange={(e) => setBearCase(e.target.value)} rows={3} placeholder="What is the strongest case against it?" className={inputClass} />
                </Field>
              </div>

              <Field label="Open questions">
                <textarea
                  value={openQuestions}
                  onChange={(e) => setOpenQuestions(e.target.value)}
                  rows={4}
                  placeholder={"One per line\nWhat needs to be true?\nWhat would falsify the thesis?\nWhat benchmark or macro confounders matter?"}
                  className={inputClass}
                />
              </Field>
            </div>
          </details>

          {error ? <p className="text-sm text-red-600 dark:text-red-400">{error}</p> : null}

          <div className="flex justify-end gap-3 pt-2">
            <button type="button" onClick={onClose} className="rounded-md border border-gray-300 px-4 py-2 text-sm font-medium text-gray-700 transition-colors hover:bg-gray-50 dark:border-gray-700 dark:text-gray-300 dark:hover:bg-gray-800">
              Cancel
            </button>
            <button disabled={submitting || !ticker.trim()} type="submit" className="rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-indigo-700 disabled:opacity-50">
              {submitting ? "Creating..." : "Track name"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block space-y-1">
      <span className="text-sm font-medium text-gray-700 dark:text-gray-300">{label}</span>
      {children}
    </label>
  );
}

const inputClass =
  "w-full rounded-md border border-gray-300 px-3 py-2 shadow-sm focus:border-indigo-500 focus:ring-2 focus:ring-indigo-500 dark:border-gray-700 dark:bg-gray-950 dark:text-gray-100";
