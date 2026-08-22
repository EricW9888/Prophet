"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { ArrowLeft, ExternalLink } from "lucide-react";

import AppNav from "@/components/AppNav";
import { apiFetch, SourceEvidenceDetail } from "@/lib/api";
import { formatUserLabel } from "@/lib/formatting";
import { normalizeSourceOrigin } from "@/lib/sourceOrigin";

export default function EvidenceReceiptPage() {
  const params = useParams<{ id: string }>();
  const evidenceId = Array.isArray(params.id) ? params.id[0] : params.id;
  const [receipt, setReceipt] = useState<SourceEvidenceDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    async function loadReceipt() {
      if (!evidenceId) return;
      try {
        setLoading(true);
        setReceipt(await apiFetch<SourceEvidenceDetail>(`/sources/evidence/${evidenceId}`));
        setError(null);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Unable to load evidence receipt.");
      } finally {
        setLoading(false);
      }
    }

    void loadReceipt();
  }, [evidenceId]);

  const metadataRows = useMemo(() => evidenceMetadataRows(receipt?.metadata), [receipt?.metadata]);
  const origin = useMemo(() => normalizeSourceOrigin(receipt), [receipt]);

  return (
    <main className="min-h-screen bg-[#fafafa] text-gray-900 selection:bg-indigo-100 selection:text-indigo-900 dark:bg-black dark:text-gray-100 dark:selection:bg-indigo-900 dark:selection:text-indigo-100">
      <AppNav active="sources" />

      <div className="mx-auto max-w-5xl px-4 py-10 sm:px-6 lg:px-8">
        <Link href="/sources" className="mb-6 inline-flex items-center gap-2 text-sm font-semibold text-gray-500 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-100">
          <ArrowLeft className="h-4 w-4" />
          Sources
        </Link>

        {loading ? (
          <div className="rounded-3xl border border-gray-200 bg-white p-8 text-sm text-gray-500 shadow-sm dark:border-gray-800 dark:bg-[#0d0d0d] dark:text-gray-400">
            Loading receipt...
          </div>
        ) : error || !receipt ? (
          <div className="rounded-3xl border border-rose-100 bg-rose-50 p-8 text-sm font-semibold text-rose-600 dark:border-rose-900/40 dark:bg-rose-950/10 dark:text-rose-300">
            {error ?? "Evidence receipt not found."}
          </div>
        ) : (
          <div className="space-y-6">
            <section className="rounded-3xl border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-800 dark:bg-[#0d0d0d]">
              <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
                <div className="min-w-0">
                  <div className="mb-3 flex flex-wrap items-center gap-2">
                    <StatusPill>{formatUserLabel(receipt.source_item_type)}</StatusPill>
                    <StatusPill>{receipt.source_name}</StatusPill>
                    <OriginPill kind={origin.origin_kind}>{origin.origin_label}</OriginPill>
                    <StatusPill tone={receipt.is_processed ? "good" : "neutral"}>
                      {receipt.is_processed ? "processed" : "unprocessed"}
                    </StatusPill>
                  </div>
                  <h1 className="text-2xl font-black tracking-tight sm:text-3xl">
                    {receipt.title || "Untitled evidence receipt"}
                  </h1>
                  <p className="mt-2 text-sm text-gray-500 dark:text-gray-400">
                    Evidence ID {receipt.id}
                  </p>
                </div>
                {receipt.url ? (
                  <a
                    href={receipt.url}
                    target="_blank"
                    rel="noreferrer"
                    className="inline-flex shrink-0 items-center gap-2 rounded-xl border border-gray-200 px-3 py-2 text-sm font-semibold text-gray-600 hover:border-gray-300 hover:text-gray-900 dark:border-gray-800 dark:text-gray-300 dark:hover:border-gray-700"
                  >
                    Open source
                    <ExternalLink className="h-4 w-4" />
                  </a>
                ) : null}
              </div>

              <div className="mt-6 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                <ReceiptMetric label="Source type" value={formatUserLabel(receipt.source_type)} />
                <ReceiptMetric label="Origin" value={origin.origin_label} />
                <ReceiptMetric label="Author" value={receipt.author || "Unknown"} />
                <ReceiptMetric label="Public time" value={formatReceiptDate(receipt.public_time)} />
                <ReceiptMetric label="Event time" value={formatReceiptDate(receipt.event_time)} />
              </div>
            </section>

            <section className="grid gap-6 lg:grid-cols-[1.1fr_0.9fr]">
              <div className="rounded-3xl border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-800 dark:bg-[#0d0d0d]">
                <h2 className="text-sm font-bold uppercase tracking-wider text-gray-500 dark:text-gray-400">
                  Extracted receipt text
                </h2>
                <div className="mt-4 rounded-2xl border border-gray-100 bg-gray-50 p-4 text-sm leading-relaxed text-gray-700 dark:border-gray-800 dark:bg-gray-950 dark:text-gray-300">
                  {receipt.source_item_summary || receipt.source_item_excerpt || "No extracted text is attached to this receipt yet."}
                </div>
              </div>

              <div className="rounded-3xl border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-800 dark:bg-[#0d0d0d]">
                <h2 className="text-sm font-bold uppercase tracking-wider text-gray-500 dark:text-gray-400">
                  Provenance metadata
                </h2>
                <div className="mt-4 space-y-2">
                  <MetadataRow label="External ID" value={receipt.external_id} />
                  <MetadataRow label="Origin detail" value={origin.origin_detail} />
                  <MetadataRow label="Ingested" value={formatReceiptDate(receipt.ingest_time)} />
                  <MetadataRow label="Action eligible" value={formatReceiptDate(receipt.eligible_action_time)} />
                  <MetadataRow label="Processing" value={formatUserLabel(receipt.source_item_processing_status)} />
                  {metadataRows.map((row) => (
                    <MetadataRow key={row.label} label={formatUserLabel(row.label)} value={row.value} />
                  ))}
                </div>
              </div>
            </section>
          </div>
        )}
      </div>
    </main>
  );
}

function ReceiptMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-2xl border border-gray-100 bg-gray-50 px-4 py-3 dark:border-gray-800 dark:bg-gray-950">
      <div className="text-[10px] font-bold uppercase tracking-widest text-gray-400">{label}</div>
      <div className="mt-1 truncate text-sm font-bold text-gray-900 dark:text-gray-100">{value}</div>
    </div>
  );
}

function MetadataRow({ label, value }: { label: string; value?: string | null }) {
  if (!value) return null;
  return (
    <div className="rounded-2xl border border-gray-100 px-3 py-2 text-sm dark:border-gray-800">
      <div className="text-[10px] font-bold uppercase tracking-widest text-gray-400">{label}</div>
      <div className="mt-1 break-words text-gray-700 dark:text-gray-300">{value}</div>
    </div>
  );
}

function StatusPill({ children, tone = "neutral" }: { children: string; tone?: "neutral" | "good" }) {
  const className =
    tone === "good"
      ? "border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-900 dark:bg-emerald-950/30 dark:text-emerald-300"
      : "border-gray-200 bg-gray-50 text-gray-600 dark:border-gray-800 dark:bg-gray-950 dark:text-gray-300";
  return (
    <span className={`rounded-full border px-2.5 py-1 text-[10px] font-bold uppercase tracking-widest ${className}`}>
      {children}
    </span>
  );
}

function OriginPill({ children, kind }: { children: string; kind?: string | null }) {
  const normalized = (kind || "").toLowerCase();
  const className =
    normalized === "manual"
      ? "border-sky-200 bg-sky-50 text-sky-700 dark:border-sky-900 dark:bg-sky-950/30 dark:text-sky-300"
      : normalized === "email"
      ? "border-amber-200 bg-amber-50 text-amber-800 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-300"
      : normalized === "automation"
      ? "border-violet-200 bg-violet-50 text-violet-700 dark:border-violet-900 dark:bg-violet-950/30 dark:text-violet-300"
      : normalized === "discovery"
      ? "border-cyan-200 bg-cyan-50 text-cyan-700 dark:border-cyan-900 dark:bg-cyan-950/30 dark:text-cyan-300"
      : normalized === "chat"
      ? "border-fuchsia-200 bg-fuchsia-50 text-fuchsia-700 dark:border-fuchsia-900 dark:bg-fuchsia-950/30 dark:text-fuchsia-300"
      : normalized === "disclosure"
      ? "border-orange-200 bg-orange-50 text-orange-700 dark:border-orange-900 dark:bg-orange-950/30 dark:text-orange-300"
      : "border-gray-200 bg-gray-50 text-gray-600 dark:border-gray-800 dark:bg-gray-950 dark:text-gray-300";
  return (
    <span className={`rounded-full border px-2.5 py-1 text-[10px] font-bold uppercase tracking-widest ${className}`}>
      {children}
    </span>
  );
}

function formatReceiptDate(value?: string | null): string {
  if (!value) return "Unknown";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Unknown";
  return date.toLocaleString(undefined, { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" });
}

function evidenceMetadataRows(metadata?: Record<string, unknown>) {
  if (!metadata) return [];
  const preferredKeys = ["sender", "uid", "document_type", "confidence", "parser", "subject"];
  return preferredKeys
    .map((key) => ({ label: key, value: metadataValue(metadata[key]) }))
    .filter((row): row is { label: string; value: string } => Boolean(row.value));
}

function metadataValue(value: unknown): string | null {
  if (value == null) return null;
  if (typeof value === "string") return value.trim() || null;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value);
}
