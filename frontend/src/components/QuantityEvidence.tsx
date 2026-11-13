import {formatUserLabel} from "@/lib/formatting";

export default function QuantityEvidence({check}: {check: Record<string, unknown> | null}) {
  const measurements = Array.isArray(check?.measurements) ? check.measurements.filter((item): item is Record<string, unknown> => !!item && typeof item === "object" && !Array.isArray(item)) : [];
  const issues = Array.isArray(check?.issues) ? check.issues.map(String) : [];
  return <details className="mt-2 border-l-2 border-current/20 pl-3 text-xs">
    <summary className="cursor-pointer font-medium">{check?.verified === true ? `${measurements.length} recorded numerical measurement${measurements.length === 1 ? "" : "s"} matched` : "Numerical evidence needs review"}</summary>
    {!check ? <p className="mt-2">No numerical verification was saved for this historical claim. A cited field alone does not verify an amount.</p> : null}
    {issues.length ? <p className="mt-2">{issues.map(formatUserLabel).join(" · ")}</p> : null}
    {measurements.map((item, index) => <div key={`${item.measurement_ref}-${index}`} className="mt-3 space-y-1 border-t border-current/15 pt-2">
      <p className="font-medium break-words">{String(item.value)} {String(item.unit ?? "")}</p>
      <p>{formatUserLabel(String(item.basis ?? "Measurement basis unavailable"))}</p>
      <p>As of: {String(item.as_of ?? "unknown")}</p>
      {item.period_start || item.period_end ? <p>Period: {String(item.period_start ?? "point observation")} → {String(item.period_end ?? item.as_of ?? "unknown")}</p> : null}
      {Array.isArray(item.notes) && item.notes.length ? <p>{item.notes.map(String).join(" ")}</p> : null}
      <details><summary className="cursor-pointer">Record references</summary><dl className="mt-1 grid gap-1 break-all"><dt>Subject</dt><dd>{String(item.subject_ref ?? "unknown")}</dd><dt>Snapshot</dt><dd>{String(item.snapshot_ref ?? "unknown")}</dd><dt>Source</dt><dd>{String(item.source_ref ?? "unknown")}</dd></dl></details>
    </div>)}
  </details>;
}
