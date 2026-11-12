// Which Playwright specs CI runs (scripts/ci/select-e2e.mjs).
//
// Run:  node --test tests/js/select-e2e.test.mjs   (or plain node, as CI does)

import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { globToRegExp, loadMap, select } from "../../scripts/ci/select-e2e.mjs";

const map = loadMap();
const pr = (files, base = "big-release") => select({ event: "pull_request", base, files, map, exists: () => true });
const spec = (name) => `tests/e2e/${name}.spec.mjs`;
const smoke = map.smoke.map(spec);

test("a pull request to main runs everything", () => {
  assert.equal(pr(["static/js/queue.js"], "main").full, true);
});

test("a push runs everything", () => {
  assert.equal(select({ event: "push", base: "", files: [], map }).full, true);
});

test("a diff that could not be computed runs everything", () => {
  assert.equal(pr(null).full, true);
});

test("an empty diff runs the smoke set only", () => {
  const r = pr([]);
  assert.equal(r.full, false);
  assert.deepEqual(r.specs, [...smoke].sort());
});

test("a changed spec is selected", () => {
  const r = pr(["tests/e2e/zoom.spec.mjs"]);
  assert.equal(r.full, false);
  assert.ok(r.specs.includes(spec("zoom")));
});

test("a deleted spec is not passed to playwright", () => {
  const r = select({ event: "pull_request", base: "big-release", files: [spec("gone")], map, exists: () => false });
  assert.equal(r.full, false);
  assert.ok(!r.specs.includes(spec("gone")));
});

test("queue.js selects queue-progress, the untouched spec #806 broke", () => {
  const r = pr(["static/js/queue.js"]);
  assert.equal(r.full, false);
  assert.ok(r.specs.includes(spec("queue-progress")));
});

test("modeUi.js selects modes and help-mode", () => {
  const r = pr(["static/js/modeUi.js"]);
  assert.equal(r.full, false);
  assert.ok(r.specs.includes(spec("modes")));
  assert.ok(r.specs.includes(spec("help-mode")));
});

test("full mode runs everything, whatever the base", () => {
  assert.equal(select({ mode: "full", base: "big-release", files: ["README.md"], map }).full, true);
  assert.equal(select({ mode: "full", base: "", files: [], map }).full, true);
});

test("scoped mode to main still runs everything", () => {
  assert.equal(select({ mode: "scoped", base: "main", files: ["static/js/queue.js"], map, exists: () => true }).full, true);
});

test("scoped mode to another base selects like a pull request", () => {
  const r = select({ mode: "scoped", base: "big-release", files: ["static/js/queue.js"], map, exists: () => true });
  assert.equal(r.full, false);
  assert.ok(r.specs.includes(spec("queue-progress")));
});

const core = map.core.map(spec);
const hasAll = (r, list) => list.every((s) => r.specs.includes(s));

test("a hub module adds the core set, smoke and mapped specs, not everything", () => {
  const r = pr(["static/js/main.js", "static/js/queue.js"]);
  assert.equal(r.full, false);
  assert.equal(r.core, true);
  assert.ok(hasAll(r, core), "core");
  assert.ok(hasAll(r, smoke), "smoke");
  assert.ok(r.specs.includes(spec("queue-remove")), "mapped");
});

test("app/api/jobs.py is not the whole suite", () => {
  const r = pr(["app/api/jobs.py"]);
  assert.equal(r.full, false);
  assert.ok(hasAll(r, core));
});

test("every former shared source file adds core instead of everything", () => {
  for (const f of map.hubs) {
    const file = f.replace("**", "x.py");
    const r = pr([file]);
    assert.equal(r.full, false, f);
    assert.equal(r.core, true, f);
  }
});

test("an unmapped file under static/ or app/ adds the core set", () => {
  for (const f of ["static/js/brandNewModule.js", "app/pipeline/separate.py"]) {
    const r = pr([f]);
    assert.equal(r.full, false, f);
    assert.ok(hasAll(r, core), f);
  }
});

test("a mapped file does not add the core set", () => {
  const r = pr(["static/js/queue.js"]);
  assert.equal(r.core, false);
  assert.ok(!r.specs.includes(spec("boot-failure")));
});

test("uv.lock alone adds core, not everything", () => {
  const r = pr(["uv.lock"]);
  assert.equal(r.full, false);
  assert.ok(hasAll(r, core));
});

test("unmapped e2e code runs everything", () => {
  assert.equal(pr(["tests/e2e/new-helper.mjs"]).full, true);
});

test("a change to how the suite runs runs everything", () => {
  for (const f of ["playwright.config.mjs", "tests/e2e/helpers.mjs", "tests/e2e/serve.sh", "tests/e2e/seed.py",
    "package.json", "package-lock.json", ".github/workflows/ci.yml", ".github/workflows/e2e.yml",
    ".github/workflows/e2e-full.yml", "tests/e2e/spec-map.json", "scripts/ci/select-e2e.mjs"]) {
    assert.equal(pr([f]).full, true, f);
  }
});

test("the core set stays small and does not repeat smoke", () => {
  assert.ok(map.core.length >= 12 && map.core.length <= 20, String(map.core.length));
  for (const s of map.core) assert.ok(!map.smoke.includes(s), s);
});

test("files outside static/, app/ and tests/e2e/ add nothing to the smoke set", () => {
  const r = pr(["README.md", "desktop/src-tauri/src/main.rs", "tests/test_jobs.py"]);
  assert.equal(r.full, false);
  assert.deepEqual(r.specs, [...smoke].sort());
});

test("windows separators are normalised", () => {
  const r = pr(["static\\js\\queue.js"]);
  assert.ok(r.specs.includes(spec("queue-progress")));
});

test("globs: * stays in one folder, ** crosses folders", () => {
  assert.ok(globToRegExp("static/js/eq/**").test("static/js/eq/a/b.js"));
  assert.ok(globToRegExp("app/pipeline/lyrics_*.py").test("app/pipeline/lyrics_sync.py"));
  assert.ok(!globToRegExp("tests/e2e/helpers*").test("tests/e2e/sub/helpers.mjs"));
});

test("every spec named in the map exists", () => {
  const named = new Set([...map.smoke, ...map.core, ...map.rules.flatMap((r) => r.specs)]);
  for (const name of named) assert.ok(existsSync(new URL(`../e2e/${name}.spec.mjs`, import.meta.url)), name);
});
