"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { apiFetch } from "@/lib/api";
import { useWorkspaceReads } from "@/lib/workspace-reads";
import { WorkspaceRead } from "@/components/WorkspaceState";
import HintMarker from "@/components/HintMarker";

type Listing = { id: string; symbol: string; name: string; exchange: string; instrument_type: string;
  research_eligible: boolean; operator_excluded: boolean; member_id: string | null; observed_at: string; error: string | null };
export type ListingCoverage = { scope: "manual" | "us_listed"; total: number; eligible: number; materialized: number;
  match_count: number; offset: number; items: Listing[]; directories: Array<{url: string; status: string; error: string | null; last_success_at: string | null}> };

export default function ListingUniversePanel({ onCoverage }: { onCoverage: (value: ListingCoverage) => void }) {
  const [data, setData] = useState<ListingCoverage | null>(null);
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { read, status } = useWorkspaceReads<"directory">();
  const update = useCallback((value: ListingCoverage) => { setData(value); onCoverage(value); }, [onCoverage]);
  const load = useCallback(() => read<ListingCoverage>("directory", `/opportunities/listings?${new URLSearchParams({offset: String(offset), q: query})}`, update), [offset, query, read, update]);
  useEffect(() => {
    void load();
    const timer = window.setInterval(() => { if (!document.hidden) void load(); }, 60_000);
    return () => window.clearInterval(timer);
  }, [load]);

  async function setScope(scope: "manual" | "us_listed") {
    setBusy(true); setError(null);
    try {
      await apiFetch("/integrations/settings", {method: "PUT", body: JSON.stringify({opportunity_discovery: {universe_scope: scope}})});
      await load();
    } catch (e) { setError(e instanceof Error ? e.message : "Research scope could not be changed."); }
    finally { setBusy(false); }
  }

  async function review(listing: Listing) {
    setBusy(true); setError(null);
    try {
      await apiFetch(`/opportunities/listings/${listing.id}`, {method: "PATCH", body: JSON.stringify({excluded: !listing.operator_excluded})});
      await load();
    } catch (e) { setError(e instanceof Error ? e.message : "Listing choice could not be saved."); }
    finally { setBusy(false); }
  }

  return <section aria-label="Research scope" className="mb-5 border border-line bg-panel p-4">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div className="flex items-center gap-2"><h2 className="font-semibold">Research scope</h2>
        <HintMarker label="About the research universe">Exchange metadata defines a research frontier; it is not an investment recommendation or proof that a company has been investigated. Common and ordinary shares and depositary shares with an explicit type can join in bounded batches. ETFs, test issues and unresolved types remain outside automatic company research. Directory descriptions do not establish a complete issuer or fund classification. Discovery prioritizes recently researched subjects and reserves capacity for unexplored listings. Current frontier only stops expansion while retaining prepared subjects and your pause decisions.</HintMarker>
      </div>
      <div className="flex flex-wrap gap-2">
        <select aria-label="Research scope" className="workspace-control" value={data?.scope ?? "manual"} disabled={busy || !data} onChange={e => void setScope(e.target.value as "manual" | "us_listed")}>
          <option value="manual">Current frontier only</option><option value="us_listed">Broad U.S.-listed companies</option>
        </select>
        <button className="workspace-control" onClick={() => void load()}>Refresh</button>
      </div>
    </div>
    <WorkspaceRead name="listing directory" state={status.directory} retry={() => void load()}>
      {data && <>
        <p className="mt-3 text-sm text-muted">{data.total.toLocaleString()} directory listings · {data.eligible.toLocaleString()} eligible for company research · {data.materialized.toLocaleString()} prepared frontier links. Preparation is separate from completed investigation.</p>
        {data.scope === "us_listed" && !data.total && <p className="mt-2 text-sm text-muted">The approved broad scope is waiting for its first directory check. Feed status below shows acquisition progress.</p>}
        <details className="mt-3 border-t border-line pt-3">
          <summary className="cursor-pointer text-sm">Browse directory and coverage</summary>
          <div className="my-3 space-y-2">{data.directories.map(source => <div key={source.url} className="text-sm">
            <a href={source.url} target="_blank" rel="noreferrer" className="break-all text-action underline">{source.url}</a>
            <p className="text-muted">{source.status.replaceAll("_", " ")} · {source.last_success_at ? `Last successful check ${new Date(source.last_success_at).toLocaleString()}` : "No successful check yet"}</p>
            {source.error && <p className="break-words text-negative">{source.error}</p>}
          </div>)}</div>
          <form className="my-3 flex gap-2" onSubmit={e => {e.preventDefault(); setOffset(0); setQuery(draft);}}>
            <input className="workspace-control min-w-0 flex-1" type="search" aria-label="Find a directory listing" placeholder="Ticker or company" value={draft} onChange={e => setDraft(e.target.value)} />
            <button className="workspace-control">Search</button>
          </form>
          <ul className="divide-y divide-line">{data.items.map(listing => <li key={listing.id} className="py-3">
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div className="min-w-0 flex-1"><p className="break-words font-medium">{listing.symbol} · {listing.name}</p>
                <p className="mt-1 text-xs text-muted">{listing.exchange} · {listing.instrument_type.replaceAll("_", " ")} · {listing.operator_excluded ? "Excluded by you" : listing.member_id ? "Prepared for research" : listing.research_eligible ? "Awaiting frontier capacity" : "Outside automatic company research"}</p>
                {listing.error && <p className="mt-1 text-sm text-negative">{listing.error}</p>}
              </div>
              {listing.research_eligible && <button className="workspace-control" disabled={busy} onClick={() => void review(listing)}>{listing.operator_excluded ? "Allow research" : "Exclude"}</button>}
            </div>
            <Link className="mt-1 inline-block text-sm text-action underline" href={`/chat?${new URLSearchParams({q:`Investigate ${listing.name}, listed as ${listing.symbol} on ${listing.exchange}. Verify the exact instrument and listing status before evaluating current developments, valuation, risks and any investment hypothesis. Directory metadata alone is not evidence of investment merit.`})}`}>Research draft</Link>
          </li>)}</ul>
          {!data.items.length && <p className="text-sm text-muted">No directory entries match this view.</p>}
          {data.match_count > 20 && <div className="mt-3 flex flex-wrap items-center gap-3 text-sm">
            <button className="workspace-control" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset-20))}>Previous</button>
            <span>{data.offset+1}–{data.offset+data.items.length} of {data.match_count.toLocaleString()}</span>
            <button className="workspace-control" disabled={offset+20 >= data.match_count} onClick={() => setOffset(offset+20)}>Next</button>
          </div>}
        </details>
      </>}
    </WorkspaceRead>
    {error && <p role="alert" className="mt-3 text-sm text-negative">{error}</p>}
  </section>;
}
