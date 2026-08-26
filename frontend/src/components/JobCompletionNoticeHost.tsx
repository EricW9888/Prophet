"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { apiFetch, AgentTurnJob } from "@/lib/api";
import { ACTIVE_JOB_STORAGE_KEY, COMPLETED_JOB_NOTICE_KEY } from "@/lib/job-notifications";

type NoticeState = {
  message: string;
  sessionId?: string | null;
};

export default function JobCompletionNoticeHost() {
  const [notice, setNotice] = useState<NoticeState | null>(null);

  useEffect(() => {
    if (typeof window === "undefined") return;

    const existing = window.sessionStorage.getItem(COMPLETED_JOB_NOTICE_KEY);
    if (existing) {
      setTimeout(() => {
        try {
          setNotice(JSON.parse(existing) as NoticeState);
        } catch {
          window.sessionStorage.removeItem(COMPLETED_JOB_NOTICE_KEY);
        }
      }, 0);
    }

    const interval = window.setInterval(() => {
      const raw = window.sessionStorage.getItem(ACTIVE_JOB_STORAGE_KEY);
      if (!raw) return;
      try {
        const active = JSON.parse(raw) as { job_id?: string; request_message?: string | null; session_id?: string | null };
        if (!active.job_id) return;
        void apiFetch<AgentTurnJob>(`/agent/turn-jobs/${active.job_id}`)
          .then((job) => {
            if (job.status !== "completed" && job.status !== "error") {
              return;
            }
            window.sessionStorage.removeItem(ACTIVE_JOB_STORAGE_KEY);
            const nextNotice: NoticeState = {
              message:
                job.status === "completed"
                  ? "Prophet finished the latest analysis."
                  : "Prophet stopped on an error. Open chat to inspect it.",
              sessionId: job.session_id ?? active.session_id ?? null,
            };
            window.sessionStorage.setItem(COMPLETED_JOB_NOTICE_KEY, JSON.stringify(nextNotice));
            setNotice(nextNotice);
          })
          .catch(() => undefined);
      } catch {
        window.sessionStorage.removeItem(ACTIVE_JOB_STORAGE_KEY);
      }
    }, 2500);
    return () => window.clearInterval(interval);
  }, []);

  if (!notice) {
    return null;
  }

  return (
    <div className="fixed inset-x-4 bottom-6 z-50 flex justify-center pointer-events-none">
      <div className="pointer-events-auto flex w-full max-w-xl items-center justify-between gap-4 rounded-lg border border-sky-200 bg-white/95 px-4 py-3 shadow-xl backdrop-blur dark:border-sky-900 dark:bg-slate-950/95">
        <div>
          <p className="text-sm font-medium text-slate-900 dark:text-slate-100">{notice.message}</p>
          <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">You can jump straight back into the reasoning thread.</p>
        </div>
        <div className="flex items-center gap-2">
          <Link
            href={notice.sessionId ? `/chat?session_id=${notice.sessionId}` : "/chat"}
            className="rounded-full bg-sky-600 px-3 py-2 text-xs font-semibold text-white"
          >
            Open chat
          </Link>
          <button
            type="button"
            onClick={() => {
              if (typeof window !== "undefined") {
                window.sessionStorage.removeItem(COMPLETED_JOB_NOTICE_KEY);
              }
              setNotice(null);
            }}
            className="rounded-full border border-slate-300 px-3 py-2 text-xs text-slate-600 dark:border-slate-700 dark:text-slate-300"
          >
            Dismiss
          </button>
        </div>
      </div>
    </div>
  );
}
