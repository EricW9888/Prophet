"use client";

import { Suspense, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";

import AppNav from "@/components/AppNav";
import PageHeader from "@/components/PageHeader";
import WorkspaceState, { WorkspaceSkeleton } from "@/components/WorkspaceState";
import { AgentActionLogItem, apiFetch } from "@/lib/api";
import { activityStatus, activityTrigger, groupRepeats, type Tone } from "@/lib/activity-feed";
import { formatUserLabel, readableMachineText } from "@/lib/formatting";

const SOURCES = ["all", "automation", "chat", "research"] as const;
const STATUSES = ["all", "ok", "processed_with_errors", "deferred", "rejected", "skipped", "no_new_knowledge", "no_new_evidence", "error", "empty_result", "blocked"] as const;
type FeedItem = AgentActionLogItem & {
  displaySource: string;
  displayTitle: string;
  displayStatus: string;
  displaySummary: string;
};

const TONE_DOT: Record<Tone, string> = {
  positive: "bg-positive",
  caution: "bg-caution",
  negative: "bg-negative",
  neutral: "bg-muted",
};

function timeOfDay(timestamp: string) {
  return new Date(timestamp).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function cleanResearchTitle(title: string | null | undefined) {
  return (title || "")
    .replace("Research on: ", "")
    .replace("Ad hoc portfolio research: ", "")
    .trim();
}

function statusLabel(item: AgentActionLogItem) {
  const normalized = item.status.toLowerCase();
  if (normalized === "empty_result") return "No usable result";
  if (normalized === "processed_with_errors") return "Partial success";
  // "Routine" is reserved for the refreshes the routine filter hides (price, risk, backup).
  if (normalized === "ok" && item.source === "automation") return "Completed";
  if (normalized === "blocked") return "Blocked";
  if (normalized.includes("error") || normalized.includes("failed")) return "Error";
  return formatUserLabel(item.status);
}

function actionTitle(item: AgentActionLogItem) {
  if (item.subject_name) return item.subject_name;
  if (item.source === "research" && typeof item.metadata?.title === "string") {
    return cleanResearchTitle(item.metadata.title);
  }
  if (item.source === "research" && typeof item.metadata?.query === "string") {
    return String(item.metadata.query);
  }
  if (item.action_type === "research_loop" && typeof item.metadata?.question_id === "string") {
    return "Research loop";
  }
  return formatUserLabel(item.action_type);
}

function isResearchMiss(item: AgentActionLogItem) {
  return item.status === "empty_result" && ["research_loop", "external_research"].includes(item.action_type);
}

function questionId(item: AgentActionLogItem) {
  return typeof item.metadata?.question_id === "string" ? item.metadata.question_id : null;
}

function mergeRelatedActions(items: AgentActionLogItem[]): FeedItem[] {
  const merged: FeedItem[] = [];
  for (let index = 0; index < items.length; index += 1) {
    const item = items[index];
    const next = items[index + 1];
    if (
      next &&
      isResearchMiss(item) &&
      isResearchMiss(next) &&
      questionId(item) &&
      questionId(item) === questionId(next)
    ) {
      const query =
        (typeof item.metadata?.query === "string" && item.metadata.query) ||
        (typeof next.metadata?.query === "string" && next.metadata.query) ||
        cleanResearchTitle(typeof item.metadata?.title === "string" ? item.metadata.title : "") ||
        cleanResearchTitle(typeof next.metadata?.title === "string" ? next.metadata.title : "") ||
        "research question";
      merged.push({
        ...item,
        id: `${item.id}:${next.id}`,
        source: "research",
        action_type: "research_attempt",
        displaySource: "Research",
        displayTitle: query,
        displayStatus: "No usable result",
        displaySummary: `A research pass for ${query} ran but did not return usable source content. The question is still open for a better query or source path.`,
        metadata: {
          ...item.metadata,
          paired_actions: [item.action_type, next.action_type],
        },
      });
      index += 1;
      continue;
    }
    merged.push({
      ...item,
      displaySource: formatUserLabel(item.source),
      displayTitle: actionTitle(item),
      displayStatus: statusLabel(item),
      displaySummary: readableMachineText(item.summary),
    });
  }
  return merged;
}

export default function ActivityPage() {
  return <Suspense fallback={<><AppNav active="activity" /><main className="workspace-frame py-8"><WorkspaceSkeleton label="Loading activity" rows={4} /></main></>}><ActivityWorkspace /></Suspense>;
}

function ActivityWorkspace() {
  const [items, setItems] = useState<AgentActionLogItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [source, setSource] = useState<(typeof SOURCES)[number]>("all");
  const [status, setStatus] = useState<(typeof STATUSES)[number]>("all");
  const [includeRoutine, setIncludeRoutine] = useState(false);
  // A link from a chat session narrows the feed to that session's actions.
  const sessionId = useSearchParams().get("session_id");

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      setError(null);
      try {
        const params = new URLSearchParams({ limit: "120", include_routine: String(includeRoutine) });
        if (source !== "all") params.set("source", source);
        if (status !== "all") params.set("status", status);
        if (sessionId) params.set("session_id", sessionId);
        const result = await apiFetch<AgentActionLogItem[]>(`/activity/agent?${params.toString()}`);
        if (!cancelled) {
          setItems(result);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Unable to load activity.");
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    }
    void load();
    const interval = window.setInterval(load, 8000);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [source, status, sessionId, includeRoutine]);

  const normalizedItems = useMemo(() => mergeRelatedActions(items), [items]);

  const groups = useMemo(() => {
    const byDay = new Map<string, FeedItem[]>();
    for (const item of normalizedItems) {
      const key = new Date(item.timestamp).toLocaleDateString([], {
        month: "short",
        day: "numeric",
        year: "numeric",
      });
      const current = byDay.get(key) ?? [];
      current.push(item);
      byDay.set(key, current);
    }
    return Array.from(byDay.entries());
  }, [normalizedItems]);

  return (
    <div className="min-h-screen bg-background text-foreground">
      <AppNav active="activity" />
      <main className="workspace-frame py-8">
        <PageHeader
          eyebrow="System activity"
          title="What Prophet did"
          description="Catch up on research, actions and failures. Routine price, risk and backup refreshes are available when you need them."
          actions={(
            <>
            <select
              aria-label="Activity source"
              value={source}
              onChange={(event) => setSource(event.target.value as (typeof SOURCES)[number])}
              className="rounded-none border border-slate-300 bg-white px-4 py-2 text-sm dark:border-slate-700 dark:bg-slate-950"
            >
              {SOURCES.map((value) => (
                <option key={value} value={value}>
                  {value === "all" ? "All sources" : formatUserLabel(value)}
                </option>
              ))}
            </select>
            <select
              aria-label="Activity status"
              value={status}
              onChange={(event) => setStatus(event.target.value as (typeof STATUSES)[number])}
              className="rounded-none border border-slate-300 bg-white px-4 py-2 text-sm dark:border-slate-700 dark:bg-slate-950"
            >
              {STATUSES.map((value) => (
                <option key={value} value={value}>
                  {value === "all" ? "All statuses" : formatUserLabel(value)}
                </option>
              ))}
            </select>
            <label className="inline-flex items-center gap-2 px-2 text-sm text-muted">
              <input type="checkbox" checked={includeRoutine} onChange={(event) => setIncludeRoutine(event.target.checked)} />
              Include routine refreshes
            </label>
            </>
          )}
        />

        {error && items.length > 0 ? (
          <WorkspaceState
            kind="degraded"
            title="Activity refresh is delayed"
            description={`${error} The last successfully loaded activity remains visible, and Prophet will retry automatically.`}
            compact
            className="mt-8"
          />
        ) : null}

        {loading && items.length === 0 ? (
          <WorkspaceState
            kind="loading"
            title="Loading recent activity"
            description="Prophet is collecting dated research, reasoning, and automation actions."
            className="mt-8 min-h-48"
          />
        ) : error && items.length === 0 ? (
          <WorkspaceState
            kind="error"
            title="Activity unavailable"
            description={`${error} Prophet will retry automatically.`}
            className="mt-8 min-h-48"
          />
        ) : groups.length === 0 ? (
          <WorkspaceState
            kind="empty"
            title="No activity matches this view"
            description="Change the source or status filters, or return after Prophet has completed more work."
            className="mt-8 min-h-48"
          />
        ) : null}

        <div className="mt-8 space-y-8">
          {groups.map(([day, dayItems]) => (
            <section key={day} aria-label={day}>
              <h2 className="mb-2 text-sm font-semibold text-foreground">{day}</h2>
              <ul className="divide-y divide-line border-y border-line">
                {groupRepeats(dayItems, (item) => [item.source, item.action_type, item.status, item.displayTitle, item.displaySummary].join("|")).map(({ item, count, earliest }) => {
                  const state = activityStatus(item.status);
                  const trigger = activityTrigger(item.metadata?.automation_trigger);
                  return (
                    <li key={item.id} className="py-2">
                      <details>
                        <summary className="grid cursor-pointer list-none grid-cols-[4.25rem_minmax(0,1fr)] gap-x-3 gap-y-0.5 text-sm sm:grid-cols-[4.25rem_minmax(0,15rem)_minmax(0,1fr)] [&::-webkit-details-marker]:hidden">
                          <time dateTime={item.timestamp} className="text-xs leading-5 text-muted tabular-nums">{timeOfDay(item.timestamp)}</time>
                          <span className="min-w-0 truncate font-medium">{item.displayTitle}</span>
                          <span className="col-start-2 flex min-w-0 items-center gap-2 text-muted sm:col-start-3">
                            <span aria-hidden="true" className={`h-2 w-2 shrink-0 rounded-full ${TONE_DOT[state.tone]}`} />
                            <span className="shrink-0">{state.label}</span>
                            <span className="min-w-0 truncate">{item.displaySummary}</span>
                            {count > 1 ? <span className="shrink-0 tabular-nums">×{count}</span> : null}
                          </span>
                        </summary>
                        <div className="mt-2 space-y-2 text-sm sm:pl-[5.5rem]">
                          <p className="leading-relaxed text-foreground">{item.displaySummary}</p>
                          <p className="text-xs text-muted">
                            {item.displaySource}
                            {trigger ? ` · ran because ${trigger}` : ""}
                            {count > 1 ? ` · happened ${count} times between ${timeOfDay(earliest)} and ${timeOfDay(item.timestamp)}` : ""}
                          </p>
                          {Object.keys(item.metadata || {}).length > 0 ? (
                            <details>
                              <summary className="cursor-pointer text-xs text-action">Technical details</summary>
                              <pre className="mt-2 overflow-x-auto whitespace-pre-wrap text-xs leading-relaxed text-muted">
                                {JSON.stringify(item.metadata, null, 2)}
                              </pre>
                            </details>
                          ) : null}
                        </div>
                      </details>
                    </li>
                  );
                })}
              </ul>
            </section>
          ))}
        </div>
      </main>
    </div>
  );
}
