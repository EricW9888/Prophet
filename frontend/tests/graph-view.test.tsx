import assert from "node:assert/strict";
import { test } from "node:test";
import { filterGraphView } from "../src/lib/graph-view";
import type { GraphNeighborhood } from "../src/lib/api";

test("same-label metrics retain separate identities and all company and period links", () => {
  const graph: GraphNeighborhood = {
    root_key: "company-a", depth: 2,
    nodes: [
      ["company-a", "Issuer A", "entity"], ["company-b", "Issuer B", "entity"],
      ["metric-a", "Revenue", "fundamental_metric"], ["metric-b", "Revenue", "fundamental_metric"],
      ["question", "What changed?", "unresolved_question"], ["unconnected", "Unlinked lead", "claim"],
    ].map(([key, label, node_type]) => ({ key, id: key, label, node_type, layer: "knowledge", is_root: key === "company-a", is_autonomous: false })),
    edges: [["company-a", "metric-a"], ["company-b", "metric-b"], ["company-a", "question"]].map(([source_key, target_key], i) => ({id: String(i), source_key, target_key, relationship_type: "supports", confidence: 1})),
  };
  const view = filterGraphView(graph, { includeSystem: false, nodeTypes: [], showUnconnected: false });
  assert.deepEqual(view.graph.nodes.map(node => node.key), ["company-a", "company-b", "metric-a", "metric-b", "question"]);
  assert.equal(view.graph.edges.length, 3);
  assert.equal(view.browseNodes.length, 6);
  assert.equal(view.unconnectedCount, 1);
  assert.equal(view.filteredNodeCount, 0);
  assert.equal(filterGraphView(graph, { includeSystem: false, nodeTypes: [], showUnconnected: true }).graph.nodes.length, 6);
  assert.equal(graph.nodes.length, 6);
});

test("explicit type filters preserve root and remove dangling edges without hiding an empty graph", () => {
  const node = (key: string, type: string) => ({key, id:key, node_type:type, label:key, layer:"knowledge", is_root:key === "root", is_autonomous:false});
  const graph: GraphNeighborhood = {root_key:"root", depth:1, nodes:[node("root", "entity"), node("metric", "fundamental_metric"), node("claim", "claim")], edges:[{id:"edge",source_key:"root",target_key:"claim",relationship_type:"supports",confidence:1}]};
  const view = filterGraphView(graph, {includeSystem:false,nodeTypes:["fundamental_metric"],showUnconnected:false});
  assert.deepEqual(view.graph.nodes.map(node => node.key), ["root", "metric"]);
  assert.equal(view.graph.edges.length, 0);
  assert.equal(view.filteredNodeCount, 1);
  assert.equal(view.filteredEdgeCount, 1);
});
