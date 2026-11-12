import { formatUserLabel } from "@/lib/formatting";

/** Readable structured details, retaining the complete original record. */
export default function RecordFields({ value, raw = false }: { value: unknown; raw?: boolean }) {
  return <div className="min-w-0 text-sm text-foreground">
    <Fields value={value} />
    {raw && <details className="mt-3 text-xs text-muted">
      <summary className="cursor-pointer">Raw record</summary>
      <pre className="mt-2 overflow-auto whitespace-pre-wrap break-words">{JSON.stringify(value, null, 2)}</pre>
    </details>}
  </div>;
}

function Fields({ value }: { value: unknown }) {
  if (value == null || value === "") return <span className="text-muted">Not recorded</span>;
  if (Array.isArray(value)) return value.length
    ? <ul className="space-y-2">{value.map((entry, index) => <li key={index} className="min-w-0 border-l border-line pl-3"><Fields value={entry} /></li>)}</ul>
    : <span className="text-muted">None</span>;
  if (typeof value === "object") {
    const entries = Object.entries(value);
    return entries.length ? <dl className="space-y-3">{entries.map(([key, entry]) => <div key={key} className="min-w-0">
      <dt className="mb-1 text-xs font-medium text-muted">{formatUserLabel(key)}</dt>
      <dd className="min-w-0 break-words"><Fields value={entry} /></dd>
    </div>)}</dl> : <span className="text-muted">None</span>;
  }
  if (typeof value === "boolean") return <span>{value ? "Yes" : "No"}</span>;
  if (typeof value === "number") return <span className="tabular-nums">{value.toLocaleString()}</span>;
  const text = String(value);
  const date = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(text) ? new Date(text) : null;
  return <span>{date && Number.isFinite(date.getTime())
    ? date.toLocaleString()
    : /^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$/.test(text) ? formatUserLabel(text) : text}</span>;
}
