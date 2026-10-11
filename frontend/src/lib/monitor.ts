import type { KnowledgeChangeSummary, TimelineItem } from "./api";

export async function loadMonitorSnapshot(
  fetchTimeline: () => Promise<TimelineItem[]>,
  fetchChanges: () => Promise<KnowledgeChangeSummary>,
) {
  const [timeline, changes] = await Promise.allSettled([fetchTimeline(), fetchChanges()]);
  if (timeline.status === "rejected") throw timeline.reason;
  return {
    items: timeline.value,
    changes: changes.status === "fulfilled" ? changes.value : null,
    warning: changes.status === "rejected" ? "The knowledge change summary is unavailable. The stored evidence feed loaded successfully." : null,
  };
}
