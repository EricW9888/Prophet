"use client";

import { useRef, useState } from "react";
import { apiFetch } from "@/lib/api";
import { useWorkspaceReads } from "@/lib/workspace-reads";

type Receipt = {
  evidence_id: string;
  title: string | null;
  public_time: string | null;
  mailbox_identity?: { folder?: string } | null;
  content_excerpt?: string | null;
  content_truncated?: boolean;
  identity_pending?: boolean;
  transactions?: Array<{ id: string; status: string; action: string; quantity: string; price: string | null }>;
};
type Summary = Receipt & { reason: string | null; candidate_count: number; attention_score: number | null; can_reconcile: boolean };
type IdentityReview = Receipt & { reason: string | null; candidates: Receipt[]; unavailable_candidate_count: number; can_reconcile: boolean };
type Page = { items: Summary[]; total: number; limit: number; offset: number; query: string };

function reasonLabel(reason: string | null) {
  if (reason === "ambiguous_legacy_identity") return "The earlier receipt lacks a verified account and mailbox identity.";
  if (reason === "provider_message_content_changed") return "The same provider identifier was received with different content.";
  return "Available mailbox identifiers do not establish whether this is a duplicate.";
}

function recordedNumber(value: string) {
  const parsed = Number(value);
  return value.trim() && Number.isFinite(parsed) ? parsed.toLocaleString("en-US", { maximumSignificantDigits: 10 }) : value;
}

export default function MailboxIdentityReviews() {
  const [page, setPage] = useState<Page | null>(null);
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [working, setWorking] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<IdentityReview | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const selectedRef = useRef<string | null>(null);
  const pageGeneration = useRef(0);
  const { read, status } = useWorkspaceReads<"page" | "detail">();

  async function load(search = page?.query ?? "", offset = 0) {
    const generation = ++pageGeneration.current;
    setLoading(true);
    await read<Page>("page", `/integrations/gmail/identity-reviews/page?${new URLSearchParams({ limit: "25", offset: String(offset), query: search })}`, (result) => {
      setPage(result);
      setSelected(null);
      selectedRef.current = null;
      setDetail(null);
    });
    if (generation === pageGeneration.current) setLoading(false);
  }

  async function compare(id: string) {
    const next = selectedRef.current === id ? null : id;
    selectedRef.current = next;
    setSelected(next);
    setDetail(null);
    setActionError(null);
    if (!next) return;
    setDetailLoading(true);
    await read<IdentityReview>("detail", `/integrations/gmail/identity-reviews/${id}`, (result) => {
      if (selectedRef.current === id) setDetail(result);
    });
    if (selectedRef.current === id) setDetailLoading(false);
  }

  async function reconcile(receipt: IdentityReview, existingId?: string) {
    setWorking(true);
    setActionError(null);
    setNotice(null);
    try {
      await apiFetch(`/integrations/gmail/identity-reviews/${receipt.evidence_id}`, {
        method: "POST", body: JSON.stringify({ decision: existingId ? "link_existing" : "create_distinct", existing_evidence_id: existingId ?? null }),
      });
      setNotice(existingId ? "Receipts linked. Preserved ledger entries and corrections remain authoritative." : "Receipt confirmed as distinct. The next scoped sync classifies it before posting.");
      const offset = page && page.total - 1 <= page.offset ? Math.max(0, page.offset - page.limit) : page?.offset ?? 0;
      await load(page?.query ?? "", offset);
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Unable to reconcile receipt identity.");
    } finally {
      setWorking(false);
    }
  }

  return <section className="mt-5 min-w-0 space-y-3 border-t border-line pt-5" aria-label="Receipt identity review">
    <h3 className="text-sm font-semibold">Receipt identity review</h3>
    <p className="text-sm text-muted">Compare ambiguous messages with their preserved originals and ledger entries. Linking receipts never creates a second trade.</p>
    <button type="button" disabled={loading || working} onClick={() => void load()} className="workspace-control">{loading ? "Loading reviews…" : page ? "Refresh identity reviews" : "Load identity reviews"}</button>
    {status.page?.error && <p role="alert" className="text-sm text-rose-400">{status.page.error}{page && " Showing the previous results."}</p>}
    {notice && <p role="status" className="text-sm text-muted">{notice}</p>}
    {page && <>
      <form className="flex flex-wrap items-end gap-2" onSubmit={(event) => { event.preventDefault(); void load(query); }}>
        <label className="min-w-0 flex-1 text-sm">Search receipt titles<input type="search" value={query} onChange={(event) => setQuery(event.target.value)} maxLength={200} disabled={working} className="mt-1 block w-full border border-line bg-background px-3 py-2" /></label>
        <button type="submit" disabled={working} className="workspace-control">Search receipts</button>
      </form>
      <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
        <p>{page.total ? `${page.offset + 1}–${Math.min(page.offset + page.items.length, page.total)} of ${page.total} pending` : page.query ? "No matching pending receipts." : "No pending receipt identity reviews."}<span className="ml-2 text-muted">Priority, then newest first</span></p>
        <div className="flex gap-2"><button type="button" disabled={working || loading || page.offset === 0} onClick={() => void load(page.query, Math.max(0, page.offset - page.limit))} className="workspace-control">Previous receipts</button><button type="button" disabled={working || loading || page.offset + page.limit >= page.total} onClick={() => void load(page.query, page.offset + page.limit)} className="workspace-control">Next receipts</button></div>
      </div>
      {page.items.map((review) => <article key={review.evidence_id} className="min-w-0 space-y-3 border border-line p-4 [overflow-wrap:anywhere]">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0"><a href={`/sources/evidence/${review.evidence_id}`} className="font-medium text-action underline">{review.title || "Untitled receipt"}</a><p className="mt-1 text-xs text-muted">{review.public_time ? new Date(review.public_time).toLocaleString() : "Date unavailable"} · {review.candidate_count} preserved candidate{review.candidate_count === 1 ? "" : "s"}</p></div>
          <button type="button" aria-expanded={selected === review.evidence_id} disabled={working} onClick={() => void compare(review.evidence_id)} className="workspace-control">{selected === review.evidence_id ? "Close comparison" : "Compare receipts"}</button>
        </div>
        {selected === review.evidence_id && <>
          {detailLoading && <p role="status" className="text-sm text-muted">Loading preserved messages and ledger links…</p>}
          {!detailLoading && status.detail?.error && <p role="alert" className="text-sm text-rose-400">{status.detail.error}<button type="button" className="ml-2 underline" onClick={() => { selectedRef.current = null; void compare(review.evidence_id); }}>Retry comparison</button></p>}
          {detail && <>
            <p className="text-sm text-muted">{reasonLabel(detail.reason)}</p>
            {!detail.can_reconcile && <p className="border-l-2 border-amber-400 pl-3 text-sm">This receipt is already linked to ledger activity. Its identity cannot be rebound. Inspect the ledger receipts below.</p>}
            {detail.unavailable_candidate_count > 0 && <p className="text-sm text-amber-400">{detail.unavailable_candidate_count} preserved candidate{detail.unavailable_candidate_count === 1 ? " is" : "s are"} unavailable. Compare other authoritative records before deciding.</p>}
            <div className="grid min-w-0 gap-4 xl:grid-cols-2">
              <ReceiptLink receipt={detail} label="Incoming receipt" />
              <div className="min-w-0 space-y-4">{detail.candidates.map((candidate) => <div key={candidate.evidence_id} className="min-w-0 space-y-2 border border-line p-3">
                <ReceiptLink receipt={candidate} label="Preserved candidate" />
                {candidate.identity_pending && <p className="text-sm text-amber-400">Resolve the candidate identity first.</p>}
                <button type="button" disabled={working || !detail.can_reconcile || candidate.identity_pending} onClick={() => void reconcile(detail, candidate.evidence_id)} className="workspace-control">Confirm same receipt</button>
              </div>)}</div>
            </div>
            <button type="button" disabled={working || !detail.can_reconcile} onClick={() => void reconcile(detail)} className="workspace-control">Confirm separate receipt</button>
          </>}
          {actionError && <p role="alert" className="text-sm text-rose-400">{actionError}</p>}
        </>}
      </article>)}
    </>}
  </section>;
}

function ReceiptLink({ receipt, label }: { receipt: Receipt; label: string }) {
  return <div className="min-w-0 space-y-2">
    <p className="text-xs text-muted">{label}</p>
    <a href={`/sources/evidence/${receipt.evidence_id}`} target="_blank" rel="noreferrer" className="block text-sm text-action underline">{receipt.title || "Untitled receipt"}</a>
    <p className="text-xs text-muted">{receipt.public_time ? new Date(receipt.public_time).toLocaleString() : "Date unavailable"}{receipt.mailbox_identity?.folder && ` · Folder ${receipt.mailbox_identity.folder}`}</p>
    <details><summary className="cursor-pointer text-sm">Read preserved content</summary><pre className="mt-2 max-h-80 overflow-y-auto whitespace-pre-wrap border border-line p-3 text-xs [overflow-wrap:anywhere]">{receipt.content_excerpt || "Original content is unavailable. Inspect ledger history and other authoritative records before deciding."}</pre>{receipt.content_truncated && <p className="text-xs text-muted">Excerpt limited to 5,000 characters. Open the evidence record for the retained original.</p>}</details>
    {receipt.transactions?.map((transaction) => <a key={transaction.id} className="block text-xs text-action underline" href={`/history/receipt/transaction/${transaction.id}`} title={`Recorded quantity ${transaction.quantity}; price ${transaction.price ?? "unavailable"}`}>Ledger: {transaction.status} · {transaction.action} · quantity {recordedNumber(transaction.quantity)} · price {transaction.price === null ? "unavailable" : recordedNumber(transaction.price)}</a>)}
  </div>;
}
