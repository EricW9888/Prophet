import "./helpers/dom.mjs";
import assert from "node:assert/strict";
import { afterEach, beforeEach, test } from "node:test";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { AppRouterContext } from "next/dist/shared/lib/app-router-context.shared-runtime.js";
import { PathnameContext, SearchParamsContext } from "next/dist/shared/lib/hooks-client-context.shared-runtime.js";
import ChatPage from "../src/app/chat/page";
import WorkStatusProvider from "../src/components/WorkStatusProvider";
import PresentationLanguageProvider from "../src/components/PresentationLanguageProvider";
import { ACTIVE_JOB_STORAGE_KEY } from "../src/lib/job-notifications";

const sessionId = "11111111-1111-4111-8111-111111111111";
const subjectId = "22222222-2222-4222-8222-222222222222";
const conversation = {session_id: sessionId, title: "Older research", subject_type: "entity", subject_id: subjectId, subject_name: "Synthetic company", created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z", latest_message: "Older question"};
const list = {conversations: [conversation], autonomous_reviews: []};
const originalFetch = globalThis.fetch;
let container: HTMLDivElement;
let root: Root;
let requests: string[];
let posts: Record<string, unknown>[];
let getConversations: () => unknown | Promise<unknown>;
let postTurn: () => Response | Promise<Response>;
let activeJob: Record<string, unknown> | null;
const response = (value: unknown) => new Response(JSON.stringify(value), {status: 200, headers: {"Content-Type": "application/json"}});
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return {promise, resolve};
}

beforeEach(() => {
  window.sessionStorage.clear(); window.localStorage.clear();
  window.requestAnimationFrame = callback => { callback(0); return 0; };
  window.matchMedia = () => ({matches: false, addEventListener() {}, removeEventListener() {}}) as unknown as MediaQueryList;
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
  container = document.createElement("div"); document.body.append(container);
  root = createRoot(container); requests = []; posts = [];
  activeJob = null;
  getConversations = () => list;
  postTurn = () => new Response("Synthetic request rejection", {status: 400});
  globalThis.fetch = async (input, init) => {
    const url = String(input); requests.push(url);
    if (url.endsWith("/agent/conversations")) {const value = await getConversations(); return value instanceof Response ? value : response(value);}
    if (url.endsWith("/agent/turn-jobs") && init?.method === "POST") {posts.push(JSON.parse(String(init.body))); return postTurn();}
    if (url.includes("/agent/turn-jobs?")) return response({jobs: []});
    if (url.endsWith("/agent/turn-jobs/synthetic-job") && activeJob) return response(activeJob);
    if (url.endsWith("/automation/status")) return response({automation_enabled: true, jobs: []});
    if (url.endsWith("/integrations/settings")) return response({presentation_language: "en"});
    if (url.includes("/agent/history?")) return response({session_id: sessionId, subject_type: "entity", entries: [{role: "assistant", content: "Saved prior answer", created_at: "2026-01-01T00:00:00Z"}]});
    if (url.endsWith("/agent/attachments/capabilities")) return response({accept: ".txt,.pdf,.png", max_files: 5, max_file_bytes: 25000000, max_turn_bytes: 50000000});
    throw new Error(`Unexpected test request: ${url}`);
  };
});

afterEach(async () => {
  await act(async () => root.unmount()); container.remove(); globalThis.fetch = originalFetch;
  window.sessionStorage.clear(); window.localStorage.clear();
});

async function render(params = "") {
  const router = {bfcacheId: "synthetic-router", push() {}, replace() {}, back() {}, forward() {}, refresh() {}, prefetch: async () => {}, hmrRefresh() {}};
  await act(async () => root.render(<AppRouterContext value={router}><PathnameContext value="/chat"><SearchParamsContext value={new URLSearchParams(params)}><PresentationLanguageProvider><WorkStatusProvider><ChatPage /></WorkStatusProvider></PresentationLanguageProvider></SearchParamsContext></PathnameContext></AppRouterContext>));
}
const composer = () => container.querySelector("textarea")!;
async function input(value: string) {
  await act(async () => {
    Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value")!.set!.call(composer(), value);
    composer().dispatchEvent(new Event("input", {bubbles: true}));
  });
}
async function click(button: HTMLButtonElement) {
  await act(async () => button.dispatchEvent(new MouseEvent("click", {bubbles: true})));
}

test("plain Research stays a new chat when saved conversations finish loading", async () => {
  const history = deferred<unknown>(); getConversations = () => history.promise;
  await render();
  assert.match(container.textContent!, /Loading saved chats/);
  assert.doesNotMatch(container.textContent!, /No saved chats yet/);
  await act(async () => history.resolve(list));
  assert.equal(container.querySelector("main h1")!.textContent, "New chat");
  assert.ok(!requests.some(url => url.includes("/agent/history?")));
});

test("a starter draft is not moved into saved history after a delayed list response", async () => {
  const history = deferred<unknown>(); getConversations = () => history.promise;
  await render();
  const starter = [...container.querySelectorAll("button")].find(button => button.textContent?.trim().endsWith("?"))!;
  await click(starter);
  const draft = composer().value;
  assert.ok(draft.length > 0);
  await act(async () => history.resolve(list));
  assert.equal(container.querySelector("main h1")!.textContent, "New chat");
  assert.equal(composer().value, draft);
  await act(async () => container.querySelector("form")!.dispatchEvent(new Event("submit", {bubbles: true, cancelable: true})));
  assert.equal(posts.length, 1);
  assert.equal(posts[0].session_id, undefined);
  assert.equal(posts[0].auto_execute, false);
});

test("input-only edits and an in-flight send retain their new-chat context", async () => {
  const history = deferred<unknown>(); getConversations = () => history.promise;
  const submission = deferred<Response>(); postTurn = () => submission.promise;
  await render();
  await input("Synthetic new question");
  assert.equal(container.querySelector<HTMLButtonElement>('button[aria-label="Send message"]')!.disabled, false);
  await act(async () => container.querySelector("form")!.dispatchEvent(new Event("submit", {bubbles: true, cancelable: true})));
  assert.equal(posts.length, 1);
  await act(async () => history.resolve(list));
  assert.equal(container.querySelector("main h1")!.textContent, "New chat");
  assert.ok(!requests.some(url => url.includes("/agent/history?")));
  await act(async () => submission.resolve(new Response("Synthetic rejection", {status: 400})));
  assert.equal(composer().value, "Synthetic new question");
  assert.equal(posts[0].session_id, undefined);
});

test("explicit saved-session links still open that exact history", async () => {
  await render(`session_id=${sessionId}`);
  assert.ok(requests.some(url => url.includes(`/agent/history?session_id=${sessionId}`)));
  assert.match(container.textContent!, /Saved prior answer/);
  await input("Follow-up on this saved conversation");
  await act(async () => container.querySelector("form")!.dispatchEvent(new Event("submit", {bubbles: true, cancelable: true})));
  assert.equal(posts[0].session_id, sessionId);
});

test("a pinned subject remains pinned while older conversations arrive", async () => {
  const history = deferred<unknown>(); getConversations = () => history.promise;
  await render(`subject_id=${subjectId}&subject_type=entity&subject_name=Synthetic+subject&q=Check+this+subject`);
  await act(async () => history.resolve(list));
  assert.equal(composer().value, "Check this subject");
  await act(async () => container.querySelector("form")!.dispatchEvent(new Event("submit", {bubbles: true, cancelable: true})));
  assert.equal(posts[0].subject_id, subjectId);
  assert.equal(posts[0].subject_type, "entity");
  assert.equal(posts[0].session_id, undefined);
});

test("history failure and retry preserve the draft and do not masquerade as empty history", async () => {
  getConversations = () => new Response(JSON.stringify({detail: "Synthetic history failure"}), {status: 403});
  await render();
  assert.match(container.textContent!, /Saved chats could not be loaded/);
  assert.doesNotMatch(container.textContent!, /No saved chats yet/);
  await input("Keep this unsent draft");
  getConversations = () => list;
  await click([...container.querySelectorAll("button")].find(button => button.textContent === "Retry saved chats")!);
  assert.equal(container.querySelector("main h1")!.textContent, "New chat");
  assert.equal(composer().value, "Keep this unsent draft");
  assert.doesNotMatch(container.textContent!, /Saved chats could not be loaded/);
  assert.ok(!requests.some(url => url.includes("/agent/history?")));
});

test("selecting a saved chat then starting a new one remains explicit", async () => {
  await render();
  const saved = [...container.querySelectorAll("button")].find(button => button.textContent?.includes("Older research"))!;
  await click(saved);
  assert.equal(container.querySelector("main h1")!.textContent, "Older research");
  assert.match(container.textContent!, /Saved prior answer/);
  await click([...container.querySelectorAll("button")].find(button => button.textContent === "Start new chat")!);
  assert.equal(container.querySelector("main h1")!.textContent, "New chat");
  await input("Separate new conversation");
  await act(async () => container.querySelector("form")!.dispatchEvent(new Event("submit", {bubbles: true, cancelable: true})));
  assert.equal(posts[0].session_id, undefined);
});

test("a saved active job restores its exact session independently of the conversation list", async () => {
  const history = deferred<unknown>(); getConversations = () => history.promise;
  activeJob = {job_id: "synthetic-job", session_id: sessionId, status: "deferred", resumable: true, request_message: "Saved in-flight question", created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z", events: []};
  window.sessionStorage.setItem(ACTIVE_JOB_STORAGE_KEY, JSON.stringify(activeJob));
  await render();
  await act(async () => history.resolve(list));
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 1250)); });
  assert.ok(requests.some(url => url.endsWith("/agent/turn-jobs/synthetic-job")));
  assert.ok(requests.some(url => url.includes(`/agent/history?session_id=${sessionId}`)));
  assert.equal(container.querySelector("main h1")!.textContent, "Older research");
  assert.match(container.textContent!, /Saved in-flight question/);
});
