import assert from "node:assert/strict";
import test from "node:test";
import { readResearchEntry, researchHref, researchTurnContext, reviewRecordLink, reviewSubject, reviewResearchQuestion, reviewChallengeHref, selectReviewContext } from "../src/lib/workflows.ts";

const subject = { subject_id: "11111111-1111-4111-8111-111111111111", subject_type: "entity", subject_name: "Synthetic A & B" };
const profile = "22222222-2222-4222-8222-222222222222";
const session = "33333333-3333-4333-8333-333333333333";

test("a concern travels from its profile into an unsent draft with exact subject identity", () => {
  const question = "What changed? Compare +12% with −3%. [source 1]";
  const entry = readResearchEntry(new URL(researchHref(subject, question), "http://localhost").searchParams);
  assert.deepEqual(entry.subject, subject);
  assert.equal(entry.question, question);
  assert.equal(entry.sessionId, null);
  assert.equal(entry.invalid, false);
  assert.deepEqual(researchTurnContext(entry.sessionId, entry.subject), { subject_id: subject.subject_id, subject_type: "entity" });
});

test("a conversation notice opens its exact session without carrying another subject into the turn", () => {
  const entry = readResearchEntry(new URLSearchParams({ session_id: session, ...subject }));
  assert.equal(entry.sessionId, session);
  assert.equal(entry.subject, null);
  assert.deepEqual(researchTurnContext(session, subject), { session_id: session });
});

test("unknown, incomplete and external-looking identifiers never become a pinned context", () => {
  for (const params of [{ subject_id: "javascript:alert(1)", subject_type: "entity" }, { subject_id: subject.subject_id }, { ...subject, subject_type: "raw_evidence" }, { session_id: "not-a-session" }]) {
    const entry = readResearchEntry(new URLSearchParams(params));
    assert.equal(entry.invalid, true);
    assert.equal(entry.subject, null);
    assert.equal(entry.sessionId, null);
  }
});

test("an ordinary chat and a legacy question link do not invent a subject or session", () => {
  const normal = readResearchEntry(new URLSearchParams());
  assert.equal(normal.hasEntry, false);
  const legacy = readResearchEntry(new URLSearchParams({ q: "Challenge my assumption" }));
  assert.equal(legacy.question, "Challenge my assumption");
  assert.deepEqual(researchTurnContext(legacy.sessionId, legacy.subject), {});
});

test("review navigation uses resolved profile and receipt IDs rather than queue or subject IDs", () => {
  const item = { id: session, item_id: subject.subject_id, item_type: "position", item_label: subject.subject_name, ...subject, profile_id: profile };
  assert.deepEqual(reviewRecordLink(item), { href: `/profile/${profile}`, label: "Open subject and evidence" });
  assert.equal(reviewSubject(item).subject_id, subject.subject_id);
  assert.equal(reviewRecordLink({ ...item, profile_id: null }), null);
  assert.deepEqual(reviewRecordLink({ ...item, evidence_id: profile }), { href: `/sources/evidence/${profile}`, label: "Inspect original receipt" });
});

test("unresolved records keep their concern without guessing a profile or treating paper runs as subjects", () => {
  assert.equal(reviewSubject({ item_type: "entity", item_id: subject.subject_id }), null);
  assert.equal(reviewRecordLink({ item_type: "unknown", item_id: subject.subject_id }), null);
  assert.deepEqual(reviewRecordLink({ experiment_id: profile }), { href: `/shadow#experiment-${profile}`, label: "Inspect paper run" });
});

test("a prepared research question retains why now and the requested check", () => {
  const question = reviewResearchQuestion({ item_label: "Synthetic subject", why_now_summary: "Dated evidence contradicts the view", trigger_reason: "older summary", next_action: "Inspect the original filing" });
  assert.match(question, /Dated evidence contradicts the view/);
  assert.match(question, /Inspect the original filing/);
  assert.match(question, /contrary evidence/);
  assert.doesNotMatch(question, /older summary/);
});

test("a queue refresh between click and navigation retains the exact subject and unsent question", () => {
  const item = { id: session, item_id: subject.subject_id, item_type: "entity", ...subject, profile_id: profile, item_label: subject.subject_name, why_now_summary: "Synthetic dated conflict", next_action: "Check original source" };
  const params = new URL(reviewChallengeHref(item), "http://localhost").searchParams;
  const profiles = [{ id: profile, ...subject }, { id: session, subject_id: profile, subject_type: "theme", subject_name: "Other subject" }];
  const context = selectReviewContext(profiles, [], params);
  assert.equal(context.subject.subject_id, subject.subject_id);
  assert.equal(context.profile.id, profile);
  assert.match(context.question, /Synthetic dated conflict/);
  assert.match(context.notice, /no longer queued/);
});

test("a missing review never silently selects another subject", () => {
  const context = selectReviewContext([{ id: profile, ...subject }], [], new URLSearchParams({ item_id: session }));
  assert.equal(context.subject, null);
  assert.equal(context.profile, null);
  assert.match(context.notice, /Choose a subject explicitly/);
});

test("a live queue item overrides stale subject and question parameters", () => {
  const item = { id: session, ...subject, item_label: subject.subject_name, why_now_summary: "Current evidence", next_action: "Current next check" };
  const context = selectReviewContext([{ id: profile, ...subject }], [item], new URLSearchParams({ item_id: session, subject_id: profile, subject_type: "theme", q: "Old question" }));
  assert.equal(context.subject.subject_id, subject.subject_id);
  assert.match(context.question, /Current evidence/);
  assert.doesNotMatch(context.question, /Old question/);
  assert.equal(context.notice, null);
});
