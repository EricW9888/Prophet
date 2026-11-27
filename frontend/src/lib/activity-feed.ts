import { readableMachineText } from "@/lib/formatting";

export type Tone = "positive" | "caution" | "negative" | "neutral";

const STATUS_WORDS: Record<string, [string, Tone]> = {
  ok: ["Done", "positive"],
  deferred: ["Waiting", "caution"],
  pending: ["Pending", "neutral"],
  partial: ["Partly done", "caution"],
  processed_with_errors: ["Partly done", "caution"],
  warning: ["Needs a look", "caution"],
  error: ["Failed", "negative"],
  blocked: ["Blocked", "caution"],
  rejected: ["Rejected", "caution"],
  skipped: ["Skipped", "neutral"],
  empty_result: ["No usable result", "caution"],
  no_new_knowledge: ["Nothing new", "neutral"],
  no_new_evidence: ["Nothing new", "neutral"],
  retained_source_assessed: ["Checked, not answered yet", "neutral"],
  duplicate_recent_research: ["Already researched recently", "neutral"],
  research_provider_limit_exceeded: ["Search limit reached", "caution"],
  extraction_pending: ["Reading the source", "neutral"],
  resolution_assessment_deferred: ["Waiting", "caution"],
  resolution_provider_unavailable: ["Waiting for the AI provider", "caution"],
  retry_scheduled: ["Will retry", "caution"],
  answered: ["Answered", "positive"],
};

/** A status code in words, with the tone its colour should carry. */
export function activityStatus(status: string): { label: string; tone: Tone } {
  const known = STATUS_WORDS[status.toLowerCase()];
  if (known) return { label: known[0], tone: known[1] };
  const label = readableMachineText(status);
  return { label: label.charAt(0).toUpperCase() + label.slice(1), tone: /error|fail/.test(status) ? "negative" : "neutral" };
}

const TRIGGER_WORDS: Record<string, string> = {
  interval: "on schedule",
  manual: "you asked",
  provider_recovered: "the AI provider came back",
  evidence_ready: "evidence was ready to process",
  evidence_arrived: "new evidence arrived",
  evidence_backlog: "evidence was waiting",
  subject_refresh_ready: "a company's new evidence was ready",
  question_actionable: "a research question became ready",
};

/** Why a job ran, as the end of the sentence "Ran because …". */
export function activityTrigger(trigger: unknown): string | null {
  if (typeof trigger !== "string" || !trigger) return null;
  const key = trigger.startsWith("signal:") ? trigger.slice(7) : trigger;
  return TRIGGER_WORDS[key] ?? readableMachineText(key);
}

export type Repeated<T> = { item: T; count: number; earliest: string };

/**
 * Identical events (same kind, status and summary) within one list become one
 * entry at the latest time, with how many times it happened and since when.
 * Items arrive newest first.
 */
export function groupRepeats<T extends { timestamp: string }>(items: T[], key: (item: T) => string): Repeated<T>[] {
  const groups = new Map<string, Repeated<T>>();
  for (const item of items) {
    const k = key(item);
    const existing = groups.get(k);
    if (existing) {
      existing.count += 1;
      existing.earliest = item.timestamp;
    } else {
      groups.set(k, { item, count: 1, earliest: item.timestamp });
    }
  }
  return Array.from(groups.values());
}
