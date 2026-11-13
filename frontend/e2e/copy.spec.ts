import { expect, test } from "@playwright/test";
import { readdirSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";
import ts from "typescript";

// Owner copy rule: no em dash, en dash or spaced hyphen ("a - b") in UI text. This scans every string literal, template
// fragment and JSX text under src/ (comments are not visible text and are ignored). Generated API types are skipped.
// Fix a failure by rewording ("care-gap" is allowed inside identifiers, but prose uses plain words); for text that comes
// from the backend, render it through displayText() from src/lib/display.ts.
const SRC = join(process.cwd(), "src");
const SKIP = new Set(["api-types.ts"]);
const BAD = /[–—]| - /;

function files(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
    const p = join(dir, e.name);
    if (e.isDirectory()) return files(p);
    return /\.tsx?$/.test(e.name) && !SKIP.has(e.name) ? [p] : [];
  });
}

function offenders(path: string): string[] {
  const text = readFileSync(path, "utf8");
  const sf = ts.createSourceFile(path, text, ts.ScriptTarget.Latest, true, path.endsWith("x") ? ts.ScriptKind.TSX : ts.ScriptKind.TS);
  const out: string[] = [];
  const check = (node: ts.Node, value: string) => {
    if (BAD.test(value)) {
      const { line } = sf.getLineAndCharacterOfPosition(node.getStart());
      out.push(`${relative(process.cwd(), path)}:${line + 1}: ${JSON.stringify(value.trim().slice(0, 80))}`);
    }
  };
  const visit = (node: ts.Node) => {
    if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) check(node, node.text);
    else if (ts.isTemplateHead(node) || ts.isTemplateMiddle(node) || ts.isTemplateTail(node)) check(node, node.text);
    else if (ts.isJsxText(node)) check(node, node.text);
    ts.forEachChild(node, visit);
  };
  visit(sf);
  return out;
}

test("UI text contains no em dash, en dash or spaced hyphen", () => {
  const bad = files(SRC).flatMap(offenders);
  expect(bad, `Found dashes in UI text:\n${bad.join("\n")}`).toEqual([]);
});
