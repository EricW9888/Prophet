"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { apiFetch, type AgentTurnJob, type AgentTurnJobList, type AutomationStatus } from "@/lib/api";
import { isActiveChatJob } from "@/lib/work-status";

type WorkStatus = {
  jobs: AgentTurnJob[]; automation: AutomationStatus | null;
  jobsReady: boolean; automationReady: boolean;
  jobsError: boolean; automationError: boolean;
  checkedAt: number | null;
  refreshing: boolean;
  foregroundJobId: string | null;
  observeJob: (jobId: string | null) => void;
  refresh: () => Promise<void>;
  updateJob: (job: AgentTurnJob) => void;
  removeJob: (jobId: string) => void;
};

export const WorkStatusContext = createContext<WorkStatus | null>(null);
type ChatWorkStatus = Pick<WorkStatus, "jobs" | "refresh" | "updateJob" | "removeJob" | "observeJob">;
const ChatWorkStatusContext = createContext<ChatWorkStatus | null>(null);
export function useChatWorkStatus() {
  const value = useContext(ChatWorkStatusContext);
  if (!value) throw new Error("WorkStatusProvider is required for workspace activity.");
  return value;
}

export default function WorkStatusProvider({ children }: { children: React.ReactNode }) {
  const [jobs, setJobs] = useState<AgentTurnJob[]>([]);
  const [automation, setAutomation] = useState<AutomationStatus | null>(null);
  const [jobsReady, setJobsReady] = useState(false);
  const [automationReady, setAutomationReady] = useState(false);
  const [jobsError, setJobsError] = useState(false);
  const [automationError, setAutomationError] = useState(false);
  const [checkedAt, setCheckedAt] = useState<number | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [foregroundJobId, observeJob] = useState<string | null>(null);
  const jobsRef = useRef<AgentTurnJob[]>([]);
  const revisions = useRef(new Map<string, number>());
  const revision = useRef(0);
  const inFlight = useRef<Promise<void> | null>(null);
  const controller = useRef<AbortController | null>(null);

  const commitJobs = useCallback((next: AgentTurnJob[]) => {
    const ordered = [...next].sort((first, second) => Date.parse(second.created_at) - Date.parse(first.created_at) || first.job_id.localeCompare(second.job_id));
    if (JSON.stringify(jobsRef.current) === JSON.stringify(ordered)) return false;
    jobsRef.current = ordered;
    setJobs(ordered);
    return true;
  }, []);

  const updateJob = useCallback((job: AgentTurnJob) => {
    const remaining = jobsRef.current.filter((item) => item.job_id !== job.job_id);
    const next = isActiveChatJob(job) ? [job, ...remaining] : remaining;
    if (commitJobs(next) || !isActiveChatJob(job)) revisions.current.set(job.job_id, ++revision.current);
  }, [commitJobs]);

  const removeJob = useCallback((jobId: string) => {
    commitJobs(jobsRef.current.filter((job) => job.job_id !== jobId));
    revisions.current.set(jobId, ++revision.current);
  }, [commitJobs]);

  const refresh = useCallback(() => {
    if (inFlight.current) return inFlight.current;
    const request = new AbortController();
    controller.current = request;
    setRefreshing(true);
    const startedRevision = revision.current;
    const pending = Promise.allSettled([
      apiFetch<AgentTurnJobList>("/agent/turn-jobs?status=active&limit=30", { signal: request.signal }),
      apiFetch<AutomationStatus>("/automation/status", { signal: request.signal }),
    ]).then(([chat, background]) => {
      if (request.signal.aborted) return;
      if (chat.status === "fulfilled") {
        const protectedIds = new Set([...revisions.current].filter(([, value]) => value > startedRevision).map(([id]) => id));
        commitJobs([
          ...jobsRef.current.filter((job) => protectedIds.has(job.job_id)),
          ...chat.value.jobs.filter((job) => !protectedIds.has(job.job_id)),
        ]);
        setJobsReady(true);
      }
      setJobsError(chat.status === "rejected");
      if (background.status === "fulfilled") {
        setAutomation((previous) => JSON.stringify(previous) === JSON.stringify(background.value) ? previous : background.value);
        setAutomationReady(true);
      }
      setAutomationError(background.status === "rejected");
      setCheckedAt(Date.now());
    }).finally(() => {
      if (controller.current === request) controller.current = null;
      if (inFlight.current === pending) { inFlight.current = null; setRefreshing(false); }
    });
    inFlight.current = pending;
    return pending;
  }, [commitJobs]);

  useEffect(() => {
    let disposed = false;
    let generation = 0;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      const current = ++generation;
      await refresh();
      if (!disposed && current === generation) timer = setTimeout(poll, document.hidden ? 60000 : 5000);
    };
    const onVisible = () => {
      generation += 1;
      clearTimeout(timer);
      if (!document.hidden) void poll(); else timer = setTimeout(poll, 60000);
    };
    void poll();
    document.addEventListener("visibilitychange", onVisible);
    return () => { disposed = true; clearTimeout(timer); controller.current?.abort(); controller.current = null; inFlight.current = null; document.removeEventListener("visibilitychange", onVisible); };
  }, [refresh]);

  const value = useMemo(() => ({ jobs, automation, jobsReady, automationReady, jobsError, automationError, checkedAt, refreshing, foregroundJobId, observeJob, refresh, updateJob, removeJob }), [jobs, automation, jobsReady, automationReady, jobsError, automationError, checkedAt, refreshing, foregroundJobId, refresh, updateJob, removeJob]);
  const chats = useMemo(() => ({ jobs, refresh, updateJob, removeJob, observeJob }), [jobs, refresh, updateJob, removeJob]);
  return <WorkStatusContext.Provider value={value}><ChatWorkStatusContext.Provider value={chats}>{children}</ChatWorkStatusContext.Provider></WorkStatusContext.Provider>;
}
