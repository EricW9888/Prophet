import type { AgentTurnJob, AutomationStatus } from "./api";
import { ApiError } from "./api";

export type ActivityState = "working" | "queued" | "waiting" | "reconnecting" | "idle" | "error" | "completed" | "cancelled";

export function isActiveChatJob(job: Pick<AgentTurnJob, "status">) {
  return ["queued", "running", "deferred"].includes(job.status);
}

export function isMissingChatJob(error: unknown) {
  return error instanceof ApiError && error.status === 404 && error.message === "job_not_found";
}

export function sameChatJob(first: AgentTurnJob | null, second: AgentTurnJob | null) {
  return first === second || JSON.stringify(first) === JSON.stringify(second);
}

export function visibleWorkEvents(job?: AgentTurnJob | null) {
  return job?.events.filter((event) => event.phase !== "thinking") ?? [];
}

const phaseLabels: Record<string, string> = {
  queued: "Queued", running: "Starting", routing: "Understanding your question",
  planning: "Planning research", retrieval: "Reading evidence", research: "Checking sources",
  reasoning: "Analyzing", synthesis: "Combining findings", perspectives: "Comparing perspectives", review: "Reviewing evidence",
  answering: "Preparing answer", actions: "Applying enabled actions", completed: "Complete",
};

export function chatActivity(job?: AgentTurnJob | null, connectionError?: string | null) {
  if (connectionError) return { state: "reconnecting" as const, label: "Reconnecting", detail: connectionError };
  if (!job) return { state: "working" as const, label: "Sending question", detail: "Sending your request to Prophet." };
  if (job.status === "deferred") return { state: "waiting" as const, label: "Waiting for AI", detail: "Completed work is saved. This question will resume when the AI service is available." };
  if (job.status === "queued") return { state: "queued" as const, label: "Queued", detail: "Your question is saved and waiting to start." };
  if (job.status === "cancelled") return { state: "cancelled" as const, label: "Response stopped", detail: "" };
  if (job.status === "error") return { state: "error" as const, label: "Needs attention", detail: job.error || "This question could not finish." };
  if (job.status === "completed") return { state: "completed" as const, label: "Complete", detail: "The response is ready." };
  const latest = visibleWorkEvents(job).at(-1);
  return { state: "working" as const, label: phaseLabels[latest?.phase ?? "running"] ?? "Working", detail: latest?.message || "Prophet is working on your question." };
}

export function elapsedLabel(seconds: number) {
  if (!Number.isFinite(seconds)) return "Time unavailable";
  const total = Math.max(0, Math.floor(seconds));
  if (total < 60) return `${total}s`;
  if (total < 3600) return `${Math.floor(total / 60)}m ${total % 60}s`;
  return `${Math.floor(total / 3600)}h ${Math.floor((total % 3600) / 60)}m`;
}

export function workspaceActivity(jobs: AgentTurnJob[], automation: AutomationStatus | null, ready: boolean, unavailable: boolean) {
  if (unavailable) return { state: "reconnecting" as ActivityState, label: "Status unavailable" };
  if (!ready) return { state: "working" as ActivityState, label: "Checking activity" };
  if (automation?.jobs.some((job) => typeof job.is_running !== "boolean")) return { state: "reconnecting" as ActivityState, label: "Status unavailable" };
  const running = (automation?.jobs ?? []).filter((job) => job.is_running);
  if (jobs.some((job) => job.status === "running") || running.length) {
    return { state: "working" as ActivityState, label: !jobs.length && running.length === 1 && running[0].name === "provider_health_probe" ? "Checking AI" : "Working" };
  }
  if (jobs.some((job) => job.status === "queued")) return { state: "queued" as ActivityState, label: "Queued" };
  const waiting = automation?.jobs.some((job) => job.enabled && job.last_status === "deferred");
  if (jobs.some((job) => job.status === "deferred") || (waiting && automation?.provider_health && ["open", "probing", "unobserved", "waiting_for_config"].includes(automation.provider_health.state))) return { state: "waiting" as ActivityState, label: "Waiting for AI" };
  if (waiting) return { state: "waiting" as ActivityState, label: "Waiting for inputs" };
  if (automation?.jobs.some((job) => job.enabled && ["error", "waiting_for_config"].includes(job.last_status))) return { state: "error" as ActivityState, label: "Needs attention" };
  return { state: "idle" as ActivityState, label: "Standing by" };
}
