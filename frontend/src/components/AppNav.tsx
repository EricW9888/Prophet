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
    <nav className="sticky top-0 z-50 w-full border-b border-gray-200 bg-white/95 backdrop-blur-md dark:border-gray-800 dark:bg-[#0a0a0a]/95">
      <div className="mx-auto w-full max-w-[1440px] px-4 sm:px-6 lg:px-8">
        <div className="flex min-w-0 items-center justify-between gap-4 py-2 md:py-2.5">
          <div className="flex shrink-0 items-center gap-2.5">
            <div className="flex h-8 w-8 items-center justify-center rounded border border-gray-900 bg-gray-900 dark:border-gray-100 dark:bg-gray-100">
              <span className="text-xs font-semibold text-white dark:text-gray-950">PR</span>
            </div>
            <div>
              <p className="text-base font-semibold">Prophet</p>
            </div>
          </div>

          <button
            type="button"
            onClick={() => setMobileOpen(current => !current)}
            className="inline-flex h-9 w-9 items-center justify-center rounded border border-gray-200 text-gray-600 transition-colors hover:bg-gray-50 hover:text-gray-950 md:hidden dark:border-gray-800 dark:text-gray-300 dark:hover:bg-gray-900 dark:hover:text-white"
            aria-label={mobileOpen ? "Close navigation" : "Open navigation"}
            aria-expanded={mobileOpen}
          >
            {mobileOpen ? <X className="h-4 w-4" /> : <Menu className="h-4 w-4" />}
          </button>

          <div className="hidden min-w-0 items-center gap-1 whitespace-nowrap md:flex">
            {navItems.map((item) => (
              <Link
                key={item.key}
                href={item.href}
                aria-current={active === item.key ? "page" : undefined}
                className={`shrink-0 border-b-2 px-3 py-2 text-sm font-medium transition-colors ${
                  active === item.key
                    ? "border-gray-900 text-gray-950 dark:border-gray-100 dark:text-gray-100"
                    : "border-transparent text-gray-500 hover:border-gray-300 hover:text-gray-900 dark:text-gray-400 dark:hover:border-gray-700 dark:hover:text-gray-100"
                }`}
              >
                {item.label}
              </Link>
            ))}
          </div>
        </div>

        {mobileOpen ? (
          <div className="grid grid-cols-2 gap-1 border-t border-gray-100 py-2 md:hidden dark:border-gray-800">
            {navItems.map(item => (
              <Link
                key={item.key}
                href={item.href}
                onClick={() => setMobileOpen(false)}
                aria-current={active === item.key ? "page" : undefined}
                className={`rounded px-3 py-2 text-sm font-medium transition-colors ${
                  active === item.key
                    ? "bg-gray-900 text-white dark:bg-gray-100 dark:text-gray-950"
                    : "text-gray-600 hover:bg-gray-100 hover:text-gray-950 dark:text-gray-300 dark:hover:bg-gray-900 dark:hover:text-white"
                }`}
              >
                {item.label}
              </Link>
            ))}
          </div>
        ) : null}
      </div>
    </nav>
  );
}
