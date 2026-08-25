"use client";

import Link from "next/link";
import {
  AlertTriangle,
  ArrowUpRight,
  Check,
  ChevronDown,
  CircleDot,
  Clock3,
  Database,
  Eye,
  FlaskConical,
  LoaderCircle,
  Play,
  Plus,
  RefreshCw,
  Search,
  Settings2,
  Trash2,
  X,
} from "lucide-react";
import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";

import AppNav from "@/components/AppNav";
import FloatingNotice from "@/components/FloatingNotice";
import {
  apiFetch,
  AutomationStatus,
  IntegrationSettings,
  OpportunityCandidate,
  OpportunityDiscoveryRun,
  OpportunityUniverseImportPreview,
  OpportunityUniverseImportResult,
  OpportunityUniverseImportSource,
  OpportunityUniverseMember,
} from "@/lib/api";

type WorkspaceTab = "queue" | "universe" | "runs";
type CandidateFilter =
  | "all"
  | "new"
  | "monitoring"
  | "shadow_tested"
  | "rejected"
  | "expired";

const candidateFilters: Array<{ value: CandidateFilter; label: string }> = [
  { value: "all", label: "All" },
  { value: "new", label: "New" },
  { value: "monitoring", label: "Monitoring" },
  { value: "shadow_tested", label: "Shadow tested" },
  { value: "rejected", label: "Rejected" },
  { value: "expired", label: "Expired" },
];

const fieldClass =
  "w-full rounded-md border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 outline-none transition focus:border-gray-500 focus:ring-2 focus:ring-gray-200 dark:border-gray-700 dark:bg-gray-950 dark:text-gray-100 dark:focus:border-gray-500 dark:focus:ring-gray-800";
const DISCOVERY_RUN_TIMEOUT_MS = 120000;

function formatDate(value?: string | null): string {
  if (!value) return "Not yet";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Unknown" : date.toLocaleString();
}

function formatDuration(seconds: number): string {
  if (seconds % 86400 === 0) return `${seconds / 86400}d`;
  if (seconds % 3600 === 0) return `${seconds / 3600}h`;
  return `${Math.round(seconds / 60)}m`;
}

function displayValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "Not recorded";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value).replaceAll("_", " ");
}

function statusTone(status: string): string {
  if (status === "new") return "border-blue-200 bg-blue-50 text-blue-700 dark:border-blue-900 dark:bg-blue-950/40 dark:text-blue-300";
  if (status === "monitoring") return "border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-300";
  if (status === "shadow_tested") return "border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-900 dark:bg-emerald-950/40 dark:text-emerald-300";
  return "border-gray-200 bg-gray-100 text-gray-600 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-300";
}

function EvidenceItem({ item, index }: { item: Record<string, unknown>; index: number }) {
  const title = displayValue(item.title ?? item.source_name ?? item.ref ?? `Evidence ${index + 1}`);
  const url = typeof item.url === "string" ? item.url : null;
  const detail = item.summary ?? item.text ?? item.claim ?? item.detail;
  const timestamp = item.public_time ?? item.captured_at ?? item.created_at;

  return (
    <li className="min-w-0 border-l-2 border-gray-200 pl-3 dark:border-gray-700">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <p className="min-w-0 break-words text-sm font-medium text-gray-900 dark:text-gray-100">{title}</p>
        {url ? (
          <a
            href={url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 text-xs text-blue-600 hover:underline dark:text-blue-400"
          >
            Source <ArrowUpRight className="h-3 w-3" />
          </a>
        ) : null}
      </div>
      {detail ? <p className="mt-1 break-words text-sm text-gray-600 dark:text-gray-300">{displayValue(detail)}</p> : null}
      {timestamp ? <p className="mt-1 text-xs text-gray-400">{formatDate(String(timestamp))}</p> : null}
    </li>
  );
}

export default function OpportunitiesPage() {
  const [tab, setTab] = useState<WorkspaceTab>("queue");
  const [filter, setFilter] = useState<CandidateFilter>("all");
  const [candidates, setCandidates] = useState<OpportunityCandidate[]>([]);
  const [universe, setUniverse] = useState<OpportunityUniverseMember[]>([]);
  const [importPreview, setImportPreview] = useState<OpportunityUniverseImportPreview | null>(null);
  const [importPreviewError, setImportPreviewError] = useState<string | null>(null);
  const [runs, setRuns] = useState<OpportunityDiscoveryRun[]>([]);
  const [settings, setSettings] = useState<IntegrationSettings | null>(null);
  const [automation, setAutomation] = useState<AutomationStatus | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [expandedRun, setExpandedRun] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [reviewReason, setReviewReason] = useState("");
  const [ticker, setTicker] = useState("");
  const [entityName, setEntityName] = useState("");
  const [priority, setPriority] = useState("0.5");
  const [shadowBasis, setShadowBasis] = useState<"cash_only" | "clone_portfolio">("cash_only");
  const [startingCash, setStartingCash] = useState("10000");

  const loadState = useCallback(async () => {
    try {
      const importRequest = apiFetch<OpportunityUniverseImportPreview>(
        "/opportunities/universe/import-preview",
      )
        .then((data) => ({ data, error: null }))
        .catch((err: unknown) => ({
          data: null,
          error: err instanceof Error ? err.message : "Import preview is unavailable.",
        }));
      const [candidateData, universeData, importResult, runData, settingsData, automationData] = await Promise.all([
        apiFetch<OpportunityCandidate[]>("/opportunities/candidates?limit=500"),
        apiFetch<OpportunityUniverseMember[]>("/opportunities/universe"),
        importRequest,
        apiFetch<OpportunityDiscoveryRun[]>("/opportunities/runs?limit=50"),
        apiFetch<IntegrationSettings>("/integrations/settings"),
        apiFetch<AutomationStatus>("/automation/status"),
      ]);
      setCandidates(candidateData);
      setUniverse(universeData);
      setImportPreview(importResult.data);
      setImportPreviewError(importResult.error);
      setRuns(runData);
      setSettings(settingsData);
      setAutomation(automationData);
      setSelectedId((current) =>
        current && candidateData.some((candidate) => candidate.id === current)
          ? current
          : candidateData[0]?.id ?? null,
      );
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to load opportunity discovery.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadState();
    const timer = window.setInterval(loadState, 30000);
    return () => window.clearInterval(timer);
  }, [loadState]);

  const filteredCandidates = useMemo(
    () => candidates.filter((candidate) => filter === "all" || candidate.status === filter),
    [candidates, filter],
  );
  const selected = candidates.find((candidate) => candidate.id === selectedId) ?? null;
  const latestRun = runs[0] ?? null;
  const job = automation?.jobs.find((item) => item.name === "opportunity_discovery");
  const dueCount = universe.filter((member) => {
    if (!member.enabled) return false;
    if (!member.next_inspection_at) return true;
    return new Date(member.next_inspection_at).getTime() <= Date.now();
  }).length;

  function updateDiscoverySettings(
    field: keyof IntegrationSettings["opportunity_discovery"],
    value: boolean | number,
  ) {
    setSettings((current) =>
      current
        ? {
            ...current,
            opportunity_discovery: {
              ...current.opportunity_discovery,
              [field]: value,
            },
          }
        : current,
    );
  }

  async function saveDiscoverySettings() {
    if (!settings) return;
    setBusy("settings");
    setError(null);
    try {
      const updated = await apiFetch<IntegrationSettings>("/integrations/settings", {
        method: "PUT",
        body: JSON.stringify({ opportunity_discovery: settings.opportunity_discovery }),
      });
      setSettings(updated);
      setAutomation(await apiFetch<AutomationStatus>("/automation/status"));
      setNotice("Discovery controls saved and applied to the scheduler.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to save discovery controls.");
    } finally {
      setBusy(null);
    }
  }

  async function runDiscovery() {
    setBusy("run");
    setError(null);
    try {
      await apiFetch<AutomationStatus>(
        "/automation/run/opportunity_discovery",
        { method: "POST" },
        DISCOVERY_RUN_TIMEOUT_MS,
      );
      await loadState();
      setNotice("Bounded discovery run completed. Review the run telemetry and any new candidate.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to run opportunity discovery.");
    } finally {
      setBusy(null);
    }
  }

  async function reviewCandidate(status: "new" | "monitoring" | "rejected" | "expired") {
    if (!selected) return;
    setBusy(`review-${status}`);
    setError(null);
    try {
      const updated = await apiFetch<OpportunityCandidate>(
        `/opportunities/candidates/${selected.id}/review`,
        {
          method: "POST",
          body: JSON.stringify({ status, reason: reviewReason.trim() || null }),
        },
      );
      setCandidates((current) => current.map((item) => (item.id === updated.id ? updated : item)));
      setReviewReason(updated.review_reason ?? "");
      setNotice(`Candidate moved to ${displayValue(status)}.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to update candidate review state.");
    } finally {
      setBusy(null);
    }
  }

  async function startShadowTest() {
    if (!selected) return;
    setBusy("shadow");
    setError(null);
    try {
      const cash = shadowBasis === "cash_only" ? Number(startingCash) : undefined;
      const updated = await apiFetch<OpportunityCandidate>(
        `/opportunities/candidates/${selected.id}/shadow-test`,
        {
          method: "POST",
          body: JSON.stringify({ account_basis: shadowBasis, starting_cash: cash }),
        },
      );
      setCandidates((current) => current.map((item) => (item.id === updated.id ? updated : item)));
      setNotice("Paper-only shadow experiment started. No real holding or order was created.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to start shadow test.");
    } finally {
      setBusy(null);
    }
  }

  async function addUniverseMember(event: FormEvent) {
    event.preventDefault();
    setBusy("add-member");
    setError(null);
    try {
      await apiFetch<OpportunityUniverseMember>("/opportunities/universe", {
        method: "POST",
        body: JSON.stringify({
          ticker: ticker.trim().toUpperCase(),
          entity_name: entityName.trim() || null,
          priority: Number(priority),
          enabled: true,
        }),
      });
      setTicker("");
      setEntityName("");
      setPriority("0.5");
      await loadState();
      setNotice("Security added to the bounded discovery universe.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to add universe member.");
    } finally {
      setBusy(null);
    }
  }

  async function updateUniverseMember(
    member: OpportunityUniverseMember,
    update: { enabled?: boolean; priority?: number },
  ) {
    setBusy(`member-${member.id}`);
    setError(null);
    try {
      const updated = await apiFetch<OpportunityUniverseMember>(
        `/opportunities/universe/${member.id}`,
        { method: "PATCH", body: JSON.stringify(update) },
      );
      setUniverse((current) => current.map((item) => (item.id === updated.id ? updated : item)));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to update universe member.");
    } finally {
      setBusy(null);
    }
  }

  async function removeUniverseMember(member: OpportunityUniverseMember) {
    setBusy(`member-${member.id}`);
    setError(null);
    try {
      await apiFetch<void>(`/opportunities/universe/${member.id}`, { method: "DELETE" });
      setUniverse((current) => current.filter((item) => item.id !== member.id));
      setNotice(`${member.ticker} removed from the discovery universe.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to remove universe member.");
    } finally {
      setBusy(null);
    }
  }

  async function importUniverseSource(source: OpportunityUniverseImportSource) {
    setBusy(`import-${source}`);
    setError(null);
    try {
      const result = await apiFetch<OpportunityUniverseImportResult>(
        "/opportunities/universe/import",
        { method: "POST", body: JSON.stringify({ sources: [source] }) },
      );
      await loadState();
      setNotice(
        result.imported_count
          ? `${result.imported_count} eligible securities added. Existing priorities and paused states were preserved.`
          : "No missing eligible securities. Provenance is already current.",
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to import universe source.");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="min-h-screen min-w-0 overflow-x-clip bg-gray-50 text-gray-950 dark:bg-[#0a0a0a] dark:text-gray-100">
      <AppNav active="opportunities" />
      {error ? <FloatingNotice tone="error" message={error} onDismiss={() => setError(null)} /> : null}
      {!error && notice ? <FloatingNotice tone="success" message={notice} onDismiss={() => setNotice(null)} /> : null}

      <main className="mx-auto min-w-0 w-full max-w-[1440px] px-4 py-7 sm:px-6 lg:px-8">
        <header className="flex flex-col gap-4 border-b border-gray-200 pb-5 dark:border-gray-800 lg:flex-row lg:items-end lg:justify-between">
          <div className="min-w-0">
            <div className="flex items-center gap-2 text-xs font-semibold uppercase text-gray-500">
              <Search className="h-4 w-4" /> Discovery
            </div>
            <h1 className="mt-2 break-words text-2xl font-semibold sm:text-3xl">Opportunity Queue</h1>
            <p className="mt-2 max-w-3xl break-words text-sm text-gray-500 dark:text-gray-400">
              Prophet scans only the investable universe you define, under explicit time and provider budgets. Candidates remain provisional until you monitor, reject, expire, or test them in Shadow Lab.
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={() => void loadState()}
              className="inline-flex h-9 items-center gap-2 rounded-md border border-gray-300 bg-white px-3 text-sm font-medium hover:bg-gray-50 dark:border-gray-700 dark:bg-gray-950 dark:hover:bg-gray-900"
            >
              <RefreshCw className="h-4 w-4" /> Refresh
            </button>
            <button
              type="button"
              onClick={() => void runDiscovery()}
              disabled={busy === "run"}
              className="inline-flex h-9 items-center gap-2 rounded-md bg-gray-900 px-3 text-sm font-medium text-white hover:bg-gray-700 disabled:opacity-50 dark:bg-gray-100 dark:text-gray-950 dark:hover:bg-white"
            >
              {busy === "run" ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
              Run bounded scan
            </button>
          </div>
        </header>

        <section className="grid grid-cols-2 gap-px border-b border-gray-200 bg-gray-200 sm:grid-cols-4 dark:border-gray-800 dark:bg-gray-800">
          {[
            ["Universe", `${universe.filter((item) => item.enabled).length} enabled / ${universe.length} total`],
            ["Due now", String(dueCount)],
            ["Last run", latestRun ? `${latestRun.inspected_count}/${latestRun.universe_size} inspected` : "No runs"],
            ["Provider cost", latestRun ? `${latestRun.estimated_credits} estimated credits` : "Not recorded"],
          ].map(([label, value]) => (
            <div key={label} className="bg-gray-50 px-4 py-4 dark:bg-[#0a0a0a]">
              <p className="text-xs font-semibold uppercase text-gray-400">{label}</p>
              <p className="mt-1 break-words text-sm font-semibold text-gray-900 dark:text-gray-100">{value}</p>
            </div>
          ))}
        </section>

        <div className="flex gap-1 border-b border-gray-200 py-4 dark:border-gray-800" role="tablist" aria-label="Opportunity workspace views">
          {([
            ["queue", "Queue"],
            ["universe", "Universe"],
            ["runs", "Run history"],
          ] as Array<[WorkspaceTab, string]>).map(([value, label]) => (
            <button
              key={value}
              type="button"
              role="tab"
              aria-selected={tab === value}
              onClick={() => setTab(value)}
              className={`rounded-md px-3 py-2 text-sm font-medium ${tab === value ? "bg-gray-900 text-white dark:bg-gray-100 dark:text-gray-950" : "text-gray-500 hover:bg-gray-100 hover:text-gray-900 dark:hover:bg-gray-900 dark:hover:text-gray-100"}`}
            >
              {label}
            </button>
          ))}
        </div>

        {loading ? (
          <div className="flex min-h-64 items-center justify-center text-sm text-gray-500">
            <LoaderCircle className="mr-2 h-4 w-4 animate-spin" /> Loading opportunity state
          </div>
        ) : null}

        {!loading && tab === "queue" ? (
          <div className="grid min-h-[620px] min-w-0 grid-cols-1 gap-6 py-6 xl:grid-cols-[380px_minmax(0,1fr)]">
            <aside className="min-w-0 border-r-0 border-gray-200 xl:border-r xl:pr-6 dark:border-gray-800">
              <div className="flex max-w-full gap-1 overflow-x-auto pb-3" aria-label="Candidate status filters">
                {candidateFilters.map((item) => {
                  const count = candidates.filter((candidate) => item.value === "all" || candidate.status === item.value).length;
                  return (
                    <button
                      key={item.value}
                      type="button"
                      onClick={() => {
                        const nextCandidates = candidates.filter(
                          (candidate) => item.value === "all" || candidate.status === item.value,
                        );
                        setFilter(item.value);
                        setSelectedId(nextCandidates[0]?.id ?? null);
                        setReviewReason(nextCandidates[0]?.review_reason ?? "");
                      }}
                      className={`shrink-0 rounded-md border px-2.5 py-1.5 text-xs font-medium ${filter === item.value ? "border-gray-900 bg-gray-900 text-white dark:border-gray-100 dark:bg-gray-100 dark:text-gray-950" : "border-gray-200 bg-white text-gray-600 dark:border-gray-800 dark:bg-gray-950 dark:text-gray-300"}`}
                    >
                      {item.label} {count}
                    </button>
                  );
                })}
              </div>
              <div className="space-y-2">
                {filteredCandidates.length === 0 ? (
                  <div className="min-w-0 border border-dashed border-gray-300 px-4 py-8 text-center dark:border-gray-700">
                    <CircleDot className="mx-auto h-5 w-5 text-gray-400" />
                    <p className="mt-2 text-sm font-medium">No candidates in this state</p>
                    <p className="mt-1 break-words text-xs text-gray-500">Run discovery after adding securities to the universe.</p>
                  </div>
                ) : (
                  filteredCandidates.map((candidate) => (
                    <button
                      key={candidate.id}
                      type="button"
                      onClick={() => {
                        setSelectedId(candidate.id);
                        setReviewReason(candidate.review_reason ?? "");
                      }}
                      className={`w-full border p-4 text-left transition ${selectedId === candidate.id ? "border-gray-900 bg-white dark:border-gray-100 dark:bg-gray-950" : "border-gray-200 bg-transparent hover:border-gray-400 dark:border-gray-800 dark:hover:border-gray-600"}`}
                    >
                      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                        <div className="min-w-0">
                          <p className="text-sm font-semibold text-blue-700 dark:text-blue-300">{candidate.ticker}</p>
                          <p className="mt-1 line-clamp-2 break-words text-sm font-medium">{candidate.title}</p>
                        </div>
                        <span className="shrink-0 text-sm font-semibold tabular-nums">{Math.round(candidate.priority_score * 100)}</span>
                      </div>
                      <div className="mt-3 flex flex-wrap items-center gap-2">
                        <span className={`rounded border px-2 py-0.5 text-[11px] font-medium ${statusTone(candidate.status)}`}>
                          {displayValue(candidate.status)}
                        </span>
                        {candidate.signal_stage ? <span className="text-xs text-gray-400">{displayValue(candidate.signal_stage)}</span> : null}
                      </div>
                      <p className="mt-3 line-clamp-2 break-words text-xs leading-5 text-gray-500 dark:text-gray-400">{candidate.why_now}</p>
                    </button>
                  ))
                )}
              </div>
            </aside>

            <section className={`min-w-0 ${filteredCandidates.length === 0 ? "hidden xl:block" : ""}`}>
              {!selected ? (
                <div className="flex min-h-64 items-center justify-center border border-dashed border-gray-300 text-sm text-gray-500 dark:border-gray-700">Select a candidate to inspect its evidence and thesis.</div>
              ) : (
                <div className="min-w-0 space-y-6">
                  <header className="border-b border-gray-200 pb-5 dark:border-gray-800">
                    <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
                      <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="text-sm font-semibold text-blue-700 dark:text-blue-300">{selected.ticker}</span>
                          <span className={`rounded border px-2 py-0.5 text-xs font-medium ${statusTone(selected.status)}`}>{displayValue(selected.status)}</span>
                          {selected.signal_stage ? <span className="text-xs text-gray-500">Stage: {displayValue(selected.signal_stage)}</span> : null}
                        </div>
                        <h2 className="mt-2 break-words text-xl font-semibold sm:text-2xl">{selected.title}</h2>
                        <p className="mt-2 text-xs text-gray-400">Captured {formatDate(selected.captured_at)} · expires {formatDate(selected.expires_at)}</p>
                      </div>
                      <div className="shrink-0 text-left sm:text-right">
                        <p className="text-xs font-semibold uppercase text-gray-400">Priority score</p>
                        <p className="mt-1 text-2xl font-semibold tabular-nums">{Math.round(selected.priority_score * 100)}</p>
                      </div>
                    </div>
                  </header>

                  <div className="grid gap-6 lg:grid-cols-2">
                    <section className="min-w-0">
                      <h3 className="text-xs font-semibold uppercase text-gray-400">Why now</h3>
                      <p className="mt-2 break-words text-sm leading-6 text-gray-700 dark:text-gray-200">{selected.why_now}</p>
                    </section>
                    <section className="min-w-0">
                      <h3 className="text-xs font-semibold uppercase text-gray-400">Investable thesis</h3>
                      <p className="mt-2 break-words text-sm leading-6 text-gray-700 dark:text-gray-200">{selected.investable_thesis}</p>
                    </section>
                    <section className="min-w-0">
                      <h3 className="text-xs font-semibold uppercase text-gray-400">Portfolio transmission</h3>
                      <p className="mt-2 break-words text-sm leading-6 text-gray-700 dark:text-gray-200">{selected.portfolio_transmission}</p>
                    </section>
                    <section className="min-w-0">
                      <h3 className="text-xs font-semibold uppercase text-gray-400">Expected edge</h3>
                      <p className="mt-2 break-words text-sm leading-6 text-gray-700 dark:text-gray-200">{selected.expected_edge}</p>
                    </section>
                  </div>

                  <div className="grid gap-6 border-y border-gray-200 py-6 lg:grid-cols-3 dark:border-gray-800">
                    <ListSection title="Falsification tests" items={selected.falsification_tests} tone="neutral" />
                    <ListSection title="Assumptions, not facts" items={selected.assumptions} tone="warning" />
                    <ListSection title="Uncertainties" items={selected.uncertainties} tone="warning" />
                  </div>

                  <OutcomeSection observations={selected.observations} ticker={selected.ticker} />

                  <section>
                    <div className="flex flex-wrap items-center justify-between gap-3">
                      <h3 className="text-xs font-semibold uppercase text-gray-400">Evidence snapshot</h3>
                      <span className="text-xs text-gray-400">{selected.evidence_snapshot.length} items · {selected.evidence_refs.length} refs</span>
                    </div>
                    {selected.evidence_snapshot.length ? (
                      <ul className="mt-4 space-y-4">
                        {selected.evidence_snapshot.map((item, index) => <EvidenceItem key={`${displayValue(item.ref)}-${index}`} item={item} index={index} />)}
                      </ul>
                    ) : (
                      <p className="mt-3 border-l-2 border-red-300 pl-3 text-sm text-red-600 dark:text-red-400">No evidence snapshot is attached. This candidate should not be promoted or acted on.</p>
                    )}
                  </section>

                  <section className="grid gap-6 border-t border-gray-200 pt-6 lg:grid-cols-[minmax(0,1fr)_minmax(280px,0.7fr)] dark:border-gray-800">
                    <div>
                      <label htmlFor="review-reason" className="text-xs font-semibold uppercase text-gray-400">Review note</label>
                      <textarea id="review-reason" rows={4} value={reviewReason} onChange={(event) => setReviewReason(event.target.value)} className={`${fieldClass} mt-2`} placeholder="Why this should be monitored, rejected, or revisited" />
                      {selected.status !== "shadow_tested" ? (
                        <div className="mt-3 flex flex-wrap gap-2">
                          <ActionButton icon={Eye} label="Monitor" busy={busy === "review-monitoring"} onClick={() => void reviewCandidate("monitoring")} />
                          <ActionButton icon={X} label="Reject" busy={busy === "review-rejected"} onClick={() => void reviewCandidate("rejected")} />
                          <ActionButton icon={Clock3} label="Expire" busy={busy === "review-expired"} onClick={() => void reviewCandidate("expired")} />
                          {selected.status !== "new" ? <ActionButton icon={CircleDot} label="Return to new" busy={busy === "review-new"} onClick={() => void reviewCandidate("new")} /> : null}
                        </div>
                      ) : null}
                    </div>
                    <div className="border-l-0 border-gray-200 lg:border-l lg:pl-6 dark:border-gray-800">
                      <div className="flex items-center gap-2">
                        <FlaskConical className="h-4 w-4" />
                        <h3 className="text-sm font-semibold">Paper-only test</h3>
                      </div>
                      {selected.shadow_experiment_id ? (
                        <div className="mt-3">
                          <p className="text-sm text-emerald-700 dark:text-emerald-300">Shadow experiment is active or complete.</p>
                          <Link href="/shadow" className="mt-2 inline-flex items-center gap-1 text-sm text-blue-600 hover:underline dark:text-blue-400">Open Shadow Lab <ArrowUpRight className="h-3 w-3" /></Link>
                        </div>
                      ) : (
                        <>
                          <div className="mt-3 grid grid-cols-2 gap-1 rounded-md border border-gray-200 p-1 dark:border-gray-700" role="group" aria-label="Shadow account basis">
                            {(["cash_only", "clone_portfolio"] as const).map((basis) => (
                              <button key={basis} type="button" onClick={() => setShadowBasis(basis)} className={`rounded px-2 py-1.5 text-xs font-medium ${shadowBasis === basis ? "bg-gray-900 text-white dark:bg-gray-100 dark:text-gray-950" : "text-gray-500"}`}>{basis === "cash_only" ? "Cash only" : "Clone portfolio"}</button>
                            ))}
                          </div>
                          {shadowBasis === "cash_only" ? <input aria-label="Starting cash" type="number" min="1" value={startingCash} onChange={(event) => setStartingCash(event.target.value)} className={`${fieldClass} mt-2`} /> : null}
                          <button type="button" onClick={() => void startShadowTest()} disabled={busy === "shadow" || selected.status === "rejected" || selected.status === "expired" || (shadowBasis === "cash_only" && Number(startingCash) <= 0)} className="mt-2 inline-flex w-full items-center justify-center gap-2 rounded-md border border-gray-900 px-3 py-2 text-sm font-medium hover:bg-gray-900 hover:text-white disabled:cursor-not-allowed disabled:opacity-40 dark:border-gray-100 dark:hover:bg-gray-100 dark:hover:text-gray-950">
                            {busy === "shadow" ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <FlaskConical className="h-4 w-4" />} Start shadow test
                          </button>
                          <p className="mt-2 text-xs leading-5 text-gray-500">Creates a simulated account only. It cannot place a real order or add a real holding.</p>
                        </>
                      )}
                    </div>
                  </section>
                </div>
              )}
            </section>
          </div>
        ) : null}

        {!loading && tab === "universe" ? (
          <div className="grid gap-8 py-6 lg:grid-cols-[minmax(320px,0.45fr)_minmax(0,1fr)]">
            <div className="space-y-6">
              <section className="border border-gray-200 bg-white p-5 dark:border-gray-800 dark:bg-gray-950">
                <div className="flex items-center gap-2"><Database className="h-4 w-4" /><h2 className="text-sm font-semibold">Build from Prophet</h2></div>
                <p className="mt-2 text-xs leading-5 text-gray-500">
                  Preview eligible active equities and ETFs from durable Prophet state, then import one source at a time. Imports are additive and never remove, re-enable, or reprioritize existing members.
                </p>
                <div className="mt-4 divide-y divide-gray-200 dark:divide-gray-800">
                  {importPreview?.source_summaries.map((source) => (
                    <div key={source.source_type} className="py-3 first:pt-0 last:pb-0">
                      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                        <div className="min-w-0">
                          <p className="text-sm font-medium">{source.label}</p>
                          <p className="mt-1 break-words text-xs text-gray-500">
                            {source.eligible_count} eligible · {source.existing_count} present · {source.missing_count} missing
                            {source.skipped_count ? ` · ${source.skipped_count} skipped` : ""}
                          </p>
                        </div>
                        <button
                          type="button"
                          onClick={() => void importUniverseSource(source.source_type)}
                          disabled={!source.missing_count || busy === `import-${source.source_type}`}
                          title={`Import missing securities from ${source.label}`}
                          className="inline-flex h-9 shrink-0 items-center gap-2 rounded-md border border-gray-300 px-3 text-xs font-medium hover:border-gray-500 disabled:cursor-not-allowed disabled:opacity-40 dark:border-gray-700"
                        >
                          {busy === `import-${source.source_type}` ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />}
                          Import {source.missing_count}
                        </button>
                      </div>
                      <details className="mt-2 text-xs">
                        <summary className="cursor-pointer select-none text-gray-500 hover:text-gray-800 dark:hover:text-gray-200">
                          Review eligible securities
                        </summary>
                        <div className="mt-2 max-h-48 overflow-y-auto border-l border-gray-200 pl-3 dark:border-gray-800">
                          {importPreview.candidates
                            .filter((candidate) => candidate.origins.some((origin) => origin.source_type === source.source_type))
                            .map((candidate) => (
                              <div key={`${source.source_type}-${candidate.security_id}`} className="flex items-baseline justify-between gap-3 py-1.5">
                                <p className="min-w-0 truncate">
                                  <span className="font-semibold text-gray-800 dark:text-gray-200">{candidate.ticker}</span>
                                  <span className="ml-2 text-gray-500">{candidate.entity_name}</span>
                                </p>
                                <span className="shrink-0 text-gray-400">
                                  {candidate.status === "present" ? "Present" : "Will add"}
                                </span>
                              </div>
                            ))}
                          {!source.eligible_count ? <p className="py-2 text-gray-500">No eligible stored securities.</p> : null}
                        </div>
                      </details>
                    </div>
                  ))}
                  {!importPreview && !importPreviewError ? <p className="py-3 text-xs text-gray-500">Loading import preview...</p> : null}
                  {importPreviewError ? <p className="py-3 text-xs text-red-600 dark:text-red-400">Import preview unavailable: {importPreviewError}</p> : null}
                </div>
                <p className="mt-4 border-t border-gray-200 pt-3 text-xs leading-5 text-gray-500 dark:border-gray-800">
                  Tracked positions include holdings, watchlists, and considering lists. Research catalog entries require an entity profile. Benchmark imports use only each benchmark&apos;s latest stored constituent snapshot.
                </p>
              </section>

              <form onSubmit={addUniverseMember} className="border border-gray-200 bg-white p-5 dark:border-gray-800 dark:bg-gray-950">
                <div className="flex items-center gap-2"><Plus className="h-4 w-4" /><h2 className="text-sm font-semibold">Add security</h2></div>
                <div className="mt-4 grid gap-3">
                  <input required value={ticker} onChange={(event) => setTicker(event.target.value)} className={fieldClass} placeholder="Ticker" aria-label="Ticker" />
                  <input value={entityName} onChange={(event) => setEntityName(event.target.value)} className={fieldClass} placeholder="Company name (optional)" aria-label="Company name" />
                  <label className="text-xs font-medium text-gray-500">Priority 0–1<input required type="number" min="0" max="1" step="0.05" value={priority} onChange={(event) => setPriority(event.target.value)} className={`${fieldClass} mt-1`} /></label>
                  <button disabled={busy === "add-member"} className="inline-flex items-center justify-center gap-2 rounded-md bg-gray-900 px-3 py-2 text-sm font-medium text-white disabled:opacity-50 dark:bg-gray-100 dark:text-gray-950">{busy === "add-member" ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />} Add to universe</button>
                </div>
              </form>

              {settings ? (
                <section className="border border-gray-200 bg-white p-5 dark:border-gray-800 dark:bg-gray-950">
                  <div className="flex items-center gap-2"><Settings2 className="h-4 w-4" /><h2 className="text-sm font-semibold">Discovery controls</h2></div>
                  <p className="mt-2 text-xs leading-5 text-gray-500">These limits apply to scheduled scans and are recorded on each run. Manual scans use the same per-run limits.</p>
                  <label className="mt-4 flex items-center justify-between gap-4 text-sm"><span>Scheduled discovery</span><input type="checkbox" checked={settings.opportunity_discovery.enabled} onChange={(event) => updateDiscoverySettings("enabled", event.target.checked)} className="h-4 w-4 accent-gray-900" /></label>
                  <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2">
                    <NumberSetting label="Cadence (hours)" value={settings.opportunity_discovery.interval_seconds / 3600} min={1} max={168} onChange={(value) => updateDiscoverySettings("interval_seconds", Math.round(value * 3600))} />
                    <NumberSetting label="Subjects / run" value={settings.opportunity_discovery.max_subjects_per_run} min={1} max={100} onChange={(value) => updateDiscoverySettings("max_subjects_per_run", value)} />
                    <NumberSetting label="Revisit (hours)" value={settings.opportunity_discovery.revisit_hours} min={1} max={8760} onChange={(value) => updateDiscoverySettings("revisit_hours", value)} />
                    <NumberSetting label="Candidate TTL (days)" value={settings.opportunity_discovery.candidate_ttl_days} min={1} max={365} onChange={(value) => updateDiscoverySettings("candidate_ttl_days", value)} />
                  </div>
                  <button type="button" onClick={() => void saveDiscoverySettings()} disabled={busy === "settings"} className="mt-4 inline-flex w-full items-center justify-center gap-2 rounded-md border border-gray-900 px-3 py-2 text-sm font-medium hover:bg-gray-900 hover:text-white disabled:opacity-50 dark:border-gray-100 dark:hover:bg-gray-100 dark:hover:text-gray-950">{busy === "settings" ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />} Apply controls</button>
                  <div className="mt-4 border-t border-gray-200 pt-3 text-xs text-gray-500 dark:border-gray-800">
                    Scheduler: {job?.enabled ? `active every ${formatDuration(job.interval_seconds ?? settings.opportunity_discovery.interval_seconds)}` : "disabled"}. Last status: {displayValue(job?.last_status)}.
                  </div>
                </section>
              ) : null}
            </div>

            <section>
              <div className="flex flex-wrap items-end justify-between gap-4 border-b border-gray-200 pb-3 dark:border-gray-800">
                <div className="min-w-0"><h2 className="text-lg font-semibold">Investable universe</h2><p className="mt-1 break-words text-sm text-gray-500">This list is the explicit boundary of discovery coverage.</p></div>
                <span className="text-xs text-gray-400">{dueCount} due</span>
              </div>
              <div className="divide-y divide-gray-200 dark:divide-gray-800">
                {universe.map((member) => (
                  <div key={member.id} className="grid gap-4 py-4 sm:grid-cols-[minmax(0,1fr)_160px_auto] sm:items-center">
                    <div className="min-w-0">
                      <div className="flex items-center gap-2"><p className="font-semibold text-blue-700 dark:text-blue-300">{member.ticker}</p><span className="truncate text-sm text-gray-500">{member.entity_name}</span></div>
                      <p className="mt-1 text-xs text-gray-400">Last inspected: {formatDate(member.last_inspected_at)} · Next: {formatDate(member.next_inspection_at)}</p>
                      <p className="mt-1 truncate text-xs text-gray-500" title={member.origins.map((origin) => origin.label).join(", ")}>
                        Sources: {member.origins.length ? member.origins.map((origin) => origin.label).join(" · ") : displayValue(member.source)}
                      </p>
                    </div>
                    <label className="text-xs font-medium text-gray-500">Priority<input type="number" min="0" max="1" step="0.05" defaultValue={member.priority} onBlur={(event) => { const value = Number(event.target.value); if (value !== member.priority) void updateUniverseMember(member, { priority: value }); }} className={`${fieldClass} mt-1`} /></label>
                    <div className="flex items-center justify-end gap-2">
                      <button type="button" onClick={() => void updateUniverseMember(member, { enabled: !member.enabled })} disabled={busy === `member-${member.id}`} title={member.enabled ? "Pause discovery for this security" : "Enable discovery for this security"} className={`inline-flex h-9 items-center gap-2 rounded-md border px-3 text-sm font-medium ${member.enabled ? "border-emerald-300 text-emerald-700 dark:border-emerald-800 dark:text-emerald-300" : "border-gray-300 text-gray-500 dark:border-gray-700"}`}>{member.enabled ? <Check className="h-4 w-4" /> : <CircleDot className="h-4 w-4" />}{member.enabled ? "Enabled" : "Paused"}</button>
                      <button type="button" onClick={() => void removeUniverseMember(member)} disabled={busy === `member-${member.id}`} title={`Remove ${member.ticker} from discovery`} aria-label={`Remove ${member.ticker} from discovery`} className="inline-flex h-9 w-9 items-center justify-center rounded-md border border-gray-300 text-gray-500 hover:border-red-300 hover:text-red-600 dark:border-gray-700"><Trash2 className="h-4 w-4" /></button>
                    </div>
                  </div>
                ))}
                {!universe.length ? <p className="py-12 text-center text-sm text-gray-500">No securities are in the discovery universe.</p> : null}
              </div>
            </section>
          </div>
        ) : null}

        {!loading && tab === "runs" ? (
          <section className="py-6">
            <div className="border-b border-gray-200 pb-3 dark:border-gray-800">
              <h2 className="text-lg font-semibold">Run history</h2>
              <p className="mt-1 text-sm text-gray-500">Point-in-time coverage, provider attempts, skips, failures, and the exact limits used.</p>
            </div>
            <div className="divide-y divide-gray-200 dark:divide-gray-800">
              {runs.map((run) => {
                const expanded = expandedRun === run.id;
                return (
                  <article key={run.id} className="py-4">
                    <button type="button" onClick={() => setExpandedRun(expanded ? null : run.id)} className="grid w-full gap-3 text-left sm:grid-cols-[minmax(200px,1fr)_repeat(4,minmax(90px,auto))_auto] sm:items-center">
                      <div><div className="flex items-center gap-2"><span className={`h-2 w-2 rounded-full ${run.failed_count ? "bg-red-500" : run.status === "completed" ? "bg-emerald-500" : "bg-amber-500"}`} /><p className="text-sm font-semibold">{displayValue(run.status)}</p></div><p className="mt-1 text-xs text-gray-400">{formatDate(run.captured_at)}</p></div>
                      <RunMetric label="Coverage" value={`${run.inspected_count}/${run.universe_size}`} />
                      <RunMetric label="Skipped" value={String(run.skipped_count)} />
                      <RunMetric label="Failed" value={String(run.failed_count)} />
                      <RunMetric label="Credits" value={String(run.estimated_credits)} />
                      <ChevronDown className={`h-4 w-4 justify-self-end transition ${expanded ? "rotate-180" : ""}`} />
                    </button>
                    {expanded ? (
                      <div className="mt-4 grid gap-5 border-l-2 border-gray-200 pl-4 lg:grid-cols-3 dark:border-gray-700">
                        <RunDetail title="Limits" items={Object.entries(run.limits).map(([key, value]) => ({ label: key, detail: displayValue(value) }))} />
                        <RunDetail title="Provider attempts" items={run.provider_attempts.map((item) => ({ label: displayValue(item.provider ?? item.status), detail: `${displayValue(item.status)} · ${displayValue(item.estimated_credits)} credits · ${displayValue(item.query)}` }))} />
                        <RunDetail title="Skips and failures" items={[...run.skipped, ...run.failures].map((item) => ({ label: displayValue(item.ticker ?? item.stage ?? item.member_id ?? "Run item"), detail: displayValue(item.reason ?? item.error) }))} />
                        <p className="text-xs text-gray-500 lg:col-span-3">Detail: {displayValue(run.detail)} · started {formatDate(run.started_at)} · completed {formatDate(run.completed_at)}</p>
                      </div>
                    ) : null}
                  </article>
                );
              })}
              {!runs.length ? <p className="py-12 text-center text-sm text-gray-500">No discovery runs have been recorded.</p> : null}
            </div>
          </section>
        ) : null}
      </main>
    </div>
  );
}

function ListSection({ title, items, tone }: { title: string; items: string[]; tone: "neutral" | "warning" }) {
  return (
    <section>
      <h3 className={`text-xs font-semibold uppercase ${tone === "warning" ? "text-amber-600 dark:text-amber-400" : "text-gray-400"}`}>{title}</h3>
      {items.length ? <ul className="mt-3 space-y-2 text-sm text-gray-700 dark:text-gray-200">{items.map((item, index) => <li key={`${item}-${index}`} className="flex min-w-0 gap-2">{tone === "warning" ? <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-500" /> : <CircleDot className="mt-1 h-3 w-3 shrink-0 text-gray-400" />}<span className="min-w-0 break-words">{item}</span></li>)}</ul> : <p className="mt-3 text-sm text-gray-400">None recorded.</p>}
    </section>
  );
}

function ActionButton({ icon: Icon, label, busy, onClick }: { icon: typeof Eye; label: string; busy: boolean; onClick: () => void }) {
  return <button type="button" onClick={onClick} disabled={busy} className="inline-flex items-center gap-2 rounded-md border border-gray-300 px-3 py-2 text-sm font-medium hover:bg-gray-100 disabled:opacity-50 dark:border-gray-700 dark:hover:bg-gray-900">{busy ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <Icon className="h-4 w-4" />}{label}</button>;
}

function NumberSetting({ label, value, min, max, onChange }: { label: string; value: number; min: number; max: number; onChange: (value: number) => void }) {
  return <label className="text-xs font-medium text-gray-500">{label}<input type="number" min={min} max={max} value={value} onChange={(event) => onChange(Number(event.target.value))} className={`${fieldClass} mt-1`} /></label>;
}

function RunMetric({ label, value }: { label: string; value: string }) {
  return <div><p className="text-[10px] font-semibold uppercase text-gray-400">{label}</p><p className="mt-0.5 text-sm font-medium tabular-nums">{value}</p></div>;
}

function RunDetail({ title, items }: { title: string; items: Array<{ label: string; detail: string }> }) {
  return <section><h3 className="text-xs font-semibold uppercase text-gray-400">{title}</h3>{items.length ? <ul className="mt-2 space-y-2">{items.map((item, index) => <li key={`${item.label}-${index}`}><p className="text-xs font-medium">{item.label}</p><p className="mt-0.5 break-words text-xs leading-5 text-gray-500">{item.detail}</p></li>)}</ul> : <p className="mt-2 text-xs text-gray-400">None recorded.</p>}</section>;
}

function OutcomeSection({
  observations,
  ticker,
}: {
  observations: OpportunityCandidate["observations"];
  ticker: string;
}) {
  const observation = observations[0] ?? null;
  if (!observation) {
    return (
      <section className="border-b border-gray-200 pb-6 dark:border-gray-800">
        <h3 className="text-xs font-semibold uppercase text-gray-400">Point-in-time outcome</h3>
        <p className="mt-2 text-sm text-gray-500">
          No immutable discovery observation exists for this legacy candidate. Prophet will not infer a historical direction after seeing the result.
        </p>
      </section>
    );
  }

  const evaluated = observation.status === "evaluated";
  const resultTone =
    observation.result_label === "supported"
      ? "text-emerald-700 dark:text-emerald-300"
      : observation.result_label === "challenged"
        ? "text-red-700 dark:text-red-300"
        : "text-gray-600 dark:text-gray-300";

  return (
    <section className="border-b border-gray-200 pb-6 dark:border-gray-800">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h3 className="text-xs font-semibold uppercase text-gray-400">Point-in-time outcome</h3>
          <p className="mt-2 text-sm font-medium">
            Expected {ticker} to {displayValue(observation.expected_relative_direction)} {observation.benchmark_ticker}
          </p>
          <p className="mt-1 text-xs text-gray-500">
            {observation.horizon_days}-day calibration window · due {formatDate(observation.due_at)}
          </p>
        </div>
        <p className={`text-sm font-semibold ${resultTone}`}>
          {evaluated ? displayValue(observation.result_label) : "Awaiting settled outcome"}
        </p>
      </div>

      {evaluated ? (
        <div className="mt-4 grid grid-cols-2 gap-px bg-gray-200 sm:grid-cols-4 dark:bg-gray-800">
          <OutcomeMetric label={ticker} value={formatPct(observation.candidate_return_pct)} />
          <OutcomeMetric label={`${observation.benchmark_ticker} control`} value={formatPct(observation.benchmark_return_pct)} />
          <OutcomeMetric label="Excess return" value={formatPct(observation.excess_return_pct)} />
          <OutcomeMetric label="Cash control" value={formatPct(observation.cash_return_pct)} />
        </div>
      ) : (
        <p className="mt-4 text-sm text-gray-500">
          {observation.candidate_start_time
            ? `Baseline anchored after capture at ${formatDate(observation.candidate_start_time)}.`
            : "The first eligible shared settled close has not been anchored yet."}
        </p>
      )}

      <p className="mt-3 text-xs leading-5 text-gray-500">
        Uses stored adjusted closes from {displayValue(observation.market_data_provider)}. The baseline is the first shared close after capture; the outcome is the first shared close on or after the fixed due date. Current-day and future-dated prices are ineligible.
      </p>
      {observation.last_error ? (
        <p className="mt-2 text-xs text-amber-700 dark:text-amber-300">
          Data status: {displayValue(observation.last_error)}
        </p>
      ) : null}
      {observations.length > 1 ? (
        <div className="mt-4 border-t border-gray-200 pt-3 dark:border-gray-800">
          <p className="text-[10px] font-semibold uppercase text-gray-400">Prior frozen observations</p>
          <div className="mt-2 divide-y divide-gray-200 dark:divide-gray-800">
            {observations.slice(1).map((item) => (
              <div key={item.id} className="grid gap-1 py-2 text-xs sm:grid-cols-[1fr_auto_auto] sm:items-center sm:gap-4">
                <span className="text-gray-500">Captured {formatDate(item.captured_at)}</span>
                <span>{displayValue(item.result_label ?? item.status)}</span>
                <span className="tabular-nums text-gray-500">{formatPct(item.excess_return_pct)} excess</span>
              </div>
            ))}
          </div>
        </div>
      ) : null}
    </section>
  );
}

function OutcomeMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-gray-50 px-3 py-3 dark:bg-[#0a0a0a]">
      <p className="text-[10px] font-semibold uppercase text-gray-400">{label}</p>
      <p className="mt-1 text-sm font-semibold tabular-nums">{value}</p>
    </div>
  );
}

function formatPct(value?: number | null): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "Not available";
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;
}
