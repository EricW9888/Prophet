"use client";

import Link from "next/link";
import { useContext, useEffect, useRef } from "react";
import { ArrowUpRight, ChevronDown, RefreshCw } from "lucide-react";
import { WorkStatusContext } from "@/components/WorkStatusProvider";
import ActivityIndicator from "@/components/ActivityIndicator";
import { chatActivity, workspaceActivity } from "@/lib/work-status";
import { formatUserLabel } from "@/lib/formatting";

export default function WorkspaceWorkIndicator() {
  const work = useContext(WorkStatusContext);
  const disclosure = useRef<HTMLDetailsElement>(null);
  useEffect(() => {
    const dismiss = (event: PointerEvent) => { if (!disclosure.current?.contains(event.target as Node)) disclosure.current?.removeAttribute("open"); };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape" && disclosure.current?.open) { disclosure.current.removeAttribute("open"); disclosure.current.querySelector("summary")?.focus(); } };
    document.addEventListener("pointerdown", dismiss);
    document.addEventListener("keydown", escape);
    return () => { document.removeEventListener("pointerdown", dismiss); document.removeEventListener("keydown", escape); };
  }, []);
  if (!work) return null;
  const status = workspaceActivity(work.jobs, work.automation, work.jobsReady && work.automationReady, work.jobsError || work.automationError);
  const running = work.automation?.jobs.filter((job) => job.is_running) ?? [];
  const waiting = work.automation?.jobs.filter((job) => job.enabled && job.last_status === "deferred" && !job.is_running) ?? [];
  const attention = work.automation?.jobs.filter((job) => job.enabled && !job.is_running && ["error", "waiting_for_config"].includes(job.last_status)) ?? [];
  return <details ref={disclosure} className="relative shrink-0">
    <summary className="flex min-h-9 max-w-40 cursor-pointer list-none items-center gap-2 border border-line px-2.5 text-xs font-medium hover:bg-panel-muted sm:max-w-none [&::-webkit-details-marker]:hidden" aria-label={`Workspace activity: ${status.label}`}>
      <ActivityIndicator state={status.state} />
      <span className="min-w-0 truncate">{status.label}</span>
      <ChevronDown className="h-3 w-3 text-muted" aria-hidden="true" />
    </summary>
    <section aria-label="Workspace activity" className="fixed inset-x-3 top-14 z-[80] max-h-[75dvh] overflow-y-auto border border-line-strong bg-panel p-4 shadow-xl sm:absolute sm:inset-x-auto sm:right-0 sm:top-[calc(100%+0.5rem)] sm:w-96">
      <div className="flex items-center justify-between gap-3"><h2 className="text-sm font-semibold">Activity</h2><button type="button" className="workspace-control" disabled={work.refreshing} onClick={() => void work.refresh()} aria-label="Refresh activity"><RefreshCw className={`h-3.5 w-3.5 ${work.refreshing ? "motion-safe:animate-spin" : ""}`} /></button></div>
      {work.jobsError || work.automationError ? <p role="status" className="mt-3 text-sm text-amber-700 dark:text-amber-400">Activity could not be refreshed. Previously loaded items may be out of date.</p> : null}
      <h3 className="mt-4 text-xs font-semibold text-muted">Your chats{work.jobs.length ? ` · ${work.jobs.length}${work.jobs.length === 30 ? "+" : ""}` : ""}</h3>
      {work.jobs.length ? <ul className="mt-2 divide-y divide-line">{work.jobs.map((job) => {
        const activity = chatActivity(job);
        return <li key={job.job_id}><Link href={job.session_id ? `/chat?session_id=${job.session_id}` : "/chat"} className="block py-3 hover:text-action-strong" onClick={() => disclosure.current?.removeAttribute("open")}><span className="flex items-center gap-2 text-sm"><ActivityIndicator state={work.jobsError ? "reconnecting" : activity.state} /><span>{work.jobsError ? `Last known: ${activity.label}` : activity.label}</span><ArrowUpRight className="ml-auto h-3 w-3 shrink-0" /></span><span className="mt-1 block line-clamp-2 break-words text-xs text-muted">{job.request_message}</span></Link></li>;
      })}</ul> : <p className="mt-2 text-sm text-muted">{work.jobsError ? "Chat activity is unavailable." : work.jobsReady ? "No active chat questions." : "Checking chat questions…"}</p>}
      <h3 className="mt-4 text-xs font-semibold text-muted">Background work</h3>
      {running.length ? <ul className="mt-2 space-y-2">{running.map((job) => <li key={job.name} className="flex items-center gap-2 text-sm"><ActivityIndicator state={work.automationError ? "reconnecting" : "working"} />{work.automationError ? "Last known: " : ""}{formatUserLabel(job.name)}</li>)}</ul> : <p className="mt-2 text-sm text-muted">{work.automationError ? "Background activity is unavailable." : !work.automationReady ? "Checking background work…" : work.automation?.automation_enabled ? "Enabled · no job running right now." : "Background work is off."}</p>}
      {attention.length ? <ul className="mt-3 space-y-2">{attention.map((job) => <li key={job.name} className="flex items-start gap-2 text-sm"><ActivityIndicator state="error" /><span>{formatUserLabel(job.name)}<span className="block text-xs text-muted">{job.last_status === "waiting_for_config" ? "Needs setup" : "Last attempt failed"}</span></span></li>)}</ul> : null}
      {waiting.length ? <p className="mt-3 text-xs text-muted">{waiting.length} background {waiting.length === 1 ? "job is" : "jobs are"} waiting for {work.automation?.provider_health && ["open", "probing", "unobserved", "waiting_for_config"].includes(work.automation.provider_health.state) ? "the AI service" : "required inputs"}. Work that can run independently may continue.</p> : null}
      <div className="mt-4 flex flex-wrap items-center justify-between gap-2 border-t border-line pt-3 text-xs"><Link href="/activity" className="text-action-strong underline" onClick={() => disclosure.current?.removeAttribute("open")}>Open activity</Link>{work.checkedAt ? <span className="text-muted">Last check {new Date(work.checkedAt).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}</span> : null}</div>
    </section>
  </details>;
}
