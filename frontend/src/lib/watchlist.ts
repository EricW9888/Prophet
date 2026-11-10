import type { Position } from "./api";

/** A watchlist is a view of securities; holdings don't create extra ledger rows. */
export function watchlistRows(holdings: Position[], tracked: Position[], order: string) {
  const positions = new Map<string, { position: Position; holding: boolean; tracked: boolean }>();
  for (const position of holdings) {
    if (!positions.has(position.security_id)) positions.set(position.security_id, { position, holding: true, tracked: false });
  }
  for (const position of tracked) {
    const existing = positions.get(position.security_id);
    if (existing) existing.tracked = true;
    else positions.set(position.security_id, { position, holding: false, tracked: true });
  }
  let stored: unknown;
  try { stored = JSON.parse(order); } catch { /* Default to holdings first. */ }
  const ids = new Set([...(Array.isArray(stored) ? stored.filter((id): id is string => typeof id === "string") : []), ...positions.keys()]);
  return [...ids].flatMap((id) => {
    const entry = positions.get(id);
    return entry ? [entry] : [];
  });
}
