import "./helpers/dom.mjs";
import assert from "node:assert/strict";
import { test } from "node:test";
import React, {act} from "react";
import {createRoot} from "react-dom/client";
import GraphNodeNavigator from "../src/components/GraphNodeNavigator";
import GraphMetricSummary from "../src/components/GraphMetricSummary";

test("node navigation pages complete labels and chooses the exact matching identity", async () => {
  const nodes=Array.from({length:35},(_,index)=>({key:`entity:${index}`,id:String(index),node_type:"entity",layer:"knowledge",label:`Synthetic company ${String(index).padStart(2,"0")} with a complete research title`,is_root:false,is_autonomous:false,degree:index}));
  const container=document.createElement("div");document.body.append(container);const root=createRoot(container);let selected="";
  try {
    await act(async()=>root.render(<GraphNodeNavigator nodes={nodes} onSelect={node=>{selected=node.key;}}/>));
    await act(async()=>container.querySelector("button")!.click());
    assert.equal(container.querySelectorAll("li").length,20);
    const next=[...container.querySelectorAll("button")].find(button=>button.textContent==="Next nodes")!;
    await act(async()=>next.click());
    assert.equal(container.querySelectorAll("li").length,15);
    assert.ok(container.textContent!.includes(nodes[34].label));
    const input=container.querySelector("input")!;
    await act(async()=>{Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,"value")!.set!.call(input,"company 34");input.dispatchEvent(new Event("input",{bubbles:true}));});
    assert.equal(container.querySelectorAll("li").length,1);
    await act(async()=>container.querySelector<HTMLButtonElement>("li button")!.click());
    assert.equal(selected,"entity:34");
    assert.equal(container.querySelector("ul"),null);
  } finally {await act(async()=>root.unmount());container.remove();}
});

test("metric inspector separates source value, unit, period and observation date", async () => {
  const container = document.createElement("div"); document.body.append(container); const root = createRoot(container);
  try {
    await act(async () => root.render(<GraphMetricSummary properties={{value_text:"55,555,555",numeric_value:55555555,unit:"shares",period_label:"offering",as_of:"2026-01-02T00:00:00Z",freshness_status:"current",next_test:"Check final allocation"}} />));
    const values = [...container.querySelectorAll("dd")].map(node => node.textContent);
    assert.deepEqual(values, ["55,555,555", "shares", "offering", "2026-01-02T00:00:00Z", "current"]);
    assert.equal((container.textContent!.match(/55,555,555/g) ?? []).length, 1);
    assert.ok(container.textContent!.includes("Check final allocation"));
    await act(async () => root.render(<GraphMetricSummary properties={{numeric_value:0,unit:"USD"}} />));
    assert.deepEqual([...container.querySelectorAll("dd")].map(node => node.textContent), ["0", "USD"]);
  } finally {await act(async () => root.unmount());container.remove();}
});

test("library keeps server body matches and does not invent connection counts", async () => {
  const container = document.createElement("div"); document.body.append(container); const root = createRoot(container); let selected = "";
  try {
    const record = {key:"claim:source-match",id:"source-match",node_type:"claim",label:"A claim whose body matched",layer:"knowledge",is_root:false,is_autonomous:false};
    await act(async () => root.render(<GraphNodeNavigator library nodes={[record]} search={{value:"supply disruption",onChange:()=>{},remote:true}} onSelect={node => {selected=node.key;}} />));
    assert.equal(container.querySelectorAll("li").length, 1);
    assert.ok(!container.textContent!.includes("0 connections"));
    await act(async () => container.querySelector<HTMLButtonElement>("li button")!.click());
    assert.equal(selected, record.key);
  } finally {await act(async () => root.unmount());container.remove();}
});
