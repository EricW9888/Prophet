"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as d3 from "d3";
import Link from "next/link";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { Maximize2, RotateCcw, Search, X, ZoomIn, ZoomOut } from "lucide-react";
import AppNav from "@/components/AppNav";
import SourceProvenanceLinks from "@/components/SourceProvenanceLinks";
import {
  apiFetch,
  AgentTurn,
  AgentTurnJob,
  GraphNeighborhood,
  GraphNodeDetail,
  GraphSearchResult,
  GraphStats,
  GraphWebEdge,
  GraphWebNode,
  ProfileListItem,
} from "@/lib/api";
import { formatUserLabel } from "@/lib/formatting";

type PositionedNode = GraphWebNode & {
  x?: number;
  y?: number;
  vx?: number;
  vy?: number;
  fx?: number | null;
  fy?: number | null;
  index?: number;
  degree?: number;
  layout_node_count?: number;
};
type PositionedLink = d3.SimulationLinkDatum<PositionedNode>;
type GestureEventWithScale = Event & { scale?: number };
type GraphViewBox = {
  x: number;
  y: number;
  width: number;
  height: number;
};
type PortfolioGraphOverview = {
  id?: string;
  portfolio_id?: string;
  holdings?: Array<{ id: string }>;
};
type GraphMode = "portfolio" | "all" | "focused";

const WIDTH = 5200;
const HEIGHT = 3400;
const CENTER_X = WIDTH / 2;
const CENTER_Y = HEIGHT / 2;
const DEFAULT_GRAPH_VIEW_BOX: GraphViewBox = { x: 0, y: 0, width: WIDTH, height: HEIGHT };
const GRAPH_LAYOUT_PADDING = 96;
const MAX_GRAPH_LAYOUT_TARGET_ASPECT = (WIDTH / HEIGHT) * 0.92;
const MIN_GRAPH_LAYOUT_TARGET_ASPECT = 0.65;
const NODE_COLLISION_PADDING = 10;
const OVERLAP_RESOLUTION_ITERATIONS = 36;
const NODE_DRAG_THRESHOLD_PX = 6;
const OVERVIEW_SEED_LIMIT = 40;
const ALL_KNOWLEDGE_SEED_LIMIT = 100;
const OVERVIEW_NEIGHBORHOOD_DEPTH = 2;
const OVERVIEW_NEIGHBORHOOD_LIMIT = 20;
const FOCUSED_NEIGHBORHOOD_DEPTH = 2;
const FOCUSED_NEIGHBORHOOD_LIMIT = 56;
const LABEL_DETAIL_ZOOM = 1.35;
const LABEL_DENSE_ZOOM = 1.9;
const HIDDEN_DETAIL_PROPERTY_KEYS = new Set([
  "portfolio_significance",
  "why_in_graph",
  "linked_holdings",
  "linked_companies",
  "portfolio_mechanism",
  "affected_holdings",
  "next_test",
]);

function graphViewBoxForViewport(width: number, height: number): GraphViewBox {
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
    return DEFAULT_GRAPH_VIEW_BOX;
  }
  const viewportAspect = width / height;
  const layoutAspect = WIDTH / HEIGHT;
  if (viewportAspect < layoutAspect) {
    const logicalWidth = HEIGHT * viewportAspect;
    return {
      x: (WIDTH - logicalWidth) / 2,
      y: 0,
      width: logicalWidth,
      height: HEIGHT,
    };
  }
  const logicalHeight = WIDTH / viewportAspect;
  return {
    x: 0,
    y: (HEIGHT - logicalHeight) / 2,
    width: WIDTH,
    height: logicalHeight,
  };
}

function graphLayoutTargetAspect(viewBox: GraphViewBox): number {
  return Math.max(
    MIN_GRAPH_LAYOUT_TARGET_ASPECT,
    Math.min(MAX_GRAPH_LAYOUT_TARGET_ASPECT, (viewBox.width / viewBox.height) * 0.9),
  );
}

function formatCount(value: number): string {
  return new Intl.NumberFormat("en-US").format(value);
}

function graphEdgeIdentity(edge: GraphWebEdge): string {
  return [
    edge.id,
    edge.source_key,
    edge.target_key,
    edge.relationship_type,
  ].join(":");
}

function propertyText(value: unknown): string | null {
  if (typeof value === "string") {
    const trimmed = value.trim();
    return trimmed ? trimmed : null;
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return null;
}

function propertyList(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.map((item) => String(item).trim()).filter(Boolean);
}

const NODE_STYLES: Record<
  string,
  {
    fill: string;
    stroke: string;
    text: string;
    glow: string;
  }
> = {
  fact: {
    fill: "#2563eb",
    stroke: "#bfdbfe",
    text: "#ffffff",
    glow: "rgba(37,99,235,0.3)",
  },
  claim: {
    fill: "#f97316",
    stroke: "#fdba74",
    text: "#ffffff", // White text for better visibility on dark orange
    glow: "rgba(249,115,22,0.35)",
  },
  event: {
    fill: "#0ea5e9",
    stroke: "#bae6fd",
    text: "#ffffff",
    glow: "rgba(14,165,233,0.26)",
  },
  entity: {
    fill: "#10b981",
    stroke: "#a7f3d0",
    text: "#ffffff",
    glow: "rgba(16,185,129,0.24)",
  },
  position: {
    fill: "#0284c7",
    stroke: "#7dd3fc",
    text: "#ffffff",
    glow: "rgba(8,145,178,0.24)",
  },
  theme: {
    fill: "#0f766e",
    stroke: "#99f6e4",
    text: "#ffffff",
    glow: "rgba(15,118,110,0.24)",
  },
  source_item: {
    fill: "#d97706",
    stroke: "#fde68a",
    text: "#ffffff",
    glow: "rgba(217,119,6,0.24)",
  },
  raw_evidence: {
    fill: "#64748b",
    stroke: "#cbd5e1",
    text: "#ffffff",
    glow: "rgba(100,116,139,0.24)",
  },
  source: {
    fill: "#16a34a",
    stroke: "#86efac",
    text: "#ffffff",
    glow: "rgba(34,197,94,0.24)",
  },
  conclusion: {
    fill: "#0369a1",
    stroke: "#bae6fd",
    text: "#ffffff",
    glow: "rgba(3,105,161,0.24)",
  },
  lesson: {
    fill: "#65a30d",
    stroke: "#bef264",
    text: "#ffffff",
    glow: "rgba(132,204,22,0.24)",
  },
  review_item: {
    fill: "#ef4444",
    stroke: "#fecaca",
    text: "#ffffff",
    glow: "rgba(239,68,68,0.24)",
  },
  unresolved_question: {
    fill: "#ea580c",
    stroke: "#ffedd5",
    text: "#ffffff",
    glow: "rgba(234,88,12,0.3)",
  },
  shadow_experiment: {
    fill: "#0f766e",
    stroke: "#5eead4",
    text: "#ffffff",
    glow: "rgba(15,118,110,0.24)",
  },
  experiment_result: {
    fill: "#2563eb",
    stroke: "#bfdbfe",
    text: "#ffffff",
    glow: "rgba(37,99,235,0.22)",
  },
  coverage_map: {
    fill: "#64748b",
    stroke: "#cbd5e1",
    text: "#ffffff",
    glow: "rgba(100,116,139,0.22)",
  },
  market_setup_signal: {
    fill: "#c2410c",
    stroke: "#fed7aa",
    text: "#ffffff",
    glow: "rgba(194,65,12,0.24)",
  },
  fundamental_metric: {
    fill: "#0891b2",
    stroke: "#bae6fd",
    text: "#ffffff",
    glow: "rgba(8,145,178,0.24)",
  },
};

function getNodeStyle(nodeType: string) {
  return (
    NODE_STYLES[nodeType] ?? {
      fill: "#475569",
      stroke: "#cbd5e1",
      text: "#ffffff",
      glow: "rgba(71,85,105,0.24)",
    }
  );
}

function compactNodeLabel(label: string, radius: number) {
  const clean = label.replace(/\s+/g, " ").trim();
  const maxCharsPerLine = radius >= 52 ? 16 : radius >= 44 ? 14 : radius >= 36 ? 12 : 10;
  if (!clean) return ["Node"];
  if (clean.length <= maxCharsPerLine) return [clean];
  const words = clean.split(" ");
  const lines: string[] = [];
  let current = "";
  for (const word of words) {
    const next = current ? `${current} ${word}` : word;
    if (next.length <= maxCharsPerLine) {
      current = next;
    } else {
      if (current) lines.push(current);
      current = word;
      if (lines.length >= 2) break;
    }
  }
  if (current && lines.length < 3) lines.push(current);

  return lines.slice(0, 3).map(line =>
    line.length > maxCharsPerLine ? `${line.slice(0, maxCharsPerLine - 1)}…` : line
  );
}

function nodeVisualRadius(node: GraphWebNode, state: { selected: boolean; compare: boolean }) {
  const degree = Number((node as { degree?: number }).degree ?? 0);
  const graphSize = Number((node as { layout_node_count?: number }).layout_node_count ?? 0);
  const densityScale =
    graphSize >= 450
      ? 0.48
      : graphSize >= 300
        ? 0.64
        : graphSize >= 180
          ? 0.78
          : 1;
  const tierBump = node.tier === "critical" ? 5 : node.tier === "high" ? 3 : 0;
  const degreeBump = degree >= 8 ? 5 : degree >= 4 ? 2 : 0;
  const radius = node.is_root
    ? (42 + degreeBump) * Math.max(0.7, densityScale)
    : state.compare
      ? (30 + tierBump) * Math.max(0.65, densityScale)
      : Math.max(9, (22 + tierBump + degreeBump) * densityScale);
  return state.selected ? Math.max(38, radius) : radius;
}

function shouldShowNodeLabel(
  node: PositionedNode,
  state: { selected: boolean; connected: boolean; hovered: boolean; zoom: number },
) {
  if (state.hovered || state.selected || node.is_root) {
    return true;
  }
  if (node.tier === "critical" || node.tier === "high") {
    return true;
  }
  if (state.connected && (node.layer !== "source" || (node.degree ?? 0) > 1)) {
    return true;
  }
  if (state.zoom >= LABEL_DENSE_ZOOM) {
    return node.node_type !== "raw_evidence";
  }
  if (state.zoom >= LABEL_DETAIL_ZOOM) {
    return node.layer === "knowledge" || node.node_type === "position";
  }
  return false;
}

function isSystemDiagnosticNode(node: GraphWebNode) {
  return node.layer === "system";
}

function layerLabel(layer: string) {
  if (layer === "operating") return "operating memory";
  if (layer === "source") return "source";
  if (layer === "system") return "internal system";
  return "knowledge";
}

function nodeTypeLabel(nodeType: string) {
  if (nodeType === "fact") return "Fact";
  if (nodeType === "claim") return "Claim";
  if (nodeType === "event") return "Event";
  if (nodeType === "entity") return "Entity";
  if (nodeType === "position") return "Position";
  if (nodeType === "theme") return "Theme";
  if (nodeType === "source_item") return "Research note";
  if (nodeType === "source") return "Source";
  if (nodeType === "conclusion") return "Conclusion";
  if (nodeType === "lesson") return "Lesson";
  if (nodeType === "fundamental_metric") return "Fundamental metric";
  if (nodeType === "market_setup_signal") return "Market setup";
  if (nodeType === "review_item") return "Review signal";
  if (nodeType === "shadow_experiment") return "Shadow run";
  if (nodeType === "experiment_result") return "Result";
  if (nodeType === "coverage_map") return "Coverage status";
  if (nodeType === "raw_evidence") return "Raw evidence";
  return nodeType.replaceAll("_", " ");
}

function citationOriginClasses(kind?: string | null) {
  const normalized = (kind ?? "").toLowerCase();
  if (["email", "disclosure"].includes(normalized)) {
    return "border-emerald-300 bg-emerald-50 text-emerald-700 dark:border-emerald-800 dark:bg-emerald-950/30 dark:text-emerald-300";
  }
  if (["discovery", "automation"].includes(normalized)) {
    return "border-sky-300 bg-sky-50 text-sky-700 dark:border-sky-800 dark:bg-sky-950/30 dark:text-sky-300";
  }
  if (["manual", "chat"].includes(normalized)) {
    return "border-sky-300 bg-sky-50 text-sky-700 dark:border-sky-800 dark:bg-sky-950/30 dark:text-sky-300";
  }
  return "border-slate-300 bg-slate-50 text-slate-600 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300";
}

function humanizeGraphError(error: unknown) {
  const raw = error instanceof Error ? error.message : String(error || "");
  if (!raw || raw === "Failed to fetch") {
    return "Prophet is reconnecting to the backend. Keeping your last saved knowledge view on screen.";
  }
  if (raw.includes("Failed to fetch")) {
    return "Prophet lost the backend connection for a moment. Keeping your last saved knowledge view on screen.";
  }
  if (raw.includes("graph_node_not_found")) {
    return "There is not enough saved knowledge yet to open this node cleanly.";
  }
  return raw;
}

function normalizeNodeLabel(label: string) {
  return label
    .toLowerCase()
    .replace(/^[a-z0-9.-]{1,8}\s*[·-]\s*/, "")
    .replace(/\([a-z0-9.-]{1,8}\)/g, " ")
    .replace(/\b(corporation|corp|incorporated|inc|company|co)\b/g, " ")
    .replace(/[$%.,:;!?()[\]{}"'`]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function shouldUseNativeGestureEvents() {
  if (typeof navigator === "undefined") {
    return false;
  }
  const vendor = navigator.vendor ?? "";
  const userAgent = navigator.userAgent ?? "";
  const isSafari = vendor.includes("Apple") && !/CriOS|Chrome|Chromium|Edg\//.test(userAgent);
  return isSafari;
}

function svgSafeId(value: string) {
  return value.replace(/[^a-zA-Z0-9_-]/g, "-");
}

function nodeTypeSortOrder(nodeType: string) {
  const orderedTypes = [
    "portfolio",
    "position",
    "entity",
    "theme",
    "conclusion",
    "lesson",
    "fundamental_metric",
    "market_setup_signal",
    "claim",
    "fact",
    "event",
    "source",
    "source_item",
    "review_item",
    "raw_evidence",
    "unresolved_question",
    "shadow_experiment",
    "experiment_result",
    "coverage_map",
  ];
  const index = orderedTypes.indexOf(nodeType);
  return index === -1 ? orderedTypes.length + 1 : index;
}

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

function markSingleGraphRoot<T extends GraphWebNode>(nodes: T[], rootKey: string | null) {
  return nodes.map((node) => {
    const isRoot = Boolean(rootKey && node.key === rootKey);
    return node.is_root === isRoot ? node : { ...node, is_root: isRoot };
  });
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
  for (let iteration = 0; iteration < iterations; iteration += 1) {
    let moved = false;
    for (let aIndex = 0; aIndex < nodes.length; aIndex += 1) {
      for (let bIndex = aIndex + 1; bIndex < nodes.length; bIndex += 1) {
        const a = nodes[aIndex];
        const b = nodes[bIndex];
        const aRadius = nodeVisualRadius(a, { selected: false, compare: false });
        const bRadius = nodeVisualRadius(b, { selected: false, compare: false });
        const minDistance = aRadius + bRadius + NODE_COLLISION_PADDING;
        let dx = (b.x ?? CENTER_X) - (a.x ?? CENTER_X);
        let dy = (b.y ?? CENTER_Y) - (a.y ?? CENTER_Y);
        let distance = Math.hypot(dx, dy);
        if (distance >= minDistance) {
          continue;
        }
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
  for (let tick = 0; tick < 220; tick += 1) {
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

function nodeRetentionScore(node: GraphWebNode) {
  const layerScore =
    node.layer === "knowledge"
      ? 40
      : node.layer === "operating"
        ? 30
        : node.layer === "source"
          ? 20
          : 10;
  const typeScore =
    node.node_type === "entity"
      ? 40
      : node.node_type === "theme"
        ? 36
        : node.node_type === "conclusion"
          ? 34
          : node.node_type === "lesson"
            ? 32
            : node.node_type === "claim"
              ? 28
              : node.node_type === "fact"
                ? 26
                : node.node_type === "event"
                  ? 24
                  : 12;
  const tierScore =
    node.tier === "critical"
      ? 20
      : node.tier === "high"
        ? 14
        : node.tier === "medium"
          ? 8
          : 0;
  return layerScore + typeScore + tierScore + (node.is_root ? 100 : 0);
}

function makeNodeAskPrompt(node: GraphNodeDetail) {
  return `Analyze this graph node in context: ${node.label}. Explain what it means, why it matters, how it connects to my portfolio or current theses, the strongest support and contradiction around it, and what I should watch next.`;
}

function makeEdgeAskPrompt(edge: GraphWebEdge, nodeByKey: Record<string, PositionedNode>) {
  const source = nodeByKey[edge.source_key];
  const target = nodeByKey[edge.target_key];
  const sourceLabel = source?.label ?? edge.source_key;
  const targetLabel = target?.label ?? edge.target_key;
  return `Explain this graph connection: ${sourceLabel} ${edge.relationship_type.replaceAll("_", " ")} ${targetLabel}. Why does this relationship matter, what evidence supports it, what could weaken it, and what are the portfolio implications?`;
}

export default function GraphPage() {
  const [seedItems, setSeedItems] = useState<ProfileListItem[]>([]);
  const [knowledgeQuery, setKnowledgeQuery] = useState("");
  const [knowledgeResults, setKnowledgeResults] = useState<GraphSearchResult[]>([]);
  const [knowledgeSearching, setKnowledgeSearching] = useState(false);
  const [workspaceGraph, setWorkspaceGraph] = useState<GraphNeighborhood | null>(null);
  const [allKnowledgeGraph, setAllKnowledgeGraph] = useState<GraphNeighborhood | null>(null);
  const [stableGraph, setStableGraph] = useState<GraphNeighborhood | null>(null);
  const [graphStats, setGraphStats] = useState<GraphStats | null>(null);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [selectedDetail, setSelectedDetail] = useState<GraphNodeDetail | null>(null);
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null);
  const [graph, setGraph] = useState<GraphNeighborhood | null>(null);
  const [graphMode, setGraphMode] = useState<GraphMode>("portfolio");
  const [showSystemNodes, setShowSystemNodes] = useState(false);
  const [showDuplicateNodes, setShowDuplicateNodes] = useState(false);
  const [activeNodeTypes, setActiveNodeTypes] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [graphNotice, setGraphNotice] = useState<string | null>(null);
  const [hoveredNodeKey, setHoveredNodeKey] = useState<string | null>(null);
  const [hoveredEdgeId, setHoveredEdgeId] = useState<string | null>(null);

  // Layout Stability: Persistent coordinate map to prevent 'rebuilding' jumps
  const nodePositionsRef = useRef<Map<string, { x: number; y: number; vx?: number; vy?: number }>>(new Map());
  const manualNodePositionsRef = useRef<Record<string, { x: number; y: number }>>({});
  const selectionGenerationRef = useRef(0);
  const graphInitializationStartedRef = useRef(false);
  const [graphAskJob, setGraphAskJob] = useState<AgentTurnJob | null>(null);
  const [graphAskResult, setGraphAskResult] = useState<AgentTurn | null>(null);
  const [graphAskError, setGraphAskError] = useState<string | null>(null);
  const [graphAskTargetLabel, setGraphAskTargetLabel] = useState<string | null>(null);
  const [graphAskContext, setGraphAskContext] = useState<"node" | "edge" | null>(null);
  const [graphAskUserPrompt, setGraphAskUserPrompt] = useState("");
  const [showSystemCitations, setShowSystemCitations] = useState(false);
  const simulationRef = useRef<d3.Simulation<PositionedNode, PositionedLink> | null>(null);
  const [nodes, setNodes] = useState<PositionedNode[]>([]);
  const stableGraphRef = useRef<GraphNeighborhood | null>(null);
  const graphViewportRef = useRef<HTMLDivElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const containerRef = useRef<SVGGElement>(null);
  const renderedNodesRef = useRef<PositionedNode[]>([]);
  const [layoutNonce, setLayoutNonce] = useState(0);
  const [viewTransform, setViewTransform] = useState({ x: 0, y: 0, k: 1 });
  const viewTransformRef = useRef(viewTransform);
  const [graphViewBox, setGraphViewBox] = useState<GraphViewBox>(DEFAULT_GRAPH_VIEW_BOX);
  const graphViewBoxRef = useRef<GraphViewBox>(DEFAULT_GRAPH_VIEW_BOX);
  const layoutTargetAspectRef = useRef(MAX_GRAPH_LAYOUT_TARGET_ASPECT);

  const activeNodeDragRef = useRef<{
    key: string;
    pointerId: number;
    dx: number;
    dy: number;
    startClientX: number;
    startClientY: number;
    nodeType: string;
    nodeId: string;
  } | null>(null);
  const isDraggingNodeRef = useRef(false);
  const zoomRef = useRef<d3.ZoomBehavior<SVGSVGElement, unknown> | null>(null);
  const hasGraphNodes = nodes.length > 0;


  // Attach D3 zoom when the graph surface exists. Keep this separate from
  // viewTransform state so selection/detail renders cannot rebind zoom or snap
  // the viewport.
  useEffect(() => {
    if (!svgRef.current || !containerRef.current) return;

    const svgEl = svgRef.current;
    const svg = d3.select(svgEl);
    const zoom = d3.zoom<SVGSVGElement, unknown>()
      .scaleExtent([0.05, 5])
      .filter((event) => {
        if (event.type === "wheel") {
          return true;
        }
        const target = event.target;
        if (!(target instanceof Element)) {
          return true;
        }
        return !target.closest(".graph-node, .graph-edge-hit, button, a, textarea, input, select");
      })
      .on("zoom", (event) => {
        const { transform } = event;
        d3.select(containerRef.current).attr("transform", transform.toString());
        setViewTransform({ x: transform.x, y: transform.y, k: transform.k });
        viewTransformRef.current = { x: transform.x, y: transform.y, k: transform.k };
      });

    svg.call(zoom);
    zoomRef.current = zoom;

    // specialized handling for Safari and trackpad pinch-to-zoom
    const handleWheel = (e: WheelEvent) => {
      if (e.ctrlKey) {
        // This is a pinch gesture on trackpads (macOS) or ctrl+wheel
        // We prevent default to stop the browser from zooming the whole page
        e.preventDefault();
        // D3 zoom usually handles the wheel event automatically when svg.call(zoom) is used,
        // but it needs the event NOT to be defaultPrevented unless we're in Safari
        // where we might need specialized handling.
      }
    };

    svgEl.addEventListener("wheel", handleWheel, { passive: false });

    // Safari specific gesture events
    const handleGesture = (e: Event) => {
      e.preventDefault();
      if (e.type === "gesturechange") {
        const gesture = e as GestureEventWithScale;
        const transform = viewTransformRef.current;
        viewTransformRef.current = { ...transform, k: transform.k * (gesture.scale ?? 1) };
        // This is a simplification; D3 zoom is better if we can just pipe it.
      }
    };

    if (shouldUseNativeGestureEvents()) {
      svgEl.addEventListener("gesturestart", handleGesture);
      svgEl.addEventListener("gesturechange", handleGesture);
      svgEl.addEventListener("gestureend", handleGesture);
    }

    return () => {
      svgEl.removeEventListener("wheel", handleWheel);
      if (shouldUseNativeGestureEvents()) {
        svgEl.removeEventListener("gesturestart", handleGesture);
        svgEl.removeEventListener("gesturechange", handleGesture);
        svgEl.removeEventListener("gestureend", handleGesture);
      }
    };
  }, [hasGraphNodes]);

  const toSvgPoint = useCallback((clientX: number, clientY: number) => {
    if (!svgRef.current) return { x: CENTER_X, y: CENTER_Y };
    const rect = svgRef.current.getBoundingClientRect();
    const viewBox = graphViewBoxRef.current;
    return {
      x: viewBox.x + ((clientX - rect.left) / rect.width) * viewBox.width,
      y: viewBox.y + ((clientY - rect.top) / rect.height) * viewBox.height,
    };
  }, []);

  const toGraphPoint = useCallback((clientX: number, clientY: number) => {
    const point = toSvgPoint(clientX, clientY);
    const transform = viewTransformRef.current;
    return {
      x: (point.x - transform.x) / transform.k,
      y: (point.y - transform.y) / transform.k,
    };
  }, [toSvgPoint]);

  const viewportAnchor = useCallback(() => {
    return { x: CENTER_X, y: CENTER_Y };
  }, []);

  const adjustZoom = useCallback((newK: number, centerX: number, centerY: number) => {
    if (!svgRef.current || !zoomRef.current) return;
    const svg = d3.select(svgRef.current);
    // Scale around the point (centerX, centerY)
    svg.transition().duration(250).call(
      zoomRef.current.transform,
      d3.zoomIdentity
        .translate(centerX, centerY)
        .scale(newK)
        .translate(-centerX, -centerY)
    );
  }, []);

  const fitNodesToViewport = useCallback((targetNodes: PositionedNode[]) => {
    if (!targetNodes.length || !svgRef.current) return;
    const positioned = targetNodes.filter((n): n is PositionedNode & { x: number; y: number } => Number.isFinite(n.x) && Number.isFinite(n.y));
    if (!positioned.length) return;

    const firstRadius = nodeVisualRadius(positioned[0], { selected: false, compare: false });
    let minX = positioned[0].x - firstRadius;
    let maxX = positioned[0].x + firstRadius;
    let minY = positioned[0].y - firstRadius;
    let maxY = positioned[0].y + firstRadius;
    for (const n of positioned) {
      const radius = nodeVisualRadius(n, { selected: false, compare: false });
      minX = Math.min(minX, n.x - radius); maxX = Math.max(maxX, n.x + radius);
      minY = Math.min(minY, n.y - radius); maxY = Math.max(maxY, n.y + radius);
    }

    const viewBox = graphViewBoxRef.current;
    const padding = Math.min(120, Math.max(64, Math.min(viewBox.width, viewBox.height) * 0.045));
    const spanX = Math.max(maxX - minX, 100);
    const spanY = Math.max(maxY - minY, 100);
    const availableWidth = Math.max(100, viewBox.width - padding * 2);
    const availableHeight = Math.max(100, viewBox.height - padding * 2);
    const scale = Math.max(0.08, Math.min(3.5, Math.min(availableWidth / spanX, availableHeight / spanY)));
    const viewportCenterX = viewBox.x + viewBox.width / 2;
    const viewportCenterY = viewBox.y + viewBox.height / 2;

    const nextTransform = d3.zoomIdentity
      .translate(
        viewportCenterX - ((minX + maxX) / 2) * scale,
        viewportCenterY - ((minY + maxY) / 2) * scale,
      )
      .scale(scale);

    const nextViewTransform = { x: nextTransform.x, y: nextTransform.y, k: nextTransform.k };
    viewTransformRef.current = nextViewTransform;
    setViewTransform(nextViewTransform);

    if (zoomRef.current) {
      d3.select(svgRef.current)
        .transition()
        .duration(750)
        .call(zoomRef.current.transform, nextTransform);
    } else {
      d3.select(containerRef.current).attr("transform", nextTransform.toString());
    }
  }, []);

  useEffect(() => {
    const viewport = graphViewportRef.current;
    if (!viewport) return;

    let animationFrame = 0;
    const resizeObserver = new ResizeObserver((entries) => {
      const rect = entries[0]?.contentRect;
      if (!rect) return;
      const nextViewBox = graphViewBoxForViewport(rect.width, rect.height);
      const current = graphViewBoxRef.current;
      const changed =
        Math.abs(current.x - nextViewBox.x) > 0.5 ||
        Math.abs(current.y - nextViewBox.y) > 0.5 ||
        Math.abs(current.width - nextViewBox.width) > 0.5 ||
        Math.abs(current.height - nextViewBox.height) > 0.5;
      if (!changed) return;
      graphViewBoxRef.current = nextViewBox;
      setGraphViewBox(nextViewBox);
      const nextTargetAspect = graphLayoutTargetAspect(nextViewBox);
      if (Math.abs(layoutTargetAspectRef.current - nextTargetAspect) > 0.04) {
        layoutTargetAspectRef.current = nextTargetAspect;
        nodePositionsRef.current.clear();
        setLayoutNonce((currentNonce) => currentNonce + 1);
      }
      cancelAnimationFrame(animationFrame);
      animationFrame = requestAnimationFrame(() => {
        fitNodesToViewport(renderedNodesRef.current);
      });
    });
    resizeObserver.observe(viewport);
    return () => {
      cancelAnimationFrame(animationFrame);
      resizeObserver.disconnect();
    };
  }, [fitNodesToViewport, hasGraphNodes]);

  const freezeSimulationNodes = useCallback(
    (simulation: d3.Simulation<PositionedNode, PositionedLink>) => {
      const frozen = (simulation.nodes() as PositionedNode[]).map((node) => {
        const x = node.x ?? CENTER_X;
        const y = node.y ?? CENTER_Y;
        node.x = x;
        node.y = y;
        node.vx = 0;
        node.vy = 0;
        nodePositionsRef.current.set(node.key, { x, y, vx: 0, vy: 0 });
        return { ...node, x, y, vx: 0, vy: 0 };
      });
      simulation.stop();
      return frozen;
    },
    [],
  );

  const handleNodePointerDown = useCallback(
    (event: React.PointerEvent<SVGGElement>, node: PositionedNode) => {
      event.stopPropagation();
      const pointer = toGraphPoint(event.clientX, event.clientY);
      isDraggingNodeRef.current = false;
      activeNodeDragRef.current = {
        key: node.key,
        pointerId: event.pointerId,
        dx: pointer.x - (node.x ?? 0),
        dy: pointer.y - (node.y ?? 0),
        startClientX: event.clientX,
        startClientY: event.clientY,
        nodeType: node.node_type,
        nodeId: node.id,
      };
      graphViewportRef.current?.setPointerCapture(event.pointerId);
    },
    [toGraphPoint],
  );

  useEffect(() => {
    stableGraphRef.current = stableGraph;
  }, [stableGraph]);

  const loadSeeds = useCallback(async () => {
    const items = await apiFetch<ProfileListItem[]>("/profiles?show_all=false");
    setSeedItems(items);
    return items;
  }, []);

  const loadGraphStats = useCallback(async () => {
    try {
      setGraphStats(await apiFetch<GraphStats>("/graph/stats"));
    } catch {
      setGraphStats(null);
    }
  }, []);

  const buildGraphFromSeeds = useCallback(
    async (
      seeds: Array<{ subject_type: string; subject_id: string }>,
      options?: { preferredRootKey?: string | null; depth?: number; limit?: number },
    ) => {
      const seedMap = new Map<string, { subject_type: string; subject_id: string }>();
      for (const seed of seeds) {
        seedMap.set(`${seed.subject_type}:${seed.subject_id}`, seed);
      }
      const seedSlice = Array.from(seedMap.values());
      const depth = options?.depth ?? OVERVIEW_NEIGHBORHOOD_DEPTH;
      const limit = options?.limit ?? OVERVIEW_NEIGHBORHOOD_LIMIT;
      const neighborhoods = await Promise.all(
        seedSlice.map((item) =>
          apiFetch<GraphNeighborhood>(
            `/graph/neighborhood/${item.subject_type}/${item.subject_id}?depth=${depth}&limit=${limit}&include_system=${showSystemNodes}`,
          ).catch(() => null),
        ),
      );
      const valid = neighborhoods.filter((item): item is GraphNeighborhood => item !== null);
      const nonSystemValid = valid.filter((item) => {
        const root = item.nodes.find((node) => node.key === item.root_key);
        return root?.layer !== "system";
      });
      const usable = nonSystemValid.length > 0 ? nonSystemValid : valid;
      if (usable.length === 0) {
        return null;
      }
      const nodeMap = new Map<string, GraphWebNode>();
      const edgeMap = new Map<string, GraphNeighborhood["edges"][number]>();
      for (const neighborhood of usable) {
        for (const node of neighborhood.nodes) {
          if (!nodeMap.has(node.key)) {
            nodeMap.set(node.key, node);
          }
        }
        for (const edge of neighborhood.edges) {
          const edgeKey = graphEdgeIdentity(edge);
          if (!edgeMap.has(edgeKey)) {
            edgeMap.set(edgeKey, edge);
          }
        }
      }
      const mergedNodes = Array.from(nodeMap.values());
      const overviewRootKey =
        (options?.preferredRootKey && mergedNodes.some((node) => node.key === options.preferredRootKey)
          ? options.preferredRootKey
          : null) ??
        mergedNodes.find((node) => node.node_type === "portfolio")?.key ??
        usable[0].root_key;
      return {
        root_key: overviewRootKey,
        depth,
        nodes: markSingleGraphRoot(mergedNodes, overviewRootKey),
        edges: Array.from(edgeMap.values()),
      } satisfies GraphNeighborhood;
    },
    [showSystemNodes],
  );

  const buildWorkspaceGraph = useCallback(async (items: ProfileListItem[]) => {
    const portfolioProfile = items.find((item) => item.subject_type === "portfolio");
    let portfolioId: string | null = portfolioProfile?.subject_id ?? null;
    let holdingSeeds: Array<{ subject_type: string; subject_id: string }> = [];
    try {
      const overview = await apiFetch<PortfolioGraphOverview>("/portfolio/overview");
      portfolioId = overview.id ?? overview.portfolio_id ?? portfolioId;
      holdingSeeds = (overview.holdings ?? []).slice(0, OVERVIEW_SEED_LIMIT).map((holding) => ({
        subject_type: "position",
        subject_id: holding.id,
      }));
    } catch {
      // Fallback if overview fails
    }

    const seedCandidates = [
      ...(portfolioId ? [{ subject_type: "portfolio", subject_id: portfolioId }] : []),
      ...holdingSeeds,
      ...items.slice(0, OVERVIEW_SEED_LIMIT).map((item) => ({
        subject_type: item.subject_type,
        subject_id: item.subject_id,
      })),
    ];
    return buildGraphFromSeeds(seedCandidates, {
      preferredRootKey: portfolioId ? `portfolio:${portfolioId}` : null,
    });
  }, [buildGraphFromSeeds]);

  const buildAllKnowledgeGraph = useCallback(async () => {
    const items = await apiFetch<ProfileListItem[]>("/profiles?show_all=true");
    const portfolioProfile = items.find((item) => item.subject_type === "portfolio");
    return buildGraphFromSeeds(
      items.slice(0, ALL_KNOWLEDGE_SEED_LIMIT).map((item) => ({
        subject_type: item.subject_type,
        subject_id: item.subject_id,
      })),
      {
        preferredRootKey: portfolioProfile ? `portfolio:${portfolioProfile.subject_id}` : null,
      },
    );
  }, [buildGraphFromSeeds]);

  useEffect(() => {
    const query = knowledgeQuery.trim();
    if (query.length < 2) {
      setKnowledgeResults([]);
      setKnowledgeSearching(false);
      return;
    }
    setKnowledgeSearching(true);
    const timeout = window.setTimeout(() => {
      void apiFetch<GraphSearchResult[]>(
        `/graph/search?query=${encodeURIComponent(query)}&limit=12`,
      )
        .then(setKnowledgeResults)
        .catch(() => setKnowledgeResults([]))
        .finally(() => setKnowledgeSearching(false));
    }, 250);
    return () => window.clearTimeout(timeout);
  }, [knowledgeQuery]);

  const loadDetail = useCallback(async (nodeType: string, nodeId: string, generation?: number) => {
    setDetailLoading(true);
    try {
      const detail = await apiFetch<GraphNodeDetail>(`/graph/nodes/${nodeType}/${nodeId}`);
      if (generation !== undefined && generation !== selectionGenerationRef.current) {
        return;
      }
      setSelectedDetail(detail);
      setSelectedKey(`${nodeType}:${nodeId}`);
      setSelectedEdgeId(null);
    } catch (err) {
      if (generation === undefined || generation === selectionGenerationRef.current) {
        setError(err instanceof Error ? err.message : "Unable to load node detail.");
      }
    } finally {
      if (generation === undefined || generation === selectionGenerationRef.current) {
        setDetailLoading(false);
      }
    }
  }, []);

  const openGraph = useCallback(async (nodeType: string, nodeId: string, options?: { setActive?: boolean }) => {
    const generation = ++selectionGenerationRef.current;
    const setActive = options?.setActive ?? true;
    setLoading(true);
    setError(null);
    setGraphNotice(null);
    try {
      const neighborhood = await apiFetch<GraphNeighborhood>(
        `/graph/neighborhood/${nodeType}/${nodeId}?depth=${FOCUSED_NEIGHBORHOOD_DEPTH}&limit=${FOCUSED_NEIGHBORHOOD_LIMIT}&include_system=${showSystemNodes}`,
      );
      if (generation !== selectionGenerationRef.current) return;
      setGraph(neighborhood);
      setStableGraph(neighborhood);
      setGraphMode("focused");
      viewTransformRef.current = { x: 0, y: 0, k: 1 };
      setViewTransform({ x: 0, y: 0, k: 1 });
      nodePositionsRef.current.clear();
      manualNodePositionsRef.current = {};
      if (setActive) {
        setSelectedKey(neighborhood.root_key);
      }
      setSelectedEdgeId(null);
      await loadDetail(nodeType, nodeId, generation);
    } catch (err) {
      if (generation !== selectionGenerationRef.current) return;
      const message = humanizeGraphError(err);
      if (stableGraphRef.current) {
        setGraphNotice(message);
      } else {
        setError(message);
      }
    } finally {
      if (generation === selectionGenerationRef.current) {
        setLoading(false);
      }
    }
  }, [loadDetail, showSystemNodes]);

  useEffect(() => {
    if (graphInitializationStartedRef.current) return;
    graphInitializationStartedRef.current = true;
    const initializationGeneration = selectionGenerationRef.current;
    void (async () => {
      try {
        void loadGraphStats();
        const items = await loadSeeds();
        if (initializationGeneration !== selectionGenerationRef.current) return;
        setGraphNotice(null);
        if (items[0]) {
          const workspaceGraph = await buildWorkspaceGraph(items);
          if (initializationGeneration !== selectionGenerationRef.current) return;
          if (workspaceGraph) {
            setWorkspaceGraph(workspaceGraph);
            setGraph(workspaceGraph);
            setStableGraph(workspaceGraph);
            setGraphMode("portfolio");
            nodePositionsRef.current.clear();
            manualNodePositionsRef.current = {};
            setSelectedKey(workspaceGraph.root_key);
            setSelectedEdgeId(null);
            const portfolioProfile = items.find((item) => item.subject_type === "portfolio");
            await loadDetail(
              portfolioProfile?.subject_type ?? items[0].subject_type,
              portfolioProfile?.subject_id ?? items[0].subject_id,
              initializationGeneration,
            );
            if (initializationGeneration === selectionGenerationRef.current) {
              setLoading(false);
            }
          } else {
            await openGraph(items[0].subject_type, items[0].subject_id);
          }
        } else {
          setError(humanizeGraphError("graph_node_not_found"));
          setLoading(false);
        }
      } catch (err) {
        if (initializationGeneration === selectionGenerationRef.current) {
          setError(humanizeGraphError(err));
          setLoading(false);
        }
      }
    })();
  }, [buildWorkspaceGraph, loadDetail, loadGraphStats, loadSeeds, openGraph]);

  useEffect(() => {
    if (!graphAskJob || !["queued", "running"].includes(graphAskJob.status)) {
      return;
    }
    const interval = window.setInterval(() => {
      void apiFetch<AgentTurnJob>(`/agent/turn-jobs/${graphAskJob.job_id}`)
        .then((job) => {
          setGraphAskJob(job);
          if (job.status === "completed") {
            setGraphAskResult(job.result ?? null);
          } else if (job.status === "error") {
            setGraphAskError(job.error || "Unable to complete the graph analysis.");
          }
        })
        .catch((err) => {
          setGraphAskError(err instanceof Error ? err.message : "Unable to refresh graph analysis.");
        });
    }, 1200);
    return () => window.clearInterval(interval);
  }, [graphAskJob]);

  useEffect(() => {
    setGraphAskJob(null);
    setGraphAskResult(null);
    setGraphAskError(null);
    setGraphAskTargetLabel(null);
    setGraphAskContext(null);
    setGraphAskUserPrompt("");
  }, [selectedKey, selectedEdgeId]);

  useEffect(() => {
    setShowSystemCitations(false);
  }, [selectedDetail?.id]);

  const activeGraph = graph ?? stableGraph;
  const activeRootKey = activeGraph?.root_key ?? null;

  const availableNodeTypes = useMemo(() => {
    if (!activeGraph) {
      return [] as Array<{ type: string; count: number }>;
    }
    const counts = new Map<string, number>();
    for (const node of activeGraph.nodes) {
      if (!showSystemNodes && isSystemDiagnosticNode(node)) {
        continue;
      }
      if (node.node_type === "unresolved_question") {
        continue;
      }
      counts.set(node.node_type, (counts.get(node.node_type) ?? 0) + 1);
    }
    return Array.from(counts.entries())
      .sort((a, b) => nodeTypeSortOrder(a[0]) - nodeTypeSortOrder(b[0]) || b[1] - a[1])
      .map(([type, count]) => ({ type, count }));
  }, [activeGraph, showSystemNodes]);

  const toggleNodeTypeFilter = useCallback((nodeType: string) => {
    setActiveNodeTypes((current) => {
      if (current.length === 0) {
        return [nodeType];
      }
      if (current.includes(nodeType)) {
        const next = current.filter((value) => value !== nodeType);
        return next.length === 0 ? [] : next;
      }
      return [...current, nodeType].sort((a, b) => nodeTypeSortOrder(a) - nodeTypeSortOrder(b));
    });
  }, []);

  const visibleGraph = useMemo(() => {
    if (!activeGraph) {
      return null;
    }
    let visibleNodes = activeGraph.nodes.filter(
      (node) => {
        const protectedNode = node.key === activeRootKey;
        if (protectedNode) {
          return true;
        }
        if (!showSystemNodes && isSystemDiagnosticNode(node)) {
          return false;
        }
        if (node.node_type === "unresolved_question") {
          return false;
        }
        if (activeNodeTypes.length > 0 && !activeNodeTypes.includes(node.node_type)) {
          return false;
        }
        return true;
      },
    );
    if (!showDuplicateNodes) {
      const protectedKeys = new Set([activeRootKey].filter((key): key is string => Boolean(key)));
      const buckets = new Map<string, GraphWebNode[]>();
      for (const node of visibleNodes) {
        const bucket = `${node.node_type}:${normalizeNodeLabel(node.label)}`;
        const current = buckets.get(bucket) ?? [];
        current.push(node);
        buckets.set(bucket, current);
      }
      const hiddenKeys = new Set<string>();
      for (const bucketNodes of buckets.values()) {
        if (bucketNodes.length <= 1) {
          continue;
        }
        const ranked = [...bucketNodes].sort((a, b) => {
          const aProtected = protectedKeys.has(a.key) ? 1 : 0;
          const bProtected = protectedKeys.has(b.key) ? 1 : 0;
          return (
            bProtected - aProtected ||
            nodeRetentionScore(b) - nodeRetentionScore(a) ||
            a.label.localeCompare(b.label)
          );
        });
        for (const node of ranked.slice(1)) {
          hiddenKeys.add(node.key);
        }
      }
      visibleNodes = visibleNodes.filter((node) => !hiddenKeys.has(node.key));
    }
    const portfolioNodes = visibleNodes.filter((node) => node.node_type === "portfolio");
    if (portfolioNodes.length > 1) {
      const preferredPortfolioKey =
        portfolioNodes.find((node) => node.key === activeRootKey)?.key ?? portfolioNodes[0]?.key;
      visibleNodes = visibleNodes.filter(
        (node) => node.node_type !== "portfolio" || node.key === preferredPortfolioKey,
      );
    }
    const visibleKeys = new Set(visibleNodes.map((node) => node.key));
    const candidateEdges: GraphWebEdge[] = [];
    const seenEdgeKeys = new Set<string>();
    for (const edge of activeGraph.edges) {
      if (!visibleKeys.has(edge.source_key) || !visibleKeys.has(edge.target_key)) {
        continue;
      }
      const edgeKey = graphEdgeIdentity(edge);
      if (seenEdgeKeys.has(edgeKey)) {
        continue;
      }
      seenEdgeKeys.add(edgeKey);
      candidateEdges.push(edge);
    }

    return {
      ...activeGraph,
      root_key: activeRootKey ?? activeGraph.nodes[0]?.key ?? "",
      nodes: markSingleGraphRoot(visibleNodes, activeRootKey ?? activeGraph.nodes[0]?.key ?? null),
      edges: candidateEdges,
    };
  }, [
    activeGraph,
    activeRootKey,
    activeNodeTypes,
    showDuplicateNodes,
    showSystemNodes,
  ]);

  const visibleSeedItems = useMemo(() => seedItems, [seedItems]);
  const rawNodeCount = activeGraph?.nodes.length ?? 0;
  const rawEdgeCount = activeGraph?.edges.length ?? 0;
  const visibleNodeCount = visibleGraph?.nodes.length ?? 0;
  const visibleEdgeCount = visibleGraph?.edges.length ?? 0;
  const hiddenNodeCount = Math.max(0, rawNodeCount - visibleNodeCount);
  const hiddenEdgeCount = Math.max(0, rawEdgeCount - visibleEdgeCount);
  const modeLabel =
    graphMode === "all"
      ? "Expanded Web"
      : graphMode === "focused"
        ? "Focused Node"
        : "Portfolio Web";

  // --- Physics Simulation ---
  useEffect(() => {
    if (!visibleGraph || !visibleGraph.nodes.length) {
      setNodes([]);
      return;
    }

    const degreeByKey = new Map<string, number>();
    for (const edge of visibleGraph.edges) {
      degreeByKey.set(edge.source_key, (degreeByKey.get(edge.source_key) ?? 0) + 1);
      degreeByKey.set(edge.target_key, (degreeByKey.get(edge.target_key) ?? 0) + 1);
    }

    // Stable Layout: Use target positions from the persistent ref map
    const initialNodes: PositionedNode[] = visibleGraph.nodes.map((n, index) => {
      const manualPosition = manualNodePositionsRef.current[n.key];
      const existing = nodePositionsRef.current.get(n.key);
      const fallback = deterministicNodePosition(n, index);
      const degree = degreeByKey.get(n.key) ?? 0;
      return {
        ...n,
        degree,
        layout_node_count: visibleGraph.nodes.length,
        x: manualPosition?.x ?? existing?.x ?? fallback.x,
        y: manualPosition?.y ?? existing?.y ?? fallback.y,
        vx: existing?.vx ?? 0,
        vy: existing?.vy ?? 0,
        fx: manualPosition?.x,
        fy: manualPosition?.y,
      };
    });

    const initialLinks: PositionedLink[] = visibleGraph.edges.map(e => ({
      source: e.source_key,
      target: e.target_key,
    }));

    // Re-use or initialize simulation instance
    let simulation = simulationRef.current;
    if (!simulation) {
      simulation = d3.forceSimulation<PositionedNode>()
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

      simulationRef.current = simulation;
    }

    // Update simulation data
    simulation.nodes(initialNodes);
    simulation
      .force("x", d3.forceX<PositionedNode>(CENTER_X).strength(nodeLayoutStrength))
      .force("y", d3.forceY<PositionedNode>(CENTER_Y).strength(nodeLayoutStrength))
      .force("clusterX", null)
      .force("clusterY", null);
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
    for (let index = 0; index < 180; index += 1) {
      simulation.tick();
    }
    const frozen = resolveNodeOverlaps(
      normalizeGraphLayout(freezeSimulationNodes(simulation), visibleGraph.root_key),
      visibleGraph.root_key,
      layoutTargetAspectRef.current,
    );
    for (const node of frozen) {
      nodePositionsRef.current.set(node.key, { x: node.x ?? CENTER_X, y: node.y ?? CENTER_Y, vx: 0, vy: 0 });
    }
    renderedNodesRef.current = frozen;
    setNodes(frozen);
    fitNodesToViewport(frozen);
  }, [fitNodesToViewport, freezeSimulationNodes, layoutNonce, visibleGraph]);

  // Clean stop on page unmount
  useEffect(() => {
    return () => {
      if (simulationRef.current) simulationRef.current.stop();
    };
  }, []);

  const displayedNodes = useMemo(
    () =>
      nodes.map((node) => {
        const manual = manualNodePositionsRef.current[node.key];
        return manual ? { ...node, x: manual.x, y: manual.y, fx: manual.x, fy: manual.y } : node;
      }),
    [nodes],
  );

  const nodeByKey = useMemo(() => {
    const map: Record<string, PositionedNode> = {};
    for (const n of displayedNodes) {
      map[n.key] = n;
    }
    return map;
  }, [displayedNodes]);
  const selectedNode = selectedKey ? nodeByKey[selectedKey] ?? null : null;
  const displayedEdges = useMemo(() => visibleGraph?.edges ?? [], [visibleGraph]);
  const activeEdge = useMemo(
    () => displayedEdges.find((edge) => graphEdgeIdentity(edge) === selectedEdgeId) ?? null,
    [displayedEdges, selectedEdgeId],
  );
  const hoverPreview = useMemo(() => {
    const toViewportPercent = (x: number, y: number) => {
      const transformedX = x * viewTransform.k + viewTransform.x;
      const transformedY = y * viewTransform.k + viewTransform.y;
      const horizontalPercent = ((transformedX - graphViewBox.x) / graphViewBox.width) * 100;
      const verticalPercent = ((transformedY - graphViewBox.y) / graphViewBox.height) * 100;
      return {
        left: `clamp(152px, ${horizontalPercent}%, calc(100% - 152px))`,
        top: `clamp(96px, ${verticalPercent}%, calc(100% - 132px))`,
      };
    };

    if (hoveredNodeKey) {
      const node = nodeByKey[hoveredNodeKey];
      if (!node || !Number.isFinite(node.x) || !Number.isFinite(node.y)) {
        return null;
      }
      return {
        kind: "node",
        title: node.label,
        eyebrow: `${nodeTypeLabel(node.node_type)} · ${layerLabel(node.layer)}`,
        detail: node.subtitle ?? `${node.degree ?? 0} graph connections`,
        ...toViewportPercent(node.x ?? CENTER_X, node.y ?? CENTER_Y),
      };
    }

    if (hoveredEdgeId) {
      const edge = displayedEdges.find((candidate) => graphEdgeIdentity(candidate) === hoveredEdgeId);
      const source = edge ? nodeByKey[edge.source_key] : null;
      const target = edge ? nodeByKey[edge.target_key] : null;
      if (!edge || !source || !target) {
        return null;
      }
      const sourceX = source.x ?? CENTER_X;
      const sourceY = source.y ?? CENTER_Y;
      const targetX = target.x ?? CENTER_X;
      const targetY = target.y ?? CENTER_Y;
      return {
        kind: "edge",
        title: edge.relationship_type.replaceAll("_", " "),
        eyebrow: `Connection · ${Math.round(edge.confidence * 100)}% confidence`,
        detail: `${source.label} -> ${target.label}`,
        ...toViewportPercent((sourceX + targetX) / 2, (sourceY + targetY) / 2),
      };
    }

    return null;
  }, [displayedEdges, graphViewBox, hoveredEdgeId, hoveredNodeKey, nodeByKey, viewTransform]);
  const connectedKeys = useMemo(() => {
    if (!selectedKey) return new Set<string>();
    const keys = new Set<string>();
    for (const e of displayedEdges) {
      if (e.source_key === selectedKey) keys.add(e.target_key);
      if (e.target_key === selectedKey) keys.add(e.source_key);
    }
    return keys;
  }, [displayedEdges, selectedKey]);

  const describeEdge = useCallback((edge: GraphWebEdge | null) => {
    if (!edge) return "";
    const source = nodeByKey[edge.source_key];
    const target = nodeByKey[edge.target_key];
    const sName = source?.label ?? edge.source_key;
    const tName = target?.label ?? edge.target_key;
    return `${sName} ${edge.relationship_type.replace(/_/g, " ")} ${tName}`;
  }, [nodeByKey]);

  const handleNodeClick = useCallback(
    (node: { key: string; node_type: string; id: string }) => {
      const generation = ++selectionGenerationRef.current;
      setSelectedKey(node.key);
      setSelectedEdgeId(null);
      setSelectedDetail(null);
      void loadDetail(node.node_type, node.id, generation);
    },
    [loadDetail],
  );

  const isBackgroundInteractionTarget = useCallback((target: EventTarget | null) => {
    if (!(target instanceof Element)) return true;
    return !target.closest(".graph-node, .graph-edge-hit, button, a, textarea, input, select");
  }, []);

  const handleCanvasPointerDown = useCallback((event: React.PointerEvent) => {
    // Canvas background clicks to deselect
    if (isBackgroundInteractionTarget(event.target)) {
      selectionGenerationRef.current += 1;
      setDetailLoading(false);
      setSelectedKey(null);
      setSelectedEdgeId(null);
      setSelectedDetail(null);
    }
  }, [isBackgroundInteractionTarget]);

  const handleCanvasPointerMove = useCallback((event: React.PointerEvent) => {
    if (activeNodeDragRef.current) {
      const drag = activeNodeDragRef.current;
      if (!isDraggingNodeRef.current) {
        const dist = Math.hypot(event.clientX - drag.startClientX, event.clientY - drag.startClientY);
        if (dist > NODE_DRAG_THRESHOLD_PX) {
          isDraggingNodeRef.current = true;
        }
      }

      if (isDraggingNodeRef.current) {
        const pointer = toGraphPoint(event.clientX, event.clientY);
        const x = pointer.x - drag.dx;
        const y = pointer.y - drag.dy;

        manualNodePositionsRef.current = { ...manualNodePositionsRef.current, [drag.key]: { x, y } };
        nodePositionsRef.current.set(drag.key, { x, y, vx: 0, vy: 0 });
        setNodes(curr => curr.map(n => n.key === drag.key ? { ...n, x, y, fx: x, fy: y } : n));
      }
    }
  }, [toGraphPoint]);

  const handleCanvasPointerUp = useCallback((event: React.PointerEvent) => {
    if (activeNodeDragRef.current) {
      const drag = activeNodeDragRef.current;
      if (!isDraggingNodeRef.current) {
        // Simple click
        handleNodeClick({ key: drag.key, node_type: drag.nodeType, id: drag.nodeId });
      }
      activeNodeDragRef.current = null;
      isDraggingNodeRef.current = false;
      graphViewportRef.current?.releasePointerCapture(event.pointerId);
    }
  }, [handleNodeClick]);

  function restoreWorkspaceGraph() {
    if (!workspaceGraph) {
      return;
    }
    setGraph(workspaceGraph);
    setStableGraph(workspaceGraph);
    setGraphMode("portfolio");
    viewTransformRef.current = { x: 0, y: 0, k: 1 };
    setViewTransform({ x: 0, y: 0, k: 1 });
    nodePositionsRef.current.clear();
    manualNodePositionsRef.current = {};
    setSelectedEdgeId(null);
    setSelectedKey(workspaceGraph.root_key);
  }

  function resetGraphCachesForScopeChange() {
    setWorkspaceGraph(null);
    setAllKnowledgeGraph(null);
    setStableGraph(null);
    setGraph(null);
    setSelectedDetail(null);
    setSelectedEdgeId(null);
    nodePositionsRef.current.clear();
    manualNodePositionsRef.current = {};
    setLoading(true);
  }

  async function showPortfolioGraph() {
    if (workspaceGraph) {
      restoreWorkspaceGraph();
      return;
    }
    setLoading(true);
    try {
      const items = await loadSeeds();
      const nextGraph = await buildWorkspaceGraph(items);
      if (!nextGraph) {
        setError(humanizeGraphError("graph_node_not_found"));
        return;
      }
      setWorkspaceGraph(nextGraph);
      setGraph(nextGraph);
      setStableGraph(nextGraph);
      setGraphMode("portfolio");
      viewTransformRef.current = { x: 0, y: 0, k: 1 };
      setViewTransform({ x: 0, y: 0, k: 1 });
      nodePositionsRef.current.clear();
      manualNodePositionsRef.current = {};
      setSelectedKey(nextGraph.root_key);
      setSelectedEdgeId(null);
    } catch (err) {
      setError(humanizeGraphError(err));
    } finally {
      setLoading(false);
    }
  }

  async function showAllKnowledgeGraph() {
    setLoading(true);
    setError(null);
    setGraphNotice(null);
    try {
      const nextGraph = allKnowledgeGraph ?? await buildAllKnowledgeGraph();
      if (!nextGraph) {
        setError(humanizeGraphError("graph_node_not_found"));
        return;
      }
      if (!allKnowledgeGraph) {
        setAllKnowledgeGraph(nextGraph);
      }
      setGraph(nextGraph);
      setStableGraph(nextGraph);
      setGraphMode("all");
      viewTransformRef.current = { x: 0, y: 0, k: 1 };
      setViewTransform({ x: 0, y: 0, k: 1 });
      nodePositionsRef.current.clear();
      manualNodePositionsRef.current = {};
      setSelectedKey(nextGraph.root_key);
      setSelectedEdgeId(null);
    } catch (err) {
      setError(humanizeGraphError(err));
    } finally {
      setLoading(false);
    }
  }

  async function startGraphAsk(
    prompt: string,
    options?: {
      targetLabel?: string;
      subjectId?: string;
      subjectType?: string;
      context?: "node" | "edge";
    },
  ) {
    setGraphAskError(null);
    setGraphAskResult(null);
    setGraphAskTargetLabel(options?.targetLabel ?? null);
    setGraphAskContext(options?.context ?? null);
    const finalPrompt = graphAskUserPrompt.trim()
      ? `${prompt}\n\nAdditional user guidance for this analysis:\n${graphAskUserPrompt.trim()}`
      : prompt;
    try {
      const job = await apiFetch<AgentTurnJob>("/agent/turn-jobs", {
        method: "POST",
        body: JSON.stringify({
          subject_id: options?.subjectId,
          subject_type: options?.subjectType,
          message: finalPrompt,
          auto_execute: false,
        }),
      });
      setGraphAskJob(job);
    } catch (err) {
      setGraphAskError(err instanceof Error ? err.message : "Unable to start graph analysis.");
    }
  }

  const visibleCitations = selectedDetail?.citations.filter((citation) => !citation.is_system) ?? [];
  const systemCitations = selectedDetail?.citations.filter((citation) => citation.is_system) ?? [];
  const graphAskPanel =
    graphAskJob || graphAskResult || graphAskError ? (
      <div className="mt-4 rounded-lg border border-amber-200 bg-amber-50/70 p-4 dark:border-amber-900 dark:bg-amber-950/25">
        <div className="flex items-center justify-between gap-3">
          <div>
            <div className="text-[11px] uppercase tracking-wider text-amber-500 dark:text-amber-300">
              Analysis
            </div>
            {graphAskTargetLabel ? (
              <div className="mt-1 text-sm text-slate-700 dark:text-slate-200">
                Analyzing {graphAskTargetLabel}
              </div>
            ) : null}
          </div>
          {graphAskJob ? (
            <span className="rounded-full border border-amber-300 px-3 py-1 text-[11px] font-semibold uppercase tracking-wider text-amber-700 dark:border-amber-800 dark:text-amber-300">
              {graphAskJob.status}
            </span>
          ) : null}
        </div>
        {graphAskJob && ["queued", "running"].includes(graphAskJob.status) ? (
          <div className="mt-3 space-y-2">
            {graphAskJob.events.slice(-4).map((event) => (
              <div key={`${event.phase}-${event.created_at}`} className="rounded-lg border border-amber-100 bg-white/70 px-3 py-2 text-sm text-slate-600 dark:border-amber-950 dark:bg-slate-950/40 dark:text-slate-300">
                <span className="font-medium text-slate-900 dark:text-slate-100">{event.phase}</span>
                {" · "}
                {event.message}
              </div>
            ))}
          </div>
        ) : null}
        {graphAskError ? (
          <div className="mt-3 rounded-lg border border-red-200 bg-red-50/70 px-3 py-2 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/20 dark:text-red-300">
            {graphAskError}
          </div>
        ) : null}
        {graphAskResult ? (
          <div className="mt-3 rounded-lg border border-amber-100 bg-white/75 px-3 py-3 dark:border-amber-950 dark:bg-slate-950/40">
            <div className="whitespace-pre-wrap text-sm leading-relaxed text-slate-800 dark:text-slate-100">
              {graphAskResult.assistant_message}
            </div>
            {graphAskResult.session_id ? (
              <div className="mt-3 flex items-center justify-between gap-3">
                <div className="text-xs text-slate-500 dark:text-slate-400">
                  Want to keep going in a longer thread?
                </div>
                <Link
                  href={`/chat?session_id=${graphAskResult.session_id}`}
                  className="inline-flex items-center justify-center rounded-full border border-slate-300 px-3 py-1.5 text-xs text-slate-600 hover:border-slate-400 dark:border-slate-700 dark:text-slate-300"
                >
                  Open in chat
                </Link>
              </div>
            ) : null}
          </div>
        ) : null}
      </div>
    ) : null;

  return (
    <div className="min-h-screen bg-background text-foreground font-sans">
      <AppNav active="knowledge" />

      <main className="mx-auto grid w-full max-w-[1920px] grid-cols-1 gap-5 px-3 py-5 sm:gap-8 sm:px-6 sm:py-8 lg:px-8 2xl:grid-cols-[minmax(0,1fr)_360px]">
        <section className="min-w-0 space-y-4 sm:space-y-6">
          <header className="flex flex-col items-start justify-between gap-3 sm:flex-row sm:gap-6">
            <div className="min-w-0">
              <h1 className="text-3xl font-bold tracking-tight">Knowledge Web</h1>
              <div className="mt-3 inline-flex rounded-full border border-slate-200 bg-white p-1 dark:border-slate-800 dark:bg-slate-950">
                <button
                  type="button"
                  onClick={() => void showPortfolioGraph()}
                  disabled={loading}
                  className={[
                    "rounded-full px-3 py-1 text-xs font-semibold transition-colors disabled:opacity-50",
                    graphMode === "portfolio"
                      ? "bg-slate-900 text-white dark:bg-slate-100 dark:text-slate-950"
                      : "text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-900",
                  ].join(" ")}
                >
                  Portfolio Web
                </button>
                <button
                  type="button"
                  onClick={() => void showAllKnowledgeGraph()}
                  disabled={loading}
                  className={[
                    "rounded-full px-3 py-1 text-xs font-semibold transition-colors disabled:opacity-50",
                    graphMode === "all"
                      ? "bg-slate-900 text-white dark:bg-slate-100 dark:text-slate-950"
                      : "text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-900",
                  ].join(" ")}
                >
                  Expanded Web
                </button>
              </div>
            </div>
            <div className="flex max-w-full flex-wrap items-center gap-2">
              {selectedDetail && selectedKey !== activeRootKey ? (
                <button
                  type="button"
                  onClick={() => void openGraph(selectedDetail.node_type, selectedDetail.id)}
                  className="rounded-full border border-sky-200 bg-sky-50 px-4 py-2 text-sm text-sky-700 hover:border-sky-300 dark:border-sky-800/40 dark:bg-sky-900/20 dark:text-sky-300"
                >
                  Focus on node
                </button>
              ) : null}
              {graph && workspaceGraph && graph.root_key !== workspaceGraph.root_key ? (
                <button
                  type="button"
                  onClick={() => restoreWorkspaceGraph()}
                  disabled={!workspaceGraph}
                  className="rounded-full border border-slate-300 px-4 py-2 text-sm text-slate-600 disabled:opacity-50 dark:border-slate-700 dark:text-slate-300"
                >
                  Back to overview
                </button>
              ) : null}
            </div>
          </header>

          <section className="min-w-0 rounded-lg border border-slate-200 bg-white p-3 sm:p-6 dark:border-slate-800 dark:bg-slate-950">
            {graphNotice ? (
              <div className="mb-4 rounded-lg border border-amber-200 bg-amber-50/80 px-4 py-3 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950/25 dark:text-amber-200">
                {graphNotice}
              </div>
            ) : null}
            <div className="flex flex-wrap items-center justify-between gap-4">
              <div className="flex min-w-0 flex-wrap items-center gap-2 text-xs font-medium text-slate-500 dark:text-slate-400">
                <span className="rounded-full bg-slate-100 px-3 py-1 dark:bg-slate-900">
                  {visibleNodeCount} nodes
                </span>
                <span className="rounded-full bg-slate-100 px-3 py-1 dark:bg-slate-900">
                  {visibleEdgeCount} connections
                </span>
                <span className="rounded-full bg-slate-100 px-3 py-1 dark:bg-slate-900">
                  {modeLabel}
                </span>
                {hiddenNodeCount > 0 || hiddenEdgeCount > 0 ? (
                  <span className="rounded-full bg-amber-50 px-3 py-1 text-amber-700 dark:bg-amber-950/25 dark:text-amber-300">
                    {hiddenNodeCount} nodes / {hiddenEdgeCount} links hidden
                  </span>
                ) : null}
                {graphMode === "focused" ? (
                  <span className="rounded-full bg-sky-50 px-3 py-1 text-sky-700 dark:bg-sky-900/20 dark:text-sky-300">
                    Focused view
                  </span>
                ) : null}
              </div>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => {
                    const anchor = viewportAnchor();
                    adjustZoom(viewTransformRef.current.k * 0.85, anchor.x, anchor.y);
                  }}
                  className="rounded-full hover:bg-slate-100 p-2 text-slate-400 hover:text-slate-600 dark:hover:bg-slate-800"
                  aria-label="Zoom out"
                  title="Zoom out"
                >
                  <ZoomOut className="h-4 w-4" aria-hidden="true" />
                </button>
                <button
                  type="button"
                  onClick={() => fitNodesToViewport(displayedNodes)}
                  className="rounded-full border border-slate-200 px-3 py-1 text-xs font-semibold text-slate-600 dark:border-slate-800 dark:text-slate-300"
                  title="Fit graph"
                >
                  <span className="inline-flex items-center gap-1.5">
                    <Maximize2 className="h-3.5 w-3.5" aria-hidden="true" />
                    Fit graph
                  </span>
                </button>
                <button
                  type="button"
                  onClick={() => {
                    viewTransformRef.current = { x: 0, y: 0, k: 1 };
                    setViewTransform({ x: 0, y: 0, k: 1 });
                    nodePositionsRef.current.clear();
                    manualNodePositionsRef.current = {};
                    setLayoutNonce((current) => current + 1);
                  }}
                  className="rounded-full border border-slate-200 px-3 py-1 text-xs font-semibold text-slate-600 dark:border-slate-800 dark:text-slate-300"
                  title="Reset layout"
                >
                  <span className="inline-flex items-center gap-1.5">
                    <RotateCcw className="h-3.5 w-3.5" aria-hidden="true" />
                    Reset layout
                  </span>
                </button>
                <button
                  type="button"
                  onClick={() => {
                    const anchor = viewportAnchor();
                    adjustZoom(viewTransformRef.current.k * 1.15, anchor.x, anchor.y);
                  }}
                  className="rounded-full hover:bg-slate-100 p-2 text-slate-400 hover:text-slate-600 dark:hover:bg-slate-800"
                  aria-label="Zoom in"
                  title="Zoom in"
                >
                  <ZoomIn className="h-4 w-4" aria-hidden="true" />
                </button>
              </div>
            </div>

            <details className="mt-4 border-t border-slate-200 pt-3 dark:border-slate-800">
              <summary className="cursor-pointer text-xs font-semibold text-slate-600 dark:text-slate-300">
                Filters{activeNodeTypes.length > 0 ? ` · ${activeNodeTypes.length} active` : ""}
              </summary>
              <div className="mt-3 flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  onClick={() => setActiveNodeTypes([])}
                  className={[
                    "rounded-full border px-3 py-1 text-[11px] font-semibold transition-colors",
                    activeNodeTypes.length === 0
                      ? "border-sky-300 bg-sky-50 text-sky-700 dark:border-sky-700 dark:bg-sky-950/30 dark:text-sky-300"
                      : "border-slate-200 text-slate-600 hover:border-slate-300 dark:border-slate-800 dark:text-slate-300",
                  ].join(" ")}
                >
                  All types
                </button>
                {availableNodeTypes.map(({ type, count }) => {
                  const active = activeNodeTypes.length === 0 || activeNodeTypes.includes(type);
                  return (
                    <button
                      key={type}
                      type="button"
                      onClick={() => toggleNodeTypeFilter(type)}
                      className={[
                        "inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-[11px] font-semibold transition-colors",
                        active
                          ? "border-sky-300 bg-sky-50 text-sky-700 dark:border-sky-700 dark:bg-sky-950/30 dark:text-sky-300"
                          : "border-slate-200 text-slate-500 hover:border-slate-300 dark:border-slate-800 dark:text-slate-400",
                      ].join(" ")}
                    >
                      <span
                        className="h-2 w-2 rounded-full"
                        style={{ backgroundColor: getNodeStyle(type).fill }}
                      />
                      <span>{nodeTypeLabel(type)} · {count}</span>
                    </button>
                  );
                })}
              </div>
              <div className="mt-3 flex flex-wrap items-center gap-3 text-xs text-slate-500 dark:text-slate-400">
                <label className="inline-flex items-center gap-2 rounded-full border border-slate-200 bg-white px-3 py-1.5 dark:border-slate-800 dark:bg-slate-950">
                  <input
                    type="checkbox"
                    checked={showDuplicateNodes}
                    onChange={(event) => setShowDuplicateNodes(event.target.checked)}
                    className="h-3.5 w-3.5 rounded border-slate-300 text-sky-600 focus:ring-sky-500"
                  />
                  <span>Show duplicate labels</span>
                </label>
                <label className="inline-flex items-center gap-2 rounded-full border border-slate-200 bg-white px-3 py-1.5 dark:border-slate-800 dark:bg-slate-950">
                  <input
                    type="checkbox"
                    checked={showSystemNodes}
                    onChange={(event) => {
                      resetGraphCachesForScopeChange();
                      setShowSystemNodes(event.target.checked);
                    }}
                    className="h-3.5 w-3.5 rounded border-slate-300 text-sky-600 focus:ring-sky-500"
                  />
                  <span>Include internal memory</span>
                </label>
                <span>
                  Portfolio Web is scoped to active holdings. Use Expanded Web or search to open broader neighborhoods.
                </span>
              </div>
            </details>

            {graphStats ? (
              <details className="mt-4 border-t border-slate-200 pt-4 dark:border-slate-800">
                <summary className="cursor-pointer text-xs font-semibold text-slate-600 dark:text-slate-300">
                  Knowledge inventory
                </summary>
                <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-3 lg:grid-cols-6">
                  <InventoryMetric label="Active knowledge" value={graphStats.active_knowledge_nodes} />
                  <InventoryMetric label="Evidence" value={graphStats.raw_evidence} />
                  <InventoryMetric label="Source items" value={graphStats.source_items} />
                  <InventoryMetric label="Metrics" value={graphStats.fundamental_metrics} />
                  <InventoryMetric label="Market setups" value={graphStats.market_setup_signals} />
                  <InventoryMetric label="Stored links" value={graphStats.total_edges} />
                </dl>
                <p className="mt-3 text-xs leading-5 text-slate-500 dark:text-slate-400">
                  The canvas shows a bounded neighborhood of the stored knowledge base, so the visible node count is intentionally smaller than the inventory.
                </p>
              </details>
            ) : null}

            <div className="relative mt-4 min-w-0 overflow-hidden rounded-lg border border-slate-200 bg-[radial-gradient(circle_at_center,rgba(14,165,233,0.1),transparent_48%),linear-gradient(180deg,rgba(255,255,255,0.98),rgba(241,245,249,0.94))] sm:mt-6 dark:border-slate-800 dark:bg-[radial-gradient(circle_at_center,rgba(14,165,233,0.14),transparent_48%),linear-gradient(180deg,rgba(8,15,22,0.98),rgba(5,8,13,0.98))]">
              {error && !activeGraph ? (
                <div className="absolute inset-0 flex items-center justify-center bg-slate-50/10 backdrop-blur-[2px] z-10">
                  <div className="max-w-md p-6 bg-white rounded-lg border border-rose-100 shadow-xl text-center">
                    <X className="w-10 h-10 text-rose-500 mx-auto mb-4" />
                    <p className="text-sm font-bold text-slate-900 mb-2">Knowledge Web is temporarily unavailable</p>
                    <p className="text-xs text-rose-500 bg-rose-50 p-3 rounded-lg break-words">
                      {error}
                    </p>
                    <button
                      onClick={() => {
                        setError(null);
                        setGraphNotice(null);
                        void loadSeeds().then((s) => s && openGraph(s[0].subject_type, s[0].subject_id));
                      }}
                      className="mt-6 px-4 py-2 bg-sky-600 text-white rounded-lg text-xs font-bold hover:bg-sky-700 transition-all font-mono uppercase tracking-widest"
                    >
                      Reconnect
                    </button>
                  </div>
                </div>
              ) : !activeGraph ? (
                <div className="flex h-[72dvh] min-h-[460px] max-h-[720px] flex-col items-center justify-center px-6 text-center sm:h-[calc(100dvh-11rem)] sm:min-h-[620px] sm:max-h-[980px] sm:px-8">
                  {error && error.includes("graph_node_not_found") ? (
                    <>
                      <svg className="h-12 w-12 text-slate-300 dark:text-slate-700 mb-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1} d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1" /></svg>
                      <h3 className="text-lg font-semibold text-slate-900 dark:text-slate-100">Knowledge Web is initializing</h3>
                      <p className="mt-1 text-sm text-slate-500 dark:text-slate-400 max-w-md">
                        There are currently no nodes in the graph to display. The system will populate connections once it ingests your portfolio, notes, or external intelligence.
                      </p>
                    </>
                  ) : (
                    <div className="text-sm text-red-500">{error || "Building connected web..."}</div>
                  )}
                </div>
              ) : (
                <div
                  ref={graphViewportRef}
                  data-pull-refresh-ignore
                  className="relative h-[72dvh] min-h-[460px] max-h-[720px] w-full touch-pan-y select-none overflow-hidden rounded-lg border border-white/5 bg-slate-950/20 sm:h-[calc(100dvh-11rem)] sm:min-h-[620px] sm:max-h-[980px] sm:touch-none"
                  style={{ overscrollBehavior: "contain" }}
                  onPointerDown={handleCanvasPointerDown}
                  onPointerMove={handleCanvasPointerMove}
                  onPointerUp={handleCanvasPointerUp}
                  onPointerCancel={handleCanvasPointerUp}
                >
                  <div className="pointer-events-none absolute left-3 top-3 z-10 rounded border border-white/10 bg-slate-950/72 px-2.5 py-1.5 text-[10px] text-slate-300 backdrop-blur-sm">
                    Pinch or scroll to zoom · tap a node for its full label
                  </div>
                  {loading && (
                    <div className="absolute inset-0 flex items-center justify-center bg-slate-900/40 backdrop-blur-[2px] z-20 pointer-events-none">
                      <div className="flex flex-col items-center gap-4">
                        <div className="w-8 h-8 border-4 border-sky-500/30 border-t-sky-500 rounded-full animate-spin"></div>
                        <p className="text-xs font-bold text-sky-100 tracking-widest uppercase animate-pulse">Syncing Map...</p>
                      </div>
                    </div>
                  )}
                  <svg
                    ref={svgRef}
                    viewBox={`${graphViewBox.x} ${graphViewBox.y} ${graphViewBox.width} ${graphViewBox.height}`}
                    className="h-full w-full touch-pan-y sm:touch-none"
                  >
                    <defs>
                      <filter id="graph-glow" x="-50%" y="-50%" width="200%" height="200%">
                        <feGaussianBlur stdDeviation="8" result="blur" />
                        <feMerge>
                          <feMergeNode in="blur" />
                          <feMergeNode in="SourceGraphic" />
                        </feMerge>
                      </filter>
                    </defs>
                    <g ref={containerRef}>
                      {displayedEdges.map((edge) => {
	                        const source = nodeByKey[edge.source_key];
	                        const target = nodeByKey[edge.target_key];
	                        if (!source || !target) return null;
	                        const edgeKey = graphEdgeIdentity(edge);
	                        const hovered = hoveredEdgeId === edgeKey;
	                        const highlighted = Boolean(
	                          hovered || (selectedKey && (edge.source_key === selectedKey || edge.target_key === selectedKey)),
	                        );
	                        const selected = selectedEdgeId === edgeKey;
	                        return (
	                          <g key={edgeKey}>
                            <title>{describeEdge(edge)}</title>
                            <line
                              className={[
                                "graph-edge-visible",
                                selected ? "text-amber-400" : highlighted ? "text-sky-400" : "text-slate-700"
                              ].join(" ")}
                              x1={source.x ?? 0} y1={source.y ?? 0}
                              x2={target.x ?? 0} y2={target.y ?? 0}
                              stroke="currentColor"
                              strokeWidth={selected ? 4 : highlighted ? 2.8 : 1.35}
                              strokeOpacity={selected ? 1 : highlighted ? 0.88 : 0.28}
                            />
                            <line
                              className="graph-edge-hit cursor-pointer"
	                              x1={source.x ?? 0} y1={source.y ?? 0}
	                              x2={target.x ?? 0} y2={target.y ?? 0}
	                              stroke="transparent" strokeWidth={15}
	                              onMouseEnter={() => setHoveredEdgeId(edgeKey)}
	                              onMouseLeave={() => setHoveredEdgeId((current) => current === edgeKey ? null : current)}
	                              onClick={(e) => {
                                  e.stopPropagation();
                                  setHoveredEdgeId(null);
                                  setSelectedKey(null);
                                  setSelectedEdgeId(edgeKey);
                                }}
	                            />
                          </g>
                        );
                      })}

                      {displayedNodes.map((node: PositionedNode) => {
                        const isSelected = selectedKey === node.key;
                        const isHovered = hoveredNodeKey === node.key;
                        const isConnected = selectedKey ? connectedKeys.has(node.key) : node.is_root;
                        const isCompareNode = false;
                        const radius = nodeVisualRadius(node, {
                          selected: isSelected,
                          compare: isCompareNode,
                        });
                        const style = getNodeStyle(node.node_type);
                        const labelVisible =
                          (radius >= 20 || isSelected || isHovered || node.is_root) &&
                          shouldShowNodeLabel(node, {
                            selected: isSelected,
                            connected: Boolean(isConnected),
                            hovered: isHovered,
                            zoom: viewTransform.k,
                          });
                        const labelLines = labelVisible ? compactNodeLabel(node.label, radius) : [];
                        const fontSize = radius >= 46 ? 8.25 : radius >= 38 ? 7.5 : 6.75;
                        const clipId = `node-clip-${svgSafeId(node.key)}`;
                        return (
                          <g
                            key={node.key}
                            data-node-key={node.key}
                            onPointerDown={(event) => handleNodePointerDown(event, node)}
                            onMouseEnter={() => setHoveredNodeKey(node.key)}
                            onMouseLeave={() => setHoveredNodeKey((current) => current === node.key ? null : current)}
                            className="graph-node cursor-pointer"
                            transform={`translate(${node.x ?? 0}, ${node.y ?? 0})`}
                          >
                            <title>{node.label}</title>
                            <circle
                              cx={0}
                              cy={0}
                              r={radius}
                              filter={isSelected || isHovered || node.is_autonomous ? "url(#graph-glow)" : undefined}
                              fill={style.fill}
                              fillOpacity={
                                isSelected
                                  ? 0.98
                                  : isHovered
                                    ? 0.94
                                    : isConnected || node.is_root
                                    ? 0.9
                                    : selectedKey
                                      ? 0.5
                                      : 0.74
                              }
                              stroke={node.is_autonomous ? "#38bdf8" : style.stroke}
                              strokeWidth={node.is_root || isSelected || isHovered ? 2.8 : node.is_autonomous ? 2.5 : 2}
                              strokeDasharray={node.is_autonomous ? "4 2" : undefined}
                            />
                            {isSelected ? (
                              <circle
                                cx={0}
                                cy={0}
                                r={radius + 6}
                                fill="none"
                                stroke={style.stroke}
                                strokeOpacity={0.95}
                                strokeWidth={2}
                              />
                            ) : null}
                            {labelVisible ? (
                              <>
                                <defs>
                                  <clipPath id={clipId}>
                                    <circle cx={0} cy={0} r={Math.max(12, radius - 4)} />
                                  </clipPath>
                                </defs>
                                <g clipPath={`url(#${clipId})`}>
                                  {labelLines.map((line: string, index: number) => {
                                    const lineHeight = 12;
                                    const totalHeight = labelLines.length * lineHeight;
                                    const yOffset = (index * lineHeight) - (totalHeight / 2) + (lineHeight / 2);
                                    return (
                                      <text
                                        key={`${node.key}-${index}`}
                                        x={0}
                                        y={yOffset}
                                        textAnchor="middle"
                                        dominantBaseline="middle"
                                        className="pointer-events-none font-bold"
                                        fontSize={fontSize}
                                        fill={style.text}
                                        stroke={style.fill === "#111827" ? "rgba(255,255,255,0.45)" : "rgba(15,23,42,0.25)"}
                                        strokeWidth={3}
                                        paintOrder="stroke fill"
                                      >
                                        {line}
                                      </text>
                                    );
                                  })}
                                </g>
                              </>
                            ) : null}
                          </g>
                        );
                      })}
                    </g>
                  </svg>
                  {hoverPreview ? (
                    <div
                      className="pointer-events-none absolute z-30 w-[min(300px,calc(100%-1.5rem))] rounded-lg border border-white/15 bg-slate-950/88 px-3 py-2 text-white shadow-lg backdrop-blur-md"
                      style={{
                        left: hoverPreview.left,
                        top: hoverPreview.top,
                        transform: "translate(-50%, calc(-100% - 14px))",
                      }}
                    >
                      <div className="text-[10px] font-semibold uppercase tracking-wider text-sky-200">
                        {hoverPreview.eyebrow}
                      </div>
                      <div className="mt-1 break-words text-xs font-semibold leading-snug [overflow-wrap:anywhere]">
                        {hoverPreview.title}
                      </div>
                      {hoverPreview.detail ? (
                        <div className="mt-1 break-words text-[11px] leading-snug text-slate-300 [overflow-wrap:anywhere]">
                          {hoverPreview.detail}
                        </div>
                      ) : null}
                    </div>
                  ) : null}
                  {activeEdge ? (
                    <div className="absolute inset-x-3 bottom-3 z-30 flex min-w-0 items-start justify-between gap-3 rounded-lg border border-white/15 bg-slate-950/92 px-3 py-3 text-white shadow-xl backdrop-blur-md sm:inset-x-auto sm:left-4 sm:max-w-[520px]">
                      <div className="min-w-0">
                        <p className="text-[10px] font-semibold uppercase text-amber-200">
                          Connection · {Math.round(activeEdge.confidence * 100)}% confidence
                        </p>
                        <p className="mt-1 break-words text-sm font-semibold leading-5 [overflow-wrap:anywhere]">
                          {describeEdge(activeEdge)}
                        </p>
                      </div>
                      <button
                        type="button"
                        onClick={(event) => {
                          event.stopPropagation();
                          document.getElementById("knowledge-connection-detail")?.scrollIntoView({
                            behavior: "smooth",
                            block: "start",
                          });
                        }}
                        className="shrink-0 rounded border border-amber-300/50 px-2.5 py-1.5 text-[11px] font-semibold text-amber-100 hover:border-amber-200 hover:text-white"
                      >
                        Details
                      </button>
                    </div>
                  ) : selectedNode && selectedKey !== activeRootKey ? (
                    <div className="absolute inset-x-3 bottom-3 z-30 flex min-w-0 items-start justify-between gap-3 rounded-lg border border-white/15 bg-slate-950/92 px-3 py-3 text-white shadow-xl backdrop-blur-md sm:inset-x-auto sm:left-4 sm:max-w-[420px]">
                      <div className="min-w-0">
                        <p className="text-[10px] font-semibold uppercase text-sky-200">
                          {nodeTypeLabel(selectedNode.node_type)} · {layerLabel(selectedNode.layer)}
                        </p>
                        <p className="mt-1 break-words text-sm font-semibold leading-5 [overflow-wrap:anywhere]">
                          {selectedNode.label}
                        </p>
                        {selectedNode.subtitle ? (
                          <p className="mt-1 break-words text-xs leading-4 text-slate-300 [overflow-wrap:anywhere]">
                            {selectedNode.subtitle}
                          </p>
                        ) : null}
                      </div>
                      <button
                        type="button"
                        onClick={(event) => {
                          event.stopPropagation();
                          document.getElementById("knowledge-node-detail")?.scrollIntoView({
                            behavior: "smooth",
                            block: "start",
                          });
                        }}
                        className="shrink-0 rounded border border-sky-300/50 px-2.5 py-1.5 text-[11px] font-semibold text-sky-100 hover:border-sky-200 hover:text-white"
                      >
                        Details
                      </button>
                    </div>
                  ) : null}
                </div>
              )}
            </div>
          </section>

        </section>

        <aside className="min-w-0 space-y-6 self-start 2xl:sticky 2xl:top-24 2xl:max-h-[calc(100vh-7rem)] 2xl:overflow-y-auto 2xl:pr-2">
          {activeEdge ? (
            <section id="knowledge-connection-detail" className="min-w-0 scroll-mt-24 rounded-lg border border-slate-200 bg-white p-4 sm:p-6 dark:border-slate-800 dark:bg-slate-950">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <h2 className="text-sm font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400">Connection detail</h2>
                  <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                    Relationship meaning is shown here instead of spraying labels across the graph.
                  </p>
                </div>
                <button
                  type="button"
                  onClick={() => setSelectedEdgeId(null)}
                  className="rounded-full border border-slate-300 px-3 py-1 text-xs text-slate-600 dark:border-slate-700 dark:text-slate-300"
                >
                  Clear
                </button>
              </div>
              <div className="mt-5 space-y-3">
                <div className="rounded-lg border border-amber-200 bg-amber-50/70 p-4 dark:border-amber-900 dark:bg-amber-950/20">
                  <div className="text-[11px] uppercase tracking-wider text-amber-500 dark:text-amber-300">
                    {activeEdge.relationship_type.replaceAll("_", " ")}
                  </div>
                  <div className="mt-2 text-sm font-semibold text-slate-900 dark:text-slate-100">
                    {describeEdge(activeEdge)}
                  </div>
                  <div className="mt-2 text-xs text-slate-500 dark:text-slate-400">
                    Confidence {Math.round(activeEdge.confidence * 100)}%
                  </div>
                </div>
                <div className="grid grid-cols-1 gap-2">
                  <div className="rounded-lg border border-slate-200 px-3 py-2 dark:border-slate-800">
                    <div className="text-[10px] uppercase tracking-wider text-slate-400">Source node</div>
                    <div className="mt-0.5 text-xs text-slate-800 dark:text-slate-200">
                      {nodeByKey[activeEdge.source_key]?.label ?? activeEdge.source_key}
                    </div>
                  </div>
                  <div className="rounded-lg border border-slate-200 px-3 py-2 dark:border-slate-800">
                    <div className="text-[10px] uppercase tracking-wider text-slate-400">Target node</div>
                    <div className="mt-0.5 text-xs text-slate-800 dark:text-slate-200">
                      {nodeByKey[activeEdge.target_key]?.label ?? activeEdge.target_key}
                    </div>
                  </div>
                </div>
                <button
                  type="button"
                  onClick={() => {
                    void startGraphAsk(makeEdgeAskPrompt(activeEdge, nodeByKey), {
                      targetLabel: describeEdge(activeEdge),
                      context: "edge",
                    });
                  }}
                  className="inline-flex items-center justify-center rounded-full border border-sky-300 px-4 py-2 text-xs font-medium text-sky-700 hover:border-sky-400 dark:border-sky-800 dark:text-sky-300"
                >
                  Analyze this connection
                </button>
                {graphAskContext === "edge" ? graphAskPanel : null}
              </div>
            </section>
          ) : null}

          <section id="knowledge-node-detail" className="min-w-0 scroll-mt-24 rounded-lg border border-slate-200 bg-white p-4 sm:p-6 dark:border-slate-800 dark:bg-slate-950">
            <div className="flex items-center justify-between gap-3">
              <div>
                <h2 className="text-sm font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400">Node detail</h2>
                <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">Inspect the selected node, its evidence, and why it belongs in the system.</p>
              </div>
              {selectedDetail ? (
                <button
                  type="button"
                  onClick={() =>
                    void startGraphAsk(makeNodeAskPrompt(selectedDetail), {
                      targetLabel: selectedDetail.label,
                      subjectId: ["entity", "theme"].includes(selectedDetail.node_type)
                        ? selectedDetail.id
                        : undefined,
                      subjectType: ["entity", "theme"].includes(selectedDetail.node_type)
                        ? selectedDetail.node_type
                        : undefined,
                      context: "node",
                    })
                  }
                  className="rounded-full border border-amber-300 px-3 py-1 text-[10px] text-amber-700 hover:border-amber-400 dark:border-amber-800 dark:text-amber-300"
                >
                  Analyze node
                </button>
              ) : null}
            </div>

            {detailLoading ? (
              <div className="mt-5 rounded-lg border border-slate-200 p-4 text-xs text-slate-500 animate-pulse dark:border-slate-800 dark:text-slate-400">
                Loading node detail...
              </div>
            ) : selectedDetail ? (
              <div className="mt-5 space-y-5">
                <div className="rounded-lg border border-slate-200 bg-slate-50 p-3 dark:border-slate-800 dark:bg-slate-900">
                  <label className="block text-[10px] font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400">
                    Guide this analysis
                  </label>
                  <p className="mt-1 text-[10px] text-slate-500 dark:text-slate-400">
                    Write the exact question you want answered about this node.
                  </p>
                  <textarea
                    value={graphAskUserPrompt}
                    onChange={(event) => setGraphAskUserPrompt(event.target.value)}
                    rows={2}
                    placeholder="Why does this matter to me?"
                    className="mt-2 w-full resize-y rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs text-slate-800 outline-none placeholder:text-slate-400 focus:border-amber-400 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-100"
                  />
                </div>
                {selectedKey === activeRootKey ? (
                  <button
                    type="button"
                    onClick={() =>
                      void startGraphAsk(makeNodeAskPrompt(selectedDetail), {
                        targetLabel: selectedDetail.label,
                        subjectId: ["entity", "theme"].includes(selectedDetail.node_type)
                          ? selectedDetail.id
                          : undefined,
                        subjectType: ["entity", "theme"].includes(selectedDetail.node_type)
                          ? selectedDetail.node_type
                          : undefined,
                        context: "node",
                      })
                    }
                    className="inline-flex items-center justify-center rounded-full border border-amber-300 px-4 py-2 text-xs font-medium text-amber-700 hover:border-amber-400 dark:border-amber-800 dark:text-amber-300"
                  >
                    Run node analysis
                  </button>
                ) : null}
                {graphAskContext === "node" ? graphAskPanel : null}
                <div className="min-w-0 rounded-lg border border-sky-200 bg-sky-50/70 p-4 dark:border-sky-900 dark:bg-sky-950/30">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="rounded-md border border-sky-300 bg-white/60 px-2 py-0.5 text-[11px] font-bold uppercase tracking-wider text-sky-700 dark:border-sky-700 dark:bg-sky-900/50 dark:text-sky-300">
                      {nodeTypeLabel(selectedDetail.node_type)}
                    </span>
                    {selectedDetail.tier ? (
                      <span className="rounded-md border border-amber-300 bg-amber-50 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider text-amber-700 dark:border-amber-900/60 dark:bg-amber-950/40 dark:text-amber-400">
                        {selectedDetail.tier.replaceAll("_", " ")}
                      </span>
                    ) : null}
                    <span className="rounded-md border border-slate-300 bg-white/40 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-slate-600 dark:border-slate-700 dark:bg-slate-800/40 dark:text-slate-400">
                      {layerLabel(selectedDetail.layer)}
                    </span>
                  </div>
                  <h3 className="mt-3 text-base font-semibold text-slate-900 dark:text-slate-100">{selectedDetail.label}</h3>
                  {selectedDetail.body && selectedDetail.body.trim() !== selectedDetail.label.trim() ? (
                    <div className="mt-3 bg-white px-4 py-3 dark:bg-[#1e1e1e] border-l-4 border-sky-500 rounded-r-lg">
                      <div className="mb-2 text-[10px] font-bold uppercase tracking-widest text-sky-500/80 dark:text-sky-400/80">Description & Reasoning</div>
                      <div className="prose prose-xs dark:prose-invert max-w-none prose-sky leading-relaxed break-words">
                        <ReactMarkdown
                          remarkPlugins={[remarkGfm]}
                          components={{
                            img: ({ alt, ...props }) => (
                              // eslint-disable-next-line @next/next/no-img-element
                              <img
                                {...props}
                                alt={alt ?? ""}
                                className="my-4 max-w-full rounded-lg border border-slate-200 dark:border-slate-800"
                                loading="lazy"
                              />
                            )
                          }}
                        >
                          {selectedDetail.body}
                        </ReactMarkdown>
                      </div>
                    </div>
                  ) : null}
                  {selectedDetail.properties?.horizon_reasoning ? (
                    <div className="mt-3 bg-amber-50/50 px-4 py-3 dark:bg-amber-950/10 border-l-4 border-amber-400 rounded-r-lg">
                      <div className="mb-1 text-[10px] font-bold uppercase tracking-widest text-amber-600/80 dark:text-amber-400/80">Horizon Awareness</div>
                      <div className="text-xs italic leading-relaxed text-slate-700 dark:text-slate-300">
                        {String(selectedDetail.properties.horizon_reasoning)}
                      </div>
                    </div>
                  ) : null}
                </div>

	                {/* Portfolio Significance Section */}
	                {selectedDetail.relevance_reasoning && (
	                  <div className="mb-6 mx-4 rounded-lg border border-teal-200 bg-teal-50 p-4 dark:border-teal-900/60 dark:bg-teal-950/25">
	                    <div className="flex flex-wrap items-center gap-2 mb-2">
                      <div className="h-2 w-2 rounded-full bg-teal-500 dark:bg-teal-400" />
                      <span className="text-[10px] font-bold uppercase tracking-wider text-teal-700 dark:text-teal-300">
                        Why This Is In Your Graph
                      </span>
                      {selectedDetail.properties?.portfolio_significance ? (
                        <span className="rounded-full border border-teal-300 bg-teal-100 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-teal-700 dark:border-teal-700 dark:bg-teal-900/50 dark:text-teal-200">
                          {String(selectedDetail.properties.portfolio_significance)}
                        </span>
                      ) : null}
                    </div>
	                    <p className="text-sm leading-relaxed text-teal-950 dark:text-teal-100">
	                      {selectedDetail.relevance_reasoning}
	                    </p>
	                    {propertyText(selectedDetail.properties?.portfolio_mechanism) ? (
	                      <div className="mt-4 rounded-lg border border-teal-200 bg-white/65 px-3 py-2 dark:border-teal-800/70 dark:bg-teal-950/30">
	                        <div className="mb-1 text-[10px] font-bold uppercase tracking-wider text-teal-700 dark:text-teal-300/90">
	                          Portfolio Mechanism
	                        </div>
	                        <p className="text-xs leading-relaxed text-teal-950 dark:text-teal-100">
	                          {propertyText(selectedDetail.properties?.portfolio_mechanism)}
	                        </p>
	                      </div>
	                    ) : null}
	                    {propertyList(selectedDetail.properties?.affected_holdings).length > 0 ? (
	                      <div className="mt-3">
	                        <div className="mb-1 text-[10px] font-bold uppercase tracking-wider text-teal-700 dark:text-teal-300/90">
	                          Affected Holdings
	                        </div>
	                        <div className="flex flex-wrap gap-2">
	                          {propertyList(selectedDetail.properties?.affected_holdings).map((holding) => (
	                            <span
	                              key={`affected-${holding}`}
	                              className="rounded-full border border-teal-300 bg-white/70 px-2 py-1 text-[10px] font-semibold tracking-wide text-teal-700 dark:border-teal-400/30 dark:bg-teal-500/10 dark:text-teal-100"
	                            >
	                              {holding}
	                            </span>
	                          ))}
	                        </div>
	                      </div>
	                    ) : null}
	                    {propertyText(selectedDetail.properties?.next_test) ? (
	                      <div className="mt-3 rounded-lg border border-teal-200 bg-white/65 px-3 py-2 dark:border-teal-800/70 dark:bg-teal-950/30">
	                        <div className="mb-1 text-[10px] font-bold uppercase tracking-wider text-teal-700 dark:text-teal-300/90">
	                          Next Useful Test
	                        </div>
	                        <p className="text-xs leading-relaxed text-teal-950 dark:text-teal-100">
	                          {propertyText(selectedDetail.properties?.next_test)}
	                        </p>
	                      </div>
	                    ) : null}
	                    {Array.isArray(selectedDetail.properties?.linked_holdings) &&
	                    selectedDetail.properties.linked_holdings.length > 0 ? (
	                      <div className="mt-3">
                        <div className="mb-1 text-[10px] font-bold uppercase tracking-wider text-teal-700 dark:text-teal-300/90">
                          Linked Holdings
                        </div>
                        <div className="flex flex-wrap gap-2">
                          {selectedDetail.properties.linked_holdings.map((holding) => (
                            <span
                              key={`holding-${String(holding)}`}
                              className="rounded-full border border-sky-300 bg-sky-100 px-2 py-1 text-[10px] font-semibold tracking-wide text-sky-700 dark:border-sky-400/30 dark:bg-sky-500/10 dark:text-sky-200"
                            >
                              {String(holding)}
                            </span>
                          ))}
                        </div>
                      </div>
                    ) : null}
                    {Array.isArray(selectedDetail.properties?.linked_companies) &&
                    selectedDetail.properties.linked_companies.length > 0 ? (
                      <div className="mt-3">
                        <div className="mb-1 text-[10px] font-bold uppercase tracking-wider text-teal-700 dark:text-teal-300/90">
                          Linked Companies
                        </div>
                        <div className="flex flex-wrap gap-2">
                          {selectedDetail.properties.linked_companies.map((company) => (
                            <span
                              key={`company-${String(company)}`}
                              className="rounded-full border border-emerald-300 bg-emerald-100 px-2 py-1 text-[10px] font-semibold tracking-wide text-emerald-700 dark:border-emerald-400/30 dark:bg-emerald-500/10 dark:text-emerald-200"
                            >
                              {String(company)}
                            </span>
                          ))}
                        </div>
                      </div>
                    ) : null}
                  </div>
                )}

                {Object.keys(selectedDetail.properties || {}).length > 0 ? (
                  <div className="border-t border-slate-100 pt-4 dark:border-slate-800">
                    <h4 className="text-[10px] font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400">Node Properties</h4>
                    <div className="mt-2 grid grid-cols-1 gap-1.5">
                      {selectedDetail.relevance !== undefined && (
                        <div className="min-w-0 rounded-lg border border-sky-200 bg-sky-50/30 px-2 py-1.5 dark:border-sky-800/60 dark:bg-sky-950/20">
                          <div className="text-[10px] text-sky-500 font-bold uppercase tracking-tighter">Match Relevance</div>
                          <div className="mt-0.5 flex items-center gap-2">
                            <div className="h-1.5 flex-1 bg-slate-200 dark:bg-slate-800 rounded-full overflow-hidden">
                              <div
                                className="h-full bg-sky-500 transition-all duration-1000"
                                style={{ width: `${(selectedDetail.relevance || 0) * 100}%` }}
                              />
                            </div>
                            <span className="text-xs font-mono font-bold text-sky-600 dark:text-sky-400">
                              {Math.round((selectedDetail.relevance || 0) * 100)}%
                            </span>
                          </div>
                        </div>
                      )}
	                      {Object.entries(selectedDetail.properties)
	                        .filter(([key]) => !HIDDEN_DETAIL_PROPERTY_KEYS.has(key))
	                        .map(([key, value]) => (
                        <div key={key} className="min-w-0 rounded-lg border border-slate-200 bg-slate-50 px-2 py-1.5 dark:border-slate-800 dark:bg-slate-900">
                          <div className="text-[10px] text-slate-500">{formatUserLabel(key)}</div>
                          <div className="mt-0.5 break-words text-xs font-medium text-slate-900 dark:text-white">
                            {Array.isArray(value) ? value.join(", ") : String(value)}
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                ) : null}

                <div className="space-y-3">
                  <div className="flex items-center justify-between gap-3">
                    <h4 className="text-[10px] font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400">Citations</h4>
                    <span className="text-[10px] text-slate-400">{visibleCitations.length}{systemCitations.length > 0 ? ` + ${systemCitations.length} internal` : ""}</span>
                  </div>
                  {visibleCitations.length === 0 ? (
                    <div className="rounded-lg border border-slate-200 p-3 text-xs text-slate-500 dark:border-slate-800 dark:text-slate-400">
                      {systemCitations.length > 0
                        ? "No external citation is attached yet. Internal operating-memory provenance is available below."
                        : "No source citation attached yet."}
                    </div>
                  ) : (
                    visibleCitations.map((citation) => (
                      <div key={citation.raw_evidence_id} className="min-w-0 rounded-lg border border-slate-200 p-3 dark:border-slate-800">
                        <div className="text-xs font-semibold text-slate-900 dark:text-slate-100">{citation.source_name}</div>
                        <div className="mt-2 flex flex-wrap gap-1.5">
                          <span className="rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-slate-500 dark:border-slate-800 dark:bg-slate-900 dark:text-slate-300">
                            {citation.source_type.replaceAll("_", " ")}
                          </span>
                          {citation.source_item_type ? (
                            <span className="rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-slate-500 dark:border-slate-800 dark:bg-slate-900 dark:text-slate-300">
                              {citation.source_item_type.replaceAll("_", " ")}
                            </span>
                          ) : null}
                          <span className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider ${citationOriginClasses(citation.origin_kind)}`}>
                            {citation.origin_label ?? "Source catalog"}
                          </span>
                        </div>
                        {citation.title ? <div className="mt-2 break-words text-xs text-slate-700 dark:text-slate-200">{citation.title}</div> : null}
                        {citation.origin_detail ? (
                          <div className="mt-2 line-clamp-2 text-[10px] text-slate-500 dark:text-slate-400">
                            Origin: {citation.origin_detail}
                          </div>
                        ) : null}
                        <div className="mt-2 flex flex-wrap gap-3 text-[10px] text-slate-500 dark:text-slate-400">
                          {citation.author ? <span>Author: {citation.author}</span> : null}
                          <SourceProvenanceLinks
                            evidenceId={citation.raw_evidence_id}
                            sourceName={citation.source_name}
                            sourceType={citation.source_type}
                            url={citation.url}
                            urlKind={citation.url_kind}
                            compact
                            showUnavailable
                          />
                        </div>
                      </div>
                    ))
                  )}
                  {systemCitations.length > 0 ? (
                    <div className="rounded-lg border border-dashed border-amber-200 p-3 dark:border-amber-900">
                      <div className="flex items-center justify-between gap-3">
                        <div>
                          <div className="text-[10px] font-bold uppercase tracking-wider text-amber-500 dark:text-amber-300">
                            Internal provenance
                          </div>
                          <div className="mt-0.5 text-[10px] text-slate-500 dark:text-slate-400">
                            These citations come from Prophet operating memory.
                          </div>
                        </div>
                        <button
                          type="button"
                          onClick={() => setShowSystemCitations((current) => !current)}
                          className="rounded-full border border-amber-300 px-3 py-1 text-xs text-amber-700 dark:border-amber-800 dark:text-amber-300"
                        >
                          {showSystemCitations ? "Hide" : "Show"}
                        </button>
                      </div>
                      {showSystemCitations ? (
                        <div className="mt-4 space-y-3">
                          {systemCitations.map((citation) => (
                            <div key={citation.raw_evidence_id} className="min-w-0 rounded-lg border border-amber-100 bg-amber-50/60 p-4 dark:border-amber-950 dark:bg-amber-950/10">
                              <div className="text-sm font-semibold text-slate-900 dark:text-slate-100">{citation.source_name}</div>
                              <div className="mt-2 flex flex-wrap gap-1.5">
                                <span className="rounded-full border border-amber-200 bg-white/70 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-amber-700 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-300">
                                  {citation.system_reason?.replaceAll("_", " ") ?? "system memory"}
                                </span>
                                <span className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider ${citationOriginClasses(citation.origin_kind)}`}>
                                  {citation.origin_label ?? "Source catalog"}
                                </span>
                              </div>
                              {citation.title ? <div className="mt-3 break-words text-sm text-slate-700 dark:text-slate-200">{citation.title}</div> : null}
                              {citation.origin_detail ? (
                                <div className="mt-2 line-clamp-2 text-xs text-slate-500 dark:text-slate-400">
                                  Origin: {citation.origin_detail}
                                </div>
                              ) : null}
                              <div className="mt-3">
                                <SourceProvenanceLinks
                                  evidenceId={citation.raw_evidence_id}
                                  sourceName={citation.source_name}
                                  sourceType={citation.source_type}
                                  url={citation.url}
                                  urlKind={citation.url_kind}
                                  compact
                                  showUnavailable
                                />
                              </div>
                            </div>
                          ))}
                        </div>
                      ) : null}
                    </div>
                  ) : null}
                </div>
              </div>
            ) : (
              <div className="mt-5 rounded-lg border border-dashed border-slate-300 p-4 text-sm text-slate-500 dark:border-slate-700 dark:text-slate-400">
                Select a node in the graph to inspect its detail.
              </div>
            )}
          </section>

          <section className="min-w-0 rounded-lg border border-slate-200 bg-white p-4 sm:p-6 dark:border-slate-800 dark:bg-slate-950">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <h2 className="text-sm font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400">
                Tracked research subjects
              </h2>
              <label className="flex w-full items-center gap-2 border-b border-slate-300 px-1 py-1.5 text-sm dark:border-slate-700">
                <Search size={15} className="shrink-0 text-slate-400" aria-hidden="true" />
                <input
                  value={knowledgeQuery}
                  onChange={(event) => setKnowledgeQuery(event.target.value)}
                  placeholder="Search all knowledge"
                  className="min-w-0 flex-1 bg-transparent outline-none placeholder:text-slate-400"
                />
              </label>
            </div>
            <div className="mt-4 grid grid-cols-1 gap-2">
              {(knowledgeQuery.trim().length >= 2 ? knowledgeResults : visibleSeedItems).map((item) => {
                const nodeType = "node_type" in item ? item.node_type : item.subject_type;
                const nodeId = "node_id" in item ? item.node_id : item.subject_id;
                const label = "label" in item ? item.label : item.subject_name;
                const subtitle = "subtitle" in item ? item.subtitle : null;
                return (
                  <button
                    key={`${nodeType}:${nodeId}`}
                    type="button"
                    onClick={() => void openGraph(nodeType, nodeId)}
                    className="min-w-0 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-left text-xs text-slate-700 hover:border-sky-400 hover:bg-white dark:border-slate-800 dark:bg-slate-900 dark:text-slate-300 dark:hover:border-sky-600 dark:hover:bg-slate-950"
                  >
                    <span className="flex min-w-0 items-start justify-between gap-2">
                      <span className="shrink-0 text-[9px] font-semibold uppercase text-slate-400">
                        {formatUserLabel(nodeType)}
                      </span>
                      {subtitle ? (
                        <span className="min-w-0 break-words text-right text-[10px] text-slate-400 [overflow-wrap:anywhere]">
                          {subtitle}
                        </span>
                      ) : null}
                    </span>
                    <span className="mt-1 block break-words font-medium leading-5 [overflow-wrap:anywhere]">
                      {label}
                    </span>
                  </button>
                );
              })}
              {knowledgeSearching ? (
                <span className="py-1.5 text-xs text-slate-400">Searching...</span>
              ) : null}
              {!knowledgeSearching && knowledgeQuery.trim().length >= 2 && knowledgeResults.length === 0 ? (
                <span className="py-1.5 text-xs text-slate-400">No matching knowledge</span>
              ) : null}
            </div>
          </section>
        </aside>
      </main>
    </div>
  );
}

function InventoryMetric({ label, value }: { label: string; value: number }) {
  return (
    <div className="min-w-0 border-l-2 border-slate-200 pl-3 dark:border-slate-700">
      <dt className="text-[11px] text-slate-500 dark:text-slate-400">{label}</dt>
      <dd className="mt-1 text-sm font-semibold text-slate-800 dark:text-slate-100">
        {formatCount(value)}
      </dd>
    </div>
  );
}
