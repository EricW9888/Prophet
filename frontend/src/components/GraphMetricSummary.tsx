import type { GraphNodeDetail } from "@/lib/api";

export default function GraphMetricSummary({ properties: p }: {properties: GraphNodeDetail["properties"]}) {
  const text = (value: unknown) => typeof value === "string" && value.trim() ? value.trim() : null;
  const value = text(p.value_text) ?? (typeof p.numeric_value === "number" && Number.isFinite(p.numeric_value)
    ? new Intl.NumberFormat("en-US", {maximumFractionDigits:20}).format(p.numeric_value) : null);
  const fields = [
    ["Value", value], ["Unit", text(p.unit)], ["Currency", text(p.currency)],
    ["Period", text(p.period_label)], ["As of", text(p.as_of)],
    ["Recorded freshness", text(p.freshness_status)],
  ].filter(([, value]) => value);
  return <section aria-label="Metric evidence" className="mt-3 space-y-3 border-t border-line pt-3 text-sm">
    <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-2">{fields.map(([label, value]) => <div key={label} className="contents">
      <dt className="text-muted">{label}</dt><dd className="break-words font-medium">{value}</dd>
    </div>)}</dl>
    {text(p.investment_relevance) ? <div><h4 className="font-medium">Why it matters</h4><p className="mt-1 whitespace-pre-wrap leading-relaxed">{text(p.investment_relevance)}</p></div> : null}
    {text(p.next_test) ? <div><h4 className="font-medium">Next check</h4><p className="mt-1 whitespace-pre-wrap leading-relaxed">{text(p.next_test)}</p></div> : null}
  </section>;
}
