"use client";

import {
  Activity,
  BookOpenText,
  BriefcaseBusiness,
  ChevronDown,
  ClipboardCheck,
  FlaskConical,
  History,
  Lightbulb,
  Menu,
  Network,
  Radar,
  Scale,
  Search,
  Settings,
  ShieldAlert,
  TableProperties,
  X,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { type ComponentType, type SVGProps, useEffect, useRef, useState } from "react";

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

type NavItem = {
  href: string;
  label: string;
  key: NavKey;
  icon: NavIcon;
};

const primaryItems: readonly NavItem[] = [
  { href: "/", label: "Portfolio", key: "portfolio", icon: BriefcaseBusiness },
  { href: "/chat", label: "Research", key: "research", icon: Search },
  { href: "/timeline", label: "Monitor", key: "timeline", icon: Radar },
  { href: "/graph", label: "Knowledge", key: "knowledge", icon: Network },
  { href: "/verification", label: "Review", key: "review", icon: ClipboardCheck },
  { href: "/shadow", label: "Simulate", key: "experiments", icon: FlaskConical },
] as const;

const secondaryGroups: ReadonlyArray<{ label: string; items: readonly NavItem[] }> = [
  {
    label: "Portfolio records",
    items: [
      { href: "/positions", label: "Positions", key: "positions", icon: TableProperties },
      { href: "/history", label: "History", key: "history", icon: History },
      { href: "/risk", label: "Risk", key: "risk", icon: ShieldAlert },
      { href: "/decisions", label: "Decisions", key: "decisions", icon: Scale },
    ],
  },
  {
    label: "Research library",
    items: [
      { href: "/sources", label: "Sources", key: "sources", icon: BookOpenText },
      { href: "/opportunities", label: "Ideas", key: "opportunities", icon: Lightbulb },
    ],
  },
  {
    label: "System",
    items: [
      { href: "/activity", label: "Activity", key: "activity", icon: Activity },
    ],
  },
] as const;

const settingsItem: NavItem = {
  href: "/settings",
  label: "Settings",
  key: "settings",
  icon: Settings,
} as const;

const allItems = [
  ...primaryItems,
  ...secondaryGroups.flatMap((group) => group.items),
  settingsItem,
];

function routeMatches(href: string, pathname: string) {
  if (href === "/") {
    return pathname === "/";
  }
  if (href === "/sources") {
    return pathname === href || pathname.startsWith("/sources/");
  }
  return pathname === href;
}

function NavLink({
  href,
  label,
  icon: Icon,
  selected,
  compact = false,
  onNavigate,
}: {
  href: string;
  label: string;
  icon: NavIcon;
  selected: boolean;
  compact?: boolean;
  onNavigate?: () => void;
}) {
  return (
    <Link
      href={href}
      onClick={onNavigate}
      aria-current={selected ? "page" : undefined}
      className={`flex min-w-0 items-center gap-2 rounded px-3 py-2 text-sm font-medium transition-colors ${
        selected
          ? "bg-slate-900 text-white dark:bg-slate-100 dark:text-slate-950"
          : "text-slate-600 hover:bg-slate-100 hover:text-slate-950 dark:text-slate-300 dark:hover:bg-slate-900 dark:hover:text-white"
      } ${compact ? "justify-center px-2" : ""}`}
      title={compact ? label : undefined}
    >
      <Icon className="h-4 w-4 shrink-0" aria-hidden="true" />
      <span className={compact ? "sr-only" : "truncate"}>{label}</span>
    </Link>
  );
}

export default function AppNav({ active }: { active: NavKey }) {
  const pathname = usePathname();
  const [mobileOpen, setMobileOpen] = useState(false);
  const [moreOpen, setMoreOpen] = useState(false);
  const moreRef = useRef<HTMLDivElement>(null);
  const activeKey = allItems.find((item) => routeMatches(item.href, pathname))?.key ?? active;
  const secondaryActive = secondaryGroups.some((group) =>
    group.items.some((item) => item.key === activeKey),
  );

  useEffect(() => {
    function onPointerDown(event: MouseEvent) {
      if (moreRef.current && !moreRef.current.contains(event.target as Node)) {
        setMoreOpen(false);
      }
    }

    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setMobileOpen(false);
        setMoreOpen(false);
      }
    }

    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, []);

  return (
    <header className="sticky top-0 z-50 w-full border-b border-line bg-panel/95 backdrop-blur-md">
      <nav aria-label="Primary" className="mx-auto w-full max-w-[1440px] px-4 sm:px-6 lg:px-8">
        <div className="flex min-w-0 items-center gap-3 py-2">
          <Link
            href="/"
            className="mr-auto shrink-0 text-base font-semibold text-foreground transition-colors hover:text-action xl:mr-3"
          >
            Prophet
          </Link>

          <div className="hidden min-w-0 flex-1 items-center gap-1 xl:flex">
            {primaryItems.map((item) => (
              <NavLink
                key={item.key}
                href={item.href}
                label={item.label}
                icon={item.icon}
                selected={activeKey === item.key}
              />
            ))}

            <div ref={moreRef} className="relative">
              <button
                type="button"
                onClick={() => setMoreOpen((current) => !current)}
                className={`flex items-center gap-2 rounded px-3 py-2 text-sm font-medium transition-colors ${
                  secondaryActive
                    ? "bg-slate-900 text-white dark:bg-slate-100 dark:text-slate-950"
                    : "text-slate-600 hover:bg-slate-100 hover:text-slate-950 dark:text-slate-300 dark:hover:bg-slate-900 dark:hover:text-white"
                }`}
                aria-expanded={moreOpen}
                aria-controls="prophet-more-navigation"
                aria-haspopup="true"
              >
                More
                <ChevronDown
                  className={`h-4 w-4 transition-transform ${moreOpen ? "rotate-180" : ""}`}
                  aria-hidden="true"
                />
              </button>

              {moreOpen ? (
                <div
                  id="prophet-more-navigation"
                  className="absolute right-0 top-[calc(100%+0.5rem)] w-72 rounded-lg border border-line bg-panel p-2 shadow-xl shadow-slate-950/10 dark:shadow-black/30"
                >
                  {secondaryGroups.map((group, index) => (
                    <div
                      key={group.label}
                      className={index === 0 ? "" : "mt-2 border-t border-line pt-2"}
                    >
                      <p className="px-3 pb-1 pt-1 text-[11px] font-semibold uppercase text-muted">
                        {group.label}
                      </p>
                      <div className="grid grid-cols-2 gap-1">
                        {group.items.map((item) => (
                          <NavLink
                            key={item.key}
                            href={item.href}
                            label={item.label}
                            icon={item.icon}
                            selected={activeKey === item.key}
                            onNavigate={() => setMoreOpen(false)}
                          />
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              ) : null}
            </div>

            <div className="ml-auto border-l border-line pl-2">
              <NavLink
                href={settingsItem.href}
                label={settingsItem.label}
                icon={settingsItem.icon}
                selected={activeKey === settingsItem.key}
                compact
              />
            </div>
          </div>

          <button
            type="button"
            onClick={() => setMobileOpen((current) => !current)}
            className="inline-flex h-9 w-9 items-center justify-center rounded border border-line text-muted transition-colors hover:border-line-strong hover:bg-panel-muted hover:text-foreground xl:hidden"
            aria-label={mobileOpen ? "Close navigation" : "Open navigation"}
            aria-expanded={mobileOpen}
            aria-controls="prophet-mobile-navigation"
          >
            {mobileOpen ? <X className="h-4 w-4" /> : <Menu className="h-4 w-4" />}
          </button>
        </div>

        {mobileOpen ? (
          <div id="prophet-mobile-navigation" className="border-t border-line py-3 xl:hidden">
            <div className="grid grid-cols-2 gap-1 sm:grid-cols-3">
              {primaryItems.map((item) => (
                <NavLink
                  key={item.key}
                  href={item.href}
                  label={item.label}
                  icon={item.icon}
                  selected={activeKey === item.key}
                  onNavigate={() => setMobileOpen(false)}
                />
              ))}
            </div>
            <div className="mt-3 grid gap-3 border-t border-line pt-3 sm:grid-cols-3">
              {secondaryGroups.map((group) => (
                <div key={group.label}>
                  <p className="px-3 pb-1 text-[11px] font-semibold uppercase text-muted">
                    {group.label}
                  </p>
                  <div className="space-y-1">
                    {group.items.map((item) => (
                      <NavLink
                        key={item.key}
                        href={item.href}
                        label={item.label}
                        icon={item.icon}
                        selected={activeKey === item.key}
                        onNavigate={() => setMobileOpen(false)}
                      />
                    ))}
                  </div>
                </div>
              ))}
            </div>
            <div className="mt-3 border-t border-line pt-3">
              <NavLink
                href={settingsItem.href}
                label={settingsItem.label}
                icon={settingsItem.icon}
                selected={activeKey === settingsItem.key}
                onNavigate={() => setMobileOpen(false)}
              />
            </div>
          </div>
        ) : null}

        <p className="border-t border-line py-1.5 text-[11px] leading-4 text-muted">
          AI can be incomplete or wrong. Verify cited evidence and reconcile portfolio data before acting.
        </p>
      </nav>
    </header>
  );
}
