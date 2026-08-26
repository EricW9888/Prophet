"use client";

import { use, useEffect, useState } from "react";
import Link from "next/link";
import { Activity, Clock3, ExternalLink, Gauge, SearchCheck } from "lucide-react";

import AppNav from "@/components/AppNav";
import { apiFetch, ProfileDetail } from "@/lib/api";

function compactDate(value?: string | null) {
  if (!value) return "date unavailable";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric" }).format(date);
}

function metricDisplay(metric: ProfileDetail["fundamental_metrics"][number]) {
  if (metric.value_text) return metric.value_text;
  if (metric.numeric_value == null) return "value not recorded";
  const suffix = metric.unit ? ` ${metric.unit}` : "";
  const prefix = metric.currency === "USD" ? "$" : "";
  return `${prefix}${metric.numeric_value.toLocaleString()}${suffix}`;
}

export default function ProfilePage({ params }: { params: Promise<{ id: string }> }) {
  const resolvedParams = use(params);
  const profileId = resolvedParams.id;
  const [profile, setProfile] = useState<ProfileDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void apiFetch<ProfileDetail>(`/profiles/${profileId}`)
      .then((data) => {
        setProfile(data);
        setError(null);
      })
      .catch((err) => setError(err instanceof Error ? err.message : "Unable to load profile."))
      .finally(() => setLoading(false));
  }, [profileId]);

  const getStanceColor = (stance: string | null | undefined) => {
    switch (stance) {
      case "bullish":
        return "text-emerald-600 dark:text-emerald-400 bg-emerald-50 dark:bg-emerald-950/30 border-emerald-200 dark:border-emerald-800/50";
      case "bearish":
        return "text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-950/30 border-red-200 dark:border-red-800/50";
      default:
        return "text-slate-600 dark:text-slate-400 bg-slate-50 dark:bg-slate-800/50 border-slate-200 dark:border-slate-700";
    }
  };

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-background">
        <div className="animate-pulse text-slate-500 font-mono text-sm tracking-widest">LOADING PROFILE...</div>
      </div>
    );
  }

  if (!profile || error) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-background text-red-500">
        {error ?? "Profile not found."}
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-background text-foreground font-sans pb-20">
      <AppNav active="research" />

      <header className="max-w-5xl mx-auto px-4 sm:px-6 lg:px-8 pt-12 pb-8 border-b border-slate-200 dark:border-slate-800">
        <div className="flex flex-col md:flex-row md:items-start justify-between gap-6">
          <div className="space-y-2">
            <div className="flex items-center gap-3">
              <span className="px-2.5 py-0.5 text-[10px] font-bold uppercase tracking-widest text-sky-600 bg-sky-50 border border-sky-200 rounded dark:bg-sky-950/40 dark:text-sky-400 dark:border-sky-800/50">
                {profile.subject_type} PROFILE
              </span>
            </div>
            <h1 className="text-4xl sm:text-5xl font-extrabold tracking-tight text-slate-900 dark:text-white">{profile.subject_name}</h1>
            <p className="text-sm font-mono text-slate-500">Node ID: {profile.subject_id}</p>
          </div>
          <div className="flex flex-col sm:flex-row items-start sm:items-center gap-4">
            <div className={`flex flex-col p-3 rounded-lg border ${getStanceColor(profile.current_stance)} min-w-[140px]`}>
              <span className="text-[10px] uppercase font-bold tracking-wider opacity-70 mb-1">System Stance</span>
              <span className="text-xl font-bold uppercase tracking-tight leading-none">{profile.current_stance ?? "no_view"}</span>
              <span className="text-xs font-medium mt-2 opacity-80">Confidence: {profile.confidence_band ?? "unknown"}</span>
            </div>
            <div className="flex flex-col p-3 bg-white dark:bg-[#111] border border-slate-200 dark:border-slate-800 rounded-lg min-w-[140px]">
              <span className="text-[10px] uppercase font-bold tracking-wider text-slate-500 mb-1">Coverage Score</span>
              <div className="flex items-baseline gap-1">
                <span className="text-2xl font-bold tracking-tight text-slate-900 dark:text-white leading-none">{(profile.coverage_score ?? 0).toFixed(1)}</span>
                <span className="text-sm text-slate-400">/ 100</span>
              </div>
            </div>
          </div>
        </div>
      </header>

      <main className="max-w-5xl mx-auto px-4 sm:px-6 lg:px-8 py-10 grid grid-cols-1 lg:grid-cols-3 gap-8">
        <div className="lg:col-span-2 space-y-8">
          <section className="bg-white dark:bg-[#111] border border-slate-200 dark:border-slate-800 rounded-lg p-6">
            <h2 className="text-sm font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400 mb-4">Core Conclusion State</h2>
            <p className="text-lg md:text-xl font-medium text-slate-900 dark:text-slate-100 leading-snug">
              {profile.current_thesis_summary ?? profile.executive_summary ?? "No current conclusion state yet."}
            </p>
          </section>

          {(profile.fundamental_metrics?.length || profile.market_setup_signals?.length) ? (
            <section className="border-y border-slate-200 bg-white dark:border-slate-800 dark:bg-[#111]">
              {profile.fundamental_metrics?.length ? (
                <div className="px-6 py-6">
                  <div className="mb-4 flex items-start gap-3">
                    <Gauge className="mt-0.5 h-5 w-5 text-sky-600 dark:text-sky-400" aria-hidden="true" />
                    <div>
                      <h2 className="text-sm font-bold uppercase text-slate-600 dark:text-slate-300">Fundamentals & Valuation</h2>
                      <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
                        Source-dated measurements tied to this profile. Stale and contradictory values remain visible for review.
                      </p>
                    </div>
                  </div>
                  <div className="divide-y divide-slate-200 dark:divide-slate-800">
                    {profile.fundamental_metrics.map((metric) => (
                      <article key={metric.id} className="grid gap-3 py-4 first:pt-0 md:grid-cols-[minmax(0,0.7fr)_minmax(0,1.3fr)]">
                        <div>
                          <div className="flex flex-wrap items-center gap-2">
                            <h3 className="font-semibold text-slate-950 dark:text-white">{metric.metric_name}</h3>
                            <span className="rounded bg-sky-50 px-2 py-0.5 text-[11px] font-medium text-sky-700 dark:bg-sky-950/40 dark:text-sky-300">
                              {metric.metric_family.replaceAll("_", " ")}
                            </span>
                          </div>
                          <p className="mt-2 text-lg font-semibold text-slate-900 dark:text-slate-100">{metricDisplay(metric)}</p>
                          <p className="mt-1 text-xs text-slate-500">
                            {[metric.ticker, metric.period_label, compactDate(metric.as_of ?? metric.public_time)].filter(Boolean).join(" · ")}
                          </p>
                        </div>
                        <div className="text-sm leading-relaxed text-slate-700 dark:text-slate-300">
                          <p>{metric.investment_relevance ?? "Investment relevance has not been recorded yet."}</p>
                          {metric.next_test ? <p className="mt-2 text-slate-500 dark:text-slate-400">Next check: {metric.next_test}</p> : null}
                          <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-slate-500">
                            <span>{metric.freshness_status?.replaceAll("_", " ") ?? "freshness unscored"}</span>
                            <span aria-hidden="true">·</span>
                            <span>{metric.source_name ?? metric.source_type ?? "source unavailable"}</span>
                            {metric.url ? (
                              <a href={metric.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 font-medium text-sky-700 hover:underline dark:text-sky-300">
                                Open source <ExternalLink className="h-3 w-3" aria-hidden="true" />
                              </a>
                            ) : null}
                          </div>
                        </div>
                      </article>
                    ))}
                  </div>
                </div>
              ) : null}

              {profile.market_setup_signals?.length ? (
                <div className="border-t border-slate-200 px-6 py-6 dark:border-slate-800">
                  <div className="mb-4 flex items-start gap-3">
                    <Activity className="mt-0.5 h-5 w-5 text-amber-600 dark:text-amber-400" aria-hidden="true" />
                    <div>
                      <h2 className="text-sm font-bold uppercase text-slate-600 dark:text-slate-300">Market Setup & Expectations</h2>
                      <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
                        What investors appeared to expect, what occurred, and the evidence still needed to score the signal.
                      </p>
                    </div>
                  </div>
                  <div className="divide-y divide-slate-200 dark:divide-slate-800">
                    {profile.market_setup_signals.map((signal) => (
                      <article key={signal.id} className="py-4 first:pt-0">
                        <div className="flex flex-wrap items-start justify-between gap-3">
                          <div>
                            <h3 className="font-semibold text-slate-950 dark:text-white">{signal.signal_name}</h3>
                            <p className="mt-1 text-xs text-slate-500">
                              {[signal.ticker, signal.signal_family.replaceAll("_", " "), compactDate(signal.as_of ?? signal.public_time)].filter(Boolean).join(" · ")}
                            </p>
                          </div>
                          <span className="rounded bg-amber-50 px-2 py-1 text-[11px] font-medium text-amber-800 dark:bg-amber-950/40 dark:text-amber-300">
                            {(signal.outcome_status ?? "unscored").replaceAll("_", " ")}
                          </span>
                        </div>
                        <div className="mt-3 grid gap-3 text-sm leading-relaxed md:grid-cols-2">
                          <p className="text-slate-800 dark:text-slate-200">{signal.setup_context ?? "Setup context was not recorded."}</p>
                          <p className="text-slate-600 dark:text-slate-400">
                            {signal.actual_context ?? signal.price_reaction ?? signal.investment_relevance ?? "The later result has not been scored yet."}
                          </p>
                        </div>
                        {signal.outcome_status === "unscored" && signal.outcome_assessment_attempt ? (
                          <div className="mt-3 border-l-2 border-amber-300 pl-3 text-xs text-slate-600 dark:border-amber-700 dark:text-slate-400">
                            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 font-medium text-slate-800 dark:text-slate-200">
                              <span className="inline-flex items-center gap-1">
                                <Clock3 className="h-3.5 w-3.5" aria-hidden="true" />
                                Outcome review deferred
                              </span>
                              <span>
                                Attempt {signal.outcome_assessment_attempt.attempt_count ?? 1}
                                {signal.outcome_assessment_attempt.next_retry_at
                                  ? ` · eligible again ${compactDate(signal.outcome_assessment_attempt.next_retry_at)}`
                                  : ""}
                              </span>
                              {signal.outcome_assessment_attempt.research_followup?.started ? (
                                <span className="inline-flex items-center gap-1 text-sky-700 dark:text-sky-300">
                                  <SearchCheck className="h-3.5 w-3.5" aria-hidden="true" />
                                  Follow-up research started
                                </span>
                              ) : null}
                            </div>
                            <p className="mt-1">
                              {signal.outcome_assessment_attempt.rationale
                                ?? signal.outcome_assessment_attempt.limitations
                                ?? "Later evidence did not clear the confidence threshold."}
                            </p>
                          </div>
                        ) : null}
                        {signal.next_test ? <p className="mt-2 text-sm text-slate-500 dark:text-slate-400">Next check: {signal.next_test}</p> : null}
                        <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-slate-500">
                          <span>{signal.source_name ?? signal.source_type ?? "source unavailable"}</span>
                          {signal.url ? (
                            <a href={signal.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 font-medium text-amber-700 hover:underline dark:text-amber-300">
                              Open source <ExternalLink className="h-3 w-3" aria-hidden="true" />
                            </a>
                          ) : null}
                        </div>
                      </article>
                    ))}
                  </div>
                </div>
              ) : null}
            </section>
          ) : null}

          {profile.historical_analogy_lenses.length ? (
            <section className="bg-white dark:bg-[#111] border border-slate-200 dark:border-slate-800 rounded-lg p-6">
              <div className="flex items-start justify-between gap-4 mb-5">
                <div>
                  <h2 className="text-sm font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400">Historical Rhyme</h2>
                  <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
                    Causal analogies to test against current evidence, not predictions.
                  </p>
                </div>
              </div>
              <div className="space-y-4">
                {profile.historical_analogy_lenses.map((lens) => (
                  <article key={`${lens.name}-${lens.period ?? "period"}`} className="rounded-lg border border-slate-200 dark:border-slate-800 p-4">
                    <div className="flex flex-wrap items-center gap-2">
                      <h3 className="text-base font-bold text-slate-900 dark:text-white">{lens.name}</h3>
                      {lens.period ? (
                        <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[11px] font-semibold text-slate-500 dark:bg-slate-900 dark:text-slate-400">
                          {lens.period}
                        </span>
                      ) : null}
                    </div>
                    {lens.what_rhymes ? (
                      <p className="mt-3 text-sm leading-relaxed text-slate-800 dark:text-slate-200">{lens.what_rhymes}</p>
                    ) : null}
                    <div className="mt-4 grid gap-3 md:grid-cols-2">
                      {lens.dominant_channel_test ? (
                        <div className="rounded-lg border border-slate-200 bg-slate-50 p-3 dark:border-slate-800 dark:bg-slate-950/40">
                          <p className="text-[10px] font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400">Channel To Test</p>
                          <p className="mt-1 text-sm text-slate-800 dark:text-slate-200">{lens.dominant_channel_test}</p>
                        </div>
                      ) : null}
                      {lens.where_analogy_breaks ? (
                        <div className="rounded-lg border border-slate-200 bg-slate-50 p-3 dark:border-slate-800 dark:bg-slate-950/40">
                          <p className="text-[10px] font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400">Break Condition</p>
                          <p className="mt-1 text-sm text-slate-800 dark:text-slate-200">{lens.where_analogy_breaks}</p>
                        </div>
                      ) : null}
                      {lens.portfolio_transmission ? (
                        <div className="rounded-lg border border-slate-200 bg-slate-50 p-3 dark:border-slate-800 dark:bg-slate-950/40">
                          <p className="text-[10px] font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400">Portfolio Route</p>
                          <p className="mt-1 text-sm text-slate-800 dark:text-slate-200">{lens.portfolio_transmission}</p>
                        </div>
                      ) : null}
                      {lens.best_next_check ? (
                        <div className="rounded-lg border border-slate-200 bg-slate-50 p-3 dark:border-slate-800 dark:bg-slate-950/40">
                          <p className="text-[10px] font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400">Best Next Check</p>
                          <p className="mt-1 text-sm text-slate-800 dark:text-slate-200">{lens.best_next_check}</p>
                        </div>
                      ) : null}
                    </div>
                    {lens.investor_questions.length ? (
                      <ul className="mt-4 space-y-2">
                        {lens.investor_questions.slice(0, 3).map((question) => (
                          <li key={question} className="text-sm text-slate-700 dark:text-slate-300">
                            <span className="mr-2 text-slate-400">•</span>
                            {question}
                          </li>
                        ))}
                      </ul>
                    ) : null}
                  </article>
                ))}
              </div>
            </section>
          ) : null}

          <section className="bg-red-50/50 dark:bg-red-950/10 border border-red-100 dark:border-red-900/30 rounded-lg p-6">
            <h2 className="text-sm font-bold uppercase tracking-wider text-red-800 dark:text-red-400 mb-4">What Would Falsify This</h2>
            <ul className="space-y-3">
              {profile.what_would_falsify.length ? profile.what_would_falsify.map((item, i) => (
                <li key={i} className="flex items-start gap-3">
                  <span className="flex-shrink-0 w-5 h-5 rounded-full bg-red-100 dark:bg-red-900/50 flex items-center justify-center mt-0.5">
                    <span className="w-1.5 h-1.5 rounded-full bg-red-600 dark:bg-red-400"></span>
                  </span>
                  <span className="text-slate-800 dark:text-slate-200">{item}</span>
                </li>
              )) : <li className="text-sm text-slate-500">No falsifiers recorded yet.</li>}
            </ul>
          </section>

          <section className="border border-slate-200 dark:border-slate-800 rounded-lg p-6 bg-white dark:bg-[#111]">
            <h2 className="text-sm font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400 mb-4">Recent Evidence</h2>
            <div className="space-y-3">
              {profile.recent_evidence.map((item) => (
                <div key={item.id} className="rounded-lg border border-slate-200 dark:border-slate-800 p-4">
                  <div className="flex items-center justify-between gap-4">
                    <span className="text-xs uppercase tracking-wider text-slate-400">{item.node_type}</span>
                    <span className="text-xs font-mono text-slate-400">{item.tier ?? "unknown"}</span>
                  </div>
                  <p className="mt-2 text-sm text-slate-800 dark:text-slate-200">{item.text}</p>
                </div>
              ))}
            </div>
          </section>
        </div>

        <div className="space-y-6">
          <section className="bg-white dark:bg-[#111] border border-slate-200 dark:border-slate-800 rounded-lg p-6">
            <h3 className="text-xs font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400 mb-5 border-b border-slate-100 dark:border-slate-800 pb-2">Coverage Audit</h3>
            <div className="space-y-4">
              <div>
                <span className="text-xs text-slate-500 block mb-1">Missing Evidence Classes</span>
                <div className="flex flex-wrap gap-2">
                  {profile.missing_evidence.map((item) => (
                    <span key={item.id} className="inline-flex items-center px-2 py-1 rounded bg-amber-100 dark:bg-amber-900/30 text-amber-800 dark:text-amber-400 text-xs font-medium border border-amber-200 dark:border-amber-800/50">
                      {item.class_name}
                    </span>
                  ))}
                </div>
              </div>
              <div className="pt-2 border-t border-slate-100 dark:border-slate-800">
                <span className="text-xs text-slate-500 block mb-1">Unresolved Questions</span>
                <div className="space-y-3">
                  {profile.unresolved_questions.map((item) => (
                    <div key={item.id}>
                      <p className="text-sm text-slate-800 dark:text-slate-300">{item.question_text}</p>
                      <div className="mt-1 text-[10px] text-sky-600 dark:text-sky-400 font-mono">
                        Status: {item.status} | Urgency: {item.urgency}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          </section>

          <section className="bg-white dark:bg-[#111] border border-slate-200 dark:border-slate-800 rounded-lg p-6">
            <h3 className="text-xs font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400 mb-4 border-b border-slate-100 dark:border-slate-800 pb-2">Narrative Split</h3>
            <div className="space-y-4 text-sm">
              <div>
                <p className="text-xs uppercase tracking-wider text-slate-400 mb-1">Bull Case</p>
                <p>{profile.bull_case ?? "Not set yet."}</p>
              </div>
              <div>
                <p className="text-xs uppercase tracking-wider text-slate-400 mb-1">Bear Case</p>
                <p>{profile.bear_case ?? "Not set yet."}</p>
              </div>
            </div>
            <Link href="/chat" className="mt-6 block w-full py-2 bg-slate-100 dark:bg-slate-800 hover:bg-slate-200 dark:hover:bg-slate-700 text-center text-slate-900 dark:text-white rounded-lg text-sm font-medium transition-colors border border-slate-200 dark:border-slate-700">
              Launch Research Chat
            </Link>
          </section>
        </div>
      </main>
    </div>
  );
}
