"use client";

import { useMemo, useState } from "react";
import { formatUserLabel } from "@/lib/formatting";
import type { GraphWebNode } from "@/lib/api";

type BrowseNode = GraphWebNode & {degree?: number};

export default function GraphNodeNavigator({ nodes, onSelect, library = false, search }: {
  nodes: BrowseNode[]; onSelect: (node: BrowseNode) => void; library?: boolean;
  search?: {value: string; onChange: (value: string) => void; remote: boolean; status?: string};
}) {
  const [localQuery, setQuery] = useState("");
  const query = search?.value ?? localQuery;
  const [expanded, setExpanded] = useState(library);
  const [offset, setOffset] = useState(0);
  const matches = useMemo(() => {
    const text = query.trim().toLocaleLowerCase();
    return nodes.filter(node => search?.remote || `${node.label} ${node.node_type} ${node.subtitle ?? ""}`.toLocaleLowerCase().includes(text))
      .sort((a, b) => library
        ? Number(!["entity", "theme"].includes(a.node_type)) - Number(!["entity", "theme"].includes(b.node_type)) || a.label.localeCompare(b.label)
        : Number(b.is_root) - Number(a.is_root) || a.label.localeCompare(b.label));
  }, [nodes, query, library, search?.remote]);
  const pageOffset = Math.min(offset, Math.max(0, Math.ceil(matches.length / 20) - 1) * 20);
  const page = matches.slice(pageOffset, pageOffset + 20);
  return <section aria-label="Find a graph node" className="mt-3 border-t border-line pt-3">
    <div className="flex flex-wrap items-center gap-2">
      <input type="search" aria-label={library ? "Search all stored knowledge" : "Find in this graph"} placeholder={library ? "Search all stored knowledge…" : "Find a holding, company, claim or source…"}
        value={query} onFocus={() => setExpanded(true)} onChange={event => {(search?.onChange ?? setQuery)(event.target.value); setOffset(0); setExpanded(true);}}
        className="workspace-control min-w-0 flex-1" />
      {!library && <button className="workspace-control" aria-expanded={expanded} aria-controls="graph-node-results" onClick={() => setExpanded(value => !value)}>{expanded ? "Hide node list" : `Browse ${nodes.length} nodes`}</button>}
    </div>
    {expanded && <div id="graph-node-results" className="mt-3">
      {search?.status ? <p role="status" className="mb-2 text-sm text-muted">{search.status}</p> : <p className="mb-2 text-sm text-muted">{search?.remote ? `${matches.length} results across stored knowledge (up to 12 best matches).` : `${matches.length} records in this loaded view.`} {library ? "Choose one to inspect its evidence and connected neighborhood." : "Choose one to center its connections and open its details."}</p>}
      <ul className={`${library ? "" : "max-h-80 overflow-auto"} divide-y divide-line`}>{page.map(node => <li key={node.key}>
        <button className="w-full p-3 text-left hover:bg-panel focus-visible:outline-2 focus-visible:outline-current" onClick={() => {onSelect(node); setExpanded(false);}}>
          <span className="block break-words text-sm font-medium">{node.label}</span>
          <span className="mt-1 block text-xs text-muted">{formatUserLabel(node.node_type)}{node.degree !== undefined ? ` · ${node.degree} connections in this view` : ""}{node.subtitle ? ` · ${node.subtitle}` : ""}</span>
        </button>
      </li>)}</ul>
      {matches.length > 20 && <div className="mt-2 flex items-center gap-3 text-sm">
        <button className="workspace-control" disabled={!pageOffset} onClick={() => setOffset(Math.max(0, pageOffset - 20))}>Previous nodes</button>
        <span>{pageOffset + 1}–{Math.min(pageOffset + 20, matches.length)} of {matches.length}</span>
        <button className="workspace-control" disabled={pageOffset + 20 >= matches.length} onClick={() => setOffset(pageOffset + 20)}>Next nodes</button>
      </div>}
      {!matches.length && !search?.status && <p className="py-3 text-sm text-muted">No matching records. Try another search or change the view filters.</p>}
    </div>}
  </section>;
}
