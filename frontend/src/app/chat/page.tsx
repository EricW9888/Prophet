"use client";

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { Check, ChevronDown, PanelLeftOpen, Pencil, X } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import AppNav from "@/components/AppNav";
import SourceProvenanceLinks from "@/components/SourceProvenanceLinks";
import {
  API_BASE,
  AgentConversationHistory,
  AgentConversationList,
  AgentConversationSummary,
  AgentResolve,
  AgentTurnJob,
  AgentTurnJobList,
  AgentTurn,
  ActiveWatcher,
  apiFetch,
  ReasoningTrace,
} from "@/lib/api";

import { formatModelUsedLabel, formatUserLabel } from "@/lib/formatting";

const ACTIVE_JOB_STORAGE_KEY = "prophet:active-chat-job";
const ALLOW_ACTIONS_STORAGE_KEY = "prophet:allow-agent-actions";
const WATCHERS_COLLAPSED_STORAGE_KEY = "prophet:research-watches-collapsed";
const ACTIVE_JOBS_POLL_ACTIVE_MS = 5000;
const ACTIVE_JOBS_POLL_IDLE_MS = 15000;
const ACTIVE_JOBS_POLL_HIDDEN_MS = 60000;

type Message = {
  role: "user" | "assistant" | "system";
  content: string;
  createdAt?: string;
  messageKind?: string;
  isArtifact?: boolean;
  meta?: {
    process_mode?: string | null;
    resolution_reason?: string | null;
    stance?: string | null;
    confidence_band?: string | null;
    thesis_summary?: string | null;
    rationale_summary?: string | null;
    source_feedback_influence?: AgentTurn["source_feedback_influence"];
    historical_analogy_lenses?: AgentTurn["historical_analogy_lenses"];
    actions?: AgentTurn["actions"];
    reasoning_run_id?: string | null;
    subagents?: Record<string, string> | null;
  };
};

function isActiveTurnJob(job: Pick<AgentTurnJob, "status">) {
  return job.status === "queued" || job.status === "running";
}

function stripInternalReasoningBlock(content: string) {
  return content.replace(/\n\n---\n\*\*CHAIN OF THOUGHT\*\*[\s\S]*$/i, "").trim();
}

function formatTimestamp(value?: string | null) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(date);
}

function formatCountdown(seconds?: number | null) {
  if (seconds == null) return null;
  if (seconds <= 0) return "Due now";
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days > 0) return `${days}d ${hours}h left`;
  if (hours > 0) return `${hours}h ${minutes}m left`;
  return `${Math.max(1, minutes)}m left`;
}

function watchesStartCollapsed() {
  if (typeof window === "undefined") return false;
  try {
    return window.localStorage.getItem(WATCHERS_COLLAPSED_STORAGE_KEY) === "true";
  } catch {
    return false;
  }
}

type StructuredReasoningSummary = {
  stance: string | null;
  confidenceBand: string | null;
  thesisSummary: string | null;
  reasoningText: string | null;
  strengthen: string[];
  falsify: string[];
  contradictions: string[];
  gapFlags: string[];
  retrievalLayers: string[];
  researchPlan: {
    objective: string;
    mode: string;
    retrievalQuery: string;
    informationNeeds: string[];
    externalRequired: boolean;
    portfolioRole: string;
    reason: string;
  } | null;
  answerability: {
    status: string;
    reason: string;
    missingInformation: string[];
  } | null;
  corroboration: {
    status: string;
    independentSources: number;
    duplicateCopies: number;
    canPromote: boolean;
    blockedReason: string | null;
    assertions: Array<{
      statement: string;
      status: string;
      evidenceBasis: string;
      sourceCount: number;
      contradictionCount: number;
      contextPathCount: number;
    }>;
    assumptions: Array<{ statement: string; status: string; falsifier: string | null }>;
  } | null;
  alternativeHypotheses: Array<{ hypothesis: string; decisiveTest: string }>;
  independentReview: {
    stance: string;
    summary: string;
    disagrees: boolean;
    answerability: string;
    answerabilityReason: string;
  } | null;
};

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function asRecordArray(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value)
    ? value.map(asRecord).filter((item): item is Record<string, unknown> => item !== null)
    : [];
}

function summarizeReasoningTrace(trace?: ReasoningTrace): StructuredReasoningSummary {
  const structured = trace?.structured_output_json ?? {};
  const corroboration = asRecord(structured.corroboration);
  const independentReview = asRecord(structured.independent_review);
  const researchPlan = asRecord(structured.research_plan);
  const answerability = asRecord(structured.answerability);
  return {
    stance: typeof structured.stance === "string" ? structured.stance : null,
    confidenceBand:
      typeof structured.confidence_band === "string" ? structured.confidence_band : null,
    thesisSummary:
      typeof structured.thesis_summary === "string" ? structured.thesis_summary : null,
    reasoningText: typeof structured.reasoning === "string" ? structured.reasoning : null,
    strengthen: Array.isArray(structured.what_would_strengthen)
      ? structured.what_would_strengthen.filter((item): item is string => typeof item === "string")
      : [],
    falsify: Array.isArray(structured.what_would_falsify)
      ? structured.what_would_falsify.filter((item): item is string => typeof item === "string")
      : [],
    contradictions: Array.isArray(structured.active_contradictions)
      ? structured.active_contradictions.filter((item): item is string => typeof item === "string")
      : [],
    gapFlags: trace?.evidence_packet?.gap_flags ?? [],
    retrievalLayers: trace?.evidence_packet?.retrieval_layers_used ?? [],
    researchPlan: researchPlan
      ? {
          objective:
            typeof researchPlan.research_objective === "string"
              ? researchPlan.research_objective
              : "",
          mode:
            typeof researchPlan.research_mode === "string"
              ? researchPlan.research_mode
              : "stored_context",
          retrievalQuery:
            typeof researchPlan.retrieval_query === "string"
              ? researchPlan.retrieval_query
              : "",
          informationNeeds: Array.isArray(researchPlan.information_needs)
            ? researchPlan.information_needs.filter(
                (item): item is string => typeof item === "string",
              )
            : [],
          externalRequired: researchPlan.external_research_required === true,
          portfolioRole:
            typeof researchPlan.portfolio_context_role === "string"
              ? researchPlan.portfolio_context_role
              : "not_needed",
          reason: typeof researchPlan.reason === "string" ? researchPlan.reason : "",
        }
      : null,
    answerability: answerability
      ? {
          status:
            typeof answerability.status === "string" ? answerability.status : "unknown",
          reason:
            typeof answerability.reason === "string" ? answerability.reason : "",
          missingInformation: Array.isArray(answerability.missing_information)
            ? answerability.missing_information.filter(
                (item): item is string => typeof item === "string",
              )
            : [],
        }
      : null,
    corroboration: corroboration
      ? {
          status:
            typeof corroboration.status === "string" ? corroboration.status : "unknown",
          independentSources:
            typeof corroboration.independent_supporting_source_count === "number"
              ? corroboration.independent_supporting_source_count
              : 0,
          duplicateCopies:
            typeof corroboration.duplicate_copy_count === "number"
              ? corroboration.duplicate_copy_count
              : 0,
          canPromote: corroboration.can_promote === true,
          blockedReason:
            typeof structured.state_update_blocked_reason === "string"
              ? structured.state_update_blocked_reason
              : null,
          assertions: asRecordArray(corroboration.assertions).map((item) => ({
            statement: typeof item.statement === "string" ? item.statement : "Unlabeled assertion",
            status: typeof item.status === "string" ? item.status : "unknown",
            evidenceBasis:
              typeof item.evidence_basis === "string"
                ? item.evidence_basis
                : "retrieved_source",
            sourceCount:
              typeof item.independent_supporting_source_count === "number"
                ? item.independent_supporting_source_count
                : 0,
            contradictionCount:
              typeof item.independent_contradicting_source_count === "number"
                ? item.independent_contradicting_source_count
                : 0,
            contextPathCount: Array.isArray(item.valid_supporting_context_paths)
              ? item.valid_supporting_context_paths.length
              : 0,
          })),
          assumptions: asRecordArray(corroboration.material_assumptions).map((item) => ({
            statement: typeof item.statement === "string" ? item.statement : "Unlabeled assumption",
            status: typeof item.status === "string" ? item.status : "unknown",
            falsifier: typeof item.falsifier === "string" ? item.falsifier : null,
          })),
        }
      : null,
    alternativeHypotheses: asRecordArray(structured.alternative_hypotheses).map((item) => ({
      hypothesis: typeof item.hypothesis === "string" ? item.hypothesis : "Alternative explanation",
      decisiveTest: typeof item.decisive_test === "string" ? item.decisive_test : "",
    })),
    independentReview: independentReview
      ? {
          stance:
            typeof independentReview.candidate_stance === "string"
              ? independentReview.candidate_stance
              : "uncertain",
          summary:
            typeof independentReview.summary === "string" ? independentReview.summary : "",
          disagrees: independentReview.stance_disagrees === true,
          answerability:
            typeof independentReview.question_answerability === "string"
              ? independentReview.question_answerability
              : "unknown",
          answerabilityReason:
            typeof independentReview.answerability_reason === "string"
              ? independentReview.answerability_reason
              : "",
        }
      : null,
  };
}

function CorroborationPanel({ summary }: { summary: StructuredReasoningSummary }) {
  const assessment = summary.corroboration;
  if (!assessment) return null;
  const tone = assessment.canPromote
    ? "border-emerald-300 bg-emerald-50/60 text-emerald-950 dark:border-emerald-900 dark:bg-emerald-950/20 dark:text-emerald-100"
    : assessment.status === "disputed" || assessment.status === "analyst_disagreement"
      ? "border-rose-300 bg-rose-50/60 text-rose-950 dark:border-rose-900 dark:bg-rose-950/20 dark:text-rose-100"
      : "border-amber-300 bg-amber-50/60 text-amber-950 dark:border-amber-900 dark:bg-amber-950/20 dark:text-amber-100";

  return (
    <div className={`rounded-lg border p-3 ${tone}`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs font-medium uppercase tracking-wider">Evidence confidence</p>
        <span className="rounded-full border border-current/25 px-2 py-1 text-[10px] font-medium uppercase">
          {formatUserLabel(assessment.status)}
        </span>
      </div>
      <p className="mt-2 text-sm">
        {assessment.independentSources} independent source{assessment.independentSources === 1 ? "" : "s"}
        {assessment.duplicateCopies > 0
          ? ` · ${assessment.duplicateCopies} copied or repeated item${assessment.duplicateCopies === 1 ? "" : "s"} ignored`
          : ""}
        {assessment.blockedReason ? " · accepted state unchanged" : ""}
      </p>
      {assessment.assertions.length > 0 ? (
        <div className="mt-3 space-y-2">
          {assessment.assertions.slice(0, 5).map((assertion, index) => (
            <div key={`${assertion.statement}-${index}`} className="border-t border-current/15 pt-2 text-sm">
              <div className="flex flex-wrap justify-between gap-2">
                <p className="font-medium">{assertion.statement}</p>
                <span className="text-xs uppercase opacity-75">{formatUserLabel(assertion.status)}</span>
              </div>
              <p className="mt-1 text-xs opacity-75">
                {assertion.evidenceBasis === "portfolio_ledger"
                  ? `${assertion.contextPathCount} account-data reference${assertion.contextPathCount === 1 ? "" : "s"}`
                  : assertion.evidenceBasis === "market_data_calculation"
                    ? `${assertion.contextPathCount} calculation input reference${assertion.contextPathCount === 1 ? "" : "s"}`
                    : `${assertion.sourceCount} independent support`}
                {` · ${assertion.contradictionCount} contradiction${assertion.contradictionCount === 1 ? "" : "s"}`}
              </p>
            </div>
          ))}
        </div>
      ) : null}
      {assessment.assumptions.length > 0 ? (
        <div className="mt-3 border-t border-current/15 pt-2">
          <p className="text-xs font-medium uppercase tracking-wider">Load-bearing assumptions</p>
          {assessment.assumptions.slice(0, 4).map((assumption, index) => (
            <div key={`${assumption.statement}-${index}`} className="mt-2 text-sm">
              <p>{assumption.statement}</p>
              <p className="mt-1 text-xs opacity-75">
                {formatUserLabel(assumption.status)}
                {assumption.falsifier ? ` · Falsifier: ${assumption.falsifier}` : ""}
              </p>
            </div>
          ))}
        </div>
      ) : null}
      {summary.independentReview ? (
        <div className="mt-3 border-t border-current/15 pt-2 text-sm">
          <p className="text-xs font-medium uppercase tracking-wider">
            Independent review · {formatUserLabel(summary.independentReview.stance)}
            {summary.independentReview.disagrees ? " · disagrees" : ""}
            {summary.independentReview.answerability !== "unknown"
              ? ` · ${formatUserLabel(summary.independentReview.answerability)} scope`
              : ""}
          </p>
          {summary.independentReview.summary ? (
            <p className="mt-1 opacity-80">{summary.independentReview.summary}</p>
          ) : null}
          {summary.independentReview.answerabilityReason ? (
            <p className="mt-1 opacity-80">
              Scope check: {summary.independentReview.answerabilityReason}
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function LiveWatchers() {
  const [watchers, setWatchers] = useState<ActiveWatcher[]>([]);
  const [collapsed, setCollapsed] = useState(watchesStartCollapsed);
  const [showAll, setShowAll] = useState(false);

  useEffect(() => {
    const load = async () => {
      try {
        const data = await apiFetch<ActiveWatcher[]>("/watcher/active");
        setWatchers(data);
      } catch {}
    };
    load();
    const interval = setInterval(load, 10000);
    return () => clearInterval(interval);
  }, []);

  if (watchers.length === 0) return null;
  const visibleWatchers = showAll ? watchers : watchers.slice(0, 12);
  const overdueCount = watchers.filter((watcher) => watcher.is_overdue).length;

  const toggleCollapsed = () => {
    setCollapsed((current) => {
      const next = !current;
      try {
        window.localStorage.setItem(WATCHERS_COLLAPSED_STORAGE_KEY, String(next));
      } catch {}
      return next;
    });
  };

  const dismissWatcher = async (watcherId: string) => {
    await apiFetch(`/watcher/${watcherId}/deactivate`, { method: "POST" });
    setWatchers((current) => current.filter((watcher) => watcher.id !== watcherId));
  };

  return (
    <section className="shrink-0 border-b border-slate-200 dark:border-slate-800">
      <button
        type="button"
        onClick={toggleCollapsed}
        className="flex w-full items-center justify-between gap-3 px-4 py-3 text-left text-xs font-medium text-slate-500 transition-colors hover:bg-slate-50 hover:text-slate-900 dark:text-slate-400 dark:hover:bg-slate-900/60 dark:hover:text-slate-100"
        aria-expanded={!collapsed}
        aria-controls="research-live-watches"
      >
        <span>
          Live watches <span className="tabular-nums">({watchers.length})</span>
          {overdueCount > 0 ? (
            <span className="ml-2 text-rose-600 dark:text-rose-400">
              {overdueCount} overdue
            </span>
          ) : null}
        </span>
        <ChevronDown
          className={`h-4 w-4 shrink-0 transition-transform ${collapsed ? "-rotate-90" : ""}`}
          aria-hidden="true"
        />
      </button>
      <div
        id="research-live-watches"
        hidden={collapsed}
        className="max-h-[30vh] space-y-3 overflow-y-auto overscroll-contain px-4 pb-4 pr-5"
      >
        {visibleWatchers.map((w) => (
          <div key={w.id} className="rounded-lg border border-sky-200 bg-sky-50/30 p-3 text-xs dark:border-sky-900/30 dark:bg-sky-900/10">
            <div className="flex items-center justify-between">
              <span className="font-bold text-sky-600 dark:text-sky-400">{w.ticker}</span>
              <div className="flex items-center gap-1.5">
                <span className="text-[10px] uppercase text-slate-400 font-bold">
                  {w.condition_type.replaceAll("_", " ")}
                </span>
                <button
                  type="button"
                  title="Dismiss watch"
                  aria-label={`Dismiss ${w.ticker ?? "untargeted"} watch`}
                  onClick={() => void dismissWatcher(w.id)}
                  className="grid size-6 place-items-center text-slate-400 hover:text-slate-700 dark:hover:text-slate-200"
                >
                  <X size={13} aria-hidden="true" />
                </button>
              </div>
            </div>
            <p className="mt-1 font-medium">{w.objective}</p>
            {w.adjustment_plan ? (
              <p className="mt-1 line-clamp-2 text-[11px] text-slate-500 dark:text-slate-400">
                If it fires: {w.adjustment_plan}
              </p>
            ) : null}
            {!w.evaluation_status && !["price_above", "price_below", "deadline", "reminder"].includes(w.condition_type) ? (
              <p className="mt-1 text-[10px] text-slate-400">
                Monitoring new source-backed evidence
              </p>
            ) : null}
            {w.evaluation_status === "no_match" && w.last_checked_at ? (
              <p className="mt-1 text-[10px] text-slate-400" title={w.evaluation_detail ?? undefined}>
                Last evidence check {formatTimestamp(w.last_checked_at)} · condition not met
              </p>
            ) : null}
            {w.evaluation_status === "deferred" ? (
              <p className="mt-1 text-[10px] text-amber-600 dark:text-amber-400" title={w.evaluation_error ?? undefined}>
                Evidence check deferred · retry scheduled
              </p>
            ) : null}
            {w.deadline && (
              <p className={`mt-1 text-[10px] font-semibold uppercase tracking-wide ${w.is_overdue ? "text-rose-500" : "text-slate-400"}`}>
                {w.is_overdue ? "Overdue" : formatCountdown(w.countdown_seconds)}
                {" · "}
                {formatTimestamp(w.deadline)}
              </p>
            )}
          </div>
        ))}
        {watchers.length > 12 ? (
          <button
            type="button"
            onClick={() => setShowAll((current) => !current)}
            className="w-full py-1 text-left text-[11px] font-semibold text-sky-600 hover:text-sky-500 dark:text-sky-400"
          >
            {showAll ? "Show fewer" : `Show ${watchers.length - 12} more`}
          </button>
        ) : null}
      </div>
    </section>
  );
}

function RunningChats({
  jobs,
  onFocus,
  onCancel,
}: {
  jobs: AgentTurnJob[];
  onFocus: (job: AgentTurnJob) => void;
  onCancel: (jobId: string) => void;
}) {
  if (jobs.length === 0) return null;

  return (
    <div className="shrink-0 border-b border-slate-200 p-4 dark:border-slate-800">
      <p className="mb-3 text-xs font-medium uppercase tracking-wider text-slate-500 dark:text-slate-400">
        Running chats ({jobs.length})
      </p>
      <div className="space-y-2">
        {jobs.slice(0, 5).map((job) => (
          <div key={job.job_id} className="rounded-lg border border-amber-200 bg-amber-50/40 p-3 text-xs dark:border-amber-900/40 dark:bg-amber-950/10">
            <button type="button" onClick={() => onFocus(job)} className="w-full text-left">
              <div className="flex items-center justify-between gap-2">
                <span className="font-bold uppercase tracking-wider text-amber-700 dark:text-amber-300">
                  {job.status}
                </span>
                <span className="text-[10px] text-slate-400">{formatTimestamp(job.updated_at)}</span>
              </div>
              <p className="mt-1 line-clamp-2 font-medium text-slate-700 dark:text-slate-200">{job.request_message}</p>
              <p className="mt-1 text-[10px] text-slate-500 dark:text-slate-400">Turn ID {job.job_id.slice(0, 8)}</p>
            </button>
            <button
              type="button"
              onClick={() => onCancel(job.job_id)}
              className="mt-2 text-[10px] font-bold uppercase tracking-wider text-slate-500 hover:text-rose-600 dark:text-slate-400"
            >
              Cancel
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}

export default function ChatPage() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [draftMessagesBySession, setDraftMessagesBySession] = useState<Record<string, Message[]>>({});
  const [input, setInput] = useState("");
  const [isTyping, setIsTyping] = useState(false);
  const [conversations, setConversations] = useState<AgentConversationSummary[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [selectedSubjectName, setSelectedSubjectName] = useState("Portfolio");
  const [resolvedPreview, setResolvedPreview] = useState<AgentResolve | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [allowActions, setAllowActions] = useState(false);
  const [allowActionsLoaded, setAllowActionsLoaded] = useState(false);
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  const [openTraceRunId, setOpenTraceRunId] = useState<string | null>(null);
  const [traceCache, setTraceCache] = useState<Record<string, ReasoningTrace>>({});
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [activeJob, setActiveJob] = useState<AgentTurnJob | null>(null);
  const [activeJobs, setActiveJobs] = useState<AgentTurnJob[]>([]);
  const [activeJobRequestMessage, setActiveJobRequestMessage] = useState<string | null>(null);
  const [renamingSessionId, setRenamingSessionId] = useState<string | null>(null);
  const [renameTitle, setRenameTitle] = useState("");
  const [renameBusy, setRenameBusy] = useState(false);
  const [mobileWorkspaceOpen, setMobileWorkspaceOpen] = useState(false);
  const endRef = useRef<HTMLDivElement>(null);
  const messagesScrollRef = useRef<HTMLDivElement>(null);
  const lastPrefillRef = useRef<string | null>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const activeJobsRef = useRef<AgentTurnJob[]>([]);
  const loadActiveJobsRef = useRef<() => Promise<void>>(async () => undefined);
  const mobileWorkspacePanelRef = useRef<HTMLElement>(null);
  const mobileWorkspaceTriggerRef = useRef<HTMLButtonElement>(null);

  const closeMobileWorkspace = useCallback(() => {
    setMobileWorkspaceOpen(false);
    window.requestAnimationFrame(() => mobileWorkspaceTriggerRef.current?.focus());
  }, []);

  function resizeComposer(nextValue?: string) {
    if (!composerRef.current) return;
    const composer = composerRef.current;
    composer.style.height = "0px";
    if (!(nextValue ?? composer.value).trim()) {
      composer.style.height = "64px";
      composer.scrollTop = 0;
      return;
    }
    const nextHeight = Math.min(composer.scrollHeight, 220);
    composer.style.height = `${Math.max(nextHeight, 64)}px`;
  }

  const loadConversations = useCallback(async (preferredSessionId?: string | null) => {
    const result = await apiFetch<AgentConversationList>("/agent/conversations");
    setConversations(result.conversations);
    setActiveSessionId((current) => {
      if (preferredSessionId) {
        return preferredSessionId;
      }
      if (!current && result.conversations.length > 0) {
        return result.conversations[0].session_id;
      }
      return current;
    });
  }, []);

  function persistActiveJob(job: AgentTurnJob | null) {
    if (typeof window === "undefined") return;
    if (!job || job.status === "completed" || job.status === "error") {
      window.sessionStorage.removeItem(ACTIVE_JOB_STORAGE_KEY);
      return;
    }
    window.sessionStorage.setItem(
      ACTIVE_JOB_STORAGE_KEY,
      JSON.stringify({
        job_id: job.job_id,
        request_message: job.request_message,
        session_id: job.session_id ?? null,
      }),
    );
  }

  function upsertActiveJob(job: AgentTurnJob) {
    setActiveJobs((current) => {
      const withoutJob = current.filter((item) => item.job_id !== job.job_id);
      return isActiveTurnJob(job) ? [job, ...withoutJob].slice(0, 30) : withoutJob;
    });
  }

  async function loadActiveJobs() {
    try {
      const result = await apiFetch<AgentTurnJobList>("/agent/turn-jobs?status=active&limit=30");
      setActiveJobs(result.jobs);
    } catch {
      // Active job visibility is helpful, but it should never break chat.
    }
  }
  loadActiveJobsRef.current = loadActiveJobs;

  function focusJob(job: AgentTurnJob) {
    setActiveJob(job);
    setActiveJobRequestMessage(job.request_message);
    setIsTyping(isActiveTurnJob(job));
    if (job.session_id) {
      setActiveSessionId(job.session_id);
    } else {
      setActiveSessionId(null);
      setMessages([
        {
          role: "user",
          content: job.request_message,
          createdAt: job.created_at,
        },
      ]);
    }
  }

  async function cancelJob(jobId: string) {
    try {
      await apiFetch(`/agent/turn-jobs/${jobId}/cancel`, { method: "POST" });
      setActiveJobs((current) => current.filter((job) => job.job_id !== jobId));
      if (activeJob?.job_id === jobId) {
        setActiveJob(null);
        setActiveJobRequestMessage(null);
        setIsTyping(false);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to cancel this chat turn.");
    }
  }

  const mergeAssistantTurn = useCallback((turn: AgentTurn, requestMessage: string, requestCreatedAt?: string | null) => {
    const assistantMessage: Message = {
      role: "assistant",
      content: stripInternalReasoningBlock(turn.assistant_message),
      createdAt: turn.responded_at,
      meta: {
        process_mode: turn.process_mode ?? (turn.reasoning_run_id ? "reasoning_analysis" : "operating_context_llm"),
        resolution_reason: turn.resolution_reason,
        stance: turn.stance,
        confidence_band: turn.confidence_band,
        thesis_summary: turn.thesis_summary,
        rationale_summary: turn.rationale_summary,
        source_feedback_influence: turn.source_feedback_influence,
        historical_analogy_lenses: turn.historical_analogy_lenses,
        actions: turn.actions,
        reasoning_run_id: turn.reasoning_run_id,
        subagents: turn.subagents,
      },
      };
    setMessages((prev) => {
      const restoredUserMessage: Message = {
        role: "user",
        content: requestMessage,
        createdAt: requestCreatedAt || turn.responded_at,
      };
      const hasLatestUser =
        prev.length > 0 &&
        prev[prev.length - 1]?.role === "user" &&
        prev[prev.length - 1]?.content === requestMessage;
      const nextMessages = hasLatestUser
        ? [...prev, assistantMessage]
        : [...prev, restoredUserMessage, assistantMessage];
      setDraftMessagesBySession((current) => ({
        ...current,
        [turn.session_id]: nextMessages,
      }));
      return nextMessages;
    });
    setActiveSessionId(turn.session_id);
    setSelectedSubjectName(turn.subject_name ?? turn.subject_type);
    if (turn.reasoning_run_id) {
      void apiFetch<ReasoningTrace>(`/reasoning/runs/${turn.reasoning_run_id}`)
        .then((trace) => setTraceCache((current) => ({ ...current, [turn.reasoning_run_id!]: trace })))
        .catch(() => undefined);
    }
    void loadConversations(turn.session_id);
  }, [loadConversations]);

  useEffect(() => {
    void Promise.all([loadConversations(), loadActiveJobs()]).catch(() => undefined);
  }, [loadConversations]);

  useEffect(() => {
    activeJobsRef.current = activeJobs;
  }, [activeJobs]);

  useEffect(() => {
    const previousHtmlOverflow = document.documentElement.style.overflow;
    const previousBodyOverflow = document.body.style.overflow;
    const previousBodyHeight = document.body.style.height;
    document.documentElement.style.overflow = "hidden";
    document.body.style.overflow = "hidden";
    document.body.style.height = "100dvh";
    return () => {
      document.documentElement.style.overflow = previousHtmlOverflow;
      document.body.style.overflow = previousBodyOverflow;
      document.body.style.height = previousBodyHeight;
    };
  }, []);

  useEffect(() => {
    if (!mobileWorkspaceOpen) return;

    const desktopMedia = window.matchMedia("(min-width: 1024px)");

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        closeMobileWorkspace();
        return;
      }

      if (event.key !== "Tab") return;
      const focusable = mobileWorkspacePanelRef.current?.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      );
      if (!focusable?.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }

    function closeAtDesktopWidth(event: MediaQueryListEvent) {
      if (event.matches) {
        setMobileWorkspaceOpen(false);
      }
    }

    document.addEventListener("keydown", handleKeyDown);
    desktopMedia.addEventListener("change", closeAtDesktopWidth);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      desktopMedia.removeEventListener("change", closeAtDesktopWidth);
    };
  }, [closeMobileWorkspace, mobileWorkspaceOpen]);

  useEffect(() => {
    // Local job restoration remains in useEffect
    const raw = window.sessionStorage.getItem(ACTIVE_JOB_STORAGE_KEY);
    if (!raw) return;
    try {
      const saved = JSON.parse(raw) as { job_id?: string; request_message?: string | null };
      if (saved.job_id) {
        setActiveJobRequestMessage(saved.request_message ?? null);
        void apiFetch<AgentTurnJob>(`/agent/turn-jobs/${saved.job_id}`)
          .then((job) => {
            setIsTyping(["queued", "running"].includes(job.status));
            setActiveJob(job);
            persistActiveJob(job);
          })
          .catch(() => {
            window.sessionStorage.removeItem(ACTIVE_JOB_STORAGE_KEY);
          });
      }
    } catch {
      window.sessionStorage.removeItem(ACTIVE_JOB_STORAGE_KEY);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    let inFlight = false;
    let timeoutId: number | null = null;

    const hasActiveJobs = () => activeJobsRef.current.some(isActiveTurnJob);
    const nextDelay = () => {
      if (document.hidden) return ACTIVE_JOBS_POLL_HIDDEN_MS;
      return hasActiveJobs() ? ACTIVE_JOBS_POLL_ACTIVE_MS : ACTIVE_JOBS_POLL_IDLE_MS;
    };
    const schedule = (delay = nextDelay()) => {
      if (cancelled) return;
      timeoutId = window.setTimeout(tick, delay);
    };
    const tick = () => {
      if (cancelled) return;
      if (inFlight) {
        schedule();
        return;
      }
      inFlight = true;
      void loadActiveJobsRef.current().finally(() => {
        inFlight = false;
        schedule();
      });
    };
    const refreshWhenVisible = () => {
      if (document.hidden) return;
      if (timeoutId !== null) {
        window.clearTimeout(timeoutId);
      }
      schedule(250);
    };

    schedule();
    document.addEventListener("visibilitychange", refreshWhenVisible);
    return () => {
      cancelled = true;
      if (timeoutId !== null) {
        window.clearTimeout(timeoutId);
      }
      document.removeEventListener("visibilitychange", refreshWhenVisible);
    };
  }, []);

  useEffect(() => {
    const matchingJob = activeJobs.find((job) => {
      if (!isActiveTurnJob(job)) return false;
      if (job.session_id && activeSessionId) return job.session_id === activeSessionId;
      return !job.session_id && !activeSessionId;
    });
    if (matchingJob) {
      setActiveJob(matchingJob);
      setActiveJobRequestMessage(matchingJob.request_message);
      setIsTyping(true);
      return;
    }
    if (activeJob && isActiveTurnJob(activeJob)) {
      const stillCurrent =
        (activeJob.session_id && activeSessionId && activeJob.session_id === activeSessionId) ||
        (!activeJob.session_id && !activeSessionId);
      if (!stillCurrent) {
        setActiveJob(null);
        setActiveJobRequestMessage(null);
        setIsTyping(false);
      }
    }
  }, [activeJob, activeJobs, activeSessionId]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    setAllowActions(window.localStorage.getItem(ALLOW_ACTIONS_STORAGE_KEY) === "true");
    setAllowActionsLoaded(true);
  }, []);

  // Robust Persistence: Sync to localStorage whenever allowActions changes
  useEffect(() => {
    if (typeof window === "undefined" || !allowActionsLoaded) return;
    window.localStorage.setItem(ALLOW_ACTIONS_STORAGE_KEY, allowActions.toString());
  }, [allowActions, allowActionsLoaded]);

  useEffect(() => {
    if (!activeSessionId) {
      setMessages([]);
      return;
    }
    setLoadingHistory(true);
    void apiFetch<AgentConversationHistory>(`/agent/history?session_id=${activeSessionId}`)
      .then((history) => {
        const historyMessages: Message[] = history.entries.map((entry): Message => ({
          role: entry.is_artifact
            ? "system"
            : entry.role === "assistant"
              ? "assistant"
              : entry.role === "user"
                ? "user"
                : "system",
          content: entry.role === "assistant" ? stripInternalReasoningBlock(entry.content) : entry.content,
          createdAt: entry.created_at,
          messageKind: entry.message_kind ?? "chat",
          isArtifact: Boolean(entry.is_artifact),
          meta:
            entry.role === "assistant" && !entry.is_artifact
              ? {
                  process_mode: entry.process_mode,
                  resolution_reason: entry.resolution_reason,
                  stance: entry.stance,
                  confidence_band: entry.confidence_band,
                  thesis_summary: entry.thesis_summary,
                  rationale_summary: entry.rationale_summary,
                  source_feedback_influence: entry.source_feedback_influence,
                  historical_analogy_lenses: entry.historical_analogy_lenses,
                  actions: entry.actions,
                  reasoning_run_id: entry.reasoning_run_id,
                  subagents: entry.subagents,
                }
              : undefined,
        }));
        const draftMessages = draftMessagesBySession[activeSessionId] ?? [];
        const preferredMessages =
          historyMessages.length >= draftMessages.length || draftMessages.length === 0
            ? historyMessages
            : draftMessages;
        setMessages(preferredMessages);
        const matchedConversation = conversations.find((item) => item.session_id === activeSessionId);
        setSelectedSubjectName(
          matchedConversation?.subject_name ?? history.subject_type ?? "Portfolio",
        );
      })
      .catch(() => {
        const draftMessages = draftMessagesBySession[activeSessionId] ?? [];
        if (draftMessages.length > 0) {
          setMessages(draftMessages);
        }
      })
      .finally(() => setLoadingHistory(false));
  }, [activeSessionId, conversations, draftMessagesBySession]);

  useEffect(() => {
    if (input.trim().length < 2) {
      setResolvedPreview(null);
      return;
    }
    const timeout = window.setTimeout(() => {
      void apiFetch<AgentResolve>("/agent/resolve", {
        method: "POST",
        body: JSON.stringify({
          session_id: activeSessionId || undefined,
          message: input.trim(),
          auto_execute: false,
        }),
      })
        .then((result) => setResolvedPreview(result))
        .catch(() => undefined);
    }, 250);
    return () => window.clearTimeout(timeout);
  }, [activeSessionId, input]);

  useEffect(() => {
    const pendingRunIds = Array.from(
      new Set(
        messages
          .map((message) => message.meta?.reasoning_run_id)
          .filter((runId): runId is string => typeof runId === "string")
          .filter((runId) => !traceCache[runId]),
      ),
    ).slice(0, 6);
    if (pendingRunIds.length === 0) return;

    void Promise.all(
      pendingRunIds.map(async (runId) => {
        try {
          const trace = await apiFetch<ReasoningTrace>(`/reasoning/runs/${runId}`);
          return [runId, trace] as const;
        } catch {
          return null;
        }
      }),
    ).then((results) => {
      const loaded = results.filter((item): item is readonly [string, ReasoningTrace] => item !== null);
      if (loaded.length === 0) return;
      setTraceCache((current) => {
        const next = { ...current };
        for (const [runId, trace] of loaded) {
          next[runId] = trace;
        }
        return next;
      });
    });
  }, [messages, traceCache]);

  useEffect(() => {
    if (typeof window === "undefined") {
      return;
    }
    const prefill = new URLSearchParams(window.location.search).get("q");
    if (!prefill) {
      return;
    }
    if (lastPrefillRef.current === prefill) {
      return;
    }
    lastPrefillRef.current = prefill;
    setInput((current) => (current.trim().length > 0 ? current : prefill));
  }, []);

  useEffect(() => {
    // Scroll the message list itself, never an ancestor — otherwise entering a
    // conversation can scroll the whole shell and strand the composer mid-screen.
    const container = messagesScrollRef.current;
    if (container) {
      container.scrollTop = container.scrollHeight;
    }
  }, [messages]);

  useLayoutEffect(() => {
    resizeComposer(input);
  }, [input]);

  useEffect(() => {
    if (!isTyping) {
      setElapsedSeconds(0);
      return;
    }
    const interval = window.setInterval(() => {
      setElapsedSeconds((current) => current + 1);
    }, 1000);
    return () => window.clearInterval(interval);
  }, [isTyping]);

  useEffect(() => {
    if (!activeJob || !["queued", "running"].includes(activeJob.status)) {
      return;
    }
    persistActiveJob(activeJob);
    const interval = window.setInterval(() => {
      void apiFetch<AgentTurnJob>(`/agent/turn-jobs/${activeJob.job_id}`)
        .then((job) => {
          setIsTyping(["queued", "running"].includes(job.status));
          setActiveJob(job);
          upsertActiveJob(job);
          persistActiveJob(job);
          if (job.status === "completed" && job.result) {
            setIsTyping(false);
            setError(null);
            setActiveJobRequestMessage(null);
            mergeAssistantTurn(job.result, job.request_message, job.created_at);
            setActiveJob(null);
            upsertActiveJob(job);
          } else if (job.status === "error") {
            setIsTyping(false);
            setError(job.error || "Unable to complete this turn.");
            setActiveJobRequestMessage(null);
            setActiveJob(null);
            upsertActiveJob(job);
          }
        })
        .catch((err) => {
          setIsTyping(false);
          setError(err instanceof Error ? err.message : "Unable to refresh turn status.");
        });
    }, 1200);
    return () => window.clearInterval(interval);
  }, [activeJob, mergeAssistantTurn]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!input.trim() || Boolean(activeJob && isActiveTurnJob(activeJob))) return;

    const userMessage = input.trim();
    setInput("");
    resizeComposer("");
    setError(null);
    const optimisticUserMessage = { role: "user", content: userMessage, createdAt: new Date().toISOString() } as Message;
    setMessages((prev) => [...prev, optimisticUserMessage]);
    setIsTyping(true);
    setActiveJobRequestMessage(userMessage);

    try {
      const response = await fetch(`${API_BASE}/agent/turn-jobs`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: activeSessionId || undefined,
          message: userMessage,
          auto_execute: allowActions,
        }),
      });
      if (!response.ok) {
        throw new Error(await response.text());
      }
      const job = (await response.json()) as AgentTurnJob;
      setActiveJob(job);
      upsertActiveJob(job);
      persistActiveJob(job);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to start this turn.");
      setActiveJobRequestMessage(null);
      setIsTyping(false);
    } finally {
    }
  }

  function handleComposerKeyDown(event: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void handleSubmit(event);
    }
  }

  function startNewChat() {
    setActiveSessionId(null);
    setMessages([]);
    setError(null);
    setResolvedPreview(null);
  }

  async function toggleTrace(runId: string) {
    if (openTraceRunId === runId) {
      setOpenTraceRunId(null);
      return;
    }
    setOpenTraceRunId(runId);
    if (traceCache[runId]) return;
    try {
      const trace = await apiFetch<ReasoningTrace>(`/reasoning/runs/${runId}`);
      setTraceCache((current) => ({ ...current, [runId]: trace }));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to load reasoning trace.");
    }
  }

  function startRename(conversation: AgentConversationSummary) {
    setRenamingSessionId(conversation.session_id);
    setRenameTitle(conversation.title);
    setError(null);
  }

  function cancelRename() {
    setRenamingSessionId(null);
    setRenameTitle("");
    setRenameBusy(false);
  }

  async function saveRename(sessionId: string) {
    const cleanTitle = renameTitle.trim();
    if (!cleanTitle || renameBusy) return;
    setRenameBusy(true);
    setError(null);
    try {
      const updated = await apiFetch<AgentConversationSummary>(`/agent/conversations/${sessionId}`, {
        method: "PATCH",
        body: JSON.stringify({ title: cleanTitle }),
      });
      setConversations((current) =>
        current
          .map((conversation) =>
            conversation.session_id === sessionId ? { ...conversation, ...updated } : conversation,
          )
          .sort(
            (a, b) =>
              new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime(),
          ),
      );
      cancelRename();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to rename this chat.");
      setRenameBusy(false);
    }
  }

  const activeConversation = conversations.find((item) => item.session_id === activeSessionId) ?? null;
  const liveEvents = activeJob?.events.slice(-5) ?? [];
  const executionActive = allowActionsLoaded ? allowActions : false;
  const liveStatus =
    elapsedSeconds < 6
      ? "Resolving context and assembling evidence."
      : elapsedSeconds < 20
      ? "Running the reasoning pass on the current packet."
      : elapsedSeconds < 45
      ? "Still analyzing. This turn is deeper than a quick summary."
      : "Long-running reasoning in progress. You can move around the product and reconnect to it.";

  return (
    <div className="flex h-[100dvh] max-h-[100dvh] flex-col overflow-hidden bg-background font-sans text-foreground">
      <AppNav active="research" />

      <main className="flex min-h-0 w-full flex-1 overflow-hidden">
        {mobileWorkspaceOpen ? (
          <button
            type="button"
            aria-label="Close chats and watches"
            onClick={closeMobileWorkspace}
            className="fixed inset-0 z-[60] bg-slate-950/45 backdrop-blur-[1px] lg:hidden"
          />
        ) : null}
        <aside
          ref={mobileWorkspacePanelRef}
          id="research-workspace-panel"
          role={mobileWorkspaceOpen ? "dialog" : undefined}
          aria-modal={mobileWorkspaceOpen ? true : undefined}
          aria-label="Chats and watches"
          className={`${
            mobileWorkspaceOpen
              ? "fixed inset-y-0 left-0 z-[70] flex w-[min(88vw,360px)] shadow-2xl"
              : "hidden"
          } h-full min-h-0 shrink-0 flex-col border-r border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-950 lg:relative lg:inset-auto lg:z-auto lg:flex lg:w-[320px] lg:shadow-none`}
        >
          {mobileWorkspaceOpen ? (
            <div className="flex items-center justify-between border-b border-slate-200 px-4 py-3 dark:border-slate-800 lg:hidden">
              <p className="text-sm font-semibold">Chats and watches</p>
              <button
                type="button"
                autoFocus
                onClick={closeMobileWorkspace}
                className="inline-flex h-9 w-9 items-center justify-center rounded border border-slate-200 text-slate-500 dark:border-slate-800 dark:text-slate-400"
                title="Close chats and watches"
              >
                <X className="h-4 w-4" />
                <span className="sr-only">Close chats and watches</span>
              </button>
            </div>
          ) : null}
          <div className="border-b border-slate-200 p-4 dark:border-slate-800">
            <p className="mb-3 text-xs font-medium uppercase tracking-wider text-slate-500 dark:text-slate-400">
              Saved chats
            </p>
            <button
              type="button"
              onClick={() => {
                startNewChat();
                setMobileWorkspaceOpen(false);
              }}
              className="w-full rounded-lg bg-sky-600 px-4 py-3 text-sm font-medium text-white"
            >
              Start new chat
            </button>
          </div>
          <RunningChats
            jobs={activeJobs}
            onFocus={(job) => {
              focusJob(job);
              setMobileWorkspaceOpen(false);
            }}
            onCancel={(jobId) => void cancelJob(jobId)}
          />
          <LiveWatchers />
          <div className="flex-1 overflow-y-auto p-3 space-y-2">
            {conversations.length === 0 ? (
              <p className="rounded-lg border border-dashed border-slate-300 p-4 text-sm text-slate-500 dark:border-slate-700 dark:text-slate-400">
                No saved chats yet.
              </p>
            ) : (
              conversations.map((conversation) => {
                const isActive = activeSessionId === conversation.session_id;
                const isRenaming = renamingSessionId === conversation.session_id;
                return (
                  <div
                    key={conversation.session_id}
                    className={`group w-full rounded-lg border px-3 py-3 transition ${
                      isActive
                        ? "border-sky-300 bg-sky-50 dark:border-sky-700 dark:bg-sky-950/30"
                        : "border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-950"
                    }`}
                  >
                    {isRenaming ? (
                      <form
                        onSubmit={(event) => {
                          event.preventDefault();
                          void saveRename(conversation.session_id);
                        }}
                        className="space-y-2"
                      >
                        <input
                          autoFocus
                          value={renameTitle}
                          onChange={(event) => setRenameTitle(event.target.value)}
                          onKeyDown={(event) => {
                            if (event.key === "Escape") {
                              event.preventDefault();
                              cancelRename();
                            }
                          }}
                          className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm outline-none focus:border-sky-500 focus:ring-2 focus:ring-sky-500/20 dark:border-slate-700 dark:bg-slate-900"
                        />
                        <div className="flex justify-end gap-2">
                          <button
                            type="submit"
                            disabled={renameBusy || !renameTitle.trim()}
                            className="inline-flex h-8 w-8 items-center justify-center rounded-lg bg-sky-600 text-white disabled:opacity-50"
                            title="Save chat name"
                          >
                            <Check className="h-4 w-4" />
                            <span className="sr-only">Save chat name</span>
                          </button>
                          <button
                            type="button"
                            onClick={cancelRename}
                            className="inline-flex h-8 w-8 items-center justify-center rounded-lg border border-slate-200 text-slate-500 dark:border-slate-800"
                            title="Cancel rename"
                          >
                            <X className="h-4 w-4" />
                            <span className="sr-only">Cancel rename</span>
                          </button>
                        </div>
                      </form>
                    ) : (
                      <div className="flex items-start gap-2">
                        <button
                          type="button"
                          onClick={() => {
                            setActiveSessionId(conversation.session_id);
                            setMobileWorkspaceOpen(false);
                          }}
                          className="min-w-0 flex-1 text-left"
                        >
                          <div className="flex items-start justify-between gap-2">
                            <p className="truncate text-sm font-medium">{conversation.title}</p>
                            <span className="shrink-0 text-[10px] uppercase tracking-wide text-slate-400">
                              {formatTimestamp(conversation.updated_at)}
                            </span>
                          </div>
                          {conversation.subject_name ? (
                            <p className="mt-1 truncate text-xs text-slate-500 dark:text-slate-400">{conversation.subject_name}</p>
                          ) : null}
                          {conversation.latest_message_preview ? (
                            <p className="mt-2 line-clamp-2 text-xs text-slate-500 dark:text-slate-400">
                              {stripInternalReasoningBlock(conversation.latest_message_preview)}
                            </p>
                          ) : null}
                          {conversation.artifact_count ? (
                            <p className="mt-2 text-[11px] font-medium uppercase tracking-wide text-slate-400 dark:text-slate-500">
                              {conversation.artifact_count} system {conversation.artifact_count === 1 ? "note" : "notes"} hidden
                            </p>
                          ) : null}
                        </button>
                        <button
                          type="button"
                          onClick={() => startRename(conversation)}
                          className="mt-0.5 inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-slate-400 opacity-100 transition hover:bg-slate-100 hover:text-slate-700 dark:hover:bg-slate-900 dark:hover:text-slate-200 lg:opacity-0 lg:group-hover:opacity-100"
                          title="Rename chat"
                        >
                          <Pencil className="h-4 w-4" />
                          <span className="sr-only">Rename chat</span>
                        </button>
                      </div>
                    )}
                  </div>
                );
              })
            )}
          </div>
        </aside>

        <section className="flex h-full min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
          <div className="shrink-0 border-b border-slate-200 bg-white/70 px-4 py-4 dark:border-slate-800 dark:bg-slate-950/70 sm:px-6">
            <div className="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
              <div>
                {activeConversation && renamingSessionId === activeConversation.session_id ? (
                  <form
                    onSubmit={(event) => {
                      event.preventDefault();
                      void saveRename(activeConversation.session_id);
                    }}
                    className="flex max-w-xl items-center gap-2"
                  >
                    <input
                      autoFocus
                      value={renameTitle}
                      onChange={(event) => setRenameTitle(event.target.value)}
                      onKeyDown={(event) => {
                        if (event.key === "Escape") {
                          event.preventDefault();
                          cancelRename();
                        }
                      }}
                      className="min-w-0 flex-1 rounded-lg border border-slate-300 bg-white px-3 py-2 text-xl font-semibold tracking-tight outline-none focus:border-sky-500 focus:ring-2 focus:ring-sky-500/20 dark:border-slate-700 dark:bg-slate-900"
                    />
                    <button
                      type="submit"
                      disabled={renameBusy || !renameTitle.trim()}
                      className="inline-flex h-9 w-9 items-center justify-center rounded-lg bg-sky-600 text-white disabled:opacity-50"
                      title="Save chat name"
                    >
                      <Check className="h-4 w-4" />
                      <span className="sr-only">Save chat name</span>
                    </button>
                    <button
                      type="button"
                      onClick={cancelRename}
                      className="inline-flex h-9 w-9 items-center justify-center rounded-lg border border-slate-200 text-slate-500 dark:border-slate-800"
                      title="Cancel rename"
                    >
                      <X className="h-4 w-4" />
                      <span className="sr-only">Cancel rename</span>
                    </button>
                  </form>
                ) : (
                  <div className="flex items-center gap-2">
                    <h1 className="text-2xl font-semibold tracking-tight">
                      {activeConversation?.title ?? "New chat"}
                    </h1>
                    {activeConversation ? (
                      <button
                        type="button"
                        onClick={() => startRename(activeConversation)}
                        className="inline-flex h-8 w-8 items-center justify-center rounded-lg text-slate-400 transition hover:bg-slate-100 hover:text-slate-700 dark:hover:bg-slate-900 dark:hover:text-slate-200"
                        title="Rename chat"
                      >
                        <Pencil className="h-4 w-4" />
                        <span className="sr-only">Rename chat</span>
                      </button>
                    ) : null}
                  </div>
                )}
                <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
                  {activeConversation
                    ? `Talking with Prophet about ${activeConversation.subject_name ?? selectedSubjectName}.`
                    : "Ask a question naturally. Prophet will usually resolve the right context for you."}
                </p>
              </div>
              <div className="flex flex-wrap gap-2 lg:hidden">
                <button
                  ref={mobileWorkspaceTriggerRef}
                  type="button"
                  onClick={() => setMobileWorkspaceOpen(true)}
                  className="inline-flex items-center gap-2 rounded border border-slate-300 px-3 py-1.5 text-sm dark:border-slate-700"
                  aria-expanded={mobileWorkspaceOpen}
                  aria-controls="research-workspace-panel"
                >
                  <PanelLeftOpen className="h-4 w-4" aria-hidden="true" />
                  Chats and watches
                </button>
                <button
                  type="button"
                  onClick={startNewChat}
                  className="rounded border border-slate-300 px-3 py-1.5 text-sm dark:border-slate-700"
                >
                  New chat
                </button>
              </div>
            </div>

            {/* Settings and Dossier quick create merged to Settings page */}

            {error ? <p className="mt-3 text-sm text-red-500">{error}</p> : null}
            {loadingHistory ? (
              <p className="mt-2 text-xs text-slate-500 dark:text-slate-400">
                Loading saved conversation...
              </p>
            ) : null}
          </div>

          <div ref={messagesScrollRef} className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 pb-6 pt-6 sm:px-6">
            {messages.length === 0 ? (
              <div className="mx-auto mt-16 max-w-2xl text-center">
                <div className="inline-flex h-16 w-16 items-center justify-center rounded-lg bg-sky-100 dark:bg-sky-900/30">
                  <svg className="h-8 w-8 text-sky-600 dark:text-sky-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 10V3L4 14h7v7l9-11h-7z" />
                  </svg>
                </div>
                <h2 className="mt-6 text-2xl font-semibold tracking-tight text-slate-900 dark:text-white">Good morning. Focus on what matters.</h2>
                <p className="mt-4 text-base text-slate-500 dark:text-slate-400">
                  Prophet intelligently groups your queries to the right context. Probe a specific thesis, ask for research updates, or challenge an assumption.
                </p>
                <div className="mt-8 flex flex-wrap justify-center gap-3">
                  <button onClick={() => setInput("What are the core vulnerabilities in our largest tech holding?")} className="rounded-full border border-slate-200 bg-white px-4 py-2 text-sm text-slate-600 hover:border-slate-300 dark:border-slate-800 dark:bg-slate-900 dark:text-slate-300 dark:hover:border-slate-700">What are the core vulnerabilities in our largest tech holding?</button>
                  <button onClick={() => setInput("Summarize recent developments for Apple.")} className="rounded-full border border-slate-200 bg-white px-4 py-2 text-sm text-slate-600 hover:border-slate-300 dark:border-slate-800 dark:bg-slate-900 dark:text-slate-300 dark:hover:border-slate-700">Summarize recent developments for Apple.</button>
                </div>
              </div>
            ) : (
              <div className="space-y-6">
                {messages.map((m, idx) => (
                  <div key={idx} className={`flex w-full ${m.role === "user" ? "justify-end" : "justify-start"}`}>
                    <div className={`flex w-full gap-4 ${m.role === "user" ? "max-w-[720px] flex-row-reverse" : "max-w-[960px] flex-row"}`}>
                      <div className="mt-1 flex-shrink-0">
                        <div className={`flex h-8 w-8 items-center justify-center ${
                          m.role === "assistant"
                            ? "rounded-lg bg-sky-600"
                            : m.role === "user"
                              ? "rounded-full border border-slate-300 bg-slate-200 dark:border-slate-700 dark:bg-slate-800"
                              : "rounded-lg border border-slate-300 bg-white dark:border-slate-700 dark:bg-slate-900"
                        }`}>
                          <span className={`${
                            m.role === "assistant"
                              ? "font-mono text-white"
                              : m.role === "user"
                                ? "text-slate-600 dark:text-slate-300"
                                : "font-mono text-slate-500 dark:text-slate-400"
                          } text-xs font-bold`}>
                            {m.role === "assistant" ? "AI" : m.role === "user" ? "U" : "S"}
                          </span>
                        </div>
                      </div>
                      <div
                        className={`w-full rounded-lg p-4 text-sm leading-relaxed md:text-base ${
                          m.role === "user"
                            ? "bg-slate-100 text-slate-900 dark:bg-slate-800 dark:text-slate-100"
                            : m.role === "system"
                              ? "border border-dashed border-slate-200 bg-slate-50 text-slate-700 dark:border-slate-800 dark:bg-slate-950 dark:text-slate-300"
                            : "border border-slate-200 bg-white text-slate-800 dark:border-slate-800 dark:bg-[#111] dark:text-slate-200"
                        }`}
                      >
                        {formatTimestamp(m.createdAt) ? (
                          <p
                            className={`mb-2 text-[11px] uppercase tracking-wide ${
                              m.role === "user"
                                ? "text-right text-slate-500 dark:text-slate-400"
                                : "text-slate-400"
                            }`}
                          >
                            {formatTimestamp(m.createdAt)}
                          </p>
                        ) : null}
                        {m.isArtifact ? (
                          <p className="mb-2 text-[11px] font-medium uppercase tracking-wide text-slate-400">
                            {formatUserLabel(m.messageKind ?? "system_artifact")}
                          </p>
                        ) : null}
                        {m.content.includes("Enable state updates first") ? (
                          <div className="space-y-3">
                            <p>{m.content.replace("Enable state updates first.", "")}</p>
                            <button
                              onClick={() => setAllowActions(true)}
                              className="rounded-lg bg-sky-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-sky-700"
                            >
                              Enable state updates
                            </button>
                          </div>
                        ) : m.role === "assistant" ? (
                          <div className="prose prose-slate max-w-none break-words text-sm leading-7 dark:prose-invert md:text-base prose-headings:mb-2 prose-headings:mt-5 prose-headings:text-current prose-p:my-3 prose-li:my-1 prose-strong:text-current prose-a:text-sky-600 dark:prose-a:text-sky-400">
                            <ReactMarkdown remarkPlugins={[remarkGfm]}>
                              {m.content}
                            </ReactMarkdown>
                          </div>
                        ) : (
                          <div className="whitespace-pre-wrap break-words leading-7">{m.content}</div>
                        )}
                        {m.role === "assistant" && m.meta ? (
                          <div className="mt-4 space-y-3 border-t border-slate-200 pt-4 dark:border-slate-800">
                            <ActualProcessSummary
                              message={m}
                              trace={m.meta.reasoning_run_id ? traceCache[m.meta.reasoning_run_id] : undefined}
                            />
                            <div className="flex flex-wrap gap-2 text-xs">
                              {m.meta.stance ? (
                                <span className="rounded-full border border-slate-200 px-3 py-1 dark:border-slate-700">
                                  Stance: {formatUserLabel(m.meta.stance)}
                                </span>
                              ) : null}
                              {m.meta.confidence_band ? (
                                <span className="rounded-full border border-slate-200 px-3 py-1 dark:border-slate-700">
                                  Confidence: {formatUserLabel(m.meta.confidence_band)}
                                </span>
                              ) : null}
                            </div>
                            {m.meta.actions && m.meta.actions.length > 0 ? (
                              <details className="rounded-lg border border-slate-200 px-3 py-2 dark:border-slate-800">
                                <summary className="cursor-pointer text-xs font-medium uppercase tracking-wider text-slate-500 dark:text-slate-400">
                                  Agent actions · {m.meta.actions.length}
                                </summary>
                                <div className="mt-3 space-y-2">
                                {m.meta.actions.map((action, actionIndex) => (
                                  <div key={`${action.action_type}-${actionIndex}`} className="rounded-lg border border-slate-200 px-3 py-2 text-sm dark:border-slate-800">
                                    <p className="font-medium">{action.action_type.replaceAll("_", " ")}</p>
                                    <p className="mt-1 text-slate-500 dark:text-slate-400">{action.summary}</p>
                                  </div>
                                ))}
                                </div>
                              </details>
                            ) : null}
                            {m.meta.subagents && Object.keys(m.meta.subagents).length > 0 ? (
                              <details className="rounded-lg border border-slate-200 px-3 py-2 dark:border-slate-800">
                                <summary className="cursor-pointer text-xs font-medium uppercase tracking-wider text-slate-500 dark:text-slate-400">
                                  Independent analysis lenses · {Object.keys(m.meta.subagents).length}
                                </summary>
                                <div className="mt-3 space-y-2">
                                {Object.entries(m.meta.subagents).map(([label, insight]) => (
                                  <div key={label} className="rounded-lg border border-slate-200 px-3 py-2 text-sm dark:border-slate-800">
                                    <p className="font-medium">{label.replaceAll("_", " ")}</p>
                                    <p className="mt-1 text-slate-500 dark:text-slate-400">{insight}</p>
                                  </div>
                                ))}
                                </div>
                              </details>
                            ) : null}
                            {m.meta.reasoning_run_id ? (
                              <div className="space-y-2">
                                <button
                                  type="button"
                                  onClick={() => void toggleTrace(m.meta!.reasoning_run_id!)}
                                  className="rounded-lg border border-slate-200 px-3 py-2 text-sm dark:border-slate-800"
                                >
                                  {openTraceRunId === m.meta.reasoning_run_id ? "Hide analysis detail" : "View analysis detail"}
                                </button>
                                {openTraceRunId === m.meta.reasoning_run_id ? (
                                  <ReasoningTraceCard trace={traceCache[m.meta.reasoning_run_id]} />
                                ) : null}
                              </div>
                            ) : null}
                          </div>
                        ) : null}
                      </div>
                    </div>
                  </div>
                ))}
                {isTyping ? (
                  <div className="flex w-full justify-start">
                    <div className="flex w-full max-w-[960px] gap-4">
                      <div className="mt-1 flex-shrink-0">
                        <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-sky-600">
                          <span className="font-mono text-xs font-bold text-white">AI</span>
                        </div>
                      </div>
                      <div className="rounded-lg border border-slate-200 bg-white p-4 text-sm leading-relaxed text-slate-800 dark:border-slate-800 dark:bg-[#111] dark:text-slate-200 md:text-base">
                        <p className="font-medium">
                          {allowActions ? "Prophet is analyzing and can act on what it finds." : "Prophet is analyzing your request."}
                        </p>
                        <div className="mt-3 rounded-lg border border-slate-200 p-3 text-sm text-slate-500 dark:border-slate-800 dark:text-slate-400">
                          <p>
                            {resolvedPreview?.subject_name
                              ? `Resolved context: ${resolvedPreview.subject_name}.`
                              : "Waiting for the backend to finish this turn."}
                          </p>
                          <p className="mt-2">
                            {liveStatus}
                          </p>
                          <p className="mt-2">
                            Analysis continues in the background even if you leave this page briefly.
                          </p>
                          {activeJobRequestMessage ? (
                            <p className="mt-2 text-slate-600 dark:text-slate-300">
                              Working on: <span className="font-medium">{activeJobRequestMessage}</span>
                            </p>
                          ) : null}
                        </div>
                        <div className="mt-4 flex items-center justify-between gap-4">
                          <p className="text-xs uppercase tracking-wider text-slate-400">{elapsedSeconds}s elapsed</p>
                          {activeJob?.job_id ? (
                            <p className="text-xs text-slate-500 dark:text-slate-400">Turn ID {activeJob.job_id.slice(0, 8)}</p>
                          ) : null}
                        </div>
                        {liveEvents.length > 0 ? (
                          <div className="mt-4 space-y-2">
                            <p className="text-xs font-medium uppercase tracking-wider text-slate-500 dark:text-slate-400">
                              Live trace
                            </p>
                            {liveEvents.map((event, index) => {
                              const eventDetail = event.metadata ?? event.detail;
                              const subagentRole =
                                typeof eventDetail?.subagent_role === "string" ? eventDetail.subagent_role : null;
                              const subagentInsight =
                                typeof eventDetail?.subagent_insight === "string" ? eventDetail.subagent_insight : null;
                              return (
                                <div key={`${event.phase}-${event.created_at}-${index}`} className="rounded-lg border border-slate-200 px-3 py-2 text-sm dark:border-slate-800">
                                  <div className="flex items-center justify-between gap-3">
                                    <p className="font-medium capitalize">{event.phase.replaceAll("_", " ")}</p>
                                    <time className="text-[11px] text-slate-400">{formatTimestamp(event.created_at)}</time>
                                  </div>
                                  <p className="mt-1 text-slate-500 dark:text-slate-400">{event.message}</p>
                                  {subagentRole && subagentInsight ? (
                                    <div className="mt-2 rounded-lg bg-slate-50/50 p-2 text-xs italic text-slate-600 dark:bg-slate-900/40 dark:text-slate-300 border-l-2 border-sky-500">
                                      <span className="font-bold not-italic">{subagentRole}:</span> {subagentInsight}
                                    </div>
                                  ) : null}
                                </div>
                              );
                            })}
                          </div>
                        ) : null}
                        <details className="mt-4 rounded-lg border border-dashed border-slate-200 px-3 py-3 text-xs text-slate-500 dark:border-slate-800 dark:text-slate-400">
                          <summary className="cursor-pointer font-medium uppercase tracking-wider">
                            What this live trace means
                          </summary>
                          <p className="mt-2">
                            This is a live progress view. It shows what Prophet is already doing in the background, not a rigid scripted checklist.
                          </p>
                        </details>
                      </div>
                    </div>
                  </div>
                ) : null}
                <div ref={endRef} />
              </div>
            )}
          </div>

          <div className="shrink-0 border-t border-line bg-background/90 px-4 py-3 backdrop-blur sm:px-6">
            <form onSubmit={handleSubmit} className="mx-auto max-w-5xl">
              <div className="mb-2 flex min-h-8 items-center justify-between gap-3">
                <button
                  type="button"
                  role="switch"
                  aria-checked={executionActive}
                  disabled={!allowActionsLoaded}
                  onClick={() => setAllowActions((current) => !current)}
                  title="Allow Prophet to use enabled research and system actions for new turns"
                  className="inline-flex items-center gap-2 rounded border border-line bg-panel px-2.5 py-1.5 text-xs font-medium text-muted transition-colors hover:border-line-strong hover:text-foreground disabled:cursor-wait disabled:opacity-60"
                >
                  <span
                    aria-hidden="true"
                    className={`relative h-4 w-7 shrink-0 rounded-full transition-colors ${
                      executionActive ? "bg-emerald-600" : "bg-slate-300 dark:bg-slate-700"
                    }`}
                  >
                    <span
                      className={`absolute left-0.5 top-0.5 h-3 w-3 rounded-full bg-white shadow-sm transition-transform ${
                        executionActive ? "translate-x-3" : "translate-x-0"
                      }`}
                    />
                  </span>
                  Agent actions
                  <span className="w-5 text-left text-muted">{executionActive ? "On" : "Off"}</span>
                </button>
                {activeJob && isActiveTurnJob(activeJob) ? (
                  <span className="truncate text-xs text-muted">Turn running in background</span>
                ) : null}
              </div>
              <div className="relative">
                <textarea
                  ref={composerRef}
                  value={input}
                  onChange={(e) => {
                    setInput(e.target.value);
                    resizeComposer(e.target.value);
                  }}
                  onKeyDown={handleComposerKeyDown}
                  placeholder="Tell Prophet what you think, what changed, or what to analyze next..."
                  rows={1}
                  className="min-h-[64px] w-full resize-none overflow-y-auto rounded-lg border border-slate-300 bg-white py-4 pl-5 pr-20 outline-none transition-[border-color,box-shadow,height] focus:border-sky-500 focus:ring-2 focus:ring-sky-500 dark:border-slate-700 dark:bg-slate-900 dark:text-white"
                />
                <button
                  type="submit"
                  disabled={Boolean(activeJob && isActiveTurnJob(activeJob)) || !input.trim()}
                  className="absolute bottom-2.5 right-2.5 rounded-lg bg-sky-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-sky-700 disabled:opacity-50"
                >
                  Send
                </button>
              </div>
            </form>
          </div>
        </section>
      </main>
    </div>
  );
}

function ReasoningTraceCard({ trace }: { trace?: ReasoningTrace }) {
  if (!trace) {
    return (
      <div className="rounded-lg border border-slate-200 px-3 py-3 text-sm text-slate-500 dark:border-slate-800 dark:text-slate-400">
        Loading trace...
      </div>
    );
  }

  const {
    stance,
    confidenceBand,
    thesisSummary,
    reasoningText,
    strengthen,
    falsify,
    contradictions,
    gapFlags,
    retrievalLayers,
    researchPlan,
    answerability,
    alternativeHypotheses,
  } =
    summarizeReasoningTrace(trace);

  return (
    <div className="space-y-3 rounded-lg border border-slate-200 px-3 py-3 text-sm dark:border-slate-800">
      <div className="flex flex-wrap gap-2 text-xs">
        <span className="rounded-full border border-slate-200 px-2 py-1 dark:border-slate-800">{formatUserLabel(trace.run_type)}</span>
        <span className="rounded-full border border-slate-200 px-2 py-1 dark:border-slate-800">{formatModelUsedLabel(trace.model_used)}</span>
        <span className="rounded-full border border-slate-200 px-2 py-1 dark:border-slate-800">{trace.input_tokens ?? 0} in</span>
        <span className="rounded-full border border-slate-200 px-2 py-1 dark:border-slate-800">{trace.output_tokens ?? 0} out</span>
        <span className="rounded-full border border-slate-200 px-2 py-1 dark:border-slate-800">{trace.duration_ms ?? 0}ms</span>
      </div>
      {researchPlan ? (
        <div className="rounded-lg border border-slate-200 p-3 dark:border-slate-800">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="font-medium">Question research plan</p>
            <span className="rounded-full border border-slate-200 px-2 py-1 text-xs dark:border-slate-800">
              {formatUserLabel(researchPlan.mode)}
            </span>
          </div>
          {researchPlan.objective ? (
            <p className="mt-2 text-slate-600 dark:text-slate-300">{researchPlan.objective}</p>
          ) : null}
          {researchPlan.informationNeeds.length > 0 ? (
            <ul className="mt-2 list-disc space-y-1 pl-5 text-slate-500 dark:text-slate-400">
              {researchPlan.informationNeeds.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          ) : null}
          <p className="mt-2 text-xs text-slate-500 dark:text-slate-400">
            {researchPlan.externalRequired ? "External check required" : "Stored context may be sufficient"}
            {researchPlan.portfolioRole
              ? ` · portfolio ${formatUserLabel(researchPlan.portfolioRole)}`
              : ""}
          </p>
        </div>
      ) : null}
      {trace.evidence_packet ? (
        <div className="rounded-lg border border-slate-200 p-3 dark:border-slate-800">
          <p className="font-medium">Evidence packet</p>
          <p className="mt-1 text-slate-500 dark:text-slate-400">
            direct {trace.evidence_packet.direct_evidence_count} · connected {trace.evidence_packet.connected_evidence_count} · historical {trace.evidence_packet.historical_evidence_count} · contradiction {trace.evidence_packet.contradiction_evidence_count}
          </p>
          {trace.evidence_packet.gap_flags.length > 0 ? (
            <p className="mt-1 text-slate-500 dark:text-slate-400">gaps: {trace.evidence_packet.gap_flags.join(", ")}</p>
          ) : null}
          <EvidenceSourceList sources={trace.evidence_packet.sources ?? []} />
        </div>
      ) : null}
      <CorroborationPanel summary={summarizeReasoningTrace(trace)} />
      {answerability ? (
        <div
          className={`rounded-lg border p-3 ${
            answerability.status === "adequate"
              ? "border-emerald-300 bg-emerald-50/50 dark:border-emerald-900 dark:bg-emerald-950/20"
              : "border-amber-300 bg-amber-50/60 dark:border-amber-900 dark:bg-amber-950/20"
          }`}
        >
          <p className="text-xs font-medium uppercase tracking-wider text-slate-500 dark:text-slate-400">
            Question scope · {formatUserLabel(answerability.status)}
          </p>
          {answerability.reason ? <p className="mt-2">{answerability.reason}</p> : null}
          {answerability.missingInformation.length > 0 ? (
            <ul className="mt-2 list-disc space-y-1 pl-5 text-slate-600 dark:text-slate-300">
              {answerability.missingInformation.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
      <div className="rounded-lg border border-slate-200 p-3 dark:border-slate-800">
        <p className="font-medium">Readable summary</p>
        <div className="mt-2 space-y-3 text-sm text-slate-600 dark:text-slate-300">
          {thesisSummary ? <p>{thesisSummary}</p> : null}
          {reasoningText ? (
            <p className="rounded-lg bg-slate-50 px-3 py-2 dark:bg-slate-900/40">{reasoningText}</p>
          ) : null}
          <div className="flex flex-wrap gap-2 text-xs">
            {stance ? (
              <span className="rounded-full border border-slate-200 px-2 py-1 dark:border-slate-800">
                stance {formatUserLabel(stance)}
              </span>
            ) : null}
            {confidenceBand ? (
              <span className="rounded-full border border-slate-200 px-2 py-1 dark:border-slate-800">
                confidence {formatUserLabel(confidenceBand)}
              </span>
            ) : null}
          </div>
          {strengthen.length > 0 ? (
            <div>
              <p className="text-xs font-medium uppercase tracking-wider text-slate-500 dark:text-slate-400">
                What would strengthen this view
              </p>
              <ul className="mt-2 list-disc space-y-1 pl-5">
                {strengthen.slice(0, 4).map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            </div>
          ) : null}
          {gapFlags.length > 0 ? (
            <div>
              <p className="text-xs font-medium uppercase tracking-wider text-slate-500 dark:text-slate-400">
                What looks thin or missing
              </p>
              <div className="mt-2 flex flex-wrap gap-2">
                {gapFlags.slice(0, 5).map((item) => (
                  <span
                    key={item}
                    className="rounded-full border border-amber-300/60 bg-amber-50 px-2 py-1 text-xs text-amber-800 dark:border-amber-700/60 dark:bg-amber-950/30 dark:text-amber-200"
                  >
                    {formatUserLabel(item)}
                  </span>
                ))}
              </div>
            </div>
          ) : null}
          {falsify.length > 0 ? (
            <div>
              <p className="text-xs font-medium uppercase tracking-wider text-slate-500 dark:text-slate-400">
                What would change the conclusion
              </p>
              <ul className="mt-2 list-disc space-y-1 pl-5">
                {falsify.slice(0, 4).map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            </div>
          ) : null}
          {retrievalLayers.length > 0 ? (
            <div>
              <p className="text-xs font-medium uppercase tracking-wider text-slate-500 dark:text-slate-400">
                Retrieval path
              </p>
              <div className="mt-2 flex flex-wrap gap-2">
                {retrievalLayers.map((item) => (
                  <span
                    key={item}
                    className="rounded-full border border-slate-200 px-2 py-1 text-xs dark:border-slate-800"
                  >
                    {formatUserLabel(item)}
                  </span>
                ))}
              </div>
            </div>
          ) : null}
          {contradictions.length > 0 ? (
            <div>
              <p className="text-xs font-medium uppercase tracking-wider text-slate-500 dark:text-slate-400">
                Active contradictions
              </p>
              <ul className="mt-2 list-disc space-y-1 pl-5">
                {contradictions.slice(0, 4).map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            </div>
          ) : null}
          {alternativeHypotheses.length > 0 ? (
            <div>
              <p className="text-xs font-medium uppercase tracking-wider text-slate-500 dark:text-slate-400">
                Alternative explanations
              </p>
              <div className="mt-2 space-y-2">
                {alternativeHypotheses.slice(0, 4).map((item, index) => (
                  <div key={`${item.hypothesis}-${index}`} className="rounded-lg border border-slate-200 p-2 dark:border-slate-800">
                    <p>{item.hypothesis}</p>
                    {item.decisiveTest ? (
                      <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">Test: {item.decisiveTest}</p>
                    ) : null}
                  </div>
                ))}
              </div>
            </div>
          ) : null}
        </div>
        <details className="mt-3 rounded-lg border border-dashed border-slate-200 px-3 py-2 dark:border-slate-800">
          <summary className="cursor-pointer text-xs font-medium uppercase tracking-wider text-slate-500 dark:text-slate-400">
            View structured analysis record
          </summary>
          <p className="mt-2 text-xs text-slate-500 dark:text-slate-400">
            Auditable evidence, assumptions, alternatives, scope checks, and actions. This is not a private token-by-token thought stream.
          </p>
          <pre className="mt-2 overflow-x-auto whitespace-pre-wrap text-xs text-slate-500 dark:text-slate-400">
            {JSON.stringify(trace.structured_output_json, null, 2)}
          </pre>
        </details>
      </div>
      {trace.critique ? (
        <div className="rounded-lg border border-slate-200 p-3 dark:border-slate-800">
          <p className="font-medium">Critique</p>
          <p className="mt-1 text-slate-500 dark:text-slate-400">{trace.critique.critique_text}</p>
          {trace.critique.issues_found.length > 0 ? (
            <p className="mt-1 text-slate-500 dark:text-slate-400">issues: {trace.critique.issues_found.join(" | ")}</p>
          ) : null}
        </div>
      ) : (
        <div className="rounded-lg border border-slate-200 p-3 text-slate-500 dark:border-slate-800 dark:text-slate-400">
          No critique stored for this interactive turn.
        </div>
      )}
    </div>
  );
}

function EvidenceSourceList({
  sources,
  compact = false,
}: {
  sources: NonNullable<ReasoningTrace["evidence_packet"]>["sources"];
  compact?: boolean;
}) {
  if (sources.length === 0) {
    return (
      <p className="mt-2 text-xs text-slate-400">
        No attributable source receipt is available for the stored packet items.
      </p>
    );
  }

  return (
    <details className="mt-3 border-t border-slate-200 pt-3 dark:border-slate-800" open={!compact}>
      <summary className="cursor-pointer text-xs font-medium text-slate-600 dark:text-slate-300">
        {sources.length} attributable source {sources.length === 1 ? "record" : "records"}
      </summary>
      <div className="mt-3 max-h-80 divide-y divide-slate-200 overflow-y-auto pr-1 dark:divide-slate-800">
        {sources.map((source) => (
          <div key={source.raw_evidence_id} className="py-3 first:pt-0 last:pb-0">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xs font-semibold text-slate-800 dark:text-slate-100">{source.source_name}</span>
              <span className="rounded border border-slate-200 px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-slate-500 dark:border-slate-700 dark:text-slate-400">
                {source.origin_label ?? source.source_type.replaceAll("_", " ")}
              </span>
              {source.evidence_roles.map((role) => (
                <span key={role} className="text-[10px] font-medium uppercase tracking-wider text-sky-600 dark:text-sky-400">
                  {role}
                </span>
              ))}
            </div>
            {source.title ? (
              <p className="mt-1 line-clamp-2 text-xs leading-relaxed text-slate-600 dark:text-slate-300">{source.title}</p>
            ) : null}
            <div className="mt-2">
              <SourceProvenanceLinks
                evidenceId={source.raw_evidence_id}
                sourceName={source.source_name}
                sourceType={source.source_type}
                url={source.url}
                urlKind={source.url_kind}
                compact
                showUnavailable
              />
            </div>
          </div>
        ))}
      </div>
    </details>
  );
}

function ActualProcessSummary({
  message,
  trace,
}: {
  message: Message;
  trace?: ReasoningTrace;
}) {
  if (!message.meta) return null;
  const summary = summarizeReasoningTrace(trace);
  const displayedThesis = summary.thesisSummary ?? message.meta.thesis_summary ?? null;
  const displayedStance = summary.stance ?? message.meta.stance ?? null;
  const displayedConfidence = summary.confidenceBand ?? message.meta.confidence_band ?? null;
  const displayedRationale = message.meta.rationale_summary ?? null;
  const sourceFeedbackSummary = message.meta.source_feedback_influence?.summary ?? null;
  const historicalLens = (message.meta.historical_analogy_lenses ?? []).find(
    (lens) => lens?.name || lens?.what_rhymes || lens?.dominant_channel_test,
  );
  const nextBestStep = summary.strengthen[0] ?? null;

  return (
    <details className="rounded-lg border border-slate-200 bg-slate-50/70 px-3 py-3 text-sm dark:border-slate-800 dark:bg-slate-950/40">
      <summary className="flex cursor-pointer list-none items-center justify-between gap-3 text-xs font-medium uppercase tracking-wider text-slate-500 dark:text-slate-400">
        <span>Answer audit</span>
        {displayedConfidence ? (
          <span className="rounded-full border border-slate-200 px-2 py-1 text-[10px] dark:border-slate-700">
            {formatUserLabel(displayedConfidence)}
          </span>
        ) : null}
      </summary>
      <div className="mt-3 space-y-3">
      <div className="space-y-1 text-slate-500 dark:text-slate-400">
        {message.meta.process_mode ? (
          <p>analysis path: {formatUserLabel(message.meta.process_mode)}</p>
        ) : null}
        {message.meta.resolution_reason ? <p>{message.meta.resolution_reason}</p> : null}
        {displayedRationale ? <p>{displayedRationale}</p> : null}
        {sourceFeedbackSummary ? <p>source feedback: {sourceFeedbackSummary}</p> : null}
        {trace ? (
          <details className="pt-1">
            <summary className="cursor-pointer text-xs uppercase tracking-wider text-slate-400">
              Technical trace
            </summary>
            <p className="mt-1">
              execution {formatModelUsedLabel(trace.model_used)} · {trace.duration_ms ?? 0}ms · {trace.input_tokens ?? 0} in · {trace.output_tokens ?? 0} out
            </p>
          </details>
        ) : null}
        {trace?.evidence_packet ? (
          <>
            <p>
              evidence used: direct {trace.evidence_packet.direct_evidence_count}, connected {trace.evidence_packet.connected_evidence_count}, historical {trace.evidence_packet.historical_evidence_count}, contradiction {trace.evidence_packet.contradiction_evidence_count}
            </p>
            <EvidenceSourceList
              sources={trace.evidence_packet.sources ?? []}
              compact
            />
          </>
        ) : null}
      </div>
      {summary.researchPlan ? (
        <div className="rounded-lg border border-slate-200 p-3 dark:border-slate-800">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="text-xs font-medium uppercase tracking-wider text-slate-500 dark:text-slate-400">
              Research scope
            </p>
            <span className="text-xs text-slate-500 dark:text-slate-400">
              {formatUserLabel(summary.researchPlan.mode)}
            </span>
          </div>
          {summary.researchPlan.objective ? (
            <p className="mt-2 text-slate-700 dark:text-slate-200">
              {summary.researchPlan.objective}
            </p>
          ) : null}
          {summary.researchPlan.informationNeeds.length > 0 ? (
            <ul className="mt-2 list-disc space-y-1 pl-5 text-slate-600 dark:text-slate-300">
              {summary.researchPlan.informationNeeds.slice(0, 4).map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
      {summary.answerability ? (
        <div className="rounded-lg border border-slate-200 p-3 dark:border-slate-800">
          <p className="text-xs font-medium uppercase tracking-wider text-slate-500 dark:text-slate-400">
            Scope verdict · {formatUserLabel(summary.answerability.status)}
          </p>
          {summary.answerability.reason ? (
            <p className="mt-2 text-slate-700 dark:text-slate-200">
              {summary.answerability.reason}
            </p>
          ) : null}
        </div>
      ) : null}
      <CorroborationPanel summary={summary} />
      {historicalLens ? (
        <div className="mt-3 rounded-lg border border-slate-200 p-3 dark:border-slate-800">
          <p className="text-xs font-medium uppercase tracking-wider text-slate-500 dark:text-slate-400">
            Historical rhyme
          </p>
          <p className="mt-2 font-medium text-slate-800 dark:text-slate-100">
            {[historicalLens.name, historicalLens.period].filter(Boolean).join(" · ")}
          </p>
          <div className="mt-2 space-y-2 text-slate-600 dark:text-slate-300">
            {historicalLens.lens_use_policy ? <p>Use policy: {historicalLens.lens_use_policy}</p> : null}
            {historicalLens.current_application_prompt ? (
              <p>Application prompt: {historicalLens.current_application_prompt}</p>
            ) : null}
            {historicalLens.what_rhymes ? <p>What rhymes: {historicalLens.what_rhymes}</p> : null}
            {historicalLens.dominant_channel_test ? (
              <p>Channel test: {historicalLens.dominant_channel_test}</p>
            ) : null}
            {historicalLens.where_analogy_breaks ? (
              <p>What would break it: {historicalLens.where_analogy_breaks}</p>
            ) : null}
            {historicalLens.portfolio_transmission ? (
              <p>Portfolio tie-in: {historicalLens.portfolio_transmission}</p>
            ) : null}
            {historicalLens.best_next_check ? <p>Next check: {historicalLens.best_next_check}</p> : null}
            {historicalLens.investor_questions?.length ? (
              <div>
                <p>Open investor questions:</p>
                <ul className="mt-1 list-disc space-y-1 pl-5">
                  {historicalLens.investor_questions.slice(0, 4).map((question) => (
                    <li key={question}>{question}</li>
                  ))}
                </ul>
              </div>
            ) : null}
          </div>
        </div>
      ) : null}
      {displayedThesis || summary.gapFlags.length > 0 || nextBestStep ? (
        <div className="mt-3 grid gap-3 md:grid-cols-3">
          {displayedThesis ? (
            <div className="rounded-lg border border-slate-200 p-3 dark:border-slate-800">
              <p className="text-xs font-medium uppercase tracking-wider text-slate-500 dark:text-slate-400">
                Current read
              </p>
              <p className="mt-2 text-slate-700 dark:text-slate-200">{displayedThesis}</p>
              {(displayedStance || displayedConfidence) ? (
                <p className="mt-2 text-xs text-slate-500 dark:text-slate-400">
                  {[displayedStance ? `stance ${formatUserLabel(displayedStance)}` : null, displayedConfidence ? `confidence ${formatUserLabel(displayedConfidence)}` : null]
                    .filter(Boolean)
                    .join(" · ")}
                </p>
              ) : null}
            </div>
          ) : null}
          {summary.gapFlags.length > 0 ? (
            <div className="rounded-lg border border-slate-200 p-3 dark:border-slate-800">
              <p className="text-xs font-medium uppercase tracking-wider text-slate-500 dark:text-slate-400">
                Missing or thin
              </p>
              <ul className="mt-2 space-y-1 text-slate-700 dark:text-slate-200">
                {summary.gapFlags.slice(0, 3).map((item) => (
                  <li key={item}>• {formatUserLabel(item)}</li>
                ))}
              </ul>
            </div>
          ) : null}
          {nextBestStep ? (
            <div className="rounded-lg border border-slate-200 p-3 dark:border-slate-800">
              <p className="text-xs font-medium uppercase tracking-wider text-slate-500 dark:text-slate-400">
                Best next check
              </p>
              <p className="mt-2 text-slate-700 dark:text-slate-200">{nextBestStep}</p>
            </div>
          ) : null}
        </div>
      ) : null}
      </div>
    </details>
  );
}
