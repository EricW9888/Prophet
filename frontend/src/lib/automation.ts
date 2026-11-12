import type { AutomationStatus } from "@/lib/api";

type AutomationJob = AutomationStatus["jobs"][number];

export function automationJobPresentation(job: AutomationJob): { tone: "ok" | "warn" | "error" | "idle"; meaning: string } {
  const states = {
    disabled: { tone: "idle", meaning: "Disabled by runtime settings." },
    error: { tone: "error", meaning: "Last attempt failed; inspect the cause before retrying." },
    warning: { tone: "warn", meaning: "Some work remains incomplete or needs attention." },
    waiting_for_config: { tone: "warn", meaning: "Waiting for a required setting before work can run." },
    deferred: { tone: "warn", meaning: "Work is deferred. Its prerequisite must recover before it can proceed." },
    rejected: { tone: "warn", meaning: "The result was rejected; no accepted conclusion is implied." },
    idle: { tone: "idle", meaning: "Waiting for eligible work or the next check." },
    running: { tone: "idle", meaning: "Work is in progress; no completed result yet." },
    cancelled: { tone: "idle", meaning: "Work was interrupted; no completed result is implied." },
    skipped: { tone: "idle", meaning: "No work was performed on this check." },
    no_new_evidence: { tone: "idle", meaning: "The check found no new evidence." },
    no_new_knowledge: { tone: "idle", meaning: "The check produced no new accepted knowledge." },
    ok: { tone: "ok", meaning: "Last run completed. Inspect its recorded result for what changed." },
    unknown: { tone: "idle", meaning: "Status is unavailable; completion has not been verified." },
  } as const;
  return states[automationJobOutcome(job)];
}

export function automationOverallHealth(enabled: boolean, jobs: AutomationJob[]) {
  if (!enabled) return "Disabled";
  const active = jobs.filter(job => job.enabled);
  if (active.some(job => ["error", "warning", "partial", "busy", "waiting_for_config", "deferred", "rejected"].includes(job.last_status))) return "Needs review";
  if (active.some(job => automationJobOutcome(job) === "unknown")) return "Unknown";
  if (!active.length || active.some(job => !job.last_run_at)) return "Starting";
  return "Operational";
}

export function automationJobOutcome(job: AutomationJob) {
  if (!job.enabled || job.last_status === "disabled") return "disabled";
  switch (job.last_status) {
    case "error":
    case "warning":
    case "waiting_for_config":
    case "idle":
    case "running":
    case "cancelled":
    case "ok":
    case "deferred":
    case "skipped":
    case "rejected":
    case "no_new_evidence":
    case "no_new_knowledge":
      return job.last_status;
    case "partial":
    case "busy":
      return "warning";
    default:
      return "unknown";
  }
}

export type AutomationHealth = {
  label: string;
  detail: string;
  tone: "ok" | "warn" | "idle";
};

export function automationJobHealth(
  jobs: AutomationJob[] | undefined,
  jobName: string,
  label: string,
): AutomationHealth {
  const job = jobs?.find((item) => item.name === jobName);
  if (!job) {
    return { label, detail: "status unavailable", tone: "idle" };
  }
  const outcome = automationJobOutcome(job);
  if (outcome === "disabled") {
    return { label, detail: "disabled", tone: "idle" };
  }
  if (outcome === "error") {
    return { label, detail: job.detail || "last run failed", tone: "warn" };
  }
  if (outcome === "warning" || outcome === "waiting_for_config" || outcome === "deferred" || outcome === "rejected") {
    return { label, detail: job.detail || "needs attention", tone: "warn" };
  }
  if (outcome === "running") {
    return { label, detail: "running", tone: "idle" };
  }
  if (outcome === "unknown") {
    return { label, detail: "status unavailable", tone: "idle" };
  }
  if (outcome === "ok" && job.last_run_at) {
    return { label, detail: `last ran ${formatRelativeJobTime(job.last_run_at)}`, tone: "ok" };
  }
  return { label, detail: job.last_status === "idle" ? "waiting for first run" : job.last_status, tone: "idle" };
}

function formatRelativeJobTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "at an unknown time";
  const diffMs = Date.now() - date.getTime();
  const diffMinutes = Math.max(0, Math.round(diffMs / 60000));
  if (diffMinutes < 1) return "just now";
  if (diffMinutes < 60) return `${diffMinutes}m ago`;
  const diffHours = Math.round(diffMinutes / 60);
  if (diffHours < 48) return `${diffHours}h ago`;
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
