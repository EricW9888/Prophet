"use client";

import { Suspense, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import WorkspaceDivider, { useWorkspacePane } from "@/components/WorkspaceDivider";
import { useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";
import { Check, Maximize2, PanelLeftOpen, Pencil, X } from "lucide-react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import AppNav from "@/components/AppNav";
import SourceProvenanceLinks from "@/components/SourceProvenanceLinks";
import ResearchReceipt from "@/components/ResearchReceipt";
import { AttachmentButton, AttachmentDrafts, AttachmentReceipts, transferredFiles, useChatAttachments } from "@/components/ChatAttachments";
import {
  API_BASE,
  AgentConversationHistory,
  AgentConversationList,
  AgentConversationSummary,
  AgentResearchReceipt,
  AgentTurnJob,
  AgentTurnJobList,
  AgentTurn,
  ChatAttachment,
  ActiveWatcher,
  apiFetch,
  ReasoningTrace,
} from "@/lib/api";

import { formatModelUsedLabel, formatUserLabel } from "@/lib/formatting";
import { readResearchEntry, researchTurnContext, type ResearchSubject } from "@/lib/workflows";
import { ACTIVE_JOB_STORAGE_KEY, COMPLETED_JOB_NOTICE_KEY, clearCompletedJobNotice } from "@/lib/job-notifications";

const ALLOW_ACTIONS_STORAGE_KEY = "prophet:allow-agent-actions";
const RESEARCH_SIDEBAR_WIDTH = 320;
const RESEARCH_VIEWS = ["Chats", "Autonomous", "Watches"] as const;
const ACTIVE_JOBS_POLL_ACTIVE_MS = 5000;
const ACTIVE_JOBS_POLL_IDLE_MS = 15000;
const ACTIVE_JOBS_POLL_HIDDEN_MS = 60000;
const ANSWER_TEXT_CLASS = "prose prose-slate max-w-none break-words text-sm leading-7 dark:prose-invert md:text-base prose-headings:mb-2 prose-headings:mt-5 prose-headings:text-current prose-p:my-3 prose-li:my-1 prose-strong:text-current prose-a:text-sky-600 dark:prose-a:text-sky-400";
const ANSWER_COMPONENTS: Components = {
  table: ({ children }) => <div className="overflow-x-auto"><table>{children}</table></div>,
};

type Message = {
  role: "user" | "assistant" | "system";
  content: string;
  createdAt?: string;
  messageKind?: string;
  isArtifact?: boolean;
  meta?: {
    job_id?: string | null;
    process_mode?: string | null;
    attachments?: ChatAttachment[];
    resolution_reason?: string | null;
    stance?: string | null;
    confidence_band?: string | null;
    thesis_summary?: string | null;
    rationale_summary?: string | null;
    research_receipt?: AgentResearchReceipt | null;
    source_feedback_influence?: AgentTurn["source_feedback_influence"];
    historical_analogy_lenses?: AgentTurn["historical_analogy_lenses"];
    actions?: AgentTurn["actions"];
    reasoning_run_id?: string | null;
    subagents?: Record<string, string> | null;
  };
};

function isActiveTurnJob(job: Pick<AgentTurnJob, "status">) {
  return job.status === "queued" || job.status === "running" || job.status === "deferred";
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
        <span className="rounded-none border border-current/25 px-2 py-1 text-[10px] font-medium uppercase">
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
  const [loadState, setLoadState] = useState<"loading" | "ready" | "error">("loading");
  const [showAll, setShowAll] = useState(false);

  useEffect(() => {
    const load = async () => {
      try {
        const data = await apiFetch<ActiveWatcher[]>("/watcher/active");
        setWatchers(data);
        setLoadState("ready");
      } catch {
        setLoadState("error");
      }
    };
    load();
    const interval = setInterval(load, 10000);
    return () => clearInterval(interval);
  }, []);

  if (watchers.length === 0) return (
    <p className="p-4 text-sm text-muted" role="status">
      {loadState === "loading" ? "Loading watches…" : loadState === "error" ? "Watches are unavailable. Prophet will retry." : "No active watches. When Prophet monitors a condition, it will appear here."}
    </p>
  );
  const visibleWatchers = showAll ? watchers : watchers.slice(0, 12);
  const overdueCount = watchers.filter((watcher) => watcher.is_overdue).length;

  const dismissWatcher = async (watcherId: string) => {
    await apiFetch(`/watcher/${watcherId}/deactivate`, { method: "POST" });
    setWatchers((current) => current.filter((watcher) => watcher.id !== watcherId));
  };

  return (
    <section className="shrink-0 border-b border-slate-200 dark:border-slate-800">
      <h2 className="px-4 py-3 text-xs font-medium text-muted">
        <span>
          Live watches <span className="tabular-nums">({watchers.length})</span>
          {overdueCount > 0 ? (
            <span className="ml-2 text-rose-600 dark:text-rose-400">
              {overdueCount} overdue
            </span>
          ) : null}
        </span>
      </h2>
      <div className="space-y-3 px-4 pb-4">
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
        Active chats ({jobs.length})
      </p>
      <div className="space-y-2">
        {jobs.slice(0, 5).map((job) => (
          <div key={job.job_id} className="rounded-lg border border-amber-200 bg-amber-50/40 p-3 text-xs dark:border-amber-900/40 dark:bg-amber-950/10">
            <button type="button" onClick={() => onFocus(job)} className="w-full text-left">
              <div className="flex items-center justify-between gap-2">
                <span className="font-bold uppercase tracking-wider text-amber-700 dark:text-amber-300">
                  {job.status === "deferred" ? "Waiting for AI" : job.status}
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
  return <Suspense fallback={<p className="p-6 text-muted">Loading research workspace…</p>}><ChatWorkspace /></Suspense>;
}

function ChatWorkspace() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const entryKey = searchParams.toString();
  const entry = useMemo(() => readResearchEntry(new URLSearchParams(entryKey)), [entryKey]);
  const [pinnedSubject, setPinnedSubject] = useState<ResearchSubject | null>(null);
  const conversationChosenRef = useRef(entry.hasEntry);
  const selectionEpochRef = useRef(0);
  const [messages, setMessages] = useState<Message[]>([]);
  const [draftMessagesBySession, setDraftMessagesBySession] = useState<Record<string, Message[]>>({});
  const [input, setInput] = useState("");
  const [isTyping, setIsTyping] = useState(false);
  const [conversations, setConversations] = useState<AgentConversationSummary[]>([]);
  const [autonomousReviews, setAutonomousReviews] = useState<AgentConversationSummary[]>([]);
  const [researchView, setResearchView] = useState<(typeof RESEARCH_VIEWS)[number]>("Chats");
  const [openReport, setOpenReport] = useState<AgentConversationSummary | null>(null);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [selectedSubjectName, setSelectedSubjectName] = useState("Portfolio");
  const [error, setError] = useState<string | null>(null);
  const fileInputs = useChatAttachments();
  const attachments = fileInputs.attachments;
  const [useSessionAttachments, setUseSessionAttachments] = useState(true);
  const sessionAttachments = [...messages].reverse().find((message) => message.role === "assistant")?.meta?.attachments ?? [];
  const usingSessionFiles = useSessionAttachments && sessionAttachments.length > 0;
  const [focusedAnswer, setFocusedAnswer] = useState<Message | null>(null);
  const answerDialog = useRef<HTMLDialogElement>(null);
  const [allowActions, setAllowActions] = useState(false);
  const [allowActionsLoaded, setAllowActionsLoaded] = useState(false);
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  const [openTraceRunId, setOpenTraceRunId] = useState<string | null>(null);
  const [traceCache, setTraceCache] = useState<Record<string, ReasoningTrace>>({});
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [activeJob, setActiveJob] = useState<AgentTurnJob | null>(null);
  const [failedJob, setFailedJob] = useState<AgentTurnJob | null>(null);
  const [activeJobs, setActiveJobs] = useState<AgentTurnJob[]>([]);
  const [activeJobRequestMessage, setActiveJobRequestMessage] = useState<string | null>(null);
  const [renamingSessionId, setRenamingSessionId] = useState<string | null>(null);
  const [renameTitle, setRenameTitle] = useState("");
  const [renameBusy, setRenameBusy] = useState(false);
  const [mobileWorkspaceOpen, setMobileWorkspaceOpen] = useState(false);
  const researchPane = useWorkspacePane("research-sidebar", RESEARCH_SIDEBAR_WIDTH);
  const endRef = useRef<HTMLDivElement>(null);
  const messagesScrollRef = useRef<HTMLDivElement>(null);
  const lastEntryRef = useRef<string | null>(null);
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

  const loadConversations = useCallback(async () => {
    const result = await apiFetch<AgentConversationList>("/agent/conversations");
    setConversations(result.conversations);
    setAutonomousReviews(result.autonomous_reviews ?? []);
    setActiveSessionId((current) => {
      if (!conversationChosenRef.current && !current && result.conversations.length > 0) {
        return result.conversations[0].session_id;
      }
      return current;
    });
  }, []);

  function persistActiveJob(job: AgentTurnJob | null) {
    if (typeof window === "undefined") return;
    if (!job || job.status === "completed" || job.status === "cancelled") {
      window.sessionStorage.removeItem(ACTIVE_JOB_STORAGE_KEY);
      return;
    }
    const notice = window.sessionStorage.getItem(COMPLETED_JOB_NOTICE_KEY);
    if (notice) {
      try {if (JSON.parse(notice).jobId !== job.job_id) clearCompletedJobNotice();}
      catch {clearCompletedJobNotice();}
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
    setFailedJob(null);
    selectionEpochRef.current += 1;
    setPinnedSubject(null);
    conversationChosenRef.current = true;
    if (entryKey) router.replace("/chat", { scroll: false });
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
          meta: {attachments: job.request_attachments},
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

  const mergeAssistantTurn = useCallback((turn: AgentTurn, requestMessage: string, requestCreatedAt?: string | null, jobId?: string) => {
    const assistantMessage: Message = {
      role: "assistant",
      content: stripInternalReasoningBlock(turn.assistant_message),
      createdAt: turn.responded_at,
      meta: {
        job_id: jobId,
        process_mode: turn.process_mode ?? (turn.reasoning_run_id ? "reasoning_analysis" : "operating_context_llm"),
        resolution_reason: turn.resolution_reason,
        stance: turn.stance,
        confidence_band: turn.confidence_band,
        thesis_summary: turn.thesis_summary,
        rationale_summary: turn.rationale_summary,
        research_receipt: turn.research_receipt,
        attachments: turn.attachments,
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
        meta: { attachments: turn.attachments, job_id: jobId },
      };
      const hasLatestUser =
        prev.length > 0 &&
        prev[prev.length - 1]?.role === "user" &&
        prev[prev.length - 1]?.content === requestMessage;
      const nextMessages = hasLatestUser
        ? [...prev.slice(0, -1), {...prev[prev.length - 1], meta: {...prev[prev.length - 1].meta, attachments: turn.attachments, job_id: jobId}}, assistantMessage]
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
    void loadConversations();
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
    let cancelled = false;
    if (entry.hasEntry || conversationChosenRef.current) return;
    const restorationEpoch = selectionEpochRef.current;
    const raw = window.sessionStorage.getItem(ACTIVE_JOB_STORAGE_KEY);
    if (!raw) return;
    try {
      const saved = JSON.parse(raw) as { job_id?: string; request_message?: string | null };
      if (saved.job_id) {
        setActiveJobRequestMessage(saved.request_message ?? null);
        void apiFetch<AgentTurnJob>(`/agent/turn-jobs/${saved.job_id}`)
          .then((job) => {
            if (cancelled || restorationEpoch !== selectionEpochRef.current) return;
            setIsTyping(isActiveTurnJob(job));
            if (job.status === "error") {
              setFailedJob(job);
              setError(job.error || "Unable to complete this turn.");
              setActiveJobRequestMessage(null);
            } else {
              if (conversationChosenRef.current) return;
              conversationChosenRef.current = true;
              if (job.session_id) setActiveSessionId(job.session_id);
              setActiveJob(job);
            }
            persistActiveJob(job);
          })
          .catch(() => {
            if (cancelled) return;
            window.sessionStorage.removeItem(ACTIVE_JOB_STORAGE_KEY);
          });
      }
    } catch {
      window.sessionStorage.removeItem(ACTIVE_JOB_STORAGE_KEY);
    }
    return () => { cancelled = true; };
  }, [entry.hasEntry]);

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
    if (pinnedSubject && !activeSessionId) return;
    const matchingJob = activeJobs.find((job) => {
      if (!isActiveTurnJob(job)) return false;
      if (job.session_id && activeSessionId) return job.session_id === activeSessionId;
      return !job.session_id && !activeSessionId && job.job_id === activeJob?.job_id;
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
  }, [activeJob, activeJobs, activeSessionId, pinnedSubject]);

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
    let cancelled = false;
    if (!activeSessionId) {
      setLoadingHistory(false);
      return;
    }
    setLoadingHistory(true);
    void apiFetch<AgentConversationHistory>(`/agent/history?session_id=${activeSessionId}`)
      .then((history) => {
        if (cancelled) return;
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
                  job_id: entry.job_id,
                  process_mode: entry.process_mode,
                  attachments: entry.attachments,
                  resolution_reason: entry.resolution_reason,
                  stance: entry.stance,
                  confidence_band: entry.confidence_band,
                  thesis_summary: entry.thesis_summary,
                  rationale_summary: entry.rationale_summary,
                  research_receipt: entry.research_receipt,
                  source_feedback_influence: entry.source_feedback_influence,
                  historical_analogy_lenses: entry.historical_analogy_lenses,
                  actions: entry.actions,
                  reasoning_run_id: entry.reasoning_run_id,
                  subagents: entry.subagents,
                }
              : entry.attachments?.length || entry.job_id ? {attachments: entry.attachments, job_id: entry.job_id} : undefined,
        }));
        const draftMessages = draftMessagesBySession[activeSessionId] ?? [];
        const preferredMessages =
          historyMessages.length >= draftMessages.length || draftMessages.length === 0
            ? historyMessages
            : draftMessages;
        const pending = activeJobsRef.current.find((job) => job.resumable && job.session_id === activeSessionId && isActiveTurnJob(job));
        setMessages(pending && !preferredMessages.some((message) => message.meta?.job_id === pending.job_id) ? [...preferredMessages, {role: "user", content: pending.request_message, createdAt: pending.created_at, meta: {job_id: pending.job_id, attachments: pending.request_attachments}}] : preferredMessages);
        const matchedConversation = conversations.find((item) => item.session_id === activeSessionId);
        setSelectedSubjectName(
          matchedConversation?.subject_name ?? history.subject_type ?? "Portfolio",
        );
      })
      .catch((err) => {
        if (cancelled) return;
        const draftMessages = draftMessagesBySession[activeSessionId] ?? [];
        if (draftMessages.length > 0) {
          setMessages(draftMessages);
        } else {
          setMessages([]);
          setError(err instanceof Error ? err.message : "Unable to load this conversation. Select another chat or retry.");
        }
      })
      .finally(() => { if (!cancelled) setLoadingHistory(false); });
    return () => { cancelled = true; };
  }, [activeSessionId, conversations, draftMessagesBySession]);

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
    if (lastEntryRef.current === entryKey) return;
    lastEntryRef.current = entryKey;
    if (!entry.hasEntry) return;
    selectionEpochRef.current += 1;
    conversationChosenRef.current = true;
    setActiveSessionId(entry.sessionId);
    setPinnedSubject(entry.subject);
    setSelectedSubjectName(entry.subject?.subject_name ?? "Portfolio");
    setMessages([]);
    setInput(entry.question);
    setActiveJob(null);
    setIsTyping(false);
    setError(entry.invalid ? "This research link has an invalid subject or conversation. Choose a chat or describe the subject before sending." : null);
  }, [entry, entryKey]);

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
    if (!activeJob || !isActiveTurnJob(activeJob)) {
      return;
    }
    let cancelled = false;
    persistActiveJob(activeJob);
    const interval = window.setInterval(() => {
      void apiFetch<AgentTurnJob>(`/agent/turn-jobs/${activeJob.job_id}`)
        .then((job) => {
          if (cancelled) return;
          setIsTyping(isActiveTurnJob(job));
          setActiveJob(job);
          upsertActiveJob(job);
          persistActiveJob(job);
          if (job.status === "completed" && job.result) {
            setIsTyping(false);
            setError(null);
            setActiveJobRequestMessage(null);
            mergeAssistantTurn(job.result, job.request_message, job.created_at, job.job_id);
            setActiveJob(null);
            upsertActiveJob(job);
          } else if (job.status === "error") {
            setIsTyping(false);
            setError(job.error || "Unable to complete this turn.");
            setFailedJob(job);
            setActiveJobRequestMessage(null);
            setActiveJob(null);
            upsertActiveJob(job);
          }
        })
        .catch((err) => {
          if (cancelled) return;
          setIsTyping(false);
          setError(err instanceof Error ? err.message : "Unable to refresh turn status.");
        });
    }, 1200);
    return () => { cancelled = true; window.clearInterval(interval); };
  }, [activeJob, mergeAssistantTurn, entryKey]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!input.trim() || fileInputs.blocked || isTyping || Boolean(activeJob && isActiveTurnJob(activeJob))) return;

    const userMessage = input.trim();
    setFailedJob(null);
    const submittedAttachments = attachments;
    const submissionEpoch = selectionEpochRef.current;
    setInput("");
    fileInputs.clear();
    resizeComposer("");
    setError(null);
    const optimisticUserMessage = { role: "user", content: userMessage, createdAt: new Date().toISOString(), meta: {attachments: submittedAttachments.flatMap((file) => file.receipt ? [file.receipt] : [])} } as Message;
    setMessages((prev) => [...prev, optimisticUserMessage]);
    setIsTyping(true);
    setActiveJobRequestMessage(userMessage);

    try {
      const response = await fetch(`${API_BASE}/agent/turn-jobs`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...researchTurnContext(activeSessionId, pinnedSubject),
          message: userMessage,
          auto_execute: allowActions && !submittedAttachments.length && !usingSessionFiles,
          attachment_ids: submittedAttachments.flatMap((file) => file.receipt ? [file.receipt.id] : []),
          use_session_attachments: useSessionAttachments,
        }),
      });
      if (!response.ok) {
        throw new Error(await response.text());
      }
      const job = (await response.json()) as AgentTurnJob;
      upsertActiveJob(job);
      if (submissionEpoch !== selectionEpochRef.current) return;
      setActiveJob(job);
      setMessages((current) => current.map((message) => message === optimisticUserMessage ? {...message, meta: {...message.meta, job_id: job.job_id, attachments: job.request_attachments}} : message));
      if (job.resumable && job.session_id && !activeSessionId) setActiveSessionId(job.session_id);
      persistActiveJob(job);
    } catch (err) {
      if (submissionEpoch !== selectionEpochRef.current) return;
      setError(err instanceof Error ? err.message : "Unable to start this turn.");
      setMessages((previous) => previous.filter((item) => item !== optimisticUserMessage));
      setInput(userMessage);
      resizeComposer(userMessage);
      fileInputs.restore(submittedAttachments);
      setActiveJobRequestMessage(null);
      setIsTyping(false);
    }
  }

  function draftFollowUp(question: string) {
    setInput((current) => {
      const draft = current.trim() ? `${current}\n\n${question}` : question;
      return draft;
    });
    answerDialog.current?.close();
    window.requestAnimationFrame(() => {
      resizeComposer(composerRef.current?.value ?? "");
      composerRef.current?.focus();
    });
  }

  useLayoutEffect(() => {
    if (focusedAnswer && !answerDialog.current?.open) answerDialog.current?.showModal();
  }, [focusedAnswer]);

  function handleComposerKeyDown(event: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void handleSubmit(event);
    }
  }

  function startNewChat() {
    setFailedJob(null);
    if (failedJob) persistActiveJob(null);
    setUseSessionAttachments(true);
    setResearchView("Chats");
    fileInputs.clear();
    selectionEpochRef.current += 1;
    conversationChosenRef.current = true;
    setPinnedSubject(null);
    setSelectedSubjectName("Portfolio");
    setInput("");
    setActiveJob(null);
    setActiveJobRequestMessage(null);
    setIsTyping(false);
    setActiveSessionId(null);
    setMessages([]);
    setError(null);
    if (entryKey) router.replace("/chat", { scroll: false });
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
  const linkedReport = autonomousReviews.find((item) => item.session_id === activeSessionId) ?? null;
  const liveEvents = activeJob?.events.filter((event) => event.phase !== "thinking") ?? [];
  const fileContextActive = attachments.length > 0 || usingSessionFiles || Boolean(activeJob?.resumable);
  const executionActive = allowActionsLoaded && !fileContextActive ? allowActions : false;

  return (
    <div className="flex h-[100dvh] max-h-[100dvh] flex-col overflow-hidden bg-background font-sans text-foreground">
      <AppNav active="research" />

      <main className="flex min-h-0 w-full flex-1 overflow-hidden" style={researchPane.style}>
        {mobileWorkspaceOpen ? (
          <button
            type="button"
            aria-label="Close research workspace"
            onClick={closeMobileWorkspace}
            className="fixed inset-0 z-[60] bg-slate-950/45 backdrop-blur-[1px] lg:hidden"
          />
        ) : null}
        <aside
          ref={mobileWorkspacePanelRef}
          id="research-workspace-panel"
          role={mobileWorkspaceOpen ? "dialog" : undefined}
          aria-modal={mobileWorkspaceOpen ? true : undefined}
          aria-label="Research workspace"
          className={`${
            mobileWorkspaceOpen
              ? "fixed inset-y-0 left-0 z-[70] flex w-[min(88vw,360px)] shadow-2xl"
              : "hidden"
          } h-full min-h-0 shrink-0 flex-col border-r border-line bg-panel lg:relative lg:inset-auto lg:z-auto lg:w-[clamp(260px,var(--pane-width),min(720px,50%))] lg:shadow-none ${researchPane.open ? "lg:flex" : "lg:hidden"}`}
        >
          {mobileWorkspaceOpen ? (
            <div className="flex items-center justify-between border-b border-slate-200 px-4 py-3 dark:border-slate-800 lg:hidden">
              <p className="text-sm font-semibold">Research workspace</p>
              <button
                type="button"
                autoFocus
                onClick={closeMobileWorkspace}
                className="inline-flex h-9 w-9 items-center justify-center rounded border border-slate-200 text-slate-500 dark:border-slate-800 dark:text-slate-400"
                title="Close research workspace"
              >
                <X className="h-4 w-4" />
                <span className="sr-only">Close research workspace</span>
              </button>
            </div>
          ) : null}
          <div role="group" aria-label="Research views" className="grid grid-cols-3 gap-1 border-b border-line p-2">
            {RESEARCH_VIEWS.map((view) => (
              <button key={view} type="button" aria-pressed={view === researchView} className={`min-w-0 border px-1 py-2 text-xs font-medium ${view === researchView ? "border-line-strong bg-background text-foreground" : "border-transparent text-muted hover:text-foreground"}`}
                onClick={() => {
                  setResearchView(view);
                  if (view === "Autonomous") void loadConversations().catch(() => setError("Autonomous reviews could not be refreshed."));
                }}>{view}</button>
            ))}
          </div>
          {researchView === "Watches" ? <div className="min-h-0 flex-1 overflow-y-auto"><LiveWatchers /></div> : null}
          {researchView === "Autonomous" ? <AutonomousReviewList reviews={autonomousReviews} onOpen={(report) => { setOpenReport(report); setMobileWorkspaceOpen(false); }} /> : null}
          {researchView === "Chats" ? <>
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
                        : "border-line bg-panel "
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
                            selectionEpochRef.current += 1;
                            conversationChosenRef.current = true;
                            setPinnedSubject(null);
                            setInput("");
                            setActiveJob(null);
                            setActiveJobRequestMessage(null);
                            setIsTyping(false);
                            setMessages([]);
                            setError(null);
                            setActiveSessionId(conversation.session_id);
                            if (entryKey) router.replace("/chat", { scroll: false });
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
          </> : null}
        </aside>
        {researchPane.open ? (
          <WorkspaceDivider side="left" controls="research-workspace-panel" label="Resize research sidebar" width={researchPane.width} defaultWidth={RESEARCH_SIDEBAR_WIDTH} onResize={researchPane.setWidth} />
        ) : null}

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
                      {activeConversation?.title ?? linkedReport?.title ?? "New chat"}
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
                  {linkedReport
                    ? "Saved background review. Open the report to start a separate follow-up."
                    : activeConversation
                    ? `Talking with Prophet about ${activeConversation.subject_name ?? selectedSubjectName}.`
                    : pinnedSubject
                      ? `Research context: ${pinnedSubject.subject_name ?? pinnedSubject.subject_type}. Review the draft, then send when ready.`
                      : "Ask a question naturally. Prophet will usually resolve the right context for you."}
                </p>
                {!activeSessionId && pinnedSubject && <div className="mt-2 flex flex-wrap items-center gap-3 text-xs text-muted">
                  <span>No research has been started by opening this link.</span>
                  <button type="button" disabled={isTyping} onClick={() => { setPinnedSubject(null); setSelectedSubjectName("Portfolio"); if (entryKey) router.replace("/chat", { scroll: false }); }} className="text-action underline disabled:opacity-50">Release subject context</button>
                  <Link href="/verification" className="text-action underline">Return to review</Link>
                </div>}
              </div>
              <div className="flex flex-wrap gap-2">
                <button
                  ref={mobileWorkspaceTriggerRef}
                  type="button"
                  onClick={() => setMobileWorkspaceOpen(true)}
                  className="workspace-control lg:hidden"
                  aria-expanded={mobileWorkspaceOpen}
                  aria-controls="research-workspace-panel"
                >
                  <PanelLeftOpen className="h-4 w-4" aria-hidden="true" />
                  Browse research
                </button>
                <button
                  type="button"
                  onClick={researchPane.toggle}
                  className="workspace-control hidden lg:inline-flex"
                  aria-expanded={researchPane.open}
                  aria-controls="research-workspace-panel"
                >
                  <PanelLeftOpen size={16} aria-hidden="true" />
                  {researchPane.open ? "Hide sidebar" : "Show sidebar"}
                </button>
                <button
                  type="button"
                  onClick={startNewChat}
                  className="workspace-control lg:hidden"
                >
                  New chat
                </button>
              </div>
            </div>

            {/* Settings and Dossier quick create merged to Settings page */}

            {error ? <p role="alert" className="mt-3 text-sm text-red-500">{error}</p> : null}
            {failedJob ? <div className="mt-2 flex flex-wrap items-center gap-2 text-sm">
              <span className="text-muted">The failed question and its files are retained.</span>
              <button type="button" className="workspace-control" disabled={Boolean(input.trim() || attachments.length || isTyping)} onClick={() => {
                const job = failedJob;
                selectionEpochRef.current += 1;
                conversationChosenRef.current = true;
                setPinnedSubject(job.request_subject_id && job.request_subject_type ? {subject_id: job.request_subject_id, subject_type: job.request_subject_type} : null);
                setInput(job.request_message);
                fileInputs.restore((job.request_attachments ?? []).map((receipt) => ({key: receipt.id, name: receipt.name, bytes: receipt.size_bytes, state: "ready", receipt})));
                setUseSessionAttachments(job.use_session_attachments ?? true);
                setActiveSessionId(job.session_id ?? null);
                setMessages((current) => !job.session_id || job.session_id !== activeSessionId ? [] : current.at(-1)?.role === "user" && current.at(-1)?.content === job.request_message ? current.slice(0, -1) : current);
                setFailedJob(null); setError(null); persistActiveJob(null);
              }}>Restore question and files</button>
              {input.trim() || attachments.length ? <span className="text-xs text-muted">Send or clear your current draft first.</span> : null}
            </div> : null}
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
                  <button onClick={() => setInput("What are the core vulnerabilities in our largest tech holding?")} className="rounded-none border border-line bg-panel px-4 py-2 text-sm text-slate-600 hover:border-slate-300  dark:text-slate-300 dark:hover:border-slate-700">What are the core vulnerabilities in our largest tech holding?</button>
                  <button onClick={() => setInput("Summarize recent developments for Apple.")} className="rounded-none border border-line bg-panel px-4 py-2 text-sm text-slate-600 hover:border-slate-300  dark:text-slate-300 dark:hover:border-slate-700">Summarize recent developments for Apple.</button>
                </div>
              </div>
            ) : (
              <div className="space-y-6">
                {messages.map((m, idx) => (linkedReport && m.role === "user" && m.content.startsWith("Autonomous review for ") ? null : (
                  <div key={idx} className={`flex w-full ${m.role === "user" ? "justify-end" : "justify-start"}`}>
                    <div className={`flex min-w-0 gap-4 ${m.role === "user" ? "w-fit max-w-[85%] flex-row-reverse" : "w-full flex-row"}`}>
                      {m.role === "system" ? <div aria-hidden="true" className="mt-1 shrink-0 font-mono text-xs text-muted">S</div> : null}
                      <div
                        className={`min-w-0 text-sm leading-relaxed md:text-base ${
                          m.role === "user"
                            ? "w-fit rounded-none bg-panel px-4 py-3 text-foreground"
                            : m.role === "system"
                              ? "border border-dashed border-slate-200 bg-slate-50 text-slate-700 dark:border-slate-800 dark:bg-slate-950 dark:text-slate-300"
                            : "w-full bg-transparent py-3 text-foreground"
                        }`}
                      >
                        {formatTimestamp(m.createdAt) || m.role === "assistant" ? (
                          <div className={`mb-2 flex flex-wrap items-center gap-2 ${m.role === "user" ? "justify-end" : "justify-between"}`}>
                            <p className="text-[11px] uppercase tracking-wide text-muted">{formatTimestamp(m.createdAt)}</p>
                            {m.role === "assistant" ? (
                              <button type="button" className="workspace-control" onClick={() => setFocusedAnswer(m)}><Maximize2 size={14} aria-hidden="true" /> Full screen</button>
                            ) : null}
                          </div>
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
                          <div className={ANSWER_TEXT_CLASS}>
                            <ReactMarkdown remarkPlugins={[remarkGfm]} components={ANSWER_COMPONENTS}>
                              {m.content}
                            </ReactMarkdown>
                          </div>
                        ) : (
                          <div className="whitespace-pre-wrap break-words leading-7">{m.content}</div>
                        )}
                        {!linkedReport && m.role === "assistant" && (m.meta?.research_receipt || ["attached_text", "attached_files"].includes(m.meta?.process_mode ?? "")) && !messages.slice(idx + 1).some((entry) => entry.role === "assistant") ? (
                          <button type="button" className="workspace-control mt-3" onClick={() => draftFollowUp("What evidence would change your conclusion, and how would you check it?")}>Draft follow-up</button>
                        ) : null}
                        <AttachmentReceipts attachments={m.meta?.attachments} showReadStatus={m.role === "assistant"} />
                        {m.meta?.research_receipt ? <ResearchReceipt receipt={m.meta.research_receipt} /> : null}
                        {m.role === "assistant" && m.meta ? (
                          <div className="mt-4 space-y-3 border-t border-slate-200 pt-4 dark:border-slate-800">
                            <ActualProcessSummary
                              message={m}
                              trace={m.meta.reasoning_run_id ? traceCache[m.meta.reasoning_run_id] : undefined}
                            />
                            <div className="flex flex-wrap gap-2 text-xs">
                              {m.meta.stance ? (
                                <span className="rounded-none border border-slate-200 px-3 py-1 dark:border-slate-700">
                                  Stance: {formatUserLabel(m.meta.stance)}
                                </span>
                              ) : null}
                              {m.meta.confidence_band ? (
                                <span className="rounded-none border border-slate-200 px-3 py-1 dark:border-slate-700">
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
                )))}
                {isTyping ? (
                  <section className="w-full border-l-2 border-line pl-4 py-3 text-sm" aria-label="Research activity">
                    <div className="flex flex-wrap justify-between gap-2">
                      <span className="font-semibold">{activeJob?.status === "deferred" ? "Waiting for AI" : liveEvents.at(-1) ? formatUserLabel(liveEvents.at(-1)!.phase) : "Starting turn"}</span>
                      <span className="text-xs tabular-nums text-muted">{elapsedSeconds}s elapsed</span>
                    </div>
                    <p role="status" className="mt-1 text-muted">{liveEvents.at(-1)?.message ?? "Sending your request to Prophet."}</p>
                    {activeJob?.status === "deferred" && activeJob.retry_at ? <p className="mt-1 text-xs text-muted">Next planned availability check: {formatTimestamp(activeJob.retry_at)}. You can leave this page; Cancel stops this question.</p> : null}
                    {activeJobRequestMessage && !messages.some((message) => message.role === "user" && message.content.startsWith(activeJobRequestMessage)) ? (
                      <p className="mt-1 truncate text-xs text-muted">{activeJobRequestMessage}</p>
                    ) : null}
                    {liveEvents.length ? (
                      <details className="mt-3 border-t border-line pt-2">
                        <summary className="cursor-pointer text-xs font-medium">Work log · {liveEvents.length} updates</summary>
                        <ol className="mt-2 max-h-64 space-y-2 overflow-auto">
                          {liveEvents.map((event, index) => {
                            const detail = event.detail ?? event.metadata;
                            const query = typeof detail?.query === "string" ? detail.query : null;
                            const direct = detail?.direct_evidence_count;
                            const linked = detail?.holdings_with_evidence;
                            return (
                              <li key={`${event.phase}-${event.created_at}-${index}`} className="border-l border-line pl-2">
                                <p><span className="font-medium">{formatUserLabel(event.phase)}:</span> {event.message}</p>
                                {query ? <p className="break-words text-xs text-muted">Search: {query}</p> : null}
                                {typeof direct === "number" ? <p className="text-xs text-muted">{direct} direct evidence items · {Number(detail?.contradiction_evidence_count ?? 0)} contradictory</p> : null}
                                {Array.isArray(linked) && typeof detail?.holding_count === "number" ? <p className="text-xs text-muted">{linked.length} of {detail.holding_count} holdings linked in this packet</p> : null}
                              </li>
                            );
                          })}
                        </ol>
                      </details>
                    ) : null}
                  </section>
                ) : null}
                <div ref={endRef} />
              </div>
            )}
          </div>

          <div className="shrink-0 border-t border-line bg-background/90 px-4 py-3 backdrop-blur sm:px-6">
            {linkedReport ? <button type="button" className="workspace-control" onClick={() => setOpenReport(linkedReport)}>Open autonomous review</button> : <form onSubmit={handleSubmit} className="w-full" onPointerDownCapture={() => {conversationChosenRef.current = true;}} onKeyDownCapture={(event) => {if (event.key !== "Tab") conversationChosenRef.current = true;}} onDragOver={(event) => {if (event.dataTransfer.types.includes("Files")) event.preventDefault();}} onDrop={(event) => {if (event.dataTransfer.files.length) {event.preventDefault(); conversationChosenRef.current = true; fileInputs.addFiles(event.dataTransfer.files);}}}>
              <div className="mb-2 flex min-h-8 items-center justify-between gap-3">
                <button
                  type="button"
                  role="switch"
                  aria-checked={executionActive}
                  disabled={!allowActionsLoaded || fileContextActive}
                  onClick={() => setAllowActions((current) => !current)}
                  title={fileContextActive ? "File questions only analyze the attached files." : "Allow Prophet to use enabled research and system actions for new turns"}
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
                <div className="ml-auto flex items-center gap-2">
                  <AttachmentButton files={fileInputs} disabled={isTyping} />
                </div>
                {activeJob && isActiveTurnJob(activeJob) ? (
                  <span className="truncate text-xs text-muted">Turn running in background</span>
                ) : null}
              </div>
              {sessionAttachments.length ? <label className="mb-2 flex items-center gap-2 text-xs text-muted"><input type="checkbox" checked={useSessionAttachments} disabled={isTyping} onChange={(event) => setUseSessionAttachments(event.target.checked)} /> Include files from this chat: {sessionAttachments.map((file) => file.name).join(", ")}</label> : null}
              <AttachmentDrafts files={fileInputs} />
              <div className="relative">
                <textarea
                  ref={composerRef}
                  value={input}
                  onChange={(e) => {
                    setInput(e.target.value);
                    resizeComposer(e.target.value);
                  }}
                  onKeyDown={handleComposerKeyDown}
                  onPaste={(event) => {
                    const pastedFiles = transferredFiles(event.clipboardData);
                    if (!pastedFiles.length) return;
                    event.preventDefault();
                    fileInputs.addFiles(pastedFiles);
                    const pasted = event.clipboardData.getData("text/plain");
                    if (pasted) {const field = event.currentTarget; const position = field.selectionStart; const next = input.slice(0, position) + pasted + input.slice(field.selectionEnd); setInput(next); resizeComposer(next); window.requestAnimationFrame(() => field.setSelectionRange(position + pasted.length, position + pasted.length));}
                  }}
                  placeholder="Tell Prophet what you think, what changed, or what to analyze next..."
                  rows={1}
                  className="min-h-[64px] w-full resize-none overflow-y-auto rounded-lg border border-slate-300 bg-white py-4 pl-5 pr-20 outline-none transition-[border-color,box-shadow,height] focus:border-sky-500 focus:ring-2 focus:ring-sky-500 dark:border-slate-700 dark:bg-slate-900 dark:text-white"
                />
                <button
                  type="submit"
                  disabled={fileInputs.blocked || isTyping || Boolean(activeJob && isActiveTurnJob(activeJob)) || !input.trim()}
                  className="absolute bottom-2.5 right-2.5 rounded-lg bg-sky-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-sky-700 disabled:opacity-50"
                >
                  Send
                </button>
              </div>
            </form>}
          </div>
        </section>
      </main>
      {openReport ? <AutonomousReport report={openReport} onClose={() => setOpenReport(null)} onDiscuss={(summary) => {
        startNewChat();
        if (openReport.subject_id && openReport.subject_type) {
          setPinnedSubject({ subject_id: openReport.subject_id, subject_type: openReport.subject_type, subject_name: openReport.subject_name ?? undefined });
        }
        const draft = `Follow up on Prophet’s autonomous review of ${openReport.subject_name ?? "this subject"}. What evidence supports the finding, and what is still uncertain?\n\nFinding to check: ${summary}`;
        setInput(draft);
        setOpenReport(null);
        window.requestAnimationFrame(() => { resizeComposer(draft); composerRef.current?.focus(); });
      }} /> : null}
      <dialog ref={answerDialog} onClose={() => setFocusedAnswer(null)} aria-labelledby="focused-answer-title" className="fixed inset-0 m-0 h-dvh max-h-none w-screen max-w-none border-none bg-background p-0 text-foreground backdrop:bg-background">
        {focusedAnswer ? (
          <div className="flex min-h-full flex-col">
            <div className="sticky top-0 z-10 flex items-center justify-between border-b border-line bg-background px-4 py-3 sm:px-8">
              <h2 id="focused-answer-title" className="font-semibold">Prophet answer</h2>
              <button type="button" className="workspace-control" onClick={() => answerDialog.current?.close()}><X size={16} aria-hidden="true" /> Close full screen</button>
            </div>
            <article className="w-full px-4 py-6 sm:px-8">
              <div className={ANSWER_TEXT_CLASS}><ReactMarkdown remarkPlugins={[remarkGfm]} components={ANSWER_COMPONENTS}>{focusedAnswer.content}</ReactMarkdown></div>
              {focusedAnswer.meta?.rationale_summary ? <p className="mt-5 border-t border-line pt-4 text-sm"><strong>Rationale:</strong> {focusedAnswer.meta.rationale_summary}</p> : null}
              {focusedAnswer.meta?.research_receipt ? <ResearchReceipt receipt={focusedAnswer.meta.research_receipt} /> : null}
            </article>
          </div>
        ) : null}
      </dialog>
    </div>
  );
}

function AutonomousReviewList({ reviews, onOpen }: { reviews: AgentConversationSummary[]; onOpen: (review: AgentConversationSummary) => void }) {
  const [query, setQuery] = useState("");
  const [expandedGroup, setExpandedGroup] = useState<string | null>(null);
  const grouped = useMemo(() => {
    const groups = new Map<string, AgentConversationSummary[]>();
    const term = query.trim().toLowerCase();
    for (const report of reviews) {
      if (term && ![report.subject_name, report.title, report.autonomous_reason, report.latest_message_preview].some((text) => text?.toLowerCase().includes(term))) continue;
      const key = report.subject_id ? `${report.subject_type}:${report.subject_id}` : report.subject_name ?? report.title;
      const group = groups.get(key) ?? [];
      group.push(report);
      groups.set(key, group);
    }
    return [...groups.entries()];
  }, [query, reviews]);
  function reportButton(report: AgentConversationSummary, earlier = false) {
    return (
      <button key={report.session_id} type="button" className="block w-full border border-line p-3 text-left hover:border-line-strong" onClick={() => onOpen(report)}>
        <span className="flex flex-wrap items-baseline justify-between gap-2">
          <strong className="text-sm">{earlier ? "Earlier review" : report.subject_name ?? report.title}</strong>
          <time className="text-xs text-muted" dateTime={report.updated_at}>{formatTimestamp(report.updated_at)}</time>
        </span>
        {report.autonomous_reason ? <span className="mt-1 line-clamp-2 text-xs text-muted">{report.autonomous_reason}</span> : null}
        {report.latest_message_preview ? <span className="mt-2 line-clamp-2 text-xs text-muted">{stripInternalReasoningBlock(report.latest_message_preview)}</span> : null}
      </button>
    );
  }
  return (
    <div className="min-h-0 flex-1 space-y-2 overflow-y-auto p-3">
      <p className="text-xs text-muted">Latest finding for each subject. Open it to see why Prophet looked and what it found.</p>
      {reviews.length ? <input value={query} onChange={(event) => setQuery(event.target.value)} aria-label="Search autonomous findings" placeholder="Search findings" className="w-full border border-line bg-background px-2 py-2 text-sm" /> : null}
      {!grouped.length ? <p className="text-sm text-muted">{reviews.length ? "No findings match this search." : "No saved autonomous reviews yet."}</p> : null}
      {grouped.map(([key, group]) => {
        const [latest, ...earlier] = group;
        return (
          <div key={key} className="space-y-1">
            {reportButton(latest)}
            {earlier.length ? <button type="button" aria-expanded={expandedGroup === key} className="w-full py-1 text-left text-xs text-action" onClick={() => setExpandedGroup(expandedGroup === key ? null : key)}>
              {expandedGroup === key ? "Hide earlier reviews" : `Show ${earlier.length} earlier ${earlier.length === 1 ? "review" : "reviews"}`}
            </button> : null}
            {expandedGroup === key ? <div className="space-y-1 border-l border-line pl-2">{earlier.map((report) => reportButton(report, true))}</div> : null}
          </div>
        );
      })}
    </div>
  );
}

function AutonomousReport({
  report,
  onClose,
  onDiscuss,
}: {
  report: AgentConversationSummary;
  onClose: () => void;
  onDiscuss: (summary: string) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [finding, setFinding] = useState<AgentConversationHistory["entries"][number] | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  useLayoutEffect(() => { dialog.current?.showModal(); }, []);
  useEffect(() => {
    const controller = new AbortController();
    void apiFetch<AgentConversationHistory>(`/agent/history?session_id=${report.session_id}`, { signal: controller.signal })
      .then((history) => {
        if (controller.signal.aborted) return;
        setFinding(history.entries.find((entry) => entry.role === "assistant" && !entry.is_artifact) ?? null);
        setState("ready");
      }, () => { if (!controller.signal.aborted) setState("error"); });
    return () => controller.abort();
  }, [report.session_id]);
  const summary = finding ? stripInternalReasoningBlock(finding.content) : "";
  return (
    <dialog ref={dialog} onClose={onClose} aria-labelledby="autonomous-report-title"
      className="fixed inset-0 m-0 h-dvh max-h-none w-screen max-w-none border-none bg-background p-0 text-foreground backdrop:bg-background">
      <article className="min-h-full w-full px-4 py-5 sm:px-8">
        <header className="mb-5 flex flex-wrap items-start justify-between gap-3 border-b border-line pb-4">
          <div>
            <p className="text-xs font-semibold uppercase tracking-wide text-muted">Autonomous finding · {formatTimestamp(report.updated_at)}</p>
            <h2 id="autonomous-report-title" className="mt-1 text-xl font-semibold">{report.subject_name ?? report.title}</h2>
          </div>
          <button type="button" className="workspace-control" onClick={() => dialog.current?.close()}><X size={15} aria-hidden="true" /> Close review</button>
        </header>
        {report.autonomous_reason ? <p className="mb-4 border-l-2 border-line-strong pl-3 text-sm"><strong>Why Prophet looked:</strong> {report.autonomous_reason}</p> : null}
        {state !== "ready" || !finding ? (
          <p role="status" className="text-sm text-muted">{state === "loading" ? "Loading the saved review…" : state === "error" ? "The saved review could not be loaded. Close and reopen it to retry." : "No completed finding was saved for this review."}</p>
        ) : (
          <>
            <div className={ANSWER_TEXT_CLASS}><ReactMarkdown remarkPlugins={[remarkGfm]} components={ANSWER_COMPONENTS}>{summary}</ReactMarkdown></div>
            {finding.rationale_summary ? <p className="mt-4 text-sm"><strong>Rationale:</strong> {finding.rationale_summary}</p> : null}
            {finding.research_receipt ? <ResearchReceipt receipt={finding.research_receipt} /> : null}
            <div className="mt-5 flex flex-wrap gap-2 border-t border-line pt-4">
              <button type="button" className="workspace-control" onClick={() => onDiscuss(summary.replace(/\s+/g, " ").slice(0, 480))}>Discuss in a new chat</button>
              <Link href="/activity" className="workspace-control">See Activity</Link>
            </div>
          </>
        )}
      </article>
    </dialog>
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
        <span className="rounded-none border border-slate-200 px-2 py-1 dark:border-slate-800">{formatUserLabel(trace.run_type)}</span>
        <span className="rounded-none border border-slate-200 px-2 py-1 dark:border-slate-800">{formatModelUsedLabel(trace.model_used)}</span>
        <span className="rounded-none border border-slate-200 px-2 py-1 dark:border-slate-800">{trace.input_tokens ?? 0} in</span>
        <span className="rounded-none border border-slate-200 px-2 py-1 dark:border-slate-800">{trace.output_tokens ?? 0} out</span>
        <span className="rounded-none border border-slate-200 px-2 py-1 dark:border-slate-800">{trace.duration_ms ?? 0}ms</span>
      </div>
      {researchPlan ? (
        <div className="rounded-lg border border-slate-200 p-3 dark:border-slate-800">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="font-medium">Question research plan</p>
            <span className="rounded-none border border-slate-200 px-2 py-1 text-xs dark:border-slate-800">
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
              <span className="rounded-none border border-slate-200 px-2 py-1 dark:border-slate-800">
                stance {formatUserLabel(stance)}
              </span>
            ) : null}
            {confidenceBand ? (
              <span className="rounded-none border border-slate-200 px-2 py-1 dark:border-slate-800">
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
                    className="rounded-none border border-amber-300/60 bg-amber-50 px-2 py-1 text-xs text-amber-800 dark:border-amber-700/60 dark:bg-amber-950/30 dark:text-amber-200"
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
                    className="rounded-none border border-slate-200 px-2 py-1 text-xs dark:border-slate-800"
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
          <span className="rounded-none border border-slate-200 px-2 py-1 text-[10px] dark:border-slate-700">
            {formatUserLabel(displayedConfidence)}
          </span>
        ) : null}
      </summary>
      <div className="mt-3 space-y-3">
      <p className="text-xs text-muted">Check the original evidence and its dates before relying on this answer.</p>
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
