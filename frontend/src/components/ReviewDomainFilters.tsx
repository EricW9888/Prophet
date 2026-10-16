import Link from "next/link";
import type { ReviewDomain, ReviewQueueCounts } from "@/lib/api";

export function reviewDomain(value: string | null): ReviewDomain | null {
  return value === "investment" || value === "portfolio_data" ? value : null;
}

export function reviewPagePath(domain: ReviewDomain | null, offset = 0) {
  const params = new URLSearchParams();
  if (domain) params.set("domain", domain);
  if (offset) params.set("offset", String(offset));
  return `/review/queue/page${params.size ? `?${params}` : ""}`;
}

// Filtering the queue must not replace the owner's prepared challenge draft.
export function reviewSelectionKey(targetKey: string) {
  const params = new URLSearchParams(targetKey);
  params.delete("domain");
  return params.toString();
}

export function reviewDomainLabel(domain: ReviewDomain | null) {
  return domain === "investment" ? "Investment" : domain === "portfolio_data" ? "Portfolio data" : "All";
}

export default function ReviewDomainFilters({ domain, counts, targetKey }: { domain: ReviewDomain | null; counts: ReviewQueueCounts; targetKey: string }) {
  return <nav aria-label="Review purpose" className="mt-4 flex flex-wrap gap-2">
    {([null, "investment", "portfolio_data"] as const).map(value => {
      const params = new URLSearchParams(targetKey);
      if (value) params.set("domain", value); else params.delete("domain");
      const count = value ? counts[value] : counts.investment + counts.portfolio_data;
      return <Link key={value ?? "all"} href={`/verification${params.size ? `?${params}` : ""}`} aria-current={domain === value ? "page" : undefined} className={`rounded border px-3 py-2 text-sm ${domain === value ? "border-action bg-action/10 text-action" : "border-line text-muted"}`}>
        {reviewDomainLabel(value)} · {count}
      </Link>;
    })}
  </nav>;
}
