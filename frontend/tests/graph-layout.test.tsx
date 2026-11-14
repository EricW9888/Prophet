import assert from "node:assert/strict";
import { test } from "node:test";
import { polygonHull, polygonContains } from "d3";
import { calculateGraphLayout } from "../src/lib/graph-layout";
import { CENTER_X, CENTER_Y, nodeVisualRadius, type PositionedNode } from "../src/lib/graph-layout-geometry";
import { mixedGraph } from "./helpers/graph-fixtures";

function assertNoOverlaps(nodes: PositionedNode[]) {
  for (let i = 0; i < nodes.length; i += 1) {
    const a = nodes[i];
    assert.ok(Number.isFinite(a.x) && Number.isFinite(a.y));
    for (let j = i + 1; j < nodes.length; j += 1) {
      const b = nodes[j];
      const radius = nodeVisualRadius(a, { selected: false, compare: false }) + nodeVisualRadius(b, { selected: false, compare: false });
      assert.ok(Math.hypot(a.x! - b.x!, a.y! - b.y!) >= radius - 1, `Overlapping ${a.key} and ${b.key}`);
    }
  }
}

test("mixed connected groups do not accumulate on a fixed rectangle boundary", () => {
  const graph = mixedGraph();
  const before = structuredClone(graph);
  const result = calculateGraphLayout({ graph, positions: new Map(), manual: {}, targetAspect: 1.5 });
  const xCounts = new Map<string, number>(), yCounts = new Map<string, number>();
  for (const node of result) {
    const x = node.x!.toFixed(6), y = node.y!.toFixed(6);
    xCounts.set(x, (xCounts.get(x) || 0) + 1); yCounts.set(y, (yCounts.get(y) || 0) + 1);
  }
  assert.ok(Math.max(...xCounts.values(), ...yCounts.values()) < result.length * .05, "Many nodes were projected onto the same artificial boundary");
  assert.deepEqual(result.map(node => node.key), graph.nodes.map(node => node.key));
  assert.deepEqual(graph, before, "D3 must only mutate layout copies");
  assert.equal(new Set(result.map(node => node.layout_component)).size, 61);
  const byKey = new Map(result.map(node => [node.key, node]));
  for (const edge of graph.edges) assert.equal(byKey.get(edge.source_key)!.layout_component, byKey.get(edge.target_key)!.layout_component);
  assertNoOverlaps(result);
});

test("manual node anchors remain exact through repeated simulation and packing", () => {
  const graph = mixedGraph(3, 12, 30);
  const manual = { root: { x: 1500.37, y: -2000.13 }, "group:0:3": { x: CENTER_X + 500, y: CENTER_Y - 350 } };
  const request = { graph, positions: new Map<string, { x: number; y: number }>(), manual, targetAspect: 2.5 };
  for (let pass = 0; pass < 2; pass += 1) {
    const nodes = calculateGraphLayout(request);
    for (const [key, position] of Object.entries(manual)) {
      const node = nodes.find(candidate => candidate.key === key)!;
      assert.deepEqual({ x: node.x, y: node.y }, position);
    }
    assertNoOverlaps(nodes);
    request.positions = new Map(nodes.map(node => [node.key, { x: node.x!, y: node.y! }]));
  }
});

test("disconnected groups and isolates remain separate in wide and tall maps", () => {
  const graph = mixedGraph(8, 15, 60, 100);
  for (const targetAspect of [.65, 2.5]) {
    const result = calculateGraphLayout({ graph, positions: new Map(), manual: {}, targetAspect });
    assertNoOverlaps(result);
    assert.equal(new Set(result.map(node => node.layout_component)).size, 109);
    assert.equal(result[0].x, CENTER_X); assert.equal(result[0].y, CENTER_Y);
    assert.deepEqual(result, calculateGraphLayout({ graph, positions: new Map(), manual: {}, targetAspect }));
    const groups = new Map<string, [number, number][]>();
    for (const node of result) {
      const key = node.layout_component!;
      const points = groups.get(key) ?? [];
      points.push([node.x!, node.y!]); groups.set(key, points);
    }
    for (const [key, points] of groups) {
      const hull = polygonHull(points);
      if (!hull) continue;
      for (const node of result) if (node.layout_component !== key) {
        assert.equal(polygonContains(hull, [node.x!, node.y!]), false, `Unrelated ${node.key} inside group ${key}`);
      }
    }
  }
  assert.deepEqual(calculateGraphLayout({ graph: { ...graph, nodes: [], edges: [] }, positions: new Map(), manual: {}, targetAspect: 1 }), []);
});
