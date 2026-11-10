"use client";

import Link from "next/link";
import { useState, type MouseEvent } from "react";
import { safeFormatCurrency } from "@/lib/formatting";

type ReceiptLink = { href?: string; receiptLabel?: string; appReceipt?: boolean };
export type ValuePoint = ReceiptLink & { id: string; at: string; value: number | null; detail?: string };
export type ChartEvent = ReceiptLink & { id: string; at: string; label: string; detail?: string; kind: "fill" | "decision" | "ledger" };

function timeLabel(time: number, compact = false) {
  return new Intl.DateTimeFormat(undefined, compact ? { month: "short", day: "numeric" } : { dateStyle: "medium", timeStyle: "short" }).format(time);
}

export default function CapturedValueChart({ label, points, events = [], eventsLabel = "Actions and decisions" }: { label: string; points: ValuePoint[]; events?: ChartEvent[]; eventsLabel?: string }) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const dated = points.map((point) => ({ ...point, time: Date.parse(point.at) })).filter((point) => Number.isFinite(point.time)).sort((a, b) => a.time - b.time);
  const datedEvents = events.map((event) => ({ ...event, time: Date.parse(event.at) })).filter((event) => Number.isFinite(event.time)).sort((a, b) => a.time - b.time);
  const selection = [...dated, ...datedEvents].sort((a, b) => a.time - b.time);
  const selected = selection.find((item) => item.id === selectedId) ?? dated.at(-1) ?? selection.at(-1);
  const hovered = selection.find((item) => item.id === hoveredId);
  const values = dated.flatMap((point) => point.value != null && Number.isFinite(point.value) ? [point.value] : []);
  if (!selected) return <p className="text-sm text-muted">No dated observations yet.</p>;
  const start = selection[0].time;
  const end = selection.at(-1)!.time;
  const sameDay = new Date(start).toDateString() === new Date(end).toDateString();
  const axisLabel = (time: number) => sameDay && start !== end
    ? new Intl.DateTimeFormat(undefined, { timeStyle: "short" }).format(time) : timeLabel(time, true);
  const min = values.length ? Math.min(...values) : 0;
  const max = values.length ? Math.max(...values) : 0;
  const span = max - min || Math.max(Math.abs(max) * 0.02, 1);
  const pad = span * 0.12;
  const x = (time: number) => end === start ? 292 : 16 + (time - start) / (end - start) * 552;
  const y = (value: number) => 139 - (value - min + pad) / (max - min + 2 * pad) * 126;
  const active = hovered ?? selected;
  const description = (item: typeof selected) => "value" in item
    ? item.value == null || !Number.isFinite(item.value) ? "Value unavailable" : safeFormatCurrency(item.value, 2)
    : item.label;
  const activePercent = x(active.time) / 584 * 100;

  function observationAt(event: MouseEvent<SVGSVGElement>) {
    const mark = (event.target as Element).closest("[data-observation]")?.getAttribute("data-observation");
    if (mark) return mark;
    const bounds = event.currentTarget.getBoundingClientRect();
    if (!bounds.width) return active.id;
    const plotX = (event.clientX - bounds.left) * 584 / bounds.width;
    return selection.reduce((nearest, item) =>
      Math.abs(x(item.time) - plotX) < Math.abs(x(nearest.time) - plotX) ? item : nearest).id;
  }

  function selectObservation(id: string) {
    setHoveredId(null);
    setSelectedId(id);
  }

  const timelineControl = selection.length > 1 && <input type="range" min={0} max={selection.length - 1} step={1} value={selection.indexOf(selected)} aria-label={`Inspect ${label.toLowerCase()} timeline`} aria-valuetext={`${timeLabel(selected.time)} · ${description(selected)}`} onChange={(event) => selectObservation(selection[Number(event.target.value)].id)} className={values.length ? "sr-only" : "min-w-20 flex-1 accent-current"} />;
  const path = dated.map((point, index) => {
    if (point.value == null || !Number.isFinite(point.value)) return "";
    const prior = dated[index - 1]?.value;
    const command = prior != null && Number.isFinite(prior) ? "L" : "M";
    return `${command} ${x(point.time)} ${y(point.value)}`;
  }).join(" ");

  return <div className="min-w-0 space-y-2">
    {values.length ? (
      <div className="grid grid-cols-[auto_minmax(0,1fr)] gap-2 border border-line bg-background px-2 py-2">
        <div className="flex flex-col justify-between py-2 text-right text-xs tabular-nums text-muted" aria-hidden="true">
          <span>{safeFormatCurrency(max + pad)}</span><span>{safeFormatCurrency(min - pad)}</span>
        </div>
        <div className="relative min-w-0 focus-within:outline-2 focus-within:outline-action">
          {timelineControl}
          <svg viewBox="0 0 584 156" preserveAspectRatio="none" className="block h-40 w-full cursor-crosshair text-action" aria-hidden="true"
            onPointerMove={(event) => {
              if (event.pointerType !== "touch") setHoveredId(observationAt(event));
            }}
            onPointerLeave={() => setHoveredId(null)}
            onClick={(event) => selectObservation(observationAt(event))}>
            {[13, 76, 139].map((grid) => <path key={grid} d={`M 16 ${grid} H 568`} className="stroke-line" vectorEffect="non-scaling-stroke" />)}
            <path d={`M ${x(active.time)} 13 V 145`} className="stroke-muted" strokeDasharray="3 3" vectorEffect="non-scaling-stroke" />
            <path d={path} fill="none" stroke="currentColor" strokeWidth="2" vectorEffect="non-scaling-stroke" />
            {dated.filter((point) => point.value != null && Number.isFinite(point.value)).map((point) => (
              <circle key={point.id} data-observation={point.id} cx={x(point.time)} cy={y(point.value!)} r={point.id === active.id ? 5 : 3.25} fill="currentColor" />
            ))}
            {datedEvents.map((event) => {
              const tick = `M ${x(event.time)} 130 V 151`;
              return <g key={event.id} data-observation={event.id}>
                <path d={tick} stroke="transparent" strokeWidth="12" vectorEffect="non-scaling-stroke" />
                <path d={tick} strokeWidth={event.id === active.id ? 5 : 2} vectorEffect="non-scaling-stroke" className={event.kind === "fill" ? "stroke-emerald-500" : "stroke-amber-500"} />
              </g>;
            })}
          </svg>
          {hovered && <div aria-hidden="true" className="pointer-events-none absolute top-2 z-10 max-w-[min(14rem,48%)] border border-line bg-panel px-2 py-1 text-xs shadow-sm"
            style={activePercent > 50 ? { right: `calc(${100 - activePercent}% + 6px)` } : { left: `calc(${activePercent}% + 6px)` }}>
            <time dateTime={hovered.at} className="block text-muted">{timeLabel(hovered.time)}</time>
            <strong className="block break-words tabular-nums">{description(hovered)}</strong>
          </div>}
        </div>
        <span />
        <div className="flex justify-between px-2 text-xs text-muted"><span>{axisLabel(start)}</span><span>{end !== start ? axisLabel(end) : ""}</span></div>
      </div>
    ) : <p className="text-sm text-muted">No complete values to plot.</p>}
    {!values.length && timelineControl && <label className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
      <span>Inspect timeline</span>
      {timelineControl}
    </label>}
    <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 border-l-2 border-line pl-3 text-sm" aria-live="polite">
      <p><time dateTime={selected.at} className="text-muted">{timeLabel(selected.time)}</time>{" · "}<strong>{description(selected)}</strong></p>
      {selected.detail && <p className="text-xs text-muted">{selected.detail}</p>}
      {selected.href && (selected.appReceipt
        ? <Link href={selected.href} prefetch={false} className="text-xs text-action underline">{selected.receiptLabel}</Link>
        : <a href={selected.href} className="text-xs text-action underline">{selected.receiptLabel ?? "Open capture receipt"}</a>)}
    </div>
    {datedEvents.length > 0 && <details className="border-t border-line pt-2 text-xs">
      <summary className="cursor-pointer text-muted">{eventsLabel} ({datedEvents.length})</summary>
      <ol className="mt-2 max-h-56 divide-y divide-line overflow-auto">
        {[...datedEvents].reverse().map((event) => <li key={event.id}><button type="button" onClick={() => selectObservation(event.id)} aria-pressed={event.id === selected.id} className="w-full px-2 py-2 text-left hover:bg-panel aria-pressed:border-l-2 aria-pressed:border-action"><span className={event.kind === "fill" ? "text-emerald-500" : "text-amber-500"}>{event.kind === "fill" ? "Fill" : event.kind === "ledger" ? "Ledger" : "Decision"}</span>{" · "}{event.label}<span className="ml-2 text-muted">{timeLabel(event.time)}</span></button></li>)}
      </ol>
    </details>}
  </div>;
}
