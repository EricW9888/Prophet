/**
 * Prophet's navigation model: six sections whose labels name what is inside,
 * each with its own pages, and no hidden "More" menu. Detail routes belong to
 * the section of their parent page, so the location is always visible.
 */

export type SectionKey = "portfolio" | "chat" | "research" | "monitor" | "review" | "simulate";

export type SectionPage = {
  href: string;
  label: string;
  /** Extra path prefixes that belong to this page (detail routes). */
  prefixes?: readonly string[];
};

export type NavSection = {
  key: SectionKey;
  label: string;
  pages: readonly SectionPage[];
};

export const NAV_SECTIONS: readonly NavSection[] = [
  {
    key: "portfolio",
    label: "Portfolio",
    pages: [
      { href: "/", label: "Overview" },
      { href: "/positions", label: "Positions" },
      { href: "/history", label: "History", prefixes: ["/history/"] },
      { href: "/risk", label: "Risk" },
      { href: "/decisions", label: "Decisions" },
    ],
  },
  { key: "chat", label: "Chat", pages: [{ href: "/chat", label: "Chat" }] },
  {
    key: "research",
    label: "Research",
    pages: [
      { href: "/sources", label: "Sources", prefixes: ["/sources/"] },
      { href: "/graph", label: "Knowledge", prefixes: ["/profile/"] },
      { href: "/opportunities", label: "Ideas", prefixes: ["/opportunities/"] },
    ],
  },
  {
    key: "monitor",
    label: "Monitor",
    pages: [
      { href: "/timeline", label: "Feed" },
      { href: "/activity", label: "Activity" },
    ],
  },
  { key: "review", label: "Review", pages: [{ href: "/verification", label: "Review" }] },
  { key: "simulate", label: "Simulate", pages: [{ href: "/shadow", label: "Simulate" }] },
];

export const SETTINGS_HREF = "/settings";

function pageMatches(page: SectionPage, pathname: string): boolean {
  if (page.href === "/") return pathname === "/";
  return pathname === page.href || (page.prefixes ?? []).some((prefix) => pathname.startsWith(prefix));
}

/** The section and page for a path, or nulls (for example on Settings). */
export function locate(pathname: string): { section: NavSection | null; page: SectionPage | null } {
  for (const section of NAV_SECTIONS) {
    const page = section.pages.find((candidate) => pageMatches(candidate, pathname));
    if (page) return { section, page };
  }
  return { section: null, page: null };
}

/** Every label the navigation can show, so translations can be checked. */
export function navigationLabels(): string[] {
  return [...new Set(NAV_SECTIONS.flatMap((section) => [section.label, ...section.pages.map((page) => page.label)]).concat("Settings"))];
}
