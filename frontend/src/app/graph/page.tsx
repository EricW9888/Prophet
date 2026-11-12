"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as d3 from "d3";
import Link from "next/link";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { Maximize2, PanelRightClose, PanelRightOpen, RotateCcw, Search, ZoomIn, ZoomOut } from "lucide-react";
import { GraphEdgeGlyph, GraphNodeGlyph, getNodeStyle } from "@/components/GraphGlyphs";
import GraphNodeNavigator from "@/components/GraphNodeNavigator";
import HintMarker from "@/components/HintMarker";
import { graphLabelCandidates } from "@/lib/graph-labels";
import AppNav from "@/components/AppNav";
import WorkspaceDivider, { useWorkspacePane } from "@/components/WorkspaceDivider";
import WorkspaceState from "@/components/WorkspaceState";
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
} from "@/lib/api";
import { formatUserLabel } from "@/lib/formatting";
import { CENTER_X, CENTER_Y, HEIGHT, WIDTH, nodeVisualRadius, type PositionedNode } from "@/lib/graph-layout-geometry";
import type { GraphLayoutRequest } from "@/lib/graph-layout";
import { useGraphCamera } from "@/lib/use-graph-camera";

type GraphViewBox = {
  x: number;
  y: number;
  width: number;
  height: number;
};
type GraphMode = "portfolio" | "all" | "focused";

const INSPECTOR_DEFAULT_WIDTH = 420;
const DEFAULT_GRAPH_VIEW_BOX: GraphViewBox = { x: 0, y: 0, width: WIDTH, height: HEIGHT };
const MAX_GRAPH_LAYOUT_TARGET_ASPECT = (WIDTH / HEIGHT) * 0.92;
const MIN_GRAPH_LAYOUT_TARGET_ASPECT = 0.65;
const NODE_DRAG_THRESHOLD_PX = 6;
const FOCUSED_NEIGHBORHOOD_DEPTH = 2;
const FOCUSED_NEIGHBORHOOD_LIMIT = 56;
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

function markSingleGraphRoot<T extends GraphWebNode>(nodes: T[], rootKey: string | null) {
  return nodes.map((node) => {
    const isRoot = Boolean(rootKey && node.key === rootKey);
    return node.is_root === isRoot ? node : { ...node, is_root: isRoot };
  });
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
  const [holdingSubjects, setHoldingSubjects] = useState<{ position_id: string; entity_id: string; ticker: string; name: string; has_profile: boolean }[] | null>(null);
  const [holdingsError, setHoldingsError] = useState(false);
  const [knowledgeQuery, setKnowledgeQuery] = useState("");
  const [knowledgeResults, setKnowledgeResults] = useState<GraphSearchResult[]>([]);
  const [knowledgeSearching, setKnowledgeSearching] = useState(false);
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
  const graphRequestRef = useRef<AbortController | null>(null);
  const inspector = useWorkspacePane("knowledge-inspector", INSPECTOR_DEFAULT_WIDTH);
  const setInspectorOpen = inspector.setOpen;
  const [detailError, setDetailError] = useState<string | null>(null);
  const [graphAskJob, setGraphAskJob] = useState<AgentTurnJob | null>(null);
  const [graphAskResult, setGraphAskResult] = useState<AgentTurn | null>(null);
  const [graphAskError, setGraphAskError] = useState<string | null>(null);
  const [graphAskTargetLabel, setGraphAskTargetLabel] = useState<string | null>(null);
  const [graphAskContext, setGraphAskContext] = useState<"node" | "edge" | null>(null);
  const [graphAskUserPrompt, setGraphAskUserPrompt] = useState("");
  const [showSystemCitations, setShowSystemCitations] = useState(false);
  const [nodes, setNodes] = useState<PositionedNode[]>([]);
  const [layoutGraph, setLayoutGraph] = useState<GraphNeighborhood | null>(null);
  const [layoutError, setLayoutError] = useState<{ graph: GraphNeighborhood; message: string } | null>(null);
  const hasGraphSurface = graph !== null;
  const graphRef = useRef<GraphNeighborhood | null>(null);
  const graphViewportRef = useRef<HTMLDivElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const containerRef = useRef<SVGGElement>(null);
  const renderedNodesRef = useRef<PositionedNode[]>([]);
  const [layoutNonce, setLayoutNonce] = useState(0);
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
  const clearGraphHover = useCallback(() => {
    setHoveredNodeKey(null);
    setHoveredEdgeId(null);
  }, []);
  const { transformRef: viewTransformRef, zoomRef, detailZoom } = useGraphCamera(
    svgRef, containerRef, hasGraphSurface, clearGraphHover,
  );

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
  }, [toSvgPoint, viewTransformRef]);

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
  }, [zoomRef]);

  const fitNodesToViewport = useCallback((targetNodes: PositionedNode[], animate = false) => {
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

    if (zoomRef.current) {
      const svg = d3.select(svgRef.current).interrupt();
      if (animate && !window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
        svg.transition().duration(180).call(zoomRef.current.transform, nextTransform);
      } else {
        svg.call(zoomRef.current.transform, nextTransform);
      }
    } else {
      d3.select(containerRef.current).attr("transform", nextTransform.toString());
    }
  }, [viewTransformRef, zoomRef]);

  useEffect(() => {
    const viewport = graphViewportRef.current;
    if (!viewport) return;

    let animationFrame = 0;
    let initialFit = hasGraphSurface;
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
      if (changed) {
        graphViewBoxRef.current = nextViewBox;
        setGraphViewBox(nextViewBox);
        // A pane resize changes the camera. It must not recompute the world
        // layout or take away the user's current pan and zoom.
        layoutTargetAspectRef.current = graphLayoutTargetAspect(nextViewBox);
      }
      if (initialFit) {
        initialFit = false;
        animationFrame = requestAnimationFrame(() => fitNodesToViewport(renderedNodesRef.current));
      }
    });
    resizeObserver.observe(viewport);
    return () => {
      cancelAnimationFrame(animationFrame);
      resizeObserver.disconnect();
    };
  }, [fitNodesToViewport, hasGraphSurface]);

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

  const loadInventory = useCallback(async (signal: AbortSignal) => {
    setHoldingsError(false);
    await Promise.allSettled([
      apiFetch<NonNullable<typeof holdingSubjects>>("/graph/holding-subjects", { signal }).then((holdings) => {
        if (!signal.aborted) setHoldingSubjects(holdings);
      }, () => { if (!signal.aborted) setHoldingsError(true); }),
      apiFetch<GraphStats>("/graph/stats", { signal }).then((stats) => {
        if (!signal.aborted) setGraphStats(stats);
      }),
    ]);
  }, []);

  useEffect(() => {
    const query = knowledgeQuery.trim();
    if (query.length < 2) {
      setKnowledgeResults([]);
      setKnowledgeSearching(false);
      return;
    }
    const controller = new AbortController();
    setKnowledgeSearching(true);
    const timeout = window.setTimeout(() => {
      void apiFetch<GraphSearchResult[]>(
        `/graph/search?query=${encodeURIComponent(query)}&limit=12`,
        { signal: controller.signal },
      )
        .then((results) => { if (!controller.signal.aborted) setKnowledgeResults(results); })
        .catch(() => { if (!controller.signal.aborted) setKnowledgeResults([]); })
        .finally(() => { if (!controller.signal.aborted) setKnowledgeSearching(false); });
    }, 250);
    return () => {
      window.clearTimeout(timeout);
      controller.abort();
    };
  }, [knowledgeQuery]);

  const loadDetail = useCallback(async (nodeType: string, nodeId: string, generation: number) => {
    setDetailLoading(true);
    setDetailError(null);
    try {
      const detail = await apiFetch<GraphNodeDetail>(`/graph/nodes/${nodeType}/${nodeId}`);
      if (generation !== selectionGenerationRef.current) return;
      setSelectedDetail(detail);
    } catch (err) {
      if (generation === selectionGenerationRef.current) setDetailError(humanizeGraphError(err));
    } finally {
      if (generation === selectionGenerationRef.current) setDetailLoading(false);
    }
  }, []);

  const loadGraph = useCallback(async (
    mode: GraphMode,
    includeSystem: boolean,
    focus?: { nodeType: string; nodeId: string },
  ) => {
    if (mode === "focused" && !focus) return;
    graphRequestRef.current?.abort();
    const request = new AbortController();
    graphRequestRef.current = request;
    setLoading(true);
    setError(null);
    setGraphNotice(null);
    const path = focus
      ? `/graph/neighborhood/${focus.nodeType}/${focus.nodeId}?depth=${FOCUSED_NEIGHBORHOOD_DEPTH}&limit=${FOCUSED_NEIGHBORHOOD_LIMIT}&include_system=${includeSystem}`
      : `/graph/workspace?scope=${mode === "all" ? "expanded" : "portfolio"}&include_system=${includeSystem}`;
    try {
      const next = await apiFetch<GraphNeighborhood | null>(path, { signal: request.signal });
      if (request.signal.aborted) return;
      graphRef.current = next;
      setGraph(next);
      setGraphMode(mode);
      setShowSystemNodes(includeSystem);
      const generation = ++selectionGenerationRef.current;
      setSelectedKey(next?.root_key ?? null);
      setSelectedDetail(null);
      setSelectedEdgeId(null);
      setDetailError(null);
      setDetailLoading(false);
      const root = next?.nodes.find((node) => node.key === next.root_key);
      if (root) void loadDetail(root.node_type, root.id, generation);
    } catch (err) {
      if (request.signal.aborted) return;
      const message = humanizeGraphError(err);
      if (graphRef.current) setGraphNotice(message);
      else setError(message);
    } finally {
      if (!request.signal.aborted) setLoading(false);
    }
  }, [loadDetail]);

  const openGraph = useCallback((nodeType: string, nodeId: string) => (
    loadGraph("focused", showSystemNodes, { nodeType, nodeId })
  ), [loadGraph, showSystemNodes]);

  useEffect(() => {
    const inventory = new AbortController();
    void loadGraph("portfolio", false);
    void loadInventory(inventory.signal);
    return () => {
      graphRequestRef.current?.abort();
      inventory.abort();
      selectionGenerationRef.current += 1;
    };
  }, [loadGraph, loadInventory]);

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

  const activeGraph = graph;
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

  // The bounded physics run must not block pan, navigation or the inspector.
  useEffect(() => {
    if (!visibleGraph?.nodes.length) return;
    const worker = new Worker(new URL("../../lib/graph-layout.worker.ts", import.meta.url), { type: "module" });
    worker.onmessage = ({ data }: MessageEvent<PositionedNode[]>) => {
      for (const node of data) {
        nodePositionsRef.current.set(node.key, { x: node.x ?? CENTER_X, y: node.y ?? CENTER_Y, vx: 0, vy: 0 });
      }
      renderedNodesRef.current = data;
      setNodes(data);
      setLayoutGraph(visibleGraph);
      setLayoutError(null);
      fitNodesToViewport(data);
    };
    worker.onerror = (event) => {
      event.preventDefault();
      setLayoutError({ graph: visibleGraph, message: "The graph data loaded, but its layout could not be drawn." });
    };
    worker.postMessage({
      graph: visibleGraph,
      positions: nodePositionsRef.current,
      manual: manualNodePositionsRef.current,
      targetAspect: layoutTargetAspectRef.current,
    } satisfies GraphLayoutRequest);
    return () => worker.terminate();
  }, [fitNodesToViewport, layoutNonce, visibleGraph]);

  const layoutPending = Boolean(visibleGraph?.nodes.length && layoutGraph !== visibleGraph);
  const activeLayoutError = layoutError?.graph === visibleGraph ? layoutError?.message : null;

  const displayedNodes = useMemo(
    () =>
      (layoutGraph === visibleGraph ? nodes : []).map((node) => {
        const manual = manualNodePositionsRef.current[node.key];
        return manual ? { ...node, x: manual.x, y: manual.y, fx: manual.x, fy: manual.y } : node;
      }),
    [nodes, layoutGraph, visibleGraph],
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
      const camera = viewTransformRef.current;
      const transformedX = x * camera.k + camera.x;
      const transformedY = y * camera.k + camera.y;
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
  }, [displayedEdges, graphViewBox, hoveredEdgeId, hoveredNodeKey, nodeByKey, viewTransformRef]);
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
      setInspectorOpen(true);
      void loadDetail(node.node_type, node.id, generation);
    },
    [loadDetail, setInspectorOpen],
  );

  const labelCandidates = useMemo(() => graphLabelCandidates(displayedNodes, selectedKey, hoveredNodeKey, connectedKeys),
    [displayedNodes, selectedKey, hoveredNodeKey, connectedKeys]);
  const jumpToNode = useCallback((node: PositionedNode) => {
    handleNodeClick(node);
    const neighborhood = new Set([node.key]);
    for (const edge of displayedEdges) {
      if (edge.source_key === node.key) neighborhood.add(edge.target_key);
      if (edge.target_key === node.key) neighborhood.add(edge.source_key);
    }
    fitNodesToViewport(displayedNodes.filter(candidate => neighborhood.has(candidate.key)), true);
  }, [displayedEdges, displayedNodes, fitNodesToViewport, handleNodeClick]);

  const isBackgroundInteractionTarget = useCallback((target: EventTarget | null) => {
    if (!(target instanceof Element)) return true;
    return !target.closest(".graph-node, .graph-edge-hit, button, a, textarea, input, select");
  }, []);

  const handleCanvasPointerDown = useCallback((event: React.PointerEvent) => {
    // Canvas background clicks to deselect
    if (isBackgroundInteractionTarget(event.target)) {
      selectionGenerationRef.current += 1;
      setDetailLoading(false);
      setDetailError(null);
      setSelectedKey(null);
      setSelectedEdgeId(null);
      setSelectedDetail(null);
    }
  }, [isBackgroundInteractionTarget]);

  const selectGraphEdge = useCallback((key: string) => {
    setHoveredEdgeId(null);
    setSelectedKey(null);
    setSelectedEdgeId(key);
  }, []);

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
            <span className="rounded-none border border-amber-300 px-3 py-1 text-[11px] font-semibold uppercase tracking-wider text-amber-700 dark:border-amber-800 dark:text-amber-300">
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
                  className="inline-flex items-center justify-center rounded-none border border-slate-300 px-3 py-1.5 text-xs text-slate-600 hover:border-slate-400 dark:border-slate-700 dark:text-slate-300"
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

      <main className="workspace-frame space-y-3 py-4">
          <header className="flex flex-wrap items-center justify-between gap-3 border-b border-line pb-3">
            <div className="flex min-w-0 flex-wrap items-center gap-3">
              <h1 className="text-xl font-semibold tracking-tight sm:text-2xl">Knowledge Web</h1>
              <HintMarker label="How to use the knowledge graph">Scroll or pinch to zoom, drag the background to pan, and select a node or connection to inspect it. Search or browse the node list for full names and center a node’s connections. Overview labels prioritize holdings, important subjects and your selection; hidden labels do not remove graph records.</HintMarker>
              <div className="inline-flex gap-1">
                <button
                  type="button"
                  onClick={() => void loadGraph("portfolio", showSystemNodes)}
                  disabled={loading}
                  className="workspace-control"
                  aria-pressed={graphMode === "portfolio"}
                >
                  Portfolio Web
                </button>
                <button
                  type="button"
                  onClick={() => void loadGraph("all", showSystemNodes)}
                  disabled={loading}
                  className="workspace-control"
                  aria-pressed={graphMode === "all"}
                >
                  Expanded Web
                </button>
              </div>
            </div>
            <div className="flex max-w-full flex-wrap items-center gap-2">
              <button
                type="button"
                className="workspace-control"
                aria-expanded={inspector.open}
                aria-controls="knowledge-inspector"
                onClick={inspector.toggle}
              >
                {inspector.open ? <PanelRightClose size={16} /> : <PanelRightOpen size={16} />}
                {inspector.open ? "Hide inspector" : "Show inspector"}
              </button>
              {selectedDetail && selectedKey !== activeRootKey ? (
                <button
                  type="button"
                  onClick={() => void openGraph(selectedDetail.node_type, selectedDetail.id)}
                  className="rounded-none border border-sky-200 bg-sky-50 px-4 py-2 text-sm text-sky-700 hover:border-sky-300 dark:border-sky-800/40 dark:bg-sky-900/20 dark:text-sky-300"
                >
                  Focus on node
                </button>
              ) : null}
              {graphMode === "focused" ? (
                <button
                  type="button"
                  onClick={() => void loadGraph("portfolio", showSystemNodes)}
                  disabled={loading}
                  className="rounded-none border border-slate-300 px-4 py-2 text-sm text-slate-600 disabled:opacity-50 dark:border-slate-700 dark:text-slate-300"
                >
                  Back to overview
                </button>
              ) : null}
            </div>
          </header>

        <div className="knowledge-split" style={inspector.style} data-inspector-open={inspector.open}>
        <section className="min-w-0">
          <section className="min-w-0 rounded-lg border border-line bg-panel p-3 sm:p-6">
            {graphNotice ? (
              <WorkspaceState compact kind="degraded" className="mb-3" title="Could not switch graph views" description={`${graphNotice} The last loaded graph is still available.`} />
            ) : null}
            <div className="flex flex-wrap items-center justify-between gap-4">
              <div className="flex min-w-0 flex-wrap items-center gap-2 text-xs font-medium text-slate-500 dark:text-slate-400">
                <span className="rounded-none bg-slate-100 px-3 py-1 dark:bg-slate-900">
                  {visibleNodeCount} nodes
                </span>
                <span className="rounded-none bg-slate-100 px-3 py-1 dark:bg-slate-900">
                  {visibleEdgeCount} connections
                </span>
                <span className="rounded-none bg-slate-100 px-3 py-1 dark:bg-slate-900">
                  {modeLabel}
                </span>
                {hiddenNodeCount > 0 || hiddenEdgeCount > 0 ? (
                  <span className="rounded-none bg-amber-50 px-3 py-1 text-amber-700 dark:bg-amber-950/25 dark:text-amber-300">
                    {hiddenNodeCount} nodes / {hiddenEdgeCount} links hidden
                  </span>
                ) : null}
                {graphMode === "focused" ? (
                  <span className="rounded-none bg-sky-50 px-3 py-1 text-sky-700 dark:bg-sky-900/20 dark:text-sky-300">
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
                  className="rounded-none hover:bg-slate-100 p-2 text-slate-400 hover:text-slate-600 dark:hover:bg-slate-800"
                  aria-label="Zoom out"
                  title="Zoom out"
                >
                  <ZoomOut className="h-4 w-4" aria-hidden="true" />
                </button>
                <button
                  type="button"
                  onClick={() => fitNodesToViewport(displayedNodes, true)}
                  className="rounded-none border border-slate-200 px-3 py-1 text-xs font-semibold text-slate-600 dark:border-slate-800 dark:text-slate-300"
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

                    nodePositionsRef.current.clear();
                    manualNodePositionsRef.current = {};
                    setLayoutNonce((current) => current + 1);
                  }}
                  className="rounded-none border border-slate-200 px-3 py-1 text-xs font-semibold text-slate-600 dark:border-slate-800 dark:text-slate-300"
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
                  className="rounded-none hover:bg-slate-100 p-2 text-slate-400 hover:text-slate-600 dark:hover:bg-slate-800"
                  aria-label="Zoom in"
                  title="Zoom in"
                >
                  <ZoomIn className="h-4 w-4" aria-hidden="true" />
                </button>
              </div>
            </div>

            <GraphNodeNavigator nodes={displayedNodes} onSelect={jumpToNode} />
            <details className="mt-4 border-t border-slate-200 pt-3 dark:border-slate-800">
              <summary className="cursor-pointer text-xs font-semibold text-slate-600 dark:text-slate-300">
                Filters{activeNodeTypes.length > 0 ? ` · ${activeNodeTypes.length} active` : ""}
              </summary>
              <div className="mt-3 flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  onClick={() => setActiveNodeTypes([])}
                  className={[
                    "rounded-none border px-3 py-1 text-[11px] font-semibold transition-colors",
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
                        "inline-flex items-center gap-1.5 rounded-none border px-3 py-1 text-[11px] font-semibold transition-colors",
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
                <label className="inline-flex items-center gap-2 rounded-none border border-line bg-panel px-3 py-1.5">
                  <input
                    type="checkbox"
                    checked={showDuplicateNodes}
                    onChange={(event) => setShowDuplicateNodes(event.target.checked)}
                    className="h-3.5 w-3.5 rounded border-slate-300 text-sky-600 focus:ring-sky-500"
                  />
                  <span>Show duplicate labels</span>
                </label>
                <label className="inline-flex items-center gap-2 rounded-none border border-line bg-panel px-3 py-1.5">
                  <input
                    type="checkbox"
                    checked={showSystemNodes}
                    disabled={loading}
                    onChange={(event) => {
                      const root = graph?.nodes.find((node) => node.key === graph.root_key);
                      void loadGraph(graphMode, event.target.checked, graphMode === "focused" && root
                        ? { nodeType: root.node_type, nodeId: root.id }
                        : undefined);
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

            <div className="relative mt-3 min-w-0 overflow-hidden border border-line bg-background">
              {!activeGraph ? (
                <div className="knowledge-canvas flex items-center justify-center p-4">
                  <WorkspaceState
                    kind={error ? "error" : loading ? "loading" : "empty"}
                    title={error ? "Knowledge Web unavailable" : loading ? "Loading Knowledge Web" : "No knowledge to map yet"}
                    description={error ?? (loading ? "Connecting stored subjects and evidence." : "Add a holding, track a research subject, or ingest evidence to start building the map.")}
                    actionLabel={error ? "Retry" : undefined}
                    onAction={error ? () => void loadGraph("portfolio", showSystemNodes) : undefined}
                  />
                </div>
              ) : (
                <div
                  ref={graphViewportRef}
                  data-pull-refresh-ignore
                  className="knowledge-canvas relative w-full touch-pan-y select-none overflow-hidden sm:touch-none"
                  style={{ overscrollBehavior: "contain" }}
                  onPointerDown={handleCanvasPointerDown}
                  onPointerMove={handleCanvasPointerMove}
                  onPointerUp={handleCanvasPointerUp}
                  onPointerCancel={handleCanvasPointerUp}
                >
                  {activeLayoutError ? (
                    <div className="absolute inset-0 z-20 grid place-items-center p-4">
                      <WorkspaceState kind="error" title="Graph layout unavailable" description={activeLayoutError} actionLabel="Retry" onAction={() => { setLayoutError(null); setLayoutNonce((value) => value + 1); }} />
                    </div>
                  ) : (loading || layoutPending) && (
                    <div className="absolute inset-0 flex items-center justify-center bg-slate-900/40 backdrop-blur-[2px] z-20 pointer-events-none">
                      <div className="flex flex-col items-center gap-4">
                        <div className="w-8 h-8 border-4 border-sky-500/30 border-t-sky-500 rounded-full animate-spin"></div>
                        <p className="text-sm text-sky-100" role="status">{loading ? "Loading graph…" : "Arranging graph…"}</p>
                      </div>
                    </div>
                  )}
                  <svg
                    ref={svgRef}
                    viewBox={`${graphViewBox.x} ${graphViewBox.y} ${graphViewBox.width} ${graphViewBox.height}`}
                    className="h-full w-full touch-pan-y sm:touch-none"
                  >
                    <g ref={containerRef}>
                      {displayedEdges.map((edge) => {
                        const source = nodeByKey[edge.source_key];
                        const target = nodeByKey[edge.target_key];
                        if (!source || !target) return null;
                        const edgeKey = graphEdgeIdentity(edge);
                        return <GraphEdgeGlyph key={edgeKey} edgeKey={edgeKey} source={source} target={target}
                          description={describeEdge(edge)} selected={selectedEdgeId === edgeKey}
                          highlighted={hoveredEdgeId === edgeKey || Boolean(selectedKey && (edge.source_key === selectedKey || edge.target_key === selectedKey))}
                          onHover={setHoveredEdgeId} onSelect={selectGraphEdge} />;
                      })}

                      {displayedNodes.map((node) => <GraphNodeGlyph key={node.key}
                        node={node} isSelected={selectedKey === node.key} isHovered={hoveredNodeKey === node.key}
                        isConnected={selectedKey ? connectedKeys.has(node.key) : node.is_root} hasSelection={Boolean(selectedKey)}
                        zoom={detailZoom} labelCandidate={labelCandidates.has(node.key)} onSelect={handleNodeClick}
                        onHover={setHoveredNodeKey} onPointerDown={handleNodePointerDown} />)}
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
                          inspector.setOpen(true);
                          requestAnimationFrame(() => document.getElementById("knowledge-connection-detail")?.scrollIntoView({
                            behavior: "smooth",
                            block: "start",
                          }));
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
                          inspector.setOpen(true);
                          requestAnimationFrame(() => document.getElementById("knowledge-node-detail")?.scrollIntoView({
                            behavior: "smooth",
                            block: "start",
                          }));
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

        {inspector.open ? (
          <WorkspaceDivider side="right" controls="knowledge-inspector" label="Resize Knowledge inspector" width={inspector.width} defaultWidth={INSPECTOR_DEFAULT_WIDTH} onResize={inspector.setWidth} />
        ) : null}
        <aside id="knowledge-inspector" aria-label="Knowledge inspector" hidden={!inspector.open} className="min-w-0 space-y-3 self-start lg:sticky lg:top-20 lg:max-h-[calc(100dvh-6rem)] lg:overflow-y-auto">
          {activeEdge ? (
            <section id="knowledge-connection-detail" className="min-w-0 scroll-mt-24 rounded-lg border border-line bg-panel p-4 sm:p-6">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <h2 className="text-sm font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400">Connection detail</h2>
                  <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                    Inspect the link between the two subjects.
                  </p>
                </div>
                <button
                  type="button"
                  onClick={() => setSelectedEdgeId(null)}
                  className="rounded-none border border-slate-300 px-3 py-1 text-xs text-slate-600 dark:border-slate-700 dark:text-slate-300"
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
                  className="inline-flex items-center justify-center rounded-none border border-sky-300 px-4 py-2 text-xs font-medium text-sky-700 hover:border-sky-400 dark:border-sky-800 dark:text-sky-300"
                >
                  Analyze this connection
                </button>
                {graphAskContext === "edge" ? graphAskPanel : null}
              </div>
            </section>
          ) : null}

          <section id="knowledge-node-detail" className="min-w-0 scroll-mt-24 rounded-lg border border-line bg-panel p-4 sm:p-6">
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
                  className="rounded-none border border-amber-300 px-3 py-1 text-[10px] text-amber-700 hover:border-amber-400 dark:border-amber-800 dark:text-amber-300"
                >
                  Analyze node
                </button>
              ) : null}
            </div>

            {detailLoading ? (
              <div className="mt-5 rounded-lg border border-slate-200 p-4 text-xs text-slate-500 animate-pulse dark:border-slate-800 dark:text-slate-400">
                Loading node detail...
              </div>
            ) : detailError ? (
              <p role="alert" className="mt-4 text-sm text-rose-700 dark:text-rose-300">{detailError}</p>
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
                    className="inline-flex items-center justify-center rounded-none border border-amber-300 px-4 py-2 text-xs font-medium text-amber-700 hover:border-amber-400 dark:border-amber-800 dark:text-amber-300"
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
                        <span className="rounded-none border border-teal-300 bg-teal-100 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-teal-700 dark:border-teal-700 dark:bg-teal-900/50 dark:text-teal-200">
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
	                              className="rounded-none border border-teal-300 bg-white/70 px-2 py-1 text-[10px] font-semibold tracking-wide text-teal-700 dark:border-teal-400/30 dark:bg-teal-500/10 dark:text-teal-100"
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
                              className="rounded-none border border-sky-300 bg-sky-100 px-2 py-1 text-[10px] font-semibold tracking-wide text-sky-700 dark:border-sky-400/30 dark:bg-sky-500/10 dark:text-sky-200"
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
                              className="rounded-none border border-emerald-300 bg-emerald-100 px-2 py-1 text-[10px] font-semibold tracking-wide text-emerald-700 dark:border-emerald-400/30 dark:bg-emerald-500/10 dark:text-emerald-200"
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
                            <div className="h-1.5 flex-1 bg-slate-200 dark:bg-slate-800 rounded-none overflow-hidden">
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
                          <span className="rounded-none border border-slate-200 bg-slate-50 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-slate-500 dark:border-slate-800 dark:bg-slate-900 dark:text-slate-300">
                            {citation.source_type.replaceAll("_", " ")}
                          </span>
                          {citation.source_item_type ? (
                            <span className="rounded-none border border-slate-200 bg-slate-50 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-slate-500 dark:border-slate-800 dark:bg-slate-900 dark:text-slate-300">
                              {citation.source_item_type.replaceAll("_", " ")}
                            </span>
                          ) : null}
                          <span className={`rounded-none border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider ${citationOriginClasses(citation.origin_kind)}`}>
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
                          className="rounded-none border border-amber-300 px-3 py-1 text-xs text-amber-700 dark:border-amber-800 dark:text-amber-300"
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
                                <span className="rounded-none border border-amber-200 bg-white/70 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-amber-700 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-300">
                                  {citation.system_reason?.replaceAll("_", " ") ?? "system memory"}
                                </span>
                                <span className={`rounded-none border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider ${citationOriginClasses(citation.origin_kind)}`}>
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

          <section className="min-w-0 rounded-lg border border-line bg-panel p-4 sm:p-6">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <h2 className="text-sm font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400">
                {knowledgeQuery.trim().length >= 2 ? "Knowledge search" : `Portfolio holdings${holdingSubjects ? ` (${holdingSubjects.length})` : ""}`}
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
              {knowledgeQuery.trim().length < 2 ? (
                holdingsError ? <p className="text-xs text-muted">Holdings could not be loaded. Knowledge search is still available.</p> :
                  holdingSubjects === null ? <p className="text-xs text-muted">Loading holdings…</p> :
                    holdingSubjects.length === 0 ? <p className="text-xs text-muted">No open positions.</p> : null
              ) : null}
              {(knowledgeQuery.trim().length >= 2 ? knowledgeResults.map((item) => ({ key: `${item.node_type}:${item.node_id}`, type: item.node_type, id: item.node_id, label: item.label, caption: formatUserLabel(item.node_type), subtitle: item.subtitle })) : (holdingSubjects ?? []).map((item) => ({ key: item.position_id, type: "entity", id: item.entity_id, label: item.name, caption: item.ticker, subtitle: item.has_profile ? "Research profile" : "No profile yet" }))).map((item) => {
                return (
                  <button
                    key={item.key}
                    type="button"
                    onClick={() => void openGraph(item.type, item.id)}
                    className="min-w-0 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-left text-xs text-slate-700 hover:border-sky-400 hover:bg-white dark:border-slate-800 dark:bg-slate-900 dark:text-slate-300 dark:hover:border-sky-600 dark:hover:bg-slate-950"
                  >
                    <span className="flex min-w-0 items-start justify-between gap-2">
                      <span className="shrink-0 text-[9px] font-semibold uppercase text-slate-400">
                        {item.caption}
                      </span>
                      {item.subtitle ? (
                        <span className="min-w-0 break-words text-right text-[10px] text-slate-400 [overflow-wrap:anywhere]">
                          {item.subtitle}
                        </span>
                      ) : null}
                    </span>
                    <span className="mt-1 block break-words font-medium leading-5 [overflow-wrap:anywhere]">
                      {item.label}
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
        </div>
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
