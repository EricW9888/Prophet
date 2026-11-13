"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { FileText, Loader2, Paperclip, RotateCcw, X } from "lucide-react";
import { API_BASE, type ChatAttachment } from "../lib/api";
import Image from "next/image";

type Capabilities = {
  accept: string;
  max_files: number;
  max_file_bytes: number;
  max_turn_bytes: number;
  vision_model?: string | null;
};
export type DraftAttachment = {
  key: string;
  name: string;
  bytes: number;
  state: "uploading" | "ready" | "error";
  file?: File;
  receipt?: ChatAttachment;
  error?: string;
};

export function transferredFiles(transfer: DataTransfer): File[] {
  return transfer.files.length
    ? Array.from(transfer.files)
    : Array.from(transfer.items)
        .filter((item) => item.kind === "file")
        .flatMap((item) => {
          const file = item.getAsFile();
          return file ? [file] : [];
        });
}

async function errorMessage(response: Response) {
  try {
    const body = await response.json();
    return typeof body.detail === "string"
      ? body.detail
      : "The attachment could not be processed.";
  } catch {
    return "The attachment could not be processed.";
  }
}

export function useChatAttachments() {
  const [attachments, setAttachments] = useState<DraftAttachment[]>([]);
  const live = useRef<DraftAttachment[]>([]);
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [error, setError] = useState<string | null>(null);
  const mounted = useRef(true);
  const update = useCallback(
    (change: (previous: DraftAttachment[]) => DraftAttachment[]) => {
      const next = change(live.current);
      live.current = next;
      if (mounted.current) setAttachments(next);
    },
    [],
  );
  const refresh = useCallback(async () => {
    try {
      const response = await fetch(
        `${API_BASE}/agent/attachments/capabilities`,
      );
      if (!response.ok)
        throw new Error(
          "Attachment options are unavailable. Retry when Prophet is connected.",
        );
      const result = (await response.json()) as Capabilities;
      if (mounted.current) {
        setCapabilities(result);
        setError(null);
      }
    } catch (cause) {
      if (mounted.current)
        setError(
          cause instanceof Error
            ? cause.message
            : "Attachment options are unavailable.",
        );
    }
  }, []);
  useEffect(() => {
    mounted.current = true;
    void refresh();
    return () => {
      mounted.current = false;
    };
  }, [refresh]);

  const upload = useCallback(
    async (draft: DraftAttachment) => {
      if (!draft.file) return;
      try {
        const body = new FormData();
        body.append("file", draft.file, draft.name);
        const response = await fetch(`${API_BASE}/agent/attachments`, {
          method: "POST",
          body,
        });
        if (!response.ok) throw new Error(await errorMessage(response));
        const receipt = (await response.json()) as ChatAttachment;
        update((previous) =>
          previous.map((item) =>
            item.key === draft.key
              ? {
                  ...item,
                  state: "ready",
                  receipt,
                  file: undefined,
                  error: undefined,
                }
              : item,
          ),
        );
      } catch (cause) {
        update((previous) =>
          previous.map((item) =>
            item.key === draft.key
              ? {
                  ...item,
                  state: "error",
                  error:
                    cause instanceof Error ? cause.message : "Upload failed.",
                }
              : item,
          ),
        );
      }
    },
    [update],
  );

  const addFiles = useCallback(
    (files: Iterable<File>) => {
      const selected = Array.from(files);
      if (!selected.length) return;
      if (!capabilities) {
        setError(
          "Attachment options are still loading. Retry when Prophet is connected.",
        );
        return;
      }
      if (live.current.length + selected.length > capabilities.max_files) {
        setError(`Choose up to ${capabilities.max_files} files per message.`);
        return;
      }
      if (selected.some((file) => file.size > capabilities.max_file_bytes)) {
        setError(
          `Each file must be ${Math.floor(capabilities.max_file_bytes / 1024 / 1024)} MB or less.`,
        );
        return;
      }
      if (
        live.current.reduce((sum, item) => sum + item.bytes, 0) +
          selected.reduce((sum, file) => sum + file.size, 0) >
        capabilities.max_turn_bytes
      ) {
        setError(
          `Files must total ${Math.floor(capabilities.max_turn_bytes / 1024 / 1024)} MB or less per message.`,
        );
        return;
      }
      const additions: DraftAttachment[] = selected.map((file) => ({
        key: crypto.randomUUID(),
        name: file.name || "clipboard.png",
        bytes: file.size,
        file,
        state: "uploading",
      }));
      update((previous) => [...previous, ...additions]);
      setError(null);
      // Upload each selection in order; native PDF parsing is serialized by the server.
      void (async () => {
        for (const draft of additions) await upload(draft);
      })();
    },
    [capabilities, update, upload],
  );
  const clear = useCallback(() => {
    update(() => []);
    setError(null);
  }, [update]);
  const restore = useCallback(
    (value: DraftAttachment[]) => {
      update((current) => [
        ...value,
        ...current.filter(
          (item) => !value.some((restored) => restored.key === item.key),
        ),
      ]);
    },
    [update],
  );
  const remove = useCallback(
    (key: string) => {
      update((previous) => previous.filter((item) => item.key !== key));
    },
    [update],
  );
  const retry = useCallback(
    (key: string) => {
      const draft = live.current.find((item) => item.key === key);
      if (!draft?.file) return;
      update((previous) =>
        previous.map((item) =>
          item.key === key
            ? { ...item, state: "uploading", error: undefined }
            : item,
        ),
      );
      void upload(draft);
    },
    [update, upload],
  );
  return {
    attachments,
    capabilities,
    error,
    refresh,
    addFiles,
    clear,
    restore,
    remove,
    retry,
    blocked: attachments.some((item) => item.state !== "ready"),
  };
}

export function AttachmentButton({
  files,
  disabled,
}: {
  files: ReturnType<typeof useChatAttachments>;
  disabled: boolean;
}) {
  const input = useRef<HTMLInputElement>(null);
  return (
    <>
      <input
        ref={input}
        type="file"
        multiple
        hidden
        accept={files.capabilities?.accept}
        onChange={(event) => {
          if (event.currentTarget.files)
            files.addFiles(event.currentTarget.files);
          event.currentTarget.value = "";
        }}
      />
      <button
        type="button"
        className="workspace-control"
        disabled={disabled}
        onClick={() => input.current?.click()}
      >
        <Paperclip size={15} aria-hidden="true" /> Attach files
      </button>
    </>
  );
}

export function AttachmentDrafts({
  files,
}: {
  files: ReturnType<typeof useChatAttachments>;
}) {
  return (
    <>
      {files.attachments.length ? (
        <div className="mb-3 space-y-2">
          <ul aria-label="Attached files" className="flex flex-wrap gap-2">
            {files.attachments.map((file) => (
              <li
                key={file.key}
                className="flex min-w-0 max-w-full items-start gap-2 border border-line bg-panel p-2 text-xs"
              >
                {file.receipt?.content_type.startsWith("image/") ? (
                  <Image
                    unoptimized
                    width={48}
                    height={48}
                    src={`${API_BASE}/agent/attachments/${file.receipt.id}/content`}
                    alt=""
                    className="h-12 w-12 object-contain"
                  />
                ) : file.state === "uploading" ? (
                  <Loader2
                    size={18}
                    className="animate-spin"
                    aria-hidden="true"
                  />
                ) : (
                  <FileText size={18} aria-hidden="true" />
                )}
                <div className="min-w-0">
                  <p className="max-w-64 truncate font-medium">{file.name}</p>
                  <p role="status" className="text-muted">
                    {file.state === "uploading"
                      ? "Reading file…"
                      : file.state === "error"
                        ? file.error
                        : `${file.receipt?.page_count ? `${file.receipt.page_count} pages · ` : ""}${Math.ceil(file.bytes / 1024)} KB · Ready to send`}
                  </p>
                  {file.receipt?.warnings.map((warning) => (
                    <p
                      key={warning}
                      className="max-w-96 whitespace-normal text-amber-500"
                    >
                      {warning}
                    </p>
                  ))}
                  {file.receipt ? (
                    <a
                      href={`${API_BASE}/agent/attachments/${file.receipt.id}/content`}
                      target="_blank"
                      rel="noreferrer"
                      className="text-action underline"
                    >
                      Open file
                    </a>
                  ) : null}
                </div>
                {file.state === "error" ? (
                  <button
                    type="button"
                    aria-label={`Retry ${file.name}`}
                    onClick={() => files.retry(file.key)}
                  >
                    <RotateCcw size={14} />
                  </button>
                ) : null}
                <button
                  type="button"
                  aria-label={`Remove ${file.name}`}
                  onClick={() => files.remove(file.key)}
                >
                  <X size={14} />
                </button>
              </li>
            ))}
          </ul>
          <p className="text-xs text-muted">
            Files stay in this private Prophet instance for follow-ups. Sending
            uses the configured model; file contents are not accepted as
            portfolio records or research facts.
          </p>
        </div>
      ) : null}
      {files.error ? (
        <p role="alert" className="mb-2 text-xs text-red-500">
          {files.error}{" "}
          {!files.capabilities ? (
            <button
              type="button"
              className="underline"
              onClick={() => void files.refresh()}
            >
              Retry
            </button>
          ) : null}
        </p>
      ) : null}
    </>
  );
}

export function AttachmentReceipts({
  attachments,
  showReadStatus = true,
}: {
  attachments?: ChatAttachment[];
  showReadStatus?: boolean;
}) {
  if (!attachments?.length) return null;
  return (
    <ul
      aria-label="Message files"
      className="mt-3 flex flex-wrap gap-2 text-xs"
    >
      {attachments.map((file) => (
        <li
          key={file.id}
          className="min-w-0 max-w-full border border-line px-2 py-1"
        >
          <a
            className="text-action underline"
            href={`${API_BASE}/agent/attachments/${file.id}/content`}
            target="_blank"
            rel="noreferrer"
          >
            {file.name}
          </a>
          {file.page_count ? ` · ${file.page_count} pages` : ""}
          {showReadStatus && file.visual_count ? (
            <p className="text-muted">
              Visual reading: {file.visual_pages_read ?? 0}/{file.visual_count}
              {file.reader_model ? ` · ${file.reader_model}` : ""}
            </p>
          ) : null}
          {file.warnings.length ? (
            <details>
              <summary className="cursor-pointer text-amber-500">
                Read limitations
              </summary>
              <ul className="mt-1 space-y-1">
                {file.warnings.map((warning) => (
                  <li key={warning}>{warning}</li>
                ))}
              </ul>
            </details>
          ) : null}
        </li>
      ))}
    </ul>
  );
}
