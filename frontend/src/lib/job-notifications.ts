export const ACTIVE_JOB_STORAGE_KEY = "prophet:active-chat-job";
export const COMPLETED_JOB_NOTICE_KEY = "prophet:completed-chat-job";
export const JOB_NOTICE_CLEARED_EVENT = "prophet:chat-notice-cleared";

export function clearActiveJobReference(jobId: string) {
  if (typeof window === "undefined") return;
  const raw = window.sessionStorage.getItem(ACTIVE_JOB_STORAGE_KEY);
  if (!raw) return;
  try {
    if (JSON.parse(raw).job_id === jobId) window.sessionStorage.removeItem(ACTIVE_JOB_STORAGE_KEY);
  } catch { window.sessionStorage.removeItem(ACTIVE_JOB_STORAGE_KEY); }
}

export function clearCompletedJobNotice() {
  if (typeof window === "undefined") return;
  window.sessionStorage.removeItem(COMPLETED_JOB_NOTICE_KEY);
  window.dispatchEvent(new Event(JOB_NOTICE_CLEARED_EVENT));
}
