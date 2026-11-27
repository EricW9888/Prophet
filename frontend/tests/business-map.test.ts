import assert from "node:assert/strict";
import test from "node:test";

import { businessMapGroups, compactDollars, sharePercent, type BusinessMap } from "../src/lib/business-map";

const map: BusinessMap = {
  entity_id: "synthetic",
  period_start: "2025-01-01",
  period_end: "2025-12-31",
  total_revenue: 100e9,
  filing_url: "https://www.sec.gov/Archives/edgar/data/1/synthetic.htm",
  drivers: [
    { kind: "region", label: "United States", revenue: 60e9, share: 0.6 },
    { kind: "customer", label: "Customer A", revenue: 12e9, share: 0.12 },
    { kind: "segment", label: "Gaming", revenue: 20e9, share: 0.2 },
    { kind: "segment", label: "Data center", revenue: 80e9, share: 0.8 },
    { kind: "region", label: "Rest of world", revenue: 40e9, share: 0.4 },
  ],
};

test("breakdowns read segment, product, region, then customers, largest first", () => {
  const groups = businessMapGroups(map);
  assert.deepEqual(groups.map((group) => group.title), ["By segment", "By region", "Major customers"]);
  assert.deepEqual(groups[0].drivers.map((driver) => driver.label), ["Data center", "Gaming"]);
});

test("a partial breakdown says how much is not broken out; a complete one does not", () => {
  const groups = businessMapGroups(map);
  assert.equal(groups[0].notBrokenOut, null);
  assert.equal(sharePercent(groups[2].notBrokenOut ?? 0), "88%");
});

test("amounts and shares are short and readable", () => {
  assert.equal(compactDollars(412e6), "$412M");
  assert.equal(compactDollars(3.14e9), "$3.1B");
  assert.equal(compactDollars(1.2e12), "$1.2T");
  assert.equal(sharePercent(0.004), "<1%");
  assert.equal(sharePercent(0.255), "26%");
});

test("a breakdown with one row covering the whole company is left out", () => {
  const single: BusinessMap = { ...map, drivers: [{ kind: "segment", label: "Reportable segment", revenue: 100e9, share: 1 }, ...map.drivers.filter((d) => d.kind !== "segment")] };
  assert.deepEqual(businessMapGroups(single).map((group) => group.kind), ["region", "customer"]);
});
