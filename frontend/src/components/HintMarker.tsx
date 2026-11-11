"use client";

import { useCallback, useEffect, useId, useRef } from "react";

export default function HintMarker({
  label = "Hint",
  children,
}: {
  label?: string;
  children: React.ReactNode;
}) {
  const id = useId();
  const trigger = useRef<HTMLButtonElement>(null);
  const popup = useRef<HTMLDivElement>(null);
  const dismissTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  function keepOpen() {
    if (dismissTimer.current) clearTimeout(dismissTimer.current);
  }

  const position = useCallback(() => {
    const anchor = trigger.current?.getBoundingClientRect();
    const tooltip = popup.current;
    if (!anchor || !tooltip) return;
    const bounds = tooltip.getBoundingClientRect();
    tooltip.style.left = `${Math.max(8, Math.min(anchor.left, window.innerWidth - bounds.width - 8))}px`;
    const below = anchor.bottom + 8;
    tooltip.style.top = `${below + bounds.height <= window.innerHeight - 8 ? below : Math.max(8, anchor.top - bounds.height - 8)}px`;
  }, []);

  function show() {
    keepOpen();
    if (!popup.current?.matches(":popover-open")) popup.current?.showPopover();
    position();
  }

  function dismiss() {
    keepOpen();
    if (trigger.current === document.activeElement && trigger.current?.matches(":focus-visible")) return;
    dismissTimer.current = setTimeout(() => popup.current?.hidePopover(), 150);
  }

  useEffect(() => {
    const reposition = () => {
      if (popup.current?.matches(":popover-open")) position();
    };
    window.addEventListener("resize", reposition);
    window.addEventListener("scroll", reposition, true);
    return () => {
      if (dismissTimer.current) clearTimeout(dismissTimer.current);
      window.removeEventListener("resize", reposition);
      window.removeEventListener("scroll", reposition, true);
    };
  }, [position]);

  return (
    <span className="inline-flex align-middle">
      <button
        ref={trigger}
        type="button"
        aria-label={label}
        aria-describedby={id}
        popoverTarget={id}
        popoverTargetAction="show"
        onPointerEnter={(event) => { if (event.pointerType !== "touch") show(); }}
        onPointerLeave={dismiss}
        onFocus={() => { if (trigger.current?.matches(":focus-visible")) show(); }}
        onBlur={dismiss}
        className="inline-flex h-7 w-7 shrink-0 cursor-help items-center justify-center border border-line text-sm font-semibold text-muted hover:border-action hover:text-action focus-visible:outline-2 focus-visible:outline-action"
      >
        <span aria-hidden="true">i</span>
      </button>
      <div
        ref={popup}
        id={id}
        popover="auto"
        role="tooltip"
        onToggle={(event) => { if (event.newState === "open") position(); }}
        onPointerEnter={keepOpen}
        onPointerLeave={dismiss}
        className="fixed m-0 w-80 max-w-[calc(100vw-1rem)] border border-line bg-panel p-3 text-sm leading-relaxed text-foreground shadow-lg"
      >
        {children}
      </div>
    </span>
  );
}
