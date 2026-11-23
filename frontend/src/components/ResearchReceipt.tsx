import Link from "next/link";
import type { AgentResearchReceipt } from "../lib/api";
import { formatUserLabel } from "../lib/formatting";

function SourceTime({ label, value }: { label: string; value?: string | null }) {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  const dateOnly = /^\d{4}-\d{2}-\d{2}$/.test(value);
  return <span>{label} <time dateTime={dateOnly ? value : date.toISOString()}>{dateOnly ? value : date.toLocaleString()}</time></span>;
}

export default function ResearchReceipt({ receipt }: { receipt: AgentResearchReceipt }) {
  const linkedCount = receipt.holdings_with_linked_evidence.length;
  const coverage = receipt.holding_search_coverage || [];
  const completedCount = coverage.filter((row) => row.searched && ["candidate_found", "no_matching_candidate"].includes(row.status)).length;
  const incompleteCount = coverage.length - completedCount;
  return (
    <details className="mt-4 border border-line bg-panel p-3 text-sm">
      <summary className="cursor-pointer font-medium">
        Evidence coverage{coverage.length ? ` · ${completedCount} of ${coverage.length} holding checks complete${incompleteCount ? ` · ${incompleteCount} incomplete` : ""}` : receipt.holdings_count != null ? ` · ${linkedCount} of ${receipt.holdings_count} holdings with linked evidence` : ""}
      </summary>
      {receipt.question_scope ? <p className="mt-2 break-words">Research scope: {receipt.question_scope.search_text}</p> : null}
      {receipt.evidence_prepared_at ? <p className="mt-2 text-muted">Evidence assembled <time dateTime={receipt.evidence_prepared_at}>{new Date(receipt.evidence_prepared_at).toLocaleString()}</time>.</p> : null}
      {coverage.length ? (
        <div className="mt-3">
          <p className="text-muted">Bounded searches for this answer. Candidates need source verification; a search miss does not mean there was no news.</p>
          <ul aria-label="Holding search coverage" className="mt-2 grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
            {coverage.map((row) => (
              <li key={row.ticker} className="flex flex-wrap justify-between gap-2 border border-line p-2">
                <span className="font-medium">{row.ticker}</span>
                <span className="text-muted">{row.status === "candidate_found" ? `${row.candidate_count} candidate${row.candidate_count === 1 ? "" : "s"}` : row.status === "no_matching_candidate" ? "No matching candidate" : row.status === "request_limit" ? "Not searched · request limit" : `${row.searched ? "Incomplete" : "Not searched"} · ${formatUserLabel(row.status)}`}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {receipt.holdings_count != null ? <p className="mt-3 text-muted">Stored evidence explicitly linked to {linkedCount} of {receipt.holdings_count} holdings in this answer. This is separate from the searches above.{linkedCount ? ` Linked: ${receipt.holdings_with_linked_evidence.join(", ")}.` : ""}</p> : null}
      {receipt.checks.length ? (
        <ul className="mt-3 space-y-2">
          {receipt.checks.map((check) => (
            <li key={`${check.lane}:${check.query}`} className="border-l border-line pl-2">
              <span className="font-medium">{check.lane}</span> · {check.result_count ? `${check.result_count} candidate${check.result_count === 1 ? "" : "s"}` : formatUserLabel(check.status)}
              {check.fresh_feeds != null && check.enabled_feeds != null
                ? <span className="text-muted"> · {check.fresh_feeds} of {check.enabled_feeds} enabled feeds checked recently</span>
                : null}
              <p className="break-words text-xs text-muted">{check.query}</p>
            </li>
          ))}
        </ul>
      ) : <p className="mt-2 text-muted">No external check recorded for this answer.</p>}
      {receipt.candidate_sources.length ? (
        <ul className="mt-3 space-y-1">
          {receipt.candidate_sources.filter((source) => /^https?:\/\//i.test(source.url)).map((source) => (
            <li key={source.url} className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
              <a className="min-w-0 break-words text-sky-600 underline dark:text-sky-400" href={source.url} target="_blank" rel="noreferrer">{source.title || source.url}</a>
              <span className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted">
                <SourceTime label="Published" value={source.published_date} />
                <SourceTime label="Feed updated" value={source.updated_date} />
                <SourceTime label="Discovered" value={source.observed_at} />
                {source.evidence_id ? <Link className="text-action underline" href={`/sources/evidence/${encodeURIComponent(source.evidence_id)}`}>Capture receipt</Link> : null}
              </span>
            </li>
          ))}
        </ul>
      ) : null}
    </details>
  );
}
