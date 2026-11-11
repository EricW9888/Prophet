"use client";

import { memo, type Dispatch, type SetStateAction } from "react";
import { nodeVisualRadius, type PositionedNode } from "@/lib/graph-layout-geometry";
import { LABEL_DETAIL_ZOOM, LABEL_DENSE_ZOOM } from "@/lib/use-graph-camera";

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

export function getNodeStyle(nodeType: string) {
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

function svgSafeId(value: string) {
  return value.replace(/[^a-zA-Z0-9_-]/g, "-");
}

type NodeProps = {
  node: PositionedNode;
  isSelected: boolean;
  isHovered: boolean;
  isConnected: boolean;
  hasSelection: boolean;
  zoom: number;
  onHover: Dispatch<SetStateAction<string | null>>;
  onPointerDown: (event: React.PointerEvent<SVGGElement>, node: PositionedNode) => void;
};

export const GraphNodeGlyph = memo(function GraphNodeGlyph({ node, isSelected, isHovered, isConnected, hasSelection, zoom, onHover, onPointerDown }: NodeProps) {
  const radius = nodeVisualRadius(node, {
    selected: isSelected,
    compare: false,
  });
  const style = getNodeStyle(node.node_type);
  const labelVisible =
    (radius >= 20 || isSelected || isHovered || node.is_root) &&
    shouldShowNodeLabel(node, {
      selected: isSelected,
      connected: Boolean(isConnected),
      hovered: isHovered,
      zoom,
    });
  const labelLines = labelVisible ? compactNodeLabel(node.label, radius) : [];
  const fontSize = radius >= 46 ? 8.25 : radius >= 38 ? 7.5 : 6.75;
  const clipId = `node-clip-${svgSafeId(node.key)}`;

  return (
    <g
      key={node.key}
      data-node-key={node.key}
      onPointerDown={(event) => onPointerDown(event, node)}
      onMouseEnter={() => onHover(node.key)}
      onMouseLeave={() => onHover((current) => current === node.key ? null : current)}
      className="graph-node cursor-pointer"
      transform={`translate(${node.x ?? 0}, ${node.y ?? 0})`}
    >
      <title>{node.label}</title>
      <circle
        cx={0}
        cy={0}
        r={radius}
        fill={style.fill}
        fillOpacity={
          isSelected
            ? 0.98
            : isHovered
              ? 0.94
              : isConnected || node.is_root
              ? 0.9
              : hasSelection
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
});

type EdgeProps = {
  edgeKey: string;
  source: PositionedNode;
  target: PositionedNode;
  description: string;
  selected: boolean;
  highlighted: boolean;
  onHover: Dispatch<SetStateAction<string | null>>;
  onSelect: (key: string) => void;
};

export const GraphEdgeGlyph = memo(function GraphEdgeGlyph({ edgeKey, source, target, description, selected, highlighted, onHover, onSelect }: EdgeProps) {
  return (
    <g key={edgeKey}>
      <title>{description}</title>
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
          onMouseEnter={() => onHover(edgeKey)}
          onMouseLeave={() => onHover((current) => current === edgeKey ? null : current)}
          onClick={(event) => { event.stopPropagation(); onSelect(edgeKey); }}
        />
    </g>
  );
});
