"use client";

import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";

import AppNav from "@/components/AppNav";
import PageHeader from "@/components/PageHeader";
import WorkspaceState from "@/components/WorkspaceState";
import ReviewItemActions from "@/components/ReviewItemActions";
import ReviewDomainFilters, { reviewDomain, reviewDomainLabel, reviewPagePath, reviewSelectionKey } from "@/components/ReviewDomainFilters";
import { isRecordId, reviewResearchQuestion, reviewSubject, selectReviewContext } from "@/lib/workflows";
import { API_BASE, apiFetch, ProfileListItem, ReviewDomain, ReviewQueueCounts, ReviewQueueItem, ReviewQueuePage, VerificationResult } from "@/lib/api";

export default function VerificationPage() {
  return <Suspense fallback={<p className="p-6 text-muted">Loading review workspace…</p>}><VerificationWorkspace /></Suspense>;
}

function VerificationWorkspace() {
  const searchParams = useSearchParams();
  const targetKey = searchParams.toString();
  const selectionKeyRef = useRef<string | null>(null);
  const workspaceRequestRef = useRef(0);
  const [profiles, setProfiles] = useState<ProfileListItem[]>([]);
  const [queue, setQueue] = useState<ReviewQueueItem[]>([]);
  const [visibleQueueCount, setVisibleQueueCount] = useState(8);
  const [queueTotal, setQueueTotal] = useState(0);
  const [queueCounts, setQueueCounts] = useState<ReviewQueueCounts>({ investment: 0, portfolio_data: 0 });
  const [loadedDomain, setLoadedDomain] = useState<ReviewDomain | null>(null);
  const [queueOffset, setQueueOffset] = useState(0);
  const [loadingMore, setLoadingMore] = useState(false);
  const [selectedId, setSelectedId] = useState("");
  const [selectedSubjectId, setSelectedSubjectId] = useState("");
  const [selectedSubjectType, setSelectedSubjectType] = useState("entity");
  const [selectedSubjectName, setSelectedSubjectName] = useState("");
  const [contextNotice, setContextNotice] = useState<string | null>(null);
  const [challengeText, setChallengeText] = useState("Are you sure? Re-check contradictions, missing evidence, and whether the current stance should change.");
  const [result, setResult] = useState<VerificationResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [loaded, setLoaded] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expandedSignals, setExpandedSignals] = useState<Record<string, boolean>>({});

  const loadWorkspace = useCallback(async () => {
    const request = ++workspaceRequestRef.current;
    const params = new URLSearchParams(targetKey);
    const domain = reviewDomain(params.get("domain"));
    const selectionKey = reviewSelectionKey(targetKey);
    setLoading(true);
    setLoadingMore(false);
    try {
      const [items, page] = await Promise.all([
        apiFetch<ProfileListItem[]>("/profiles"),
        apiFetch<ReviewQueuePage>(reviewPagePath(domain)),
      ]);
      const queueItems = page.items;
      const requestedId = params.get("item_id");
      const linkedItem = isRecordId(requestedId) && !queueItems.some(item => item.id === requestedId)
        ? await apiFetch<ReviewQueueItem | null>(`/review/queue/${requestedId}`) : null;
      if (request !== workspaceRequestRef.current) return;
      setProfiles(items);
      setQueue(queueItems);
      setQueueTotal(page.total);
      setQueueCounts(page.counts);
      setLoadedDomain(domain);
      setVisibleQueueCount(8);
      setQueueOffset(queueItems.length);
      if (selectionKeyRef.current !== selectionKey) {
        selectionKeyRef.current = selectionKey;
        const context = selectReviewContext(items, linkedItem ? [...queueItems, linkedItem] : queueItems, new URLSearchParams(targetKey));
        const subject = context.subject;
        const selected = context.profile;
        setSelectedId(selected?.id ?? "");
        setSelectedSubjectId(selected?.subject_id ?? subject?.subject_id ?? "");
        setSelectedSubjectType(selected?.subject_type ?? subject?.subject_type ?? "entity");
        setSelectedSubjectName(selected?.subject_name ?? subject?.subject_name ?? "");
        setResult(null);
        if (context.question) setChallengeText(context.question);
        setContextNotice(context.notice);
        setError(null);
      }
      setLoaded(true);
      setLoadError(null);
    } catch (err) {
      if (request !== workspaceRequestRef.current) return;
      setLoadError(`${reviewDomainLabel(domain)} reviews could not be loaded. ${err instanceof Error ? err.message : "Unable to load the review workspace."}`);
    } finally {
      if (request === workspaceRequestRef.current) setLoading(false);
    }
  }, [targetKey]);

  useEffect(() => {
    void loadWorkspace();
    return () => { workspaceRequestRef.current += 1; };
  }, [loadWorkspace]);

  useEffect(() => {
    if (loaded && window.location.hash === "#manual-challenge") {
      document.getElementById("manual-challenge")?.scrollIntoView({ block: "start" });
    }
  }, [loaded, targetKey]);

  async function refreshQueue() {
    setError(null);
    setLoading(true);
    try {
      await apiFetch<ReviewQueueItem[]>("/review/queue/refresh", { method: "POST" });
      await loadWorkspace();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to refresh review queue.");
    } finally {
      setLoading(false);
    }
  }

  async function showMoreConcerns() {
    if (visibleQueueCount < queue.length) {
      setVisibleQueueCount(current => current + 8);
      return;
    }
    const request = workspaceRequestRef.current;
    setLoadingMore(true);
    try {
      const page = await apiFetch<ReviewQueuePage>(reviewPagePath(loadedDomain, queueOffset));
      if (request !== workspaceRequestRef.current) return;
      setQueue(current => [...current, ...page.items.filter(item => !current.some(existing => existing.id === item.id))]);
      setQueueOffset(current => current + page.items.length);
      setQueueTotal(page.items.length ? page.total : queueOffset);
      setQueueCounts(page.counts);
      setVisibleQueueCount(current => current + 8);
      setLoadError(null);
    } catch (err) {
      if (request === workspaceRequestRef.current) setLoadError(err instanceof Error ? err.message : "Unable to load more concerns.");
    } finally {
      if (request === workspaceRequestRef.current) setLoadingMore(false);
    }
  }

  function handleProfileChange(value: string) {
    setResult(null);
    setError(null);
    setContextNotice(null);
    setSelectedId(value);
    const profile = profiles.find((item) => item.id === value);
    if (profile) {
      setSelectedSubjectId(profile.subject_id);
      setSelectedSubjectType(profile.subject_type);
      setSelectedSubjectName(profile.subject_name);
    }
  }

  function prepareChallenge(item: ReviewQueueItem) {
    const subject = reviewSubject(item);
    if (!subject) return;
    const profile = profiles.find(candidate => candidate.subject_id === subject.subject_id && candidate.subject_type === subject.subject_type);
    setSelectedId(profile?.id ?? "");
    setSelectedSubjectId(subject.subject_id);
    setSelectedSubjectType(subject.subject_type);
    setSelectedSubjectName(profile?.subject_name ?? item.item_label);
    setChallengeText(reviewResearchQuestion(item));
    setResult(null);
    setError(null);
    setContextNotice(null);
    window.requestAnimationFrame(() => document.getElementById("challenge-text")?.focus());
  }

  async function runVerification(e: React.FormEvent) {
    e.preventDefault();
    if (!selectedSubjectId) return;
    setRunning(true);
    setError(null);
    setResult(null);
    try {
      const response = await fetch(`${API_BASE}/verification`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          subject_id: selectedSubjectId,
          subject_type: selectedSubjectType,
          trigger: "user_challenge",
          challenge_text: challengeText,
        }),
      });
      if (!response.ok) {
        throw new Error(await response.text());
      }
      setResult(await response.json());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to run verification.");
    } finally {
      setRunning(false);
    }
  }

  return (
    <div className="min-h-screen bg-background text-foreground font-sans">
      <AppNav active="review" />
      <main className="mx-auto grid w-full max-w-[1440px] grid-cols-1 gap-8 px-4 py-8 sm:px-6 lg:px-8 xl:grid-cols-[0.95fr_1.05fr]">
        <section className="space-y-6">
          <PageHeader
            eyebrow="Decision quality"
            title="Review queue"
            description="Review investment uncertainty and portfolio data integrity. Receipt and reconciliation holds remain separate from research priorities."
          />
          {contextNotice && <WorkspaceState kind="degraded" compact title="Review context changed" description={contextNotice} />}
          {!loaded && loading ? (
            <WorkspaceState
              kind="loading"
              title="Loading review workspace"
              description="Retrieving review priorities and the subjects available for verification."
            />
          ) : !loaded && loadError ? (
            <WorkspaceState
              kind="error"
              title="Review workspace is unavailable"
              description={loadError}
              actionLabel="Retry"
              onAction={() => void loadWorkspace()}
            />
          ) : null}
          {loaded && loadError ? (
            <WorkspaceState
              kind="degraded"
              compact
              title="Review refresh is delayed"
              description={`${loadError} The last loaded ${reviewDomainLabel(loadedDomain)} queue remains available.`}
              actionLabel="Retry refresh"
              onAction={() => void loadWorkspace()}
            />
          ) : null}
          {loaded ? (
            <>
          <section className="rounded-lg border border-slate-200 bg-white p-6 dark:border-slate-800 dark:bg-slate-950">
            <div className="flex items-center justify-between gap-4">
              <div>
                <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
                  Investment reviews cover evidence and views. Portfolio data reviews cover receipts, posting holds and reconciliation. Priority is ordered within the selected view.
                </p>
              </div>
              <button disabled={loading || loadingMore} onClick={() => void refreshQueue()} className="rounded-lg border border-slate-300 dark:border-slate-700 px-3 py-2 text-sm disabled:opacity-50">
                Refresh queue
              </button>
            </div>
            <ReviewDomainFilters domain={loadedDomain} counts={queueCounts} targetKey={targetKey} />
            {loading && <p role="status" className="mt-3 text-sm text-muted">Loading the requested view; the last loaded queue remains visible.</p>}
            <div className="mt-4 space-y-3">
              {queue.length === 0 ? (
                <WorkspaceState
                  kind="empty"
                  compact
                  title={`No pending ${loadedDomain === "portfolio_data" ? "portfolio data" : loadedDomain === "investment" ? "investment" : "review"} items`}
                  description="No reviews are currently queued in this view. This does not establish complete coverage, reconciled balances or an absence of risk."
                />
              ) : (
                queue.slice(0, visibleQueueCount).map((item) => (
                  <div key={item.id} className="rounded-lg border border-slate-200 dark:border-slate-800 p-4">
                    <div className="flex items-center justify-between gap-4">
                      <div>
                        <p className="font-medium">{item.item_label}</p>
                        <p className="mt-1 text-xs uppercase tracking-wider text-slate-400">{item.item_type.replaceAll("_", " ")}</p>
                      </div>
                      <span className="text-xs uppercase tracking-wider text-slate-400">
                        priority {item.priority_score.toFixed(1)}
                      </span>
                    </div>
                    <div className="mt-2 rounded-lg border border-sky-100 bg-sky-50/60 px-3 py-2 text-sm text-sky-900 dark:border-sky-900 dark:bg-sky-950/20 dark:text-sky-200">
                      Next best move: {item.next_action}
                    </div>
                    <div className="mt-3 flex flex-wrap gap-2">
                      {item.signal_tags.map((tag) => (
                        <span
                          key={tag}
                          className="rounded-full border border-slate-200 px-2.5 py-1 text-[11px] uppercase tracking-wider text-slate-500 dark:border-slate-800 dark:text-slate-400"
                        >
                          {tag}
                        </span>
                      ))}
                    </div>
                    <ReviewItemActions item={item} disabled={running} onChallenge={() => prepareChallenge(item)} />
                    <button
                      type="button"
                      onClick={() =>
                        setExpandedSignals((current) => ({
                          ...current,
                          [item.id]: !current[item.id],
                        }))
                      }
                      className="mt-3 text-xs font-bold uppercase tracking-wider text-slate-500 hover:text-slate-700 dark:text-slate-400 dark:hover:text-slate-200"
                    >
                      {expandedSignals[item.id] ? "Hide rationale and signals" : "View rationale and signals"}
                    </button>
                    {expandedSignals[item.id] ? (
                      <div className="mt-3 space-y-3 border-t border-slate-200 pt-3 dark:border-slate-800">
                        <p className="break-words text-sm leading-6 text-slate-700 dark:text-slate-200">
                          {item.why_now_summary}
                        </p>
                        <div className="flex flex-wrap gap-2 text-xs text-slate-400">
                          <span>priority {item.priority_score.toFixed(1)}</span>
                          <span>contradiction {item.contradiction_pressure.toFixed(1)}</span>
                          <span>coverage pressure {item.coverage_weakness.toFixed(1)}</span>
                          <span>drift {item.thesis_drift.toFixed(1)}</span>
                        </div>
                      </div>
                    ) : null}
                  </div>
                ))
              )}
            </div>
            {queue.length > 0 && <div className="mt-4 flex flex-wrap items-center gap-4 text-sm text-muted">
              <p>Showing {Math.min(visibleQueueCount, queue.length)} of {Math.max(queueTotal, queue.length)} concerns.</p>
              {(visibleQueueCount < queue.length || queueOffset < queueTotal) && <button type="button" disabled={loading || loadingMore} onClick={() => void showMoreConcerns()} className="text-action underline disabled:opacity-50">{loadingMore ? "Loading concerns…" : "Show more concerns"}</button>}
            </div>}
          </section>

          {profiles.length === 0 && !selectedSubjectId ? (
            <WorkspaceState
              kind="empty"
              title="No subjects are ready for verification"
              description="Create or ingest research for a company, position, or theme before running a manual challenge."
            />
          ) : (
            <form id="manual-challenge" onSubmit={runVerification} className="scroll-mt-24 rounded-lg border border-slate-200 bg-white p-6 dark:border-slate-800 dark:bg-slate-950 space-y-4">
              <h2 className="text-sm font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400">Manual challenge</h2>
              <p className="text-sm text-muted">{selectedSubjectId ? `Subject: ${selectedSubjectName || selectedSubjectType}. Review the question before running verification.` : "Choose the subject you want to verify."}</p>
              <label htmlFor="challenge-subject" className="block text-xs text-muted">Subject</label>
              <select id="challenge-subject" disabled={running} value={selectedId} onChange={(e) => handleProfileChange(e.target.value)} className={inputClass}>
                {!selectedId && <option value="">{selectedSubjectId ? selectedSubjectName || "Linked subject" : "Choose a subject"}</option>}
                {profiles.map((profile) => (
                  <option key={profile.id} value={profile.id}>
                    {profile.subject_name} ({profile.subject_type})
                  </option>
                ))}
              </select>
              <label htmlFor="challenge-text" className="block text-xs text-muted">Question to verify</label>
              <textarea id="challenge-text" disabled={running} value={challengeText} onChange={(e) => setChallengeText(e.target.value)} rows={8} className={inputClass} />
              <p className="text-xs text-muted">Preparing a challenge does not run it. Run verification uses your configured research provider.</p>
              <button disabled={running || !selectedSubjectId} className="w-full rounded-lg bg-sky-600 px-4 py-3 text-white disabled:opacity-50">
                {running ? "Verifying..." : "Run verification"}
              </button>
            </form>
          )}
            </>
          ) : null}
          {error ? (
            <WorkspaceState
              kind="error"
              compact
              title="The requested review action failed"
              description={error}
            />
          ) : null}
        </section>

        <section>
          {!result ? (
            <WorkspaceState
              kind="empty"
              title="No verification result yet"
              description="Choose a subject and run a manual challenge. The result will show whether the accepted stance held, narrowed, or changed."
            />
          ) : (
            <article className="rounded-lg border border-slate-200 bg-white p-6 dark:border-slate-800 dark:bg-slate-950 space-y-5">
              <div className="flex items-center justify-between gap-4">
                <div>
                  <h2 className="text-xl font-semibold tracking-tight">{result.subject_type}</h2>
                  <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">{new Date(result.verified_at).toLocaleString()}</p>
                </div>
                <span className={`text-xs uppercase tracking-wider ${result.conclusion_changed ? "text-amber-500" : "text-emerald-500"}`}>
                  {result.conclusion_changed ? "conclusion changed" : "stance held"}
                </span>
              </div>
              <div className="grid grid-cols-2 gap-4 text-sm">
                <div className="rounded-lg border border-slate-200 dark:border-slate-800 p-4">
                  <p className="text-slate-500 dark:text-slate-400">Prior stance</p>
                  <p className="mt-1 font-medium">{result.prior_stance}</p>
                </div>
                <div className="rounded-lg border border-slate-200 dark:border-slate-800 p-4">
                  <p className="text-slate-500 dark:text-slate-400">Verified stance</p>
                  <p className="mt-1 font-medium">{result.verified_stance} · {result.confidence_band}</p>
                </div>
              </div>
              <div className="rounded-lg border border-slate-200 dark:border-slate-800 p-4">
                <p className="text-sm text-slate-500 dark:text-slate-400">Coverage status</p>
                <p className="mt-1 font-medium">{result.contradiction_coverage_status}</p>
                {result.missing_classes_found.length ? (
                  <p className="mt-2 text-sm text-slate-600 dark:text-slate-300">Missing: {result.missing_classes_found.join(", ")}</p>
                ) : null}
              </div>
              <div>
                <h3 className="text-sm font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400">Reasoning</h3>
                <p className="mt-2 text-sm text-slate-700 dark:text-slate-300">{result.change_reasoning}</p>
              </div>
              {result.what_would_falsify.length ? (
                <div>
                  <h3 className="text-sm font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400">Falsifiers</h3>
                  <ul className="mt-2 space-y-2 text-sm text-slate-700 dark:text-slate-300">
                    {result.what_would_falsify.map((item) => (
                      <li key={item}>{item}</li>
                    ))}
                  </ul>
                </div>
              ) : null}
            </article>
          )}
        </section>
      </main>
    </div>
  );
}

const inputClass =
  "w-full rounded-lg border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-900 px-4 py-3";
