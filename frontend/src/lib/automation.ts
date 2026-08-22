import { AutomationStatus } from "@/lib/api";

type AutomationJob = AutomationStatus["jobs"][number];

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
  if (!job.enabled || job.last_status === "disabled") {
    return { label, detail: "disabled", tone: "idle" };
  }
  if (job.last_status === "error") {
    return { label, detail: job.detail || "last run failed", tone: "warn" };
  }
  if (job.last_run_at) {
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
