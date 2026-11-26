import Link from "next/link";
import DeferredDisclosure from "@/components/DeferredDisclosure";
import { ShadowLifecycleCounts } from "@/lib/api";

const ALL_COUNTS = ["active", "queued", "running", "waiting", "due", "overdue", "completed", "failed", "superseded", "skipped", "manual"] as const;

/**
 * The lifecycle in one line, naming only states that hold runs. Overdue and
 * failed runs are called out separately because they need attention.
 */
export function shadowLifecycleLine(counts: ShadowLifecycleCounts): { status: string; attention: string | null } {
  const nonZero = (keys: readonly (keyof ShadowLifecycleCounts)[]) =>
    keys.filter(key => counts[key] > 0).map(key => `${counts[key]} ${key}`);
  const activeParts = nonZero(["waiting", "due"]);
  const active = counts.active > 0
    ? `${counts.active} active${activeParts.length ? ` (${activeParts.join(", ")})` : ""}`
    : "No active experiments";
  const finished = nonZero(["completed", "superseded", "skipped"]);
  const attention = nonZero(["overdue", "failed"]);
  return {
    status: [active, ...finished].join(" · "),
    attention: attention.length ? `Needs attention: ${attention.join(", ")}` : null,
  };
}

export default function ShadowLifecycleSummary({ counts, reviewLink = true }: {
  counts?: ShadowLifecycleCounts | null;
  /** Off on the Simulate page itself, where the link would point at the same page. */
  reviewLink?: boolean;
}) {
  if (!counts) return null;
  const { status, attention } = shadowLifecycleLine(counts);
  return (
    <section className="min-w-0 rounded-lg border border-line bg-panel p-4" aria-label="Shadow lifecycle">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-semibold">Shadow experiments</h2>
        {reviewLink ? <Link href="/shadow" className="text-xs text-action-strong">Review runs</Link> : null}
      </div>
      <p className="mt-2 text-sm text-muted">{status}</p>
      {attention ? <p className="mt-1 text-sm text-caution">{attention}</p> : null}
      <DeferredDisclosure className="mt-2" title={<span className="text-xs text-muted">All counts</span>}>
        <div className="grid grid-cols-2 gap-2 text-xs text-muted sm:grid-cols-4">
          {ALL_COUNTS.map(key => <p key={key}><span className="font-semibold">{counts[key]}</span> {key}</p>)}
        </div>
        <p className="mt-3 text-xs text-muted">Waiting, due and overdue divide active work. Queued and running describe execution. Completed and superseded runs retain their history.</p>
      </DeferredDisclosure>
    </section>
  );
}
