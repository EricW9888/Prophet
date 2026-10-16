import "./helpers/dom.mjs";
import assert from "node:assert/strict";
import { afterEach, beforeEach, test } from "node:test";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import EvidenceReceipt from "../src/components/EvidenceReceipt";
import EvidenceTranslationPanel from "../src/components/EvidenceTranslationPanel";
import PresentationLanguageProvider from "../src/components/PresentationLanguageProvider";

const A = "11111111-1111-4111-8111-111111111111";
const B = "22222222-2222-4222-8222-222222222222";
let root: Root;
let container: HTMLDivElement;
let language = "es";
let handleRequest: (url: string, init?: RequestInit) => unknown | Promise<unknown>;
const originalFetch = globalThis.fetch;

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}

function receipt(id: string, title: string) {
  return { id, title, source_item_type: "manual_note", source_name: "Synthetic source", source_type: "manual", metadata: { synthetic: true }, is_processed: false };
}

function translation(id: string, lang: string, text: string | null) {
  return { evidence_id: id, target_language: lang, source_hash: `hash-${id}`, status: text ? "cached" : "untranslated", translated_text: text, original_url: `/sources/evidence/${id}#original-source-text` };
}

async function render(child: React.ReactNode) {
  await act(async () => { root.render(<PresentationLanguageProvider>{child}</PresentationLanguageProvider>); });
}

async function click(text: string) {
  const target = [...container.querySelectorAll("button,a")].find(node => node.textContent === text);
  assert.ok(target, `Missing control: ${text}`);
  await act(async () => { target.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true })); });
}

beforeEach(() => {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  language = "es";
  handleRequest = url => { throw new Error(`Unexpected test request: ${url}`); };
  globalThis.fetch = async (input, init) => {
    const url = String(input);
    const value = url === "/api_proxy/integrations/settings"
      ? { presentation_language: language }
      : await handleRequest(url, init);
    return new Response(JSON.stringify(value), { status: 200, headers: { "Content-Type": "application/json" } });
  };
});

afterEach(async () => {
  await act(async () => { root.unmount(); });
  container.remove();
  globalThis.fetch = originalFetch;
});

test("a late receipt response cannot replace the receipt selected after navigation", async () => {
  const pending = deferred<ReturnType<typeof receipt>>();
  handleRequest = url => {
    if (url.endsWith(`/evidence/${A}`)) return pending.promise;
    if (url.endsWith(`/evidence/${B}`)) return receipt(B, "Receipt B");
    if (url.includes("/translation")) return translation(B, new URL(url, window.location.href).searchParams.get("language")!, null);
    throw new Error(`Unexpected test request: ${url}`);
  };
  await render(<EvidenceReceipt evidenceId={A} />);
  await render(<EvidenceReceipt evidenceId={B} />);
  assert.equal(container.querySelector("h1")?.textContent, "Receipt B");
  await act(async () => { pending.resolve(receipt(A, "Receipt A")); });
  assert.equal(container.querySelector("h1")?.textContent, "Receipt B");
  assert.doesNotMatch(container.textContent!, /Receipt A/);
});

test("a late translation cannot discard the retained view for the newly selected language", async () => {
  const pending = deferred<ReturnType<typeof translation>>();
  handleRequest = (url, init) => {
    const selected = new URL(url, window.location.href).searchParams.get("language")!;
    if (init?.method === "POST") return pending.promise;
    return translation(A, selected, selected === "fr" ? "Traduction retenue" : null);
  };
  await render(<EvidenceTranslationPanel evidenceId={A} />);
  await click("Traducir con el proveedor configurado");
  language = "fr";
  await act(async () => { window.dispatchEvent(new Event("prophet:language-saved")); });
  assert.match(container.textContent!, /Traduction retenue/);
  await act(async () => { pending.resolve(translation(A, "es", "Traducción anterior")); });
  assert.match(container.textContent!, /Traduction retenue/);
  assert.doesNotMatch(container.textContent!, /Traducción anterior/);
});

test("a late original cannot appear under a different evidence identity", async () => {
  const pending = deferred<{ text: string; source_hash: string }>();
  handleRequest = url => {
    if (url.endsWith("/original-text")) return pending.promise;
    const id = url.includes(A) ? A : B;
    return translation(id, new URL(url, window.location.href).searchParams.get("language")!, "Synthetic retained translation");
  };
  await render(<EvidenceTranslationPanel evidenceId={A} />);
  await click("Leer original sin cambios");
  await render(<EvidenceTranslationPanel evidenceId={B} />);
  await act(async () => { pending.resolve({ text: "Original for receipt A", source_hash: `hash-${A}` }); });
  assert.doesNotMatch(container.textContent!, /Original for receipt A/);
  assert.match(container.textContent!, /Synthetic retained translation/);
});

test("a bounded translation can finish after the ordinary API read timeout", async context => {
  context.mock.timers.enable({ apis: ["setTimeout"] });
  const pending = deferred<ReturnType<typeof translation>>();
  let generationSignal: AbortSignal | null | undefined;
  handleRequest = (url, init) => {
    if (init?.method === "POST") {
      generationSignal = init.signal;
      return pending.promise;
    }
    return translation(A, "es", null);
  };
  await render(<EvidenceTranslationPanel evidenceId={A} />);
  await click("Traducir con el proveedor configurado");
  await act(async () => { context.mock.timers.tick(46_000); });
  assert.equal(generationSignal?.aborted, false);
  await act(async () => { pending.resolve(translation(A, "es", "Traducción completada")); });
  assert.match(container.textContent!, /Traducción completada/);
});

test("an older settings refresh cannot revert a newer saved presentation language", async () => {
  const initial = deferred<{ presentation_language: string }>();
  let settingsReads = 0;
  globalThis.fetch = async input => {
    const url = String(input);
    const value = url === "/api_proxy/integrations/settings"
      ? (++settingsReads === 1 ? await initial.promise : { presentation_language: "fr" })
      : translation(A, new URL(url, window.location.href).searchParams.get("language")!, "Synthetic translation");
    return new Response(JSON.stringify(value), { status: 200 });
  };
  await render(<EvidenceTranslationPanel evidenceId={A} />);
  await act(async () => { window.dispatchEvent(new Event("prophet:language-saved")); });
  assert.equal(container.querySelector("section")?.lang, "fr");
  await act(async () => { initial.resolve({ presentation_language: "es" }); });
  assert.equal(container.querySelector("section")?.lang, "fr");
  assert.match(container.textContent!, /Vue traduite de la source/);
});

test("an extraction retry completing after navigation cannot replace another receipt", async () => {
  const retry = deferred<object>();
  let oldReceiptReads = 0;
  handleRequest = (url, init) => {
    if (init?.method === "POST") {
      assert.ok(url.endsWith(`/evidence/${A}/retry-extraction`));
      return retry.promise;
    }
    if (url.endsWith(`/evidence/${A}`)) {
      oldReceiptReads++;
      return { ...receipt(A, "Receipt A"), processing: { overall_status: "retry_scheduled", extraction_status: "retry_scheduled", content_status: "completed", transcript_status: "not_applicable", investment_object_status: "pending", cleanup_status: "not_applicable", extraction_attempt_count: 1, persisted_object_count: 0 } };
    }
    if (url.endsWith(`/evidence/${B}`)) return receipt(B, "Receipt B");
    if (url.includes("/translation")) return translation(url.includes(A) ? A : B, new URL(url, window.location.href).searchParams.get("language")!, null);
    throw new Error(`Unexpected test request: ${url}`);
  };
  await render(<EvidenceReceipt evidenceId={A} />);
  await click("Queue retry now");
  await render(<EvidenceReceipt evidenceId={B} />);
  await act(async () => { retry.resolve({ queued: true }); });
  assert.equal(oldReceiptReads, 2);
  assert.equal(container.querySelector("h1")?.textContent, "Receipt B");
});

test("navigation clears the previous receipt while the new receipt is still loading", async () => {
  const next = deferred<ReturnType<typeof receipt>>();
  handleRequest = url => {
    if (url.endsWith(`/evidence/${A}`)) return receipt(A, "Receipt A");
    if (url.endsWith(`/evidence/${B}`)) return next.promise;
    return translation(A, new URL(url, window.location.href).searchParams.get("language")!, null);
  };
  await render(<EvidenceReceipt evidenceId={A} />);
  await render(<EvidenceReceipt evidenceId={B} />);
  assert.doesNotMatch(container.textContent!, /Receipt A/);
  assert.match(container.textContent!, /Loading source receipt/);
  await act(async () => { next.resolve(receipt(B, "Receipt B")); });
  assert.equal(container.querySelector("h1")?.textContent, "Receipt B");
});

test("generation still has a finite deadline and exposes recovery when it expires", async context => {
  context.mock.timers.enable({ apis: ["setTimeout"] });
  let generationSignal: AbortSignal | null | undefined;
  handleRequest = (url, init) => {
    if (init?.method === "POST") {
      generationSignal = init.signal;
      return new Promise((_, reject) => {
        init.signal?.addEventListener("abort", () => reject(new DOMException("Timed out", "AbortError")), { once: true });
      });
    }
    return translation(A, "es", null);
  };
  await render(<EvidenceTranslationPanel evidenceId={A} />);
  await click("Traducir con el proveedor configurado");
  await act(async () => { context.mock.timers.tick(50_001); });
  assert.equal(generationSignal?.aborted, true);
  const retry = [...container.querySelectorAll("button")].find(button => button.textContent === "Reintentar");
  assert.ok(retry && !retry.disabled);
  assert.ok([...container.querySelectorAll("a")].some(link => link.textContent === "Leer original sin cambios"));
});

test("a competing translation rechecks the cache without submitting another generation", async () => {
  let posts = 0;
  let reads = 0;
  handleRequest = (url, init) => {
    if (init?.method === "POST") {
      posts++;
      return { ...translation(A, "es", null), status: "translation_in_progress" };
    }
    if (new URL(url, window.location.href).searchParams.get("language") === "en") return translation(A, "en", null);
    reads++;
    return translation(A, "es", reads > 1 ? "Resultado conservado" : null);
  };
  await render(<EvidenceTranslationPanel evidenceId={A} />);
  await click("Traducir con el proveedor configurado");
  assert.match(container.textContent!, /Ya se está generando una traducción/);
  assert.ok(![...container.querySelectorAll("button")].some(button => button.textContent === "Traducir con el proveedor configurado"));
  await click("Reintentar");
  assert.match(container.textContent!, /Resultado conservado/);
  assert.equal(posts, 1);
  assert.equal(reads, 2);
});
