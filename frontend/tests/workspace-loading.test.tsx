import "./helpers/dom.mjs";
import assert from "node:assert/strict";
import test from "node:test";
import React, { act, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { WorkspaceRead, WorkspaceSkeleton } from "../src/components/WorkspaceState";
import { useWorkspaceReads } from "../src/lib/workspace-reads";

test("initial content, retained data, and retry remain distinct under overlapping reads", async () => {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const originalFetch = globalThis.fetch;
  const requests: { signal?: AbortSignal | null; finish: (response: Response) => void }[] = [];
  globalThis.fetch = (_url, init) => new Promise<Response>((finish) => requests.push({ signal: init?.signal, finish }));

  function Panel() {
    const { read, status } = useWorkspaceReads<"ledger">();
    const [value, setValue] = useState("");
    useEffect(() => { void read("ledger", "/test", setValue); }, [read]);
    return <>
      <button type="button" onClick={() => void read("ledger", "/test", setValue)}>Refresh</button>
      <button type="button" onClick={() => void read("ledger", "/test", setValue, false)}>Poll</button>
      <WorkspaceRead name="ledger" state={status.ledger} retry={() => void read("ledger", "/test", setValue)}
        placeholder={<WorkspaceSkeleton label="Loading ledger" layout="chart" />}>
        <p data-content>{value}</p>
      </WorkspaceRead>
    </>;
  }

  const findButton = (name: string) => {
    const button = [...container.querySelectorAll("button")].find((candidate) => candidate.textContent === name);
    assert.ok(button, `Missing ${name} button`);
    return button;
  };
  const click = (name: string) => act(async () => { findButton(name).click(); });
  const success = (index: number, value: string) => act(async () => { requests[index]!.finish(Response.json(value)); });

  try {
    await act(async () => root.render(<Panel />));
    assert.equal(container.querySelector('[role="status"]')?.textContent, "Loading ledger…");
    assert.ok(container.querySelector('[aria-hidden="true"] .workspace-chart-plot'));
    await click("Poll");
    assert.equal(requests.length, 1);
    await success(0, "Previous ledger");

    const ledger = container.querySelector("[data-content]");
    await click("Refresh");
    assert.equal(container.querySelector("[data-content]"), ledger);
    assert.equal(container.querySelector(".workspace-chart-plot"), null);
    await click("Refresh");
    assert.equal(requests[1]!.signal?.aborted, true);
    await success(1, "Superseded ledger");
    assert.equal(ledger?.textContent, "Previous ledger");
    await act(async () => { requests[2]!.finish(new Response("Temporary failure", { status: 503 })); });
    assert.match(container.textContent ?? "", /Showing the last loaded data/);
    assert.equal(ledger?.textContent, "Previous ledger");

    await click("Retry");
    assert.equal(findButton("Retrying…").disabled, true);
    await success(3, "Updated ledger");
    assert.equal(container.querySelector("[data-content]"), ledger);
    assert.equal(ledger?.textContent, "Updated ledger");
    assert.doesNotMatch(container.textContent ?? "", /Temporary failure|last loaded data/);
  } finally {
    await act(async () => root.unmount());
    container.remove();
    globalThis.fetch = originalFetch;
  }
});
