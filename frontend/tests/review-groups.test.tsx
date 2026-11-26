import assert from "node:assert/strict";
import test from "node:test";

import { reviewFixLink } from "../src/components/ReviewItemActions";
import { groupReviewItems } from "../src/lib/review-groups";

const item = (id: string, item_type: string, next_action: string, item_label = id) =>
  ({ id, item_type, next_action, item_label }) as never;

test("the same task repeated collapses into one group at its first position", () => {
  const items = [
    item("a", "source_claim_record", "Assess"),
    item("r1", "mailbox_identity", "Compare receipts", "Your order has been executed"),
    item("r2", "mailbox_identity", "Compare receipts", "Your order has been executed"),
    item("b", "source_claim_record", "Assess differently"),
    item("r3", "mailbox_identity", "Compare receipts", "Your order has been executed"),
  ];
  const entries = groupReviewItems(items);
  assert.deepEqual(entries.map((e: { kind: string }) => e.kind), ["item", "group", "item"]);
  const group = entries[1] as { kind: "group"; items: { id: string }[]; label: string };
  assert.deepEqual(group.items.map((i) => i.id), ["r1", "r2", "r3"]);
  assert.equal(group.label, "Your order has been executed");
});

test("fewer than the threshold stay as separate items", () => {
  const entries = groupReviewItems([item("x", "t", "n"), item("y", "t", "n")]);
  assert.deepEqual(entries.map((e: { kind: string }) => e.kind), ["item", "item"]);
});

test("receipt checks link straight to where they are fixed", () => {
  assert.deepEqual(reviewFixLink({ item_type: "mailbox_identity" } as never), { href: "/settings?tab=data", label: "Open receipt comparison" });
  assert.equal(reviewFixLink({ item_type: "source_claim_record" } as never), null);
});

test("distinct claims that share a generic next step never merge", () => {
  const claims = ["c1", "c2", "c3", "c4"].map((id) => item(id, "source_claim_record", "Assess the claim", `Claim ${id}`));
  assert.deepEqual(groupReviewItems(claims).map((e: { kind: string }) => e.kind), ["item", "item", "item", "item"]);
});
