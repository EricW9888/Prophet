"use client";

import { useState } from "react";
import { apiFetch } from "@/lib/api";

type Receipt = {
  evidence_id: string;
  title: string | null;
  public_time: string | null;
  mailbox_identity?: { folder?: string; uid_validity?: string; uid?: string } | null;
  content_excerpt?: string | null;
  content_truncated?: boolean;
  transactions?: Array<{ id: string; status: string; action: string; quantity: string; price: string | null }>;
};
type IdentityReview = Receipt & { reason: string; candidates: Receipt[] };

export default function MailboxIdentityReviews() {
  const [reviews, setReviews] = useState<IdentityReview[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  async function load() {
    setBusy(true);
    setError(null);
    try {
      setReviews(await apiFetch<IdentityReview[]>("/integrations/gmail/identity-reviews"));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to load receipt identity reviews.");
    } finally {
      setBusy(false);
    }
  }

  async function reconcile(receipt: IdentityReview, existingId?: string) {
    setBusy(true);
    setError(null);
    try {
      await apiFetch(`/integrations/gmail/identity-reviews/${receipt.evidence_id}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decision: existingId ? "link_existing" : "create_distinct", existing_evidence_id: existingId ?? null }),
      });
      setReviews((current) => current?.filter((item) => item.evidence_id !== receipt.evidence_id) ?? null);
      setNotice(existingId ? "Receipts linked. The preserved ledger entry and any corrections remain authoritative." : "Receipt confirmed as distinct. The next scoped sync will classify it before any posting.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to reconcile receipt identity.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mt-5 border-t border-slate-200 pt-5 dark:border-slate-800">
      <h3 className="text-sm font-semibold">Receipt identity review</h3>
      <p className="mt-2 text-sm text-slate-500">Ambiguous older receipts wait here before they can affect the portfolio. Compare original evidence and ledger history, then confirm whether this is the same message or a separate receipt.</p>
      <button type="button" disabled={busy} onClick={() => void load()} className="mt-3 rounded-md border border-slate-300 px-3 py-2 text-sm disabled:opacity-50 dark:border-slate-700">{busy ? "Working…" : "Load identity reviews"}</button>
      {error && <p role="alert" className="mt-3 text-sm text-rose-600">{error}</p>}
      {notice && <p role="status" className="mt-3 text-sm text-slate-600 dark:text-slate-300">{notice}</p>}
      {reviews?.length === 0 && <p className="mt-3 text-sm text-slate-500">No pending receipt identity reviews.</p>}
      {reviews?.map((review) => (
        <article key={review.evidence_id} className="mt-4 min-w-0 rounded-lg border border-amber-200 p-4 [overflow-wrap:anywhere] dark:border-amber-900">
          <ReceiptLink receipt={review} label="Incoming receipt" />
          <p className="mt-2 text-xs text-slate-500">{review.reason === "ambiguous_legacy_identity" ? "The earlier receipt lacks a verified account and mailbox identity." : review.reason === "provider_message_content_changed" ? "The provider message identifier was seen with different content. Compare both preserved messages." : "The mailbox identity changed and the available identifiers do not establish whether this is a duplicate."}</p>
          <div className="mt-3 space-y-3">
            {review.candidates.map((candidate) => (
              <div key={candidate.evidence_id} className="rounded-md bg-slate-50 p-3 dark:bg-slate-900">
                <ReceiptLink receipt={candidate} label="Preserved candidate" />
                <button type="button" disabled={busy} onClick={() => void reconcile(review, candidate.evidence_id)} className="mt-2 rounded-md border border-slate-300 px-3 py-2 text-sm disabled:opacity-50 dark:border-slate-700">Confirm same receipt</button>
              </div>
            ))}
          </div>
          <button type="button" disabled={busy} onClick={() => void reconcile(review)} className="mt-3 rounded-md border border-slate-300 px-3 py-2 text-sm disabled:opacity-50 dark:border-slate-700">Confirm separate receipt</button>
        </article>
      ))}
    </div>
  );
}

function ReceiptLink({ receipt, label }: { receipt: Receipt; label: string }) {
  return <div>
    <p className="text-xs text-slate-500">{label}</p>
    <a href={`/sources/evidence/${receipt.evidence_id}`} target="_blank" rel="noreferrer" className="mt-1 block text-sm underline">{receipt.title || "Untitled receipt"}</a>
    <p className="mt-1 text-xs text-slate-500">{receipt.public_time ? new Date(receipt.public_time).toLocaleString() : "Date unavailable"}{receipt.mailbox_identity?.folder && ` · Folder ${receipt.mailbox_identity.folder}`}</p>
    <details className="mt-2">
      <summary className="cursor-pointer text-sm">Read preserved content</summary>
      <pre className="mt-2 max-h-60 overflow-y-auto whitespace-pre-wrap rounded-md border border-slate-200 p-3 text-xs [overflow-wrap:anywhere] dark:border-slate-800">{receipt.content_excerpt || "Original content is unavailable. Inspect ledger history and other authoritative records before deciding."}</pre>
      {receipt.content_truncated && <p className="mt-1 text-xs text-slate-500">Excerpt limited to 5,000 characters.</p>}
    </details>
    {receipt.transactions?.map((transaction) => <p key={transaction.id} className="mt-2 text-xs text-slate-500">Ledger: {transaction.status} · {transaction.action} · quantity {transaction.quantity} · price {transaction.price ?? "unavailable"}</p>)}
  </div>;
}
