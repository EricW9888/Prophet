import type { AgentConversationSummary } from "@/lib/api";

export type ConversationGroup = { label: string; items: AgentConversationSummary[] };

const GROUPS = ["Today", "Yesterday", "Previous 7 days", "Older"] as const;
export const ARCHIVED_GROUP = "Archived";

function startOfDay(value: Date): number {
  return new Date(value.getFullYear(), value.getMonth(), value.getDate()).getTime();
}

/** Which date group a conversation's last update falls in, by local calendar day. */
export function conversationGroup(updatedAt: string, now: Date): (typeof GROUPS)[number] {
  const time = new Date(updatedAt);
  if (Number.isNaN(time.getTime())) return "Older";
  const days = Math.round((startOfDay(now) - startOfDay(time)) / 86_400_000);
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days < 7) return "Previous 7 days";
  return "Older";
}

/**
 * Filter by title or subject, newest first, grouped by date; empty groups are
 * dropped. Archived chats are kept out of the date groups and listed last in
 * their own group, so search still finds them.
 */
export function groupConversations(conversations: AgentConversationSummary[], query: string, now: Date = new Date()): ConversationGroup[] {
  const term = query.trim().toLocaleLowerCase();
  const matches = conversations
    .filter((item) => !term || `${item.title} ${item.subject_name ?? ""}`.toLocaleLowerCase().includes(term))
    .sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at));
  const current = matches.filter((item) => !item.archived);
  return [
    ...GROUPS.map((label) => ({ label, items: current.filter((item) => conversationGroup(item.updated_at, now) === label) })),
    { label: ARCHIVED_GROUP, items: matches.filter((item) => item.archived) },
  ].filter((group) => group.items.length > 0);
}
