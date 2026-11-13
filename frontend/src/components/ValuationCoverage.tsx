import type {ValuationCoverage as Coverage} from "@/lib/api";
import {safeFormatCurrency} from "@/lib/formatting";

export default function ValuationCoverage({coverage}: {coverage?: Coverage}) {
  if (!coverage) return <p className="text-sm text-muted">Quote coverage is unavailable for this saved summary.</p>;
  return <div className="text-sm text-muted" role="status">
    <p>{coverage.priced_positions}/{coverage.total_positions} holdings have usable quote receipts. {!coverage.complete ? "Full holdings value and return are unavailable." : "Values use recorded quotes and recorded shares."}</p>
    {!coverage.complete && coverage.priced_positions > 0 ? <p>Priced subset: {safeFormatCurrency(coverage.covered_market_value,2)}. This subtotal excludes the unpriced holdings.</p> : null}
    {coverage.unavailable_tickers.length ? <p>Quote review: {coverage.unavailable_tickers.join(", ")}</p> : null}
    {coverage.cash_basis === "unavailable" ? <p>Recorded cash is unavailable; total ledger value is unavailable.</p> : null}
  </div>;
}
