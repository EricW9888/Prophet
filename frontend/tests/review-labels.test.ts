import assert from "node:assert/strict";
import test from "node:test";

import { reviewItemKind, reviewPriority, reviewTag } from "../src/lib/review-labels";

test("review items are named in words, with a readable fallback", () => {
  assert.equal(reviewItemKind("source_claim_record"), "Claim to check");
  assert.equal(reviewItemKind("mailbox_identity"), "Receipt to match");
  assert.equal(reviewItemKind("brand_new_kind"), "brand new kind");
});

test("tags read in sentence case and tickers stay as they are", () => {
  assert.equal(reviewTag("source track record"), "Source track record");
  assert.equal(reviewTag("SYNQ"), "SYNQ");
  assert.equal(reviewTag("early_inflection"), "Early inflection");
});

test("priority reads as a score out of 100", () => {
  assert.equal(reviewPriority(79.04), "Priority 79 of 100");
});
