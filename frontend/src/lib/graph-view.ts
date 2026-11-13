import type { GraphNeighborhood, GraphWebEdge } from "./api";

/** Labels describe records; only keys identify them. Filters never merge evidence. */
export function filterGraphView(graph: GraphNeighborhood, options: {
  includeSystem: boolean;
  nodeTypes: string[];
  showUnconnected: boolean;
}) {
  const candidates = graph.nodes.filter(node => node.key === graph.root_key || (
    (options.includeSystem || node.layer !== "system") &&
    (!options.nodeTypes.length || options.nodeTypes.includes(node.node_type))
  ));
  const keys = new Set(candidates.map(node => node.key));
  const seen = new Set<string>();
  const edges: GraphWebEdge[] = [];
  const degree = new Map<string, number>();
  for (const edge of graph.edges) {
    if (!keys.has(edge.source_key) || !keys.has(edge.target_key)) continue;
    const identity = JSON.stringify([edge.id, edge.source_key, edge.target_key, edge.relationship_type]);
    if (seen.has(identity)) continue;
    seen.add(identity);
    edges.push(edge);
    degree.set(edge.source_key, (degree.get(edge.source_key) ?? 0) + 1);
    degree.set(edge.target_key, (degree.get(edge.target_key) ?? 0) + 1);
  }
  const browseNodes = candidates.map(node => ({ ...node, degree: degree.get(node.key) ?? 0 }));
  const unconnectedCount = browseNodes.filter(node => !node.degree && node.key !== graph.root_key).length;
  // With no relationships yet, keep the available records discoverable on canvas too.
  const nodes = browseNodes.filter(node => options.showUnconnected || !edges.length || node.degree || node.key === graph.root_key);
  return {
    graph: { ...graph, nodes, edges },
    browseNodes,
    unconnectedCount,
    filteredNodeCount: graph.nodes.length - candidates.length,
    filteredEdgeCount: graph.edges.length - edges.length,
  };
}
