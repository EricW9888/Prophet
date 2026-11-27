import { readableMachineText } from "@/lib/formatting";

/** What each kind of review item is, in the words a person would use. */
const REVIEW_ITEM_KINDS: Record<string, string> = {
  mailbox_identity: "Receipt to match",
  raw_evidence: "New disclosure",
  source_claim_record: "Claim to check",
  position: "Position",
  reconciliation: "Balance to reconcile",
  shadow_experiment: "Simulation",
  theme: "Theme",
  entity: "Company",
  conclusion: "View",
  unresolved_question: "Open question",
};

export function reviewItemKind(itemType: string): string {
  return REVIEW_ITEM_KINDS[itemType] ?? readableMachineText(itemType);
}

/** Tags arrive as lowercase words; show them in sentence case. Tickers stay as they are. */
export function reviewTag(tag: string): string {
  if (/^[A-Z0-9.\-]{1,10}$/.test(tag)) return tag;
  const text = readableMachineText(tag);
  return text.charAt(0).toUpperCase() + text.slice(1);
}

export function reviewPriority(score: number): string {
  return `Priority ${Math.round(score)} of 100`;
}
