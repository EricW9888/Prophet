import type { ShadowExperiment } from "@/lib/api";
import { formatUserLabel, safeFormatCurrency } from "@/lib/formatting";
import CapturedValueChart, { type ChartEvent } from "./CapturedValueChart";

export default function ShadowEquityTimeline({ experiment }: { experiment: ShadowExperiment }) {
  const details = experiment.run_details;
  const manual = experiment.execution_mode === "manual";
  const invalidated = manual ? null : details.performance_invalidated_reason;
  const manualMarks = details.manual_equity_marks ?? [];
  const superseded = manualMarks.filter((row) => row.superseded_at).length;
  const points = manual ? manualMarks.filter((row) => !row.superseded_at).map((row) => ({
    id: `manual-${row.captured_at}-${row.receipt_hash}`, at: row.captured_at, value: row.equity,
    detail: row.prices.length
      ? `${row.prices.length} priced holdings · cash ${safeFormatCurrency(row.cash, 2)} · ${formatUserLabel(row.prices[0].session)} ${row.prices[0].quote_time.slice(0, 10)} UTC · receipt ${row.receipt_hash.slice(0, 12)}`
      : `${row.basis === "opening_cash" ? "Opening balance" : "Cash balance"} · receipt ${row.receipt_hash.slice(0, 12)}`,
  })) : invalidated ? [] : (details.performance_observations ?? []).flatMap((row, index) => (
    row.captured_at && Number.isFinite(row.equity) ? [{ id: `mark-${index}`, at: row.captured_at, value: row.equity, detail: "Recorded paper equity at checkpoint" }] : []
  ));
  const orderById = new Map(experiment.orders.map((order) => [order.id, order]));
  const events: ChartEvent[] = [
    ...experiment.fills.map((fill): ChartEvent => ({
      id: `fill-${fill.id}`, at: fill.filled_at, kind: "fill",
      label: `${formatUserLabel(fill.side)} ${fill.quantity} ${orderById.get(fill.order_id)?.ticker ?? "shares"} at ${safeFormatCurrency(fill.price, 2)}`,
      detail: orderById.get(fill.order_id)?.rationale ?? undefined,
    })),
    ...(details.decision_history ?? []).flatMap((checkpoint, index): ChartEvent[] => {
      if (!checkpoint.observed_at) return [];
      const actions = (checkpoint.decisions ?? []).flatMap((decision) => decision.action && decision.ticker ? [`${formatUserLabel(decision.action)} ${decision.ticker}`] : []);
      return [{ id: `decision-${index}`, at: checkpoint.observed_at, kind: "decision",
        label: `Checkpoint ${checkpoint.step_index}${actions.length ? ` · ${actions.join(", ")}` : " review"}`,
        detail: checkpoint.why_now ?? checkpoint.checkpoint_objective ?? undefined,
      }];
    }),
  ];
  return <div className="mt-3 min-w-0 space-y-3">
    <p className="text-xs text-muted">Paper equity is cash plus marked holdings at each recorded {manual ? "valuation" : "checkpoint"}. Fill and decision markers show when they happened; they don’t supply equity values between marks.</p>
    {manual && details.manual_equity_status?.status === "incomplete" && <p className="text-sm text-muted">No new mark: the latest valuation lacked matching USD prices for the full paper account ({details.manual_equity_status.covered_positions}/{details.manual_equity_status.total_positions} priced).</p>}
    {manual && superseded > 0 && <p className="text-xs text-muted">{superseded} earlier {superseded === 1 ? "mark was" : "marks were"} superseded after a ledger correction.</p>}
    {invalidated ? <p className="text-sm text-muted">Stored equity was superseded ({formatUserLabel(invalidated).toLowerCase()}). It is not plotted.</p>
      : points.length === 0 ? <p className="text-sm text-muted">No valid dated equity marks were retained for this run.</p> : <p className="text-xs text-muted">{points.length} recorded {points.length === 1 ? "mark" : "marks"} · USD</p>}
    {(points.length > 0 || events.length > 0) && <CapturedValueChart label="Paper equity" points={points} events={events} />}
  </div>;
}
