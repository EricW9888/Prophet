// These tones describe a processing stage, not acceptance of an investment claim.
export function evidenceProcessingTone(status?: string | null): "neutral" | "good" | "bad" | "warning" {
  if (["complete", "completed"].includes(status ?? "")) return "good";
  if (["retry_exhausted", "blocked_missing_content", "missing", "blocked", "failed"].includes(status ?? "")) return "bad";
  if (["quarantined", "retry_scheduled", "transcript_available", "provider_deferred", "deferred"].includes(status ?? "")) return "warning";
  return "neutral";
}
