"use client";

import Link from "next/link";
import { useMemo, useState, type MouseEvent } from "react";
import { createPortal } from "react-dom";
import { CartesianGrid, getRelativeCoordinate, Line, LineChart, ReferenceDot, ResponsiveContainer, XAxis, YAxis, usePlotArea, useXAxisInverseScale, useXAxisScale, useYAxisScale } from "recharts";
import { safeFormatCurrency } from "@/lib/formatting";

type ReceiptLink = { href?: string; receiptLabel?: string; appReceipt?: boolean };
export type ValuePoint = ReceiptLink & { id: string; at: string; value: number | null; detail?: string };
export type ChartEvent = ReceiptLink & { id: string; at: string; label: string; detail?: string; kind: "fill" | "decision" | "ledger" | "research" | "alert" };
type Observation = (ValuePoint | ChartEvent) & { time: number };
const noEvents: ChartEvent[] = [];
const eventKinds: Record<ChartEvent["kind"], { label: string; color: string }> = {
  ledger: { label: "Trades and cash flows", color: "var(--color-emerald-500)" },
  fill: { label: "Paper fills", color: "var(--color-emerald-500)" },
  decision: { label: "Decisions", color: "var(--color-amber-500)" },
  research: { label: "Research", color: "var(--color-action)" },
  alert: { label: "Alerts", color: "var(--color-rose-500)" },
};

function dated<T extends { at: string }>(items: T[]) {
  return items.map((item) => ({ ...item, time: Date.parse(item.at) }))
    .filter((item) => Number.isFinite(item.time)).sort((a, b) => a.time - b.time);
}

function description(item: Observation) {
  if (!("value" in item)) return item.label;
  return item.value == null ? "Value unavailable" : safeFormatCurrency(item.value, 2);
}

function PlotInteraction({ observations, markers, selected, bottom, readout, observationDate, onSelect }: {
  observations: Observation[];
  markers: (ChartEvent & { time: number })[];
  selected: Observation;
  bottom: number;
  readout: HTMLDivElement | null;
  observationDate: (item: Observation) => string;
  onSelect: (id: string) => void;
}) {
  // Keep pointer state below the chart and activity list. Recharts' public
  // scales place annotations without registering hundreds of reference lines.
  const [hovered, setHovered] = useState<Observation | null>(null);
  const plot = usePlotArea();
  const invert = useXAxisInverseScale();
  const scaleX = useXAxisScale();
  const scaleY = useYAxisScale();
  if (!plot || !invert || !scaleX || !scaleY) return null;
  const { x, y, width, height } = plot;
  const observationAt = (event: MouseEvent<SVGRectElement>) => {
    const time = Number(invert(x + getRelativeCoordinate(event).relativeX));
    if (!Number.isFinite(time)) return observations[0];
    return observations.reduce((nearest, item) =>
      Math.abs(item.time - time) < Math.abs(nearest.time - time) ? item : nearest);
  };
  const active = hovered ?? selected;
  const activeX = scaleX(active.time);
  return <g onPointerLeave={() => setHovered(null)}>
    {readout && createPortal(<>
      <strong className="text-xl font-semibold tabular-nums">{description(active)}</strong>
      <time dateTime={active.at} className="text-sm text-muted">{observationDate(active)}</time>
    </>, readout)}
    <line x1={activeX} x2={activeX} y1={y} y2={y + height} stroke="var(--color-muted)" strokeDasharray="3 4" className="pointer-events-none" />
    {"value" in active && active.value != null && <circle cx={activeX} cy={scaleY(active.value)} r={4.5}
      fill="var(--color-action)" stroke="var(--color-background)" strokeWidth={2} className="pointer-events-none" />}
    <rect x={x} y={y} width={width} height={height} fill="transparent" className="cursor-crosshair"
    data-chart-plot
    onPointerMove={(event) => { if (event.pointerType !== "touch") setHovered(observationAt(event)); }}
    onClick={(event) => { setHovered(null); onSelect(observationAt(event).id); }} />
    {markers.map(marker => <circle key={marker.id} cx={scaleX(marker.time)} cy={scaleY(bottom)} r={marker.id === active.id ? 5 : 3.5}
      fill={eventKinds[marker.kind].color} stroke="var(--color-background)" className="cursor-pointer"
      onMouseEnter={() => setHovered(marker)} onMouseLeave={() => setHovered(null)}
      onClick={() => { setHovered(null); onSelect(marker.id); }} />)}
  </g>;
}

export default function CapturedValueChart({ label, points, events = noEvents, eventsLabel = "Actions and decisions", calendarDates = false }: {
  label: string; points: ValuePoint[]; events?: ChartEvent[]; eventsLabel?: string; calendarDates?: boolean;
}) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [readout, setReadout] = useState<HTMLDivElement | null>(null);
  const [hiddenKinds, setHiddenKinds] = useState<Set<ChartEvent["kind"]>>(new Set());
  const [eventSearch, setEventSearch] = useState("");
  const { marks, markers, observations, times, values } = useMemo(() => {
    const marks = dated(points).map((point) => ({ ...point, value: Number.isFinite(point.value) ? point.value : null }));
    const markers = dated(events);
    const observations = [...marks, ...markers].sort((a, b) => a.time - b.time);
    return { marks, markers, observations, times: [...new Set(observations.map((item) => item.time))],
      values: marks.flatMap((mark) => mark.value == null ? [] : [mark.value]) };
  }, [points, events]);
  const query = eventSearch.trim().toLocaleLowerCase();
  const visibleMarkers = markers.filter((event) => !hiddenKinds.has(event.kind)
    && `${event.label} ${event.detail ?? ""}`.toLocaleLowerCase().includes(query));
  const visibleEventIds = new Set(visibleMarkers.map((event) => event.id));
  const selection = observations.filter((item) => "value" in item || visibleEventIds.has(item.id));
  const selected = selection.find((item) => item.id === selectedId) ?? marks.at(-1) ?? observations.at(-1);
  if (!selected) return <p className="text-sm text-muted">No dated observations yet.</p>;
  const [start, end] = [times[0], times.at(-1)!];
  const min = values.length ? Math.min(...values) : 0;
  const max = values.length ? Math.max(...values) : 0;
  const pad = (max - min || Math.max(Math.abs(max) * 0.02, 1)) * 0.12;
  const bottom = min - pad;
  const timeZone = calendarDates ? "UTC" : undefined;
  const detailedDate = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: calendarDates ? undefined : "short", timeZone });
  const eventDate = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });
  const observationDate = (item: Observation) => ("value" in item ? detailedDate : eventDate).format(item.time);
  const useTimes = !calendarDates && start !== end && new Date(start).toDateString() === new Date(end).toDateString();
  const axisDate = new Intl.DateTimeFormat(undefined, useTimes ? { timeStyle: "short" } : { month: "short", day: "numeric", timeZone });

  function selectObservation(id: string) {
    setSelectedId(id);
  }

  const timelineControl = selection.length > 1 && <input type="range" min={0} max={selection.length - 1} step={1} value={selection.indexOf(selected)}
    aria-label={`Inspect ${label.toLowerCase()} timeline`} aria-valuetext={`${observationDate(selected)} · ${description(selected)}`}
    onChange={(event) => selectObservation(selection[Number(event.target.value)].id)}
    className={values.length ? "sr-only" : "min-w-20 flex-1 accent-current"} />;

  return <div className="min-w-0 space-y-3">
    {markers.length > 0 && <fieldset className="flex flex-wrap items-center gap-x-4 gap-y-2 text-sm">
      <legend className="mb-2 font-medium">Events on the timeline</legend>
      {Object.entries(eventKinds).filter(([kind]) => markers.some((marker) => marker.kind === kind)).map(([kind, info]) => (
        <label key={kind} className="inline-flex cursor-pointer items-center gap-2">
          <input type="checkbox" checked={!hiddenKinds.has(kind as ChartEvent["kind"])}
            onChange={() => setHiddenKinds((previous) => {
              const next = new Set(previous);
              if (next.has(kind as ChartEvent["kind"])) next.delete(kind as ChartEvent["kind"]);
              else next.add(kind as ChartEvent["kind"]);
              return next;
            })} className="accent-current" />
          <span style={{ color: info.color }} aria-hidden="true">●</span>{info.label}
        </label>
      ))}
      <input type="search" value={eventSearch} onChange={(event) => setEventSearch(event.target.value)}
        aria-label="Filter timeline events" placeholder="Ticker or keyword"
        className="workspace-control min-w-0 flex-1 sm:max-w-64" />
    </fieldset>}
    {values.length > 0 ? <div className="min-w-0 border border-line bg-background px-2 pb-1 pt-3 focus-within:outline-2 focus-within:outline-action sm:px-3">
      <div ref={setReadout} className="mb-2 flex min-h-14 flex-wrap items-baseline gap-x-3 px-2" aria-hidden="true" />
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
            <Line dataKey="value" type="linear" stroke="var(--color-action)" strokeWidth={2} connectNulls={false}
              dot={{ r: 2.5, fill: "var(--color-action)", strokeWidth: 0 }} activeDot={false} isAnimationActive={false} />
            {marks.filter((mark) => mark.value == null).map((mark) => <ReferenceDot key={mark.id} x={mark.time} y={bottom} r={3}
              fill="var(--color-background)" stroke="var(--color-muted)" className="pointer-events-none" />)}
            <PlotInteraction observations={marks} markers={visibleMarkers} selected={selected} bottom={bottom} readout={readout} observationDate={observationDate} onSelect={selectObservation} />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div> : <p className="text-sm text-muted">No complete values to plot.</p>}
    {!values.length && timelineControl && <label className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-sm text-muted">
      <span>Inspect timeline</span>{timelineControl}
    </label>}
    {selection.length > 0 && <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1 border-l-2 border-line pl-3 text-sm" aria-live="polite">
      <p><time dateTime={selected.at} className="text-muted">{observationDate(selected)}</time>{" · "}<strong className="tabular-nums">{description(selected)}</strong></p>
      {selected.detail && <p className="text-muted">{selected.detail}</p>}
      {selected.href && (selected.appReceipt
        ? <Link href={selected.href} prefetch={false} className="text-action underline">{selected.receiptLabel}</Link>
        : <a href={selected.href} className="text-action underline">{selected.receiptLabel ?? "Open capture receipt"}</a>)}
    </div>}
    {markers.length > 0 && <details open className="border-t border-line pt-2 text-sm">
      <summary className="cursor-pointer text-muted">{eventsLabel} ({visibleMarkers.length} of {markers.length})</summary>
      <ol className="mt-2 max-h-56 divide-y divide-line overflow-auto">
        {[...visibleMarkers].reverse().map((event) => <li key={event.id}>
          <button type="button" onClick={() => selectObservation(event.id)} aria-pressed={event.id === selected.id}
            className="grid w-full gap-1 px-2 py-2 text-left hover:bg-panel aria-pressed:border-l-2 aria-pressed:border-action md:grid-cols-[minmax(0,1fr)_auto] md:gap-4">
            <span><span style={{ color: eventKinds[event.kind].color }}>{eventKinds[event.kind].label}</span>{" · "}{event.label}</span>
            <time dateTime={event.at} className="text-xs text-muted">{eventDate.format(event.time)}</time>
          </button>
        </li>)}
      </ol>
      {visibleMarkers.length === 0 && <p className="mt-2 text-muted">No events match these filters. Change the selected types or search.</p>}
    </details>}
  </div>;
}
