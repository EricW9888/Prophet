import assert from "node:assert/strict";
import test from "node:test";

import { conversationGroup, groupConversations } from "../src/lib/chat-sidebar";

const now = new Date(2026, 10, 26, 12, 0, 0);
const chat = (title: string, updated: Date, subject?: string) =>
  ({ session_id: title, title, subject_name: subject ?? null, updated_at: updated.toISOString() });

test("conversations group by local calendar day", () => {
  assert.equal(conversationGroup(new Date(2026, 10, 26, 1).toISOString(), now), "Today");
  assert.equal(conversationGroup(new Date(2026, 10, 25, 23).toISOString(), now), "Yesterday");
  assert.equal(conversationGroup(new Date(2026, 10, 21).toISOString(), now), "Previous 7 days");
  assert.equal(conversationGroup(new Date(2026, 10, 1).toISOString(), now), "Older");
});

test("search matches title or subject, newest first, empty groups dropped", () => {
  const chats = [
    chat("Old NVIDIA question", new Date(2026, 9, 1), "NVDA"),
    chat("Morning check", new Date(2026, 10, 26, 9)),
    chat("Memory pricing", new Date(2026, 10, 26, 10), "MU"),
  ];
  const all = groupConversations(chats, "", now);
  assert.deepEqual(all.map((g) => g.label), ["Today", "Older"]);
  assert.deepEqual(all[0].items.map((c) => c.title), ["Memory pricing", "Morning check"]);
  assert.deepEqual(groupConversations(chats, "nvda", now).map((g) => g.items.map((c) => c.title)), [["Old NVIDIA question"]]);
  assert.deepEqual(groupConversations(chats, "nothing", now), []);
});
