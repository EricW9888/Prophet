import "./helpers/dom.mjs";
import assert from "node:assert/strict";
import {afterEach, beforeEach, test} from "node:test";
import React, {act} from "react";
import {createRoot, type Root} from "react-dom/client";
import ListingUniversePanel from "../src/components/ListingUniversePanel";

let root: Root, container: HTMLDivElement;
const originalFetch = globalThis.fetch;
const coverage = () => {};
beforeEach(() => {container = document.createElement("div"); document.body.append(container); root=createRoot(container);});
afterEach(async () => {await act(async () => root.unmount()); container.remove(); globalThis.fetch=originalFetch;});

function fixture() {
  return {scope:"manual", total:25, eligible:25, materialized:0, match_count:25, offset:0,
    directories:[{url:"https://example.test/listings",status:"ok",error:null,last_success_at:"2025-05-20T18:00:00Z"}],
    items:Array.from({length:20},(_,i)=>({id:`listing-${i}`,symbol:`SYN${i}`,name:`Synthetic company ${i}`,exchange:"NASDAQ",instrument_type:"common_stock",research_eligible:true,operator_excluded:false,member_id:null,observed_at:"2025-05-20T18:00:00Z",error:null}))};
}

test("scope selection changes only research scope and keeps the directory separate from completed work", async () => {
  const data=fixture(); let saved: unknown;
  globalThis.fetch=async (_input,init) => {
    if(init?.method==="PUT") {saved=JSON.parse(String(init.body)); data.scope="us_listed";return new Response("{}");}
    return new Response(JSON.stringify(data),{headers:{"Content-Type":"application/json"}});
  };
  await act(async()=>root.render(<ListingUniversePanel onCoverage={coverage}/>));
  const select=container.querySelector("select")!;
  await act(async()=>{select.value="us_listed";select.dispatchEvent(new Event("change",{bubbles:true}));});
  assert.deepEqual(saved,{opportunity_discovery:{universe_scope:"us_listed"}});
  assert.equal(select.value,"us_listed");
  assert.match(container.textContent!,/Preparation is separate from completed investigation/);
  assert.match(container.textContent!,/25 directory listings/);
});

test("a failed scope save preserves the loaded selection and coverage", async () => {
  globalThis.fetch=async (_input,init)=> init?.method==="PUT"
    ? new Response(JSON.stringify({detail:"Scope unavailable"}),{status:503,headers:{"Content-Type":"application/json"}})
    : new Response(JSON.stringify(fixture()),{headers:{"Content-Type":"application/json"}});
  await act(async()=>root.render(<ListingUniversePanel onCoverage={coverage}/>));
  const select=container.querySelector("select")!;
  await act(async()=>{select.value="us_listed";select.dispatchEvent(new Event("change",{bubbles:true}));});
  assert.equal(select.value,"manual");
  assert.match(container.textContent!,/Scope unavailable/);
  assert.match(container.textContent!,/25 directory listings/);
});

test("directory browsing requests bounded pages without rendering the full listing set", async () => {
  const requests:string[]=[];
  globalThis.fetch=async (input)=>{
    const url=String(input);requests.push(url);const page=fixture();
    if(url.includes("offset=20")){page.offset=20;page.items=page.items.slice(0,5).map((row,i)=>({...row,id:`last-${i}`,symbol:`SYN${i+20}`}));}
    return new Response(JSON.stringify(page),{headers:{"Content-Type":"application/json"}});
  };
  await act(async()=>root.render(<ListingUniversePanel onCoverage={coverage}/>));
  assert.equal(container.querySelectorAll("li").length,20);
  const next=[...container.querySelectorAll("button")].find(b=>b.textContent==="Next")!;
  await act(async()=>next.click());
  assert.equal(container.querySelectorAll("li").length,5);
  assert.ok(requests.some(url=>url.includes("offset=20")));
  assert.match(container.textContent!,/21–25 of 25/);
});
