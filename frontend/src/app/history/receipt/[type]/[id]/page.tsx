"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import AppNav from "@/components/AppNav";
import PageHeader from "@/components/PageHeader";
import type { Transaction } from "@/lib/api";
import { formatUserLabel, safeFormatCurrency } from "@/lib/formatting";
import { useWorkspaceReads } from "@/lib/workspace-reads";
import { isRecordId } from "@/lib/workflows";

const endpoints = {
  statement: "/portfolio/broker-equity/",
  transaction: "/portfolio/transactions/",
  cash: "/portfolio/cash-entries/",
} as const;
type ReceiptType = keyof typeof endpoints;
type StatementReceipt = {
  recorded_at: string;
  statement_hash: string;
  statement: { as_of: string; equity: string; cash: string | null; holdings: Array<{ ticker: string; quantity: string }> };
  reconciliation_at_import: { reasons: string[]; ledger_hash: string; ledger_cash: string | null };
  other_receipts_at_this_time: Array<{ id: string; recorded_at: string }>;
};
type CashReceipt = { entry_type: string; amount: string; executed_at: string };

function time(value: string) {
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
}

function money(value: string | number | null | undefined) {
  return value == null ? "Unavailable" : safeFormatCurrency(Number(value), 2);
}

function ReceiptBody({ type, id }: { type: ReceiptType; id: string }) {
  const [data, setData] = useState<StatementReceipt | CashReceipt | Transaction | null>(null);
  const { read, status } = useWorkspaceReads<"receipt">();
  useEffect(() => { void read("receipt", endpoints[type] + id, setData); }, [type, id, read]);
  if (status.receipt?.error) return <p role="alert" className="text-sm text-muted">{status.receipt.error}</p>;
  if (!data) return <p role="status" className="text-sm text-muted">Loading receipt…</p>;

  if ("statement" in data) {
    const { statement, reconciliation_at_import: check } = data;
    return <div className="space-y-4">
      <dl className="grid gap-3 sm:grid-cols-2">
        <div><dt className="text-muted">Statement time</dt><dd>{time(statement.as_of)}</dd></div>
        <div><dt className="text-muted">Saved</dt><dd>{time(data.recorded_at)}</dd></div>
        <div><dt className="text-muted">Reported account equity</dt><dd>{money(statement.equity)}</dd></div>
        <div><dt className="text-muted">Reported cash</dt><dd>{money(statement.cash)}</dd></div>
      </dl>
      <section className="space-y-2 border-t border-line pt-3">
        <h2 className="font-semibold">Reconciliation when saved</h2>
        {check.reasons.length
          ? <ul className="list-disc pl-4">{check.reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul>
          : <p>Holdings and cash matched the ledger.</p>}
        <p className="text-muted">Ledger cash {money(check.ledger_cash)}. The chart checks the current ledger, including later corrections.</p>
      </section>
      <section className="space-y-2 border-t border-line pt-3">
        <h2 className="font-semibold">Reported holdings</h2>
        {statement.holdings.length
          ? <ul className="divide-y divide-line">
            {statement.holdings.map((item) => <li key={item.ticker} className="flex justify-between py-1"><span>{item.ticker}</span><span>{item.quantity}</span></li>)}
          </ul>
          : <p>Cash only.</p>}
      </section>
      {data.other_receipts_at_this_time.length > 0 && <section className="space-y-2 border-t border-line pt-3">
        <h2 className="font-semibold">Other receipts for this statement time</h2>
        <ul className="space-y-1">{data.other_receipts_at_this_time.map((other) => (
          <li key={other.id}><Link className="text-action underline" href={`/history/receipt/statement/${other.id}`}>Saved {time(other.recorded_at)}</Link></li>
        ))}</ul>
      </section>}
      <details className="border-t border-line pt-3">
        <summary className="cursor-pointer text-muted">Receipt fingerprints</summary>
        <dl className="mt-2 space-y-2 break-all">
          <div><dt>Statement</dt><dd className="font-mono">{data.statement_hash}</dd></div>
          <div><dt>Ledger at import</dt><dd className="font-mono">{check.ledger_hash}</dd></div>
        </dl>
      </details>
    </div>;
  }
  if ("entry_type" in data) {
    return <dl className="space-y-2">
      <div><dt className="text-muted">Cash activity</dt><dd>{formatUserLabel(data.entry_type)} · {money(data.amount)}</dd></div>
      <div><dt className="text-muted">Recorded time</dt><dd>{time(data.executed_at)}</dd></div>
    </dl>;
  }
  return <div className="space-y-3">
    <dl className="space-y-2">
      <div><dt className="text-muted">Action</dt><dd>{formatUserLabel(data.action)} {data.ticker}</dd></div>
      <div><dt className="text-muted">Recorded time</dt><dd>{time(data.executed_at)}</dd></div>
      <div><dt className="text-muted">Status</dt><dd>{formatUserLabel(data.status ?? "settled")}</dd></div>
      <div><dt className="text-muted">{data.position_id ? "Quantity and price" : "Recorded amount"}</dt><dd>{data.position_id ? `${data.quantity} · ` : ""}{money(data.price)}</dd></div>
    </dl>
    {data.notes && <p className="text-muted">{data.notes}</p>}
    {data.superseded_by_id && <Link className="text-action underline" href={`/history/receipt/transaction/${data.superseded_by_id}`}>Open replacement receipt</Link>}
  </div>;
}

export default function PortfolioReceiptPage() {
  const { type, id } = useParams<{ type: string; id: string }>();
  const known = (type === "statement" || type === "transaction" || type === "cash") && isRecordId(id);
  return <div className="min-h-screen bg-background text-foreground">
    <AppNav active="portfolio" />
    <main className="workspace-frame space-y-4 py-6">
      <PageHeader title={type === "statement" ? "Broker statement receipt" : "Ledger receipt"}
        description={type === "statement" ? "Review the statement values and the reconciliation recorded when they were saved." : "Review the recorded event and its ledger status."}
        actions={<Link href="/" className="workspace-control">Return to portfolio</Link>} />
      <section className="border border-line bg-panel p-4 text-sm sm:p-5">
        {known ? <ReceiptBody key={type + id} type={type} id={id} /> : <p>Receipt unavailable.</p>}
      </section>
    </main>
  </div>;
}
