"use client";

import Link from "next/link";
import { Menu, X } from "lucide-react";
import { useState } from "react";

const navItems = [
  { href: "/", label: "Portfolio", key: "portfolio" },
  { href: "/history", label: "History", key: "history" },
  { href: "/chat", label: "Research", key: "research" },
  { href: "/timeline", label: "Feed", key: "timeline" },
  { href: "/graph", label: "Knowledge", key: "knowledge" },
  { href: "/sources", label: "Sources", key: "sources" },
  { href: "/activity", label: "Activity", key: "activity" },
  { href: "/risk", label: "Risk", key: "risk" },
  { href: "/opportunities", label: "Ideas", key: "opportunities" },
  { href: "/verification", label: "Review", key: "review" },
  { href: "/shadow", label: "Experiments", key: "experiments" },
  { href: "/settings", label: "Settings", key: "settings" },
] as const;

export type NavKey = (typeof navItems)[number]["key"];

export default function AppNav({ active }: { active: NavKey }) {
  const [mobileOpen, setMobileOpen] = useState(false);

  return (
    <nav className="sticky top-0 z-50 w-full border-b border-line bg-panel/95 backdrop-blur-md">
      <div className="mx-auto w-full max-w-[1440px] px-4 sm:px-6 lg:px-8">
        <div className="flex min-w-0 items-center justify-between gap-4 py-2 md:py-2.5">
          <Link
            href="/"
            className="shrink-0 text-base font-semibold text-foreground transition-colors hover:text-action"
          >
            Prophet
          </Link>

          <button
            type="button"
            onClick={() => setMobileOpen(current => !current)}
            className="inline-flex h-9 w-9 items-center justify-center rounded border border-slate-200 text-slate-600 transition-colors hover:bg-slate-50 hover:text-slate-950 xl:hidden dark:border-slate-800 dark:text-slate-300 dark:hover:bg-slate-900 dark:hover:text-white"
            aria-label={mobileOpen ? "Close navigation" : "Open navigation"}
            aria-expanded={mobileOpen}
          >
            {mobileOpen ? <X className="h-4 w-4" /> : <Menu className="h-4 w-4" />}
          </button>

          <div className="hidden min-w-0 items-center gap-1 whitespace-nowrap xl:flex">
            {navItems.map((item) => (
              <Link
                key={item.key}
                href={item.href}
                aria-current={active === item.key ? "page" : undefined}
                className={`shrink-0 border-b-2 px-3 py-2 text-sm font-medium transition-colors ${
                  active === item.key
                    ? "border-slate-900 text-slate-950 dark:border-slate-100 dark:text-slate-100"
                    : "border-transparent text-slate-500 hover:border-slate-300 hover:text-slate-900 dark:text-slate-400 dark:hover:border-slate-700 dark:hover:text-slate-100"
                }`}
              >
                {item.label}
              </Link>
            ))}
          </div>
        </div>

        {mobileOpen ? (
          <div className="grid grid-cols-2 gap-1 border-t border-slate-100 py-2 xl:hidden dark:border-slate-800">
            {navItems.map(item => (
              <Link
                key={item.key}
                href={item.href}
                onClick={() => setMobileOpen(false)}
                aria-current={active === item.key ? "page" : undefined}
                className={`rounded px-3 py-2 text-sm font-medium transition-colors ${
                  active === item.key
                    ? "bg-slate-900 text-white dark:bg-slate-100 dark:text-slate-950"
                    : "text-slate-600 hover:bg-slate-100 hover:text-slate-950 dark:text-slate-300 dark:hover:bg-slate-900 dark:hover:text-white"
                }`}
              >
                {item.label}
              </Link>
            ))}
          </div>
        ) : null}

        <p className="border-t border-line py-1.5 text-[11px] leading-4 text-muted">
          AI-assisted research can be incomplete or wrong. Verify sources and reconcile portfolio data before acting.
        </p>
      </div>
    </nav>
  );
}
