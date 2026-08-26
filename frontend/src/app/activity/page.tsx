"use client";

import { useEffect, useMemo, useState } from "react";

import AppNav from "@/components/AppNav";
import { AgentActionLogItem, apiFetch } from "@/lib/api";
import { formatUserLabel } from "@/lib/formatting";

const SOURCES = ["all", "automation", "chat", "research"] as const;
const STATUSES = ["all", "ok", "processed_with_errors", "error", "empty_result", "blocked"] as const;
type FeedItem = AgentActionLogItem & {
  displaySource: string;
  displayTitle: string;
  displayStatus: string;
  displaySummary: string;
};

function actionWhen(timestamp: string) {
  if (!timestamp) return "";
  const date = new Date(timestamp);
  return date.toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function actionAccent(status: string) {
  const normalized = status.toLowerCase();
  if (normalized.includes("error") || normalized.includes("failed")) {
    return "border-red-200 bg-red-50/70 text-red-700 dark:border-red-900 dark:bg-red-950/20 dark:text-red-300";
  }
  if (normalized.includes("blocked") || normalized.includes("empty")) {
    return "border-amber-200 bg-amber-50/70 text-amber-700 dark:border-amber-900 dark:bg-amber-950/20 dark:text-amber-300";
  }
  return "border-emerald-200 bg-emerald-50/70 text-emerald-700 dark:border-emerald-900 dark:bg-emerald-950/20 dark:text-emerald-300";
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
  if (normalized === "ok" && item.source === "automation") return "Routine";
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
      displaySummary: item.summary,
    });
  }
  return merged;
}

export default function ActivityPage() {
  const [items, setItems] = useState<AgentActionLogItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [source, setSource] = useState<(typeof SOURCES)[number]>("all");
  const [status, setStatus] = useState<(typeof STATUSES)[number]>("all");
  const [sessionId, setSessionId] = useState<string | null>(null);

  useEffect(() => {
    const searchParams = new URLSearchParams(window.location.search);
    setSessionId(searchParams.get("session_id"));
  }, []);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      setError(null);
      try {
        const params = new URLSearchParams({ limit: "120" });
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
  }, [source, status, sessionId]);

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
      <main className="mx-auto w-full max-w-[1440px] px-4 py-8 sm:px-6 lg:px-8">
        <header className="flex flex-wrap items-end justify-between gap-6">
          <div>
            <p className="text-[11px] font-semibold uppercase tracking-[0.24em] text-slate-400 dark:text-slate-500">
              Agent Activity
            </p>
            <h1 className="mt-2 text-3xl font-bold tracking-tight">What Prophet Did</h1>
            <p className="mt-2 max-w-3xl text-sm text-slate-500 dark:text-slate-400">
              A running catch-up feed of research, reasoning, automation, and system actions. This is the full activity stream,
              not just the latest dashboard summary.
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            <select
              value={source}
              onChange={(event) => setSource(event.target.value as (typeof SOURCES)[number])}
              className="rounded-full border border-slate-300 bg-white px-4 py-2 text-sm dark:border-slate-700 dark:bg-slate-950"
            >
              {SOURCES.map((value) => (
                <option key={value} value={value}>
                  {value === "all" ? "All sources" : formatUserLabel(value)}
                </option>
              ))}
            </select>
            <select
              value={status}
              onChange={(event) => setStatus(event.target.value as (typeof STATUSES)[number])}
              className="rounded-full border border-slate-300 bg-white px-4 py-2 text-sm dark:border-slate-700 dark:bg-slate-950"
            >
              {STATUSES.map((value) => (
                <option key={value} value={value}>
                  {value === "all" ? "All statuses" : formatUserLabel(value)}
                </option>
              ))}
            </select>
          </div>
        </header>

        {error ? (
          <div className="mt-8 rounded-lg border border-red-200 bg-red-50/70 px-4 py-3 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/20 dark:text-red-300">
            {error}
          </div>
        ) : null}

        {loading && items.length === 0 ? (
          <div className="mt-8 text-sm text-slate-500 dark:text-slate-400">Loading activity…</div>
        ) : null}

        <div className="mt-8 space-y-8">
          {groups.map(([day, dayItems]) => (
            <section key={day}>
              <div className="mb-3 text-[11px] font-semibold uppercase tracking-[0.24em] text-slate-400 dark:text-slate-500">
                {day}
              </div>
              <div className="space-y-3">
                {dayItems.map((item) => (
                  <article
                    key={item.id}
                    className="rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-950"
                  >
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="text-[11px] font-semibold uppercase tracking-[0.2em] text-slate-400 dark:text-slate-500">
                            {item.displaySource}
                          </span>
                          <span className="text-sm font-semibold text-slate-900 dark:text-slate-100">
                            {item.displayTitle}
                          </span>
                          <span className={`rounded-full border px-2.5 py-1 text-[10px] font-semibold uppercase tracking-wider ${actionAccent(item.status)}`}>
                            {item.displayStatus}
                          </span>
                        </div>
                        <p className="mt-2 text-sm leading-relaxed text-slate-600 dark:text-slate-300">{item.displaySummary}</p>
                        {Object.keys(item.metadata || {}).length > 0 ? (
                          <details className="mt-3 rounded-lg border border-slate-200 px-3 py-2 dark:border-slate-800">
                            <summary className="cursor-pointer text-xs font-medium text-slate-500 dark:text-slate-400">
                              View metadata
                            </summary>
                            <pre className="mt-2 overflow-x-auto whitespace-pre-wrap text-[11px] leading-relaxed text-slate-600 dark:text-slate-300">
                              {JSON.stringify(item.metadata, null, 2)}
                            </pre>
                          </details>
                        ) : null}
                      </div>
                      <div className="text-xs text-slate-400 dark:text-slate-500">{actionWhen(item.timestamp)}</div>
                    </div>
                  </article>
                ))}
              </div>
            </section>
          ))}
        </div>
      </main>
    </div>
  );
}
