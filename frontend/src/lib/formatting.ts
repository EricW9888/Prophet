/**
 * Safe formatting utilities for Prophet to prevent UI crashes on null/undefined data.
 */

export function safeFormatCurrency(value: number | null | undefined, maximumFractionDigits = 0): string {
  if (value == null || isNaN(value)) return "$0";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits,
  }).format(value);
}

export function safeFormatSignedCurrency(value: number | null | undefined): string {
  if (value == null || isNaN(value)) return "$0.00";
  const absVal = Math.abs(value);
  const formatted = new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  }).format(absVal);
  return `${value >= 0 ? "+" : "-"}${formatted}`;
}

export function safeFormatPct(value: number | null | undefined): string {
  if (value == null || isNaN(value)) return "0.00%";
  const sign = value >= 0 ? "+" : "";
  return `${sign}${value.toFixed(2)}%`;
}

/**
 * Converts technical IDs (snake_case or camelCase) into human-readable Title Case labels.
 */
export function formatUserLabel(label: string | null | undefined): string {
  if (!label) return "Pending";
  // Handle snake_case
  let result = label.replace(/_/g, " ");
  // Handle camelCase
  result = result.replace(/([a-z])([A-Z])/g, "$1 $2");
  // Title Case
  return result.replace(/\b\w/g, (c) => c.toUpperCase());
}

export function formatRegimeLabel(regime: string | null | undefined): string {
  if (!regime) return "Neutral Status";
  return formatUserLabel(regime);
}

const EXECUTION_FAILURE_LABELS: Array<[suffix: string, label: string]> = [
  ["service_unavailable", "service unavailable"],
  ["connection_error", "connection error"],
  ["invalid_response", "invalid structured response"],
  ["network_error", "network error"],
  ["rate_limited", "rate limited"],
  ["service_error", "service error"],
  ["unconfigured", "credentials missing"],
  ["unauthorized", "unauthorized"],
  ["unavailable", "unavailable"],
  ["timeout", "timeout"],
  ["failed", "request failed"],
];

function formatProviderId(provider: string): string {
  return provider
    .split("_")
    .filter(Boolean)
    .map(token => token.length <= 4 ? token.toUpperCase() : `${token[0].toUpperCase()}${token.slice(1)}`)
    .join(" ");
}

function formatFallbackExecution(reason: string): string {
  const normalized = reason.toLowerCase().replaceAll("-", "_");
  for (const [suffix, label] of EXECUTION_FAILURE_LABELS) {
    const marker = `_${suffix}`;
    if (!normalized.endsWith(marker)) continue;
    const provider = normalized.slice(0, -marker.length);
    return `Fallback: ${formatProviderId(provider)} ${label}`;
  }
  return `Fallback: ${formatUserLabel(reason)}`;
}

export function formatModelUsedLabel(value?: string | null): string {
  const raw = (value ?? "").trim();
  const normalized = raw.toLowerCase().replaceAll("-", "_");
  if (!raw) return "Unknown execution";
  if (normalized === "cache_hit:previous_analysis" || normalized.startsWith("cached:")) {
    return "Cached previous analysis";
  }
  if (normalized.startsWith("fallback:")) {
    return formatFallbackExecution(raw.slice("fallback:".length));
  }
  if (raw.includes("->")) {
    const [primary, recovery] = raw.split("->", 2);
    return `Recovered with ${formatProviderId(recovery)} (${formatProviderId(primary)} unavailable)`;
  }
  return formatProviderId(raw);
}
