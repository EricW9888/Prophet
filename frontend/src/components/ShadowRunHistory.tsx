"use client";

import { useState } from "react";
import { apiFetch, ShadowExperiment } from "@/lib/api";

export default function ShadowRunHistory({ experiment, experiments, loadDetails, refresh }: {
  experiment: ShadowExperiment; experiments: ShadowExperiment[];
  loadDetails: (id: string) => Promise<void>; refresh: () => Promise<void>;
}) {
  const [retained, setRetained] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  async function reconcile() {
    setBusy(true); setMessage(null);
    try {
      await apiFetch(`/shadow/experiments/${experiment.id}/reconcile-family`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ retained_experiment_id: retained, reason: reason.trim() }),
      });
      setMessage("Family equivalence recorded. Prior ledgers, results and lesson links are retained.");
      await refresh();
      await loadDetails(experiment.id);
    } catch (error) { setMessage(error instanceof Error ? error.message : "Unable to reconcile families."); }
    finally { setBusy(false); }
  }
  return (
    <details className="mt-4 rounded border border-slate-200 p-3 text-xs dark:border-slate-800" onToggle={event => { if (event.currentTarget.open) void loadDetails(experiment.id); }}>
      <summary className="cursor-pointer font-medium">Run history and family reconciliation</summary>
      <button type="button" onClick={() => void loadDetails(experiment.id)} className="mt-3 text-sky-700 dark:text-sky-300">Refresh run history</button>
      <p className="mt-3">Current work: {experiment.lifecycle_state}. Planned end: {new Date(experiment.end_point).toLocaleString()}.</p>
      {experiment.run_details.observation_window?.final_captured_at ? <p className="mt-2 text-slate-500">
        Valuation captured {new Date(experiment.run_details.observation_window.final_captured_at).toLocaleString()} · {Math.round((experiment.run_details.observation_window.late_by_seconds ?? 0) / 60)} minutes beyond the planned end.
        {experiment.run_details.observation_window.calibration_allowed === false ? " Excluded from family calibration." : ""}
        {" "}Valuation basis: current or retained deterministic marks.
      </p> : null}
      {experiment.previous_run_id ? <p className="mt-2">New run from retained run {experiment.previous_run_id}.</p> : null}
      {experiment.superseded_by_id ? <p className="mt-2">Superseded by {experiments.find(row => row.id === experiment.superseded_by_id)?.name ?? experiment.superseded_by_id}. {experiment.skip_reason}</p> : null}
      {experiment.lifecycle_history?.map(event => (
        <div key={event.id} className="mt-3 border-t border-slate-100 pt-2 dark:border-slate-800">
          <p>{event.event_type.replaceAll("_", " ")} · {new Date(event.created_at).toLocaleString()}</p>
          {typeof event.detail.reason === "string" ? <p className="mt-1 text-slate-500">{event.detail.reason}</p> : null}
          <details className="mt-1 text-slate-500"><summary className="cursor-pointer">Provenance details</summary><pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-all">{JSON.stringify(event.detail, null, 2)}</pre></details>
        </div>
      ))}
      {!experiment.superseded_by_id && experiment.family_id ? (
        <div className="mt-4 space-y-2 border-t border-slate-200 pt-3 dark:border-slate-800">
          <p className="text-slate-500">Record equivalent legacy hypotheses only after checking the same subject, mechanism, counterfactual and horizon. Active duplicates stop; observations and lesson links remain on their original runs.</p>
          <label className="block">Retain this equivalent run
            <select value={retained} onChange={event => setRetained(event.target.value)} className="mt-1 w-full rounded border border-slate-300 bg-transparent p-2 dark:border-slate-700">
              <option value="">Choose retained run</option>
              {experiments.filter(row => row.id !== experiment.id && row.family_id && !row.superseded_by_id).map(row => <option key={row.id} value={row.id}>{row.name} · {row.run_status}</option>)}
            </select>
          </label>
          <label className="block">Reason for equivalent family
            <input value={reason} maxLength={500} onChange={event => setReason(event.target.value)} className="mt-1 w-full rounded border border-slate-300 bg-transparent p-2 dark:border-slate-700" />
          </label>
          <button type="button" disabled={busy || !retained || !reason.trim()} onClick={() => void reconcile()} className="rounded border border-slate-300 px-3 py-2 disabled:opacity-50 dark:border-slate-700">{busy ? "Recording…" : "Record equivalence and stop duplicates"}</button>
          {message ? <p role="status" className="text-slate-600 dark:text-slate-300">{message}</p> : null}
        </div>
      ) : null}
    </details>
  );
}
