import "./helpers/dom.mjs";
import assert from "node:assert/strict";
import { afterEach, beforeEach, test } from "node:test";
import React, { act, Profiler, useCallback, useEffect, useRef } from "react";
import { createRoot, type Root } from "react-dom/client";
import * as d3 from "d3";
import { GraphNodeGlyph } from "../src/components/GraphGlyphs";
import { useGraphCamera } from "../src/lib/use-graph-camera";
import { calculateGraphLayout } from "../src/lib/graph-layout";
import { CENTER_X, CENTER_Y, nodeVisualRadius, type PositionedNode } from "../src/lib/graph-layout-geometry";
import type { GraphNeighborhood } from "../src/lib/api";
import { graphLabelCandidates, nonOverlappingLabels, refreshGraphLabels } from "../src/lib/graph-labels";

let root: Root;
let container: HTMLDivElement;
beforeEach(() => {
  Object.assign(globalThis, { Element: window.Element, SVGElement: window.SVGElement });
  Object.assign(globalThis, {
    requestAnimationFrame: (callback: FrameRequestCallback) => window.setTimeout(() => callback(performance.now()), 0),
    cancelAnimationFrame: (id: number) => window.clearTimeout(id),
  });
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});
afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
});

test("camera movement updates the SVG without rendering React on each event", async () => {
  let renders = 0;
  let camera: ReturnType<typeof useGraphCamera>;
  const capture = (value: ReturnType<typeof useGraphCamera>) => { camera = value; };
  function Harness({ onCamera }: { onCamera: typeof capture }) {
    const svg = useRef<SVGSVGElement>(null);
    const scene = useRef<SVGGElement>(null);
    const clear = useCallback(() => {}, []);
    const value = useGraphCamera(svg, scene, true, clear);
    useEffect(() => onCamera(value), [onCamera, value]);
    return <svg ref={svg} viewBox="0 0 1200 800"><g ref={scene} /><text>{value.detailZoom}</text></svg>;
  }
  await act(async () => root.render(<Profiler id="camera" onRender={() => { renders += 1; }}><Harness onCamera={capture} /></Profiler>));
  const svg = container.querySelector("svg")!;
  Object.defineProperty(svg, "viewBox", { value: { baseVal: { x: 0, y: 0, width: 1200, height: 800 } } });
  const move = (x: number, scale = 1) => d3.select(svg).call(camera.zoomRef.current!.transform, d3.zoomIdentity.translate(x, 5).scale(scale));
  const initialRenders = renders;
  await act(async () => { for (let index = 0; index < 200; index += 1) move(index); });
  assert.equal(renders, initialRenders);
  assert.equal(container.querySelector("g")!.getAttribute("transform"), "translate(199,5) scale(1)");
  assert.deepEqual(camera!.transformRef.current, { x: 199, y: 5, k: 1 });
  await act(async () => move(200, 1.5));
  assert.equal(container.querySelector("text")!.textContent, "1.35");
  const detailedRenders = renders;
  await act(async () => { for (let index = 0; index < 100; index += 1) move(index, 1.6); });
  assert.equal(renders, detailedRenders);
  await act(async () => move(0, 2));
  assert.equal(container.querySelector("text")!.textContent, "1.9");
});

test("unrelated workspace updates leave node glyphs alone while selection still changes them", async () => {
  let labelReads = 0;
  const node: PositionedNode = {
    key: "entity:synthetic", id: "synthetic", node_type: "entity", layer: "knowledge", is_root: true, is_autonomous: false,
    get label() { labelReads += 1; return "Synthetic issuer"; }, x: 10, y: 20,
  };
  const hover = () => {};
  const pointer = () => {};
  const view = (revision: number, selected = false) => <svg data-revision={revision}><GraphNodeGlyph node={node}
    isSelected={selected} isHovered={false} isConnected={false} hasSelection={selected} zoom={1} onHover={hover} onPointerDown={pointer} /></svg>;
  await act(async () => root.render(view(1)));
  labelReads = 0;
  await act(async () => root.render(view(2)));
  assert.equal(labelReads, 0);
  await act(async () => root.render(view(3, true)));
  assert.ok(labelReads > 0);
  assert.equal(container.querySelectorAll("g.graph-node > circle:not(.graph-node-hit)").length, 2);
  assert.equal(container.querySelector("g.graph-node")!.getAttribute("transform"), "translate(10, 20)");
});

test("large graph layout preserves identity, deterministic positions and readable spacing", () => {
  const graph: GraphNeighborhood = {
    root_key: "entity:0", depth: 2,
    nodes: Array.from({ length: 700 }, (_, index) => ({ key: `entity:${index}`, id: String(index), node_type: "entity", label: `Synthetic issuer ${index}`, layer: "knowledge", is_root: index === 0, is_autonomous: false })),
    edges: Array.from({ length: 699 }, (_, index) => ({ id: `edge-${index}`, source_key: "entity:0", target_key: `entity:${index + 1}`, relationship_type: "synthetic", confidence: 1 })),
  };
  const input = { graph, positions: new Map(), manual: {}, targetAspect: 1.5 };
  const nodes = calculateGraphLayout(input);
  assert.deepEqual(nodes, calculateGraphLayout(input));
  assert.deepEqual(nodes.map((node) => node.key), graph.nodes.map((node) => node.key));
  assert.equal(nodes[0].x, CENTER_X);
  assert.equal(nodes[0].y, CENTER_Y);
  for (let index = 0; index < nodes.length; index += 1) {
    const a = nodes[index];
    assert.ok(Number.isFinite(a.x) && Number.isFinite(a.y));
    for (const b of nodes.slice(index + 1)) {
      const radii = nodeVisualRadius(a, { selected: false, compare: false }) + nodeVisualRadius(b, { selected: false, compare: false });
      assert.ok(Math.hypot(a.x! - b.x!, a.y! - b.y!) >= radii - 1, `Overlapping nodes ${a.key} and ${b.key}`);
    }
  }
});

test("overview labels retain selection and stay bounded without dropping graph nodes", () => {
  const nodes = Array.from({length: 700}, (_, index) => ({key: `entity:${index}`, id: String(index), node_type: "entity", label: `Synthetic ${index}`, layer: "knowledge", is_root: index === 0, is_autonomous: false}));
  const keys = graphLabelCandidates(nodes, "entity:699", "entity:698", new Set());
  assert.ok(keys.size <= 96 && keys.has("entity:0") && keys.has("entity:699") && keys.has("entity:698"));
  assert.equal(nodes.length, 700);
  const visible = nonOverlappingLabels([
    {key:"selected",priority:100,pinned:true,left:10,top:10,right:150,bottom:25},
    {key:"overlap",priority:10,pinned:false,left:20,top:15,right:120,bottom:30},
    {key:"clear",priority:1,pinned:false,left:200,top:10,right:300,bottom:25},
  ], {left:0,top:0,right:500,bottom:300});
  assert.deepEqual([...visible], ["selected", "clear"]);
});

test("label sizing accounts for both the viewBox scale and camera zoom with one bounds read per label", async () => {
  const node: PositionedNode = {key:"entity:screen",id:"screen",node_type:"entity",label:"Synthetic readable issuer",layer:"knowledge",is_root:true,is_autonomous:false};
  await act(async () => root.render(<svg><GraphNodeGlyph node={node} isSelected isHovered={false} isConnected={false} hasSelection zoom={0.2} onHover={()=>{}} onPointerDown={()=>{}} /></svg>));
  const svg = container.querySelector("svg")!;
  const label = container.querySelector<SVGTextElement>(".graph-node-label")!;
  let reads = 0;
  Object.defineProperty(svg, "getScreenCTM", {value:()=>({a:0.2,b:0})});
  svg.getBoundingClientRect=()=>({left:0,top:0,right:500,bottom:300} as DOMRect);
  label.getBoundingClientRect=()=>{reads+=1;return {left:10,top:10,right:200,bottom:25} as DOMRect;};
  refreshGraphLabels(svg, 0.5);
  assert.equal(svg.style.getPropertyValue("--graph-effective-scale"), "0.1");
  assert.equal(reads, 1);
  assert.equal(label.style.visibility, "visible");
  assert.equal(container.querySelector("clipPath"), null);
  assert.equal(label.textContent, node.label);
  assert.equal(container.querySelector('[role="button"]')!.getAttribute("aria-label"), node.label);
});
