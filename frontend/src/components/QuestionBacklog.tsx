"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch } from "@/lib/api";
import Link from "next/link";

type Question = {
  id: string; question_text: string; status: string; owner: string;
  next_action: string | null; next_review_at: string | null;
  last_material_outcome: string; research_attempt_count: number;
};
type Backlog = {
  historical: number; eligible_active: number; eligible_due: number;
  max_research_attempts: number; outcomes_last_24h: Record<string, number>;
  total: number; offset: number;
  items: Question[];
};
type History = { total: number; items: Array<{ id: string; event_type: string; detail: Record<string, unknown>; created_at: string }> };
const label = (value: string) => value.replaceAll("_", " ");

export default function QuestionBacklog() {
  const [data, setData] = useState<Backlog | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [history, setHistory] = useState<History | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [view, setView] = useState<"active" | "history">("active");
  const [offset, setOffset] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  const requestVersion = useRef(0);
  const load = useCallback(async () => {
    const version = ++requestVersion.current;
    try {
      const result = await apiFetch<Backlog>(`/automation/questions?view=${view}&offset=${offset}`);
      if (version !== requestVersion.current) return false;
      setData(result); setError(null); return true;
    }
    catch (err) { if (version === requestVersion.current) setError(err instanceof Error ? err.message : "Could not load research work"); return false; }
  }, [view, offset]);
  useEffect(() => { void load(); }, [load]);
  async function show(id: string, more = false) {
    setBusy(true); setError(null);
    if (!more) { setSelected(id); setHistory(null); setReason(""); }
    try {
      const result = await apiFetch<History>(`/automation/questions/${id}/history?offset=${more ? history?.items.length ?? 0 : 0}`);
      setHistory(current => more && current ? { ...result, items: [...current.items, ...result.items] } : result);
    } catch (err) { setError(err instanceof Error ? err.message : "Could not load question history"); }
    finally { setBusy(false); }
  }
  async function decide(id: string, action: "renew" | "retire") {
    setBusy(true); setError(null); setNotice(null);
    try {
      await apiFetch(`/automation/questions/${id}/decision`, { method: "POST", body: JSON.stringify({ action, reason }) });
      setNotice(action === "renew" ? "Research budget renewed. The question is scheduled in eligible active work." : "Question retired. Its history remains available in Retained question history.");
      if (await load()) await show(id);
    } catch (err) { setError(err instanceof Error ? err.message : "Decision could not be saved"); }
    finally { setBusy(false); }
  }
  return <section className="min-w-0 rounded-lg border border-line bg-panel p-4 md:p-5">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div><h2 className="text-sm font-semibold">Research work</h2>
        <p className="mt-1 text-xs text-slate-500">Eligible portfolio questions and their next actions. Older prompts remain in history.</p></div>
      <button onClick={() => void load()} className="text-xs text-sky-600">Refresh work</button>
    </div>
    {error && <p role="alert" className="mt-3 text-sm text-amber-600">{error}</p>}
    {notice && <p role="status" className="mt-3 text-sm text-sky-600">{notice}</p>}
    {!data && !error && <p className="mt-3 text-sm text-slate-500">Loading research work…</p>}
    {data && <>
      <div className="my-4 grid grid-cols-3 gap-3 text-sm">
        <p><strong>{data.eligible_due}</strong><span className="block text-xs text-slate-500">Due now</span></p>
        <p><strong>{data.eligible_active}</strong><span className="block text-xs text-slate-500">Eligible active</span></p>
        <p><strong>{data.historical}</strong><span className="block text-xs text-slate-500">All retained questions</span></p>
      </div>
      <p className="mb-3 text-xs text-slate-500">Last 24 hours: {Object.entries(data.outcomes_last_24h).map(([key, count]) => `${count} ${label(key)}`).join(" · ") || "No material outcomes recorded"}.</p>
      <details><summary className="cursor-pointer text-xs text-sky-600">Inspect questions and history</summary>
        <label className="mt-3 block text-xs">Question view
          <select value={view} onChange={event => { setView(event.target.value as "active" | "history"); setOffset(0); setSelected(null); setHistory(null); }} className="ml-2 rounded border border-line bg-panel p-2">
            <option value="active">Eligible active work</option><option value="history">Retained question history</option>
          </select>
        </label>
        {data.items.length === 0 && <p className="mt-3 text-sm text-slate-500">No questions to inspect yet. Add a tracked subject or research evidence to begin.</p>}
        <div className="mt-3 space-y-3">{data.items.map(question => <article key={question.id} className="min-w-0 border-t border-line pt-3">
          <p className="break-words text-sm">{question.question_text}</p>
          <p className="mt-1 text-xs text-slate-500">{label(question.status)} · {label(question.last_material_outcome)} · {question.research_attempt_count}/{data.max_research_attempts} research attempts</p>
          <p className="mt-1 text-xs text-slate-500">Owner: {label(question.owner)} · Next: {question.next_action ? label(question.next_action) : "No scheduled work"}{question.next_review_at ? ` · ${new Date(question.next_review_at).toLocaleString()}` : ""}</p>
          <button disabled={busy} onClick={() => void show(question.id)} className="mt-2 text-xs text-sky-600 disabled:opacity-50">View history and manage</button>
          {selected === question.id && <div className="mt-3 space-y-3">
            {busy && !history && <p className="text-xs text-slate-500">Loading history…</p>}
            {history && <><ol className="max-h-64 space-y-2 overflow-auto text-xs">{history.items.map(item => <li key={item.id}>
              <p className="font-medium">{label(item.event_type)} · {new Date(item.created_at).toLocaleString()}</p>
              <p className="mt-1 break-words text-slate-500">{[item.detail.question_text, item.detail.assessment_summary, item.detail.remaining_uncertainty, item.detail.reason, item.detail.evidence_title].filter(value => typeof value === "string" && value).join(" · ") || label(String(item.detail.outcome ?? item.detail.action ?? "Recorded lifecycle event"))}</p>
              {typeof item.detail.evidence_id === "string" && <Link href={`/sources/evidence/${item.detail.evidence_id}`} className="mt-1 inline-block text-sky-600">Inspect supporting source</Link>}
              <details className="mt-1 text-slate-500"><summary className="cursor-pointer">Provenance details</summary>
                <pre className="mt-1 whitespace-pre-wrap break-words font-sans">{JSON.stringify(item.detail, null, 2)}</pre>
              </details>
            </li>)}</ol>{history.items.length < history.total && <button disabled={busy} onClick={() => void show(question.id, true)} className="text-xs text-sky-600">Load older history</button>}</>}
            <label className="block text-xs">Reason for changing this question
              <input value={reason} onChange={event => setReason(event.target.value)} maxLength={500} className="mt-1 w-full rounded border border-line bg-transparent p-2" />
            </label>
            <div className="flex flex-wrap gap-3 text-xs">
              <button disabled={busy || !reason.trim()} onClick={() => void decide(question.id, "renew")} className="rounded border border-line px-3 py-2 disabled:opacity-50">Renew research budget</button>
              <button disabled={busy || !reason.trim()} onClick={() => void decide(question.id, "retire")} className="rounded border border-line px-3 py-2 disabled:opacity-50">Retire to history</button>
            </div>
            <p className="text-xs text-slate-500">Renewal schedules another bounded search for a tracked subject. Retirement retains every prompt, evidence outcome and resolution.</p>
          </div>}
        </article>)}</div>
        <div className="mt-4 flex flex-wrap items-center gap-3 text-xs">
          <span>{data.total ? `${data.offset + 1}–${data.offset + data.items.length} of ${data.total}` : "0 questions"}</span>
          <button disabled={offset === 0} onClick={() => { setOffset(value => Math.max(0, value - 20)); setSelected(null); }} className="text-sky-600 disabled:opacity-50">Previous questions</button>
          <button disabled={data.offset + data.items.length >= data.total} onClick={() => { setOffset(value => value + 20); setSelected(null); }} className="text-sky-600 disabled:opacity-50">Next questions</button>
        </div>
      </details>
    </>}
  </section>;
}
