import Link from "next/link";
import HintMarker from "@/components/HintMarker";
import type { SourcePassage as Passage } from "@/lib/api";

export default function SourcePassage({ passage }: { passage?: Passage | null }) {
  if (!passage) return null;
  const quote = passage.status === "bound" ? passage.quote_selector : null;
  return (
    <section aria-label="Original source passage" className="mb-6 border border-line p-4 text-sm">
      <div className="mb-3 flex items-center gap-2">
        <h4 className="font-semibold">Original source passage</h4>
        <HintMarker label="What a matched source passage means">
          This text matches the retained source. The match does not verify the interpretation or provide independent corroboration. Surrounding text is an excerpt; open the evidence record to inspect the source.
        </HintMarker>
      </div>
      {quote ? (
        <>
          <blockquote className="whitespace-pre-wrap break-words border-l-2 border-line pl-3 leading-relaxed">
            {quote.prefix && <span className="text-muted">{(passage.context_start ?? 0) > 0 ? "…" : ""}{quote.prefix}</span>}
            <span className="font-medium text-foreground">{quote.exact}</span>
            {quote.suffix && <span className="text-muted">{quote.suffix}{(passage.context_end ?? 0) < (passage.document_chars ?? 0) ? "…" : ""}</span>}
          </blockquote>
          {passage.raw_evidence_id && <Link href={`/sources/evidence/${passage.raw_evidence_id}#original-source-text`} className="mt-3 inline-block text-action underline">Open evidence record</Link>}
        </>
      ) : <p className="text-muted">{passage.reason || "No exact source passage was retained for this record."}</p>}
    </section>
  );
}
