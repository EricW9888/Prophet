import Link from "next/link";
import type { ReviewQueueItem } from "@/lib/api";
import { researchHref, reviewRecordLink, reviewResearchQuestion, reviewSubject } from "@/lib/workflows";

export default function ReviewItemActions({ item, onChallenge, disabled = false }: { item: ReviewQueueItem; onChallenge?: () => void; disabled?: boolean }) {
  const record = reviewRecordLink(item);
  const subject = reviewSubject(item);
  const style = "rounded border border-line px-3 py-2 text-xs font-medium text-action hover:border-line-strong";
  return (
    <div className="mt-3 flex flex-wrap gap-2">
      {record && <Link href={record.href} className={style}>{record.label}</Link>}
      {subject && <Link href={researchHref(subject, reviewResearchQuestion(item))} className={style}>Investigate this concern</Link>}
      {subject && (onChallenge
        ? <button type="button" disabled={disabled} onClick={onChallenge} className={`${style} disabled:opacity-50`}>Prepare a challenge</button>
        : <Link href={`/verification?item_id=${item.id}#manual-challenge`} className={style}>Prepare a challenge</Link>)}
      {!record && !subject && <p className="text-xs text-muted">The related record is unavailable. The retained concern remains visible.</p>}
    </div>
  );
}
