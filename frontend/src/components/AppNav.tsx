"use client";

import {
  BookOpenText,
  BriefcaseBusiness,
  ClipboardCheck,
  FlaskConical,
  Menu,
  MessageSquare,
  Radar,
  RefreshCw,
  Settings,
  X,
} from "lucide-react";
import Link, { useLinkStatus } from "next/link";
import WorkspaceWorkIndicator from "@/components/WorkspaceWorkIndicator";
import ActivityIndicator from "@/components/ActivityIndicator";
import ProphetMark from "@/components/ProphetMark";
import { useTranslation } from "react-i18next";
import { usePathname } from "next/navigation";
import { type ComponentType, type SVGProps, useEffect, useState } from "react";

import { locate, NAV_SECTIONS, type SectionKey, SETTINGS_HREF } from "@/lib/navigation";

type NavIcon = ComponentType<SVGProps<SVGSVGElement>>;
export type NavKey =
  | "portfolio"
  | "history"
  | "research"
  | "timeline"
  | "knowledge"
  | "sources"
  | "activity"
  | "risk"
  | "opportunities"
  | "review"
  | "experiments"
  | "positions"
  | "decisions"
  | "settings";

const SECTION_ICONS: Record<SectionKey, NavIcon> = {
  portfolio: BriefcaseBusiness,
  chat: MessageSquare,
  research: BookOpenText,
  monitor: Radar,
  review: ClipboardCheck,
  simulate: FlaskConical,
};

/** Fallback only: the URL decides the section; pages still pass their old key. */
const LEGACY_SECTION: Partial<Record<NavKey, SectionKey>> = {
  portfolio: "portfolio", history: "portfolio", risk: "portfolio", positions: "portfolio", decisions: "portfolio",
  research: "chat", knowledge: "research", sources: "research", opportunities: "research",
  timeline: "monitor", activity: "monitor", review: "review", experiments: "simulate",
};

function NavigationIcon({ icon: Icon }: { icon: NavIcon }) {
  const { pending } = useLinkStatus();
  return pending ? <><ActivityIndicator state="working" /><span className="sr-only">Loading destination</span></> : <Icon className="h-4 w-4 shrink-0" aria-hidden="true" />;
}

function NavLink({ href, label, icon, selected, compact = false, onNavigate }: {
  href: string;
  label: string;
  icon?: NavIcon;
  selected: boolean;
  compact?: boolean;
  onNavigate?: () => void;
}) {
  return (
    <Link
      href={href}
      onClick={onNavigate}
      aria-current={selected ? "page" : undefined}
      className={`flex min-w-0 items-center gap-2 px-3 py-2 text-sm font-medium transition-colors ${
        selected ? "bg-panel-muted text-action-strong" : "text-muted hover:bg-panel-muted hover:text-foreground"
      } ${compact ? "justify-center px-2" : ""}`}
      title={compact ? label : undefined}
    >
      {icon ? <NavigationIcon icon={icon} /> : null}
      <span className={compact ? "sr-only" : "truncate"}>{label}</span>
    </Link>
  );
}

export default function AppNav({ active }: { active?: NavKey }) {
  const { t, i18n } = useTranslation();
  const pathname = usePathname();
  const [mobileOpen, setMobileOpen] = useState(false);
  const located = locate(pathname);
  const sectionKey = located.section?.key ?? (active ? LEGACY_SECTION[active] : undefined);
  const section = NAV_SECTIONS.find((item) => item.key === sectionKey) ?? null;
  const settingsSelected = pathname === SETTINGS_HREF;

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") setMobileOpen(false);
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, []);

  return (
    <header className="sticky inset-x-0 top-0 z-50 w-full border-b border-line bg-panel">
      <nav lang={i18n.language} aria-label="Primary" className="workspace-frame">
        <div className="flex min-w-0 items-center gap-3 py-2">
          <Link href="/" className="mr-auto inline-flex shrink-0 items-center gap-2 text-base font-semibold text-foreground transition-colors hover:text-action xl:mr-3">
            <ProphetMark className="h-6 w-6" />
            Prophet
          </Link>

          <div className="hidden min-w-0 flex-1 items-center gap-1 xl:flex">
            {NAV_SECTIONS.map((item) => (
              <NavLink
                key={item.key}
                href={item.pages[0].href}
                label={t(item.label)}
                icon={SECTION_ICONS[item.key]}
                selected={sectionKey === item.key}
              />
            ))}
            <div className="ml-auto border-l border-line pl-2">
              <NavLink href={SETTINGS_HREF} label={t("Settings")} icon={Settings} selected={settingsSelected} compact />
            </div>
          </div>

          <WorkspaceWorkIndicator />

          <button
            type="button"
            onClick={() => window.location.reload()}
            className="hidden h-9 w-9 shrink-0 items-center justify-center border border-line text-muted transition-colors hover:border-line-strong hover:bg-panel-muted hover:text-foreground sm:inline-flex xl:hidden"
            aria-label="Refresh current view"
            title="Refresh"
          >
            <RefreshCw className="h-4 w-4" aria-hidden="true" />
          </button>

          <button
            type="button"
            onClick={() => setMobileOpen((current) => !current)}
            className="inline-flex h-9 w-9 shrink-0 items-center justify-center border border-line text-muted transition-colors hover:border-line-strong hover:bg-panel-muted hover:text-foreground xl:hidden"
            aria-label={mobileOpen ? "Close navigation" : "Open navigation"}
            aria-expanded={mobileOpen}
            aria-controls="prophet-mobile-navigation"
          >
            {mobileOpen ? <X className="h-4 w-4" /> : <Menu className="h-4 w-4" />}
          </button>
        </div>

        {section && section.pages.length > 1 ? (
          <div aria-label={t(section.label)} role="navigation" className="-mb-px flex min-w-0 gap-1 overflow-x-auto pb-1">
            {section.pages.map((page) => (
              <NavLink
                key={page.href}
                href={page.href}
                label={t(page.label)}
                selected={located.page?.href === page.href}
              />
            ))}
          </div>
        ) : null}

        {mobileOpen ? (
          <div id="prophet-mobile-navigation" className="border-t border-line py-3 xl:hidden">
            <div className="grid gap-3 sm:grid-cols-3">
              {NAV_SECTIONS.map((item) => (
                <div key={item.key}>
                  <NavLink
                    href={item.pages[0].href}
                    label={t(item.label)}
                    icon={SECTION_ICONS[item.key]}
                    selected={sectionKey === item.key}
                    onNavigate={() => setMobileOpen(false)}
                  />
                  {item.pages.length > 1 ? (
                    <div className="ml-6 space-y-1 border-l border-line pl-2">
                      {item.pages.map((page) => (
                        <NavLink
                          key={page.href}
                          href={page.href}
                          label={t(page.label)}
                          selected={located.page?.href === page.href}
                          onNavigate={() => setMobileOpen(false)}
                        />
                      ))}
                    </div>
                  ) : null}
                </div>
              ))}
            </div>
            <div className="mt-3 border-t border-line pt-3">
              <button type="button" onClick={() => window.location.reload()} className="flex w-full items-center gap-2 px-3 py-2 text-sm text-muted hover:bg-panel-muted hover:text-foreground sm:hidden"><RefreshCw className="h-4 w-4" aria-hidden="true" />Refresh current view</button>
              <NavLink href={SETTINGS_HREF} label={t("Settings")} icon={Settings} selected={settingsSelected} onNavigate={() => setMobileOpen(false)} />
            </div>
          </div>
        ) : null}
      </nav>
    </header>
  );
}
