"use client";

import Link from "next/link";
import { useCallback, useEffect, useState, useSyncExternalStore } from "react";
import { type PatternReviewPage, type ReasoningTrace } from "@/lib/api";
import { formatUserLabel } from "@/lib/formatting";
import { isRecordId } from "@/lib/workflows";
import { patternInvestigationHref, record, text } from "@/lib/pattern-reviews";
import { useWorkspaceReads } from "@/lib/workspace-reads";
import RecordFields from "./RecordFields";

const date = (value: unknown) => text(value) && !Number.isNaN(new Date(text(value)).getTime()) ? new Date(text(value)).toLocaleString() : "Unknown";
const button = "border border-line px-3 py-2 text-sm hover:bg-panel-muted disabled:opacity-50";
const statusLabel = (status: string) => ({provisional: "Provisional hypothesis", rejected: "Not admitted", duplicate: "Duplicate", generating: "No completed proposal", validating: "Review not completed", deferred: "Provider deferred", failed: "Failed", interrupted: "Interrupted"}[status] || formatUserLabel(status));
const subscribeHash = (listener: () => void) => {
  window.addEventListener("hashchange", listener);
  return () => window.removeEventListener("hashchange", listener);
};
const selectedHash = () => isRecordId(window.location.hash.slice(1)) ? window.location.hash.slice(1) : null;

export function PatternReviewDetail({ trace }: { trace: ReasoningTrace }) {
  const [captureOpen, setCaptureOpen] = useState(false);
  const receipt = trace.structured_output_json;
  const snapshot = record(receipt.snapshot);
  const proposal = record(receipt.proposal);
  const hypothesis = record(proposal.hypothesis);
  const review = record(receipt.review);
  const policy = record(snapshot.validation_policy);
  const input = record(snapshot.input);
  const sources = Array.isArray(input.source_dated_signals) ? input.source_dated_signals.map(record) : [];
  const refs = Array.isArray(hypothesis.evidence_refs) ? hypothesis.evidence_refs : [];
  sources.sort((left, right) => Number(refs.includes(right.ref)) - Number(refs.includes(left.ref)));
  const missing = refs.filter(ref => !sources.some(source => source.ref === ref));
  return <article className="min-w-0 space-y-5 border border-line bg-panel p-4 sm:p-5">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-0"><p className="text-sm text-muted">{statusLabel(text(review.status))} · {date(trace.created_at)}</p><h2 className="mt-2 break-words text-xl font-semibold">{text(hypothesis.label) || "No hypothesis returned"}</h2></div>
      {text(hypothesis.label) && text(hypothesis.next_test) ? <Link className={button} href={patternInvestigationHref(trace)}>Draft investigation</Link> : null}
    </div>
    <p className="break-words text-sm">{formatUserLabel(text(review.detail))}{review.error_type ? ` · ${text(review.error_type)}` : ""}</p>
    {["generating", "validating"].includes(text(review.status)) ? <p className="text-sm text-amber-500">No completion was recorded. This may be in progress or interrupted; this receipt alone does not establish that the worker is still running.</p> : null}
    <p className="text-sm text-muted">This records a model proposal and its admission decision. Its premise, causal mechanism and investment value still need testing. Draft investigation opens an unsent question in Research.</p>
    <dl className="grid min-w-0 gap-4 md:grid-cols-2">
      {[["Observation", hypothesis.observation], ["Proposed mechanism", hypothesis.proposed_mechanism], ["Why now", hypothesis.why_now], ["What would disprove it", hypothesis.falsifier], ["Next source check", hypothesis.next_test], ["Model's summary", proposal.reasoning_summary]].filter(([, value]) => text(value)).map(([label, value]) => <div key={text(label)} className="min-w-0 border-l border-line pl-3"><dt className="text-sm text-muted">{text(label)}</dt><dd className="mt-1 whitespace-pre-wrap break-words text-sm leading-6">{text(value)}</dd></div>)}
    </dl>
    {proposal.actionable === false ? <p className="text-sm text-muted">The model reported no actionable pattern. This does not establish that the packet or market contained no useful opportunity.</p> : <p className="text-sm text-muted">Model-reported confidence: {typeof hypothesis.confidence === "number" ? hypothesis.confidence.toFixed(2) : "Not returned"} · Admission setting at capture: {typeof policy.minimum_confidence === "number" ? policy.minimum_confidence.toFixed(2) : "Unknown"}. Confidence is uncalibrated and is not a probability of profit.</p>}
    <section className="space-y-3 border-t border-line pt-4">
      <h3 className="font-semibold">Captured evidence · {sources.length} signals</h3>
      <p className="text-sm text-muted">Captured {date(snapshot.captured_at)}. Observed in Prophet then; historical availability is not established. Unknown publication times remain unknown. Highlighted citations were selected by the model.</p>
      {missing.length ? <p className="break-words text-sm text-amber-500">Unmatched model references: {missing.map(String).join(", ")}</p> : null}
      <ul className="grid min-w-0 gap-3 lg:grid-cols-2">{sources.map((source, index) => {
        const url = text(source.evidence_url || source.source_url);
        return <li key={`${text(source.ref)}:${index}`} className={`min-w-0 border p-3 text-sm ${refs.includes(source.ref) ? "border-sky-600" : "border-line"}`}>
          <p className="break-words font-medium">{text(source.signal_name) || text(source.ref)}{refs.includes(source.ref) ? " · Cited" : ""}</p>
          <p className="mt-1 break-words text-muted">{text(source.source_name)} · {text(source.lineage_key)}</p>
          <p className="mt-1 text-muted">Published {date(source.public_time)} · Signal recorded {date(source.signal_created_at)}</p>
          {text(source.setup_context) ? <p className="mt-2 whitespace-pre-wrap break-words leading-6">{text(source.setup_context)}</p> : null}
          <div className="mt-2 flex flex-wrap gap-4 text-sky-500">
            {/^https?:\/\//i.test(url) ? <a href={url} target="_blank" rel="noreferrer" className="underline">Original source</a> : null}
            {isRecordId(text(source.raw_evidence_id)) ? <Link href={`/sources/evidence/${source.raw_evidence_id}`} className="underline">Evidence receipt</Link> : null}
          </div>
        </li>;
      })}</ul>
    </section>
    <details className="min-w-0 border-t border-line pt-3" onToggle={event => setCaptureOpen(event.currentTarget.open)}><summary className="cursor-pointer text-sm">Exact capture and configuration</summary>{captureOpen ? <><p className="my-3 break-words text-sm text-muted">{trace.model_used} · Requested model: {trace.model_version || "Provider default; exact model not recorded"} · {trace.duration_ms == null ? "No duration recorded" : `${(trace.duration_ms / 1000).toFixed(1)} seconds`} · Receipt {trace.id}</p><RecordFields value={receipt} raw /></> : null}</details>
  </article>;
}

export default function PatternReviews() {
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<PatternReviewPage | null>(null);
  const [trace, setTrace] = useState<ReasoningTrace | null>(null);
  const selected = useSyncExternalStore(subscribeHash, selectedHash, () => null);
  const { read, status } = useWorkspaceReads<"list" | "trace">();
  const refresh = useCallback(() => read<PatternReviewPage>("list", `/reasoning/pattern-reviews?limit=20&offset=${offset}`, setPage), [read, offset]);
  useEffect(() => { void refresh(); }, [refresh]);
  const pending = page?.items.some(item => ["generating", "validating"].includes(item.status))
    || ["generating", "validating"].includes(text(record(trace?.structured_output_json.review).status));
  useEffect(() => {
    if (!pending) return;
    const timer = window.setInterval(() => {
      if (document.hidden) return;
      void refresh();
      if (selected) void read<ReasoningTrace>("trace", `/reasoning/runs/${selected}`, setTrace, false);
    }, 15000);
    return () => window.clearInterval(timer);
  }, [pending, refresh, selected, read]);
  useEffect(() => {
    if (selected) void read<ReasoningTrace>("trace", `/reasoning/runs/${selected}`, setTrace);
  }, [selected, read]);
  const visibleTrace = trace?.id === selected ? trace : null;
  return <div className="space-y-5 py-5">
    <div className="flex flex-wrap items-center justify-between gap-3"><p className="text-sm text-muted">Inspect what the pattern worker proposed, including ideas it rejected. Receipts begin when this feature is installed; earlier discarded output cannot be recovered.</p><button className={button} onClick={() => { void refresh(); if (selected) void read<ReasoningTrace>("trace", `/reasoning/runs/${selected}`, setTrace); }}>Refresh</button></div>
    {status.list?.error ? <p role="alert" className="text-sm text-red-500">Could not refresh reviews: {status.list.error}</p> : null}
    {!page && !status.list?.error ? <p role="status" className="text-sm text-muted">Loading pattern reviews…</p> : null}
    <div className="grid min-w-0 gap-5 xl:grid-cols-[minmax(250px,340px)_minmax(0,1fr)]">
      <aside className="min-w-0 space-y-3">
        {page?.items.length ? <label className="block text-sm xl:hidden">Retained run
          <select value={selected || ""} onChange={event => { window.location.hash = event.target.value; }} className="mt-2 w-full min-w-0 border border-line bg-panel px-3 py-2 text-foreground">
            <option value="">Choose a retained run</option>
            {selected && !page.items.some(item => item.id === selected) ? <option value={selected}>Selected receipt outside this page</option> : null}
            {page.items.map(item => <option key={item.id} value={item.id}>{item.label || "Pattern search"} · {statusLabel(item.status)} · {date(item.created_at)}</option>)}
          </select>
        </label> : null}
        {!page?.items.length && status.list?.ready ? <p className="text-sm text-muted">No retained pattern runs yet. Source intake and generation can run independently; no receipt does not establish that there were no opportunities.</p> : null}
        <ul className="hidden divide-y divide-line border border-line xl:block">{page?.items.map(item => <li key={item.id}><a href={`#${item.id}`} aria-current={selected === item.id ? "true" : undefined} className={`block min-w-0 p-3 text-sm hover:bg-panel-muted ${selected === item.id ? "bg-panel-muted" : ""}`}><p className="break-words font-medium">{item.label || "Pattern search"}</p><p className="mt-1 text-muted">{statusLabel(item.status)} · {date(item.created_at)}</p><p className="mt-1 break-words text-muted">{formatUserLabel(item.detail)}</p></a></li>)}</ul>
        {page ? <div className="flex flex-wrap items-center justify-between gap-2 text-sm"><button className={button} disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 20))}>Previous</button><span>{page.items.length ? `${page.offset + 1}–${page.offset + page.items.length}` : "0"} of {page.total}</span><button className={button} disabled={page.offset + page.items.length >= page.total} onClick={() => setOffset(offset + 20)}>Next</button></div> : null}
      </aside>
      <div className="min-w-0">{status.trace?.error ? <p role="alert" className="mb-3 text-sm text-red-500">Could not load receipt: {status.trace.error}</p> : null}{visibleTrace ? <PatternReviewDetail trace={visibleTrace} /> : <p role="status" className="border border-line p-5 text-sm text-muted">{selected && !status.trace?.error ? "Loading captured proposal…" : "Select a run to inspect its proposal and evidence."}</p>}</div>
    </div>
  </div>;
}
