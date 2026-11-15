import "./helpers/dom.mjs";
import assert from "node:assert/strict";
import test from "node:test";
import React, { act, useContext, useEffect } from "react";
import { createRoot } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import ChatTurnStatus from "../src/components/ChatTurnStatus";
import WorkStatusProvider, { WorkStatusContext, useChatWorkStatus } from "../src/components/WorkStatusProvider";
import WorkspaceWorkIndicator from "../src/components/WorkspaceWorkIndicator";
import JobCompletionNoticeHost from "../src/components/JobCompletionNoticeHost";
import { ACTIVE_JOB_STORAGE_KEY, COMPLETED_JOB_NOTICE_KEY } from "../src/lib/job-notifications";
import { chatActivity, elapsedLabel, isMissingChatJob, workspaceActivity } from "../src/lib/work-status";
import { ApiError, apiFetch, type AgentTurnJob, type AutomationStatus } from "../src/lib/api";

const clock = "2026-11-14T12:00:00Z";
const job = (status = "running"): AgentTurnJob => ({
  job_id: "synthetic-job", session_id: "synthetic-session", status,
  request_message: "Explain the synthetic evidence", created_at: clock, updated_at: clock,
  events: [{ phase: "review", message: "Checking the synthetic sources.", created_at: clock }],
});
const automation = (running = false): AutomationStatus => ({
  automation_enabled: true,
  jobs: [{ name: "research_loop", enabled: true, last_status: "running", is_running: running }],
});

for (const [status, label, animated] of [
  ["queued", "Queued", false], ["running", "Reviewing evidence", true],
  ["deferred", "Waiting for AI", false], ["cancelled", "Response stopped", false],
  ["error", "Needs attention", false], ["completed", "Complete", false],
] as const) {
  test(`${status} has honest activity text and animation`, () => {
    const row = job(status);
    row.events.push({ phase: "thinking", message: "Private hidden model content", created_at: clock });
    const html = renderToStaticMarkup(<ChatTurnStatus job={row} />);
    assert.match(html, new RegExp(label));
    assert.equal(html.includes("motion-safe:animate-spin"), animated);
    assert.doesNotMatch(html, /Private hidden model content|aria-valuenow/);
  });
}

test("connection loss overrides a stale working phase and missing clocks stay unknown", () => {
  assert.equal(chatActivity(job(), "Connection unavailable").state, "reconnecting");
  const html = renderToStaticMarkup(<ChatTurnStatus job={{ ...job("deferred"), created_at: "invalid", retry_at: "invalid" }} connectionError="Connection unavailable" />);
  assert.match(html, /Reconnecting/);
  assert.match(html, /Elapsed time unavailable/);
  assert.doesNotMatch(html, /Invalid Date|motion-safe:animate-spin/);
  assert.equal(elapsedLabel(65), "1m 5s");
  assert.equal(elapsedLabel(3665), "1h 1m");
  assert.equal(elapsedLabel(NaN), "Time unavailable");
});

test("workspace activity uses current execution, not a previous running outcome", () => {
  assert.equal(workspaceActivity([], automation(false), true, false).label, "Standing by");
  assert.equal(workspaceActivity([], automation(true), true, false).label, "Working");
  assert.equal(workspaceActivity([job("deferred")], automation(false), true, false).state, "waiting");
  assert.equal(workspaceActivity([job()], automation(true), true, true).state, "reconnecting");
  const unavailable = automation(false);
  delete unavailable.jobs[0].is_running;
  assert.equal(workspaceActivity([], unavailable, true, false).label, "Status unavailable");
  const waiting = automation(false);
  waiting.jobs[0].last_status = "deferred";
  assert.equal(workspaceActivity([], waiting, true, false).label, "Waiting for inputs");
});

test("only an explicit missing job clears recovery, not a proxy error or service outage", () => {
  assert.equal(isMissingChatJob(new ApiError("job_not_found", 404)), true);
  assert.equal(isMissingChatJob(new ApiError("Not Found", 404)), false);
  assert.equal(isMissingChatJob(new ApiError("Service unavailable", 503)), false);
  assert.equal(isMissingChatJob(new Error("Failed to fetch")), false);
});

type Snapshot = NonNullable<React.ContextType<typeof WorkStatusContext>>;
let snapshot: Snapshot;
function Observe() {
  const current = useContext(WorkStatusContext)!;
  useEffect(() => { snapshot = current; });
  return <WorkspaceWorkIndicator />;
}

async function mounted(children: React.ReactNode) {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await act(async () => root.render(children));
  return { container, dispose: async () => { await act(async () => root.unmount()); container.remove(); } };
}

test("one shared refresh cannot overlap, and late snapshots cannot resurrect a stopped job", async () => {
  const originalFetch = globalThis.fetch;
  const pending: Array<(response: Response) => void> = [];
  let requests = 0;
  globalThis.fetch = async (_input, init) => {
    requests += 1;
    return new Promise<Response>((resolve, reject) => {
      pending.push(resolve);
      init?.signal?.addEventListener("abort", () => reject(init.signal?.reason), { once: true });
    });
  };
  const view = await mounted(<WorkStatusProvider><Observe /></WorkStatusProvider>);
  try {
    assert.equal(requests, 2, "one chat read and one automation read");
    const first = snapshot.refresh();
    const second = snapshot.refresh();
    assert.equal(first, second);
    assert.equal(requests, 2);
    await act(async () => { snapshot.updateJob(job()); snapshot.removeJob(job().job_id); });
    await act(async () => {
      pending[0](Response.json({ jobs: [job()] }));
      pending[1](Response.json(automation(false)));
      await first;
    });
    assert.deepEqual(snapshot.jobs, []);
    assert.match(view.container.querySelector("summary")!.textContent!, /Standing by/);
  } finally {
    await view.dispose(); globalThis.fetch = originalFetch;
  }
});

test("stale activity stops animating, remains inspectable and recovers after refresh", async () => {
  const originalFetch = globalThis.fetch;
  let unavailable = false;
  globalThis.fetch = async (input) => {
    if (unavailable) throw new Error("Synthetic disconnected service");
    return Response.json(String(input).includes("turn-jobs") ? { jobs: [job()] } : automation(true));
  };
  const view = await mounted(<WorkStatusProvider><Observe /></WorkStatusProvider>);
  try {
    assert.match(view.container.querySelector("summary")!.textContent!, /Working/);
    unavailable = true;
    await act(async () => { await snapshot.refresh(); });
    assert.match(view.container.querySelector("summary")!.textContent!, /Status unavailable/);
    assert.match(view.container.textContent!, /Previously loaded items may be out of date/);
    assert.equal(view.container.querySelectorAll(".motion-safe\\:animate-spin").length, 0);
    unavailable = false;
    await act(async () => { await snapshot.refresh(); });
    assert.match(view.container.querySelector("summary")!.textContent!, /Working/);
  } finally {
    await view.dispose(); globalThis.fetch = originalFetch;
  }
});

test("background refreshes do not re-render unchanged chat consumers", async () => {
  const originalFetch = globalThis.fetch;
  let renders = 0, running = false;
  function ChatHistory() { useChatWorkStatus(); useEffect(() => { renders += 1; }); return <p>Synthetic unchanged conversation</p>; }
  globalThis.fetch = async (input) => Response.json(String(input).includes("turn-jobs") ? { jobs: [] } : automation(running));
  const view = await mounted(<WorkStatusProvider><Observe /><ChatHistory /></WorkStatusProvider>);
  try {
    const before = renders;
    running = true;
    await act(async () => { await snapshot.refresh(); });
    assert.equal(renders, before);
    assert.match(view.container.querySelector("summary")!.textContent!, /Working/);
  } finally {
    await view.dispose(); globalThis.fetch = originalFetch;
  }
});

test("elapsed ticks update the clock without re-rendering conversation content", async () => {
  const originalInterval = window.setInterval, originalClear = window.clearInterval, originalNow = Date.now;
  let now = Date.parse(clock), tick!: () => void, historyRenders = 0;
  Date.now = () => now;
  window.setInterval = ((callback: () => void) => { tick = callback; return 1; }) as typeof window.setInterval;
  window.clearInterval = () => {};
  function Conversation() { useEffect(() => { historyRenders += 1; }); return <><p>Synthetic history</p><ChatTurnStatus job={job()} /></>; }
  const view = await mounted(<Conversation />);
  try {
    now += 65000;
    await act(async () => tick());
    assert.match(view.container.textContent!, /1m 5s elapsed/);
    assert.equal(historyRenders, 1);
  } finally {
    await view.dispose(); window.setInterval = originalInterval; window.clearInterval = originalClear; Date.now = originalNow;
  }
});

test("the completion watcher yields to the foreground chat and takes over after navigation", async () => {
  const originalFetch = globalThis.fetch, originalInterval = window.setInterval, originalClear = window.clearInterval;
  let poll!: () => void, details = 0;
  window.setInterval = ((callback: () => void) => { poll = callback; return 1; }) as typeof window.setInterval;
  window.clearInterval = () => {};
  globalThis.fetch = async (input) => {
    const path = String(input);
    if (path.endsWith("/synthetic-job")) { details += 1; return Response.json(job("completed")); }
    return Response.json(path.includes("turn-jobs") ? { jobs: [job()] } : automation(false));
  };
  window.sessionStorage.removeItem(COMPLETED_JOB_NOTICE_KEY);
  window.sessionStorage.setItem(ACTIVE_JOB_STORAGE_KEY, JSON.stringify({ job_id: "synthetic-job" }));
  const view = await mounted(<WorkStatusProvider><Observe /><JobCompletionNoticeHost /></WorkStatusProvider>);
  try {
    await act(async () => snapshot.observeJob("synthetic-job"));
    await act(async () => poll());
    assert.equal(details, 0);
    await act(async () => snapshot.observeJob(null));
    await act(async () => poll());
    assert.equal(details, 1);
    assert.match(view.container.textContent!, /Prophet finished the latest analysis/);
    assert.equal(window.sessionStorage.getItem(ACTIVE_JOB_STORAGE_KEY), null);
  } finally {
    await view.dispose();
    globalThis.fetch = originalFetch; window.setInterval = originalInterval; window.clearInterval = originalClear;
    window.sessionStorage.removeItem(ACTIVE_JOB_STORAGE_KEY); window.sessionStorage.removeItem(COMPLETED_JOB_NOTICE_KEY);
  }
});

test("completed reads release their upstream abort subscription", async () => {
  const originalFetch = globalThis.fetch;
  let observed: AbortSignal | null | undefined;
  globalThis.fetch = async (_input, init) => { observed = init?.signal; return Response.json({ ok: true }); };
  try {
    const parent = new AbortController();
    await apiFetch("/synthetic", { signal: parent.signal });
    parent.abort();
    assert.equal(observed?.aborted, false);
  } finally { globalThis.fetch = originalFetch; }
});
