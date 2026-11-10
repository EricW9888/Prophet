"use client";

import { useState } from "react";
import Link from "next/link";
import { ArrowDown, ArrowUp, GripVertical } from "lucide-react";
import type { Position } from "@/lib/api";
import { researchHref } from "@/lib/workflows";
import { watchlistRows } from "@/lib/watchlist";
import { useWorkspacePreference } from "@/lib/workspace-preference";

export default function PortfolioWatchlist({ holdings, tracked }: { holdings: Position[]; tracked: Position[] }) {
  const preference = useWorkspacePreference("watchlist-order");
  const [editing, setEditing] = useState(false);
  const [announcement, setAnnouncement] = useState("");
  const rows = watchlistRows(holdings, tracked, preference.value);
  const holdingCount = rows.filter((row) => row.holding).length;

  function move(from: number, to: number) {
    if (from === to || from < 0 || to < 0 || to >= rows.length) return;
    const ids = rows.map(({ position }) => position.security_id);
    const [id] = ids.splice(from, 1);
    ids.splice(to, 0, id);
    preference.write(JSON.stringify(ids));
    setAnnouncement(`${rows[from].position.ticker ?? rows[from].position.entity_name ?? "Security"} moved to ${to + 1} of ${rows.length}.`);
  }

  return (
    <section className="min-w-0 border border-line bg-panel p-4 sm:p-5" aria-label="Portfolio watchlist">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h2 className="font-semibold">Watchlist <span className="ml-1 text-sm font-normal text-muted">{rows.length}</span></h2>
          <p className="mt-1 text-xs text-muted">{holdingCount} from holdings · {rows.length - holdingCount} additional tracked</p>
        </div>
        {rows.length > 1 && <button type="button" className="workspace-control" aria-pressed={editing} onClick={() => setEditing(!editing)}>{editing ? "Done" : "Reorder"}</button>}
      </div>
      <p className="mt-2 text-xs text-muted">Open holdings appear automatically. Reordering only changes this view.</p>
      {editing && preference.value && <button type="button" className="mt-2 text-xs text-action underline" onClick={() => { preference.write(""); setAnnouncement("Default order restored."); }}>Restore default order</button>}
      <span className="sr-only" role="status">{announcement}</span>
      {rows.length ? <ol className="mt-3 divide-y divide-line border-y border-line">
        {rows.map(({ position, holding, tracked: explicit }, index) => {
          const label = position.ticker ?? position.entity_name ?? "Security";
          return <li key={position.security_id} className="flex items-center gap-2 py-2"
            onDragOver={(event) => { if (editing && event.dataTransfer.types.includes("application/x-prophet-watchlist")) event.preventDefault(); }}
            onDrop={(event) => {
              if (!editing) return;
              event.preventDefault();
              const id = event.dataTransfer.getData("application/x-prophet-watchlist");
              move(rows.findIndex((row) => row.position.security_id === id), index);
            }}>
            {editing && <span draggable onDragStart={(event) => {
              event.dataTransfer.effectAllowed = "move";
              event.dataTransfer.setData("application/x-prophet-watchlist", position.security_id);
            }} className="hidden cursor-grab text-muted sm:inline-flex" aria-hidden="true"><GripVertical size={16} /></span>}
            <div className="min-w-0 flex-1">
              <Link href={researchHref({ subject_id: position.id, subject_type: "position", subject_name: label })} className="font-medium text-action hover:underline">{label}</Link>
              {position.entity_name && position.entity_name !== label && <p className="truncate text-xs text-muted" title={position.entity_name}>{position.entity_name}</p>}
            </div>
            <span className="shrink-0 text-xs text-muted">{holding ? explicit ? "Held · Tracked" : "Held" : "Tracked"}</span>
            {editing && <div className="flex shrink-0 gap-1">
              <button className="workspace-control px-1.5 disabled:opacity-35" type="button" disabled={index === 0} aria-label={`Move ${label} up`} onClick={() => move(index, index - 1)}><ArrowUp size={14} /></button>
              <button className="workspace-control px-1.5 disabled:opacity-35" type="button" disabled={index === rows.length - 1} aria-label={`Move ${label} down`} onClick={() => move(index, index + 1)}><ArrowDown size={14} /></button>
            </div>}
          </li>;
        })}
      </ol> : <p className="mt-3 text-sm text-muted">Add a holding or track a name to get started.</p>}
    </section>
  );
}
