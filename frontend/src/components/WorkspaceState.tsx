"use client";

import {
  AlertTriangle,
  ArrowRight,
  CircleOff,
  LoaderCircle,
  RefreshCw,
} from "lucide-react";

type WorkspaceStateKind = "loading" | "empty" | "error" | "degraded";

const stateStyle: Record<WorkspaceStateKind, string> = {
  loading: "border-line bg-panel-muted text-muted",
  empty: "border-dashed border-line-strong bg-panel text-muted",
  error:
    "border-red-300 bg-red-50 text-red-800 dark:border-red-900 dark:bg-red-950/30 dark:text-red-200",
  degraded:
    "border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-200",
};

const stateIcon = {
  loading: LoaderCircle,
  empty: CircleOff,
  error: AlertTriangle,
  degraded: AlertTriangle,
};

export default function WorkspaceState({
  kind,
  title,
  description,
  actionLabel,
  onAction,
  compact = false,
  className = "",
}: {
  kind: WorkspaceStateKind;
  title: string;
  description: string;
  actionLabel?: string;
  onAction?: () => void;
  compact?: boolean;
  className?: string;
}) {
  const Icon = stateIcon[kind];
  const ActionIcon = kind === "error" || kind === "degraded" ? RefreshCw : ArrowRight;

  return (
    <section
      className={`flex ${compact ? "min-h-24" : "min-h-36"} min-w-0 items-start gap-3 border px-4 py-5 sm:px-5 ${stateStyle[kind]} ${className}`}
      role={kind === "error" ? "alert" : "status"}
      aria-live={kind === "error" ? "assertive" : "polite"}
      aria-busy={kind === "loading"}
    >
      <Icon
        className={`mt-0.5 h-5 w-5 shrink-0 ${kind === "loading" ? "animate-spin" : ""}`}
        aria-hidden="true"
      />
      <div className="min-w-0 flex-1">
        <h2 className="text-sm font-semibold text-current">{title}</h2>
        <p className="mt-1 max-w-3xl text-sm leading-6 text-current opacity-80">
          {description}
        </p>
        {actionLabel && onAction ? (
          <button
            type="button"
            onClick={onAction}
            className="mt-3 inline-flex items-center gap-2 border border-current/25 px-3 py-2 text-sm font-medium transition-colors hover:bg-black/5 dark:hover:bg-white/10"
          >
            <ActionIcon className="h-4 w-4" aria-hidden="true" />
            {actionLabel}
          </button>
        ) : null}
      </div>
    </section>
  );
}
