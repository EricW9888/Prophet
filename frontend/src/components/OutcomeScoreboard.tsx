"use client";

import { useEffect, useState } from "react";

import { apiFetch } from "@/lib/api";

type OutcomeRow = {
  id: string;
  label: string;
  outcome: string;
  notes?: string | null;
  source?: string | null;
  ticker?: string | null;
  as_of?: string | null;
};

type OutcomeGroup = {
  counts: Record<string, number>;
  scored: number;
  awaiting: number;
  overdue?: number;
  held_rate?: number | null;
  correct_rate?: number | null;
  recent: OutcomeRow[];
};

export type OutcomeSummary = {
  market_setups: OutcomeGroup;
  source_claims: OutcomeGroup;
  shadow_experiments: Record<string, number>;
};

const OUTCOME_LABELS: Record<string, string> = {
  validated: "Held",
  invalidated: "Did not hold",
  indeterminate: "Unclear",
  correct: "Correct",
  partially_correct: "Partly correct",
  incorrect: "Incorrect",
};

const POSITIVE = new Set(["validated", "correct"]);
const NEGATIVE = new Set(["invalidated", "incorrect"]);

export function outcomeLabel(outcome: string): string {
  return OUTCOME_LABELS[outcome] ?? outcome.replace(/_/g, " ");
}

/** "42 of 46 decided" style rate: the share of decided outcomes that held, with its base. */
export function rateText(rate: number | null | undefined, hits: number, misses: number): string {
  if (rate == null) return "Nothing decided yet";
  return `${Math.round(rate * 100)}% · ${hits} of ${hits + misses} decided`;
}

function Scorecard({ title, group, hitsKey, missesKey, rate, note }: {
  title: string;
  group: OutcomeGroup;
  hitsKey: string;
  missesKey: string;
  rate: number | null | undefined;
  note: string;
}) {
  const hits = group.counts[hitsKey] ?? 0;
  const misses = group.counts[missesKey] ?? 0;
  return (
    <div className="border border-line bg-panel p-4">
      <p className="text-sm text-muted">{title}</p>
      <p className="mt-1 text-2xl font-semibold tabular-nums">{rateText(rate, hits, misses)}</p>
      <p className="mt-2 text-xs text-muted">
        {group.scored} scored · {group.awaiting} waiting{group.overdue ? ` (${group.overdue} overdue)` : ""}. {note}
      </p>
    </div>
  );
}

function RecentList({ title, rows }: { title: string; rows: OutcomeRow[] }) {
  if (!rows.length) return null;
  return (
    <div>
      <h3 className="text-sm font-semibold">{title}</h3>
      <ul className="mt-2 divide-y divide-line border-y border-line">
        {rows.map((row) => (
          <li key={row.id} className="flex items-baseline justify-between gap-4 py-2 text-sm">
            <span className="min-w-0">
              <span className="block truncate" title={row.label}>{row.label}</span>
              {row.source || row.as_of ? (
                <span className="text-xs text-muted">
                  {[row.source, row.as_of ? new Date(row.as_of).toLocaleDateString() : null].filter(Boolean).join(" · ")}
                </span>
              ) : null}
            </span>
            <span className={`shrink-0 text-xs font-medium ${POSITIVE.has(row.outcome) ? "text-positive" : NEGATIVE.has(row.outcome) ? "text-negative" : "text-muted"}`}>
              {outcomeLabel(row.outcome)}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** What the learning loop has scored so far, with every rate shown beside its base. */
export default function OutcomeScoreboard() {
  const [summary, setSummary] = useState<OutcomeSummary | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    apiFetch<OutcomeSummary>("/decisions/outcomes")
      .then((value) => { if (active) setSummary(value); })
      .catch((reason: unknown) => { if (active) setError(reason instanceof Error ? reason.message : "Outcomes unavailable"); });
    return () => { active = false; };
  }, []);

  if (error) return <p className="border border-line bg-panel p-4 text-sm text-muted">Outcome scores are unavailable: {error}</p>;
  if (!summary) return <p className="border border-line bg-panel p-4 text-sm text-muted">Loading outcome scores…</p>;

  const shadowDone = summary.shadow_experiments.completed ?? 0;
  const shadowRunning = summary.shadow_experiments.running ?? 0;
  return (
    <section className="space-y-4" aria-labelledby="outcomes-heading">
      <div>
        <h2 id="outcomes-heading" className="text-lg font-semibold">How tracked calls turned out</h2>
        <p className="mt-1 text-sm text-muted">
          Scored against later evidence by Prophet&apos;s outcome checks. Unclear results are not counted as hits or misses.
        </p>
      </div>
      <div className="grid gap-4 md:grid-cols-3">
        <Scorecard title="Market setups that held" group={summary.market_setups} hitsKey="validated" missesKey="invalidated" rate={summary.market_setups.held_rate} note="Setups Prophet tracked." />
        <Scorecard title="Source claims that proved correct" group={summary.source_claims} hitsKey="correct" missesKey="incorrect" rate={summary.source_claims.correct_rate} note="Claims made by sources." />
        <div className="border border-line bg-panel p-4">
          <p className="text-sm text-muted">Shadow experiments</p>
          <p className="mt-1 text-2xl font-semibold tabular-nums">{shadowDone} completed</p>
          <p className="mt-2 text-xs text-muted">{shadowRunning} running. Results are on the Simulate page.</p>
        </div>
      </div>
      <div className="grid gap-6 lg:grid-cols-2">
        <RecentList title="Latest scored setups" rows={summary.market_setups.recent} />
        <RecentList title="Latest assessed claims" rows={summary.source_claims.recent} />
      </div>
    </section>
  );
}
