"use client";

import { useTranslation } from "react-i18next";
import { languageOptions, PresentationLanguage } from "@/lib/language-resources";
import { usePresentationLanguage } from "./PresentationLanguageProvider";

export default function LanguageSettings({ value, onChange }: { value: PresentationLanguage; onChange: (language: PresentationLanguage) => void }) {
  const { t } = useTranslation();
  const { language, error } = usePresentationLanguage();
  return <section lang={language} className="mb-6 rounded-lg border border-line bg-panel p-4">
    <label htmlFor="presentation-language" className="block text-sm font-semibold">{t("language")}</label>
    <select id="presentation-language" value={value} onChange={event => onChange(event.target.value as PresentationLanguage)} className="mt-2 w-full max-w-sm rounded border border-line bg-panel px-3 py-2">
      {languageOptions.map(option => <option key={option.code} value={option.code}>{option.label}</option>)}
    </select>
    <p className="mt-3 text-sm text-muted">{t("coverage")}</p>
    <p className="mt-2 text-sm text-muted">{t("generation")}</p>
    {error ? <p role="status" className="mt-2 text-sm text-amber-700 dark:text-amber-300">{t("unavailable")}</p> : null}
  </section>;
}
