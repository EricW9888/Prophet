"use client";

import {
  AlertTriangle,
  ArrowRight,
  CircleOff,
  LoaderCircle,
  RefreshCw,
} from "lucide-react";
import type { WorkspaceReadState } from "@/lib/workspace-reads";

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

export function WorkspaceRead({
  name, state, retry, children, placeholder,
}: {
  name: string;
  state?: WorkspaceReadState;
  retry: () => void;
  children: React.ReactNode;
  placeholder?: React.ReactNode;
}) {
  const pending = Boolean(state?.pending);
  return (
    <div className="min-w-0 space-y-3">
      {!state?.ready && !state?.error ? (
        placeholder ?? <WorkspaceSkeleton label={`Loading ${name}`} />
      ) : null}
      {state?.error ? (
        <WorkspaceState
          compact
          kind={state.ready ? "degraded" : "error"}
          title={state.ready ? `Could not refresh ${name}` : `Could not load ${name}`}
          description={state.ready ? `${state.error} Showing the last loaded data.` : state.error}
          actionLabel={pending ? `Retrying ${name}…` : `Retry ${name}`}
          onAction={retry}
          actionDisabled={pending}
        />
      ) : null}
      {state?.ready ? children : null}
    </div>
  );
}

/** Reserve space for content. Keep placeholder graphics out of the accessibility tree. */
export function WorkspaceSkeleton({ label, layout = "rows", rows = 3 }: {
  label?: string;
  layout?: "rows" | "chart";
  rows?: number;
}) {
  return <div className="min-w-0">
    {label && <p role="status" className="mb-2 text-sm text-muted">{label}…</p>}
    {layout === "chart" ? (
      <div aria-hidden="true" className="min-w-0 space-y-3 motion-safe:animate-pulse">
        <div className="h-4 w-2/5 bg-panel-muted" />
        <div className="min-w-0 border border-line bg-background px-2 pb-1 pt-3 sm:px-3">
          <div className="mb-2 flex min-h-14 items-center px-2"><span className="h-5 w-1/4 bg-panel-muted" /></div>
          <div className="workspace-chart-plot flex flex-col justify-between pb-5 pt-3">
            {Array.from({ length: 4 }, (_, index) => <span key={index} className="border-t border-line" />)}
          </div>
        </div>
        <div className="h-4 w-1/3 bg-panel-muted" />
      </div>
    ) : (
      <div aria-hidden="true" className="divide-y divide-line motion-safe:animate-pulse">
        {Array.from({ length: rows }, (_, index) => <div key={index} className="min-w-0 space-y-2 py-3">
          <div className="h-4 w-2/5 bg-panel-muted" />
          <div className="h-3 w-3/5 bg-panel-muted" />
        </div>)}
      </div>
    )}
  </div>;
}

export default function WorkspaceState({
  kind,
  title,
  description,
  actionLabel,
  onAction,
  actionDisabled = false,
  compact = false,
  className = "",
}: {
  kind: WorkspaceStateKind;
  title: string;
  description: string;
  actionLabel?: string;
  onAction?: () => void;
  actionDisabled?: boolean;
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
        className={`mt-0.5 h-5 w-5 shrink-0 ${kind === "loading" ? "motion-safe:animate-spin" : ""}`}
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
            disabled={actionDisabled}
            className="mt-3 inline-flex items-center gap-2 border border-current/25 px-3 py-2 text-sm font-medium transition-colors hover:bg-black/5 disabled:opacity-60 dark:hover:bg-white/10"
          >
            <ActionIcon className="h-4 w-4" aria-hidden="true" />
            {actionLabel}
          </button>
        ) : null}
      </div>
    </section>
  );
}
