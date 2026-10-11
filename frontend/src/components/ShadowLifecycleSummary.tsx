import Link from "next/link";
import { ShadowLifecycleCounts } from "@/lib/api";

export default function ShadowLifecycleSummary({ counts }: { counts?: ShadowLifecycleCounts | null }) {
  if (!counts) return null;
  return (
    <section className="min-w-0 rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-950" aria-label="Shadow lifecycle">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-semibold">Shadow experiments</h2>
        <Link href="/shadow" className="text-xs text-sky-700 dark:text-sky-300">Review runs</Link>
      </div>
      <p className="mt-2 text-sm text-slate-600 dark:text-slate-300">{counts.active} active · {counts.queued} queued · {counts.running} running</p>
      <div className="mt-3 grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
        {(["waiting", "due", "overdue", "completed", "failed", "superseded", "skipped", "manual"] as const).map(key => (
          <p key={key} className={key === "overdue" && counts[key] > 0 ? "text-amber-700 dark:text-amber-300" : "text-slate-500 dark:text-slate-400"}>
            <span className="font-semibold">{counts[key]}</span> {key}
          </p>
        ))}
      </div>
      <p className="mt-3 text-xs text-slate-500">Waiting, due and overdue divide active work. Completed and superseded runs retain their history.</p>
    </section>
  );
}
