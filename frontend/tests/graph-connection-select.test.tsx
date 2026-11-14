import "./helpers/dom.mjs";
import assert from "node:assert/strict";
import {test} from "node:test";
import React, {act} from "react";
import {createRoot} from "react-dom/client";
import {GraphConnectionSelect} from "../src/components/GraphConnectionSelect";
import type {GraphWebEdge, GraphWebNode} from "../src/lib/api";

test("coincident assertions retain their own source choice and direction", async () => {
  const edges: GraphWebEdge[] = [
    {id:"founder",source_key:"person",target_key:"company",relationship_type:"founded",confidence:.9,supporting_claim_id:"one"},
    {id:"departure",source_key:"person",target_key:"company",relationship_type:"departed_from",confidence:.8,supporting_claim_id:"two"},
    {id:"reverse",source_key:"company",target_key:"person",relationship_type:"employed",confidence:.8},
    {id:"elsewhere",source_key:"other",target_key:"company",relationship_type:"competes_with",confidence:.8},
  ];
  const node=(key:string,label:string):GraphWebNode=>({key,id:key,label,node_type:"entity",layer:"knowledge",is_root:false,is_autonomous:false});
  const nodes={person:node("person","Casey Vale"),company:node("company","Helix Labs"),"claim:one":node("claim:one","First source: founder"),"claim:two":node("claim:two","Second source: departure")};
  const container=document.createElement("div");document.body.append(container);
  const root=createRoot(container);
  let selected="";
  const render=(edge:GraphWebEdge)=><GraphConnectionSelect edges={edges} selected={edge} nodes={nodes} edgeKey={edge=>edge.id} onSelect={key=>{selected=key;}}/>;
  try {
    await act(async()=>root.render(render(edges[1])));
    const select=container.querySelector("select")!;
    assert.equal(select.value,"departure");
    assert.equal(select.options.length,3);
    assert.ok(select.options[0].textContent?.includes("First source: founder"));
    assert.ok(select.options[1].textContent?.includes("Second source: departure"));
    assert.ok(select.options[2].textContent?.startsWith("Helix Labs → Casey Vale"));
    await act(async()=>{select.value="founder";select.dispatchEvent(new Event("change",{bubbles:true}));});
    assert.equal(selected,"founder");
    await act(async()=>root.render(render(edges[0])));
    assert.equal(select.value,"founder");
  } finally {await act(async()=>root.unmount());container.remove();}
});
