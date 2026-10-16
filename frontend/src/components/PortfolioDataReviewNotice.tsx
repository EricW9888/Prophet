import Link from "next/link";

export default function PortfolioDataReviewNotice({ count }: { count: number }) {
  if (!count) return null;
  return <aside className="min-w-0 rounded-lg border border-amber-200 bg-amber-50/50 p-4 text-sm dark:border-amber-900 dark:bg-amber-950/20">
    <p className="font-semibold">Portfolio data needs review · {count}</p>
    <p className="mt-1 text-muted">Receipt identity, posting holds or reconciliation discrepancies need your review before relying on the affected records. Investment research can continue; these holds remain in place.</p>
    <Link href="/verification?domain=portfolio_data" className="mt-2 inline-block text-action underline">Review portfolio data</Link>
  </aside>;
}
