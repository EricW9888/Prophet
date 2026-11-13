import "./helpers/dom.mjs";
import assert from "node:assert/strict";
import {test} from "node:test";
import React, {act} from "react";
import {createRoot} from "react-dom/client";
import ValuationCoverage from "../src/components/ValuationCoverage";
import {safeFormatCurrency,safeFormatSignedCurrency,safeFormatPct} from "../src/lib/formatting";

test("financial formatting preserves unknown values separately from measured zero",()=>{
  for(const value of [null,undefined,NaN,Infinity,-Infinity]) {
    assert.equal(safeFormatCurrency(value),"—");
    assert.equal(safeFormatSignedCurrency(value),"—");
    assert.equal(safeFormatPct(value),"—");
  }
  assert.equal(safeFormatCurrency(0),"$0");
  assert.equal(safeFormatSignedCurrency(0),"+$0.00");
  assert.equal(safeFormatPct(0),"+0.00%");
});

test("a partial priced subtotal is never presented as full holdings value",async()=>{
  const container=document.createElement("div"); document.body.append(container);const root=createRoot(container);
  try {
    await act(async()=>root.render(<ValuationCoverage coverage={{complete:false,total_positions:2,priced_positions:1,covered_market_value:50,unavailable_tickers:["SYNM"],cash_basis:"unavailable"}}/>));
    assert.match(container.textContent!,/1\/2 holdings/);
    assert.match(container.textContent!,/Full holdings value and return are unavailable/);
    assert.match(container.textContent!,/Priced subset: \$50\.00/);
    assert.match(container.textContent!,/Recorded cash is unavailable/);
    await act(async()=>root.render(<ValuationCoverage coverage={{complete:true,total_positions:1,priced_positions:1,covered_market_value:0,unavailable_tickers:[],cash_basis:"cash_ledger"}}/>));
    assert.match(container.textContent!,/1\/1 holdings/);
    assert.doesNotMatch(container.textContent!,/unavailable|Priced subset/);
  } finally {await act(async()=>root.unmount());container.remove();}
});

test("fractional share formatting keeps small quantities and useful precision",async()=>{
  const {safeFormatQuantity}=await import("../src/lib/formatting");
  assert.equal(safeFormatQuantity(0.7654321),"0.7654321");
  assert.equal(safeFormatQuantity(0),"0");
  assert.notEqual(safeFormatQuantity(1e-25),"0");
  assert.equal(safeFormatQuantity(null),"—");
});
