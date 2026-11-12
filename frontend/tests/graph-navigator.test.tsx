import "./helpers/dom.mjs";
import assert from "node:assert/strict";
import { test } from "node:test";
import React, {act} from "react";
import {createRoot} from "react-dom/client";
import GraphNodeNavigator from "../src/components/GraphNodeNavigator";

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
