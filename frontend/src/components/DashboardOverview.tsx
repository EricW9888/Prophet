"use client";

import Link from "next/link";
import { type ReactNode, useEffect, useState } from "react";
import { ShieldCheck } from "lucide-react";
import {
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Area,
  AreaChart,
  XAxis,
  YAxis,
} from "recharts";

import { apiFetch, DashboardSummary } from "@/lib/api";
import { automationJobHealth, type AutomationHealth } from "@/lib/automation";
import {
  safeFormatCurrency,
  safeFormatSignedCurrency,
  safeFormatPct,
  formatUserLabel
} from "@/lib/formatting";

function HeroMetric({ label, value, detail, href }: { label: string; value: string; detail: string; href?: string }) {
  const content = (
    <div className="cursor-default border-t-2 border-line bg-panel px-1 py-4 transition-colors hover:border-line-strong">
      <p className="text-xs font-medium text-slate-500 dark:text-slate-400">{label}</p>
      <p className="mt-1.5 text-2xl font-semibold text-slate-900 dark:text-slate-100">{value}</p>
      <p className="mt-1 text-xs text-slate-500 dark:text-slate-400 line-clamp-1">{detail}</p>
    </div>
  );

  return href ? (
    <Link href={href} className="block group">
       {content}
    </Link>
  ) : content;
}

function Panel({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <div
      className={`rounded-lg border border-line bg-panel p-4 md:p-5 ${className}`}
    >
      {children}
    </div>
  );
}

function SectionTitle({
  title,
  subtitle,
  action,
}: {
  title: string;
  subtitle?: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex items-center justify-between mb-4">
      <div>
        {subtitle && (
          <div className="mb-1 text-xs font-medium text-slate-500 dark:text-slate-400">
            {subtitle}
          </div>
        )}
        <h2 className="text-lg font-semibold leading-tight text-slate-900 dark:text-slate-100">
          {title}
        </h2>
      </div>
      {action && <div>{action}</div>}
    </div>
  );
}

export default function DashboardOverview() {
  const [summary, setSummary] = useState<DashboardSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showAllQueueItems, setShowAllQueueItems] = useState(false);
  const [expandedPanels, setExpandedPanels] = useState({
    performance: false,
    trajectory: false,
    infrastructure: false,
  });

  async function loadSummary() {
    try {
      const data = await apiFetch<DashboardSummary>("/dashboard/summary");
      setSummary(data);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to load dashboard.");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void loadSummary();
    const interval = window.setInterval(() => {
      void loadSummary();
    }, 15000);
    return () => window.clearInterval(interval);
  }, []);

  if (loading) {
    return (
      <div className="animate-pulse space-y-6">
        <div className="h-24 rounded-lg bg-slate-100 dark:bg-slate-900" />
        <div className="grid grid-cols-3 gap-6">
           <div className="h-96 rounded-lg bg-slate-100 dark:bg-slate-900" />
           <div className="h-96 rounded-lg bg-slate-100 dark:bg-slate-900" />
           <div className="h-96 rounded-lg bg-slate-100 dark:bg-slate-900" />
        </div>
      </div>
    );
  }

  if (error || !summary) {
    return (
      <Panel className="border-red-100 bg-red-50/10">
        <p className="text-sm text-red-500 font-medium">{error ?? "Dashboard summary unavailable."}</p>
      </Panel>
    );
  }

  const automationHealthy =
    summary.automation_enabled && summary.jobs.every((job) => job.last_status !== "error");

  const reviewItems = summary?.review_queue ?? [];
  const visibleReviewItems = showAllQueueItems ? reviewItems.slice(0, 8) : reviewItems.slice(0, 3);
  const latestResearchTitle =
    cleanResearchTitle(summary.research_activity.latest_item_title) ||
    cleanResearchTitle(summary.portfolio_monitor?.recent_research_items?.[0]?.title) ||
    "No recent research headline yet";
  const focusValue = summary.research_activity.latest_item_subject_name || topScannerLabel(summary) || "Portfolio";
  const focusDetail = latestResearchTitle;
  const scannerItems = reviewItems.slice(0, 5);

  return (
    <div className="space-y-6">
      {/* Hero Stats */}
      <section className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <HeroMetric
            label="Total Equity"
            value={safeFormatCurrency(summary?.total_value, 2)}
            detail={`${summary?.holdings_count ?? 0} active holdings`}
          />
          <HeroMetric
            label="Buying Power"
            value={safeFormatCurrency(summary?.buying_power, 2)}
            detail="Available to deploy"
          />
          <HeroMetric
            label="Profiles"
            value={`${summary?.profile_count ?? 0}`}
            detail={`${summary?.open_questions_count ?? 0} questions · ${summary?.active_evidence_node_count ?? summary?.evidence_node_count ?? 0} active nodes${
              summary?.deprecated_evidence_node_count ? ` · ${summary.deprecated_evidence_node_count} archived` : ""
            }`}
          />
          <HeroMetric
            label="Attention"
            value={`${summary?.review_queue?.length ?? 0}`}
            detail={summary?.review_queue?.[0]?.item_label ?? "All clear"}
            href="/verification"
          />
      </section>

      <TransactionCapturePanel transactions={summary.recent_transactions ?? []} jobs={summary.jobs} />

      <div className="grid grid-cols-1 xl:grid-cols-[1.15fr_0.85fr] gap-4 md:gap-6 items-start">
        <Panel className="xl:col-span-1">
          <SectionTitle
            title="What Needs Attention"
            subtitle="Review Queue"
            action={
              <Link
                href="/verification"
                className="text-xs font-medium text-slate-600 hover:text-slate-950 dark:text-slate-300 dark:hover:text-white"
              >
                View All
              </Link>
            }
          />

          <div className="space-y-3">
            {reviewItems.length === 0 ? (
              <div className="py-8 text-center text-sm italic text-slate-400 dark:text-slate-500">
                No urgent review items right now.
              </div>
            ) : (
              visibleReviewItems.map((item) => (
                <div
                  key={item.id}
                  className="group relative border-b border-slate-200 py-3 last:border-b-0 dark:border-slate-800"
                >
                  <div className="flex items-start justify-between mb-1.5">
                    <div className="text-sm font-bold leading-tight text-slate-900 dark:text-slate-100">
                      {item.item_label}
                    </div>
                    <div className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium text-slate-600 dark:bg-slate-900 dark:text-slate-300">
                      {formatUserLabel(item.item_type)}
                    </div>
                  </div>
                  <div className="mb-2 text-[11px] leading-relaxed text-slate-600 line-clamp-2 dark:text-slate-300">
                    {item.why_now_summary || item.trigger_reason}
                  </div>
                  <div className="mb-2 border-l-2 border-slate-200 pl-2.5 text-[11px] text-slate-500 line-clamp-2 dark:border-slate-700 dark:text-slate-400">
                    Next: {item.next_action || "Review the latest evidence and decide whether to escalate."}
                  </div>
                  <div className="flex flex-wrap gap-1.5">
                    <span className="rounded border border-slate-100 bg-white px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-tighter text-slate-400 dark:border-slate-800 dark:bg-slate-950 dark:text-slate-500">
                      {item.item_type === "position" ? "holding" : "logic"}
                    </span>
                    {item.signal_tags?.slice(0, 2).map((tag) => (
                      <span
                        key={`${item.id}-${tag}`}
                        className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium text-slate-600 dark:bg-slate-900 dark:text-slate-300"
                      >
                        {tag}
                      </span>
                    ))}
                    {item.priority_score > 60 && (
                      <span className="text-[9px] font-bold text-rose-500 uppercase tracking-tighter bg-rose-50 px-1.5 py-0.5 rounded">
                        high priority
                      </span>
                    )}
                  </div>
                </div>
              ))
            )}
            {reviewItems.length > 3 ? (
              <button
                type="button"
                onClick={() => setShowAllQueueItems((current) => !current)}
                className="pt-1 text-xs font-medium text-slate-600 hover:text-slate-950 dark:text-slate-300 dark:hover:text-white"
              >
                {showAllQueueItems ? "Show less" : `Show ${Math.min(reviewItems.length - 3, 5)} more`}
              </button>
            ) : null}
          </div>
        </Panel>

        <div className="space-y-4">
          <Panel className="xl:col-span-1">
          <SectionTitle
            title="Agent Desk"
            subtitle="Live workflow"
            action={
              <button
                type="button"
                onClick={() => void loadSummary()}
                className="rounded border border-slate-300 bg-white px-2 py-1 text-xs font-medium text-slate-600 transition-colors hover:border-slate-500 hover:text-slate-950 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-300"
              >
                Refresh desk
              </button>
            }
          />
          <div className="space-y-4">
            <div className="border-b border-slate-200 pb-4 dark:border-slate-800">
              <div className="flex items-center justify-between gap-3">
                <div className="text-xs font-medium text-slate-500">
                  Agent activity
                </div>
                <Link href="/activity" className="text-xs font-medium text-slate-600 hover:text-slate-950 dark:text-slate-300">
                  Catch up
                </Link>
              </div>
              <div className="mt-3 space-y-3">
                {summary.recent_agent_actions.length === 0 ? (
                  <ActivityRow
                    label="System heartbeat"
                    value={automationHealthy ? "Running" : "Needs review"}
                    detail="Automation is enabled, but there are no recent action records yet."
                  />
                ) : (
                  summary.recent_agent_actions.slice(0, 5).map((action) => (
                    <ActionFeedRow
                      key={action.id}
                      timestamp={action.timestamp}
                      source={action.source}
                      actionType={action.action_type}
                      status={action.status}
                      subjectName={action.subject_name}
                      summary={action.summary}
                      metadata={action.metadata}
                    />
                  ))
                )}
              </div>
              <div className="mt-4 flex flex-wrap gap-2">
                <Link href="/activity" className="rounded border border-slate-200 px-3 py-1.5 text-xs font-medium text-slate-600 hover:border-slate-400">
                  Agent log
                </Link>
                <Link href="/timeline" className="rounded border border-slate-200 px-3 py-1.5 text-xs font-medium text-slate-600 hover:border-slate-400">
                  Research feed
                </Link>
                <Link href="/graph" className="rounded border border-slate-200 px-3 py-1.5 text-xs font-medium text-slate-600 hover:border-slate-400">
                  Knowledge
                </Link>
                <Link href="/shadow" className="rounded border border-slate-200 px-3 py-1.5 text-xs font-medium text-slate-600 hover:border-slate-400">
                  Shadow lab
                </Link>
                <Link href="/verification" className="rounded border border-slate-200 px-3 py-1.5 text-xs font-medium text-slate-600 hover:border-slate-400">
                  Review queue
                </Link>
              </div>
            </div>

            <div className="pt-1">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <div className="text-xs font-medium text-slate-500">
                    Priority scanner
                  </div>
                  <div className="mt-1 text-xs text-slate-500">
                    Compact ranked view of what matters now.
                  </div>
                </div>
                <Link href="/verification" className="text-xs font-medium text-slate-600 hover:text-slate-950 dark:text-slate-300">
                  Open queue
                </Link>
              </div>
              <div className="mt-4 space-y-2">
                {scannerItems.length === 0 ? (
                  <div className="rounded-lg border border-dashed border-slate-200 px-3 py-4 text-sm italic text-slate-400 dark:border-slate-800 dark:text-slate-500">
                    No active scanner items right now.
                  </div>
                ) : (
                  scannerItems.map((item, index) => (
                    <ScannerRow
                      key={item.id}
                      rank={index + 1}
                      label={item.item_label}
                      score={item.priority_score}
                      detail={item.why_now_summary || item.trigger_reason}
                      tags={item.signal_tags ?? []}
                    />
                  ))
                )}
              </div>
            </div>

            <div className="grid grid-cols-3 gap-3">
              <CompactSnapshot
                label="Focus"
                value={focusValue}
                detail={focusDetail}
              />
              <CompactSnapshot
                label="Research"
                value={`${summary?.research_activity?.open_question_count ?? 0}`}
                detail="open items"
              />
              <CompactSnapshot
                label="Heartbeat"
                value={automationHealthy ? "Healthy" : "Needs review"}
                detail={marketDataStatus(summary)}
              />
            </div>
          </div>
          </Panel>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-4 md:gap-6 items-start">
        <DisclosurePanel
          eyebrow="Returns and risk"
          title="Performance"
          summary={`Open holdings are ${summary?.portfolio_return_pct == null ? "unmeasured" : safeFormatPct(summary.portfolio_return_pct)} versus their current cost basis. Use Risk for cash-flow-aligned attribution.`}
          open={expandedPanels.performance}
          onToggle={() => setExpandedPanels((current) => ({ ...current, performance: !current.performance }))}
        >
          <SectionTitle
            title="Performance"
            subtitle="Returns and risk"
            action={
              <Link href="/risk" className="text-[11px] font-bold uppercase tracking-widest text-sky-600 hover:underline">
                Full report
              </Link>
            }
          />
          <div className="space-y-4 text-sm text-slate-600">
            <div className="rounded-lg border border-slate-100 p-4 bg-slate-50/30 font-bold">
              <p className="text-[10px] uppercase font-bold tracking-[0.2em] text-slate-400">Net Exposure (Market)</p>
              <div className="flex items-baseline gap-3 mt-1.5">
                <p className="text-2xl font-bold tracking-tighter text-slate-900">
                  {safeFormatCurrency(summary?.total_market_value ?? 0)}
                </p>
                <p className={`text-[10px] font-bold ${(summary?.total_unrealized_pnl ?? 0) >= 0 ? "text-emerald-500" : "text-rose-500"}`}>
                  {safeFormatSignedCurrency(summary?.total_unrealized_pnl ?? 0)} pnl
                </p>
              </div>
            </div>
            <div className="rounded-lg border border-slate-100 p-4">
              <p className="text-[10px] uppercase font-bold tracking-[0.2em] text-slate-400 mb-3">Reference frame</p>
              <div className="grid grid-cols-2 gap-2 mt-1">
                <div className="flex justify-between items-center bg-slate-50 p-2 rounded-lg">
                   <span className="text-[10px] text-slate-500 font-bold uppercase">Open cost basis</span>
                   <span className="font-bold text-slate-900 text-xs">{summary?.portfolio_return_pct == null ? "n/a" : safeFormatPct(summary.portfolio_return_pct)}</span>
                </div>
                <div className="flex justify-between items-center bg-slate-50 p-2 rounded-lg">
                   <span className="text-[10px] text-slate-500 font-bold uppercase">Index since first entry</span>
                   <span className="font-bold text-slate-900 text-xs">{summary?.benchmark_return_pct == null ? "n/a" : safeFormatPct(summary.benchmark_return_pct)}</span>
                </div>
              </div>
              <p className="mt-2 text-[10px] leading-4 text-slate-400">These two reference figures are not cash-flow aligned and should not be subtracted. The Risk report calculates a dated Modified Dietz comparison.</p>
            </div>
            <div className="grid gap-4 lg:grid-cols-2">
              <div className="rounded-lg border border-emerald-100 bg-emerald-50/40 p-4">
                <p className="text-[10px] font-bold text-emerald-600 uppercase tracking-widest mb-2">Top contributors</p>
                <div className="space-y-2">
                  {!summary?.top_winners || summary.top_winners.length === 0 ? (
                    <p className="text-xs text-slate-400 italic">No contributors yet.</p>
                  ) : (
                    summary.top_winners.slice(0, 3).map((pos) => (
                      <div key={pos.id} className="flex items-center justify-between gap-3 rounded-lg bg-white/80 px-3 py-2">
                        <div className="min-w-0">
                          <p className="truncate text-sm font-bold text-slate-900">{pos.ticker}</p>
                          <p className="truncate text-[10px] uppercase text-slate-500">{pos.entity_name}</p>
                        </div>
                        <p className="text-xs font-black text-emerald-600">
                          {safeFormatSignedCurrency((pos.unrealized_pnl || 0) + (pos.realized_pnl || 0))}
                        </p>
                      </div>
                    ))
                  )}
                </div>
              </div>
              <div className="rounded-lg border border-rose-100 bg-rose-50/40 p-4">
                <p className="text-[10px] font-bold text-rose-600 uppercase tracking-widest mb-2">Top detractors</p>
                <div className="space-y-2">
                  {!summary?.top_losers || summary.top_losers.length === 0 ? (
                    <p className="text-xs text-slate-400 italic">No detractors yet.</p>
                  ) : (
                    summary.top_losers.slice(0, 3).map((pos) => (
                      <div key={pos.id} className="flex items-center justify-between gap-3 rounded-lg bg-white/80 px-3 py-2">
                        <div className="min-w-0">
                          <p className="truncate text-sm font-bold text-slate-900">{pos.ticker}</p>
                          <p className="truncate text-[10px] uppercase text-slate-500">{pos.entity_name}</p>
                        </div>
                        <p className="text-xs font-black text-rose-600">
                          {safeFormatSignedCurrency((pos.unrealized_pnl || 0) + (pos.realized_pnl || 0))}
                        </p>
                      </div>
                    ))
                  )}
                </div>
              </div>
            </div>
          </div>
        </DisclosurePanel>

        <DisclosurePanel
          eyebrow="Capital deployment"
          title="Trajectory"
          summary={summary?.portfolio_build_series?.length ? `${summary.portfolio_build_series[summary.portfolio_build_series.length - 1]?.transaction_count ?? 0} transactions across the current build.` : "No transaction history yet."}
          open={expandedPanels.trajectory}
          onToggle={() => setExpandedPanels((current) => ({ ...current, trajectory: !current.trajectory }))}
        >
          <div className="space-y-4">
            <div className="rounded-lg border border-slate-100 bg-slate-50/40 p-4">
              <div className="text-[10px] font-bold uppercase tracking-[0.2em] text-slate-400">
                Capital deployment history
              </div>
              <p className="mt-2 text-sm text-slate-600">
                This tracks cumulative capital deployed across the real transaction history, not a separate shadow view.
              </p>
            </div>
            <PortfolioBuildChart points={summary?.portfolio_build_series ?? []} />
          </div>
        </DisclosurePanel>

        <DisclosurePanel
          eyebrow={summary?.automation_enabled ? "Running" : "Paused"}
          title="Infrastructure"
          summary={`${summary?.profile_count ?? 0} profiles, ${summary?.recent_lessons?.length ?? 0} lessons, ${automationHealthy ? "automation healthy" : "automation needs review"}.`}
          open={expandedPanels.infrastructure}
          onToggle={() => setExpandedPanels((current) => ({ ...current, infrastructure: !current.infrastructure }))}
        >
          <SectionTitle
            title="Infrastructure"
            subtitle={summary?.automation_enabled ? "Running" : "Paused"}
            action={<Link href="/settings" className="text-[11px] font-bold text-sky-600 uppercase tracking-widest hover:underline">Settings</Link>}
          />
          <div className="space-y-3">
            <div className="rounded-lg border border-slate-100 p-3 bg-slate-50/50">
              <div className="text-[10px] font-bold text-slate-400 uppercase tracking-widest mb-2 flex items-center gap-2">
                <ShieldCheck className="w-3 h-3 text-emerald-500" /> System Persistent
              </div>
              <div className="text-sm font-bold text-slate-900 leading-snug">
                Data durable in project storage volumes.
              </div>
            </div>
            <div className="grid grid-cols-2 gap-3">
               <div className="rounded-lg border border-slate-100 p-3 bg-white">
                  <p className="text-[10px] uppercase font-bold text-slate-400 tracking-widest mb-1">Profiles</p>
                  <p className="text-xl font-bold text-slate-900">{summary?.profile_count ?? 0}</p>
               </div>
               <div className="rounded-lg border border-slate-100 p-3 bg-white">
                  <p className="text-[10px] uppercase font-bold text-slate-400 tracking-widest mb-1">Lessons</p>
                  <p className="text-xl font-bold text-slate-900">{summary?.recent_lessons?.length ?? 0}</p>
               </div>
            </div>
          </div>
        </DisclosurePanel>
      </div>
    </div>
  );
}

function TransactionCapturePanel({
  transactions,
  jobs,
}: {
  transactions: DashboardSummary["recent_transactions"];
  jobs: DashboardSummary["jobs"];
}) {
  const visibleTransactions = transactions.slice(0, 6);
  const gmailHealth = automationJobHealth(jobs, "gmail_sync", "Gmail sync");
  return (
    <Panel>
      <SectionTitle
        title="Trade Capture"
        subtitle="Latest portfolio truth"
        action={
          <Link
            href="/history"
            className="text-xs font-medium text-slate-600 hover:text-slate-950 dark:text-slate-300"
          >
            Full ledger
          </Link>
        }
      />
      <div className="mb-3">
        <AutomationHealthPill health={gmailHealth} />
      </div>
      {visibleTransactions.length === 0 ? (
        <div className="rounded-lg border border-dashed border-slate-200 px-3 py-4 text-sm italic text-slate-400 dark:border-slate-800 dark:text-slate-500">
          No recent transactions found in the portfolio ledger.
        </div>
      ) : (
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {visibleTransactions.map((txn) => (
            <div
              key={txn.id}
              className="rounded-md border border-slate-200 bg-slate-50/50 p-3 dark:border-slate-800 dark:bg-slate-900/50"
            >
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-black uppercase text-slate-900 dark:text-slate-100">
                      {txn.ticker}
                    </span>
                    <span className="rounded bg-slate-200 px-1.5 py-0.5 text-[10px] font-medium text-slate-700 dark:bg-slate-800 dark:text-slate-200">
                      {txn.action}
                    </span>
                  </div>
                  <p className="mt-1 truncate text-[11px] text-slate-500 dark:text-slate-400">
                    {txn.entity_name || "Portfolio transaction"}
                  </p>
                </div>
                <div className="shrink-0 text-right text-[10px] font-bold uppercase tracking-wider text-slate-400">
                  {formatShortDateTime(txn.executed_at)}
                </div>
              </div>
              <div className="mt-3 flex items-end justify-between gap-3">
                <div>
                  <p className="text-[10px] font-bold uppercase tracking-widest text-slate-400">
                    Quantity
                  </p>
                  <p className="text-sm font-bold text-slate-900 dark:text-slate-100">
                    {formatQuantity(txn.quantity)} @ {safeFormatCurrency(txn.price, 2)}
                  </p>
                </div>
                <div className="min-w-0 text-right">
                  <p className="text-[10px] font-bold uppercase tracking-widest text-slate-400">
                    Source
                  </p>
                  <p className="truncate text-xs font-bold text-slate-700 dark:text-slate-300">
                    {txn.source_label || "Manual/API"}
                    {txn.source_confidence != null ? ` · ${Math.round(txn.source_confidence * 100)}%` : ""}
                  </p>
                </div>
              </div>
              {txn.source_evidence_id ? (
                <Link
                  href={`/sources/evidence/${txn.source_evidence_id}`}
                  className="mt-3 block rounded border border-slate-200 bg-white/80 px-2.5 py-2 text-xs font-medium text-slate-500 hover:border-slate-400 hover:text-slate-900 dark:border-slate-800 dark:bg-slate-950/80 dark:text-slate-400"
                >
                  Evidence receipt {txn.source_evidence_id.slice(0, 8)}
                </Link>
              ) : null}
            </div>
          ))}
        </div>
      )}
    </Panel>
  );
}

function AutomationHealthPill({ health }: { health: AutomationHealth }) {
  const toneClass =
    health.tone === "ok"
      ? "border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-900 dark:bg-emerald-950/30 dark:text-emerald-300"
      : health.tone === "warn"
        ? "border-rose-200 bg-rose-50 text-rose-700 dark:border-rose-900 dark:bg-rose-950/30 dark:text-rose-300"
        : "border-slate-200 bg-slate-50 text-slate-500 dark:border-slate-800 dark:bg-slate-950 dark:text-slate-400";
  return (
    <span className={`inline-flex rounded border px-2.5 py-1 text-xs font-medium ${toneClass}`}>
      {health.label}: {health.detail}
    </span>
  );
}

function DisclosurePanel({
  eyebrow,
  title,
  summary,
  open,
  onToggle,
  children,
}: {
  eyebrow: string;
  title: string;
  summary: string;
  open: boolean;
  onToggle: () => void;
  children: ReactNode;
}) {
  return (
    <Panel className="xl:col-span-1">
      <button type="button" onClick={onToggle} className="flex w-full items-start justify-between gap-4 text-left">
        <div>
          <div className="mb-1 text-xs font-medium text-slate-500 dark:text-slate-400">{eyebrow}</div>
          <h2 className="text-lg font-semibold leading-tight text-slate-900 dark:text-slate-100">{title}</h2>
          <p className="mt-2 text-sm text-slate-500 dark:text-slate-400">{summary}</p>
        </div>
        <span className="rounded border border-slate-200 px-3 py-1.5 text-xs font-medium text-slate-500 dark:border-slate-700 dark:text-slate-400">
          {open ? "Hide" : "Expand"}
        </span>
      </button>
      {open ? <div className="mt-5">{children}</div> : null}
    </Panel>
  );
}

function CompactSnapshot({ label, value, detail }: { label: string; value: string; detail: string }) {
  return (
    <div className="border-l-2 border-slate-200 py-1 pl-3 dark:border-slate-700">
      <p className="text-xs text-slate-500 dark:text-slate-400">{label}</p>
      <p className="mt-1 text-lg font-semibold text-slate-900 dark:text-slate-100">{value}</p>
      <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">{detail}</p>
    </div>
  );
}

function ActivityRow({ label, value, detail }: { label: string; value: string; detail: string }) {
  return (
    <div className="border-b border-slate-100 px-1 py-3 last:border-b-0 dark:border-slate-800">
      <div className="flex items-center justify-between gap-3">
        <div className="text-xs font-medium text-slate-500 dark:text-slate-400">{label}</div>
        <div className="text-xs font-medium text-slate-700 dark:text-slate-300">{value}</div>
      </div>
      <div className="mt-1.5 text-xs leading-relaxed text-slate-600 dark:text-slate-300">{detail}</div>
    </div>
  );
}

function ActionFeedRow({
  timestamp,
  source,
  actionType,
  status,
  subjectName,
  summary,
  metadata,
}: {
  timestamp: string;
  source: string;
  actionType: string;
  status: string;
  subjectName?: string | null;
  summary: string;
  metadata?: Record<string, unknown>;
}) {
  const when = timestamp
    ? new Date(timestamp).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
    : "";
  const label = subjectName || formatUserLabel(actionType);
  const recentCount =
    typeof metadata?.recent_count === "number"
      ? metadata.recent_count
      : null;
  const statusLabel =
    status === "ok"
      ? source === "automation"
        ? "routine"
        : "ok"
      : formatUserLabel(status);
  return (
    <div className="border-b border-slate-100 px-1 py-3 last:border-b-0 dark:border-slate-800">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <div className="text-xs font-medium text-slate-500 dark:text-slate-400">
              {formatUserLabel(source)} · {label}
            </div>
            {recentCount && recentCount > 1 ? (
              <span className="rounded-full bg-slate-200 px-2 py-0.5 text-[9px] font-bold uppercase tracking-wider text-slate-600 dark:bg-slate-800 dark:text-slate-300">
                {recentCount}x
              </span>
            ) : null}
          </div>
        </div>
        <div className="text-xs font-medium text-slate-600 dark:text-slate-300">
          {when || formatUserLabel(status)}
        </div>
      </div>
      <div className="mt-1.5 text-xs leading-relaxed text-slate-600 dark:text-slate-300">
        {summary}
      </div>
      <div className="mt-2 text-xs text-slate-400 dark:text-slate-500">
        {statusLabel}
      </div>
    </div>
  );
}

function ScannerRow({
  rank,
  label,
  score,
  detail,
  tags,
}: {
  rank: number;
  label: string;
  score: number;
  detail: string;
  tags: string[];
}) {
  return (
    <div className="grid grid-cols-[34px_minmax(0,1fr)_68px] items-start gap-3 border-b border-slate-100 px-1 py-3 last:border-b-0 dark:border-slate-800">
      <div className="rounded bg-slate-100 px-2 py-1 text-center text-xs font-semibold text-slate-500 dark:bg-slate-900 dark:text-slate-300">
        #{rank}
      </div>
      <div className="min-w-0">
        <div className="truncate text-sm font-bold text-slate-900 dark:text-slate-100">{label}</div>
        <div className="mt-1 line-clamp-2 text-xs leading-relaxed text-slate-500 dark:text-slate-400">{detail}</div>
        {tags.length > 0 ? (
          <div className="mt-2 flex flex-wrap gap-1.5">
            {tags.slice(0, 2).map((tag) => (
              <span
                key={`${label}-${tag}`}
                className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium text-slate-600 dark:bg-slate-900 dark:text-slate-300"
              >
                {tag}
              </span>
            ))}
          </div>
        ) : null}
      </div>
      <div className="text-right">
        <div className="text-[10px] font-bold uppercase tracking-widest text-slate-400 dark:text-slate-500">Score</div>
        <div className="mt-1 text-sm font-black text-slate-900 dark:text-slate-100">{score.toFixed(1)}</div>
      </div>
    </div>
  );
}

function PortfolioBuildChart({ points }: { points: DashboardSummary["portfolio_build_series"] }) {
  if (points.length === 0) return <div className="py-12 text-center text-slate-400 italic text-sm">No series data</div>;

  if (!points?.length) return null;

  return (
    <div className="mt-4 h-[180px] w-full">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={points}>
          <defs>
            <linearGradient id="colorDeployed" x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%" stopColor="#0284c7" stopOpacity={0.3}/>
              <stop offset="95%" stopColor="#0284c7" stopOpacity={0}/>
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="rgba(148,163,184,0.18)" />
          <XAxis
            dataKey="as_of"
            hide
          />
          <YAxis
            hide
            domain={['dataMin - 5000', 'dataMax + 5000']}
          />
          <Tooltip
            content={({ active, payload }) => {
              if (active && payload && payload.length) {
                const data = payload[0].payload;
                return (
                  <div className="rounded-lg border border-slate-200 bg-white/95 p-3 shadow-xl backdrop-blur-sm dark:border-slate-700 dark:bg-slate-950/95">
                    <p className="mb-1 text-[10px] font-bold uppercase tracking-widest text-slate-400 dark:text-slate-500">
                      {new Date(data.as_of).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })}
                    </p>
                    <p className="text-sm font-black text-slate-900 dark:text-slate-100">
                      {safeFormatCurrency(data.net_capital_deployed)}
                    </p>
                    <div className="mt-1 flex items-center gap-2">
                       <span className="rounded bg-sky-50 px-1.5 py-0.5 text-[10px] font-bold text-sky-500 dark:bg-sky-950/30 dark:text-sky-300">
                         {data.transaction_count} txns
                       </span>
                    </div>
                  </div>
                );
              }
              return null;
            }}
          />
          <Area
            type="monotone"
            dataKey="net_capital_deployed"
            stroke="#0284c7"
            strokeWidth={3}
            fillOpacity={1}
            fill="url(#colorDeployed)"
            animationDuration={1500}
          />
        </AreaChart>
      </ResponsiveContainer>
      <div className="mt-4 flex items-end justify-between">
        <div>
          <p className="text-[10px] font-bold uppercase tracking-tighter text-slate-400 dark:text-slate-500">Current Inflow</p>
          <p className="text-sm font-black text-slate-900 dark:text-slate-100">{safeFormatCurrency(points[points.length-1].net_capital_deployed)}</p>
        </div>
        <div className="text-right">
          <p className="text-[10px] font-bold uppercase tracking-tighter text-slate-400 dark:text-slate-500">Velocity</p>
          <p className="text-sm font-black text-slate-900 dark:text-slate-100">{points[points.length-1].transaction_count} txns</p>
        </div>
      </div>
    </div>
  );
}

function formatShortDateTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Unknown";
  return date.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function formatQuantity(value: number): string {
  if (!Number.isFinite(value)) return "0";
  return new Intl.NumberFormat("en-US", {
    maximumFractionDigits: value < 1 ? 4 : 2,
  }).format(value);
}

function cleanResearchTitle(title: string | null | undefined): string | null {
  if (!title) return null;
  return title.replace(/^Research on:\s*/i, "").replace(/^Ad hoc portfolio research\s*[:\-]?\s*/i, "").trim();
}

function topScannerLabel(summary: DashboardSummary): string | null {
  const top = summary.review_queue?.[0];
  if (!top) return null;
  const label = top.item_label?.trim();
  if (!label) return null;
  if (["ok", "idle", "none", "n/a"].includes(label.toLowerCase()) || label.length <= 3) {
    const fallback = top.signal_tags?.[0] || top.why_now_summary || top.trigger_reason;
    return fallback ? fallback.replace(/\.$/, "") : null;
  }
  return label;
}

function marketDataStatus(summary: DashboardSummary): string {
  const marketJob = summary.jobs?.find((job) => job.name === "market_data_refresh");
  if (!marketJob) return "automation status unavailable";
  if (marketJob.last_status === "disabled") return "market refresh disabled";
  if (marketJob.last_status === "error") return "market refresh errored";
  if (marketJob.last_run_at) {
    return `quotes refreshed ${new Date(marketJob.last_run_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`;
  }
  return "market refresh waiting";
}
