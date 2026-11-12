#!/usr/bin/env node
// Which test suites does a change need? Prints JSON, for example
//   {"base":"big-release","files":4,"browser":true,"pytest":false,"rust":false,"node":true,"i18n":true}
//
//   node scripts/changed-areas.mjs [--base big-release] [--files a b c]
//
// The files are the diff of HEAD against the merge base with <base>, plus
// uncommitted and untracked changes. With --files the list is taken as given
// (used by the tests, and handy for "what if I touch X").
//
// The rule (docs/plans/0.21.0/30-overnight-brief.md, "Test rule update"):
//   browser  static/, templates/, tests/e2e/, playwright.config.mjs, the backend
//            routes the specs hit (app/api/, app/main.py, app/core/), package files
//   pytest   any .py file, pyproject.toml, uv.lock, tests/ python
//   rust     desktop/src-tauri/
//   node     static/js, tests/js, scripts/*.mjs, package.json
//   i18n     static/js/i18n.js
// Pure docs, licence and metadata change nothing here: every flag is false.

import { execFileSync } from "node:child_process";
import { pathToFileURL } from "node:url";

export function classify(files) {
  const f = files.map((p) => p.split(String.fromCharCode(92)).join("/"));
  const any = (test) => f.some(test);
  const under = (...prefixes) => (p) => prefixes.some((x) => p.startsWith(x));
  return {
    browser: any(
      (p) =>
        under("static/", "templates/", "tests/e2e/", "app/api/", "app/core/")(p) ||
        ["playwright.config.mjs", "app/main.py", "package.json", "package-lock.json"].includes(p),
    ),
    pytest: any((p) => p.endsWith(".py") || ["pyproject.toml", "uv.lock"].includes(p)),
    rust: any(under("desktop/src-tauri/")),
    node: any((p) => under("static/js/", "tests/js/")(p) || /^scripts\/.*\.mjs$/.test(p) || p === "package.json"),
    i18n: any((p) => p === "static/js/i18n.js"),
  };
}

function git(args) {
  return execFileSync("git", args, { encoding: "utf8" }).split(/\r?\n/).filter(Boolean);
}

export function changedFiles(base) {
  const mb = git(["merge-base", base, "HEAD"])[0];
  const committed = git(["diff", "--name-only", `${mb}..HEAD`]);
  const dirty = git(["diff", "--name-only", "HEAD"]);
  const untracked = git(["ls-files", "--others", "--exclude-standard"]);
  return [...new Set([...committed, ...dirty, ...untracked])];
}

function main() {
  const argv = process.argv.slice(2);
  let base = "big-release";
  let files = null;
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === "--base") base = argv[++i];
    else if (argv[i] === "--files") files = argv.slice(i + 1);
  }
  const list = files ?? changedFiles(base);
  console.log(JSON.stringify({ base, files: list.length, ...classify(list) }));
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) main();
