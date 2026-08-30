"use client";

import { useCallback, useEffect, useState } from "react";

import AppNav from "@/components/AppNav";
import PageHeader from "@/components/PageHeader";
import WorkspaceState from "@/components/WorkspaceState";
import { API_BASE, apiFetch, ProfileListItem, ReviewQueueItem, VerificationResult } from "@/lib/api";

export default function VerificationPage() {
  const [profiles, setProfiles] = useState<ProfileListItem[]>([]);
  const [queue, setQueue] = useState<ReviewQueueItem[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [selectedSubjectId, setSelectedSubjectId] = useState("");
  const [selectedSubjectType, setSelectedSubjectType] = useState("entity");
  const [challengeText, setChallengeText] = useState("Are you sure? Re-check contradictions, missing evidence, and whether the current stance should change.");
  const [result, setResult] = useState<VerificationResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [loaded, setLoaded] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expandedSignals, setExpandedSignals] = useState<Record<string, boolean>>({});

  const loadWorkspace = useCallback(async () => {
    setLoading(true);
    try {
      const [items, queueItems] = await Promise.all([
        apiFetch<ProfileListItem[]>("/profiles"),
        apiFetch<ReviewQueueItem[]>("/review/queue"),
      ]);
      setProfiles(items);
      setQueue(queueItems);
      const selected = items[0];
      if (selected) {
        setSelectedId(selected.id);
        setSelectedSubjectId(selected.subject_id);
        setSelectedSubjectType(selected.subject_type);
      } else {
        setSelectedId("");
        setSelectedSubjectId("");
      }
      setLoaded(true);
      setLoadError(null);
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : "Unable to load the review workspace.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadWorkspace();
  }, [loadWorkspace]);

  async function refreshQueue() {
    setError(null);
    try {
      setQueue(await apiFetch<ReviewQueueItem[]>("/review/queue/refresh", { method: "POST" }));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to refresh review queue.");
    }
  }

  function handleProfileChange(value: string) {
    setSelectedId(value);
    const profile = profiles.find((item) => item.id === value);
    if (profile) {
      setSelectedSubjectId(profile.subject_id);
      setSelectedSubjectType(profile.subject_type);
    }
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
            description="Resolve thin coverage, contradictions, stale views, and simulation disagreements before they quietly shape portfolio decisions."
          />
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
              description={`${loadError} The last loaded queue remains available.`}
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
                  The system should escalate weak coverage, unresolved questions, source conflicts, and shadow divergences here.
                </p>
              </div>
              <button onClick={() => void refreshQueue()} className="rounded-lg border border-slate-300 dark:border-slate-700 px-3 py-2 text-sm">
                Refresh queue
              </button>
            </div>
            <div className="mt-4 space-y-3">
              {queue.length === 0 ? (
                <WorkspaceState
                  kind="empty"
                  compact
                  title="No pending review items"
                  description="Prophet has not found a current contradiction, coverage gap, stale view, or simulation disagreement that needs escalation."
                />
              ) : (
                queue.slice(0, 8).map((item) => (
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
          </section>

          {profiles.length === 0 ? (
            <WorkspaceState
              kind="empty"
              title="No subjects are ready for verification"
              description="Create or ingest research for a company, position, or theme before running a manual challenge."
            />
          ) : (
            <form onSubmit={runVerification} className="rounded-lg border border-slate-200 bg-white p-6 dark:border-slate-800 dark:bg-slate-950 space-y-4">
              <h2 className="text-sm font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400">Manual challenge</h2>
              <select value={selectedId} onChange={(e) => handleProfileChange(e.target.value)} className={inputClass}>
                {profiles.map((profile) => (
                  <option key={profile.id} value={profile.id}>
                    {profile.subject_name} ({profile.subject_type})
                  </option>
                ))}
              </select>
              <textarea value={challengeText} onChange={(e) => setChallengeText(e.target.value)} rows={8} className={inputClass} />
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
