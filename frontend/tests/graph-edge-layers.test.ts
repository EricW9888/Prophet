import assert from "node:assert/strict";
import test from "node:test";

import { graphEdgeLayers } from "../src/lib/graph-edge-layers";

const node = (key: string, x: number, y: number) => ({ key, x, y }) as never;
const nodes = { a: node("a", 0, 0), b: node("b", 10, 0), c: node("c", 0, 10), d: node("d", 5, 5) };
const edge = (id: string, source: string, target: string, claim = false) =>
  ({ id, source_key: source, target_key: target, supporting_claim_id: claim ? "claim" : null }) as never;
const edges = [edge("1", "a", "b"), edge("2", "a", "c", true), edge("3", "b", "d"), edge("4", "c", "d"), edge("5", "x", "d")];
const identity = (e: { id: string }) => e.id;
const none = { selectedKey: null, selectedEdgeId: null, hoveredEdgeId: null };

test("small maps keep every drawable connection interactive", () => {
  const layers = graphEdgeLayers(edges, nodes, identity, none, 10);
  assert.equal(layers.solid, "");
  assert.deepEqual(layers.interactive.map((item) => item.edgeKey), ["1", "2", "3", "4"]);
});

test("large maps batch background connections and keep the selection's interactive", () => {
  const layers = graphEdgeLayers(edges, nodes, identity, { selectedKey: "a", selectedEdgeId: "4", hoveredEdgeId: null }, 2);
  assert.deepEqual(layers.interactive.map((item) => [item.edgeKey, item.highlighted]), [["1", true], ["2", true], ["4", false]]);
  assert.equal(layers.solid, "M10 0L5 5");
  assert.equal(layers.dashed, "");
});

test("a hovered connection is drawn on its own; source-attributed ones batch as dashed", () => {
  const layers = graphEdgeLayers(edges, nodes, identity, { ...none, hoveredEdgeId: "3" }, 2);
  assert.deepEqual(layers.interactive.map((item) => item.edgeKey), ["3"]);
  assert.equal(layers.dashed, "M0 0L0 10");
  assert.equal(layers.solid, "M0 0L10 0M0 10L5 5");
});
