import assert from "node:assert/strict";
import test from "node:test";

import type { Position } from "../src/lib/api";
import { buildPositionRows, dayChangeTotal, latestQuoteTime, sortPositionRows } from "../src/lib/positions-table";

function holding(ticker: string, overrides: Partial<Position> = {}): Position {
  return {
    id: ticker,
    security_id: ticker,
    ticker,
    direction: "long",
    list_type: "holding",
    quantity: 2,
    avg_cost_basis: 50,
    current_price: 60,
    market_value: 120,
    unrealized_pnl: 20,
    realized_pnl: 0,
    added_at: "2026-01-01T00:00:00Z",
    valuation: { status: "ready", issues: [], quoted_at: "2026-11-25T15:00:00Z", observed_at: null, provider: "synthetic", currency: "USD" },
    ...overrides,
  };
}

const now = new Date("2026-11-25T18:00:00Z");

test("derives gain percent and portfolio share from ledger fields", () => {
  const rows = buildPositionRows([holding("AAA"), holding("BBB", { market_value: 360, unrealized_pnl: -40, avg_cost_basis: 200 })], now);
  assert.equal(rows[0].gainPct, 20); // 20 / (2 * 50)
  assert.equal(rows[0].weightPct, 25); // 120 / 480
  assert.equal(rows[1].gainPct, -10); // -40 / (2 * 200)
  assert.equal(rows[1].weightPct, 75);
});

test("marks only missing quotes and quotes that lag the newest one", () => {
  const rows = buildPositionRows([
    holding("FRESH"),
    holding("OLD", { valuation: { status: "ready", issues: [], quoted_at: "2026-11-20T15:00:00Z", observed_at: null, provider: null, currency: "USD" } }),
    holding("NONE", { market_value: null, valuation: { status: "unavailable", issues: ["No quote"], quoted_at: null, observed_at: null, provider: null, currency: null } }),
  ], now);
  assert.deepEqual(rows.map((row) => row.stale), [false, true, true]);
});

test("shows one table-level quote time from the newest ready quote", () => {
  const latest = latestQuoteTime([
    holding("A", { valuation: { status: "ready", issues: [], quoted_at: "2026-11-25T14:00:00Z", observed_at: null, provider: null, currency: "USD" } }),
    holding("B"),
    holding("C", { valuation: { status: "unavailable", issues: [], quoted_at: "2026-11-25T17:59:00Z", observed_at: null, provider: null, currency: null } }),
  ]);
  assert.equal(latest?.toISOString(), "2026-11-25T15:00:00.000Z");
});

test("sorts by any column and keeps missing values last in both directions", () => {
  const rows = buildPositionRows([
    holding("MID", { market_value: 200 }),
    holding("LOW", { market_value: 100 }),
    holding("NULL", { market_value: null }),
    holding("HIGH", { market_value: 300 }),
  ], now);
  assert.deepEqual(sortPositionRows(rows, "value", "desc").map((row) => row.position.ticker), ["HIGH", "MID", "LOW", "NULL"]);
  assert.deepEqual(sortPositionRows(rows, "value", "asc").map((row) => row.position.ticker), ["LOW", "MID", "HIGH", "NULL"]);
  assert.deepEqual(sortPositionRows(rows, "ticker", "asc").map((row) => row.position.ticker), ["HIGH", "LOW", "MID", "NULL"]);
});

test("overnight, when every quote is from the last close, nothing is marked", () => {
  const nextMorning = new Date("2026-11-26T13:00:00Z");
  const rows = buildPositionRows([holding("A"), holding("B")], nextMorning);
  assert.deepEqual(rows.map((row) => row.stale), [false, false]);
});

const withDay = (ticker: string, change: number | null) =>
  holding(ticker, { valuation: { status: "ready", issues: [], quoted_at: "2026-11-25T15:00:00Z", observed_at: null, provider: "synthetic", currency: "USD", day_change: change, day_change_pct: change == null ? null : change / 2 } });

test("the day change total says how many holdings it covers", () => {
  assert.deepEqual(dayChangeTotal([withDay("AAA", 12.5), withDay("BBB", -2.5), withDay("CCC", null)]), { total: 10, covered: 2, count: 3 });
  assert.deepEqual(dayChangeTotal([withDay("AAA", null)]), { total: 0, covered: 0, count: 1 });
});

test("sorting by day change keeps holdings without one last", () => {
  const rows = buildPositionRows([withDay("AAA", -3), withDay("BBB", null), withDay("CCC", 8)], now);
  assert.deepEqual(sortPositionRows(rows, "day", "desc").map((row) => row.position.ticker), ["CCC", "AAA", "BBB"]);
  assert.deepEqual(sortPositionRows(rows, "day", "asc").map((row) => row.position.ticker), ["AAA", "CCC", "BBB"]);
});
