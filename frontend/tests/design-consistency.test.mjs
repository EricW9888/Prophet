// Design consistency ratchet. Two counts may only go down:
//  - raw Tailwind palette colors (slate-500, sky-600, ...) instead of the theme
//    tokens in globals.css (text-muted, border-line, bg-panel, text-action,
//    text-positive, text-negative, ...). Raw colors drift between pages and
//    ignore the theme. Fluent 2: semantic colors carry meaning, never decoration.
//  - `uppercase` labels. Fluent 2 typography: "Don't use all caps ... it's
//    difficult to read"; use sentence case.
// When you remove some, lower the baseline in the same change so it holds.
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const SRC = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "src");
const BASELINE = { rawPalette: 2625, uppercase: 228 };

const RAW_PALETTE = /\b(?:text|bg|border|divide|ring|from|to|via|fill|stroke|outline|accent|placeholder)-(?:slate|sky|red|rose|emerald|amber|blue|gray|green|zinc|neutral|indigo|yellow|orange|teal|cyan|violet|purple|pink|lime|stone|fuchsia)-\d{2,3}\b/g;
// `first-letter:uppercase` is how sentence case is applied, so it does not count.
const UPPERCASE = /(?<!first-letter:)\buppercase\b/g;

function files(dir) {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) return files(full);
    return /\.(tsx|ts)$/.test(entry.name) ? [full] : [];
  });
}

function count(pattern, extensions) {
  const perFile = [];
  let total = 0;
  for (const file of files(SRC)) {
    if (!extensions.test(file)) continue;
    const n = (fs.readFileSync(file, "utf8").match(pattern) ?? []).length;
    if (n) perFile.push([path.relative(SRC, file), n]);
    total += n;
  }
  perFile.sort((a, b) => b[1] - a[1]);
  return { total, top: perFile.slice(0, 5).map(([file, n]) => `${file}: ${n}`).join(", ") };
}

test("raw palette colors do not grow; use the theme tokens", () => {
  const { total, top } = count(RAW_PALETTE, /\.(tsx|ts)$/);
  assert.ok(
    total <= BASELINE.rawPalette,
    `${total} raw palette color classes (baseline ${BASELINE.rawPalette}). Use theme tokens such as text-muted, text-foreground, border-line, bg-panel, bg-panel-muted, text-action, text-positive, text-negative. Most: ${top}`,
  );
});

test("uppercase labels do not grow; use sentence case", () => {
  const { total, top } = count(UPPERCASE, /\.tsx$/);
  assert.ok(total <= BASELINE.uppercase, `${total} uppercase labels (baseline ${BASELINE.uppercase}). Use sentence case. Most: ${top}`);
});
