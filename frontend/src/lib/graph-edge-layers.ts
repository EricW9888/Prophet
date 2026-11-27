import type { GraphWebEdge } from "@/lib/api";
import type { PositionedNode } from "@/lib/graph-layout-geometry";

/** Above this many connections, background connections are drawn as one path. */
export const EDGE_BATCH_THRESHOLD = 400;

export type InteractiveEdge = {
  edge: GraphWebEdge;
  edgeKey: string;
  source: PositionedNode;
  target: PositionedNode;
  highlighted: boolean;
};

/**
 * Split connections into what must stay individually tappable and what can be
 * drawn as one path. Each SVG element costs layout and repaint while the map
 * moves; the full research map has thousands of connections. The selected
 * connection, the hovered one and those touching the selected node stay
 * interactive. Small maps keep every connection interactive.
 */
export function graphEdgeLayers(
  edges: GraphWebEdge[],
  nodeByKey: Record<string, PositionedNode>,
  identity: (edge: GraphWebEdge) => string,
  state: { selectedKey: string | null; selectedEdgeId: string | null; hoveredEdgeId: string | null },
  threshold = EDGE_BATCH_THRESHOLD,
): { solid: string; dashed: string; interactive: InteractiveEdge[] } {
  const batch = edges.length > threshold;
  const solid: string[] = [];
  const dashed: string[] = [];
  const interactive: InteractiveEdge[] = [];
  for (const edge of edges) {
    const source = nodeByKey[edge.source_key];
    const target = nodeByKey[edge.target_key];
    if (!source || !target) continue;
    const edgeKey = identity(edge);
    const highlighted = state.hoveredEdgeId === edgeKey
      || Boolean(state.selectedKey && (edge.source_key === state.selectedKey || edge.target_key === state.selectedKey));
    if (batch && !highlighted && state.selectedEdgeId !== edgeKey) {
      (edge.supporting_claim_id ? dashed : solid).push(`M${source.x ?? 0} ${source.y ?? 0}L${target.x ?? 0} ${target.y ?? 0}`);
      continue;
    }
    interactive.push({ edge, edgeKey, source, target, highlighted });
  }
  return { solid: solid.join(""), dashed: dashed.join(""), interactive };
}
