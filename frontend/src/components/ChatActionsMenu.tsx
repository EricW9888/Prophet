"use client";

import { useEffect, useRef, useState } from "react";
import { Archive, ArchiveRestore, MoreHorizontal, Pencil } from "lucide-react";

/**
 * One "More" button per chat with its actions in a small menu, as chat
 * sidebars commonly do, so the title keeps the row's width.
 */
export default function ChatActionsMenu({ title, archived, onRename, onArchive }: {
  title: string;
  archived: boolean;
  onRename: () => void;
  onArchive: () => void;
}) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const close = (event: MouseEvent | KeyboardEvent) => {
      if (event instanceof KeyboardEvent ? event.key === "Escape" : !root.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", close);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", close);
    };
  }, [open]);

  const item = "flex w-full items-center gap-2 px-3 py-2 text-left text-sm hover:bg-panel-muted";
  return (
    <div ref={root} className="relative shrink-0">
      <button
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={`Actions for ${title}`}
        onClick={() => setOpen((value) => !value)}
        className={`mt-0.5 inline-flex h-8 w-8 items-center justify-center text-muted transition hover:bg-panel-muted hover:text-foreground ${open ? "opacity-100" : "opacity-100 lg:opacity-0 lg:focus-visible:opacity-100 lg:group-hover:opacity-100"}`}
      >
        <MoreHorizontal className="h-4 w-4" />
      </button>
      {open ? (
        <div role="menu" className="absolute right-0 top-9 z-20 w-40 border border-line bg-panel py-1 shadow-lg">
          <button type="button" role="menuitem" className={item} onClick={() => { setOpen(false); onRename(); }}>
            <Pencil className="h-4 w-4" /> Rename
          </button>
          <button type="button" role="menuitem" className={item} onClick={() => { setOpen(false); onArchive(); }}>
            {archived ? <ArchiveRestore className="h-4 w-4" /> : <Archive className="h-4 w-4" />} {archived ? "Restore" : "Archive"}
          </button>
        </div>
      ) : null}
    </div>
  );
}
