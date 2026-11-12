"use client";

import { useMemo, useState } from "react";
import { formatUserLabel } from "@/lib/formatting";
import type { PositionedNode } from "@/lib/graph-layout-geometry";

export default function GraphNodeNavigator({ nodes, onSelect }: {
  nodes: PositionedNode[]; onSelect: (node: PositionedNode) => void;
}) {
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState(false);
  const [offset, setOffset] = useState(0);
  const matches = useMemo(() => {
    const text = query.trim().toLocaleLowerCase();
    return nodes.filter(node => `${node.label} ${node.node_type} ${node.subtitle ?? ""}`.toLocaleLowerCase().includes(text))
      .sort((a, b) => Number(b.is_root) - Number(a.is_root) || a.label.localeCompare(b.label));
  }, [nodes, query]);
  const pageOffset = Math.min(offset, Math.max(0, Math.ceil(matches.length / 20) - 1) * 20);
  const page = matches.slice(pageOffset, pageOffset + 20);
  return <section aria-label="Find a graph node" className="mt-3 border-t border-line pt-3">
    <div className="flex flex-wrap items-center gap-2">
      <input type="search" aria-label="Find in this graph" placeholder="Find a holding, company, claim or source…"
        value={query} onFocus={() => setExpanded(true)} onChange={event => {setQuery(event.target.value); setOffset(0); setExpanded(true);}}
        className="workspace-control min-w-0 flex-1" />
      <button className="workspace-control" aria-expanded={expanded} aria-controls="graph-node-results" onClick={() => setExpanded(value => !value)}>{expanded ? "Hide node list" : `Browse ${nodes.length} nodes`}</button>
    </div>
    {expanded && <div id="graph-node-results" className="mt-3">
      <p className="mb-2 text-xs text-muted">{matches.length} matching nodes in this view. Choose one to center its connections and open its details.</p>
      <ul className="max-h-80 divide-y divide-line overflow-auto">{page.map(node => <li key={node.key}>
        <button className="w-full p-3 text-left hover:bg-panel focus-visible:outline-2 focus-visible:outline-current" onClick={() => {onSelect(node); setExpanded(false);}}>
          <span className="block break-words text-sm font-medium">{node.label}</span>
          <span className="mt-1 block text-xs text-muted">{formatUserLabel(node.node_type)} · {node.degree ?? 0} connections{node.subtitle ? ` · ${node.subtitle}` : ""}</span>
        </button>
      </li>)}</ul>
      {matches.length > 20 && <div className="mt-2 flex items-center gap-3 text-sm">
        <button className="workspace-control" disabled={!pageOffset} onClick={() => setOffset(Math.max(0, pageOffset - 20))}>Previous nodes</button>
        <span>{pageOffset + 1}–{Math.min(pageOffset + 20, matches.length)} of {matches.length}</span>
        <button className="workspace-control" disabled={pageOffset + 20 >= matches.length} onClick={() => setOffset(pageOffset + 20)}>Next nodes</button>
      </div>}
      {!matches.length && <p className="py-3 text-sm text-muted">No nodes match. Clear the search, change filters, or use the inspector’s all-knowledge search.</p>}
    </div>}
  </section>;
}
