"use client";

import Link from "next/link";
import { useMemo, useState, type MouseEvent } from "react";
import { CartesianGrid, getRelativeCoordinate, Line, LineChart, ReferenceDot, ReferenceLine, ResponsiveContainer, XAxis, YAxis, usePlotArea, useXAxisInverseScale } from "recharts";
import { safeFormatCurrency } from "@/lib/formatting";

type ReceiptLink = { href?: string; receiptLabel?: string; appReceipt?: boolean };
export type ValuePoint = ReceiptLink & { id: string; at: string; value: number | null; detail?: string };
export type ChartEvent = ReceiptLink & { id: string; at: string; label: string; detail?: string; kind: "fill" | "decision" | "ledger" };
type Observation = (ValuePoint | ChartEvent) & { time: number };
const noEvents: ChartEvent[] = [];

function dated<T extends { at: string }>(items: T[]) {
  return items.map((item) => ({ ...item, time: Date.parse(item.at) }))
    .filter((item) => Number.isFinite(item.time)).sort((a, b) => a.time - b.time);
}

function description(item: Observation) {
  if (!("value" in item)) return item.label;
  return item.value == null ? "Value unavailable" : safeFormatCurrency(item.value, 2);
}

function PlotInteraction({ observations, onHover, onSelect }: {
  observations: Observation[];
  onHover: (id: string | null) => void;
  onSelect: (id: string) => void;
}) {
  const plot = usePlotArea();
  const invert = useXAxisInverseScale();
  if (!plot || !invert) return null;
  const { x, y, width, height } = plot;
  const observationAt = (event: MouseEvent<SVGRectElement>) => {
    const time = Number(invert(x + getRelativeCoordinate(event).relativeX));
    if (!Number.isFinite(time)) return observations[0].id;
    return observations.reduce((nearest, item) =>
      Math.abs(item.time - time) < Math.abs(nearest.time - time) ? item : nearest).id;
  };
  return <rect x={x} y={y} width={width} height={height} fill="transparent" className="cursor-crosshair"
    data-chart-plot
    onPointerMove={(event) => { if (event.pointerType !== "touch") onHover(observationAt(event)); }}
    onPointerLeave={() => onHover(null)}
    onClick={(event) => onSelect(observationAt(event))} />;
}

export default function CapturedValueChart({ label, points, events = noEvents, eventsLabel = "Actions and decisions", calendarDates = false }: {
  label: string; points: ValuePoint[]; events?: ChartEvent[]; eventsLabel?: string; calendarDates?: boolean;
}) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const { marks, markers, observations, times, values } = useMemo(() => {
    const marks = dated(points).map((point) => ({ ...point, value: Number.isFinite(point.value) ? point.value : null }));
    const markers = dated(events);
    const observations = [...marks, ...markers].sort((a, b) => a.time - b.time);
    return { marks, markers, observations, times: [...new Set(observations.map((item) => item.time))],
      values: marks.flatMap((mark) => mark.value == null ? [] : [mark.value]) };
  }, [points, events]);
  const selected = observations.find((item) => item.id === selectedId) ?? marks.at(-1) ?? observations.at(-1);
  const hovered = observations.find((item) => item.id === hoveredId);
  if (!selected) return <p className="text-sm text-muted">No dated observations yet.</p>;
  const active = hovered ?? selected;
  const [start, end] = [times[0], times.at(-1)!];
  const min = values.length ? Math.min(...values) : 0;
  const max = values.length ? Math.max(...values) : 0;
  const pad = (max - min || Math.max(Math.abs(max) * 0.02, 1)) * 0.12;
  const bottom = min - pad;
  const timeZone = calendarDates ? "UTC" : undefined;
  const detailedDate = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: calendarDates ? undefined : "short", timeZone });
  const useTimes = !calendarDates && start !== end && new Date(start).toDateString() === new Date(end).toDateString();
  const axisDate = new Intl.DateTimeFormat(undefined, useTimes ? { timeStyle: "short" } : { month: "short", day: "numeric", timeZone });

  function selectObservation(id: string) {
    setHoveredId(null);
    setSelectedId(id);
  }

  const timelineControl = observations.length > 1 && <input type="range" min={0} max={observations.length - 1} step={1} value={observations.indexOf(selected)}
    aria-label={`Inspect ${label.toLowerCase()} timeline`} aria-valuetext={`${detailedDate.format(selected.time)} · ${description(selected)}`}
    onChange={(event) => selectObservation(observations[Number(event.target.value)].id)}
    className={values.length ? "sr-only" : "min-w-20 flex-1 accent-current"} />;

  return <div className="min-w-0 space-y-3">
    {values.length > 0 ? <div className="min-w-0 border border-line bg-background px-2 pb-1 pt-3 focus-within:outline-2 focus-within:outline-action sm:px-3">
      <div className="mb-2 flex min-h-14 flex-wrap items-baseline gap-x-3 px-2" aria-hidden="true">
        <strong className="text-xl font-semibold tabular-nums">{description(active)}</strong>
        <time dateTime={active.at} className="text-sm text-muted">{detailedDate.format(active.time)}</time>
      </div>
      {timelineControl}
      <div className="h-52 sm:h-64" aria-hidden="true">
        <ResponsiveContainer width="100%" height="100%" minWidth={0}>
          <LineChart data={marks} margin={{ top: 8, right: 22, bottom: 0, left: 0 }} accessibilityLayer={false}>
            <CartesianGrid vertical={false} stroke="var(--color-line)" />
            <XAxis dataKey="time" type="number" scale="linear" domain={[start, end]} ticks={times} tickFormatter={(time) => axisDate.format(time)}
              interval="preserveStartEnd" minTickGap={20} padding={{ left: 22, right: 22 }} allowDataOverflow
              tick={{ fill: "var(--color-muted)", fontSize: 12 }} tickLine={false} axisLine={false} />
            <YAxis width="auto" domain={[bottom, max + pad]} tickCount={4} allowDataOverflow
              tickFormatter={(value) => safeFormatCurrency(value, max - min < 4 ? 2 : 0)}
              tick={{ fill: "var(--color-muted)", fontSize: 12 }} tickLine={false} axisLine={false} />
            <ReferenceLine x={active.time} stroke="var(--color-muted)" strokeDasharray="3 4" />
            <Line dataKey="value" type="linear" stroke="var(--color-action)" strokeWidth={2} connectNulls={false}
              dot={{ r: 2.5, fill: "var(--color-action)", strokeWidth: 0 }} activeDot={false} isAnimationActive={false} />
            {"value" in active && active.value != null && <ReferenceDot x={active.time} y={active.value} r={4.5}
              fill="var(--color-action)" stroke="var(--color-background)" strokeWidth={2} className="pointer-events-none" />}
            <PlotInteraction observations={observations} onHover={setHoveredId} onSelect={selectObservation} />
            {marks.filter((mark) => mark.value == null).map((mark) => <ReferenceDot key={mark.id} x={mark.time} y={bottom} r={3}
              fill="var(--color-background)" stroke="var(--color-muted)" className="pointer-events-none" />)}
            {markers.map((marker) => <ReferenceDot key={marker.id} x={marker.time} y={bottom} r={marker.id === active.id ? 5 : 3.5}
              fill={marker.kind === "fill" ? "var(--color-emerald-500)" : "var(--color-amber-500)"} stroke="var(--color-background)"
              className="cursor-pointer" onMouseEnter={() => setHoveredId(marker.id)} onMouseLeave={() => setHoveredId(null)} onClick={() => selectObservation(marker.id)} />)}
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div> : <p className="text-sm text-muted">No complete values to plot.</p>}
    {!values.length && timelineControl && <label className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-sm text-muted">
      <span>Inspect timeline</span>{timelineControl}
    </label>}
    <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1 border-l-2 border-line pl-3 text-sm" aria-live="polite">
      <p><time dateTime={selected.at} className="text-muted">{detailedDate.format(selected.time)}</time>{" · "}<strong className="tabular-nums">{description(selected)}</strong></p>
      {selected.detail && <p className="text-muted">{selected.detail}</p>}
      {selected.href && (selected.appReceipt
        ? <Link href={selected.href} prefetch={false} className="text-action underline">{selected.receiptLabel}</Link>
        : <a href={selected.href} className="text-action underline">{selected.receiptLabel ?? "Open capture receipt"}</a>)}
    </div>
    {markers.length > 0 && <details className="border-t border-line pt-2 text-sm">
      <summary className="cursor-pointer text-muted">{eventsLabel} ({markers.length})</summary>
      <ol className="mt-2 max-h-56 divide-y divide-line overflow-auto">
        {[...markers].reverse().map((event) => <li key={event.id}><button type="button" onClick={() => selectObservation(event.id)} aria-pressed={event.id === selected.id} className="w-full px-2 py-2 text-left hover:bg-panel aria-pressed:border-l-2 aria-pressed:border-action"><span className={event.kind === "fill" ? "text-emerald-500" : "text-amber-500"}>{event.kind === "fill" ? "Fill" : event.kind === "ledger" ? "Ledger" : "Decision"}</span>{" · "}{event.label}<span className="ml-2 text-muted">{detailedDate.format(event.time)}</span></button></li>)}
      </ol>
    </details>}
  </div>;
}
