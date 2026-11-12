import type { AgentResearchReceipt } from "../lib/api";
import { formatUserLabel } from "../lib/formatting";

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
              <p className="break-words text-xs text-muted">{check.query}</p>
            </li>
          ))}
        </ul>
      ) : <p className="mt-2 text-muted">No external check recorded for this answer.</p>}
      {receipt.candidate_sources.length ? (
        <ul className="mt-3 space-y-1">
          {receipt.candidate_sources.filter((source) => /^https?:\/\//i.test(source.url)).map((source) => (
            <li key={source.url}><a className="break-words text-sky-600 underline dark:text-sky-400" href={source.url} target="_blank" rel="noreferrer">{source.title || source.url}</a>{source.published_date ? <span className="ml-2 text-xs text-muted">{source.published_date.slice(0, 10)}</span> : null}</li>
          ))}
        </ul>
      ) : null}
    </details>
  );
}
