import "./helpers/dom.mjs";
import assert from "node:assert/strict";
import { test } from "node:test";
import React, { act, useLayoutEffect } from "react";
import { createRoot } from "react-dom/client";
import {
  AttachmentDrafts,
  AttachmentButton,
  useChatAttachments,
  transferredFiles,
} from "../src/components/ChatAttachments";

const caps = {
  accept: ".pdf,.png,.txt",
  max_files: 2,
  max_file_bytes: 25 * 1024 * 1024,
  max_turn_bytes: 50 * 1024 * 1024,
};
function receipt(name = "clipboard.png") {
  return {
    id: "11111111-1111-4111-8111-111111111111",
    name,
    content_type: "image/png",
    size_bytes: 30000,
    status: "ready",
    page_count: null,
    text_chars: 0,
    warnings: [],
    created_at: new Date().toISOString(),
  };
}
type Files = ReturnType<typeof useChatAttachments>;
async function mount(fetcher: typeof fetch) {
  const original = globalThis.fetch;
  globalThis.fetch = fetcher;
  let current!: Files;
  function Harness() {
    const files = useChatAttachments();
    useLayoutEffect(() => {
      current = files;
    });
    return (
      <>
        <AttachmentButton files={files} disabled={false} />
        <AttachmentDrafts files={files} />
        <button disabled={files.blocked}>Send</button>
      </>
    );
  }
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await act(async () => root.render(<Harness />));
  return {
    container,
    get files() {
      return current;
    },
    async close() {
      await act(async () => root.unmount());
      container.remove();
      globalThis.fetch = original;
    },
  };
}

test("clipboard images upload as binary FormData above the old 24 KB limit", async () => {
  let binary: FormData | null = null;
  const ui = await mount(async (_url, options) => {
    if (!options?.method) return Response.json(caps);
    binary = options.body as FormData;
    return Response.json(receipt());
  });
  try {
    const file = new File([new Uint8Array(30000)], "clipboard.png", {
      type: "image/png",
    });
    const transfer = {
      files: [file],
      items: [{ kind: "file", getAsFile: () => file }],
    } as unknown as DataTransfer;
    await act(async () => ui.files.addFiles(transferredFiles(transfer)));
    assert.equal(ui.files.attachments.length, 1);
    assert.equal(ui.files.blocked, false);
    assert.equal((binary!.get("file") as File).size, 30000);
    assert.match(ui.container.textContent!, /Ready to send/);
    assert.equal(
      ui.container
        .querySelector("img")!
        .getAttribute("src")!
        .includes("/_next/image"),
      false,
    );
  } finally {
    await ui.close();
  }
});

test("a removed or old-chat upload cannot reappear after its request completes", async () => {
  let finish!: (response: Response) => void;
  const ui = await mount(async (_url, options) =>
    !options?.method
      ? Response.json(caps)
      : new Promise<Response>((resolve) => {
          finish = resolve;
        }),
  );
  try {
    await act(async () => ui.files.addFiles([new File(["abc"], "first.pdf")]));
    assert.equal(ui.files.blocked, true);
    await act(async () => ui.files.clear());
    await act(async () => finish(Response.json(receipt("first.pdf"))));
    assert.deepEqual(ui.files.attachments, []);
    assert.equal(ui.files.blocked, false);
  } finally {
    await ui.close();
  }
});

test("failed file reads block sending until retry succeeds or the file is removed", async () => {
  let attempts = 0;
  const ui = await mount(async (_url, options) => {
    if (!options?.method) return Response.json(caps);
    return ++attempts === 1
      ? Response.json({ detail: "Invalid PDF" }, { status: 400 })
      : Response.json({
          ...receipt("report.pdf"),
          content_type: "application/pdf",
        });
  });
  try {
    await act(async () => ui.files.addFiles([new File(["pdf"], "report.pdf")]));
    assert.equal(ui.files.blocked, true);
    assert.match(ui.container.textContent!, /Invalid PDF/);
    const retry = ui.container.querySelector(
      'button[aria-label="Retry report.pdf"]',
    )!;
    await act(async () =>
      retry.dispatchEvent(new MouseEvent("click", { bubbles: true })),
    );
    assert.equal(ui.files.blocked, false);
    assert.equal(attempts, 2);
  } finally {
    await ui.close();
  }
});

test("clipboard item-only image payloads work and ordinary text stays ordinary text", () => {
  const file = new File(["image"], "clipboard.png", { type: "image/png" });
  assert.deepEqual(
    transferredFiles({
      files: [],
      items: [{ kind: "file", getAsFile: () => file }],
    } as unknown as DataTransfer),
    [file],
  );
  assert.deepEqual(
    transferredFiles({
      files: [],
      items: [{ kind: "string", getAsFile: () => null }],
    } as unknown as DataTransfer),
    [],
  );
});
