import * as d3 from "d3";
import type { GraphNeighborhood } from "./api";
import { CENTER_X, CENTER_Y, nodeVisualRadius, type PositionedNode } from "./graph-layout-geometry";

type PositionedLink = d3.SimulationLinkDatum<PositionedNode>;
const NODE_COLLISION_PADDING = 10;
const COMPONENT_PADDING = 40;
const OVERLAP_RESOLUTION_ITERATIONS = 36;

type Component = d3.SimulationNodeDatum & {
  nodes: PositionedNode[];
  links: PositionedLink[];
  r: number;
  centerX: number;
  centerY: number;
  anchored: boolean;
};

export type GraphLayoutRequest = {
  graph: GraphNeighborhood;
  positions: Map<string, { x: number; y: number; vx?: number; vy?: number }>;
  manual: Record<string, { x: number; y: number }>;
  targetAspect: number;
};

// Components describe actual connectivity, not inferred subjects or merged identities.
function connectedComponents(nodes: PositionedNode[], graph: GraphNeighborhood): Component[] {
  const byKey = new Map(nodes.map(node => [node.key, node]));
  const adjacent = new Map(nodes.map(node => [node.key, new Set<string>()]));
  for (const edge of graph.edges) {
    adjacent.get(edge.source_key)?.add(edge.target_key);
    adjacent.get(edge.target_key)?.add(edge.source_key);
  }
  const membership = new Map<string, Component>();
  const components: Component[] = [];
  for (const node of nodes) {
    if (membership.has(node.key)) continue;
    const component: Component = { nodes: [], links: [], r: 0, centerX: 0, centerY: 0, anchored: false };
    const pending = [node.key];
    membership.set(node.key, component);
    while (pending.length) {
      const key = pending.pop()!;
      component.nodes.push(byKey.get(key)!);
      for (const neighbor of adjacent.get(key) ?? []) {
        if (byKey.has(neighbor) && !membership.has(neighbor)) {
          membership.set(neighbor, component);
          pending.push(neighbor);
        }
      }
    }
    for (const member of component.nodes) member.layout_component = node.key;
    components.push(component);
  }
  for (const edge of graph.edges) {
    if (byKey.has(edge.source_key) && byKey.has(edge.target_key)) {
      membership.get(edge.source_key)!.links.push({ source: edge.source_key, target: edge.target_key });
    }
  }
  return components;
}

function layoutComponent(component: Component) {
  // D3 initializes missing positions with its deterministic phyllotaxis seeds.
  const simulation = d3.forceSimulation(component.nodes)
    .force("link", d3.forceLink<PositionedNode, PositionedLink>(component.links)
      .id(node => node.key)
      .strength(0.32)
      .distance(link => {
        const source = link.source as PositionedNode, target = link.target as PositionedNode;
        if (source.is_root || target.is_root) return 260;
        if (source.node_type === "position" || target.node_type === "position") return 200;
        if (source.node_type === "entity" || target.node_type === "entity") return 180;
        return 160;
      }))
    .force("charge", d3.forceManyBody<PositionedNode>().strength(-420))
    .force("x", d3.forceX<PositionedNode>(0).strength(0.025))
    .force("y", d3.forceY<PositionedNode>(0).strength(0.025))
    .force("collision", d3.forceCollide<PositionedNode>()
      .radius(node => nodeVisualRadius(node, { selected: false, compare: false }) + NODE_COLLISION_PADDING))
    .alphaDecay(0.07)
    .stop();
  simulation.alpha(0.9);
  while (simulation.alpha() >= simulation.alphaMin()) simulation.tick();

  // Resolve collisions without projecting nodes onto an artificial world boundary.
  simulation.force("link", null).force("charge", null).force("x", null).force("y", null);
  simulation.tick(OVERLAP_RESOLUTION_ITERATIONS);
  const circle = d3.packEnclose(component.nodes.map(node => ({
    x: node.x!, y: node.y!, r: nodeVisualRadius(node, { selected: false, compare: false }),
  })));
  component.centerX = circle.x;
  component.centerY = circle.y;
  component.r = circle.r + COMPONENT_PADDING;
  component.anchored = component.nodes.some(node => node.fx != null && node.fy != null);
}

function packComponents(components: Component[]) {
  // The front-chain packing provided by D3 keeps disconnected groups apart.
  // Largest first is stable for a given input and avoids scattering tiny groups
  // through the interior of a large connected component.
  const packed = [...components].sort((a, b) => b.r - a.r);
  d3.packSiblings(packed);
  const anchor = packed.find(component => component.nodes.some(node => node.is_root))
    ?? packed.find(component => component.anchored);
  const enclosing = d3.packEnclose(packed as (Component & { x: number; y: number })[]);
  const offsetX = anchor ? anchor.centerX - anchor.x! : -enclosing.x;
  const offsetY = anchor ? anchor.centerY - anchor.y! : -enclosing.y;
  for (const component of packed) {
    component.x = component.anchored ? component.centerX : component.x! + offsetX;
    component.y = component.anchored ? component.centerY : component.y! + offsetY;
    if (component.anchored) {
      component.fx = component.centerX;
      component.fy = component.centerY;
    }
  }
  // A manually positioned component may lie outside its automatic packed slot.
  // Move free groups around fixed groups; never move an owner's fixed nodes.
  if (packed.some(component => component.anchored)) {
    d3.forceSimulation(packed)
      .force("collision", d3.forceCollide<Component>().radius(component => component.r).iterations(4))
      .stop().tick(OVERLAP_RESOLUTION_ITERATIONS);
  }
  for (const component of packed) {
    const dx = component.x! - component.centerX, dy = component.y! - component.centerY;
    for (const node of component.nodes) {
      node.x = node.x! + dx + CENTER_X;
      node.y = node.y! + dy + CENTER_Y;
      node.vx = 0; node.vy = 0;
      node.fx = null; node.fy = null;
    }
  }
}

function expandToAspect(nodes: PositionedNode[], targetAspect: number) {
  if (nodes.length < 2 || !Number.isFinite(targetAspect) || targetAspect <= 0) return;
  const minX = d3.min(nodes, node => node.x!)!, maxX = d3.max(nodes, node => node.x!)!;
  const minY = d3.min(nodes, node => node.y!)!, maxY = d3.max(nodes, node => node.y!)!;
  const aspect = Math.max(1, maxX - minX) / Math.max(1, maxY - minY);
  // Expand only: compression would reintroduce node and component collisions.
  const scaleX = Math.max(1, targetAspect / aspect), scaleY = Math.max(1, aspect / targetAspect);
  for (const node of nodes) {
    node.x = CENTER_X + (node.x! - CENTER_X) * scaleX;
    node.y = CENTER_Y + (node.y! - CENTER_Y) * scaleY;
  }
}

export function calculateGraphLayout({ graph, positions, manual, targetAspect }: GraphLayoutRequest): PositionedNode[] {
  if (!graph.nodes.length) return [];
  const degreeByKey = new Map<string, number>();
  for (const edge of graph.edges) {
    degreeByKey.set(edge.source_key, (degreeByKey.get(edge.source_key) ?? 0) + 1);
    degreeByKey.set(edge.target_key, (degreeByKey.get(edge.target_key) ?? 0) + 1);
  }
  let hasManual = false;
  const nodes: PositionedNode[] = graph.nodes.map(node => {
    const pin = manual[node.key];
    const fixed = pin && Number.isFinite(pin.x) && Number.isFinite(pin.y) ? pin : null;
    hasManual ||= !!fixed;
    const existing = positions.get(node.key);
    const root = node.key === graph.root_key || node.is_root;
    return {
      ...node, degree: degreeByKey.get(node.key) ?? 0, layout_node_count: graph.nodes.length,
      x: existing && Number.isFinite(existing.x) ? existing.x - CENTER_X : undefined,
      y: existing && Number.isFinite(existing.y) ? existing.y - CENTER_Y : undefined,
      vx: 0, vy: 0,
      fx: fixed ? fixed.x - CENTER_X : root ? 0 : undefined,
      fy: fixed ? fixed.y - CENTER_Y : root ? 0 : undefined,
    };
  });
  const components = connectedComponents(nodes, graph);
  for (const component of components) layoutComponent(component);
  packComponents(components);
  // Explicit manual coordinates take precedence over automatic aspect fitting.
  if (!hasManual) expandToAspect(nodes, targetAspect);
  for (const node of nodes) {
    const pin = manual[node.key];
    if (pin && Number.isFinite(pin.x) && Number.isFinite(pin.y)) {
      node.x = pin.x; node.y = pin.y;
    }
  }
  return nodes;
}
