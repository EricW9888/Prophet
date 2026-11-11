import "./helpers/dom.mjs";
import assert from "node:assert/strict";
import { afterEach, beforeEach, test } from "node:test";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import MailboxIdentityReviews from "../src/components/MailboxIdentityReviews";

let root: Root;
let container: HTMLDivElement;
const originalFetch = globalThis.fetch;
const id = (index: number) => `00000000-0000-0000-0000-${String(index).padStart(12, "0")}`;
const summary = (index: number) => ({ evidence_id: id(index), title: `Synthetic receipt ${index}`, public_time: "2025-01-01T00:00:00Z", reason: "ambiguous_legacy_identity", candidate_count: 1, attention_score: 100, can_reconcile: true });
const detail = (index: number, posted = false) => ({ ...summary(index), content_excerpt: `Retained incoming body ${index}`, unavailable_candidate_count: 0, can_reconcile: !posted,
  transactions: posted ? [{ id: id(900), status: "corrected", action: "buy", quantity: "2", price: "3" }] : [],
  candidates: [{ evidence_id: id(800 + index), title: `Original ${index}`, public_time: null, content_excerpt: `Preserved candidate body ${index}`, transactions: [] }],
});
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
const button = (name: string) => [...container.querySelectorAll("button")].find((item) => item.textContent === name)!;
const click = (element: HTMLElement) => act(async () => element.click());

beforeEach(() => {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});
afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  globalThis.fetch = originalFetch;
});

test("receipt pages expose the full count and fetch original bodies only for the selected comparison", async () => {
  const requested: string[] = [];
  const mutations: string[] = [];
  globalThis.fetch = async (input, options) => {
    const url = String(input);
    requested.push(url);
    if (options?.method === "POST") mutations.push(url);
    if (url.includes("/page?")) {
      const offset = Number(new URL(url, "http://localhost").searchParams.get("offset"));
      return response({ items: offset ? [summary(26)] : Array.from({ length: 25 }, (_, i) => summary(i + 1)), total: 26, offset, limit: 25, query: "" });
    }
    return response(detail(26, true));
  };
  await act(async () => root.render(<MailboxIdentityReviews />));
  assert.equal(requested.length, 0);
  await click(button("Load identity reviews"));
  assert.match(container.textContent!, /1–25 of 26 pending/);
  assert.equal(container.querySelectorAll("article").length, 25);
  assert.equal(requested.length, 1);
  await click(button("Next receipts"));
  assert.match(container.textContent!, /26–26 of 26 pending/);
  await click(button("Compare receipts"));
  assert.match(requested.at(-1)!, new RegExp(id(26)));
  assert.match(container.textContent!, /already linked to ledger activity/);
  assert.equal(button("Confirm same receipt").disabled, true);
  assert.equal(button("Confirm separate receipt").disabled, true);
  assert.equal(container.querySelector(`a[href="/history/receipt/transaction/${id(900)}"]`)?.textContent?.includes("corrected"), true);
  assert.deepEqual(mutations, []);
});

test("a late original cannot replace the newer selected receipt", async () => {
  let release!: (value: Response) => void;
  const first = new Promise<Response>((resolve) => { release = resolve; });
  globalThis.fetch = async (input) => {
    const url = String(input);
    if (url.includes("/page?")) return response({ items: [summary(1), summary(2)], total: 2, offset: 0, limit: 25, query: "" });
    return url.endsWith(id(1)) ? first : response(detail(2));
  };
  await act(async () => root.render(<MailboxIdentityReviews />));
  await click(button("Load identity reviews"));
  const compare = [...container.querySelectorAll<HTMLButtonElement>("button")].filter((item) => item.textContent === "Compare receipts");
  await click(compare[0]);
  await click(compare[1]);
  await act(async () => release(response(detail(1))));
  assert.match(container.textContent!, /Retained incoming body 2/);
  assert.doesNotMatch(container.textContent!, /Retained incoming body 1/);
});

test("a rejected identity decision retains the exact receipt and comparison", async () => {
  const decisions: unknown[] = [];
  globalThis.fetch = async (input, options) => {
    const url = String(input);
    if (options?.method === "POST") {
      decisions.push(JSON.parse(String(options.body)));
      assert.ok(url.endsWith(id(1)));
      return response({ detail: "Synthetic comparison conflict" }, 400);
    }
    return response(url.includes("/page?") ? { items: [summary(1)], total: 1, offset: 0, limit: 25, query: "" } : detail(1));
  };
  await act(async () => root.render(<MailboxIdentityReviews />));
  await click(button("Load identity reviews"));
  await click(button("Compare receipts"));
  await click(button("Confirm separate receipt"));
  assert.match(container.textContent!, /Synthetic comparison conflict/);
  assert.match(container.textContent!, /Retained incoming body 1/);
  assert.match(container.textContent!, /Preserved candidate body 1/);
  assert.deepEqual(decisions, [{ decision: "create_distinct", existing_evidence_id: null }]);
});
