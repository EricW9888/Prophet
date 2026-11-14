"use client";

import {useMemo} from "react";
import type {GraphWebEdge, GraphWebNode} from "@/lib/api";

type Props = {
  edges: GraphWebEdge[];
  selected: GraphWebEdge;
  nodes: Record<string, GraphWebNode>;
  edgeKey: (edge: GraphWebEdge) => string;
  onSelect: (key: string) => void;
};

/** Coincident lines can represent distinct assertions and sources. */
export function GraphConnectionSelect({edges, selected, nodes, edgeKey, onSelect}: Props) {
  const connections = useMemo(() => edges.filter(edge =>
    (edge.source_key === selected.source_key && edge.target_key === selected.target_key)
    || (edge.source_key === selected.target_key && edge.target_key === selected.source_key)),
  [edges, selected.source_key, selected.target_key]);
  if (connections.length < 2) return null;
  return <label className="block min-w-0 space-y-2 text-xs text-muted">
    <span>{connections.length} connections between these subjects</span>
    <select aria-label="Choose connection" className="workspace-input w-full min-w-0 text-sm"
      value={edgeKey(selected)} onChange={event => onSelect(event.target.value)}>
      {connections.map(edge => {
        const quote = edge.supporting_claim_id ? nodes[`claim:${edge.supporting_claim_id}`]?.label : null;
        const label = `${nodes[edge.source_key]?.label ?? edge.source_key} → ${nodes[edge.target_key]?.label ?? edge.target_key}: ${edge.relationship_type.replaceAll("_", " ")}${quote ? ` — ${quote}` : ""}`;
        return <option key={edgeKey(edge)} value={edgeKey(edge)} title={label}>{label.length > 180 ? `${label.slice(0, 179)}…` : label}</option>;
      })}
    </select>
  </label>;
}
