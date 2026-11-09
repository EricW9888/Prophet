import * as d3 from "d3";
import type { GraphNeighborhood, GraphWebNode } from "./api";
import { CENTER_X, CENTER_Y, HEIGHT, WIDTH, nodeVisualRadius, type PositionedNode } from "./graph-layout-geometry";

type PositionedLink = d3.SimulationLinkDatum<PositionedNode>;
const GRAPH_LAYOUT_PADDING = 96;
const NODE_COLLISION_PADDING = 10;
const OVERLAP_RESOLUTION_ITERATIONS = 36;

function stableHash(value: string) {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return hash >>> 0;
}

function stableUnit(value: string, salt: string) {
  return stableHash(`${salt}:${value}`) / 0xffffffff;
}

function stableSignedUnit(value: string, salt: string) {
  return stableUnit(value, salt) * 2 - 1;
}

function nodeLayoutAnchor(node: GraphWebNode) {
  if (node.is_root) {
    return { x: CENTER_X, y: CENTER_Y, spreadX: 0, spreadY: 0 };
  }
  if (node.node_type === "portfolio") {
    return { x: CENTER_X, y: CENTER_Y, spreadX: 120, spreadY: 80 };
  }
  const angle = stableUnit(node.key, "layout-angle") * Math.PI * 2;
  const band =
    node.node_type === "position"
      ? 260
      : node.layer === "source"
        ? 660
        : node.layer === "operating"
          ? 560
          : 420;
  return {
    x: CENTER_X + Math.cos(angle) * band,
    y: CENTER_Y + Math.sin(angle) * band * 0.72,
    spreadX: 220,
    spreadY: 150,
  };
}

function nodeLayoutStrength(node: PositionedNode) {
  if (node.is_root) return 0.18;
  if (node.node_type === "portfolio") return 0.1;
  return 0.025;
}

function organicSeedPosition(node: GraphWebNode, index: number) {
  const goldenAngle = Math.PI * (3 - Math.sqrt(5));
  const sequenceOffset = stableUnit(node.key, "organic-sequence") * 0.45;
  const angle = (index + 1 + sequenceOffset) * goldenAngle + stableUnit(node.key, "organic-angle") * 0.38;
  const radialUnit = Math.sqrt(Math.min(0.98, Math.max(0.06, stableUnit(node.key, "organic-radius"))));
  const extentX = WIDTH * 0.38;
  const extentY = HEIGHT * 0.34;
  const anchor = nodeLayoutAnchor(node);
  const anchorWeight = node.node_type === "portfolio" ? 0.45 : 0.08;
  const organicX = CENTER_X + Math.cos(angle) * extentX * radialUnit + stableSignedUnit(node.key, "organic-x") * 90;
  const organicY = CENTER_Y + Math.sin(angle) * extentY * radialUnit + stableSignedUnit(node.key, "organic-y") * 70;
  return {
    x: organicX * (1 - anchorWeight) + anchor.x * anchorWeight,
    y: organicY * (1 - anchorWeight) + anchor.y * anchorWeight,
  };
}

function deterministicNodePosition(
  node: GraphWebNode,
  index: number,
) {
  if (node.is_root) {
    return { x: CENTER_X, y: CENTER_Y };
  }
  return organicSeedPosition(node, index);
}

function normalizeGraphLayout(
  graphNodes: PositionedNode[],
  rootKey: string | null,
) {
  if (!graphNodes.length) {
    return graphNodes;
  }
  const root = graphNodes.find((node) => node.key === rootKey) ?? graphNodes.find((node) => node.is_root);
  const anchorX =
    root?.x ??
    graphNodes.reduce((sum, node) => sum + (node.x ?? CENTER_X), 0) / graphNodes.length;
  const anchorY =
    root?.y ??
    graphNodes.reduce((sum, node) => sum + (node.y ?? CENTER_Y), 0) / graphNodes.length;
  const offsetX = CENTER_X - anchorX;
  const offsetY = CENTER_Y - anchorY;
  return graphNodes.map((node) => {
    if (node.key === rootKey || (!rootKey && node.is_root)) {
      return { ...node, x: CENTER_X, y: CENTER_Y, vx: 0, vy: 0 };
    }
    return {
      ...node,
      x: (node.x ?? CENTER_X) + offsetX,
      y: (node.y ?? CENTER_Y) + offsetY,
      vx: 0,
      vy: 0,
    };
  });
}

function keepNodeWithinLayoutBounds(node: PositionedNode) {
  const beforeX = node.x ?? CENTER_X;
  const beforeY = node.y ?? CENTER_Y;
  const radius = nodeVisualRadius(node, { selected: false, compare: false });
  const minX = GRAPH_LAYOUT_PADDING + radius;
  const maxX = WIDTH - GRAPH_LAYOUT_PADDING - radius;
  const minY = GRAPH_LAYOUT_PADDING + radius;
  const maxY = HEIGHT - GRAPH_LAYOUT_PADDING - radius;
  node.x = Math.min(maxX, Math.max(minX, beforeX));
  node.y = Math.min(maxY, Math.max(minY, beforeY));
  return Math.abs((node.x ?? CENTER_X) - beforeX) > 0.001 || Math.abs((node.y ?? CENTER_Y) - beforeY) > 0.001;
}

function separateOverlappingNodes(
  nodes: PositionedNode[],
  rootKeys: Set<string>,
  iterations: number,
) {
  const radii = nodes.map((node) => nodeVisualRadius(node, { selected: false, compare: false }));
  for (let iteration = 0; iteration < iterations; iteration += 1) {
    let moved = false;
    for (let aIndex = 0; aIndex < nodes.length; aIndex += 1) {
      for (let bIndex = aIndex + 1; bIndex < nodes.length; bIndex += 1) {
        const a = nodes[aIndex];
        const b = nodes[bIndex];
        const minDistance = radii[aIndex] + radii[bIndex] + NODE_COLLISION_PADDING;
        let dx = (b.x ?? CENTER_X) - (a.x ?? CENTER_X);
        let dy = (b.y ?? CENTER_Y) - (a.y ?? CENTER_Y);
        const distanceSquared = dx * dx + dy * dy;
        if (distanceSquared >= minDistance * minDistance) continue;
        let distance = Math.sqrt(distanceSquared);
        if (distance < 0.001) {
          const angle = stableUnit(`${a.key}:${b.key}`, "overlap-angle") * Math.PI * 2;
          dx = Math.cos(angle);
          dy = Math.sin(angle);
          distance = 1;
        }
        const overlap = minDistance - distance;
        const nx = dx / distance;
        const ny = dy / distance;
        const aPinned = rootKeys.has(a.key);
        const bPinned = rootKeys.has(b.key);
        if (aPinned && bPinned) {
          continue;
        }
        if (aPinned) {
          b.x = (b.x ?? CENTER_X) + nx * overlap;
          b.y = (b.y ?? CENTER_Y) + ny * overlap;
        } else if (bPinned) {
          a.x = (a.x ?? CENTER_X) - nx * overlap;
          a.y = (a.y ?? CENTER_Y) - ny * overlap;
        } else {
          const push = overlap / 2;
          a.x = (a.x ?? CENTER_X) - nx * push;
          a.y = (a.y ?? CENTER_Y) - ny * push;
          b.x = (b.x ?? CENTER_X) + nx * push;
          b.y = (b.y ?? CENTER_Y) + ny * push;
        }
        moved = true;
      }
    }
    for (const node of nodes) {
      if (!rootKeys.has(node.key)) {
        moved = keepNodeWithinLayoutBounds(node) || moved;
      }
    }
    if (!moved) {
      break;
    }
  }
}

function resolveNodeOverlaps(
  graphNodes: PositionedNode[],
  rootKey: string | null,
  targetAspect: number,
) {
  const resolved = graphNodes.map((node) => ({
    ...node,
    x: node.x ?? CENTER_X,
    y: node.y ?? CENTER_Y,
    vx: 0,
    vy: 0,
  }));
  const rootKeys = new Set(
    resolved
      .filter((node) => node.is_root || node.key === rootKey)
      .map((node) => node.key),
  );

  for (const node of resolved) {
    if (rootKeys.has(node.key)) {
      node.fx = CENTER_X;
      node.fy = CENTER_Y;
    }
  }
  const graphSize = Math.max(1, resolved.length);
  const chargeStrength =
    graphSize >= 350
      ? -24
      : graphSize >= 220
        ? -36
        : graphSize >= 120
          ? -56
          : -84;
  const relaxation = d3.forceSimulation<PositionedNode>(resolved)
    .force("collision", d3.forceCollide<PositionedNode>().radius((node) => {
      const radius = nodeVisualRadius(node, { selected: false, compare: false });
      return radius + NODE_COLLISION_PADDING;
    }).strength(0.95).iterations(10))
    .force("charge", d3.forceManyBody<PositionedNode>().strength(chargeStrength))
    .force("x", d3.forceX<PositionedNode>(CENTER_X).strength(0.002))
    .force("y", d3.forceY<PositionedNode>(CENTER_Y).strength(0.002))
    .alphaDecay(0.045)
    .stop();
  while (relaxation.alpha() >= relaxation.alphaMin()) {
    relaxation.tick();
  }
  for (const node of resolved) {
    node.fx = null;
    node.fy = null;
    if (!rootKeys.has(node.key)) {
      keepNodeWithinLayoutBounds(node);
    }
  }
  separateOverlappingNodes(resolved, rootKeys, OVERLAP_RESOLUTION_ITERATIONS);

  const normalized = normalizeGraphLayout(resolved, rootKey);
  const positioned = normalized.filter(
    (node): node is PositionedNode & { x: number; y: number } =>
      Number.isFinite(node.x) && Number.isFinite(node.y),
  );
  if (positioned.length > 1) {
    const minX = Math.min(...positioned.map((node) => node.x));
    const maxX = Math.max(...positioned.map((node) => node.x));
    const minY = Math.min(...positioned.map((node) => node.y));
    const maxY = Math.max(...positioned.map((node) => node.y));
    const spanX = Math.max(1, maxX - minX);
    const spanY = Math.max(1, maxY - minY);
    const currentAspect = spanX / spanY;
    if (currentAspect < targetAspect) {
      const horizontalScale = Math.min(2.5, targetAspect / currentAspect);
      for (const node of normalized) {
        if (node.key !== rootKey && !node.is_root) {
          node.x = CENTER_X + ((node.x ?? CENTER_X) - CENTER_X) * horizontalScale;
        }
      }
    } else if (currentAspect > targetAspect) {
      const verticalScale = Math.min(2.5, currentAspect / targetAspect);
      for (const node of normalized) {
        if (node.key !== rootKey && !node.is_root) {
          node.y = CENTER_Y + ((node.y ?? CENTER_Y) - CENTER_Y) * verticalScale;
        }
      }
    }
  }
  return normalized;
}

export type GraphLayoutRequest = {
  graph: GraphNeighborhood;
  positions: Map<string, { x: number; y: number; vx?: number; vy?: number }>;
  manual: Record<string, { x: number; y: number }>;
  targetAspect: number;
};

export function calculateGraphLayout({ graph, positions, manual, targetAspect }: GraphLayoutRequest): PositionedNode[] {
  const degreeByKey = new Map<string, number>();
  for (const edge of graph.edges) {
    degreeByKey.set(edge.source_key, (degreeByKey.get(edge.source_key) ?? 0) + 1);
    degreeByKey.set(edge.target_key, (degreeByKey.get(edge.target_key) ?? 0) + 1);
  }

  // Stable Layout: Use target positions from the persistent ref map
  const initialNodes: PositionedNode[] = graph.nodes.map((n, index) => {
    const manualPosition = manual[n.key];
    const existing = positions.get(n.key);
    const fallback = deterministicNodePosition(n, index);
    const degree = degreeByKey.get(n.key) ?? 0;
    return {
      ...n,
      degree,
      layout_node_count: graph.nodes.length,
      x: manualPosition?.x ?? existing?.x ?? fallback.x,
      y: manualPosition?.y ?? existing?.y ?? fallback.y,
      vx: existing?.vx ?? 0,
      vy: existing?.vy ?? 0,
      fx: manualPosition?.x,
      fy: manualPosition?.y,
    };
  });

  const initialLinks: PositionedLink[] = graph.edges.map(e => ({
    source: e.source_key,
    target: e.target_key,
  }));

  const simulation = d3.forceSimulation<PositionedNode>(initialNodes)
      .force("link", d3.forceLink<PositionedNode, PositionedLink>()
        .id(d => d.key)
        .strength(0.32)
      )
      .force("charge", d3.forceManyBody<PositionedNode>().strength(d => (d.degree ?? 0) === 0 ? -120 : -420))
      .force("x", d3.forceX<PositionedNode>(CENTER_X).strength(nodeLayoutStrength))
      .force("y", d3.forceY<PositionedNode>(CENTER_Y).strength(nodeLayoutStrength))
      .force("collision", d3.forceCollide<PositionedNode>().radius(d => {
        const r = nodeVisualRadius(d, { selected: false, compare: false });
        return r + NODE_COLLISION_PADDING;
      }).iterations(6))
      .alphaDecay(0.07);


  const linkForce = simulation.force("link") as d3.ForceLink<PositionedNode, PositionedLink>;
  linkForce.links(initialLinks)
    .distance(l => {
      const s = l.source as PositionedNode;
      const t = l.target as PositionedNode;
      if (s.is_root || t.is_root) return 260;
      if (s.node_type === "position" || t.node_type === "position") return 200;
      if (s.node_type === "entity" || t.node_type === "entity") return 180;
      return 160;
    });

  simulation.stop();
  simulation.alpha(0.9);
  while (simulation.alpha() >= simulation.alphaMin()) {
    simulation.tick();
  }
  return resolveNodeOverlaps(normalizeGraphLayout(initialNodes, graph.root_key), graph.root_key, targetAspect);
}
