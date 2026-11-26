import assert from "node:assert/strict";
import test from "node:test";

import { readableName } from "../src/lib/readable-name";

test("identifier-style names become words", () => {
  assert.equal(readableName("customer_deposits_qoq"), "Customer deposits QoQ");
  assert.equal(readableName("equity_swap_realized_gain_month_2"), "Equity swap realized gain month 2");
  assert.equal(readableName("eps_ttm"), "EPS TTM");
  assert.equal(readableName("q3_revenue"), "Q3 revenue");
  assert.equal(readableName("yoy_growth"), "YoY growth");
});

test("names written for people are unchanged", () => {
  for (const name of ["Net Assets", "FY2025 Revenue", "ROE Below Threshold But Improving", "revenue", "Swap Notional (SNDK -2x Daily)"]) {
    assert.equal(readableName(name), name);
  }
});
