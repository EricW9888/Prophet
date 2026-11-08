import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { test } from "node:test";
import { clearCaches, parseForESLint } from "@typescript-eslint/parser";

test("patched glob dependency preserves project-service allow and deny decisions", () => {
  const temporaryRoot = path.resolve(tmpdir());
  const project = mkdtempSync(path.join(temporaryRoot, "prophet-eslint-compat-"));
  try {
    mkdirSync(path.join(project, "src"));
    writeFileSync(path.join(project, "src", "example.ts"), "export const value = 1;");
    writeFileSync(path.join(project, "tsconfig.json"), JSON.stringify({ compilerOptions: { strict: true }, include: ["src/**/*.ts"] }));
    const filePath = path.join(project, ".config.js");
    writeFileSync(filePath, "const value = 1;");
    const parsed = parseForESLint("const value = 1;", { filePath, tsconfigRootDir: project, projectService: { allowDefaultProject: ["*.js"] } });
    assert.equal(parsed.ast.body[0].type, "VariableDeclaration");
    assert.ok(parsed.services.program);
    clearCaches();
    assert.throws(() => parseForESLint("const value = 1;", { filePath, tsconfigRootDir: project, projectService: { allowDefaultProject: ["allowed.js"] } }), /not found by the project service/);
  } finally {
    clearCaches();
    assert.equal(path.dirname(path.resolve(project)), temporaryRoot);
    assert.ok(path.basename(project).startsWith("prophet-eslint-compat-"));
    rmSync(project, { recursive: true, force: true });
  }
});
