"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { apiFetch } from "@/lib/api";
import type { PresentationLanguage } from "@/lib/language-resources";
import { TRANSLATION_REQUEST_TIMEOUT_MS } from "@/lib/request-timeouts";
import { usePresentationLanguage } from "./PresentationLanguageProvider";

type Translation = { target_language: string; status: string; source_hash: string; translated_text: string | null; translation_id?: string; provider?: string; original_url: string };

export default function EvidenceTranslationPanel({ evidenceId }: { evidenceId: string }) {
  const { language } = usePresentationLanguage();
  return <SourceTranslation key={`${evidenceId}:${language}`} evidenceId={evidenceId} language={language} />;
}

function SourceTranslation({ evidenceId, language }: { evidenceId: string; language: PresentationLanguage }) {
  const { t } = useTranslation();
  const [view, setView] = useState<Translation | null>(null);
  const [error, setError] = useState(false);
  const [busy, setBusy] = useState(false);
  const [original, setOriginal] = useState<{ text: string; source_hash: string } | null>(null);
  const [originalError, setOriginalError] = useState(false);
  const [originalBusy, setOriginalBusy] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);
  useEffect(() => {
    let active = true;
    apiFetch<Translation>(`/sources/evidence/${evidenceId}/translation?language=${encodeURIComponent(language)}`)
      .then(value => { if (active) { setView(value); setError(false); } })
      .catch(() => { if (active) setError(true); });
    return () => { active = false; };
  }, [evidenceId, language, refreshKey]);
  async function translate() {
    setBusy(true);
    try {
      const response = await apiFetch<Translation>(`/sources/evidence/${evidenceId}/translation?language=${encodeURIComponent(language)}`, { method: "POST" }, TRANSLATION_REQUEST_TIMEOUT_MS);
      setView(response);
      if (original && original.source_hash !== response.source_hash) setOriginal(null);
      setError(false);
    }
    catch { setError(true); }
    finally { setBusy(false); }
  }
  async function readOriginal() {
    setOriginalBusy(true);
    try { setOriginal(await apiFetch<{ text: string; source_hash: string }>(`/sources/evidence/${evidenceId}/original-text`)); setOriginalError(false); }
    catch { setOriginalError(true); }
    finally { setOriginalBusy(false); }
  }
  const currentView = view?.target_language === language && (!original || original.source_hash === view.source_hash) ? view : null;
  return <section lang={language} className="rounded-lg border border-line bg-panel p-6">
    <h2 className="font-semibold">{t("translation")} · {language}</h2>
    <p role="status" className="mt-3 text-sm text-muted">{error ? t("failed") : t(original && view && original.source_hash !== view.source_hash ? "stale" : (currentView?.status ?? "loading"))}</p>
    {currentView?.translated_text && !error ? <div className="mt-4 whitespace-pre-wrap break-words text-sm leading-relaxed">{currentView.translated_text}</div> : null}
    {currentView?.source_hash ? <details className="mt-3 text-xs text-muted"><summary className="cursor-pointer">{t("version")}</summary><p className="mt-2 break-all">{currentView.source_hash}</p><p>{currentView.translation_id} · {currentView.provider}</p></details> : null}
    <p className="mt-4 text-xs text-muted">{t("caution")}</p>
    <p className="mt-3 text-xs text-muted">{t("cost")}</p>
    <div className="mt-4 flex flex-wrap items-center gap-3">
      {view?.target_language === language && !currentView?.translated_text && currentView?.status !== "translation_in_progress" ? <button onClick={() => void translate()} disabled={busy} className="rounded border border-line px-3 py-2 text-sm disabled:opacity-50">{busy ? t("working") : t("translate")}</button> : null}
      {error || currentView?.status === "translation_in_progress" ? <button onClick={() => setRefreshKey(value => value + 1)} className="rounded border border-line px-3 py-2 text-sm">{t("retry")}</button> : null}
      <a href={currentView?.original_url ?? "#original-source-text"} onClick={event => { event.preventDefault(); if (!originalBusy) void readOriginal(); }} aria-disabled={originalBusy} className="rounded border border-line px-3 py-2 text-sm">{t("original")}</a>
      <Link href="/settings" className="text-sm underline">{t("Settings")}</Link>
    </div>
    {originalError ? <p role="status" className="mt-3 text-sm text-amber-700 dark:text-amber-300">{t("failed")}</p> : null}
    {original ? <details id="original-source-text" open className="mt-5 border-t border-line pt-4"><summary className="cursor-pointer font-semibold">{t("original")}</summary><p className="mt-2 break-all text-xs text-muted">{original.source_hash}</p><pre className="mt-3 whitespace-pre-wrap break-words font-sans text-sm">{original.text}</pre></details> : null}
  </section>;
}
