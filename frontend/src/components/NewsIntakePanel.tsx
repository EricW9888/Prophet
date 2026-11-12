"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "@/lib/api";
import { useWorkspaceReads } from "@/lib/workspace-reads";
import { WorkspaceRead } from "@/components/WorkspaceState";
import HintMarker from "@/components/HintMarker";

type Feed = { id: string; name: string; url: string; enabled: boolean; status: string; error: string | null; last_checked_at: string | null; last_success_at: string | null; entry_count: number; new_count: number };
type Arrival = { id: string; title: string; url: string; published_date: string | null; observed_at: string; outcome: string; error: string | null; evidence_id: string | null };
type Intake = { enabled: boolean; pending_count: number; oldest_pending_at: string | null; poll_seconds: number; fetch_batch_size: number; feeds: Feed[]; total: number; offset: number; items: Arrival[] };

function displayTime(value: string | null, missing = "Not checked") {
  if (!value) return missing;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Time unverified" : date.toLocaleString();
}

const outcomes: Record<string, string> = {
  observed: "Awaiting source fetch", fetching: "Fetching source", ingested_evidence: "Source captured",
  fetch_failed: "Fetch retry pending", fetch_retry_exhausted: "Fetch needs review",
  publication_unverified: "Publication time unverified", publication_outside_window: "Outside intake window",
  superseded_by_feed_update: "Publisher updated this item",
};

export default function NewsIntakePanel() {
  const [data, setData] = useState<Intake | null>(null);
  const [offset, setOffset] = useState(0);
  const [showAll, setShowAll] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const { read, status } = useWorkspaceReads<"news">();
  const load = useCallback(() => read<Intake>("news", `/news-intake?offset=${offset}`, setData), [offset, read]);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => { if (!document.hidden) void load(); }, 60_000);
    return () => window.clearInterval(timer);
  }, [load]);

  async function changeFeed(feed: Feed) {
    setBusy(true); setError(null);
    try {
      await apiFetch(`/news-intake/feeds/${feed.id}`, { method: "PATCH", body: JSON.stringify({ enabled: !feed.enabled }) });
      await load();
    } catch (e) { setError(e instanceof Error ? e.message : "Feed change failed."); }
    finally { setBusy(false); }
  }

  async function addFeed(event: React.FormEvent) {
    event.preventDefault(); setBusy(true); setError(null);
    try {
      await apiFetch("/news-intake/feeds", { method: "POST", body: JSON.stringify({ name, url }) });
      setName(""); setUrl(""); await load();
    } catch (e) { setError(e instanceof Error ? e.message : "Feed could not be added."); }
    finally { setBusy(false); }
  }

  return <section className="mb-6 border border-line bg-panel p-4" aria-label="Current news intake">
    <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
      <div className="flex items-center gap-2"><h2 className="font-semibold">Current news intake</h2>
        <HintMarker label="About news intake">Publisher headlines are provisional leads. Captured means the source page was fetched, not that its claims or portfolio impact were verified. Publisher dates can describe a live page’s original publication; the discovery timestamp records when Prophet saw this version.</HintMarker>
      </div>
      <button type="button" className="border border-line px-3 py-1 text-sm" onClick={() => void load()}>Refresh</button>
    </div>
    <WorkspaceRead name="news intake" state={status.news} retry={() => void load()}>
      {data ? <>
        <p className="mb-3 text-sm text-muted">{data.enabled ? "Collecting publisher feeds" : "Automatic intake disabled"} · {data.pending_count} awaiting source capture · {data.total} retained observations
          <HintMarker label="Intake coverage and backlog">Checks run every {data.poll_seconds} seconds and fetch up to {data.fetch_batch_size} source pages per check. New and changed headlines are retained separately. One fetch slot serves the oldest lead so lower-ranked items are not silently discarded. {data.oldest_pending_at ? `Oldest pending discovery: ${displayTime(data.oldest_pending_at)}.` : "No pending source fetches."} This feed set is partial market coverage.</HintMarker>
        </p>
        <details className="mb-3 border border-line p-3">
          <summary className="cursor-pointer text-sm">Publisher feeds · {data.feeds.filter(f => f.enabled).length} enabled</summary>
          <div className="mt-3 space-y-3">{data.feeds.map(feed => <div key={feed.id} className="flex flex-wrap items-start justify-between gap-2 border-t border-line pt-2 text-sm">
            <div className="min-w-0"><a href={feed.url} target="_blank" rel="noreferrer" className="break-all text-action underline">{feed.name}</a>
              <p className={feed.status === "error" ? "text-red-400" : "text-muted"}>{feed.enabled ? feed.status === "ok" ? `${feed.entry_count} entries · ${feed.new_count} new on last check` : feed.status.replaceAll("_", " ") : "Paused"}</p>
              <p className="text-xs text-muted">Last checked {displayTime(feed.last_checked_at)} · last successful {displayTime(feed.last_success_at)}</p>
              {feed.error ? <p className="max-w-prose break-words text-red-400">{feed.error}</p> : null}
            </div><button type="button" disabled={busy} className="border border-line px-3 py-1 disabled:opacity-50" onClick={() => void changeFeed(feed)}>{feed.enabled ? "Pause" : "Resume"}</button>
          </div>)}</div>
          <form onSubmit={addFeed} className="mt-4 grid gap-2 sm:grid-cols-[minmax(0,1fr)_minmax(0,2fr)_auto]">
            <input aria-label="Feed name" placeholder="Publisher name" required maxLength={120} className="min-w-0 border border-line bg-background px-2 py-2 text-sm" value={name} onChange={e => setName(e.target.value)} />
            <input aria-label="Feed URL" placeholder="RSS or Atom URL" required type="url" maxLength={2000} className="min-w-0 border border-line bg-background px-2 py-2 text-sm" value={url} onChange={e => setUrl(e.target.value)} />
            <button disabled={busy} className="border border-line px-3 py-1 text-sm disabled:opacity-50">Add feed</button>
          </form>
        </details>
        {error ? <p role="alert" className="mb-3 text-sm text-red-400">{error}</p> : null}
        <div className="divide-y divide-line">{(showAll ? data.items : data.items.slice(0, 5)).map(item => <article key={item.id} className="py-3">
          <div className="flex flex-wrap items-start justify-between gap-2"><a className="min-w-0 flex-1 break-words text-sm font-medium text-action underline" href={item.url} target="_blank" rel="noreferrer">{item.title}</a>
            {item.evidence_id ? <Link className="text-sm text-action underline" href={`/sources/evidence/${item.evidence_id}`}>Evidence receipt</Link> : null}
            <Link className="text-sm text-action underline" href={`/chat?${new URLSearchParams({ q: `Investigate this provisional publisher report: ${JSON.stringify(item.title)}. Source: ${item.url}. Check the original report, contrary evidence, publication and discovery times, and any material direct or indirect portfolio implications. Separate reported claims from confirmed facts and hypotheses.` })}`}>Research draft</Link>
          </div>
          <p className="mt-1 text-xs text-muted">{outcomes[item.outcome] || item.outcome.replaceAll("_", " ")} · Discovered {displayTime(item.observed_at)} · Publisher time {displayTime(item.published_date, "Unverified")}</p>
          {item.error ? <p className="mt-1 break-words text-xs text-red-400">{item.error}</p> : null}
        </article>)}</div>
        {!data.items.length ? <p className="py-3 text-sm text-muted">No publisher observations retained yet. Feed status above shows whether intake has checked successfully.</p> : null}
        {!showAll && data.total > 5 ? <button type="button" className="mt-3 text-sm text-action underline" onClick={() => setShowAll(true)}>Browse all retained observations</button> : null}
        {showAll && data.total > 20 ? <div className="mt-3 flex items-center gap-3 text-sm">
          <button className="border border-line px-3 py-1 disabled:opacity-50" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 20))}>Previous</button>
          <span className="text-muted">{data.offset + 1}–{data.offset + data.items.length} of {data.total}</span>
          <button className="border border-line px-3 py-1 disabled:opacity-50" disabled={offset + 20 >= data.total} onClick={() => setOffset(offset + 20)}>Next</button>
        </div> : null}
      </> : null}
    </WorkspaceRead>
  </section>;
}
