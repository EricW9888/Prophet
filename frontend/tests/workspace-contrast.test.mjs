import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const css = readFileSync(new URL("../src/app/globals.css", import.meta.url), "utf8");
const roots = [...css.matchAll(/:root\s*\{([^}]+)\}/g)].map(match => Object.fromEntries(
  [...match[1].matchAll(/--([\w-]+):\s*(#[\da-f]{6})/gi)].map(([, key, value]) => [key, value])
));
// WCAG relative luminance, independent of the selected palette.
function luminance(hex) {
  const linear = hex.slice(1).match(/../g).map(channel => parseInt(channel, 16) / 255)
    .map(channel => channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4);
  return linear[0] * 0.2126 + linear[1] * 0.7152 + linear[2] * 0.0722;
}
for (const [index, colors] of roots.entries()) {
  test(`${index ? "dark" : "light"} workspace text tokens meet 4.5:1 on workspace surfaces`, () => {
    for (const foreground of ["foreground", "muted", "action", "action-strong", "positive", "negative"]) {
      for (const background of ["background", "panel", "panel-muted"]) {
        const values = [luminance(colors[foreground]), luminance(colors[background])].sort((a, b) => b - a);
        const ratio = (values[0] + 0.05) / (values[1] + 0.05);
        assert.ok(ratio >= 4.5, `${foreground} on ${background}: ${ratio.toFixed(2)}:1`);
      }
    }
  });
}
