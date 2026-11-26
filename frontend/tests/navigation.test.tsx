import assert from "node:assert/strict";
import test from "node:test";

import { languageOptions, languageResources } from "../src/lib/language-resources";
import { locate, NAV_SECTIONS, navigationLabels } from "../src/lib/navigation";

test("every route lands in exactly one section, detail pages included", () => {
  const cases: Record<string, [string, string]> = {
    "/": ["portfolio", "/"],
    "/positions": ["portfolio", "/positions"],
    "/history/receipt/statement/abc": ["portfolio", "/history"],
    "/decisions": ["portfolio", "/decisions"],
    "/chat": ["chat", "/chat"],
    "/sources/evidence/abc": ["research", "/sources"],
    "/profile/abc": ["research", "/graph"],
    "/opportunities/patterns": ["research", "/opportunities"],
    "/timeline": ["monitor", "/timeline"],
    "/activity": ["monitor", "/activity"],
    "/verification": ["review", "/verification"],
    "/shadow": ["simulate", "/shadow"],
  };
  for (const [path, [section, page]] of Object.entries(cases)) {
    const found = locate(path);
    assert.equal(found.section?.key, section, path);
    assert.equal(found.page?.href, page, path);
  }
  assert.equal(locate("/settings").section, null);
});

test("no hidden menu: at most six sections, and every page is reachable from one", () => {
  assert.ok(NAV_SECTIONS.length <= 6);
  const hrefs = NAV_SECTIONS.flatMap((section) => section.pages.map((page) => page.href));
  assert.equal(new Set(hrefs).size, hrefs.length, "each page has one home");
});

test("every navigation label is translated in every supported language", () => {
  for (const language of languageOptions) {
    const resources = languageResources[language.code].translation as Record<string, string>;
    for (const label of navigationLabels()) assert.ok(resources[label]?.trim(), `${language.code}: ${label}`);
  }
});
