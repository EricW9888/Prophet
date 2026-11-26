import type { Position } from "@/lib/api";

export type PositionSortKey = "ticker" | "quantity" | "cost" | "price" | "value" | "weight" | "gain" | "gainPct";
export type SortDirection = "asc" | "desc";

export type PositionRow = {
  position: Position;
  costBasis: number | null;
  gainPct: number | null;
  weightPct: number | null;
  /**
   * True when the quote is missing, or older than the table's newest quote by a
   * calendar day: a price that lags the others. Overnight, when every quote is
   * from the last close, nothing is marked; the table's "as of" time says it.
   */
  stale: boolean;
};

function finite(value: number | null | undefined): number | null {
  return value == null || !Number.isFinite(value) ? null : value;
}

function sameLocalDay(a: Date, b: Date): boolean {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
}

/**
 * Derive the comparison columns brokers show from fields the ledger already returns.
 * `weightPct` is a share of holdings value; recorded cash is not in the denominator.
 */
export function buildPositionRows(holdings: Position[], now: Date = new Date()): PositionRow[] {
  const reference = latestQuoteTime(holdings) ?? now;
  const total = holdings.reduce((sum, item) => sum + (finite(item.market_value) ?? 0), 0);
  return holdings.map((position) => {
    const quantity = finite(position.quantity);
    const cost = finite(position.avg_cost_basis);
    const costBasis = quantity != null && cost != null ? quantity * cost : null;
    const gain = finite(position.unrealized_pnl);
    const value = finite(position.market_value);
    const quotedAt = position.valuation?.quoted_at ? new Date(position.valuation.quoted_at) : null;
    const ready = position.valuation ? position.valuation.status === "ready" : value != null;
    return {
      position,
      costBasis,
      gainPct: gain != null && costBasis ? (gain / Math.abs(costBasis)) * 100 : null,
      weightPct: value != null && total > 0 ? (value / total) * 100 : null,
      stale: !ready || (quotedAt != null && !Number.isNaN(quotedAt.getTime()) && !sameLocalDay(quotedAt, reference)),
    };
  });
}

/** The newest quote time across ready holdings, shown once for the whole table. */
export function latestQuoteTime(holdings: Position[]): Date | null {
  let latest: Date | null = null;
  for (const item of holdings) {
    const raw = item.valuation?.status === "ready" ? item.valuation.quoted_at : null;
    const time = raw ? new Date(raw) : null;
    if (time && !Number.isNaN(time.getTime()) && (!latest || time > latest)) latest = time;
  }
  return latest;
}

function sortValue(row: PositionRow, key: PositionSortKey): number | string | null {
  switch (key) {
    case "ticker":
      return (row.position.ticker ?? row.position.entity_name ?? row.position.security_id ?? "").toUpperCase();
    case "quantity":
      return finite(row.position.quantity);
    case "cost":
      return finite(row.position.avg_cost_basis);
    case "price":
      return finite(row.position.current_price);
    case "value":
      return finite(row.position.market_value);
    case "weight":
      return row.weightPct;
    case "gain":
      return finite(row.position.unrealized_pnl);
    case "gainPct":
      return row.gainPct;
  }
}

/** Sort rows; missing values always sort last so they never masquerade as extremes. */
export function sortPositionRows(rows: PositionRow[], key: PositionSortKey, direction: SortDirection): PositionRow[] {
  const factor = direction === "asc" ? 1 : -1;
  return [...rows].sort((left, right) => {
    const a = sortValue(left, key);
    const b = sortValue(right, key);
    if (a == null && b == null) return 0;
    if (a == null) return 1;
    if (b == null) return -1;
    if (typeof a === "string" && typeof b === "string") return a.localeCompare(b) * factor;
    return ((a as number) - (b as number)) * factor;
  });
}
