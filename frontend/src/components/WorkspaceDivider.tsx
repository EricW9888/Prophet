"use client";

import { type CSSProperties, useEffect, useRef, useState } from "react";
import { useWorkspacePreference } from "@/lib/workspace-preference";

export function useWorkspacePane(key: string, defaultWidth: number) {
  const store = useWorkspacePreference(key);

  function parse(value: string) {
    try {
      const saved = JSON.parse(value);
      if (typeof saved?.width === "number" && Number.isFinite(saved.width) && typeof saved.open === "boolean") {
        return { width: saved.width as number, open: saved.open as boolean };
      }
    } catch { /* Start with the default layout. */ }
    return { width: defaultWidth, open: true };
  }
  const layout = parse(store.value);
  function update(next: Partial<typeof layout>) {
    store.write(JSON.stringify({ ...parse(store.read()), ...next }));
  }

  return {
    ...layout,
    setWidth: (width: number) => update({ width }),
    setOpen: (open: boolean) => update({ open }),
    toggle: () => update({ open: !parse(store.read()).open }),
    style: { "--pane-width": `${layout.width}px` } as CSSProperties,
  };
}

export default function WorkspaceDivider({
  side, controls, label, width, defaultWidth, onResize,
}: {
  side: "left" | "right";
  controls: string;
  label: string;
  width: number;
  defaultWidth: number;
  onResize: (width: number) => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [max, setMax] = useState(720);
  const drag = useRef<{ pointer: number; x: number; start: number; width: number } | null>(null);
  const min = 260;
  const clamp = (value: number) => Math.round(Math.min(max, Math.max(min, value)));
  const direction = side === "left" ? 1 : -1;
  useEffect(() => {
    const container = ref.current?.parentElement;
    if (!container) return;
    const observer = new ResizeObserver(([entry]) => {
      setMax(Math.max(min, Math.min(720, Math.floor(entry.contentRect.width * 0.5))));
    });
    observer.observe(container);
    return () => observer.disconnect();
  }, []);

  return (
    <div
      ref={ref}
      role="separator"
      aria-label={label}
      aria-controls={controls}
      aria-orientation="vertical"
      aria-valuemin={min}
      aria-valuemax={max}
      aria-valuenow={clamp(width)}
      aria-valuetext={`${clamp(width)} pixels`}
      tabIndex={0}
      title="Drag or use arrow keys to resize. Double-click to reset."
      className="workspace-divider"
      onDoubleClick={() => onResize(defaultWidth)}
      onKeyDown={(event) => {
        const step = event.shiftKey ? 64 : 16;
        const value = {
          ArrowLeft: clamp(width) - direction * step,
          ArrowRight: clamp(width) + direction * step,
          Home: min,
          End: max,
          Enter: defaultWidth,
        }[event.key];
        if (value === undefined) return;
        event.preventDefault();
        onResize(clamp(value));
      }}
      onPointerDown={(event) => {
        if (!event.isPrimary || event.button !== 0) return;
        event.currentTarget.focus({ preventScroll: true });
        event.currentTarget.setPointerCapture(event.pointerId);
        const start = clamp(width);
        drag.current = { pointer: event.pointerId, x: event.clientX, start, width: start };
      }}
      onPointerMove={(event) => {
        const active = drag.current;
        if (!active || active.pointer !== event.pointerId) return;
        active.width = clamp(active.start + direction * (event.clientX - active.x));
        event.currentTarget.parentElement?.style.setProperty("--pane-width", `${active.width}px`);
      }}
      onLostPointerCapture={(event) => {
        const active = drag.current;
        if (!active || active.pointer !== event.pointerId) return;
        drag.current = null;
        onResize(active.width);
      }}
    />
  );
}
