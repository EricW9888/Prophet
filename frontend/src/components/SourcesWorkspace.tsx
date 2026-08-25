"use client";

import { useEffect, useMemo, useState } from "react";
import { BookmarkPlus, CheckCircle2, ExternalLink, FileText, RotateCcw, ThumbsDown, ThumbsUp, Trash2, Video } from "lucide-react";

import {
  API_BASE,
  apiFetch,
  MediaIngestionCapabilityResponse,
  MediaIngestionJob,
  OwnershipDisclosureCreate,
  SourceEvidenceDetail,
  SourceEvidenceSummary,
  SourceFeedbackRecord,
  SourceRecord,
  SubjectAliasRecord,
} from "@/lib/api";
import { normalizeSourceOrigin } from "@/lib/sourceOrigin";
import HintMarker from "@/components/HintMarker";

type NoteForm = {
  title: string;
  content: string;
  sourceId: string;
  noteType: NoteType;
  url: string;
};

type NoteType = "user_note" | "research_note" | "manual_transcript" | "video_notes" | "cagr_test";
type WorkspaceTab = "review" | "capture" | "feedback" | "diagnostics";

const noteTypeOptions: Array<{ value: NoteType; label: string }> = [
  { value: "user_note", label: "Research note" },
  { value: "manual_transcript", label: "Manual transcript" },
  { value: "video_notes", label: "Video notes" },
  { value: "cagr_test", label: "CAGR test" },
  { value: "research_note", label: "Research memo" },
];

type DisclosureForm = {
  sourceName: string;
  sourceType: "filing" | "ownership_tracker";
  sourceItemType:
    | "insider_disclosure"
    | "ownership_disclosure"
    | "institutional_flow"
    | "congressional_trade_disclosure";
  ticker: string;
  issuer: string;
  actorName: string;
  actorType: string;
  transactionType: string;
  transactionValue: string;
  transactionDate: string;
  disclosureDate: string;
  url: string;
  summary: string;
};

const emptyDisclosureForm: DisclosureForm = {
  sourceName: "",
  sourceType: "ownership_tracker",
  sourceItemType: "ownership_disclosure",
  ticker: "",
  issuer: "",
  actorName: "",
  actorType: "",
  transactionType: "",
  transactionValue: "",
  transactionDate: "",
  disclosureDate: "",
  url: "",
  summary: "",
};

export default function SourcesWorkspace() {
  const [sources, setSources] = useState<SourceRecord[]>([]);
  const [recentEvidence, setRecentEvidence] = useState<SourceEvidenceSummary[]>([]);
  const [feedback, setFeedback] = useState<SourceFeedbackRecord[]>([]);
  const [notes, setNotes] = useState<SourceEvidenceSummary[]>([]);
  const [aliases, setAliases] = useState<SubjectAliasRecord[]>([]);
  const [mediaCapabilities, setMediaCapabilities] = useState<MediaIngestionCapabilityResponse | null>(null);
  const [mediaUrl, setMediaUrl] = useState("");
  const [mediaTitle, setMediaTitle] = useState("");
  const [mediaJob, setMediaJob] = useState<MediaIngestionJob | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [form, setForm] = useState({
    name: "",
    source_type: "analyst",
    url: "",
    description: "",
    is_trusted: true,
  });
  const [noteForm, setNoteForm] = useState<NoteForm>({
    title: "",
    content: "",
    sourceId: "",
    noteType: "user_note",
    url: "",
  });
  const [disclosureForm, setDisclosureForm] = useState<DisclosureForm>(emptyDisclosureForm);
  const [feedbackNotes, setFeedbackNotes] = useState<Record<string, string>>({});
  const [activeWorkspace, setActiveWorkspace] = useState<WorkspaceTab>("review");

  const trustedSources = useMemo(() => sources.filter((source) => source.is_trusted), [sources]);
  const discoveredSources = useMemo(() => sources.filter((source) => !source.is_trusted), [sources]);
  const usefulFlags = feedback.filter((item) => item.rating === "useful");
  const noisyFlags = feedback.filter((item) => item.rating === "not_useful");

  async function loadWorkspace(showSpinner = true) {
    if (showSpinner) setLoading(true);
    try {
      const [sourceData, evidenceData, feedbackData, noteData, aliasData, mediaCapabilityData] = await Promise.all([
        apiFetch<SourceRecord[]>("/sources/"),
        apiFetch<SourceEvidenceSummary[]>("/sources/recent-evidence?limit=80"),
        apiFetch<SourceFeedbackRecord[]>("/sources/feedback?limit=80"),
        apiFetch<SourceEvidenceSummary[]>("/sources/notes?limit=30"),
        apiFetch<SubjectAliasRecord[]>("/graph/aliases?limit=80"),
        apiFetch<MediaIngestionCapabilityResponse>("/sources/youtube/capabilities"),
      ]);
      setSources(sourceData);
      setRecentEvidence(evidenceData);
      setFeedback(feedbackData);
      setNotes(noteData);
      setAliases(aliasData);
      setMediaCapabilities(mediaCapabilityData);
      setNoteForm((current) => {
        if (current.sourceId || sourceData.length === 0) return current;
        const preferred = sourceData.find((source) => source.is_trusted) ?? sourceData[0];
        return { ...current, sourceId: preferred.id };
      });
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to load source workspace.");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void loadWorkspace();
    const interval = window.setInterval(() => {
      void loadWorkspace(false);
    }, 20000);
    return () => window.clearInterval(interval);
  }, []);

  useEffect(() => {
    if (!mediaJob || !["queued", "running"].includes(mediaJob.status)) return;
    const interval = window.setInterval(() => {
      void apiFetch<MediaIngestionJob>(`/sources/youtube/ingest-jobs/${mediaJob.job_id}`)
        .then((job) => {
          setMediaJob(job);
          if (job.status === "completed") {
            if (job.result?.ok) {
              setMediaUrl("");
              setMediaTitle("");
              setError(null);
              void loadWorkspace(false);
            } else {
              setError(job.result?.error || "YouTube ingestion did not produce evidence.");
            }
          } else if (job.status === "error") {
            setError(job.error || "YouTube ingestion failed.");
          }
        })
        .catch((err) => {
          setError(err instanceof Error ? err.message : "Unable to refresh video ingestion status.");
        });
    }, 1200);
    return () => window.clearInterval(interval);
  }, [mediaJob]);

  async function createSource(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      const response = await fetch(`${API_BASE}/sources/`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...form,
          url: form.url || null,
          description: form.description || null,
        }),
      });
      if (!response.ok) throw new Error(await response.text());
      setForm({
        name: "",
        source_type: "analyst",
        url: "",
        description: "",
        is_trusted: true,
      });
      await loadWorkspace(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to create source.");
    } finally {
      setSaving(false);
    }
  }

  async function startMediaIngestion(e: React.FormEvent) {
    e.preventDefault();
    if (!mediaUrl.trim() || (mediaJob && ["queued", "running"].includes(mediaJob.status))) return;
    setError(null);
    try {
      const job = await apiFetch<MediaIngestionJob>("/sources/youtube/ingest-jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          url: mediaUrl.trim(),
          title: mediaTitle.trim() || null,
        }),
      });
      setMediaJob(job);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to start YouTube ingestion.");
    }
  }

  async function cancelMediaIngestion() {
    if (!mediaJob || !["queued", "running"].includes(mediaJob.status)) return;
    try {
      await apiFetch(`/sources/youtube/ingest-jobs/${mediaJob.job_id}/cancel`, { method: "POST" });
      setMediaJob((current) => (current ? { ...current, status: "cancelled" } : current));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to cancel YouTube ingestion.");
    }
  }

  async function toggleTrusted(source: SourceRecord) {
    try {
      const response = await fetch(`${API_BASE}/sources/${source.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ is_trusted: !source.is_trusted }),
      });
      if (!response.ok) throw new Error(await response.text());
      await loadWorkspace(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to update source.");
    }
  }

  async function flagEvidence(evidenceId: string, rating: "useful" | "not_useful") {
    try {
      const response = await fetch(`${API_BASE}/sources/feedback`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          evidence_id: evidenceId,
          rating,
          note: feedbackNotes[evidenceId]?.trim() || null,
          context: "source_workspace",
        }),
      });
      if (!response.ok) throw new Error(await response.text());
      setFeedbackNotes((current) => ({ ...current, [evidenceId]: "" }));
      await loadWorkspace(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to flag evidence.");
    }
  }

  async function clearFeedback(evidenceId: string) {
    try {
      const response = await fetch(`${API_BASE}/sources/feedback/${evidenceId}`, { method: "DELETE" });
      if (!response.ok) throw new Error(await response.text());
      await loadWorkspace(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to clear feedback.");
    }
  }

  async function approveAlias(aliasId: string) {
    try {
      const response = await fetch(`${API_BASE}/graph/aliases/${aliasId}/approve`, { method: "POST" });
      if (!response.ok) throw new Error(await response.text());
      await loadWorkspace(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to approve alias.");
    }
  }

  async function removeAlias(aliasId: string) {
    if (!window.confirm("Remove this subject alias?")) return;
    try {
      const response = await fetch(`${API_BASE}/graph/aliases/${aliasId}`, { method: "DELETE" });
      if (!response.ok) throw new Error(await response.text());
      await loadWorkspace(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to remove alias.");
    }
  }

  async function createNote(e: React.FormEvent) {
    e.preventDefault();
    if (!noteForm.content.trim()) return;
    setSaving(true);
    setError(null);
    const isVideoNote = noteForm.noteType === "manual_transcript" || noteForm.noteType === "video_notes";
    const noteUrl = noteForm.url.trim();
    try {
      const response = await fetch(`${API_BASE}/ingestion/notes`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: noteForm.title.trim() || noteForm.content.trim().slice(0, 80),
          source_id: noteForm.sourceId || null,
          source_item_type: noteForm.noteType,
          url: noteUrl || null,
          metadata_json: {
            ...compactRecord({
              content_type: "text/plain",
              note_type: noteForm.noteType,
              origin: "source_workspace",
              media_source_type: isVideoNote ? "youtube" : null,
              video_url: isVideoNote ? noteUrl : null,
              ingest_mode:
                noteForm.noteType === "manual_transcript"
                  ? "manual_transcript"
                  : noteForm.noteType === "video_notes"
                  ? "manual_video_notes"
                  : null,
            }),
          },
          content: noteForm.content,
        }),
      });
      if (!response.ok) throw new Error(await response.text());
      setNoteForm((current) => ({ ...current, title: "", content: "", noteType: "user_note", url: "" }));
      await loadWorkspace(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to save note.");
    } finally {
      setSaving(false);
    }
  }

  async function createDisclosure(e: React.FormEvent) {
    e.preventDefault();
    if (!disclosureForm.sourceName.trim()) return;
    setSaving(true);
    setError(null);
    const transactionTime = toIsoOrNull(disclosureForm.transactionDate);
    const disclosureTime = toIsoOrNull(disclosureForm.disclosureDate);
    const metadata = compactRecord({
      ticker: disclosureForm.ticker.trim().toUpperCase(),
      issuer: disclosureForm.issuer.trim(),
      actor_name: disclosureForm.actorName.trim(),
      actor_type: disclosureForm.actorType.trim(),
      transaction_type: disclosureForm.transactionType.trim(),
      transaction_value: disclosureForm.transactionValue.trim(),
      transaction_date: transactionTime,
      disclosure_date: disclosureTime,
      source_url: disclosureForm.url.trim(),
      origin: "source_workspace",
    });
    const payload: OwnershipDisclosureCreate = {
      source_name: disclosureForm.sourceName.trim(),
      source_type: disclosureForm.sourceType,
      source_item_type: disclosureForm.sourceItemType,
      url: disclosureForm.url.trim() || null,
      metadata,
      summary: disclosureForm.summary.trim() || null,
      event_time: transactionTime,
      public_time: disclosureTime,
      eligible_action_time: disclosureTime,
    };
    try {
      await apiFetch<SourceEvidenceDetail>("/sources/ownership-disclosures", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      setDisclosureForm(emptyDisclosureForm);
      await loadWorkspace(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to save disclosure.");
    } finally {
      setSaving(false);
    }
  }

  function prefillCagrNote() {
    setNoteForm((current) => ({
      ...current,
      title: "25.9% CAGR test",
      content: current.content || "25.9% CAGR test",
      noteType: "cagr_test",
    }));
  }

  function prefillManualTranscript() {
    const youtubeSource =
      sources.find((source) => source.source_type === "youtube" && source.is_trusted) ??
      sources.find((source) => source.source_type === "youtube") ??
      sources.find((source) => source.is_trusted) ??
      sources[0];
    setNoteForm((current) => ({
      ...current,
      title: current.title || "Manual YouTube transcript",
      noteType: "manual_transcript",
      sourceId: youtubeSource?.id ?? current.sourceId,
    }));
  }

  return (
    <main className="mx-auto w-full max-w-[1440px] px-4 py-8 sm:px-6 lg:px-8">
      <div className="mb-6 flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
        <div>
          <div className="text-[11px] font-bold uppercase tracking-[0.2em] text-indigo-500">Source memory</div>
          <h1 className="mt-2 text-3xl font-bold tracking-tight">Sources, Notes, and Feedback</h1>
          <p className="mt-2 max-w-3xl text-sm text-gray-500 dark:text-gray-400">
            One workspace for sources Prophet trusts, sources it has only discovered, user notes, and the evidence you marked useful or noisy.
          </p>
        </div>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
          <SummaryCard label="Trusted" value={trustedSources.length} />
          <SummaryCard label="Discovered" value={discoveredSources.length} />
          <SummaryCard label="Useful" value={usefulFlags.length} />
          <SummaryCard label="Noisy" value={noisyFlags.length} />
          <SummaryCard label="Aliases" value={aliases.length} />
        </div>
      </div>

      {error ? (
        <div className="mb-4 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 dark:border-red-900/50 dark:bg-red-950/20 dark:text-red-300">
          {error}
        </div>
      ) : null}

      <div className="mb-6 flex flex-wrap gap-2 rounded-lg border border-gray-200 bg-white p-1 dark:border-gray-800 dark:bg-gray-950">
        <WorkspaceTabButton
          active={activeWorkspace === "review"}
          label="Source Review"
          count={sources.length}
          onClick={() => setActiveWorkspace("review")}
        />
        <WorkspaceTabButton
          active={activeWorkspace === "capture"}
          label="Add Evidence"
          count={null}
          onClick={() => setActiveWorkspace("capture")}
        />
        <WorkspaceTabButton
          active={activeWorkspace === "feedback"}
          label="Feedback"
          count={feedback.length + notes.length}
          onClick={() => setActiveWorkspace("feedback")}
        />
        <WorkspaceTabButton
          active={activeWorkspace === "diagnostics"}
          label="Diagnostics"
          count={aliases.length}
          onClick={() => setActiveWorkspace("diagnostics")}
        />
      </div>

      {loading ? (
        <div className="rounded-lg border border-gray-200 bg-white p-6 text-sm text-gray-500 dark:border-gray-800 dark:bg-gray-950">
          Loading source workspace...
        </div>
      ) : activeWorkspace === "review" ? (
        <section className="space-y-6">
          <SourceSection
            title="Your Trusted Sources"
            subtitle="Sources you explicitly trust. Prophet should lean on these first, while still checking them against evidence."
            sources={trustedSources}
            empty="No trusted sources yet."
            onToggleTrusted={toggleTrusted}
          />
          <SourceSection
            title="LLM / Agent Discovered Sources"
            subtitle="Sources found through web research, email, ingestion, or extraction. Promote the useful ones; demote noisy ones."
            sources={discoveredSources}
            empty="No discovered sources yet."
            onToggleTrusted={toggleTrusted}
          />
        </section>
      ) : activeWorkspace === "capture" ? (
        <div className="grid gap-6 xl:grid-cols-2">
          <div className="space-y-4">
            <AddSourcePanel
              form={form}
              setForm={setForm}
              saving={saving}
              onSubmit={createSource}
            />
            <MediaCapabilityPanel
              capabilities={mediaCapabilities}
              mediaUrl={mediaUrl}
              mediaTitle={mediaTitle}
              mediaJob={mediaJob}
              onMediaUrlChange={setMediaUrl}
              onMediaTitleChange={setMediaTitle}
              onSubmit={startMediaIngestion}
              onCancel={() => void cancelMediaIngestion()}
              onManualTranscript={prefillManualTranscript}
            />
          </div>
          <div className="space-y-4">
            <AddDisclosurePanel
              form={disclosureForm}
              setForm={setDisclosureForm}
              saving={saving}
              onSubmit={createDisclosure}
            />
            <AddNotePanel
              sources={sources}
              form={noteForm}
              setForm={setNoteForm}
              saving={saving}
              onSubmit={createNote}
              onCagrTest={prefillCagrNote}
            />
          </div>
        </div>
      ) : activeWorkspace === "feedback" ? (
        <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_420px]">
          <RecentEvidence
            evidence={recentEvidence}
            feedbackNotes={feedbackNotes}
            setFeedbackNotes={setFeedbackNotes}
            onFlag={flagEvidence}
          />
          <aside className="space-y-4 xl:sticky xl:top-24 xl:self-start">
            <FlaggedPanel feedback={feedback} onClear={clearFeedback} />
            <NotesPanel notes={notes} />
          </aside>
        </div>
      ) : (
        <section className="space-y-6">
          <SubjectAliasPanel aliases={aliases} onApprove={approveAlias} onRemove={removeAlias} />
        </section>
      )}
    </main>
  );
}

function WorkspaceTabButton({
  active,
  label,
  count,
  onClick,
}: {
  active: boolean;
  label: string;
  count: number | null;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={[
        "inline-flex items-center gap-2 rounded-md px-3 py-2 text-sm font-semibold transition-colors",
        active
          ? "bg-gray-900 text-white shadow-sm dark:bg-gray-100 dark:text-gray-950"
          : "text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-900",
      ].join(" ")}
    >
      <span>{label}</span>
      {count !== null ? (
        <span
          className={[
            "rounded-full px-2 py-0.5 text-[11px]",
            active
              ? "bg-white/15 text-white dark:bg-gray-950/10 dark:text-gray-950"
              : "bg-gray-100 text-gray-500 dark:bg-gray-900 dark:text-gray-400",
          ].join(" ")}
        >
          {count}
        </span>
      ) : null}
    </button>
  );
}

function AddDisclosurePanel({
  form,
  setForm,
  saving,
  onSubmit,
}: {
  form: DisclosureForm;
  setForm: React.Dispatch<React.SetStateAction<DisclosureForm>>;
  saving: boolean;
  onSubmit: (e: React.FormEvent) => Promise<void>;
}) {
  return (
    <form onSubmit={onSubmit} className="rounded-lg border border-orange-200 bg-white p-4 dark:border-orange-900 dark:bg-gray-950">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <h2 className="text-sm font-bold uppercase tracking-wider text-gray-500 dark:text-gray-400">Add Disclosure Signal</h2>
            <HintMarker label="Disclosure signal">
              <p>
                A source-dated ownership, insider, political, institutional, or regulatory disclosure that can affect market setup. Prophet stores the filing date and the event date separately so late disclosures do not look timely.
              </p>
            </HintMarker>
          </div>
        </div>
        <div className="inline-flex h-9 w-9 items-center justify-center rounded-lg border border-orange-200 text-orange-600 dark:border-orange-900 dark:text-orange-300">
          <FileText className="h-4 w-4" />
        </div>
      </div>
      <div className="mt-4 grid gap-3 sm:grid-cols-2">
        <select
          value={form.sourceType}
          onChange={(e) => setForm((current) => ({ ...current, sourceType: e.target.value as DisclosureForm["sourceType"] }))}
          className={inputClass}
          title="Where the disclosure came from."
        >
          <option value="ownership_tracker">Ownership tracker</option>
          <option value="filing">Filing</option>
        </select>
        <select
          value={form.sourceItemType}
          onChange={(e) => setForm((current) => ({ ...current, sourceItemType: e.target.value as DisclosureForm["sourceItemType"] }))}
          className={inputClass}
          title="What kind of disclosure this is. This controls provenance and source-learning treatment, not the conclusion."
        >
          <option value="ownership_disclosure">Ownership</option>
          <option value="insider_disclosure">Insider</option>
          <option value="institutional_flow">Institutional flow</option>
          <option value="congressional_trade_disclosure">Congressional trade</option>
        </select>
        <input value={form.sourceName} onChange={(e) => setForm((current) => ({ ...current, sourceName: e.target.value }))} placeholder="Source name" className={`${inputClass} sm:col-span-2`} title="Example: SEC Form 4, Quiver Quant, Capitol Trades, fund filing." />
        <input value={form.ticker} onChange={(e) => setForm((current) => ({ ...current, ticker: e.target.value }))} placeholder="Ticker" className={inputClass} title="The traded or affected ticker, when known." />
        <input value={form.issuer} onChange={(e) => setForm((current) => ({ ...current, issuer: e.target.value }))} placeholder="Issuer" className={inputClass} title="The company or security issuer named in the disclosure." />
        <input value={form.actorName} onChange={(e) => setForm((current) => ({ ...current, actorName: e.target.value }))} placeholder="Actor" className={inputClass} title="The insider, politician, fund, or institution connected to the disclosure." />
        <input value={form.actorType} onChange={(e) => setForm((current) => ({ ...current, actorType: e.target.value }))} placeholder="Actor type" className={inputClass} title="Example: insider, director, fund, senator, institution." />
        <input value={form.transactionType} onChange={(e) => setForm((current) => ({ ...current, transactionType: e.target.value }))} placeholder="Transaction" className={inputClass} title="Example: buy, sell, option exercise, new position, increased stake." />
        <input value={form.transactionValue} onChange={(e) => setForm((current) => ({ ...current, transactionValue: e.target.value }))} placeholder="$ value or range" className={inputClass} title="Use the disclosed dollar value, share count, or range. Ranges are preserved as text." />
        <label className="space-y-1 text-xs font-semibold uppercase tracking-wider text-gray-400">
          Trade date
          <input type="datetime-local" value={form.transactionDate} onChange={(e) => setForm((current) => ({ ...current, transactionDate: e.target.value }))} className={inputClass} title="When the trade or ownership event happened." />
        </label>
        <label className="space-y-1 text-xs font-semibold uppercase tracking-wider text-gray-400">
          Disclosure date
          <input type="datetime-local" value={form.disclosureDate} onChange={(e) => setForm((current) => ({ ...current, disclosureDate: e.target.value }))} className={inputClass} title="When the market could reasonably know about it." />
        </label>
        <input value={form.url} onChange={(e) => setForm((current) => ({ ...current, url: e.target.value }))} placeholder="Filing or tracker URL" className={`${inputClass} sm:col-span-2`} title="Source URL used for citation and recheck." />
        <textarea value={form.summary} onChange={(e) => setForm((current) => ({ ...current, summary: e.target.value }))} rows={3} placeholder="Summary" className={`${inputClass} sm:col-span-2`} title="What happened and why it might matter to the setup. Prophet will still treat it as evidence, not a conclusion." />
      </div>
      <button disabled={saving || !form.sourceName.trim()} className="mt-3 w-full rounded-lg bg-orange-600 px-4 py-3 text-sm font-medium text-white disabled:opacity-50">
        {saving ? "Saving..." : "Save disclosure"}
      </button>
    </form>
  );
}

function SummaryCard({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-lg border border-gray-200 bg-white px-4 py-3 dark:border-gray-800 dark:bg-gray-950">
      <div className="text-[10px] font-bold uppercase tracking-widest text-gray-400">{label}</div>
      <div className="mt-1 text-2xl font-bold tracking-tight">{value}</div>
    </div>
  );
}

function SourceSection({
  title,
  subtitle,
  sources,
  empty,
  onToggleTrusted,
}: {
  title: string;
  subtitle: string;
  sources: SourceRecord[];
  empty: string;
  onToggleTrusted: (source: SourceRecord) => Promise<void>;
}) {
  const visibleSources = sources.slice(0, 12);
  return (
    <section className="space-y-3">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h2 className="text-sm font-bold uppercase tracking-wider text-gray-500 dark:text-gray-400">{title}</h2>
          <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">{subtitle}</p>
        </div>
        {sources.length > visibleSources.length ? (
          <p className="text-xs text-gray-500 dark:text-gray-400">
            Showing {visibleSources.length} of {sources.length}
          </p>
        ) : null}
      </div>
      {sources.length === 0 ? (
        <div className="rounded-lg border border-dashed border-gray-200 bg-white p-5 text-sm text-gray-500 dark:border-gray-800 dark:bg-gray-950">
          {empty}
        </div>
      ) : (
        <div className="grid min-w-0 gap-3 2xl:grid-cols-2">
          {visibleSources.map((source) => {
            const origin = normalizeSourceOrigin(source.origin);
            return (
            <article key={source.id} className="min-w-0 rounded-lg border border-gray-200 bg-white p-4 dark:border-gray-800 dark:bg-gray-950">
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <h3 className="truncate text-base font-semibold tracking-tight">{source.name}</h3>
                    <StatusPill tone={source.is_trusted ? "good" : "neutral"}>{source.is_trusted ? "trusted" : "learning"}</StatusPill>
                    <StatusPill>{source.source_type.replaceAll("_", " ")}</StatusPill>
                    <OriginPill kind={origin.origin_kind}>{origin.origin_label}</OriginPill>
                  </div>
                  {source.description ? (
                    <p className="mt-2 line-clamp-2 text-sm text-gray-600 dark:text-gray-300">{source.description}</p>
                  ) : null}
                  {origin.origin_detail ? (
                    <p className="mt-2 line-clamp-2 text-xs text-gray-500 dark:text-gray-400">
                      Origin: {origin.origin_detail}
                    </p>
                  ) : null}
                  {source.url ? (
                    <a href={source.url} target="_blank" rel="noreferrer" className="mt-2 inline-flex max-w-full items-center gap-1 truncate text-sm text-indigo-600 dark:text-indigo-400">
                      <span className="truncate">{source.url}</span>
                      <ExternalLink className="h-3.5 w-3.5 shrink-0" />
                    </a>
                  ) : null}
                </div>
                <button
                  onClick={() => void onToggleTrusted(source)}
                  className="shrink-0 rounded-lg border border-gray-300 px-3 py-2 text-sm dark:border-gray-700"
                >
                  {source.is_trusted ? "Untrust" : "Trust"}
                </button>
              </div>

              <SourceScoreSummary source={source} />
              <details className="mt-3 rounded-lg border border-gray-100 bg-gray-50/60 px-3 py-2 text-sm text-gray-600 dark:border-gray-800 dark:bg-gray-900 dark:text-gray-300">
                <summary className="cursor-pointer select-none text-[10px] font-bold uppercase tracking-widest text-gray-400">
                  Score details
                </summary>
                <div className="mt-3 grid gap-3 md:grid-cols-2">
                  <SourceMetric title="Trust profile">
                    <div>Reliability: {source.trust_profile?.factual_reliability?.replaceAll("_", " ") ?? "not scored yet"}</div>
                    <div>Noise: {source.trust_profile?.noise_ratio?.replaceAll("_", " ") ?? "not scored yet"}</div>
                    <div>Trajectory: {source.trust_profile?.trust_trajectory?.replaceAll("_", " ") ?? "not scored yet"}</div>
                  </SourceMetric>
                  <SourceMetric title="Value profile">
                    <div>Idea value: {source.value_profile?.idea_generation_value?.replaceAll("_", " ") ?? "not scored yet"}</div>
                    <div>Timing value: {source.value_profile?.timing_value?.replaceAll("_", " ") ?? "not scored yet"}</div>
                    <div>Portfolio relevance: {source.value_profile?.portfolio_relevance_value?.replaceAll("_", " ") ?? "not scored yet"}</div>
                  </SourceMetric>
                </div>
                {source.performance_history.length > 0 ? (
                  <div className="mt-3 rounded-lg border border-gray-100 bg-white/70 px-3 py-3 text-sm text-gray-600 dark:border-gray-800 dark:bg-gray-950/60 dark:text-gray-300">
                    <div className="text-[10px] font-bold uppercase tracking-widest text-gray-400">Outcome history</div>
                    {source.performance_history.slice(0, 1).map((history) => (
                      <div key={history.id} className="mt-2 flex flex-wrap gap-x-4 gap-y-1">
                        <span>{history.total_claims} assessed claims</span>
                        <span>{Math.round(history.accuracy_rate * 100)}% accuracy</span>
                        <span>{Math.round(history.timing_score * 100)}% timing</span>
                      </div>
                    ))}
                  </div>
                ) : null}
                {source.claim_queue.total > 0 ? (
                  <div className="mt-3 border-t border-gray-200 pt-3 text-sm text-gray-600 dark:border-gray-800 dark:text-gray-300">
                    <div className="text-[10px] font-bold uppercase tracking-widest text-gray-400">Claim calibration queue</div>
                    <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1">
                      <span>{source.claim_queue.total} tracked</span>
                      <span>{source.claim_queue.assessed} assessed</span>
                      <span>{source.claim_queue.pending} pending</span>
                      {source.claim_queue.deferred > 0 ? <span>{source.claim_queue.deferred} waiting for retry</span> : null}
                    </div>
                    <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
                      Pending claims are tested against later evidence. Inconclusive reviews wait for new evidence instead of being repeatedly rescored.
                    </p>
                    {source.claim_queue.last_assessment_at ? (
                      <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                        Last completed assessment {formatDate(source.claim_queue.last_assessment_at)}
                      </p>
                    ) : null}
                  </div>
                ) : null}
              </details>
            </article>
            );
          })}
        </div>
      )}
    </section>
  );
}

function SourceScoreSummary({ source }: { source: SourceRecord }) {
  const reliability = source.trust_profile?.factual_reliability?.replaceAll("_", " ") ?? "unscored";
  const relevance = source.value_profile?.portfolio_relevance_value?.replaceAll("_", " ") ?? "unscored";
  const topSegment = source.quality_segments[0];
  const segmentLabel = topSegment
    ? `${(topSegment.domain || topSegment.ticker || topSegment.horizon || "general").replaceAll("_", " ")} q${topSegment.quality_score.toFixed(2)}`
    : "no segment score";
  return (
    <div className="mt-4 flex flex-wrap gap-2">
      <StatusPill>{source.evidence_count} evidence items</StatusPill>
      <StatusPill tone={source.trust_profile?.factual_reliability === "high" ? "good" : "neutral"}>
        reliability {reliability}
      </StatusPill>
      <StatusPill tone={source.value_profile?.portfolio_relevance_value === "high" ? "good" : "neutral"}>
        relevance {relevance}
      </StatusPill>
      <StatusPill tone="info">{segmentLabel}</StatusPill>
      {source.claim_queue.total > 0 ? (
        <StatusPill
          tone={source.claim_queue.deferred > 0 ? "info" : "neutral"}
          title="Older source claims are revisited against later evidence; deferred claims are waiting for their next eligible review time."
        >
          {source.claim_queue.assessed}/{source.claim_queue.total} claims assessed
        </StatusPill>
      ) : null}
    </div>
  );
}

function SourceMetric({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-gray-100 bg-white/70 px-3 py-3 dark:border-gray-800 dark:bg-gray-950/60">
      <div className="text-[10px] font-bold uppercase tracking-widest text-gray-400">{title}</div>
      <div className="mt-2 space-y-1 text-sm text-gray-600 dark:text-gray-300">{children}</div>
    </div>
  );
}

function SubjectAliasPanel({
  aliases,
  onApprove,
  onRemove,
}: {
  aliases: SubjectAliasRecord[];
  onApprove: (aliasId: string) => Promise<void>;
  onRemove: (aliasId: string) => Promise<void>;
}) {
  const visibleAliases = aliases.slice(0, 16);
  return (
    <section className="space-y-3">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h2 className="text-sm font-bold uppercase tracking-wider text-gray-500 dark:text-gray-400">Subject Aliases</h2>
          <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
            Inspect the shorthand, synonyms, and pattern labels Prophet uses when resolving context.
          </p>
        </div>
        {aliases.length > visibleAliases.length ? (
          <p className="text-xs text-gray-500 dark:text-gray-400">
            Showing {visibleAliases.length} of {aliases.length}
          </p>
        ) : null}
      </div>
      {visibleAliases.length === 0 ? (
        <div className="rounded-lg border border-dashed border-gray-200 bg-white p-5 text-sm text-gray-500 dark:border-gray-800 dark:bg-gray-950">
          No subject aliases recorded yet.
        </div>
      ) : (
        <div className="grid gap-3 2xl:grid-cols-2">
          {visibleAliases.map((alias) => (
            <article key={alias.id} className="rounded-lg border border-gray-200 bg-white p-4 dark:border-gray-800 dark:bg-gray-950">
              <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <h3 className="text-base font-semibold tracking-tight">{alias.alias}</h3>
                    <StatusPill tone={alias.source === "user_approved" ? "good" : alias.source === "system" || alias.source.startsWith("seed") ? "info" : "neutral"}>
                      {alias.source.replaceAll("_", " ")}
                    </StatusPill>
                    <StatusPill>{Math.round(alias.confidence * 100)}%</StatusPill>
                  </div>
                  <p className="mt-2 text-sm text-gray-700 dark:text-gray-200">
                    {alias.subject_name}
                  </p>
                  <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                    {alias.subject_type.replaceAll("_", " ")} · updated {formatDate(alias.updated_at)}
                  </p>
                </div>
                {alias.linked_symbols.length > 0 ? (
                  <div className="flex shrink-0 flex-wrap gap-1.5 sm:justify-end">
                    {alias.linked_symbols.slice(0, 4).map((symbol) => (
                      <StatusPill key={`${alias.id}-${symbol}`} tone="good">{symbol}</StatusPill>
                    ))}
                  </div>
                ) : null}
              </div>
              <div className="mt-4 flex flex-wrap gap-2">
                <button
                  type="button"
                  onClick={() => void onApprove(alias.id)}
                  disabled={alias.source === "user_approved"}
                  className="inline-flex items-center gap-2 rounded-lg border border-emerald-200 px-3 py-2 text-sm text-emerald-700 hover:bg-emerald-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-emerald-900 dark:text-emerald-300 dark:hover:bg-emerald-950/30"
                >
                  <CheckCircle2 className="h-4 w-4" />
                  {alias.source === "user_approved" ? "Approved" : "Approve"}
                </button>
                <button
                  type="button"
                  onClick={() => void onRemove(alias.id)}
                  className="inline-flex items-center gap-2 rounded-lg border border-rose-200 px-3 py-2 text-sm text-rose-700 hover:bg-rose-50 dark:border-rose-900 dark:text-rose-300 dark:hover:bg-rose-950/30"
                >
                  <Trash2 className="h-4 w-4" />
                  Remove
                </button>
              </div>
              {alias.reason ? (
                <p className="mt-3 line-clamp-2 text-sm text-gray-600 dark:text-gray-300">{alias.reason}</p>
              ) : null}
              {alias.normalized_alias !== alias.alias.toLowerCase() ? (
                <p className="mt-3 rounded-lg border border-gray-100 bg-gray-50 px-3 py-2 font-mono text-xs text-gray-500 dark:border-gray-800 dark:bg-gray-900 dark:text-gray-400">
                  {alias.normalized_alias}
                </p>
              ) : null}
            </article>
          ))}
        </div>
      )}
    </section>
  );
}

function RecentEvidence({
  evidence,
  feedbackNotes,
  setFeedbackNotes,
  onFlag,
}: {
  evidence: SourceEvidenceSummary[];
  feedbackNotes: Record<string, string>;
  setFeedbackNotes: React.Dispatch<React.SetStateAction<Record<string, string>>>;
  onFlag: (evidenceId: string, rating: "useful" | "not_useful") => Promise<void>;
}) {
  return (
    <section className="space-y-3">
      <div>
        <h2 className="text-sm font-bold uppercase tracking-wider text-gray-500 dark:text-gray-400">Recent Evidence to Train On</h2>
        <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
          Mark what helped and what wasted attention. These flags remain visible here and are attached to the original evidence.
        </p>
      </div>
      <div className="space-y-2">
        {evidence.slice(0, 12).map((item) => {
          const origin = normalizeSourceOrigin(item);
          return (
          <div key={item.id} className="rounded-lg border border-gray-200 bg-white p-4 dark:border-gray-800 dark:bg-gray-950">
            <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <p className="truncate font-medium">{item.title || "Untitled evidence"}</p>
                  <StatusPill>{item.source_name}</StatusPill>
                  <OriginPill kind={origin.origin_kind}>{origin.origin_label}</OriginPill>
                  {item.user_feedback?.rating ? (
                    <StatusPill tone={item.user_feedback.rating === "useful" ? "good" : "bad"}>
                      {item.user_feedback.rating === "useful" ? "useful" : "not useful"}
                    </StatusPill>
                  ) : null}
                  {item.user_feedback?.lesson_title ? (
                    <StatusPill tone="info">saved as lesson</StatusPill>
                  ) : null}
                </div>
                <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                  {item.source_item_type.replaceAll("_", " ")} · {formatDate(item.created_at)}
                </p>
                {origin.origin_detail ? (
                  <p className="mt-1 line-clamp-2 text-xs text-gray-500 dark:text-gray-400">
                    {origin.origin_detail}
                  </p>
                ) : null}
              </div>
              <div className="flex shrink-0 items-center gap-2">
                <button
                  type="button"
                  onClick={() => void onFlag(item.id, "useful")}
                  className="inline-flex h-9 w-9 items-center justify-center rounded-lg border border-emerald-200 text-emerald-700 hover:bg-emerald-50 dark:border-emerald-900 dark:text-emerald-300 dark:hover:bg-emerald-950/30"
                  title="Mark useful"
                >
                  <ThumbsUp className="h-4 w-4" />
                  <span className="sr-only">Mark useful</span>
                </button>
                <button
                  type="button"
                  onClick={() => void onFlag(item.id, "not_useful")}
                  className="inline-flex h-9 w-9 items-center justify-center rounded-lg border border-rose-200 text-rose-700 hover:bg-rose-50 dark:border-rose-900 dark:text-rose-300 dark:hover:bg-rose-950/30"
                  title="Mark not useful"
                >
                  <ThumbsDown className="h-4 w-4" />
                  <span className="sr-only">Mark not useful</span>
                </button>
              </div>
            </div>
            <input
              value={feedbackNotes[item.id] ?? ""}
              onChange={(event) =>
                setFeedbackNotes((current) => ({ ...current, [item.id]: event.target.value }))
              }
              placeholder="Optional feedback note"
              className="mt-3 w-full rounded-lg border border-gray-200 bg-gray-50 px-3 py-2 text-sm outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-500/20 dark:border-gray-800 dark:bg-gray-900"
            />
          </div>
          );
        })}
      </div>
    </section>
  );
}

function AddSourcePanel({
  form,
  setForm,
  saving,
  onSubmit,
}: {
  form: { name: string; source_type: string; url: string; description: string; is_trusted: boolean };
  setForm: React.Dispatch<React.SetStateAction<{ name: string; source_type: string; url: string; description: string; is_trusted: boolean }>>;
  saving: boolean;
  onSubmit: (e: React.FormEvent) => Promise<void>;
}) {
  return (
    <form onSubmit={onSubmit} className="rounded-lg border border-gray-200 bg-white p-4 dark:border-gray-800 dark:bg-gray-950">
      <h2 className="text-sm font-bold uppercase tracking-wider text-gray-500 dark:text-gray-400">Add Trusted Source</h2>
      <div className="mt-4 space-y-3">
        <select value={form.source_type} onChange={(e) => setForm((current) => ({ ...current, source_type: e.target.value }))} className={inputClass}>
          <option value="analyst">Analyst / investor</option>
          <option value="official">Official source</option>
          <option value="news">News</option>
          <option value="web_research">Web research</option>
          <option value="email">Email</option>
          <option value="filing">Filing / regulatory disclosure</option>
          <option value="ownership_tracker">Ownership / insider tracker</option>
          <option value="youtube">YouTube</option>
          <option value="x_account">X Account</option>
        </select>
        <input value={form.name} onChange={(e) => setForm((current) => ({ ...current, name: e.target.value }))} placeholder="Source name" className={inputClass} />
        <input value={form.url} onChange={(e) => setForm((current) => ({ ...current, url: e.target.value }))} placeholder="Optional URL" className={inputClass} />
        <textarea value={form.description} onChange={(e) => setForm((current) => ({ ...current, description: e.target.value }))} rows={3} placeholder="Why this source matters" className={inputClass} />
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={form.is_trusted} onChange={(e) => setForm((current) => ({ ...current, is_trusted: e.target.checked }))} />
          Trust immediately
        </label>
        <button disabled={saving || !form.name.trim()} className="w-full rounded-lg bg-indigo-600 px-4 py-3 text-sm font-medium text-white disabled:opacity-50">
          {saving ? "Saving..." : "Create source"}
        </button>
      </div>
    </form>
  );
}

function MediaCapabilityPanel({
  capabilities,
  mediaUrl,
  mediaTitle,
  mediaJob,
  onMediaUrlChange,
  onMediaTitleChange,
  onSubmit,
  onCancel,
  onManualTranscript,
}: {
  capabilities: MediaIngestionCapabilityResponse | null;
  mediaUrl: string;
  mediaTitle: string;
  mediaJob: MediaIngestionJob | null;
  onMediaUrlChange: (value: string) => void;
  onMediaTitleChange: (value: string) => void;
  onSubmit: (event: React.FormEvent) => Promise<void>;
  onCancel: () => void;
  onManualTranscript: () => void;
}) {
  const capabilityRows = capabilities?.capabilities ?? [];
  const jobActive = Boolean(mediaJob && ["queued", "running"].includes(mediaJob.status));
  const latestEvent = mediaJob?.events[mediaJob.events.length - 1];
  return (
    <section className="rounded-lg border border-sky-200 bg-white p-4 dark:border-sky-900 dark:bg-gray-950">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <h2 className="text-sm font-bold uppercase tracking-wider text-gray-500 dark:text-gray-400">Video Ingestion</h2>
            <HintMarker label="Video ingestion">
              <p>
                Prophet separates channel tracking, caption transcript ingestion, no-transcript audio transcription, and frame/OCR extraction. A channel source is not the same as a processed video.
              </p>
            </HintMarker>
          </div>
          <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
            No-transcript extraction: {capabilities?.can_extract_without_transcript ? "available" : "not configured"}
          </p>
        </div>
        <div className="inline-flex h-9 w-9 items-center justify-center rounded-lg border border-sky-200 text-sky-600 dark:border-sky-900 dark:text-sky-300">
          <Video className="h-4 w-4" />
        </div>
      </div>
      <form onSubmit={onSubmit} className="mt-4 space-y-2 rounded-lg border border-sky-100 bg-sky-50/60 p-3 dark:border-sky-950 dark:bg-sky-950/20">
        <label className="block text-xs font-semibold uppercase tracking-wider text-sky-800 dark:text-sky-200" htmlFor="youtube-video-url">
          Individual video
        </label>
        <input
          id="youtube-video-url"
          type="url"
          required
          value={mediaUrl}
          onChange={(event) => onMediaUrlChange(event.target.value)}
          placeholder="https://www.youtube.com/watch?v=..."
          className={inputClass}
        />
        <input
          value={mediaTitle}
          onChange={(event) => onMediaTitleChange(event.target.value)}
          placeholder="Optional research title"
          aria-label="Optional research title"
          className={inputClass}
        />
        <div className="flex flex-wrap items-center gap-2">
          <button
            disabled={!mediaUrl.trim() || jobActive}
            className="inline-flex min-h-10 flex-1 items-center justify-center gap-2 rounded-lg bg-sky-600 px-3 py-2 text-sm font-medium text-white disabled:opacity-50"
          >
            <Video className="h-4 w-4" />
            {jobActive ? "Processing video..." : "Ingest transcript"}
          </button>
          {jobActive ? (
            <button
              type="button"
              onClick={onCancel}
              className="min-h-10 rounded-lg border border-sky-200 px-3 py-2 text-sm font-medium text-sky-700 dark:border-sky-900 dark:text-sky-300"
            >
              Cancel
            </button>
          ) : null}
        </div>
        {mediaJob ? (
          <div className="flex items-start justify-between gap-3 rounded-lg border border-sky-100 bg-white px-3 py-2 text-xs dark:border-sky-950 dark:bg-gray-950">
            <div className="min-w-0">
              <p className="font-medium text-gray-700 dark:text-gray-200">{latestEvent?.message || "Video ingestion queued."}</p>
              {mediaJob.result?.ok ? (
                <p className="mt-1 text-emerald-700 dark:text-emerald-300">
                  Saved {mediaJob.result.transcript_length?.toLocaleString() ?? 0} transcript characters via {mediaJob.result.ingest_mode?.replaceAll("_", " ")}.
                </p>
              ) : mediaJob.result?.error ? (
                <p className="mt-1 text-rose-700 dark:text-rose-300">{mediaJob.result.error}</p>
              ) : null}
            </div>
            <StatusPill tone={mediaJob.result?.ok ? "good" : mediaJob.result?.ok === false || mediaJob.status === "error" ? "bad" : "info"}>
              {mediaJob.status}
            </StatusPill>
          </div>
        ) : null}
      </form>
      {capabilities ? (
        <>
          <div className="mt-4 space-y-2">
            {capabilityRows.map((item) => (
              <div key={item.key} className="rounded-lg border border-gray-100 px-3 py-3 text-sm dark:border-gray-800">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="font-medium">{item.label}</span>
                  <StatusPill tone={item.status === "available" ? "good" : "neutral"}>{item.status.replaceAll("_", " ")}</StatusPill>
                </div>
                <p className="mt-1 text-xs leading-5 text-gray-500 dark:text-gray-400">{item.detail}</p>
              </div>
            ))}
          </div>
          <p className="mt-3 rounded-lg border border-sky-100 bg-sky-50 px-3 py-2 text-xs leading-5 text-sky-800 dark:border-sky-950 dark:bg-sky-950/30 dark:text-sky-200">
            {capabilities.current_best_path}
          </p>
          <button
            type="button"
            onClick={onManualTranscript}
            className="mt-3 inline-flex w-full items-center justify-center gap-2 rounded-lg border border-sky-200 px-3 py-2 text-sm font-medium text-sky-700 hover:bg-sky-50 dark:border-sky-900 dark:text-sky-300 dark:hover:bg-sky-950/30"
            title="Prepare a note that stores user-supplied transcript text or detailed notes from a YouTube video."
          >
            <FileText className="h-4 w-4" />
            Add manual transcript
          </button>
        </>
      ) : (
        <p className="mt-4 text-sm text-gray-500 dark:text-gray-400">Loading media ingestion status...</p>
      )}
    </section>
  );
}

function AddNotePanel({
  sources,
  form,
  setForm,
  saving,
  onSubmit,
  onCagrTest,
}: {
  sources: SourceRecord[];
  form: NoteForm;
  setForm: React.Dispatch<React.SetStateAction<NoteForm>>;
  saving: boolean;
  onSubmit: (e: React.FormEvent) => Promise<void>;
  onCagrTest: () => void;
}) {
  return (
    <form onSubmit={onSubmit} className="rounded-lg border border-indigo-200 bg-white p-4 dark:border-indigo-900 dark:bg-gray-950">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="text-sm font-bold uppercase tracking-wider text-gray-500 dark:text-gray-400">Add Note</h2>
          <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">Save tests, assumptions, or observations as evidence.</p>
        </div>
        <button type="button" onClick={onCagrTest} className="inline-flex h-9 w-9 items-center justify-center rounded-lg border border-indigo-200 text-indigo-600 dark:border-indigo-900 dark:text-indigo-300" title="Prefill CAGR test note">
          <BookmarkPlus className="h-4 w-4" />
          <span className="sr-only">Prefill CAGR test note</span>
        </button>
      </div>
      <div className="mt-4 space-y-3">
        <input value={form.title} onChange={(e) => setForm((current) => ({ ...current, title: e.target.value }))} placeholder="25.9% CAGR test" className={inputClass} />
        <select value={form.noteType} onChange={(e) => setForm((current) => ({ ...current, noteType: e.target.value as NoteType }))} className={inputClass}>
          {noteTypeOptions.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
        <select value={form.sourceId} onChange={(e) => setForm((current) => ({ ...current, sourceId: e.target.value }))} className={inputClass}>
          <option value="">Manual Research Inbox</option>
          {sources.map((source) => (
            <option key={source.id} value={source.id}>
              {source.name}
            </option>
          ))}
        </select>
        <input
          value={form.url}
          onChange={(e) => setForm((current) => ({ ...current, url: e.target.value }))}
          placeholder="Optional video or source URL"
          className={inputClass}
        />
        <textarea value={form.content} onChange={(e) => setForm((current) => ({ ...current, content: e.target.value }))} rows={5} placeholder="Write the note, test, assumption, or calculation..." className={inputClass} />
        <button disabled={saving || !form.content.trim()} className="w-full rounded-lg bg-gray-900 px-4 py-3 text-sm font-medium text-white disabled:opacity-50 dark:bg-gray-100 dark:text-gray-950">
          {saving ? "Saving..." : "Save note"}
        </button>
      </div>
    </form>
  );
}

function FlaggedPanel({ feedback, onClear }: { feedback: SourceFeedbackRecord[]; onClear: (evidenceId: string) => Promise<void> }) {
  return (
    <section className="rounded-lg border border-gray-200 bg-white p-4 dark:border-gray-800 dark:bg-gray-950">
      <h2 className="text-sm font-bold uppercase tracking-wider text-gray-500 dark:text-gray-400">Flagged Later</h2>
      <div className="mt-3 space-y-2">
        {feedback.length === 0 ? (
          <p className="text-sm text-gray-500 dark:text-gray-400">No useful/noisy flags yet.</p>
        ) : (
          feedback.slice(0, 8).map((item) => {
            const origin = normalizeSourceOrigin(item);
            return (
            <div key={item.evidence_id} className="rounded-lg border border-gray-100 px-3 py-3 text-sm dark:border-gray-800">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <p className="truncate font-medium">{item.title || "Untitled evidence"}</p>
                  <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">{item.source_name} · {origin.origin_label} · {formatDate(item.flagged_at || item.created_at)}</p>
                </div>
                <StatusPill tone={item.rating === "useful" ? "good" : "bad"}>
                  {item.rating === "useful" ? "useful" : "not useful"}
                </StatusPill>
              </div>
              {item.note ? <p className="mt-2 text-gray-600 dark:text-gray-300">{item.note}</p> : null}
              {item.lesson_title ? (
                <p className="mt-2 rounded-lg bg-indigo-50 px-2.5 py-2 text-xs text-indigo-700 dark:bg-indigo-950/30 dark:text-indigo-300">
                  Preference lesson: {item.lesson_title}
                </p>
              ) : null}
              <button type="button" onClick={() => void onClear(item.evidence_id)} className="mt-2 inline-flex items-center gap-1 text-xs text-gray-500 hover:text-gray-800 dark:hover:text-gray-200">
                <RotateCcw className="h-3.5 w-3.5" />
                Clear flag
              </button>
            </div>
            );
          })
        )}
      </div>
    </section>
  );
}

function NotesPanel({ notes }: { notes: SourceEvidenceSummary[] }) {
  return (
    <section className="rounded-lg border border-gray-200 bg-white p-4 dark:border-gray-800 dark:bg-gray-950">
      <h2 className="text-sm font-bold uppercase tracking-wider text-gray-500 dark:text-gray-400">Recent Notes</h2>
      <div className="mt-3 space-y-2">
        {notes.length === 0 ? (
          <p className="text-sm text-gray-500 dark:text-gray-400">No notes saved yet.</p>
        ) : (
          notes.slice(0, 6).map((note) => {
            const origin = normalizeSourceOrigin(note);
            return (
            <div key={note.id} className="rounded-lg border border-gray-100 px-3 py-3 text-sm dark:border-gray-800">
              <p className="font-medium">{note.title || "Untitled note"}</p>
              <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">{note.source_name} · {origin.origin_label} · {formatDate(note.created_at)}</p>
            </div>
            );
          })
        )}
      </div>
    </section>
  );
}

function StatusPill({
  children,
  tone = "neutral",
  title,
}: {
  children: React.ReactNode;
  tone?: "neutral" | "good" | "bad" | "info";
  title?: string;
}) {
  const className =
    tone === "good"
      ? "border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-900 dark:bg-emerald-950/30 dark:text-emerald-300"
      : tone === "bad"
      ? "border-rose-200 bg-rose-50 text-rose-700 dark:border-rose-900 dark:bg-rose-950/30 dark:text-rose-300"
      : tone === "info"
      ? "border-indigo-200 bg-indigo-50 text-indigo-700 dark:border-indigo-900 dark:bg-indigo-950/30 dark:text-indigo-300"
      : "border-gray-200 bg-gray-50 text-gray-600 dark:border-gray-800 dark:bg-gray-900 dark:text-gray-300";
  return (
    <span title={title} className={`rounded-full border px-2.5 py-1 text-[10px] font-semibold uppercase tracking-wider ${className}`}>
      {children}
    </span>
  );
}

function OriginPill({ children, kind }: { children: React.ReactNode; kind?: string | null }) {
  const normalized = (kind || "").toLowerCase();
  const className =
    normalized === "manual"
      ? "border-sky-200 bg-sky-50 text-sky-700 dark:border-sky-900 dark:bg-sky-950/30 dark:text-sky-300"
      : normalized === "email"
      ? "border-amber-200 bg-amber-50 text-amber-800 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-300"
      : normalized === "automation"
      ? "border-violet-200 bg-violet-50 text-violet-700 dark:border-violet-900 dark:bg-violet-950/30 dark:text-violet-300"
      : normalized === "discovery"
      ? "border-cyan-200 bg-cyan-50 text-cyan-700 dark:border-cyan-900 dark:bg-cyan-950/30 dark:text-cyan-300"
      : normalized === "chat"
      ? "border-fuchsia-200 bg-fuchsia-50 text-fuchsia-700 dark:border-fuchsia-900 dark:bg-fuchsia-950/30 dark:text-fuchsia-300"
      : normalized === "disclosure"
      ? "border-orange-200 bg-orange-50 text-orange-700 dark:border-orange-900 dark:bg-orange-950/30 dark:text-orange-300"
      : "border-gray-200 bg-gray-50 text-gray-600 dark:border-gray-800 dark:bg-gray-900 dark:text-gray-300";
  return (
    <span className={`rounded-full border px-2.5 py-1 text-[10px] font-semibold uppercase tracking-wider ${className}`}>
      {children}
    </span>
  );
}

function formatDate(value?: string | null) {
  if (!value) return "unknown time";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "unknown time";
  return date.toLocaleString();
}

function toIsoOrNull(value: string) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date.toISOString();
}

function compactRecord(record: Record<string, unknown>) {
  return Object.fromEntries(
    Object.entries(record).filter(([, value]) => value !== null && value !== undefined && value !== "")
  );
}

const inputClass =
  "w-full rounded-lg border border-gray-300 bg-white px-3 py-2.5 text-sm outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-500/20 dark:border-gray-700 dark:bg-gray-900";
