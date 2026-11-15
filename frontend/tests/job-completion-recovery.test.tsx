import "./helpers/dom.mjs";
import assert from "node:assert/strict";
import test from "node:test";
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import JobCompletionNoticeHost from "../src/components/JobCompletionNoticeHost";
import {
  ACTIVE_JOB_STORAGE_KEY,
  COMPLETED_JOB_NOTICE_KEY,
  clearCompletedJobNotice,
} from "../src/lib/job-notifications";

test("starting another question clears an older terminal notice immediately", async () => {
  window.sessionStorage.setItem(
    COMPLETED_JOB_NOTICE_KEY,
    JSON.stringify({
      message: "Synthetic earlier failure",
      jobId: "earlier-job",
    }),
  );
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  try {
    await act(async () => root.render(<JobCompletionNoticeHost />));
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    assert.match(container.textContent!, /Synthetic earlier failure/);
    await act(async () => clearCompletedJobNotice());
    assert.equal(container.textContent, "");
    assert.equal(window.sessionStorage.getItem(COMPLETED_JOB_NOTICE_KEY), null);
  } finally {
    await act(async () => root.unmount());
    container.remove();
    window.sessionStorage.removeItem(COMPLETED_JOB_NOTICE_KEY);
  }
});

for (const status of ["error", "completed", "cancelled"] as const) {
  test(`the global ${status} notice preserves the right chat recovery state`, async () => {
    const originalFetch = globalThis.fetch;
    const originalInterval = window.setInterval;
    const originalClearInterval = window.clearInterval;
    let poll!: () => void;
    let requests = 0;
    window.setInterval = ((callback: () => void) => {
      poll = callback;
      return 1;
    }) as typeof window.setInterval;
    window.clearInterval = () => {};
    const saved = JSON.stringify({
      job_id: "synthetic-job",
      session_id: "synthetic-session",
    });
    window.sessionStorage.setItem(ACTIVE_JOB_STORAGE_KEY, saved);
    window.sessionStorage.removeItem(COMPLETED_JOB_NOTICE_KEY);
    globalThis.fetch = async () => {
      requests += 1;
      return Response.json({
        job_id: "synthetic-job",
        status,
        session_id: "synthetic-session",
      });
    };
    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    try {
      await act(async () => root.render(<JobCompletionNoticeHost />));
      await act(async () => {
        poll();
      });
      assert.equal(
        window.sessionStorage.getItem(ACTIVE_JOB_STORAGE_KEY),
        status === "error" ? saved : null,
      );
      assert.equal(
        container.querySelector("a")?.getAttribute("href"),
        status === "cancelled" ? undefined : status === "error" ? "/chat" : "/chat?session_id=synthetic-session",
      );
      if (status === "cancelled") assert.equal(window.sessionStorage.getItem(COMPLETED_JOB_NOTICE_KEY), null);
      else assert.equal(
        JSON.parse(window.sessionStorage.getItem(COMPLETED_JOB_NOTICE_KEY)!)
          .jobId,
        "synthetic-job",
      );
      await act(async () => {
        poll();
      });
      assert.equal(requests, 1, "terminal jobs must not be polled repeatedly");
    } finally {
      await act(async () => root.unmount());
      container.remove();
      window.sessionStorage.removeItem(ACTIVE_JOB_STORAGE_KEY);
      window.sessionStorage.removeItem(COMPLETED_JOB_NOTICE_KEY);
      globalThis.fetch = originalFetch;
      window.setInterval = originalInterval;
      window.clearInterval = originalClearInterval;
    }
  });
}

test("a slow older completion cannot erase a newer question or create duplicate polls", async () => {
  const originalFetch = globalThis.fetch, originalInterval = window.setInterval, originalClear = window.clearInterval;
  let poll!: () => void, resolve!: (response: Response) => void, requests = 0;
  window.setInterval = ((callback: () => void) => { poll = callback; return 1; }) as typeof window.setInterval;
  window.clearInterval = () => {};
  globalThis.fetch = async () => { requests += 1; return new Promise<Response>((done) => { resolve = done; }); };
  window.sessionStorage.removeItem(COMPLETED_JOB_NOTICE_KEY);
  window.sessionStorage.setItem(ACTIVE_JOB_STORAGE_KEY, JSON.stringify({ job_id: "earlier" }));
  const container = document.createElement("div"); document.body.append(container);
  const root = createRoot(container);
  try {
    await act(async () => root.render(<JobCompletionNoticeHost />));
    await act(async () => { poll(); poll(); });
    assert.equal(requests, 1);
    const newer = JSON.stringify({ job_id: "newer" });
    window.sessionStorage.setItem(ACTIVE_JOB_STORAGE_KEY, newer);
    await act(async () => resolve(Response.json({ job_id: "earlier", status: "completed" })));
    assert.equal(window.sessionStorage.getItem(ACTIVE_JOB_STORAGE_KEY), newer);
    assert.equal(window.sessionStorage.getItem(COMPLETED_JOB_NOTICE_KEY), null);
    assert.equal(container.textContent, "");
  } finally {
    await act(async () => root.unmount()); container.remove();
    window.sessionStorage.removeItem(ACTIVE_JOB_STORAGE_KEY);
    globalThis.fetch = originalFetch; window.setInterval = originalInterval; window.clearInterval = originalClear;
  }
});
