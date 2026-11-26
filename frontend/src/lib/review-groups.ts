import type { ReviewQueueItem } from "@/lib/api";

export type ReviewEntry =
  | { kind: "item"; item: ReviewQueueItem }
  | { kind: "group"; key: string; label: string; items: ReviewQueueItem[] };

/**
 * Collapse items that are the same task (same type, label, and next step) into one
 * entry once there are at least `threshold` of them, keeping the first one's
 * position. "480 of the same receipt check" is one task, not 480.
 */
export function groupReviewItems(items: ReviewQueueItem[], threshold = 3): ReviewEntry[] {
  // Same type, same label, and same next step: genuinely the same task. Distinct claims
  // share a generic next step but each needs its own verdict, so they never merge.
  const keyOf = (item: ReviewQueueItem) => [item.item_type, item.item_label, item.next_action ?? ""].join("|");
  const counts = new Map<string, number>();
  for (const item of items) counts.set(keyOf(item), (counts.get(keyOf(item)) ?? 0) + 1);
  const groups = new Map<string, ReviewQueueItem[]>();
  const entries: ReviewEntry[] = [];
  for (const item of items) {
    const key = keyOf(item);
    if ((counts.get(key) ?? 0) < threshold) {
      entries.push({ kind: "item", item });
      continue;
    }
    const members = groups.get(key);
    if (members) {
      members.push(item);
      continue;
    }
    const fresh = [item];
    groups.set(key, fresh);
    entries.push({ kind: "group", key, label: item.item_label, items: fresh });
  }
  return entries;
}
