"use client";

import Link from "next/link";
import { useContext, useEffect, useRef, useState } from "react";
import { WorkStatusContext } from "@/components/WorkStatusProvider";

import { apiFetch, AgentTurnJob } from "@/lib/api";
import { isMissingChatJob } from "@/lib/work-status";
import { ACTIVE_JOB_STORAGE_KEY, COMPLETED_JOB_NOTICE_KEY, JOB_NOTICE_CLEARED_EVENT, clearActiveJobReference, clearCompletedJobNotice } from "@/lib/job-notifications";

type NoticeState = {
  message: string;
  sessionId?: string | null;
  jobId?: string;
};

export default function JobCompletionNoticeHost() {
  const [notice, setNotice] = useState<NoticeState | null>(null);
  const terminalJobId = useRef<string | null>(null);
  const work = useContext(WorkStatusContext);
  const foregroundJobId = useRef<string | null>(null);
  useEffect(() => { foregroundJobId.current = work?.foregroundJobId ?? null; }, [work?.foregroundJobId]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    let disposed = false;
    let inFlight = false;
    let request: AbortController | null = null;
    const clearNotice = () => setNotice(null);
    window.addEventListener(JOB_NOTICE_CLEARED_EVENT, clearNotice);

    const existing = window.sessionStorage.getItem(COMPLETED_JOB_NOTICE_KEY);
    if (existing) {
      setTimeout(() => {
        try {
          const current = window.sessionStorage.getItem(COMPLETED_JOB_NOTICE_KEY);
          if (!current) return;
          const saved = JSON.parse(current) as NoticeState;
          terminalJobId.current = saved.jobId ?? null;
          setNotice(saved);
        } catch {
          window.sessionStorage.removeItem(COMPLETED_JOB_NOTICE_KEY);
        }
      }, 0);
    }

    const interval = window.setInterval(() => {
      const raw = window.sessionStorage.getItem(ACTIVE_JOB_STORAGE_KEY);
      if (!raw || inFlight) return;
      try {
        const active = JSON.parse(raw) as { job_id?: string; request_message?: string | null; session_id?: string | null };
        if (!active.job_id || active.job_id === terminalJobId.current || active.job_id === foregroundJobId.current) return;
        inFlight = true;
        request = new AbortController();
        void apiFetch<AgentTurnJob>(`/agent/turn-jobs/${active.job_id}`, { signal: request.signal })
          .then((job) => {
            if (disposed) return;
            const current = window.sessionStorage.getItem(ACTIVE_JOB_STORAGE_KEY);
            if (!current || JSON.parse(current).job_id !== job.job_id) return;
            if (job.status === "cancelled") {
              terminalJobId.current = job.job_id;
              window.sessionStorage.removeItem(ACTIVE_JOB_STORAGE_KEY);
              return;
            }
            if (job.status !== "completed" && job.status !== "error") {
              return;
            }
            terminalJobId.current = job.job_id;
            if (job.status === "completed") window.sessionStorage.removeItem(ACTIVE_JOB_STORAGE_KEY);
            const nextNotice: NoticeState = {
              message:
                job.status === "completed"
                  ? "Prophet finished the latest analysis."
                  : "Prophet stopped on an error. Open chat to inspect it.",
              // Error recovery reads the retained job reference on the chat root.
              sessionId: job.status === "error" ? null : job.session_id ?? active.session_id ?? null,
              jobId: job.job_id,
            };
            window.sessionStorage.setItem(COMPLETED_JOB_NOTICE_KEY, JSON.stringify(nextNotice));
            setNotice(nextNotice);
          })
          .catch((error) => { if (!disposed && isMissingChatJob(error)) clearActiveJobReference(active.job_id!); })
          .finally(() => { inFlight = false; request = null; });
      } catch {
        window.sessionStorage.removeItem(ACTIVE_JOB_STORAGE_KEY);
      }
    }, 2500);
    return () => {disposed = true; request?.abort(); window.clearInterval(interval); window.removeEventListener(JOB_NOTICE_CLEARED_EVENT, clearNotice);};
  }, []);

  if (!notice) {
    return null;
  }

  return (
    <div className="pointer-events-none fixed inset-x-3 top-16 z-40 flex justify-end">
      <div className="pointer-events-auto flex w-full max-w-xl flex-wrap items-center justify-between gap-3 border border-line-strong bg-panel px-4 py-3 shadow-xl">
        <div>
          <p role="status" className="text-sm font-medium text-foreground">{notice.message}</p>
          <p className="mt-1 text-xs text-muted">Open the chat to review the result.</p>
        </div>
        <div className="flex items-center gap-2">
          <Link
            href={notice.sessionId ? `/chat?session_id=${notice.sessionId}` : "/chat"}
            onClick={() => clearCompletedJobNotice()}
            className="workspace-control text-action-strong"
          >
            Open chat
          </Link>
          <button
            type="button"
            onClick={() => {
              if (typeof window !== "undefined") {
                clearCompletedJobNotice();
              }
              setNotice(null);
            }}
            className="workspace-control"
          >
            Dismiss
          </button>
        </div>
      </div>
    </div>
  );
}
