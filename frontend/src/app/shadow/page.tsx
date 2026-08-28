"use client";

import { useEffect, useRef, useState } from "react";

import AppNav from "@/components/AppNav";
import FloatingNotice from "@/components/FloatingNotice";
import PageHeader from "@/components/PageHeader";
import WorkspaceState from "@/components/WorkspaceState";
import { API_BASE, apiFetch, AutomationStatus, ShadowExperiment } from "@/lib/api";



export default function ShadowPage() {
  const [experiments, setExperiments] = useState<ShadowExperiment[]>([]);
  const [automation, setAutomation] = useState<AutomationStatus | null>(null);
  const [name, setName] = useState("Custom shadow review");
  const [operatorPrompt, setOperatorPrompt] = useState("");
  const [autoRun, setAutoRun] = useState(true);
  const [accountBasis, setAccountBasis] = useState<"clone_portfolio" | "cash_only">("clone_portfolio");
  const [startingCash, setStartingCash] = useState("100000");
  const [loading, setLoading] = useState(true);
  const [loadedOnce, setLoadedOnce] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [runningJob, setRunningJob] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [expandedZones, setExpandedZones] = useState<Set<string>>(new Set());
  const [loadingDetailIds, setLoadingDetailIds] = useState<Set<string>>(new Set());
  const detailedExperimentIds = useRef<Set<string>>(new Set());
  const loadedOnceRef = useRef(false);

  function toggleZone(key: string) {
    setExpandedZones(prev => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  async function loadState(options?: { background?: boolean }) {
    if (!options?.background && !loadedOnceRef.current) {
      setLoading(true);
    }
    try {
      const [shadowData, automationData] = await Promise.all([
        apiFetch<ShadowExperiment[]>("/shadow/experiments"),
        apiFetch<AutomationStatus>("/automation/status"),
      ]);
      setExperiments((previous) =>
        shadowData.map((summary) => {
          if (!detailedExperimentIds.current.has(summary.id)) return summary;
          const detail = previous.find((item) => item.id === summary.id);
          if (!detail) return summary;
          return {
            ...detail,
            ...summary,
            run_details: { ...detail.run_details, ...summary.run_details },
            report: { ...detail.report, ...summary.report },
            actions: detail.actions,
            orders: detail.orders,
            fills: detail.fills,
            account_events: detail.account_events,
            paper_positions: detail.paper_positions,
          };
        }),
      );
      setAutomation(automationData);
      loadedOnceRef.current = true;
      setLoadedOnce(true);
      setLoadError(null);
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : "Unable to load shadow lab.");
    } finally {
      if (!options?.background) {
        setLoading(false);
      }
    }
  }

  useEffect(() => {
    void loadState();
  }, []);

  async function loadExperimentDetails(id: string) {
    if (detailedExperimentIds.current.has(id) || loadingDetailIds.has(id)) return;
    setLoadingDetailIds((previous) => new Set(previous).add(id));
    try {
      const detail = await apiFetch<ShadowExperiment>(`/shadow/experiments/${id}`);
      detailedExperimentIds.current.add(id);
      setExperiments((previous) =>
        previous.map((item) => (item.id === id ? detail : item)),
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to load experiment details.");
    } finally {
      setLoadingDetailIds((previous) => {
        const next = new Set(previous);
        next.delete(id);
        return next;
      });
    }
  }

  const hasActiveExperiment = experiments.some((experiment) =>
    ["queued", "running", "pending"].includes(experiment.run_status),
  );

  useEffect(() => {
    const interval = window.setInterval(() => {
      void loadState({ background: true });
    }, hasActiveExperiment ? 3000 : 15000);
    return () => window.clearInterval(interval);
  }, [hasActiveExperiment]);

  async function createExperiment(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError(null);
    setNotice(autoRun ? "Creating shadow clone and queueing the experiment." : "Opening a manual paper account from the current portfolio snapshot.");
    try {
      const response = await fetch(`${API_BASE}/shadow/experiments`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name,
          policy_description: operatorPrompt.trim() || "Custom policy experiment.",
          trigger_type: "manual_review",
          trigger_reason: "User requested an explicit shadow review from the Shadow Lab.",
          horizon_label: "short_term",
          initiated_by: "user",
          operator_prompt: operatorPrompt.trim() || undefined,
          auto_run: autoRun,
          account_basis: autoRun ? "clone_portfolio" : accountBasis,
          starting_cash: !autoRun && accountBasis === "cash_only" ? Number(startingCash) : undefined,
        }),
      });
      if (!response.ok) {
        throw new Error(await response.text());
      }
      await response.json();
      setOperatorPrompt("");
      setNotice(
        autoRun
          ? "Shadow experiment queued. It will move to running once execution begins."
          : "Manual paper account opened. Orders remain simulated and can never reach a real broker.",
      );
      await loadState();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to create experiment.");
      setNotice(null);
    } finally {
      setSaving(false);
    }
  }

  async function runExperiment(id: string) {
    const response = await fetch(`${API_BASE}/shadow/experiments/${id}/run`, {
      method: "POST",
    });
    if (!response.ok) {
      throw new Error(await response.text());
    }
    return (await response.json()) as ShadowExperiment;
  }

  async function rerunExperiment(id: string) {
    setRunningJob(`experiment:${id}`);
    setError(null);
    setNotice("Queueing shadow experiment re-run.");
    try {
      await runExperiment(id);
      detailedExperimentIds.current.delete(id);
      await loadState();
      setNotice("Shadow experiment re-queued.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to rerun experiment.");
      setNotice(null);
    } finally {
      setRunningJob(null);
    }
  }

  async function cancelPaperOrder(experimentId: string, orderId: string) {
    setRunningJob(`order:${orderId}`);
    setError(null);
    try {
      const updated = await apiFetch<ShadowExperiment>(`/shadow/experiments/${experimentId}/orders/${orderId}/cancel`, {
        method: "POST",
      });
      detailedExperimentIds.current.add(experimentId);
      setExperiments((previous) =>
        previous.map((item) => (item.id === experimentId ? updated : item)),
      );
      setNotice("Paper order canceled and reserved buying power released.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to cancel paper order.");
    } finally {
      setRunningJob(null);
    }
  }

  async function submitManualPaperOrder(
    experimentId: string,
    order: { ticker: string; side: "buy" | "sell"; quantity: number; rationale: string },
  ) {
    setRunningJob(`manual-order:${experimentId}`);
    setError(null);
    try {
      const updated = await apiFetch<ShadowExperiment>(
        `/shadow/experiments/${experimentId}/orders`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(order),
        },
      );
      const latest = updated.orders.at(-1);
      setNotice(
        latest?.status === "filled"
          ? `Paper order filled by the simulator: ${latest.side.toUpperCase()} ${latest.requested_quantity.toFixed(4)} ${latest.ticker}.`
          : latest?.status === "accepted"
            ? `Paper order accepted and waiting for a regular-session quote: ${latest.ticker}.`
            : `Paper order recorded as ${latest?.status ?? "unknown"}: ${latest?.rejection_reason?.replaceAll("_", " ") ?? "review the ledger"}.`,
      );
      detailedExperimentIds.current.add(experimentId);
      setExperiments((previous) =>
        previous.map((item) => (item.id === experimentId ? updated : item)),
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to submit paper order.");
      throw err;
    } finally {
      setRunningJob(null);
    }
  }

  async function triggerAutomation(jobName: string) {
    setRunningJob(jobName);
    setError(null);
    setNotice(null);
    try {
      const response = await fetch(`${API_BASE}/automation/run/${jobName}`, { method: "POST" });
      if (!response.ok) {
        throw new Error(await response.text());
      }
      setAutomation(await response.json());
      setNotice(
        jobName === "shadow_refresh"
          ? "Queued shadow runs refreshed."
          : `${jobName} completed.`,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to trigger automation.");
    } finally {
      setRunningJob(null);
    }
  }

  return (
    <div className="min-h-screen bg-background font-sans text-foreground">
      <AppNav active="experiments" />

      <main className="mx-auto grid w-full max-w-[1440px] grid-cols-1 items-start gap-6 px-4 py-8 sm:px-6 lg:px-8 xl:grid-cols-[minmax(360px,0.72fr)_minmax(0,1.28fr)]">
        {error ? (
          <FloatingNotice tone="error" message={error} onDismiss={() => setError(null)} />
        ) : null}
        {!error && notice ? (
          <FloatingNotice tone="success" message={notice} onDismiss={() => setNotice(null)} />
        ) : null}
        <PageHeader
          className="xl:col-span-2"
          eyebrow="Simulation"
          title="Shadow lab"
          description="Run a parallel portfolio experiment from the live book, then review what changed, why, and how the result compared with the real portfolio."
        />

        <section className="self-start space-y-6 xl:sticky xl:top-20 xl:max-h-[calc(100vh-6rem)] xl:overflow-y-auto xl:pr-1">

          <form onSubmit={createExperiment} className="space-y-5 rounded-lg border border-slate-200 bg-white p-5 dark:border-slate-800 dark:bg-slate-950">
            <div className="flex items-start justify-between gap-4">
              <div>
                <h2 className="text-sm font-semibold text-slate-700 dark:text-slate-200">Launch a new experiment</h2>
                <p className="mt-2 text-sm text-slate-500 dark:text-slate-400">
                  Write a single instruction describing how the shadow portfolio should behave differently from the real one. Prophet will handle the rest.
                </p>
              </div>
              {!autoRun ? (
                <div>
                  <p className="text-xs font-medium text-slate-600 dark:text-slate-300">Starting book</p>
                  <div className="mt-2 grid grid-cols-2 rounded-md border border-slate-300 p-1 dark:border-slate-700" role="group" aria-label="Paper account starting book">
                    <button
                      type="button"
                      onClick={() => setAccountBasis("clone_portfolio")}
                      className={`rounded px-3 py-2 text-sm font-medium ${accountBasis === "clone_portfolio" ? "bg-slate-900 text-white dark:bg-slate-100 dark:text-slate-950" : "text-slate-500 hover:text-slate-900 dark:text-slate-400 dark:hover:text-white"}`}
                    >
                      Clone live book
                    </button>
                    <button
                      type="button"
                      onClick={() => setAccountBasis("cash_only")}
                      className={`rounded px-3 py-2 text-sm font-medium ${accountBasis === "cash_only" ? "bg-slate-900 text-white dark:bg-slate-100 dark:text-slate-950" : "text-slate-500 hover:text-slate-900 dark:text-slate-400 dark:hover:text-white"}`}
                    >
                      Cash only
                    </button>
                  </div>
                  {accountBasis === "cash_only" ? (
                    <label className="mt-3 block text-xs font-medium text-slate-600 dark:text-slate-300">
                      Starting paper cash
                      <input
                        value={startingCash}
                        onChange={(event) => setStartingCash(event.target.value)}
                        type="number"
                        min="0.01"
                        step="0.01"
                        className="mt-2 block w-full rounded-md border border-slate-300 bg-white px-3 py-2.5 text-sm text-slate-900 focus:border-slate-500 focus:outline-none dark:border-slate-700 dark:bg-slate-900 dark:text-white"
                      />
                    </label>
                  ) : null}
                  <p className="mt-2 text-xs text-slate-500 dark:text-slate-400">
                    {accountBasis === "clone_portfolio"
                      ? "Starts with a deterministic copy of current tracked positions and buying power."
                      : "Starts empty with the paper cash entered above; the live portfolio remains only a research reference."}
                  </p>
                </div>
              ) : null}
            </div>

            <div className="space-y-4">
              <div>
                <p className="text-xs font-medium text-slate-600 dark:text-slate-300">Account mode</p>
                <div className="mt-2 grid grid-cols-2 rounded-md border border-slate-300 p-1 dark:border-slate-700" role="group" aria-label="Paper account mode">
                  <button
                    type="button"
                    onClick={() => setAutoRun(true)}
                    className={`rounded px-3 py-2 text-sm font-medium ${autoRun ? "bg-slate-900 text-white dark:bg-slate-100 dark:text-slate-950" : "text-slate-500 hover:text-slate-900 dark:text-slate-400 dark:hover:text-white"}`}
                  >
                    Autonomous
                  </button>
                  <button
                    type="button"
                    onClick={() => setAutoRun(false)}
                    className={`rounded px-3 py-2 text-sm font-medium ${!autoRun ? "bg-slate-900 text-white dark:bg-slate-100 dark:text-slate-950" : "text-slate-500 hover:text-slate-900 dark:text-slate-400 dark:hover:text-white"}`}
                  >
                    Manual
                  </button>
                </div>
                <p className="mt-2 text-xs text-slate-500 dark:text-slate-400">
                  {autoRun
                    ? "Prophet proposes bounded target weights; the paper broker alone validates and fills orders."
                    : "You submit simulated orders through a ticket. No model action and no real-broker routing."}
                </p>
              </div>
              <div>
                <label className="block text-xs font-medium text-slate-600 dark:text-slate-300">Experiment name</label>
                <input
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  className="mt-2 block w-full rounded-md border border-slate-300 bg-white px-3 py-2.5 text-sm text-slate-900 focus:border-slate-500 focus:outline-none focus:ring-2 focus:ring-slate-900/10 dark:border-slate-700 dark:bg-slate-900 dark:text-white dark:focus:ring-white/10"
                />
              </div>

              <div>
                <label className="block text-xs font-medium text-slate-600 dark:text-slate-300">{autoRun ? "Policy prompt" : "Account note"}</label>
                <textarea
                  value={operatorPrompt}
                  onChange={(e) => setOperatorPrompt(e.target.value)}
                  rows={4}
                  placeholder={autoRun ? "Example: Act more defensively if the thesis is thin, keep 20% cash, and test exiting non-conviction positions." : "Optional note describing what this manual paper account is testing."}
                  className="mt-2 block min-h-[140px] w-full rounded-md border border-slate-300 bg-white px-3 py-2.5 text-sm text-slate-900 focus:border-slate-500 focus:outline-none focus:ring-2 focus:ring-slate-900/10 dark:border-slate-700 dark:bg-slate-900 dark:text-white dark:focus:ring-white/10"
                />
              </div>
            </div>

            <button disabled={saving || (autoRun && !operatorPrompt.trim()) || (!autoRun && accountBasis === "cash_only" && !(Number(startingCash) > 0))} className="w-full rounded-md bg-slate-900 px-4 py-2.5 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-50 dark:bg-slate-100 dark:text-slate-950 dark:hover:bg-slate-300">
              {saving ? "Creating..." : autoRun ? "Start shadow run" : "Open paper account"}
            </button>
          </form>

          <details className="rounded-lg border border-slate-200 bg-white p-5 dark:border-slate-800 dark:bg-slate-950">
            <summary className="list-none cursor-pointer">
              <div className="flex items-start justify-between gap-4">
                <div>
                  <h2 className="text-sm font-semibold text-slate-700 dark:text-slate-200">Automation</h2>
                  <p className="mt-2 text-sm text-slate-500 dark:text-slate-400">
                    Shadow refresh and agent reflection keep parallel experiments moving even when you are not on this page.
                  </p>
                </div>
                <div className="flex items-center gap-3">
                  <button onClick={() => void loadState()} className="text-xs font-medium text-slate-600 hover:text-slate-950 dark:text-slate-300 dark:hover:text-white">
                    Refresh
                  </button>
                </div>
              </div>
            </summary>
            <div className="mt-4 divide-y divide-slate-200 dark:divide-slate-800">
              {automation?.jobs.map((job) => (
                <div key={job.name} className="py-4 first:pt-0 last:pb-0">
                  <div className="flex flex-col items-start gap-3 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
                    <div className="min-w-0">
                      <p className="font-medium">{job.name}</p>
                      <p className="text-sm text-slate-500 dark:text-slate-400">{job.detail ?? "No detail yet."}</p>
                    </div>
                    <button
                      onClick={() => void triggerAutomation(job.name)}
                      disabled={runningJob !== null}
                      className="shrink-0 rounded-lg border border-slate-300 px-3 py-2 text-sm disabled:opacity-50 dark:border-slate-700"
                    >
                      Run now
                    </button>
                  </div>
                  <p className="mt-2 text-xs font-mono text-slate-400">
                    status={job.last_status} interval={job.interval_seconds ?? 0}s last={job.last_run_at ?? "never"}
                  </p>
                </div>
              ))}
            </div>
          </details>
        </section>

        <section className="min-w-0 space-y-4 overflow-hidden">
          <div className="flex min-h-9 items-center justify-between border-b border-slate-200 pb-3 dark:border-slate-800">
            <h2 className="text-sm font-semibold text-slate-700 dark:text-slate-200">Experiment runs</h2>
            <span className="text-xs text-slate-500 dark:text-slate-400">{experiments.length} total</span>
          </div>
          {loadedOnce && loadError ? (
            <WorkspaceState
              kind="degraded"
              compact
              title="Experiment refresh is delayed"
              description={`${loadError} The last loaded experiment state remains visible.`}
              actionLabel="Retry refresh"
              onAction={() => void loadState({ background: true })}
            />
          ) : null}
          {!loadedOnce && loading ? (
            <WorkspaceState
              kind="loading"
              title="Loading shadow experiments"
              description="Retrieving paper accounts, experiment checkpoints, and automation status."
            />
          ) : !loadedOnce && loadError ? (
            <WorkspaceState
              kind="error"
              title="Shadow lab is unavailable"
              description={loadError}
              actionLabel="Retry"
              onAction={() => void loadState()}
            />
          ) : experiments.length === 0 ? (
            <WorkspaceState
              kind="empty"
              title="No shadow experiments yet"
              description="Define a policy above to compare a simulated portfolio path with the live book."
            />
          ) : (
            experiments.map((experiment) => (
              <article key={experiment.id} className="min-w-0 overflow-hidden rounded-lg border border-slate-200 bg-white p-5 [overflow-wrap:anywhere] dark:border-slate-800 dark:bg-slate-950">
                <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <h2 className="text-xl font-semibold tracking-tight">{experiment.name}</h2>
                      <StatusBadge status={experiment.run_status} />
                    </div>
                    <p className="mt-2 text-sm text-slate-500 dark:text-slate-400">{experiment.policy_description}</p>
                  </div>
                  {experiment.execution_mode !== "manual" ? (
                    <button
                      onClick={() => void rerunExperiment(experiment.id)}
                      disabled={runningJob !== null}
                      className="rounded-lg border border-slate-300 dark:border-slate-700 px-4 py-2 text-sm disabled:opacity-50"
                    >
                      {runningJob === `experiment:${experiment.id}` ? "Re-running..." : "Re-run"}
                    </button>
                  ) : null}
                </div>

                <div className="mt-5 grid gap-4 md:grid-cols-2">
                  <InfoCard
                    label="Why it exists"
                    body={`trigger ${experiment.trigger_type ?? "unknown"} · initiated by ${experiment.initiated_by ?? "unknown"}`}
                    detail={experiment.trigger_reason ?? "No trigger reason stored."}
                    hint="This is why the system decided this live experiment was worth running."
                  />
                  <InfoCard
                    label="Cloned state"
                    body={`holdings ${experiment.snapshot_summary.holding_count ?? 0} · tracked ${experiment.snapshot_summary.tracked_count ?? 0}`}
                    detail={`market value ${formatCurrency(experiment.snapshot_summary.total_market_value)} · buying power ${formatCurrency(experiment.snapshot_summary.remaining_buying_power)}`}
                    hint="This is the live portfolio state the experiment started from. It is not a historical rewind."
                  />
                  <InfoCard
                    label="Guidance mode"
                    body={experiment.execution_mode === "manual" ? "user directed" : experiment.guidance_mode ? experiment.guidance_mode.replaceAll("_", " ") : "follow existing policy"}
                    detail={experiment.execution_mode === "manual" ? "Only orders submitted through the manual ticket can change this paper account." : experiment.guidance_summary ?? "No structured guidance summary stored."}
                    hint={experiment.execution_mode === "manual" ? "The model does not create orders for this account." : "The shadow LLM compresses the policy and operator guidance into one bounded management mode."}
                  />
                  <InfoCard
                    label="Horizon"
                    body={experiment.horizon_label ?? "unspecified"}
                    detail={`${describeStatus(experiment.run_status)} · created ${formatDate(experiment.created_at)}${experiment.completed_at ? ` · completed ${formatDate(experiment.completed_at)}` : ""}`}
                    hint="This is the intended decision horizon for the experiment, not a retrospective backtest window."
                  />
                </div>

                <OpportunitySummary profile={experiment.report?.opportunity_summary ?? experiment.discovery_profile} />

                {experiment.operator_prompt ? (
                  <details
                    className="mt-3 rounded-lg border border-slate-200 p-3 dark:border-slate-800"
                    open={expandedZones.has(`${experiment.id}:operator`)}
                    onToggle={(e) => {
                      const isOpen = (e.target as HTMLDetailsElement).open;
                      const key = `${experiment.id}:operator`;
                      if (isOpen !== expandedZones.has(key)) toggleZone(key);
                    }}
                  >
                    <summary className="cursor-pointer text-xs font-bold uppercase tracking-wider text-slate-500">
                      Operator guidance
                    </summary>
                    <p className="mt-3 whitespace-pre-wrap text-sm text-slate-600 dark:text-slate-300">
                      {experiment.operator_prompt}
                    </p>
                  </details>
                ) : null}

                {experiment.run_status === "completed" && experiment.result ? (
                  <div className="mt-4 grid grid-cols-1 gap-4 md:grid-cols-3">
                    <Metric label="Shadow Return" value={formatPct(experiment.result.shadow_return)} />
                    <Metric label="Real Book Baseline" value={formatPct(experiment.result.actual_return)} />
                    <Metric label="Out/Underperformance" value={formatPct(experiment.result.alpha)} />
                  </div>
                ) : null}

                {experiment.skip_reason ? (
                  <div className="mt-4 rounded-lg border border-amber-200 bg-amber-50 p-4 text-sm text-amber-800 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-300">
                    {experiment.skip_reason}
                  </div>
                ) : null}

                {experiment.run_status === "queued" ? (
                  <div className="mt-4 rounded-lg border border-sky-200 bg-sky-50 p-4 text-sm text-sky-800 dark:border-sky-900 dark:bg-sky-950/30 dark:text-sky-300">
                    This shadow run is queued. Prophet has cloned the current portfolio state and will begin the live experiment on the next shadow refresh cycle.
                  </div>
                ) : null}

                {experiment.run_status === "running" ? (
                  <div className="mt-4 rounded-lg border border-sky-200 bg-sky-50 p-4 text-sm text-sky-800 dark:border-sky-900 dark:bg-sky-950/30 dark:text-sky-300">
                    {(() => {
                      const progress = experiment.run_details?.progress ?? {};
                      const stepCount = progress.step_count ?? 0;
                      const targetSteps = progress.target_steps ?? 0;
                      return `This shadow run is active and evolving in parallel with the live portfolio. It has recorded ${stepCount} of ${targetSteps || "?"} planned checkpoints so far.`;
                    })()}
                  </div>
                ) : null}

                {experiment.execution_mode === "manual" ? (
                  <div className="mt-4 rounded-lg border border-slate-200 bg-slate-50 p-4 text-sm text-slate-700 dark:border-slate-800 dark:bg-slate-900/40 dark:text-slate-300">
                    This is a user-directed paper account. Orders use current quotes, configured slippage and buying-power checks; they never route to a real broker.
                  </div>
                ) : null}

                {experiment.run_status === "failed" ? (
                  <div className="mt-4 rounded-lg border border-rose-200 bg-rose-50 p-4 text-sm text-rose-800 dark:border-rose-900 dark:bg-rose-950/30 dark:text-rose-300">
                    This shadow run failed before completion. Review the stored reason below and re-run once the issue is resolved.
                  </div>
                ) : null}

                {experiment.run_status === "completed" && experiment.report?.policy_assessment ? (
                  <div className="mt-4 rounded-lg border border-slate-200 p-4 dark:border-slate-800">
                    <div className="flex items-start justify-between gap-3">
                      <p className="text-xs uppercase tracking-wider text-slate-400">Experiment report</p>
                    </div>
                    <p className="mt-3 text-sm text-slate-600 dark:text-slate-300">
                      {experiment.report.policy_assessment}
                    </p>
                    {experiment.report.key_lesson ? (
                      <p className="mt-3 rounded-lg border border-sky-200 bg-sky-50 px-3 py-2 text-sm text-sky-900 dark:border-sky-900 dark:bg-sky-950/30 dark:text-sky-200">
                        {experiment.report.key_lesson}
                      </p>
                    ) : null}
                    {experiment.lesson ? (
                      <div className="mt-4 border-t border-slate-200 pt-4 dark:border-slate-800">
                        <div className="flex flex-wrap items-center justify-between gap-3">
                          <div>
                            <p className="text-xs font-semibold uppercase tracking-wider text-slate-500">
                              Learning state
                            </p>
                            <p className="mt-1 text-sm font-medium text-slate-900 dark:text-slate-100">
                              {experiment.lesson.title}
                            </p>
                          </div>
                          <span
                            className="rounded-full border border-slate-300 px-2.5 py-1 text-xs font-semibold uppercase text-slate-600 dark:border-slate-700 dark:text-slate-300"
                            title="Provisional means fewer than the configured minimum repeated experiments. Validated requires repeated, directionally consistent outcomes. Mixed means the family has meaningful counterexamples."
                          >
                            {experiment.lesson.maturity_status}
                          </span>
                        </div>
                        <p className="mt-3 text-sm text-slate-600 dark:text-slate-300">
                          {experiment.lesson.summary}
                        </p>
                        <dl className="mt-4 grid grid-cols-2 gap-x-5 gap-y-3 text-sm sm:grid-cols-4">
                          <div>
                            <dt className="text-xs uppercase text-slate-400">Confidence</dt>
                            <dd className="mt-1 font-medium">
                              {formatPct(experiment.lesson.confidence_score)}
                            </dd>
                          </div>
                          <div>
                            <dt className="text-xs uppercase text-slate-400">Supportive</dt>
                            <dd className="mt-1 font-medium">{experiment.lesson.supporting_observations}</dd>
                          </div>
                          <div>
                            <dt className="text-xs uppercase text-slate-400">Contradictory</dt>
                            <dd className="mt-1 font-medium">{experiment.lesson.contradicting_observations}</dd>
                          </div>
                          <div>
                            <dt className="text-xs uppercase text-slate-400">Immaterial</dt>
                            <dd className="mt-1 font-medium">{experiment.lesson.neutral_observations}</dd>
                          </div>
                        </dl>
                      </div>
                    ) : null}
                  </div>
                ) : null}

                <details
                  className="mt-3 rounded-lg border border-slate-200 p-3 dark:border-slate-800"
                  open={expandedZones.has(`${experiment.id}:details`)}
                  onToggle={(e) => {
                    const isOpen = (e.target as HTMLDetailsElement).open;
                    const key = `${experiment.id}:details`;
                    if (isOpen) void loadExperimentDetails(experiment.id);
                    if (isOpen !== expandedZones.has(key)) toggleZone(key);
                  }}
                >
                  <summary className="cursor-pointer text-xs font-bold uppercase tracking-wider text-slate-500">
                    Run details
                  </summary>
                  {loadingDetailIds.has(experiment.id) ? (
                    <p className="mt-4 text-sm text-slate-500 dark:text-slate-400">
                      Loading the full checkpoint, order, and decision history...
                    </p>
                  ) : null}
                  {experiment.run_status === "completed" && experiment.result ? (
                    <div className="mt-4">
                      <ShadowComparisonChart experiment={experiment} />
                    </div>
                  ) : null}

                  {experiment.report?.policy_summary?.objective ||
                  experiment.report?.expected_outcome?.summary ||
                  experiment.report?.actual_outcome?.summary ? (
                    <div className="mt-4 grid gap-4 lg:grid-cols-3">
                      {experiment.report?.policy_summary?.objective ? (
                        <div className="rounded-lg border border-slate-200 p-4 dark:border-slate-800">
                          <p className="text-xs uppercase tracking-wider text-slate-400">What it was trying to do</p>
                          <p className="mt-3 text-sm text-slate-600 dark:text-slate-300">
                            {experiment.report.policy_summary.objective}
                          </p>
                        </div>
                      ) : null}
                      {experiment.report?.expected_outcome?.summary ? (
                        <div className="rounded-lg border border-amber-200 bg-amber-50/60 p-4 dark:border-amber-900 dark:bg-amber-950/20">
                          <p className="text-xs uppercase tracking-wider text-amber-600 dark:text-amber-300">What it expected</p>
                          <p className="mt-3 text-sm text-slate-700 dark:text-slate-200">
                            {experiment.report.expected_outcome.summary}
                          </p>
                        </div>
                      ) : null}
                      {experiment.report?.actual_outcome?.summary ? (
                        <div className="rounded-lg border border-emerald-200 bg-emerald-50/60 p-4 dark:border-emerald-900 dark:bg-emerald-950/20">
                          <p className="text-xs uppercase tracking-wider text-emerald-600 dark:text-emerald-300">What actually happened</p>
                          <p className="mt-3 text-sm text-slate-700 dark:text-slate-200">
                            {experiment.report.actual_outcome.summary}
                          </p>
                        </div>
                      ) : null}
                    </div>
                  ) : null}

                  {experiment.report?.learning_summary ? (
                    <div className="mt-4 rounded-lg border border-slate-200 p-4 dark:border-slate-800">
                      <p className="text-xs uppercase tracking-wider text-slate-400">How the system should learn from this</p>
                      <p className="mt-3 text-sm text-slate-600 dark:text-slate-300">
                        {experiment.report.learning_summary.why_this_matters}
                      </p>
                      <p className="mt-2 text-sm text-slate-500 dark:text-slate-400">
                        Baseline: {experiment.report.learning_summary.baseline_description}
                      </p>
                    </div>
                  ) : null}

                  {experiment.report?.thesis_context?.length ? (
                    <div className="mt-4 rounded-lg border border-slate-200 p-4 dark:border-slate-800">
                      <div className="flex items-start justify-between gap-3">
                        <p className="text-xs uppercase tracking-wider text-slate-400">Thesis context used by the run</p>
                      </div>
                      <div className="mt-3 space-y-3">
                        {experiment.report.thesis_context.map((item, index) => (
                          <div key={`${experiment.id}-thesis-${index}`} className="rounded-lg border border-slate-200 p-3 text-sm dark:border-slate-800">
                            <p className="font-medium text-slate-800 dark:text-slate-100">
                              {item.ticker} {item.entity_name ? `· ${item.entity_name}` : ""}
                            </p>
                            <p className="mt-1 text-slate-500 dark:text-slate-400">
                              stance {item.stance ?? "n/a"} · confidence {item.confidence_band ?? "n/a"} · action {item.action ?? "n/a"}
                            </p>
                            {item.thesis_summary ? (
                              <p className="mt-2 text-slate-600 dark:text-slate-300">{item.thesis_summary}</p>
                            ) : null}
                            {item.rationale ? (
                              <p className="mt-2 text-slate-500 dark:text-slate-400">{item.rationale}</p>
                            ) : null}
                          </div>
                        ))}
                      </div>
                    </div>
                  ) : null}

                  <div className="mt-4 grid gap-4 lg:grid-cols-2">
                    <div className="rounded-lg border border-slate-200 p-4 text-sm dark:border-slate-800">
                      <div className="flex items-start justify-between gap-3">
                        <p className="text-xs uppercase tracking-wider text-slate-400">Run state</p>
                      </div>
                      <div className="mt-3 space-y-2 text-slate-600 dark:text-slate-300">
                        <p>
                          progress:{" "}
                          {experiment.run_details.progress?.step_count == null
                            ? "not started"
                            : `${experiment.run_details.progress.step_count} / ${experiment.run_details.progress.target_steps ?? "?"} checkpoints`}
                        </p>
                        {experiment.run_status === "running" && experiment.run_details.progress?.next_checkpoint_at ? (
                          <p>
                            next scheduled checkpoint: {formatDate(experiment.run_details.progress.next_checkpoint_at)}
                          </p>
                        ) : null}
                        <p>
                          observation window: {formatDate(experiment.start_point)} to {formatDate(experiment.end_point)}
                        </p>
                        <p>starting buying power: {formatCurrency(experiment.run_details.starting_buying_power)}</p>
                        <p>ending buying power: {formatCurrency(experiment.run_details.ending_buying_power)}</p>
                        <p>cash reserve target: {formatCurrency(experiment.run_details.reserve_target)}</p>
                        <p>
                          max position multiplier:{" "}
                          {experiment.run_details.guidance?.max_position_multiplier == null
                            ? "n/a"
                            : `${experiment.run_details.guidance.max_position_multiplier.toFixed(2)}x`}
                        </p>
                      </div>
                    </div>
                    <div className="rounded-lg border border-slate-200 p-4 text-sm dark:border-slate-800">
                      <div className="flex items-start justify-between gap-3">
                        <p className="text-xs uppercase tracking-wider text-slate-400">What the run concluded</p>
                      </div>
                      {experiment.run_status === "completed" && experiment.result ? (
                        <p className="mt-3 text-slate-600 dark:text-slate-300">{experiment.result.reasoning}</p>
                      ) : experiment.execution_mode === "manual" ? (
                        <p className="mt-3 text-slate-500 dark:text-slate-400">
                          This account has no autonomous conclusion. Its durable order and fill ledger is the record of what was actually simulated.
                        </p>
                      ) : (
                        <p className="mt-3 text-slate-500 dark:text-slate-400">
                          The run is still gathering checkpoints. Prophet will only lock in a final comparison and lesson once the planned live observation window completes.
                        </p>
                      )}
                    </div>
                  </div>

                  {experiment.run_details.pending_evidence_events?.length ||
                  experiment.run_details.evidence_event_log?.length ? (
                    <div className="mt-4 rounded-lg border border-slate-200 p-4 dark:border-slate-800">
                      <div className="flex flex-wrap items-start justify-between gap-3">
                        <div>
                          <p className="text-xs uppercase tracking-wider text-slate-400">
                            Evidence wake-ups
                          </p>
                          <p className="mt-2 text-sm text-slate-500 dark:text-slate-400">
                            New source-backed evidence can wake an active experiment. Pending items remain queued until a provider-backed checkpoint evaluates them.
                          </p>
                        </div>
                        <span
                          className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-600 dark:border-slate-700 dark:text-slate-300"
                          title="Safe hold fallbacks do not consume evidence wake-ups. The event remains pending for a later provider-backed checkpoint."
                        >
                          {experiment.run_details.pending_evidence_events?.length ?? 0} pending
                        </span>
                      </div>
                      <div className="mt-4 divide-y divide-slate-200 dark:divide-slate-800">
                        {[
                          ...(experiment.run_details.pending_evidence_events ?? []).map((event) => ({
                            ...event,
                            state: "pending" as const,
                          })),
                          ...(experiment.run_details.evidence_event_log ?? []).map((event) => ({
                            ...event,
                            state: "consumed" as const,
                          })),
                        ].map((event) => (
                          <div key={`${experiment.id}-evidence-event-${event.event_id}`} className="py-3 text-sm">
                            <div className="flex flex-wrap items-center gap-2">
                              <span className="font-medium text-slate-800 dark:text-slate-100">
                                {event.trigger_reason}
                              </span>
                              <span className="rounded border border-slate-300 px-2 py-0.5 text-xs text-slate-500 dark:border-slate-700 dark:text-slate-400">
                                {event.state}
                              </span>
                            </div>
                            <p className="mt-1 text-slate-500 dark:text-slate-400">
                              {event.state === "consumed" && event.consumed_at
                                ? `evaluated ${formatDate(event.consumed_at)}${event.checkpoint_index ? ` at checkpoint ${event.checkpoint_index}` : ""}`
                                : event.queued_at
                                  ? `queued ${formatDate(event.queued_at)}`
                                  : "queued time unavailable"}
                              {event.metadata?.triggers?.length
                                ? ` · ${event.metadata.triggers.join(", ").replaceAll("_", " ")}`
                                : ""}
                            </p>
                          </div>
                        ))}
                      </div>
                    </div>
                  ) : null}

                  {experiment.run_details.checkpoint_log?.length ? (
                    <div className="mt-4 rounded-lg border border-slate-200 p-4 dark:border-slate-800">
                      <div className="flex items-start justify-between gap-3">
                        <p className="text-xs uppercase tracking-wider text-slate-400">Checkpoint timeline</p>
                      </div>
                      <div className="mt-3 space-y-3">
                        {experiment.run_details.checkpoint_log.map((checkpoint, index) => (
                          <div key={`${experiment.id}-checkpoint-${index}`} className="rounded-lg border border-slate-200 p-3 text-sm dark:border-slate-800">
                            <p className="font-medium text-slate-800 dark:text-slate-100">
                              Checkpoint {checkpoint.step_index}
                              {checkpoint.captured_at ? ` · ${formatDate(checkpoint.captured_at)}` : ""}
                            </p>
                            <p className="mt-2 text-slate-600 dark:text-slate-300">
                              {checkpoint.summary}
                            </p>
                          </div>
                        ))}
                      </div>
                    </div>
                  ) : null}

                  {experiment.run_details.decision_history?.length ? (
                    <div className="mt-4 rounded-lg border border-slate-200 p-4 dark:border-slate-800">
                      <div className="flex items-start justify-between gap-3">
                        <p className="text-xs uppercase tracking-wider text-slate-400">Decision history</p>
                      </div>
                      <div className="mt-3 space-y-3">
                        {experiment.run_details.decision_history.map((entry, index) => (
                          <div key={`${experiment.id}-decision-${index}`} className="rounded-lg border border-slate-200 p-3 text-sm dark:border-slate-800">
                            <p className="font-medium text-slate-800 dark:text-slate-100">
                              Checkpoint {entry.step_index}
                              {entry.observed_at ? ` · ${formatDate(entry.observed_at)}` : ""}
                            </p>
                            {entry.checkpoint_objective ? (
                              <p className="mt-2 text-slate-700 dark:text-slate-200">{entry.checkpoint_objective}</p>
                            ) : null}
                            {entry.portfolio_view ? (
                              <p className="mt-2 text-slate-500 dark:text-slate-400">{entry.portfolio_view}</p>
                            ) : null}
                            {entry.planned_posture ? (
                              <p className="mt-2 text-slate-500 dark:text-slate-400">posture: {entry.planned_posture}</p>
                            ) : null}
                            {entry.prior_realization ? (
                              <p className="mt-2 text-slate-500 dark:text-slate-400">{entry.prior_realization}</p>
                            ) : null}
                            {entry.research_goal ? (
                              <p className="mt-2 text-slate-600 dark:text-slate-300">
                                research goal: {entry.research_goal}
                              </p>
                            ) : null}
                            {entry.shadow_research?.title ? (
                              <p className="mt-2 text-slate-500 dark:text-slate-400">
                                research run: {entry.shadow_research.title} · {entry.shadow_research.reason}
                              </p>
                            ) : null}
                            {entry.baseline_comparison ? (
                              <p className="mt-2 text-slate-500 dark:text-slate-400">
                                baseline check: shadow {formatPct(entry.baseline_comparison.shadow_return)} · real {formatPct(entry.baseline_comparison.real_portfolio_return)} · alpha {formatPct(entry.baseline_comparison.alpha)}
                              </p>
                            ) : null}
                            {entry.decisions?.length ? (
                              <div className="mt-3 space-y-2">
                                {entry.decisions.map((decision, decisionIndex) => (
                                  <div key={`${experiment.id}-decision-item-${decisionIndex}`} className="rounded-lg bg-slate-50 p-3 dark:bg-slate-900/40">
                                    <p className="font-medium text-slate-800 dark:text-slate-100">
                                      {decision.ticker}
                                      {decision.entity_name ? ` · ${decision.entity_name}` : ""}
                                      {decision.action ? ` · ${decision.action.toUpperCase()}` : ""}
                                    </p>
                                    {decision.observed_signal ? (
                                      <p className="mt-1 text-slate-500 dark:text-slate-400">signal: {decision.observed_signal}</p>
                                    ) : null}
                                    {decision.expected_outcome ? (
                                      <p className="mt-1 text-slate-600 dark:text-slate-300">expected: {decision.expected_outcome}</p>
                                    ) : null}
                                    {decision.risk_guardrail ? (
                                      <p className="mt-1 text-slate-500 dark:text-slate-400">guardrail: {decision.risk_guardrail}</p>
                                    ) : null}
                                    {decision.rationale ? (
                                      <p className="mt-1 text-slate-500 dark:text-slate-400">{decision.rationale}</p>
                                    ) : null}
                                  </div>
                                ))}
                              </div>
                            ) : null}
                          </div>
                        ))}
                      </div>
                    </div>
                  ) : null}

                  <PaperBrokerLedger
                    experiment={experiment}
                    cancelingOrderId={runningJob?.startsWith("order:") ? runningJob.slice(6) : null}
                    submittingOrder={runningJob === `manual-order:${experiment.id}`}
                    onCancel={cancelPaperOrder}
                    onSubmitManual={submitManualPaperOrder}
                  />

                  <div className="mt-4 rounded-lg border border-slate-200 p-4 dark:border-slate-800">
                    <div className="flex items-start justify-between gap-3">
                      <p className="text-xs uppercase tracking-wider text-slate-400">Run log</p>
                    </div>
                    {experiment.run_details.run_log?.length ? (
                      <div className="mt-3 space-y-3">
                        {experiment.run_details.run_log.map((entry, index) => (
                          <div key={`${experiment.id}-${entry.ticker}-${index}`} className="rounded-lg border border-slate-200 p-3 text-sm dark:border-slate-800">
                            <div className="flex flex-wrap items-center gap-2">
                              <span className="font-medium">{entry.ticker}</span>
                              {entry.entity_name ? <span className="text-slate-500 dark:text-slate-400">{entry.entity_name}</span> : null}
                              <span className="text-slate-500 dark:text-slate-400">{entry.action.toUpperCase()}</span>
                              <span className="text-slate-500 dark:text-slate-400">
                                {entry.quantity.toFixed(2)} @ {formatCurrency(entry.price)}
                              </span>
                            </div>
                            <p className="mt-2 text-slate-600 dark:text-slate-300">{entry.rationale}</p>
                            <p className="mt-2 text-slate-500 dark:text-slate-400">
                              stance {entry.stance ?? "n/a"} · confidence {entry.confidence_band ?? "n/a"}
                              {entry.actual_weight_pct != null ? ` · actual weight ${entry.actual_weight_pct.toFixed(2)}%` : ""}
                            </p>
                            {entry.thesis_summary ? (
                              <p className="mt-2 text-slate-500 dark:text-slate-400">{entry.thesis_summary}</p>
                            ) : null}
                            <p className="mt-2 text-slate-500 dark:text-slate-400">
                              buying power after trade: {formatCurrency(entry.post_trade_buying_power)}
                            </p>
                          </div>
                        ))}
                      </div>
                    ) : (
                      <p className="mt-3 text-sm text-slate-500 dark:text-slate-400">No detailed run log stored yet.</p>
                    )}
                  </div>
                </details>
              </article>
            ))
          )}
        </section>
      </main>
    </div>
  );
}

function PaperBrokerLedger({
  experiment,
  cancelingOrderId,
  submittingOrder,
  onCancel,
  onSubmitManual,
}: {
  experiment: ShadowExperiment;
  cancelingOrderId: string | null;
  submittingOrder: boolean;
  onCancel: (experimentId: string, orderId: string) => Promise<void>;
  onSubmitManual: (
    experimentId: string,
    order: { ticker: string; side: "buy" | "sell"; quantity: number; rationale: string },
  ) => Promise<void>;
}) {
  const [ticker, setTicker] = useState("");
  const [side, setSide] = useState<"buy" | "sell">("buy");
  const [quantity, setQuantity] = useState("");
  const [rationale, setRationale] = useState("");
  const account = experiment.run_details.paper_account;
  const orders = experiment.orders ?? [];
  const fills = experiment.fills ?? [];
  const accountEvents = experiment.account_events ?? [];
  const paperPositions = experiment.paper_positions ?? [];
  const isManual = experiment.execution_mode === "manual";
  if (!isManual && !account && orders.length === 0) return null;

  async function submitOrder(event: React.FormEvent) {
    event.preventDefault();
    const parsedQuantity = Number(quantity);
    if (!ticker.trim() || !Number.isFinite(parsedQuantity) || parsedQuantity <= 0) return;
    await onSubmitManual(experiment.id, {
      ticker: ticker.trim().toUpperCase(),
      side,
      quantity: parsedQuantity,
      rationale: rationale.trim() || "User-submitted paper order.",
    });
    setQuantity("");
    setRationale("");
  }

  return (
    <section className="mt-5 border-t border-slate-200 pt-5 dark:border-slate-800">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-300">Paper broker ledger</p>
          <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">Orders are derived from model intent; only deterministic fills change this account.</p>
        </div>
        <span className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-600 dark:border-slate-700 dark:text-slate-300">
          {account?.provider?.replaceAll("_", " ") ?? "local simulator"} · {fills.length} fill{fills.length === 1 ? "" : "s"}
        </span>
      </div>

      {isManual ? (
        <form onSubmit={submitOrder} className="mt-4 border-y border-slate-200 py-4 dark:border-slate-800">
          <div className="flex flex-wrap items-end gap-3">
            <label className="min-w-[120px] flex-1 text-xs font-medium text-slate-500 dark:text-slate-400">
              Ticker
              <input
                value={ticker}
                onChange={(event) => setTicker(event.target.value.toUpperCase())}
                placeholder="EXMPL"
                maxLength={24}
                className="mt-2 block w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm uppercase text-slate-900 focus:border-slate-500 focus:outline-none dark:border-slate-700 dark:bg-slate-900 dark:text-white"
              />
            </label>
            <div className="text-xs font-medium text-slate-500 dark:text-slate-400">
              Side
              <div className="mt-2 flex rounded-md border border-slate-300 p-1 dark:border-slate-700" role="group" aria-label="Paper order side">
                {(["buy", "sell"] as const).map((value) => (
                  <button
                    key={value}
                    type="button"
                    onClick={() => setSide(value)}
                    className={`rounded px-3 py-1.5 text-sm font-medium ${side === value ? "bg-slate-900 text-white dark:bg-slate-100 dark:text-slate-950" : "text-slate-500 dark:text-slate-400"}`}
                  >
                    {value === "buy" ? "Buy" : "Sell"}
                  </button>
                ))}
              </div>
            </div>
            <label className="min-w-[140px] flex-1 text-xs font-medium text-slate-500 dark:text-slate-400">
              Quantity
              <input
                value={quantity}
                onChange={(event) => setQuantity(event.target.value)}
                type="number"
                min="0"
                step="any"
                placeholder="0.00"
                className="mt-2 block w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus:border-slate-500 focus:outline-none dark:border-slate-700 dark:bg-slate-900 dark:text-white"
              />
            </label>
            <button
              type="submit"
              disabled={submittingOrder || !ticker.trim() || !(Number(quantity) > 0)}
              className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-50 dark:bg-slate-100 dark:text-slate-950"
            >
              {submittingOrder ? "Submitting..." : "Submit paper order"}
            </button>
          </div>
          <label className="mt-3 block text-xs font-medium text-slate-500 dark:text-slate-400">
            Rationale
            <input
              value={rationale}
              onChange={(event) => setRationale(event.target.value)}
              placeholder="What this simulated order is testing"
              maxLength={1000}
              className="mt-2 block w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus:border-slate-500 focus:outline-none dark:border-slate-700 dark:bg-slate-900 dark:text-white"
            />
          </label>
          <p className="mt-3 text-xs text-slate-500 dark:text-slate-400">
            Market/day simulation only. The broker rejects untracked tickers, oversells, insufficient buying power, and buys above the configured per-order equity cap. Outside regular hours, valid orders wait without changing positions.
          </p>
        </form>
      ) : null}

      {account ? (
        <div className="mt-4 grid grid-cols-2 gap-x-6 gap-y-3 border-y border-slate-200 py-4 text-sm md:grid-cols-3 dark:border-slate-800">
          <Metric label="Equity" value={formatCurrency(account.equity)} />
          <Metric label="Cash" value={formatCurrency(account.cash)} />
          <Metric label="Reserved" value={formatCurrency(account.cash_reserved)} />
          <Metric label="Buying power" value={formatCurrency(account.buying_power)} />
          <Metric label="Positions value" value={formatCurrency(account.market_value)} />
          <Metric label="Paper positions" value={String(account.position_count ?? 0)} />
        </div>
      ) : null}

      {paperPositions.length ? (
        <div className="mt-4 overflow-x-auto">
          <table className="w-full min-w-[720px] border-collapse text-left text-sm">
            <thead className="border-b border-slate-200 text-xs text-slate-500 dark:border-slate-800 dark:text-slate-400">
              <tr>
                <th className="py-2 pr-4 font-medium">Position</th>
                <th className="px-4 py-2 text-right font-medium">Quantity</th>
                <th className="px-4 py-2 text-right font-medium">Average cost</th>
                <th className="px-4 py-2 text-right font-medium">Last mark</th>
                <th className="px-4 py-2 text-right font-medium">Market value</th>
                <th className="py-2 pl-4 text-right font-medium">Unrealized P&amp;L</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-200 dark:divide-slate-800">
              {paperPositions.map(position => (
                <tr key={position.security_id}>
                  <td className="py-3 pr-4">
                    <p className="font-medium text-slate-900 dark:text-slate-100">{position.ticker}</p>
                    <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                      {position.weight_pct.toFixed(2)}% of paper equity
                      {position.marked_at ? ` · marked ${formatDate(position.marked_at)}` : ""}
                    </p>
                  </td>
                  <td className="px-4 py-3 text-right tabular-nums">{position.quantity.toFixed(4)}</td>
                  <td className="px-4 py-3 text-right tabular-nums">{formatCurrency(position.avg_cost_basis)}</td>
                  <td className="px-4 py-3 text-right tabular-nums">{formatCurrency(position.current_price)}</td>
                  <td className="px-4 py-3 text-right tabular-nums">{formatCurrency(position.market_value)}</td>
                  <td className={`py-3 pl-4 text-right tabular-nums ${position.unrealized_pnl >= 0 ? "text-emerald-700 dark:text-emerald-300" : "text-red-700 dark:text-red-300"}`}>
                    {formatCurrency(position.unrealized_pnl)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : isManual ? (
        <p className="mt-4 text-sm text-slate-500 dark:text-slate-400">This paper account does not hold any positions yet.</p>
      ) : null}

      {accountEvents.length ? (
        <div className="mt-5 border-t border-slate-200 pt-4 dark:border-slate-800">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <p className="text-xs font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-300">
              Account events
            </p>
            <p className="text-xs text-slate-500 dark:text-slate-400">
              Settled portfolio events applied once to this paper account
            </p>
          </div>
          <div className="mt-2 divide-y divide-slate-200 dark:divide-slate-800">
            {accountEvents.map(event => (
              <div key={event.id} className="grid gap-2 py-3 text-sm md:grid-cols-[minmax(0,1fr)_auto] md:gap-6">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-semibold text-slate-900 dark:text-slate-100">{event.ticker}</span>
                    <span className="capitalize text-slate-700 dark:text-slate-200">{event.event_type}</span>
                    <AccountEventStatus status={event.status} />
                  </div>
                  <p className="mt-1 text-slate-600 dark:text-slate-300">{event.detail}</p>
                  <p
                    className="mt-1 text-xs text-slate-500 dark:text-slate-400"
                    title="Derivation records whether the amount came directly from broker provenance or was reconstructed from the settled transaction ledger."
                  >
                    {event.derivation.replaceAll("_", " ")}
                  </p>
                </div>
                <div className="text-left text-xs tabular-nums text-slate-500 md:text-right dark:text-slate-400">
                  <p>{formatDate(event.occurred_at)}</p>
                  {event.event_type === "split" ? (
                    <p className="mt-1">{event.quantity_before.toFixed(4)} → {event.quantity_after.toFixed(4)} shares</p>
                  ) : (
                    <p className="mt-1">cash {formatCurrency(event.cash_before)} → {formatCurrency(event.cash_after)}</p>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      ) : null}

      {orders.length ? (
        <div className="mt-3 divide-y divide-slate-200 dark:divide-slate-800">
          {orders.map(order => {
            const fill = fills.find(item => item.order_id === order.id);
            const intent = order.source_decision_json ?? {};
            const targetWeight = typeof intent.model_target_weight_pct === "number" ? intent.model_target_weight_pct : null;
            const adjustments = Array.isArray(intent.size_adjustments) ? intent.size_adjustments.filter(item => typeof item === "string") as string[] : [];
            return (
              <div key={order.id} className="grid gap-2 py-4 text-sm md:grid-cols-[minmax(0,1fr)_auto] md:gap-6">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-semibold text-slate-900 dark:text-slate-100">{order.ticker}</span>
                    <span>{order.side.toUpperCase()} {order.requested_quantity.toFixed(4)}</span>
                    <OrderStatus status={order.status} reason={order.rejection_reason} />
                    {order.status === "accepted" ? (
                      <button
                        type="button"
                        onClick={() => void onCancel(experiment.id, order.id)}
                        disabled={cancelingOrderId === order.id}
                        className="text-xs font-medium text-slate-600 underline-offset-4 hover:underline disabled:opacity-50 dark:text-slate-300"
                      >
                        {cancelingOrderId === order.id ? "Canceling..." : "Cancel"}
                      </button>
                    ) : null}
                  </div>
                  <p className="mt-1 text-slate-500 dark:text-slate-400">
                    submitted {formatDate(order.submitted_at)} · {order.order_type} / {order.time_in_force}
                    {targetWeight == null ? "" : ` · target ${targetWeight.toFixed(2)}%`}
                  </p>
                  {fill ? (
                    <p className="mt-1 text-slate-700 dark:text-slate-200">
                      filled {fill.quantity.toFixed(4)} @ {formatCurrency(fill.price)} · notional {formatCurrency(fill.gross_notional)} · slippage {fill.slippage_bps.toFixed(1)} bps
                    </p>
                  ) : null}
                  {order.rejection_reason ? <p className="mt-1 text-red-600 dark:text-red-300">{order.rejection_reason.replaceAll("_", " ")}</p> : null}
                  {adjustments.map((adjustment, index) => <p key={`${order.id}-adjustment-${index}`} className="mt-1 text-amber-700 dark:text-amber-300">{adjustment}</p>)}
                  <p className="mt-2 text-slate-600 dark:text-slate-300">{order.rationale}</p>
                </div>
                <div className="text-left text-xs text-slate-500 md:text-right dark:text-slate-400">
                  <p>reference {formatCurrency(order.reference_price)}</p>
                  <p className="mt-1">session {order.quote_session?.replaceAll("_", " ") ?? "unknown"}</p>
                  {order.evidence_refs_json.length ? <p className="mt-1">{order.evidence_refs_json.length} evidence ref{order.evidence_refs_json.length === 1 ? "" : "s"}</p> : null}
                </div>
              </div>
            );
          })}
        </div>
      ) : (
        <p className="mt-4 text-sm text-slate-500 dark:text-slate-400">No paper orders have been submitted for this run.</p>
      )}
    </section>
  );
}

function OrderStatus({ status, reason }: { status: string; reason?: string | null }) {
  const tone = status === "filled"
    ? "border-emerald-300 text-emerald-700 dark:border-emerald-800 dark:text-emerald-300"
    : status === "rejected"
      ? "border-red-300 text-red-700 dark:border-red-800 dark:text-red-300"
      : "border-amber-300 text-amber-700 dark:border-amber-800 dark:text-amber-300";
  return <span title={reason ?? undefined} className={`rounded border px-2 py-0.5 text-xs font-medium ${tone}`}>{status}</span>;
}

function AccountEventStatus({ status }: { status: string }) {
  const tone = status === "applied"
    ? "border-emerald-300 text-emerald-700 dark:border-emerald-800 dark:text-emerald-300"
    : status === "rejected"
      ? "border-red-300 text-red-700 dark:border-red-800 dark:text-red-300"
      : status === "needs_reconciliation"
        ? "border-amber-300 text-amber-700 dark:border-amber-800 dark:text-amber-300"
        : "border-slate-300 text-slate-600 dark:border-slate-700 dark:text-slate-300";
  return (
    <span className={`rounded border px-2 py-0.5 text-xs font-medium ${tone}`}>
      {status.replaceAll("_", " ")}
    </span>
  );
}

function OpportunitySummary({
  profile,
}: {
  profile?: ShadowExperiment["discovery_profile"] | null;
}) {
  if (!profile || !profile.investable_thesis) return null;
  const evidence = profile.evidence_to_check ?? [];
  const falsifiers = profile.falsification_tests ?? [];
  const controls = profile.risk_controls ?? [];
  const leading = profile.leading_indicators ?? [];
  const lagging = profile.lagging_confirmations ?? [];
  const uncertainties = profile.uncertainties ?? [];
  const evidenceSnapshot = profile.evidence_snapshot ?? [];
  const stage = profile.signal_stage?.replaceAll("_", " ");
  const pricedIn = profile.priced_in_assessment?.replaceAll("_", " ");
  return (
    <div className="mt-4 min-w-0 border-t-2 border-slate-300 pt-4 text-sm [overflow-wrap:anywhere] dark:border-slate-700">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-xs font-semibold text-slate-700 dark:text-slate-200">
          Opportunity thesis
        </p>
        <span className="max-w-full rounded border border-slate-300 px-2 py-1 text-xs text-slate-600 dark:border-slate-700 dark:text-slate-300">
          {[stage, pricedIn ? `${pricedIn} priced in` : null, profile.opportunity_type?.replaceAll("_", " "), profile.priority_score == null ? null : `priority ${(profile.priority_score * 100).toFixed(0)}%`]
            .filter(Boolean)
            .join(" · ")}
        </span>
      </div>
      <p className="mt-3 font-medium text-slate-800 dark:text-slate-100">{profile.investable_thesis}</p>
      {profile.why_now ? (
        <p className="mt-2 text-slate-700 dark:text-slate-200"><span className="font-medium">Why now:</span> {profile.why_now}</p>
      ) : null}
      {profile.portfolio_transmission ? (
        <p className="mt-2 text-slate-600 dark:text-slate-300">Transmission: {profile.portfolio_transmission}</p>
      ) : null}
      {profile.expected_edge ? (
        <p className="mt-2 text-slate-600 dark:text-slate-300">Expected edge: {profile.expected_edge}</p>
      ) : null}
      <div className="mt-3 grid gap-3 lg:grid-cols-3">
        {leading.length ? <CompactList title="Leading evidence" items={leading} /> : null}
        {lagging.length ? <CompactList title="Still unconfirmed" items={lagging} /> : null}
        {uncertainties.length ? <CompactList title="Uncertainty" items={uncertainties} /> : null}
        {evidence.length ? <CompactList title="Evidence to check" items={evidence} /> : null}
        {falsifiers.length ? <CompactList title="Falsifies if" items={falsifiers} /> : null}
        {controls.length ? <CompactList title="Risk controls" items={controls} /> : null}
      </div>
      {evidenceSnapshot.length ? (
        <details className="mt-4 border-t border-slate-200 pt-3 dark:border-slate-800">
          <summary className="cursor-pointer text-xs font-medium text-slate-600 dark:text-slate-300">
            Point-in-time evidence ({evidenceSnapshot.length})
            {profile.captured_at ? ` · captured ${formatDate(profile.captured_at)}` : ""}
          </summary>
          <div className="mt-3 space-y-2 text-xs text-slate-500 dark:text-slate-400">
            {evidenceSnapshot.map((item) => (
              <div key={item.ref} className="min-w-0 border-l-2 border-slate-200 pl-3 dark:border-slate-700">
                <p className="font-medium text-slate-700 dark:text-slate-200">
                  {[item.ticker, item.kind?.replaceAll("_", " "), item.source].filter(Boolean).join(" · ") || item.ref}
                </p>
                <p className="mt-1 break-all">
                  {typeof item.summary === "string" ? item.summary : item.summary ? JSON.stringify(item.summary) : item.ref}
                </p>
                <p className="mt-1">
                  {item.as_of ? formatDate(item.as_of) : "Date not supplied"} · {item.ref}
                  {item.url ? (
                    <> · <a href={item.url} target="_blank" rel="noreferrer" className="text-sky-600 hover:underline dark:text-sky-300">source</a></>
                  ) : null}
                </p>
              </div>
            ))}
          </div>
        </details>
      ) : null}
    </div>
  );
}

function CompactList({ title, items }: { title: string; items: string[] }) {
  return (
    <div className="min-w-0 border-l-2 border-slate-200 pl-3 [overflow-wrap:anywhere] dark:border-slate-700">
      <p className="text-xs font-medium text-slate-500 dark:text-slate-400">{title}</p>
      <ul className="mt-2 space-y-1 text-slate-600 dark:text-slate-300">
        {items.slice(0, 4).map((item) => (
          <li key={item}>• {item}</li>
        ))}
      </ul>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="border-l-2 border-slate-200 py-1 pl-3 dark:border-slate-700">
      <p className="text-xs text-slate-500 dark:text-slate-400">{label}</p>
      <p className="mt-1 text-xl font-semibold">{value}</p>
    </div>
  );
}

function InfoCard({
  label,
  body,
  detail,
  hint,
}: {
  label: string;
  body: string;
  detail: string;
  hint?: string;
}) {
  return (
    <div className="min-w-0 border-l-2 border-slate-200 py-1 pl-3 text-sm [overflow-wrap:anywhere] dark:border-slate-700">
      <div className="flex items-start justify-between gap-3">
        <p className="text-xs font-medium text-slate-500 dark:text-slate-400">{label}</p>

      </div>
      <p className="mt-2 font-medium text-slate-700 dark:text-slate-200">{body}</p>
      <p className="mt-2 text-slate-500 dark:text-slate-400">{detail}</p>
      {hint ? <p className="mt-3 text-xs text-slate-400 dark:text-slate-500">{hint}</p> : null}
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  const normalized = status === "pending" ? "queued" : status;
  const className =
    normalized === "completed"
      ? "bg-emerald-50 text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300"
      : normalized === "running"
      ? "bg-sky-50 text-sky-700 dark:bg-sky-950/40 dark:text-sky-300"
      : normalized === "queued"
      ? "bg-sky-50 text-sky-700 dark:bg-sky-950/40 dark:text-sky-300"
      : normalized === "failed"
      ? "bg-rose-50 text-rose-700 dark:bg-rose-950/40 dark:text-rose-300"
      : normalized === "skipped"
      ? "bg-amber-50 text-amber-700 dark:bg-amber-950/40 dark:text-amber-300"
      : "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300";
  return <span className={`rounded px-2 py-1 text-xs font-medium ${className}`}>{normalized}</span>;
}

function describeStatus(status: string) {
  const normalized = status === "pending" ? "queued" : status;
  switch (normalized) {
    case "queued":
      return "queued for execution";
    case "running":
      return "actively evaluating the cloned portfolio";
    case "completed":
      return "completed and available for review";
    case "failed":
      return "failed before completion";
    case "skipped":
      return "skipped because the run could not proceed";
    default:
      return normalized;
  }
}

function ShadowComparisonChart({ experiment }: { experiment: ShadowExperiment }) {
  if (!experiment.result) return null;

  const shadow = experiment.result.shadow_return;
  const actual = experiment.result.actual_return;
  const maxAbs = Math.max(Math.abs(shadow), Math.abs(actual), Math.abs(experiment.result.alpha), 0.01);

  return (
    <div className="rounded-lg border border-slate-200 p-4 dark:border-slate-800">
      <div className="flex items-start justify-between gap-3">
        <p className="text-xs uppercase tracking-wider text-slate-400">Shadow vs real portfolio</p>
      </div>
      <div className="mt-4 grid gap-4 md:grid-cols-3">
        <ComparisonBar label="Shadow" value={shadow} maxAbs={maxAbs} tone="sky" />
        <ComparisonBar label="Actual" value={actual} maxAbs={maxAbs} tone="slate" />
        <ComparisonBar label="Alpha" value={experiment.result.alpha} maxAbs={maxAbs} tone={experiment.result.alpha >= 0 ? "emerald" : "rose"} />
      </div>
    </div>
  );
}

function ComparisonBar({
  label,
  value,
  maxAbs,
  tone,
}: {
  label: string;
  value: number;
  maxAbs: number;
  tone: "sky" | "slate" | "emerald" | "rose";
}) {
  const pct = `${Math.min(100, Math.max(8, (Math.abs(value) / maxAbs) * 100))}%`;
  const toneClass =
    tone === "sky"
      ? "bg-sky-500"
      : tone === "slate"
      ? "bg-slate-500"
      : tone === "emerald"
      ? "bg-emerald-500"
      : "bg-rose-500";
  return (
    <div className="rounded-lg border border-slate-200 p-3 dark:border-slate-800">
      <p className="text-xs uppercase tracking-wider text-slate-400">{label}</p>
      <p className="mt-2 text-lg font-semibold">{formatPct(value)}</p>
      <div className="mt-3 h-2 rounded-full bg-slate-200 dark:bg-slate-800">
        <div className={`h-2 rounded-full ${toneClass}`} style={{ width: pct }} />
      </div>
    </div>
  );
}

function formatCurrency(value?: number | null) {
  if (value == null) return "n/a";
  return `$${value.toFixed(2)}`;
}

function formatPct(value?: number | null) {
  if (value == null) return "n/a";
  const displayed = Number((value * 100).toFixed(2));
  return `${displayed.toFixed(2)}%`;
}

function formatDate(value: string) {
  return new Date(value).toLocaleString();
}
