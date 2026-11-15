import type { ReasoningTrace } from "./api";

export const record = (value: unknown): Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
export const text = (value: unknown): string => typeof value === "string" ? value : "";
export const shortText = (value: unknown, limit: number) => {
  const valueText = text(value);
  return valueText.length > limit ? `${valueText.slice(0, limit - 1)}…` : valueText;
};

/** A bounded, unsent research question; it never promotes the proposal. */
export function patternInvestigationHref(trace: ReasoningTrace): string {
  const receipt = trace.structured_output_json;
  const snapshot = record(receipt.snapshot);
  const hypothesis = record(record(receipt.proposal).hypothesis);
  const input = record(snapshot.input);
  const refs = Array.isArray(hypothesis.evidence_refs) ? hypothesis.evidence_refs : [];
  const sources = (Array.isArray(input.source_dated_signals) ? input.source_dated_signals : [])
    .map(record).filter(source => refs.includes(source.ref));
  const sourceText = sources.map(source =>
    `${shortText(source.ref, 60)} | ${shortText(source.evidence_url || source.source_url, 220)} | publication: ${shortText(source.public_time, 40) || "unknown"} | signal excerpt: ${shortText(source.setup_context, 230)}`
  ).join("\n");
  const question = [
    "Investigate this UNVERIFIED model proposal. Establish the premise, seek contrary evidence, explain the economic transmission and what expectations already reflect, then propose a testable next check. Do not treat model confidence or coherence as corroboration.",
    `Receipt: ${trace.id}. Disposition: ${shortText(record(receipt.review).detail, 160)}. Captured: ${text(snapshot.captured_at)}. This is not proof of pre-market availability. Full capture: /opportunities/patterns#${trace.id}`,
    `Proposal: ${shortText(hypothesis.label, 220)}`,
    `Observation: ${shortText(hypothesis.observation, 500)}`,
    `Mechanism: ${shortText(hypothesis.proposed_mechanism, 800)}`,
    `Why now: ${shortText(hypothesis.why_now, 400)}`,
    `Falsifier: ${shortText(hypothesis.falsifier, 500)}`,
    `Next test: ${shortText(hypothesis.next_test, 600)}`,
    `Captured cited signal excerpts (not independently verified facts):\n${shortText(sourceText, 1600) || "No matching source references."}`,
    "Use the linked full receipt for the complete packet; this draft includes bounded excerpts only. Distinguish established facts, conditional implications, missing evidence and possible action. Do not imply that any portfolio action has been taken.",
  ].join("\n\n");
  return `/chat?${new URLSearchParams({ q: question })}`;
}
