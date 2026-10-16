import "./helpers/dom.mjs";
import assert from "node:assert/strict";
import { afterEach, beforeEach, test } from "node:test";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { PathnameContext, SearchParamsContext } from "next/dist/shared/lib/hooks-client-context.shared-runtime.js";
import ReviewPage from "../src/app/verification/page";
import PortfolioDataReviewNotice from "../src/components/PortfolioDataReviewNotice";
import ReviewItemActions from "../src/components/ReviewItemActions";
import PresentationLanguageProvider from "../src/components/PresentationLanguageProvider";
import type { ReviewDomain, ReviewQueueItem } from "../src/lib/api";

const A = "11111111-1111-4111-8111-111111111111";
const B = "22222222-2222-4222-8222-222222222222";
const C = "33333333-3333-4333-8333-333333333333";
let root: Root;
let container: HTMLDivElement;
let requests: string[];
let handleRequest: (url: string) => unknown | Promise<unknown>;
const originalFetch = globalThis.fetch;
const counts = { investment: 101, portfolio_data: 105 };

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}

function item(id: string, domain: ReviewDomain): ReviewQueueItem {
  return { id, item_id: id, item_label: domain === "investment" ? `Investment ${id}` : `Receipt ${id}`, item_type: domain === "investment" ? "position" : "mailbox_identity", review_domain: domain, subject_id: C, subject_type: "position", profile_id: B, evidence_id: domain === "portfolio_data" ? id : null, priority_score: 95, status: "pending", trigger_reason: "Synthetic concern", why_now_summary: "Synthetic evidence gap", next_action: "Compare original evidence", signal_tags: [], size_factor: 0, evidence_change_factor: 0, contradiction_pressure: 0, thesis_drift: 0, catalyst_proximity: 0, coverage_weakness: 0, created_at: "2026-01-01T00:00:00Z" };
}

function page(domain: ReviewDomain) {
  return { items: [item(domain === "investment" ? A : B, domain)], total: counts[domain], counts };
}

async function render(params: string) {
  await act(async () => { root.render(<PresentationLanguageProvider><PathnameContext value="/verification"><SearchParamsContext value={new URLSearchParams(params)}><ReviewPage /></SearchParamsContext></PathnameContext></PresentationLanguageProvider>); });
}

async function click(label: string) {
  const target = [...container.querySelectorAll("button")].find(node => node.textContent === label);
  assert.ok(target, `Missing button ${label}`);
  await act(async () => { target.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
}

beforeEach(() => {
  window.requestAnimationFrame = callback => { callback(0); return 0; };
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  requests = [];
  handleRequest = url => page(url.includes("portfolio_data") ? "portfolio_data" : "investment");
  globalThis.fetch = async input => {
    const url = String(input);
    requests.push(url);
    const value = url.endsWith("/integrations/settings") ? { presentation_language: "en" }
      : url.endsWith("/profiles") ? [{ id: B, subject_id: C, subject_type: "position", subject_name: "Synthetic holding" }]
        : await handleRequest(url);
    return new Response(JSON.stringify(value), { status: 200, headers: { "Content-Type": "application/json" } });
  };
});

afterEach(async () => {
  await act(async () => { root.unmount(); });
  container.remove();
  globalThis.fetch = originalFetch;
});

test("a portfolio data backlog stays visible with a direct counted review path", async () => {
  await act(async () => { root.render(<PortfolioDataReviewNotice count={105} />); });
  assert.match(container.textContent!, /Portfolio data needs review · 105/);
  assert.match(container.textContent!, /holds remain in place/);
  assert.equal(container.querySelector("a")?.getAttribute("href"), "/verification?domain=portfolio_data");
  await act(async () => { root.render(<PortfolioDataReviewNotice count={0} />); });
  assert.equal(container.textContent, "");
});

test("changing review purpose keeps the exact concern and unsent challenge draft", async () => {
  const params = `domain=investment&item_id=${A}`;
  await render(params);
  await click("Prepare a challenge");
  const draft = container.querySelector<HTMLTextAreaElement>("#challenge-text")!.value;
  assert.match(draft, /Investment/);
  const filters = container.querySelector('nav[aria-label="Review purpose"]')!;
  assert.match(filters.textContent!, /All · 206/);
  assert.match(filters.textContent!, /Portfolio data · 105/);
  assert.ok(filters.querySelector('a[href*="portfolio_data"]')?.getAttribute("href")?.includes(`item_id=${A}`));
  handleRequest = url => url.endsWith(`/queue/${A}`) ? item(A, "investment") : page("portfolio_data");
  await render(`domain=portfolio_data&item_id=${A}`);
  assert.equal(container.querySelector<HTMLTextAreaElement>("#challenge-text")!.value, draft);
  assert.match(container.textContent!, /Subject: Synthetic holding/);
  assert.ok(requests.some(url => url.endsWith(`/queue/${A}`)));
  assert.ok(requests.every(url => !url.endsWith("/verification") && !url.includes("/research")));
});

test("a late page from the former domain cannot append receipts to the investment view", async () => {
  const late = deferred<ReturnType<typeof page>>();
  handleRequest = url => url.includes("offset=") ? late.promise : page(url.includes("portfolio_data") ? "portfolio_data" : "investment");
  await render("domain=portfolio_data");
  await click("Show more concerns");
  await render("domain=investment");
  await act(async () => { late.resolve(page("portfolio_data")); });
  assert.match(container.textContent!, /Investment/);
  assert.doesNotMatch(container.textContent!, /Receipt 222/);
  assert.equal(container.querySelector('nav[aria-label="Review purpose"] a[aria-current="page"]')?.textContent?.trim(), "Investment · 101");
});

test("a failed filter keeps the last loaded domain and uses it for subsequent pagination", async () => {
  await render("domain=investment");
  handleRequest = url => {
    if (url.includes("portfolio_data")) throw new Error("Synthetic unavailable view");
    return { ...page("investment"), items: [] };
  };
  await render("domain=portfolio_data");
  assert.match(container.textContent!, /last loaded Investment queue remains available/);
  assert.match(container.textContent!, /Investment 111/);
  await click("Show more concerns");
  assert.ok(requests.at(-1)?.includes("domain=investment&offset=1"));
});

test("data reconciliation actions do not turn a position discrepancy into a thesis challenge", async () => {
  const review = { ...item(A, "portfolio_data"), item_type: "position", evidence_id: null };
  await act(async () => { root.render(<ReviewItemActions item={review} />); });
  assert.equal(container.querySelector("a")?.getAttribute("href"), "/positions");
  assert.doesNotMatch(container.textContent!, /Investigate|challenge|research profile/);
});
