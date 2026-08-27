"use client";

import { useEffect, useState } from "react";

import AppNav from "@/components/AppNav";
import PageHeader from "@/components/PageHeader";
import { API_BASE, apiFetch, DecisionJournal, Position } from "@/lib/api";

export default function DecisionsPage() {
  const [decisions, setDecisions] = useState<DecisionJournal[]>([]);
  const [positions, setPositions] = useState<Position[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [reviewingId, setReviewingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [form, setForm] = useState({
    position_id: "",
    decision_type: "enter",
    rationale: "",
    expected_catalyst_timeframe: "",
    expected_return: "",
  });
  const [reviewForm, setReviewForm] = useState({
    outcome_assessment: "correct_for_right_reason",
    actual_return: "",
    mistake_preventable: "unknown",
    what_went_right: "",
    what_went_wrong: "",
    what_to_improve: "",
  });
  const [showManualEntry, setShowManualEntry] = useState(false);

  async function loadState() {
    setLoading(true);
    try {
      const [decisionData, positionData] = await Promise.all([
        apiFetch<DecisionJournal[]>("/decisions/"),
        apiFetch<Position[]>("/portfolio/positions?list_type=all"),
      ]);
      setDecisions(decisionData);
      setPositions(positionData);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to load decision journal.");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void loadState();
  }, []);

  async function createDecision(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      const response = await fetch(`${API_BASE}/decisions/`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          position_id: form.position_id || null,
          decision_type: form.decision_type,
          rationale: form.rationale,
          expected_catalyst_timeframe: form.expected_catalyst_timeframe || null,
          expected_return: form.expected_return ? Number(form.expected_return) : null,
        }),
      });
      if (!response.ok) {
        throw new Error(await response.text());
      }
      setForm({
        position_id: "",
        decision_type: "enter",
        rationale: "",
        expected_catalyst_timeframe: "",
        expected_return: "",
      });
      await loadState();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to create decision.");
    } finally {
      setSaving(false);
    }
  }

  async function createReview(decisionId: string) {
    setSaving(true);
    setError(null);
    try {
      const response = await fetch(`${API_BASE}/decisions/reviews`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          decision_journal_id: decisionId,
          outcome_assessment: reviewForm.outcome_assessment,
          actual_return: reviewForm.actual_return ? Number(reviewForm.actual_return) : null,
          mistake_preventable:
            reviewForm.mistake_preventable === "unknown"
              ? null
              : reviewForm.mistake_preventable === "true",
          what_went_right: reviewForm.what_went_right || null,
          what_went_wrong: reviewForm.what_went_wrong || null,
          what_to_improve: reviewForm.what_to_improve || null,
        }),
      });
      if (!response.ok) {
        throw new Error(await response.text());
      }
      setReviewingId(null);
      setReviewForm({
        outcome_assessment: "correct_for_right_reason",
        actual_return: "",
        mistake_preventable: "unknown",
        what_went_right: "",
        what_went_wrong: "",
        what_to_improve: "",
      });
      await loadState();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to save review.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="min-h-screen bg-background text-foreground font-sans">
      <AppNav active="portfolio" />
      <main className="mx-auto grid w-full max-w-[1440px] grid-cols-1 gap-8 px-4 py-8 sm:px-6 lg:px-8 xl:grid-cols-[0.9fr_1.1fr]">
        <PageHeader
          className="xl:col-span-2"
          eyebrow="Learning loop"
          title="Operating memory"
          description="Review what Prophet concluded, what changed, how simulations and later outcomes differed, and what the system learned."
        />
        <section className="space-y-6">
          <section className="rounded-lg border border-slate-200 bg-white p-6 dark:border-slate-800 dark:bg-slate-950 space-y-4">
            <div className="flex items-start justify-between gap-4">
              <div>
                <h2 className="text-sm font-semibold text-slate-700 dark:text-slate-200">Automated record with manual override</h2>
                <p className="mt-2 text-sm text-slate-600 dark:text-slate-300">
                  Prophet records reasoning runs, view changes, shadow experiments, and retrospective reviews automatically. Manual entry is available when the record needs an explicit correction or addition.
                </p>
              </div>
              <button
                type="button"
                onClick={() => setShowManualEntry((value) => !value)}
                className="rounded-lg border border-slate-300 dark:border-slate-700 px-3 py-2 text-sm"
              >
                {showManualEntry ? "Hide manual override" : "Manual override"}
              </button>
            </div>
            {showManualEntry ? (
              <form onSubmit={createDecision} className="space-y-4 border-t border-slate-200 pt-4 dark:border-slate-800">
                <h3 className="text-sm font-semibold text-slate-700 dark:text-slate-200">Manual operating-memory entry</h3>
                <select value={form.position_id} onChange={(e) => setForm((current) => ({ ...current, position_id: e.target.value }))} className={inputClass}>
                  <option value="">No linked position</option>
                  {positions.map((position) => (
                    <option key={position.id} value={position.id}>
                      {position.ticker ?? position.security_id}
                    </option>
                  ))}
                </select>
                <select value={form.decision_type} onChange={(e) => setForm((current) => ({ ...current, decision_type: e.target.value }))} className={inputClass}>
                  <option value="enter">Enter</option>
                  <option value="add">Add</option>
                  <option value="trim">Trim</option>
                  <option value="exit">Exit</option>
                  <option value="hold_through_earnings">Hold Through Earnings</option>
                  <option value="pass">Pass</option>
                </select>
                <textarea value={form.rationale} onChange={(e) => setForm((current) => ({ ...current, rationale: e.target.value }))} rows={6} placeholder="Why this override matters." className={inputClass} />
                <input value={form.expected_catalyst_timeframe} onChange={(e) => setForm((current) => ({ ...current, expected_catalyst_timeframe: e.target.value }))} placeholder="Catalyst timeframe" className={inputClass} />
                <input value={form.expected_return} onChange={(e) => setForm((current) => ({ ...current, expected_return: e.target.value }))} type="number" step="0.01" placeholder="Expected return %" className={inputClass} />
                <button disabled={saving || !form.rationale.trim()} className="w-full rounded-lg bg-sky-600 px-4 py-3 text-white disabled:opacity-50">
                  {saving ? "Saving..." : "Record override"}
                </button>
              </form>
            ) : null}
          </section>
          {error ? <p className="text-sm text-red-500">{error}</p> : null}
        </section>

        <section className="space-y-4">
          {loading ? (
            <div className="text-sm text-slate-500 animate-pulse">Loading decisions...</div>
          ) : decisions.length === 0 ? (
            <div className="rounded-lg border border-slate-200 bg-white p-6 dark:border-slate-800 dark:bg-slate-950 text-slate-500 dark:text-slate-400">
              No decisions recorded yet.
            </div>
          ) : (
            decisions.map((decision) => (
              <article key={decision.id} className="rounded-lg border border-slate-200 bg-white p-6 dark:border-slate-800 dark:bg-slate-950">
                <div className="flex items-center justify-between gap-4">
                  <div>
                    <h2 className="text-xl font-semibold tracking-tight">{decision.position_label ?? "Unlinked decision"}</h2>
                    <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">{decision.decision_type} · {new Date(decision.created_at).toLocaleString()}</p>
                  </div>
                  <button onClick={() => setReviewingId(reviewingId === decision.id ? null : decision.id)} className="rounded-lg border border-slate-300 dark:border-slate-700 px-3 py-2 text-sm">
                    {reviewingId === decision.id ? "Close Review" : "Add Review"}
                  </button>
                </div>
                <p className="mt-4 text-sm text-slate-700 dark:text-slate-300">{decision.rationale}</p>
                <div className="mt-4 grid grid-cols-2 gap-4 text-sm text-slate-500 dark:text-slate-400">
                  <div>Catalyst: {decision.expected_catalyst_timeframe ?? "n/a"}</div>
                  <div>Expected return: {decision.expected_return == null ? "n/a" : `${decision.expected_return}%`}</div>
                </div>
                {decision.reviews.length ? (
                  <div className="mt-5 space-y-3 border-t border-slate-200 dark:border-slate-800 pt-4">
                    {decision.reviews.map((review) => (
                      <div key={review.id} className="rounded-lg border border-slate-200 dark:border-slate-800 p-4">
                        <p className="font-medium">{review.outcome_assessment}</p>
                        <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">{new Date(review.reviewed_at).toLocaleString()}</p>
                        {review.what_went_wrong ? <p className="mt-2 text-sm text-slate-700 dark:text-slate-300">Wrong: {review.what_went_wrong}</p> : null}
                        {review.what_to_improve ? <p className="mt-1 text-sm text-slate-700 dark:text-slate-300">Improve: {review.what_to_improve}</p> : null}
                        {review.extracted_lessons.length ? (
                          <div className="mt-3 space-y-2">
                            <p className="text-xs font-medium uppercase tracking-wider text-slate-500 dark:text-slate-400">
                              Extracted lessons
                            </p>
                            {review.extracted_lessons.map((lesson) => (
                              <div key={lesson.id} className="rounded-lg border border-slate-200 px-3 py-2 text-sm dark:border-slate-800">
                                <p className="font-medium">{lesson.title}</p>
                                <p className="mt-1 text-slate-500 dark:text-slate-400">{lesson.summary}</p>
                              </div>
                            ))}
                          </div>
                        ) : null}
                      </div>
                    ))}
                  </div>
                ) : null}
                {reviewingId === decision.id ? (
                  <div className="mt-5 space-y-3 border-t border-slate-200 dark:border-slate-800 pt-4">
                    <select value={reviewForm.outcome_assessment} onChange={(e) => setReviewForm((current) => ({ ...current, outcome_assessment: e.target.value }))} className={inputClass}>
                      <option value="correct_for_right_reason">Correct For Right Reason</option>
                      <option value="correct_for_wrong_reason">Correct For Wrong Reason</option>
                      <option value="wrong_for_right_reason">Wrong For Right Reason</option>
                      <option value="wrong_for_wrong_reason">Wrong For Wrong Reason</option>
                    </select>
                    <input value={reviewForm.actual_return} onChange={(e) => setReviewForm((current) => ({ ...current, actual_return: e.target.value }))} type="number" step="0.01" placeholder="Actual return %" className={inputClass} />
                    <select value={reviewForm.mistake_preventable} onChange={(e) => setReviewForm((current) => ({ ...current, mistake_preventable: e.target.value }))} className={inputClass}>
                      <option value="unknown">Preventable?</option>
                      <option value="true">Yes</option>
                      <option value="false">No</option>
                    </select>
                    <textarea value={reviewForm.what_went_right} onChange={(e) => setReviewForm((current) => ({ ...current, what_went_right: e.target.value }))} rows={2} placeholder="What went right" className={inputClass} />
                    <textarea value={reviewForm.what_went_wrong} onChange={(e) => setReviewForm((current) => ({ ...current, what_went_wrong: e.target.value }))} rows={2} placeholder="What went wrong" className={inputClass} />
                    <textarea value={reviewForm.what_to_improve} onChange={(e) => setReviewForm((current) => ({ ...current, what_to_improve: e.target.value }))} rows={2} placeholder="What to improve next time" className={inputClass} />
                    <button onClick={() => void createReview(decision.id)} disabled={saving} className="w-full rounded-lg border border-slate-300 dark:border-slate-700 px-4 py-3 text-sm disabled:opacity-50">
                      Save review
                    </button>
                  </div>
                ) : null}
              </article>
            ))
          )}
        </section>
      </main>
    </div>
  );
}

const inputClass =
  "w-full rounded-lg border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-900 px-4 py-3";
