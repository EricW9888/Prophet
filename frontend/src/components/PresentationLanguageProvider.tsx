"use client";

import { createInstance } from "i18next";
import { I18nextProvider } from "react-i18next";
import { createContext, useContext, useEffect, useState } from "react";
import { apiFetch, IntegrationSettings } from "@/lib/api";
import { languageOptions, languageResources, PresentationLanguage } from "@/lib/language-resources";

const LanguageContext = createContext<{ language: PresentationLanguage; error: boolean }>({ language: "en", error: false });
export const usePresentationLanguage = () => useContext(LanguageContext);

export default function PresentationLanguageProvider({ children }: { children: React.ReactNode }) {
  const [instance] = useState(() => {
    const value = createInstance();
    void value.init({ resources: languageResources, lng: "en", fallbackLng: "en", initAsync: false, interpolation: { escapeValue: false } });
    return value;
  });
  const [state, setState] = useState<{ language: PresentationLanguage; error: boolean }>({ language: "en", error: false });
  useEffect(() => {
    let active = true;
    let refreshVersion = 0;
    async function refresh() {
      const version = ++refreshVersion;
      try {
        const settings = await apiFetch<IntegrationSettings>("/integrations/settings");
        if (!active || version !== refreshVersion) return;
        const language = settings.presentation_language ?? "en";
        if (!languageOptions.some(option => option.code === language)) throw new Error("Unsupported language");
        await instance.changeLanguage(language);
        if (active && version === refreshVersion) setState({ language, error: false });
      } catch { if (active && version === refreshVersion) setState(current => ({ ...current, error: true })); }
    }
    void refresh();
    window.addEventListener("prophet:language-saved", refresh);
    return () => { active = false; window.removeEventListener("prophet:language-saved", refresh); };
  }, [instance]);
  return <LanguageContext.Provider value={state}><I18nextProvider i18n={instance}>{children}</I18nextProvider></LanguageContext.Provider>;
}
