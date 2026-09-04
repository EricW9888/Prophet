import Link from "next/link";
import { ExternalLink, FileSearch } from "lucide-react";

type SourceProvenanceLinksProps = {
  evidenceId?: string | null;
  sourceName?: string | null;
  sourceType?: string | null;
  url?: string | null;
  urlKind?: string | null;
  compact?: boolean;
  showUnavailable?: boolean;
};

function safeExternalUrl(value?: string | null) {
  if (!value) return null;
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:" ? url.toString() : null;
  } catch {
    return null;
  }
}

export default function SourceProvenanceLinks({
  evidenceId,
  sourceName,
  sourceType,
  url,
  urlKind,
  compact = false,
  showUnavailable = false,
}: SourceProvenanceLinksProps) {
  const externalUrl = safeExternalUrl(url);
  const sourceLabel = sourceName || sourceType?.replaceAll("_", " ") || "source";
  const externalLabel = urlKind === "source_home" ? "Source site" : "Original source";
  const textSize = compact ? "text-[11px]" : "text-xs";

  if (!externalUrl && !evidenceId) {
    return showUnavailable ? (
      <span className={`${textSize} text-slate-400`}>No source link recorded</span>
    ) : null;
  }

  return (
    <div className={`flex flex-wrap items-center gap-x-3 gap-y-1 ${textSize}`}>
      {externalUrl ? (
        <a
          href={externalUrl}
          target="_blank"
          rel="noreferrer noopener"
          title={`${externalLabel}: ${sourceLabel}`}
          className="inline-flex items-center gap-1 font-medium text-sky-600 hover:text-sky-500 dark:text-sky-400 dark:hover:text-sky-300"
        >
          <ExternalLink className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
          {externalLabel}
        </a>
      ) : showUnavailable ? (
        <span className="text-slate-400">No external URL recorded</span>
      ) : null}
      {evidenceId ? (
        <Link
          href={`/sources/evidence/${evidenceId}`}
          title={`Inspect Prophet's stored evidence receipt for ${sourceLabel}`}
          className="inline-flex items-center gap-1 font-medium text-slate-600 hover:text-slate-900 dark:text-slate-300 dark:hover:text-white"
        >
          <FileSearch className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
          Evidence record
        </Link>
      ) : null}
    </div>
  );
}
