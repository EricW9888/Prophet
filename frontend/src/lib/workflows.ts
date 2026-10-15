import type { ReviewQueueItem } from "./api";

export type ResearchSubject = { subject_id: string; subject_type: string; subject_name?: string };
const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export const isRecordId = (value: string | null | undefined): value is string => Boolean(value && uuidPattern.test(value));
const subjectTypes = new Set(["entity", "position", "theme", "portfolio"]);

export function researchHref(subject: ResearchSubject, question = "") {
  const params = new URLSearchParams({ subject_id: subject.subject_id, subject_type: subject.subject_type });
  if (subject.subject_name) params.set("subject_name", subject.subject_name);
  if (question) params.set("q", question);
  return `/chat?${params}`;
}

export function readResearchEntry(params: URLSearchParams) {
  const session = params.get("session_id");
  const id = params.get("subject_id");
  const type = params.get("subject_type");
  const name = params.get("subject_name")?.slice(0, 200);
  const question = params.get("q")?.slice(0, 6000) ?? "";
  const invalid = Boolean((session && !isRecordId(session)) || (!session && (id || type) && !(isRecordId(id) && type && subjectTypes.has(type))));
  return {
    hasEntry: Boolean(session || id || type || question),
    invalid,
    sessionId: isRecordId(session) ? session : null,
    subject: !session && !invalid && id && type ? { subject_id: id, subject_type: type, subject_name: name } as ResearchSubject : null,
    question,
  };
}

export function researchTurnContext(sessionId: string | null, subject: ResearchSubject | null) {
  if (sessionId) return { session_id: sessionId };
  return subject ? { subject_id: subject.subject_id, subject_type: subject.subject_type } : {};
}

export function reviewSubject(item: ReviewQueueItem): ResearchSubject | null {
  return isRecordId(item.subject_id) && item.subject_type && subjectTypes.has(item.subject_type)
    ? { subject_id: item.subject_id, subject_type: item.subject_type, subject_name: item.item_label }
    : null;
}

export function reviewRecordLink(item: ReviewQueueItem): { href: string; label: string } | null {
  if (isRecordId(item.evidence_id)) return { href: `/sources/evidence/${item.evidence_id}`, label: "Inspect original receipt" };
  if (isRecordId(item.profile_id)) return { href: `/profile/${item.profile_id}`, label: "Open subject and evidence" };
  if (isRecordId(item.experiment_id)) return { href: `/shadow#experiment-${item.experiment_id}`, label: "Inspect paper run" };
  if (item.item_type === "reconciliation") return { href: "/positions", label: "Review portfolio reconciliation" };
  return null;
}

export function reviewResearchQuestion(item: ReviewQueueItem) {
  return `Review ${item.item_label}. ${item.why_now_summary || item.trigger_reason}\nNext check: ${item.next_action}\nSeparate original evidence, contrary evidence, and remaining uncertainty.`;
}
