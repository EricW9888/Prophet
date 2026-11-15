"use client";

import { useEffect, useState } from "react";
import type { AgentTurnJob } from "@/lib/api";
import { chatActivity, elapsedLabel, visibleWorkEvents } from "@/lib/work-status";
import { formatUserLabel } from "@/lib/formatting";
import ActivityIndicator from "@/components/ActivityIndicator";

export function ElapsedTime({ since }: { since?: string | null }) {
  const [fallback] = useState(() => Date.now());
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  const started = since ? Date.parse(since) : fallback;
  return <span className="tabular-nums">{Number.isFinite(started) ? `${elapsedLabel((now - started) / 1000)} elapsed` : "Elapsed time unavailable"}</span>;
}

function eventTime(value: string) {
  return Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "Time unknown";
}

export default function ChatTurnStatus({ job, connectionError, requestMessage }: { job?: AgentTurnJob | null; connectionError?: string | null; requestMessage?: string | null }) {
  const activity = chatActivity(job, connectionError);
  const events = visibleWorkEvents(job);
  return <section className="w-full py-3 text-sm" aria-label="Research activity">
    {requestMessage ? <p className="mb-4 ml-auto w-fit max-w-[90%] whitespace-pre-wrap break-words border border-line bg-panel-muted px-4 py-2.5 text-foreground">{requestMessage}</p> : null}
    <div className="flex items-start gap-3">
      <ActivityIndicator state={activity.state} className="mt-0.5" />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
          <p role="status" aria-atomic="true" className="font-medium">{activity.label}<span className="sr-only">. {activity.detail}</span></p>
          {job || !connectionError ? <span className="text-xs text-muted"><ElapsedTime since={job?.created_at} /></span> : null}
        </div>
        <p className="mt-1 leading-6 text-muted">{activity.detail}</p>
        {job?.status === "deferred" && job.retry_at ? <p className="mt-1 text-xs leading-5 text-muted">{Number.isFinite(Date.parse(job.retry_at)) ? `Next availability check after ${eventTime(job.retry_at)}.` : "The next availability check time is unknown."} This is a service check, not an answer completion time.</p> : null}
        {events.length ? <details className="mt-2 text-xs">
          <summary className="w-fit cursor-pointer py-1 text-muted hover:text-foreground">Work details</summary>
          <ol className="mt-2 max-h-64 space-y-3 overflow-y-auto border-l border-line pl-3">
            {events.map((event, index) => {
              const detail = event.detail ?? event.metadata;
              return <li key={`${event.created_at}:${index}`}>
                <div className="flex items-baseline justify-between gap-3"><span className="font-medium">{formatUserLabel(event.phase)}</span><time className="shrink-0 text-muted" dateTime={event.created_at}>{eventTime(event.created_at)}</time></div>
                <p className="mt-0.5 break-words leading-5 text-muted">{event.message}</p>
                {typeof detail?.query === "string" ? <p className="mt-1 break-words text-muted">Search: {detail.query}</p> : null}
                {typeof detail?.direct_evidence_count === "number" ? <p className="mt-1 text-muted">{detail.direct_evidence_count} direct evidence items · {Number(detail.contradiction_evidence_count ?? 0)} contradictory</p> : null}
                {Array.isArray(detail?.holdings_with_evidence) && typeof detail?.holding_count === "number" ? <p className="mt-1 text-muted">{detail.holdings_with_evidence.length} of {detail.holding_count} holdings linked in this packet</p> : null}
              </li>;
            })}
          </ol>
        </details> : null}
      </div>
    </div>
  </section>;
}
