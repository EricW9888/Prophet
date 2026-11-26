import assert from "node:assert/strict";
import test from "node:test";

import { browseKind, sortBrowseNodes } from "../src/components/GraphNodeNavigator";

const node = (label: string, node_type: string, degree?: number, subtitle?: string) =>
  ({ key: `${node_type}:${label}`, id: label, node_type, label, degree, subtitle, is_root: false }) as never;

test("library lists subjects first, most connected first by default, ties by name", () => {
  const nodes = [node("Zeta Co", "entity", 2), node("Alpha Co", "entity", 30), node("A claim", "claim", 99), node("Beta Co", "entity", 30)];
  assert.deepEqual(sortBrowseNodes(nodes, "connected", true).map((n: { label: string }) => n.label), ["Alpha Co", "Beta Co", "Zeta Co", "A claim"]);
  assert.deepEqual(sortBrowseNodes(nodes, "name", true).map((n: { label: string }) => n.label), ["Alpha Co", "Beta Co", "Zeta Co", "A claim"].sort((a, b) => (a === "A claim" ? 1 : b === "A claim" ? -1 : a.localeCompare(b))));
});

test("rows name the specific kind instead of repeating Entity", () => {
  assert.equal(browseKind(node("x", "entity", 1, "company")), "Company");
  assert.equal(browseKind(node("x", "entity", 1, "investment_company")), "Investment Company");
  assert.equal(browseKind(node("x", "entity", 1, "aircraft configuration program")), "Entity");
  assert.equal(browseKind(node("x", "theme")), "Theme");
});
