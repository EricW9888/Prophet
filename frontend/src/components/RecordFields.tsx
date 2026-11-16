import { formatUserLabel } from "@/lib/formatting";
import DeferredDisclosure from "./DeferredDisclosure";

/** Readable structured details, retaining the complete original record. */
export default function RecordFields({ value, raw = false }: { value: unknown; raw?: boolean }) {
  return <div className="min-w-0 text-sm leading-relaxed text-foreground">
    {Array.isArray(value) ? <RecordValue value={value} /> : <Fields value={value} />}
    {raw && <DeferredDisclosure title="Raw record" className="mt-3 text-muted">
      <RawRecord value={value} />
    </DeferredDisclosure>}
  </div>;
}

function RawRecord({ value }: { value: unknown }) {
  return <pre className="max-h-96 overflow-auto whitespace-pre-wrap [overflow-wrap:anywhere] text-xs">{JSON.stringify(value, null, 2)}</pre>;
}

function Fields({ value }: { value: unknown }) {
  if (value == null || value === "") return <span className="text-muted">Not recorded</span>;
  if (Array.isArray(value)) return value.length
    ? <ol className="list-inside list-decimal space-y-3">{value.map((entry, index) => <li key={index} className="min-w-0"><RecordValue value={entry} /></li>)}</ol>
    : <span className="text-muted">None</span>;
  if (typeof value === "object") {
    const entries = Object.entries(value);
    return entries.length ? <dl className="space-y-3">{entries.map(([key, entry]) => <div key={key} className="min-w-0">
      <dt className="mb-1 text-xs font-medium text-muted">{formatUserLabel(key)}</dt>
      <dd className="min-w-0 [overflow-wrap:anywhere]"><RecordValue value={entry} /></dd>
    </div>)}</dl> : <span className="text-muted">None</span>;
  }
  if (typeof value === "boolean") return <span>{value ? "Yes" : "No"}</span>;
  if (typeof value === "number") return <span className="tabular-nums">{Number.isInteger(value) ? value.toLocaleString() : String(value)}</span>;
  const text = String(value);
  const date = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(text) ? new Date(text) : null;
  return date && Number.isFinite(date.getTime())
    ? <time dateTime={text} title={text}>{date.toLocaleString()}</time>
    : <span className="whitespace-pre-wrap [overflow-wrap:anywhere]">{text}</span>;
}

function RecordValue({ value }: { value: unknown }) {
  if (value !== null && typeof value === "object" && Object.keys(value).length) {
    const count = Object.keys(value).length;
    const unit = Array.isArray(value) ? "item" : "field";
    return <DeferredDisclosure title={`${count} ${unit}${count === 1 ? "" : "s"}`} className="border-l border-line pl-3"><Fields value={value} /></DeferredDisclosure>;
  }
  return <Fields value={value} />;
}
