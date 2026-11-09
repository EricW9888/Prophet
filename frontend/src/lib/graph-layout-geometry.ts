import type { GraphWebNode } from "./api";

export type PositionedNode = GraphWebNode & {
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
export const WIDTH = 5200;
export const HEIGHT = 3400;
export const CENTER_X = WIDTH / 2;
export const CENTER_Y = HEIGHT / 2;

export function nodeVisualRadius(node: GraphWebNode, state: { selected: boolean; compare: boolean }) {
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
