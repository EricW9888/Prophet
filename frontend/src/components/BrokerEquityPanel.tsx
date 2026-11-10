"use client";

import { type FormEvent, useCallback, useEffect, useState } from "react";
import { apiFetch } from "@/lib/api";
import { formatUserLabel, safeFormatCurrency, safeFormatSignedCurrency } from "@/lib/formatting";
import { useWorkspaceReads } from "@/lib/workspace-reads";
import CapturedValueChart from "./CapturedValueChart";

type Statement = {
  as_of: string;
  equity: string;
  cash: string | null;
  holdings_text: string;
  cash_only: boolean;
};
type Preview = {
  reconciliation: {
    reasons: string[];
    holdings_differences: Array<{ ticker: string; book_quantity: string; broker_quantity: string }>;
    ledger_cash: string | null;
  };
  preview_token: string;
};
type History = {
  points: Array<{ id: string; as_of: string; value: string | null; reasons: string[] }>;
  events: Array<{
    id: string; at: string; action: string; ticker: string | null;
    quantity: string | null; cash: string | null; receipt_type: "transaction" | "cash";
  }>;
  older_events_omitted: boolean;
};

const endpoint = "/portfolio/broker-equity";
const field = "w-full border border-line bg-background px-3 py-2 text-sm";

export default function BrokerEquityPanel() {
  const [history, setHistory] = useState<History | null>(null);
  const [busy, setBusy] = useState(false);
  const [preview, setPreview] = useState<{ input: Statement; result: Preview } | null>(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const { read, status } = useWorkspaceReads<"history">();
  const load = useCallback(() => read<History>("history", endpoint, setHistory), [read]);
  useEffect(() => { void load(); }, [load]);

  async function compare(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const fields = new FormData(event.currentTarget);
    const date = new Date(String(fields.get("as_of")));
    setError("");
    setMessage("");
    setPreview(null);
    if (!Number.isFinite(date.getTime())) {
      setError("Enter the date and time shown on the statement.");
      return;
    }
    const input: Statement = {
      as_of: date.toISOString(),
      equity: String(fields.get("equity")).trim(),
      cash: String(fields.get("cash")).trim() || null,
      holdings_text: String(fields.get("holdings")).trim(),
      cash_only: fields.has("cash_only"),
    };
    setBusy(true);
    try {
      const result = await apiFetch<Preview>(endpoint + "/preview", { method: "POST", body: JSON.stringify(input) });
      setPreview({ input, result });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Statement comparison failed.");
    } finally {
      setBusy(false);
    }
  }

  async function save() {
    if (!preview) return;
    setBusy(true);
    setError("");
    try {
      await apiFetch(endpoint, {
        method: "POST", body: JSON.stringify({ statement: preview.input, preview_token: preview.result.preview_token }),
      });
      setMessage(preview.result.reconciliation.reasons.length ? "Statement saved as a gap." : "Statement saved and reconciled.");
      setPreview(null);
      await load();
    } catch (reason) {
      setPreview(null);
      setError(reason instanceof Error ? reason.message : "Statement could not be saved.");
    } finally {
      setBusy(false);
    }
  }

  const comparison = preview?.result.reconciliation;
  return <section className="min-w-0 space-y-3 border border-line bg-panel p-4 sm:p-5" aria-label="Broker account equity">
    <div>
      <h2 className="font-semibold">Broker account equity</h2>
      <p className="mt-1 text-xs text-muted">Statement equity includes cash. Each point checks your statement against the ledger at that time. Missing or mismatched records leave a gap.</p>
    </div>
    {status.history?.error && <div className="flex items-center gap-2 text-sm text-muted" role="status">
      {history ? "Showing the last loaded history. Refresh failed." : "Broker equity history could not be loaded."}
      <button type="button" className="workspace-control" onClick={() => void load()}>Retry</button>
    </div>}
    {history?.points.length ? <CapturedValueChart label="Broker equity" eventsLabel="Ledger events"
      points={history.points.map((point) => ({
        id: point.id, at: point.as_of, value: point.value === null ? null : Number(point.value),
        detail: point.reasons.join(" ") || "Statement holdings and cash match the ledger.",
        href: "/history/receipt/statement/" + point.id, receiptLabel: "Open statement receipt", appReceipt: true,
      }))}
      events={history.events.map((event) => ({
        id: event.receipt_type + "-" + event.id, at: event.at, kind: "ledger",
        label: [formatUserLabel(event.action), event.quantity, event.ticker].filter(Boolean).join(" "),
        detail: event.cash === null ? undefined : `Cash impact ${safeFormatSignedCurrency(Number(event.cash))}`,
        href: `/history/receipt/${event.receipt_type}/${event.id}`, receiptLabel: "Open ledger receipt", appReceipt: true,
      }))} />
      : !status.history?.error && <p className="text-sm text-muted">{history ? "Import a dated statement to start your account equity history." : "Loading statement history…"}</p>}
    {history?.older_events_omitted && <p className="text-xs text-muted">The timeline shows the latest 250 ledger events in the displayed range.</p>}
    <details className="border-t border-line pt-3">
      <summary className="cursor-pointer text-sm font-semibold">Import broker statement</summary>
      <p className="mt-2 text-xs text-muted">Use a statement covering the full USD account represented by this ledger. Enter account equity, cash balance, and all holdings from the same point in time.</p>
      <form onSubmit={(event) => void compare(event)} onChange={() => { setPreview(null); setError(""); setMessage(""); }} className="mt-3 space-y-3">
        <fieldset disabled={busy} className="space-y-3">
          <div className="grid min-w-0 gap-3 md:grid-cols-3">
            <label className="min-w-0 space-y-1 text-sm"><span>Statement time (local)</span><input required name="as_of" type="datetime-local" step="1" className={field} /></label>
            <label className="min-w-0 space-y-1 text-sm"><span>Account equity (USD)</span><input required name="equity" type="number" step="0.01" className={field} /></label>
            <label className="min-w-0 space-y-1 text-sm"><span>Cash balance (USD)</span><input name="cash" type="number" step="0.01" className={field} placeholder="If available" /></label>
          </div>
          <label className="block space-y-1 text-sm"><span>Holdings</span><textarea name="holdings" className={field} rows={4} maxLength={262144} placeholder={"One ticker and share count per line, or CSV with Symbol and Quantity columns"} /></label>
          <label className="flex items-center gap-2 text-sm"><input type="checkbox" name="cash_only" /> The statement has no holdings</label>
          <button type="submit" className="workspace-control">Preview statement</button>
        </fieldset>
      </form>
      {comparison && <div className="mt-3 space-y-2 border border-line p-3" role="status">
        <p className="text-sm font-medium">{comparison.reasons.length ? "This statement will appear as a gap." : "Holdings and cash match the ledger."}</p>
        {preview && <p className="text-xs text-muted">Statement time {new Date(preview.input.as_of).toLocaleString(undefined, { timeZoneName: "short" })}</p>}
        {comparison.reasons.length > 0 && <ul className="list-disc pl-4 text-xs text-muted">{comparison.reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul>}
        {comparison.holdings_differences.length > 0 && <ul className="list-disc pl-4 text-xs text-muted">
          {comparison.holdings_differences.map((difference) => <li key={difference.ticker}>{difference.ticker} · ledger {difference.book_quantity} · statement {difference.broker_quantity}</li>)}
        </ul>}
        {comparison.ledger_cash !== null && <p className="text-xs text-muted">Ledger cash at statement time {safeFormatCurrency(Number(comparison.ledger_cash), 2)}</p>}
        <button type="button" className="workspace-control" disabled={busy} onClick={() => void save()}>Save statement receipt</button>
      </div>}
      {error && <p className="mt-3 text-sm text-rose-500" role="alert">{error}</p>}
      {message && <p className="mt-3 text-sm text-muted" role="status">{message}</p>}
    </details>
  </section>;
}
