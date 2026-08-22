"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";

import AppNav from "@/components/AppNav";
import { apiFetch, TimelineItem, API_BASE, SourceRecord, GraphNodeDetail, GraphConnection, KnowledgeChangeSummary } from "@/lib/api";

const HIDDEN_DETAIL_PROPERTY_KEYS = new Set([
  "portfolio_significance",
  "why_in_graph",
  "linked_holdings",
  "linked_companies",
  "portfolio_mechanism",
  "affected_holdings",
  "next_test",
]);

function formatDateTime(value?: string | null) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString();
}

function formatPropertyValue(key: string, value: unknown) {
  if (Array.isArray(value)) return value.join(", ");
  if (value == null || value === "") return "Not recorded";
  if (key.endsWith("_time") && typeof value === "string") {
    return formatDateTime(value) || value;
  }
  return String(value);
}

function propertyText(value: unknown): string | null {
  if (typeof value === "string") {
    const trimmed = value.trim();
    return trimmed ? trimmed : null;
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return null;
}

function propertyList(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.map((item) => String(item).trim()).filter(Boolean);
}

function NodeRelevancePanel({ node }: { node: GraphNodeDetail }) {
  const significance = propertyText(node.properties?.portfolio_significance);
  const reasoning = propertyText(node.relevance_reasoning) ?? propertyText(node.properties?.why_in_graph);
  const mechanism = propertyText(node.properties?.portfolio_mechanism);
  const affectedHoldings = propertyList(node.properties?.affected_holdings);
  const linkedHoldings = propertyList(node.properties?.linked_holdings);
  const linkedCompanies = propertyList(node.properties?.linked_companies);
  const nextTest = propertyText(node.properties?.next_test);

  if (
    !significance &&
    !reasoning &&
    !mechanism &&
    affectedHoldings.length === 0 &&
    linkedHoldings.length === 0 &&
    linkedCompanies.length === 0 &&
    !nextTest
  ) {
    return null;
  }

  return (
    <section className="space-y-3 rounded-xl border border-violet-200 bg-violet-50/70 p-4 dark:border-violet-900/60 dark:bg-violet-950/25">
      <div className="flex flex-wrap items-center gap-2">
        <div className="h-2 w-2 rounded-full bg-violet-500 dark:bg-violet-400" />
        <h4 className="text-xs font-bold uppercase tracking-wider text-violet-700 dark:text-violet-300">Why this matters</h4>
        {significance ? (
          <span className="rounded-full border border-violet-300 bg-white/70 px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wider text-violet-700 dark:border-violet-700 dark:bg-violet-900/50 dark:text-violet-200">
            {significance}
          </span>
        ) : null}
      </div>

      {reasoning ? (
        <p className="text-sm leading-relaxed text-violet-950 dark:text-violet-100">{reasoning}</p>
      ) : null}

      {mechanism ? (
        <div className="rounded-lg border border-violet-200 bg-white/70 px-3 py-2 dark:border-violet-800/70 dark:bg-violet-950/30">
          <div className="mb-1 text-[11px] font-bold uppercase tracking-wider text-violet-700 dark:text-violet-300/90">
            Portfolio mechanism
          </div>
          <p className="text-sm leading-relaxed text-violet-950 dark:text-violet-100">{mechanism}</p>
        </div>
      ) : null}

      {nextTest ? (
        <div className="rounded-lg border border-violet-200 bg-white/70 px-3 py-2 dark:border-violet-800/70 dark:bg-violet-950/30">
          <div className="mb-1 text-[11px] font-bold uppercase tracking-wider text-violet-700 dark:text-violet-300/90">
            Next useful test
          </div>
          <p className="text-sm leading-relaxed text-violet-950 dark:text-violet-100">{nextTest}</p>
        </div>
      ) : null}

      {affectedHoldings.length > 0 || linkedHoldings.length > 0 || linkedCompanies.length > 0 ? (
        <div className="grid gap-3">
          {affectedHoldings.length > 0 ? (
            <NodePillGroup label="Affected holdings" tone="violet" values={affectedHoldings} />
          ) : null}
          {linkedHoldings.length > 0 ? (
            <NodePillGroup label="Linked holdings" tone="sky" values={linkedHoldings} />
          ) : null}
          {linkedCompanies.length > 0 ? (
            <NodePillGroup label="Linked companies" tone="emerald" values={linkedCompanies} />
          ) : null}
        </div>
      ) : null}
    </section>
  );
}

function NodePillGroup({
  label,
  values,
  tone,
}: {
  label: string;
  values: string[];
  tone: "violet" | "sky" | "emerald";
}) {
  const toneClass =
    tone === "sky"
      ? "border-sky-300 bg-sky-100 text-sky-700 dark:border-sky-400/30 dark:bg-sky-500/10 dark:text-sky-200"
      : tone === "emerald"
        ? "border-emerald-300 bg-emerald-100 text-emerald-700 dark:border-emerald-400/30 dark:bg-emerald-500/10 dark:text-emerald-200"
        : "border-violet-300 bg-white/70 text-violet-700 dark:border-violet-400/30 dark:bg-violet-500/10 dark:text-violet-100";

  return (
    <div>
      <div className="mb-1 text-[11px] font-bold uppercase tracking-wider text-violet-700 dark:text-violet-300/90">
        {label}
      </div>
      <div className="flex flex-wrap gap-2">
        {values.map((value) => (
          <span key={`${label}:${value}`} className={`rounded-full border px-2 py-1 text-[11px] font-semibold tracking-wide ${toneClass}`}>
            {value}
          </span>
        ))}
      </div>
    </div>
  );
}

function KnowledgeCount({
  label,
  active,
  deprecated,
}: {
  label: string;
  active: number;
  deprecated: number;
}) {
  return (
    <div className="rounded-xl border border-gray-200 bg-gray-50 px-3 py-2 dark:border-gray-800 dark:bg-gray-900/50">
      <div className="text-[10px] font-bold uppercase tracking-widest text-gray-400">{label}</div>
      <div className="mt-1 text-lg font-bold tabular-nums text-gray-900 dark:text-gray-100">{active.toLocaleString()}</div>
      <div className="text-[10px] text-gray-400">{deprecated.toLocaleString()} archived</div>
    </div>
  );
}

function getChangeTone(changeType: string) {
  if (
    changeType === "deprecated" ||
    changeType.startsWith("deleted") ||
    changeType.startsWith("obsoleted") ||
    changeType.startsWith("closed") ||
    changeType.startsWith("reset")
  ) {
    return "border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-300";
  }
  if (changeType === "updated") {
    return "border-sky-200 bg-sky-50 text-sky-700 dark:border-sky-900 dark:bg-sky-950/30 dark:text-sky-300";
  }
  return "border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-900 dark:bg-emerald-950/30 dark:text-emerald-300";
}

const OPENABLE_CHANGE_NODE_TYPES = new Set([
  "fact",
  "claim",
  "event",
  "entity",
  "theme",
  "source_item",
  "raw_evidence",
  "source",
  "coverage_map",
  "unresolved_question",
  "conclusion",
  "review_item",
  "lesson",
  "shadow_experiment",
  "experiment_result",
  "position",
]);

function isOpenableChange(change: { node_type: string; change_type: string }) {
  return OPENABLE_CHANGE_NODE_TYPES.has(change.node_type) && !change.change_type.startsWith("deleted");
}

function VisibleNodeProperties({ node }: { node: GraphNodeDetail }) {
  const visibleProperties = Object.entries(node.properties || {}).filter(([key]) => !HIDDEN_DETAIL_PROPERTY_KEYS.has(key));

  if (visibleProperties.length === 0 && node.relevance == null) {
    return null;
  }

  return (
    <section className="space-y-3">
      <h4 className="text-xs font-bold uppercase tracking-wider text-gray-500 dark:text-gray-400">Operating context</h4>
      <div className="grid grid-cols-1 gap-2">
        {node.relevance != null ? (
          <div className="rounded-xl border border-indigo-200 bg-indigo-50 px-3 py-2 dark:border-indigo-800/60 dark:bg-indigo-950/20">
            <div className="text-[11px] uppercase tracking-wider text-indigo-500">Match relevance</div>
            <div className="mt-2 flex items-center gap-2">
              <div className="h-2 flex-1 overflow-hidden rounded-full bg-gray-200 dark:bg-gray-800">
                <div className="h-full bg-indigo-500" style={{ width: `${Math.round((node.relevance || 0) * 100)}%` }} />
              </div>
              <span className="text-xs font-mono font-bold text-indigo-600 dark:text-indigo-400">
                {Math.round((node.relevance || 0) * 100)}%
              </span>
            </div>
          </div>
        ) : null}
        {visibleProperties.map(([key, value]) => (
          <div key={key} className="rounded-xl border border-gray-200 bg-gray-50 px-3 py-2 dark:border-gray-800 dark:bg-gray-900">
            <div className="text-[11px] uppercase tracking-wider text-gray-400">{key.replaceAll("_", " ")}</div>
            <div className="mt-1 text-sm text-gray-700 dark:text-gray-200">
              {formatPropertyValue(key, value)}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

export default function TimelinePage() {
  const [items, setItems] = useState<TimelineItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [urlTitle, setUrlTitle] = useState("");
  const [urlValue, setUrlValue] = useState("");
  const [sources, setSources] = useState<SourceRecord[]>([]);
  const [knowledgeChanges, setKnowledgeChanges] = useState<KnowledgeChangeSummary | null>(null);
  const [selectedSourceId, setSelectedSourceId] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedNode, setSelectedNode] = useState<GraphNodeDetail | null>(null);
  const [selectedNodeKey, setSelectedNodeKey] = useState<string | null>(null);
  const [graphLoading, setGraphLoading] = useState(false);

  async function openNode(nodeType: string, nodeId: string) {
    setGraphLoading(true);
    setSelectedNodeKey(`${nodeType}:${nodeId}`);
    try {
      const detail = await apiFetch<GraphNodeDetail>(`/graph/nodes/${nodeType}/${nodeId}`);
      setSelectedNode(detail);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to load node detail.");
    } finally {
      setGraphLoading(false);
    }
  }

  const loadTimeline = useCallback(async () => {
    if (items.length === 0) {
      setLoading(true);
    }
    try {
      const [timelineItems, changeSummary] = await Promise.all([
        apiFetch<TimelineItem[]>("/timeline?limit=50"),
        apiFetch<KnowledgeChangeSummary>("/timeline/knowledge-changes?limit=12"),
      ]);
      setItems(timelineItems);
      setKnowledgeChanges(changeSummary);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to load timeline.");
    } finally {
      setLoading(false);
    }
  }, [items.length]);

  useEffect(() => {
    void loadTimeline();
    void apiFetch<SourceRecord[]>("/sources/")
      .then((data) => {
        setSources(data);
        if (data[0]) {
          setSelectedSourceId(data[0].id);
        }
      })
      .catch(() => undefined);
    const interval = window.setInterval(() => {
      void loadTimeline();
    }, 15000);
    return () => window.clearInterval(interval);
  }, [loadTimeline]);

  async function handleNoteSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      const response = await fetch(`${API_BASE}/ingestion/notes`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title,
          source_id: selectedSourceId || null,
          source_item_type: "manual_note",
          metadata_json: { content_type: "text/plain" },
          content,
        }),
      });
      if (!response.ok) {
        throw new Error(await response.text());
      }
      setTitle("");
      setContent("");
      await loadTimeline();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to save note.");
    } finally {
      setSaving(false);
    }
  }

  const getTypeColor = (type: string) => {
    switch (type) {
      case "event":
        return "bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-300 border-blue-200 dark:border-blue-800";
      case "fact":
        return "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/30 dark:text-emerald-300 border-emerald-200 dark:border-emerald-800";
      case "claim":
        return "bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-300 border-amber-200 dark:border-amber-800";
      default:
        return "bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-300";
    }
  };

  const getSignalTone = (score: number) => {
    if (score >= 0.85) return "border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-800 dark:bg-emerald-950/30 dark:text-emerald-300";
    if (score >= 0.6) return "border-indigo-200 bg-indigo-50 text-indigo-700 dark:border-indigo-800 dark:bg-indigo-950/30 dark:text-indigo-300";
    return "border-gray-200 bg-gray-50 text-gray-600 dark:border-gray-700 dark:bg-gray-900/40 dark:text-gray-300";
  };

  const describeWhyItMatters = (item: TimelineItem) => {
    const parts = [
      item.importance ? `${item.importance} importance` : null,
      item.novelty ? `${item.novelty} signal` : null,
      item.directness ? `${item.directness} source distance` : null,
      item.contradiction_role && item.contradiction_role !== "neutral"
        ? item.contradiction_role.replaceAll("_", " ")
        : null,
    ].filter(Boolean);
    return parts.join(" · ") || "recent connected knowledge";
  };

  const describeContext = (item: TimelineItem) => {
    const parts = [
      item.subject_name ? `about ${item.subject_name}` : null,
      item.source_name ? `from ${item.source_name}` : null,
    ].filter(Boolean);
    return parts.join(" · ") || null;
  };

  const connectedWeb = selectedNode?.connections.slice(0, 10) ?? [];
  const groupedConnections = connectedWeb.reduce<Record<string, GraphConnection[]>>((groups, connection) => {
    const key = connection.direction;
    groups[key] = groups[key] ? [...groups[key], connection] : [connection];
    return groups;
  }, {});

  const graphNodes = selectedNode
    ? [
        {
          id: `${selectedNode.node_type}:${selectedNode.id}`,
          label: selectedNode.label,
          subtitle: selectedNode.tier ?? selectedNode.node_type,
          x: 230,
          y: 180,
          kind: "center" as const,
          nodeType: selectedNode.node_type,
          nodeId: selectedNode.id,
        },
        ...connectedWeb.map((connection, index) => {
          const angle = (Math.PI * 2 * index) / Math.max(connectedWeb.length, 1) - Math.PI / 2;
          const radius = 135;
          return {
            id: `${connection.node_type}:${connection.node_id}`,
            label: connection.label,
            subtitle: `${connection.relationship_type} · ${connection.node_type}`,
            x: 230 + Math.cos(angle) * radius,
            y: 180 + Math.sin(angle) * radius,
            kind: "connected" as const,
            nodeType: connection.node_type,
            nodeId: connection.node_id,
          };
        }),
      ]
    : [];

  return (
    <div className="min-h-screen bg-gray-50 dark:bg-[#0a0a0a] text-gray-900 dark:text-gray-100 font-sans selection:bg-indigo-500/30">
      <AppNav active="timeline" />

      <main className="mx-auto grid w-full max-w-[1440px] grid-cols-1 gap-8 px-4 py-8 sm:px-6 lg:px-8 xl:grid-cols-[1.15fr_0.85fr]">
        <section className="space-y-8">
          <header className="flex flex-col gap-2">
            <div className="flex items-center gap-3">
              <h1 className="text-3xl font-bold tracking-tight">Research Feed</h1>
            </div>
            <p className="text-gray-500 dark:text-gray-400">
              Facts, claims, and events ranked by how useful they look right now, not just by when they were created. Open any item to inspect the connected graph behind it.
            </p>
          </header>

          <section className="relative border-l border-gray-200 dark:border-gray-800 ml-3 md:ml-4 space-y-8 py-4">
            {loading ? (
              <div className="pl-6 text-sm text-gray-500 animate-pulse">Loading intelligence stream...</div>
            ) : error ? (
              <div className="pl-6 text-sm text-red-500">{error}</div>
            ) : items.length === 0 ? (
              <div className="pl-6 text-sm text-gray-500">No evidence processed yet.</div>
            ) : (
              items.map((item) => (
                <div key={item.id} className="relative pl-8 md:pl-10 group">
                  <div className="absolute -left-[5px] top-1.5 h-2.5 w-2.5 rounded-full ring-4 ring-gray-50 dark:ring-[#0a0a0a] bg-indigo-500 group-hover:bg-indigo-400 transition-colors" />
                  <div className="flex flex-col gap-2">
                    <div className="flex flex-wrap items-center gap-3">
                      <span className={`px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wider rounded-md border ${getTypeColor(item.item_type)}`}>
                        {item.item_type}
                      </span>
                      <time className="text-xs text-gray-400 font-medium font-mono">
                        {item.display_time_label} {formatDateTime(item.display_time || item.created_at)}
                      </time>
                      {item.display_time !== item.created_at ? (
                        <span className="text-xs text-gray-400">
                          recorded {formatDateTime(item.created_at)}
                        </span>
                      ) : null}
                      {describeContext(item) ? (
                        <span className="text-xs text-gray-400">{describeContext(item)}</span>
                      ) : null}
                    </div>
                    <button
                      type="button"
                      onClick={() => void openNode(item.item_type, item.id)}
                      className={`w-full rounded-xl border bg-white p-5 text-left shadow-sm transition-all hover:shadow-md dark:border-gray-800 dark:bg-gray-950 ${
                        selectedNodeKey === `${item.item_type}:${item.id}` ? "border-indigo-400 dark:border-indigo-600" : "border-gray-200"
                      }`}
                    >
                      <div className="mb-3 flex flex-wrap items-center gap-2">
                        <span className={`rounded-full border px-2.5 py-1 text-[11px] font-semibold uppercase tracking-wider ${getSignalTone(item.signal_score)}`}>
                          signal {item.signal_score.toFixed(2)}
                        </span>
                        <span className="text-xs text-gray-500 dark:text-gray-400">
                          {describeWhyItMatters(item)}
                        </span>
                        {item.subject_name ? (
                          <span className="text-xs font-medium text-indigo-600 dark:text-indigo-400">
                            {item.subject_name}
                          </span>
                        ) : null}
                      </div>
                      <p className="text-gray-800 dark:text-gray-200 font-medium leading-relaxed">{item.text}</p>
                      {describeContext(item) ? (
                        <div className="mt-2 text-sm text-gray-500 dark:text-gray-400">
                          Context: {describeContext(item)}
                        </div>
                      ) : null}
                      <div className="mt-4 pt-3 border-t border-gray-100 dark:border-gray-800/60 flex items-center justify-between">
                        <span className="text-xs font-mono text-gray-400 truncate max-w-[200px]">Node ID: {item.id.split("-")[0]}</span>
                        <span className="text-xs text-gray-400">{item.tier} · Open graph</span>
                      </div>
                    </button>
                  </div>
                </div>
              ))
            )}
          </section>
        </section>

        <aside className="space-y-6 xl:sticky xl:top-24 self-start xl:max-h-[calc(100vh-7rem)] xl:overflow-y-auto xl:pr-1">
          <section className="rounded-2xl border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-800 dark:bg-gray-950">
            <div className="flex items-start justify-between gap-4">
              <div>
                <h2 className="text-sm font-bold uppercase tracking-wider text-gray-500 dark:text-gray-400">Knowledge Changes</h2>
                <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">Audit events when available; older rows are marked as derived.</p>
              </div>
              {knowledgeChanges ? (
                <span className="rounded-full border border-gray-200 px-2.5 py-1 text-[10px] font-bold uppercase tracking-widest text-gray-500 dark:border-gray-800 dark:text-gray-400">
                  {knowledgeChanges.changes.length} latest
                </span>
              ) : null}
            </div>
            {knowledgeChanges ? (
              <div className="mt-4 space-y-4">
                <div className="grid grid-cols-3 gap-2">
                  <KnowledgeCount label="Facts" active={knowledgeChanges.active_facts} deprecated={knowledgeChanges.deprecated_facts} />
                  <KnowledgeCount label="Claims" active={knowledgeChanges.active_claims} deprecated={knowledgeChanges.deprecated_claims} />
                  <KnowledgeCount label="Events" active={knowledgeChanges.active_events} deprecated={knowledgeChanges.deprecated_events} />
                </div>
                <div className="space-y-2">
                  {knowledgeChanges.changes.slice(0, 6).map((change) => {
                    const openable = isOpenableChange(change);
                    return (
                    <button
                      key={`${change.change_id ?? "derived"}:${change.node_type}:${change.id}:${change.change_type}`}
                      type="button"
                      onClick={() => {
                        if (openable) void openNode(change.node_type, change.id);
                      }}
                      disabled={!openable}
                      className={`w-full rounded-xl border border-gray-200 bg-gray-50 px-3 py-2 text-left transition-colors dark:border-gray-800 dark:bg-gray-900/50 ${
                        openable
                          ? "hover:border-indigo-300 hover:bg-indigo-50 dark:hover:border-indigo-800 dark:hover:bg-indigo-950/20"
                          : "cursor-default"
                      }`}
                    >
                      <div className="mb-1 flex flex-wrap items-center gap-2">
                        <span className={`rounded-full border px-2 py-0.5 text-[10px] font-bold uppercase tracking-widest ${getChangeTone(change.change_type)}`}>
                          {change.change_type}
                        </span>
                        <span className="text-[10px] font-bold uppercase tracking-widest text-gray-400">{change.node_type}</span>
                        <span className="rounded-full border border-gray-200 px-2 py-0.5 text-[10px] font-bold uppercase tracking-widest text-gray-400 dark:border-gray-700">
                          {change.change_source === "audit_event" ? "audit" : "derived"}
                        </span>
                        <span className="ml-auto text-[10px] text-gray-400">{formatDateTime(change.changed_at)}</span>
                      </div>
                      <p className="line-clamp-2 text-xs leading-relaxed text-gray-700 dark:text-gray-200">{change.text}</p>
                      {change.reason ? (
                        <p className="mt-1 line-clamp-1 text-[11px] text-gray-500 dark:text-gray-400">{change.reason}</p>
                      ) : null}
                      {change.deprecated_reason ? (
                        <p className="mt-1 line-clamp-1 text-[11px] text-amber-700 dark:text-amber-300">{change.deprecated_reason}</p>
                      ) : null}
                      {change.actor ? (
                        <p className="mt-1 text-[10px] uppercase tracking-wider text-gray-400">{change.actor.replaceAll("_", " ")}</p>
                      ) : null}
                    </button>
                  );
                  })}
                </div>
              </div>
            ) : (
              <div className="mt-4 rounded-xl border border-dashed border-gray-300 p-4 text-sm text-gray-500 dark:border-gray-700 dark:text-gray-400">
                Loading knowledge ledger...
              </div>
            )}
          </section>

          <section className="rounded-2xl border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-800 dark:bg-gray-950">
            <div className="flex items-center justify-between gap-3">
              <div>
                <h2 className="text-sm font-bold uppercase tracking-wider text-gray-500 dark:text-gray-400">Information Node</h2>
                <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">Inspect how one fact, claim, event, or subject links to the rest of the system.</p>
              </div>
              {selectedNode ? (
                <button
                  type="button"
                  onClick={() => {
                    setSelectedNode(null);
                    setSelectedNodeKey(null);
                  }}
                  className="rounded-full border border-gray-300 px-3 py-1 text-xs text-gray-500 hover:border-gray-400 hover:text-gray-700 dark:border-gray-700 dark:text-gray-400 dark:hover:border-gray-500 dark:hover:text-gray-200"
                >
                  Clear
                </button>
              ) : null}
            </div>

            {!selectedNode && !graphLoading ? (
              <div className="mt-5 rounded-xl border border-dashed border-gray-300 p-5 text-sm text-gray-500 dark:border-gray-700 dark:text-gray-400">
                Select a node from the feed to open its connected view.
              </div>
            ) : null}

            {graphLoading ? (
              <div className="mt-5 rounded-xl border border-gray-200 p-5 text-sm text-gray-500 animate-pulse dark:border-gray-800 dark:text-gray-400">
                Loading connected node detail...
              </div>
            ) : null}

            {selectedNode ? (
              <div className="mt-5 space-y-5">
                <div className="rounded-xl border border-indigo-200 bg-indigo-50/60 p-4 dark:border-indigo-900 dark:bg-indigo-950/30">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="rounded-md border border-indigo-300 px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wider text-indigo-700 dark:border-indigo-700 dark:text-indigo-300">
                      {selectedNode.node_type}
                    </span>
                    {selectedNode.tier ? (
                      <span className="text-xs text-indigo-600 dark:text-indigo-400">{selectedNode.tier}</span>
                    ) : null}
                  </div>
                  <h3 className="mt-3 text-lg font-semibold text-gray-900 dark:text-gray-100">{selectedNode.label}</h3>
                  {selectedNode.body ? (
                    <p className="mt-2 text-sm leading-relaxed text-gray-600 dark:text-gray-300">{selectedNode.body}</p>
                  ) : null}
                  {selectedNode.created_at ? (
                    <p className="mt-3 text-xs font-mono text-gray-400">recorded {formatDateTime(selectedNode.created_at)}</p>
                  ) : null}
                </div>

                <NodeRelevancePanel node={selectedNode} />
                <VisibleNodeProperties node={selectedNode} />

                <section className="space-y-3">
                  <div className="flex items-center justify-between gap-3">
                    <h4 className="text-xs font-bold uppercase tracking-wider text-gray-500 dark:text-gray-400">Connected web</h4>
                    <span className="text-xs text-gray-400">{connectedWeb.length} visible links</span>
                  </div>
                  <div className="rounded-xl border border-gray-200 p-4 dark:border-gray-800">
                    {connectedWeb.length === 0 ? (
                      <p className="text-sm text-gray-500 dark:text-gray-400">No connected nodes have been recorded for this node yet.</p>
                    ) : (
                      <div className="space-y-4">
                        <div className="overflow-hidden rounded-xl border border-gray-200 bg-gray-50 dark:border-gray-800 dark:bg-gray-900/50">
                          <svg viewBox="0 0 460 360" className="h-[360px] w-full">
                            {graphNodes.slice(1).map((node, index) => (
                              <g key={`edge:${node.id}`}>
                                <line
                                  x1={230}
                                  y1={180}
                                  x2={node.x}
                                  y2={node.y}
                                  stroke="currentColor"
                                  className="text-indigo-300 dark:text-indigo-700"
                                  strokeWidth="1.5"
                                  strokeDasharray={index % 2 === 0 ? "0" : "4 4"}
                                />
                              </g>
                            ))}
                            {graphNodes.map((node) => (
                              <g key={node.id}>
                                <circle
                                  cx={node.x}
                                  cy={node.y}
                                  r={node.kind === "center" ? 34 : 22}
                                  className={node.kind === "center" ? "fill-indigo-500/90" : "fill-white dark:fill-gray-950"}
                                  stroke="currentColor"
                                  strokeWidth={node.kind === "center" ? 2 : 1.5}
                                />
                                <foreignObject
                                  x={node.x - (node.kind === "center" ? 90 : 70)}
                                  y={node.y + (node.kind === "center" ? 42 : 28)}
                                  width={node.kind === "center" ? 180 : 140}
                                  height={60}
                                >
                                  <button
                                    type="button"
                                    onClick={() => void openNode(node.nodeType, node.nodeId)}
                                    className="w-full rounded-lg bg-white/90 px-2 py-1 text-center text-[11px] text-gray-700 shadow-sm hover:text-indigo-700 dark:bg-gray-950/90 dark:text-gray-200 dark:hover:text-indigo-300"
                                  >
                                    <div className="truncate font-semibold">{node.label}</div>
                                    <div className="truncate text-gray-400">{node.subtitle}</div>
                                  </button>
                                </foreignObject>
                              </g>
                            ))}
                          </svg>
                        </div>
                        {(["incoming", "outgoing"] as const).map((direction) =>
                          groupedConnections[direction]?.length ? (
                            <div key={direction}>
                              <div className="mb-2 text-[11px] uppercase tracking-wider text-gray-400">{direction}</div>
                              <div className="flex flex-wrap gap-2">
                                {groupedConnections[direction].map((connection) => (
                                  <button
                                    key={`${direction}:${connection.edge_id}`}
                                    type="button"
                                    onClick={() => void openNode(connection.node_type, connection.node_id)}
                                    className="rounded-full border border-gray-300 bg-white px-3 py-2 text-left text-xs hover:border-indigo-400 hover:text-indigo-700 dark:border-gray-700 dark:bg-gray-950 dark:hover:border-indigo-600 dark:hover:text-indigo-300"
                                  >
                                    <span className="font-semibold">{connection.label}</span>
                                    <span className="mx-1 text-gray-400">·</span>
                                    <span className="text-gray-500">{connection.relationship_type}</span>
                                  </button>
                                ))}
                              </div>
                            </div>
                          ) : null,
                        )}
                      </div>
                    )}
                  </div>
                </section>

                <section className="space-y-3">
                  <h4 className="text-xs font-bold uppercase tracking-wider text-gray-500 dark:text-gray-400">Source citations</h4>
                  {selectedNode.citations.length === 0 ? (
                    <div className="rounded-xl border border-gray-200 p-4 text-sm text-gray-500 dark:border-gray-800 dark:text-gray-400">
                      No source citation is attached to this node yet.
                    </div>
                  ) : (
                    <div className="space-y-3">
                      {selectedNode.citations.map((citation) => (
                        <div key={citation.raw_evidence_id} className="rounded-xl border border-gray-200 p-4 dark:border-gray-800">
                          <div className="flex items-center justify-between gap-3">
                            <div>
                              <div className="text-sm font-semibold text-gray-900 dark:text-gray-100">{citation.source_name}</div>
                              <div className="text-xs text-gray-400">{citation.source_type}</div>
                            </div>
                            <div className="text-xs font-mono text-gray-400">{formatDateTime(citation.created_at)}</div>
                          </div>
                          {citation.title ? (
                            <p className="mt-3 text-sm text-gray-700 dark:text-gray-200">{citation.title}</p>
                          ) : null}
                          <div className="mt-3 flex flex-wrap gap-3 text-xs text-gray-500 dark:text-gray-400">
                            {citation.author ? <span>Author: {citation.author}</span> : null}
                            {citation.url ? (
                              <a href={citation.url} target="_blank" rel="noreferrer" className="text-indigo-600 hover:text-indigo-500 dark:text-indigo-400">
                                Open source
                              </a>
                            ) : null}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </section>
              </div>
            ) : null}
          </section>

          <section className="rounded-2xl border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-800 dark:bg-gray-950">
            <h2 className="text-sm font-bold uppercase tracking-wider text-gray-500 dark:text-gray-400 mb-4">Add Research Note</h2>
            <form onSubmit={handleNoteSubmit} className="space-y-4">
              <select value={selectedSourceId} onChange={(e) => setSelectedSourceId(e.target.value)} className="w-full px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900">
                <option value="">Manual Research Inbox</option>
                {sources.map((source) => (
                  <option key={source.id} value={source.id}>{source.name} ({source.source_type})</option>
                ))}
              </select>
              <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Title" className="w-full px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900" />
              <textarea value={content} onChange={(e) => setContent(e.target.value)} rows={10} placeholder="Paste notes, article text, or transcript excerpts here." className="w-full px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900" />
              <button disabled={saving || !content.trim()} className="w-full rounded-lg bg-indigo-600 px-4 py-2 text-white disabled:opacity-50">
                {saving ? "Processing..." : "Store and Extract"}
              </button>
            </form>
          </section>

          <section className="rounded-2xl border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-800 dark:bg-gray-950">
            <h2 className="text-sm font-bold uppercase tracking-wider text-gray-500 dark:text-gray-400 mb-4">Ingest URL</h2>
            <form
              onSubmit={async (e) => {
                e.preventDefault();
                setSaving(true);
                setError(null);
                try {
                  const response = await fetch(`${API_BASE}/ingestion/url`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                      url: urlValue,
                      title: urlTitle || null,
                      source_id: selectedSourceId || null,
                      source_item_type: "web_research",
                    }),
                  });
                  if (!response.ok) {
                    throw new Error(await response.text());
                  }
                  setUrlTitle("");
                  setUrlValue("");
                  await loadTimeline();
                } catch (err) {
                  setError(err instanceof Error ? err.message : "Unable to ingest URL.");
                } finally {
                  setSaving(false);
                }
              }}
              className="space-y-4"
            >
              <input value={urlTitle} onChange={(e) => setUrlTitle(e.target.value)} placeholder="Optional title override" className="w-full px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900" />
              <input value={urlValue} onChange={(e) => setUrlValue(e.target.value)} placeholder="https://example.com/article" className="w-full px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900" />
              <button disabled={saving || !urlValue.trim()} className="w-full rounded-lg border border-indigo-300 bg-indigo-50 px-4 py-2 text-indigo-700 dark:border-indigo-800 dark:bg-indigo-950/40 dark:text-indigo-300 disabled:opacity-50">
                {saving ? "Fetching..." : "Fetch, Store, and Extract"}
              </button>
            </form>
          </section>

          <section className="rounded-2xl border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-800 dark:bg-gray-950">
            <h2 className="text-sm font-bold uppercase tracking-wider text-gray-500 dark:text-gray-400 mb-3">Research Discipline</h2>
            <p className="text-sm text-gray-500 dark:text-gray-400">Every note is persisted as raw evidence first, then parsed into claims, facts, and events with provenance preserved.</p>
            <Link href="/sources" className="mt-3 inline-block text-sm text-indigo-600 dark:text-indigo-400">Manage trusted sources</Link>
          </section>
        </aside>
      </main>
    </div>
  );
}
