"use client";

import { useEffect, useRef, useState, type RefObject } from "react";
import * as d3 from "d3";

export const LABEL_DETAIL_ZOOM = 1.35;
export const LABEL_DENSE_ZOOM = 1.9;

function labelZoom(scale: number) {
  return scale >= LABEL_DENSE_ZOOM ? LABEL_DENSE_ZOOM : scale >= LABEL_DETAIL_ZOOM ? LABEL_DETAIL_ZOOM : 1;
}

/** D3 owns continuous camera movement; React owns discrete label detail. */
export function useGraphCamera(
  svgRef: RefObject<SVGSVGElement | null>,
  containerRef: RefObject<SVGGElement | null>,
  enabled: boolean,
  clearHover: () => void,
) {
  const transformRef = useRef({ x: 0, y: 0, k: 1 });
  const zoomRef = useRef<d3.ZoomBehavior<SVGSVGElement, unknown> | null>(null);
  const [detailZoom, setDetailZoom] = useState(1);
  const detailZoomRef = useRef(1);

  useEffect(() => {
    if (!enabled || !svgRef.current || !containerRef.current) return;
    const element = svgRef.current;
    const svg = d3.select(element);
    const zoom = d3.zoom<SVGSVGElement, unknown>()
      .scaleExtent([0.05, 5])
      .filter((event) => event.type === "wheel" || !(event.target instanceof Element) || !event.target.closest(".graph-node, .graph-edge-hit, button, a, textarea, input, select"))
      .on("start", clearHover)
      .on("zoom", ({ transform }) => {
        d3.select(containerRef.current).attr("transform", transform.toString());
        transformRef.current = { x: transform.x, y: transform.y, k: transform.k };
        const nextDetail = labelZoom(transform.k);
        if (nextDetail !== detailZoomRef.current) {
          detailZoomRef.current = nextDetail;
          setDetailZoom(nextDetail);
        }
      });
    svg.call(zoom);
    zoomRef.current = zoom;
    return () => {
      svg.interrupt().on(".zoom", null);
      zoomRef.current = null;
    };
  }, [clearHover, containerRef, enabled, svgRef]);

  return { transformRef, zoomRef, detailZoom };
}
