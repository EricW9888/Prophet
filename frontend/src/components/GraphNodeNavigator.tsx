"use client";

import { useMemo, useState } from "react";
import { formatUserLabel } from "@/lib/formatting";
import type { GraphWebNode } from "@/lib/api";

type BrowseNode = GraphWebNode & {degree?: number};
export type BrowseSort = "connected" | "newest" | "name";

/**
 * Order records for the list. "Most connected" puts the records with the most
 * stored connections first (the best results at the top); "Name" is A to Z.
 * Subjects (entities and themes) lead in the library for both. "Newest" is
 * what was recorded most recently, across every kind; records without a
 * time go last.
 */
function recorded(node: BrowseNode): number {
  const time = node.created_at ? Date.parse(node.created_at) : Number.NaN;
  return Number.isNaN(time) ? Number.NEGATIVE_INFINITY : time;
}

export function sortBrowseNodes<T extends BrowseNode>(nodes: T[], sort: BrowseSort, library: boolean): T[] {
  return [...nodes].sort((a, b) => {
    if (!library) return Number(b.is_root) - Number(a.is_root) || a.label.localeCompare(b.label);
    if (sort === "newest") return recorded(b) - recorded(a) || a.label.localeCompare(b.label);
    const subject = Number(!["entity", "theme"].includes(a.node_type)) - Number(!["entity", "theme"].includes(b.node_type));
    if (subject) return subject;
    if (sort === "connected") return (b.degree ?? 0) - (a.degree ?? 0) || a.label.localeCompare(b.label);
    return a.label.localeCompare(b.label);
  });
}

/** The specific kind ("company", "person") when known, else the record type. */
export function browseKind(node: Pick<GraphWebNode, "node_type" | "subtitle">): string {
  const subtitle = (node.subtitle ?? "").trim();
  return node.node_type === "entity" && subtitle && !subtitle.includes(" ") ? formatUserLabel(subtitle) : formatUserLabel(node.node_type);
}

export default function GraphNodeNavigator({ nodes, onSelect, library = false, search }: {
  nodes: BrowseNode[]; onSelect: (node: BrowseNode) => void; library?: boolean;
  search?: {value: string; onChange: (value: string) => void; remote: boolean; status?: string};
}) {
  const [localQuery, setQuery] = useState("");
  const query = search?.value ?? localQuery;
  const [expanded, setExpanded] = useState(library);
  const [offset, setOffset] = useState(0);
  const [sort, setSort] = useState<BrowseSort>("connected");
  const matches = useMemo(() => {
    const text = query.trim().toLocaleLowerCase();
    return sortBrowseNodes(
      nodes.filter(node => search?.remote || `${node.label} ${node.node_type} ${node.subtitle ?? ""}`.toLocaleLowerCase().includes(text)),
      sort,
      library,
    );
  }, [nodes, query, library, search?.remote, sort]);
  const pageOffset = Math.min(offset, Math.max(0, Math.ceil(matches.length / 20) - 1) * 20);
  const page = matches.slice(pageOffset, pageOffset + 20);
  return <section aria-label="Find a graph node" className="mt-3 border-t border-line pt-3">
    <div className="flex flex-wrap items-center gap-2">
      <input type="search" aria-label={library ? "Search all stored knowledge" : "Find in this graph"} placeholder={library ? "Search all stored knowledge…" : "Find a holding, company, claim or source…"}
        value={query} onFocus={() => setExpanded(true)} onChange={event => {(search?.onChange ?? setQuery)(event.target.value); setOffset(0); setExpanded(true);}}
        className="workspace-control min-w-0 flex-1" />
      {library && !search?.remote ? <label className="inline-flex items-center gap-2 text-sm text-muted">
        Sort
        <select className="workspace-control" value={sort} onChange={event => {setSort(event.target.value as BrowseSort); setOffset(0);}}>
          <option value="connected">Most connected</option>
          <option value="newest">Newest first</option>
          <option value="name">Name A–Z</option>
        </select>
      </label> : null}
      {!library && <button className="workspace-control" aria-expanded={expanded} aria-controls="graph-node-results" onClick={() => setExpanded(value => !value)}>{expanded ? "Hide node list" : `Browse ${nodes.length} nodes`}</button>}
    </div>
    {expanded && <div id="graph-node-results" className="mt-3">
      {search?.status ? <p role="status" className="mb-2 text-sm text-muted">{search.status}</p> : <p className="mb-2 text-sm text-muted">{search?.remote ? `${matches.length} results across stored knowledge (up to 12 best matches).` : `${matches.length} records.`} {library ? "Choose one to see its evidence and connections." : "Choose one to center its connections and open its details."}</p>}
      <ul className={`${library ? "" : "max-h-80 overflow-auto"} divide-y divide-line`}>{page.map(node => <li key={node.key}>
        <button className="flex w-full items-baseline justify-between gap-4 px-3 py-2 text-left hover:bg-panel-muted focus-visible:outline-2 focus-visible:outline-current" onClick={() => {onSelect(node); setExpanded(false);}}>
          <span className="min-w-0 truncate text-sm font-medium" title={node.label}>{node.label}</span>
          <span className="shrink-0 text-xs text-muted">{browseKind(node)}{node.degree !== undefined ? ` · ${node.degree} connections` : ""}</span>
        </button>
      </li>)}</ul>
      {matches.length > 20 && <div className="mt-2 flex items-center gap-3 text-sm">
        <button className="workspace-control" disabled={!pageOffset} onClick={() => setOffset(Math.max(0, pageOffset - 20))}>Previous</button>
        <span>{pageOffset + 1}–{Math.min(pageOffset + 20, matches.length)} of {matches.length}</span>
        <button className="workspace-control" disabled={pageOffset + 20 >= matches.length} onClick={() => setOffset(pageOffset + 20)}>Next</button>
      </div>}
      {!matches.length && !search?.status && <p className="py-3 text-sm text-muted">No matching records. Try another search or change the type filters above.</p>}
    </div>}
  </section>;
}
