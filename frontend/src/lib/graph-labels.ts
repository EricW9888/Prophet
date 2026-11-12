import type { PositionedNode } from "./graph-layout-geometry";

export function graphLabelPriority(node: PositionedNode, selected = false, hovered = false, connected = false) {
  return (selected ? 100_000 : hovered ? 95_000 : node.is_root ? 90_000 :
    node.node_type === "position" ? 80_000 : connected ? 75_000 :
    node.node_type === "entity" ? 70_000 : node.tier === "critical" ? 65_000 :
    node.tier === "high" ? 60_000 : 0) + Math.min(999, node.degree ?? 0);
}

export function graphLabelCandidates(nodes: PositionedNode[], selected: string | null, hovered: string | null, connected: Set<string>) {
  // Bound text work while retaining every graph node. More detail is available
  // through selection, hover, the navigator, and the inspector.
  const budget = Math.min(96, Math.max(12, Math.ceil(Math.sqrt(nodes.length) * 3)));
  return new Set([...nodes].sort((a, b) =>
    graphLabelPriority(b, b.key === selected, b.key === hovered, connected.has(b.key)) -
    graphLabelPriority(a, a.key === selected, a.key === hovered, connected.has(a.key)) || a.key.localeCompare(b.key)
  ).slice(0, budget).map(node => node.key));
}

type Box = { left: number; top: number; right: number; bottom: number };
export type LabelBox = Box & { key: string; priority: number; pinned: boolean };

export function nonOverlappingLabels(labels: LabelBox[], viewport: Box) {
  const chosen: LabelBox[] = [];
  for (const label of [...labels].sort((a, b) => b.priority - a.priority || a.key.localeCompare(b.key))) {
    if (label.right < viewport.left || label.left > viewport.right || label.bottom < viewport.top || label.top > viewport.bottom) continue;
    const overlaps = chosen.some(other => label.left < other.right + 5 && label.right + 5 > other.left && label.top < other.bottom + 4 && label.bottom + 4 > other.top);
    if (label.pinned || !overlaps) chosen.push(label);
  }
  return new Set(chosen.map(label => label.key));
}

/** D3 keeps camera motion outside React; text remains in screen pixels. */
export function refreshGraphLabels(svg: SVGSVGElement, zoom: number) {
  const matrix = svg.getScreenCTM?.();
  const viewScale = matrix ? Math.hypot(matrix.a, matrix.b) : 1;
  svg.style.setProperty("--graph-effective-scale", String(Math.max(0.0001, viewScale * zoom)));
  const elements = [...svg.querySelectorAll<SVGTextElement>(".graph-node-label")];
  // Read all bounds together, then write visibility together; avoid layout
  // thrashing on individual labels. This runs once per frame, not per node.
  const boxes = elements.map(element => {
    const box = element.getBoundingClientRect();
    return {left: box.left, top: box.top, right: box.right, bottom: box.bottom,
      key: element.dataset.labelKey!, priority: Number(element.dataset.labelPriority), pinned: element.dataset.labelPinned === "true"};
  });
  const visible = nonOverlappingLabels(boxes, svg.getBoundingClientRect());
  for (const element of elements) element.style.visibility = visible.has(element.dataset.labelKey!) ? "visible" : "hidden";
}
