"use client";

import { useState } from "react";
import Link from "next/link";
import { apiFetch, type ReviewQueueItem } from "@/lib/api";
import { researchHref, reviewChallengeHref, reviewRecordLink, reviewResearchQuestion, reviewSubject } from "@/lib/workflows";

/** Verdicts for a source claim, in the values the claim record stores. */
export const CLAIM_VERDICTS = [
  { value: "correct", label: "Correct" },
  { value: "partially_correct", label: "Partly correct" },
  { value: "incorrect", label: "Incorrect" },
  { value: "indeterminate", label: "Can't tell" },
] as const;

/** Where a review item's own fix happens, when it is not on this page. */
export function reviewFixLink(item: Pick<ReviewQueueItem, "item_type">): { href: string; label: string } | null {
  if (item.item_type === "mailbox_identity") return { href: "/settings?tab=data", label: "Open receipt comparison" };
  return null;
}

export default function ReviewItemActions({ item, onChallenge, onResolved, disabled = false }: {
  item: ReviewQueueItem;
  onChallenge?: () => void;
  /** Called after a verdict is stored, so the queue can refresh. */
  onResolved?: () => void;
  disabled?: boolean;
}) {
  const [saving, setSaving] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const record = reviewRecordLink(item);
  const subject = item.review_domain === "portfolio_data" ? null : reviewSubject(item);
  const fix = reviewFixLink(item);
  const style = "border border-line px-3 py-2 text-xs font-medium text-action hover:border-line-strong";

  async function recordVerdict(value: string) {
    setSaving(value);
    setError(null);
    try {
      // Same endpoint and stored values as Prophet's own claim checker; the source's
      // performance history is recomputed from it.
      await apiFetch(`/sources/claim-records/${item.item_id}/assessment`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ assessment: value, notes: "Assessed by the owner from the review queue." }),
      });
      onResolved?.();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The verdict could not be saved.");
    } finally {
      setSaving(null);
    }
  }

  return (
    <div className="mt-3 space-y-3">
      {item.item_type === "source_claim_record" ? (
        <div role="group" aria-label="Record how this claim turned out" className="flex flex-wrap items-center gap-2">
          <span className="text-xs text-muted">How did it turn out?</span>
          {CLAIM_VERDICTS.map((verdict) => (
            <button key={verdict.value} type="button" disabled={disabled || saving !== null}
              onClick={() => void recordVerdict(verdict.value)}
              className="workspace-control disabled:opacity-50">
              {saving === verdict.value ? "Saving…" : verdict.label}
            </button>
          ))}
          {error ? <p role="alert" className="w-full text-xs text-negative">{error}</p> : null}
        </div>
      ) : null}
      <div className="flex flex-wrap gap-2">
        {fix && <Link href={fix.href} className={style}>{fix.label}</Link>}
        {record && <Link href={record.href} className={style}>{record.label}</Link>}
        {subject && <Link href={researchHref(subject, reviewResearchQuestion(item))} className={style}>Investigate this concern</Link>}
        {subject && (onChallenge
          ? <button type="button" disabled={disabled} onClick={onChallenge} className={`${style} disabled:opacity-50`}>Prepare a challenge</button>
          : <Link href={reviewChallengeHref(item)} className={style}>Prepare a challenge</Link>)}
        {!record && !subject && !fix && <p className="text-xs text-muted">The related record is unavailable. The retained concern remains visible.</p>}
      </div>
    </div>
  );
}
