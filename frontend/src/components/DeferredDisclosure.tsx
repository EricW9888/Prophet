"use client";

import { type ReactNode, useState } from "react";

/** Native disclosure: build secondary content on first open, then preserve it. */
export default function DeferredDisclosure({
  title, children, className = "", contentClassName = "mt-3", initiallyOpen = false,
}: {
  title: ReactNode;
  children: ReactNode;
  className?: string;
  contentClassName?: string;
  initiallyOpen?: boolean;
}) {
  const [open, setOpen] = useState(initiallyOpen);
  const [visited, setVisited] = useState(initiallyOpen);
  return <details open={open} className={`min-w-0 ${className}`} onToggle={event => {
    const next = event.currentTarget.open;
    setOpen(next);
    if (next) setVisited(true);
  }}>
    <summary className="cursor-pointer text-sm font-medium leading-6">{title}</summary>
    {visited ? <div className={`min-w-0 ${contentClassName}`}>{children}</div> : null}
  </details>;
}
