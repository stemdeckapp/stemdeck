// Which suites a diff needs: the skip rule for the browser suite and pytest.
//
// Run:  node tests/js/changed-areas.test.mjs

import { classify } from "../../scripts/changed-areas.mjs";
import { parseArgs, parseSummary } from "../../scripts/test-e2e-parallel.mjs";

let pass = 0;
let fail = 0;
const check = (name, cond, detail = "") => {
  if (cond) {
    pass++;
    console.log(`PASS  ${name}`);
  } else {
    fail++;
    console.log(`FAIL  ${name}${detail ? "  -- " + detail : ""}`);
  }
};
const flags = (files) => Object.entries(classify(files)).filter(([, v]) => v).map(([k]) => k).join(",");

check("docs only needs nothing", flags(["README.md", "docs/plans/x.md", "LICENSE"]) === "");
check("rust only", flags(["desktop/src-tauri/src/main.rs"]) === "rust");
check("pipeline python is pytest only", flags(["app/pipeline/runner.py", "tests/test_pipeline_runner.py"]) === "pytest");
check("api python needs browser and pytest", flags(["app/api/jobs.py"]) === "browser,pytest");
check("main.py needs browser", classify(["app/main.py"]).browser);
check("core needs browser", classify(["app/core/config.py"]).browser);
check("static js needs browser and node", flags(["static/js/eq.js"]) === "browser,node");
check("i18n table flags i18n", classify(["static/js/i18n.js"]).i18n);
check("e2e spec needs browser", flags(["tests/e2e/search.spec.mjs"]) === "browser");
check("node test needs node only", flags(["tests/js/vu-scale.test.mjs"]) === "node");
const BS = String.fromCharCode(92);
check("windows separators", classify(["static" + BS + "css" + BS + "app.css"]).browser);
check("empty diff needs nothing", flags([]) === "");

const o = parseArgs(["--shards", "2", "--base-port=9000", "tests/e2e/search.spec.mjs"]);
check("parallel args", o.shards === 2 && o.basePort === 9000 && o.extra.length === 1);
check("parallel defaults", parseArgs([]).shards === 3 && parseArgs([]).basePort === 8601);
let threw = false;
try { parseArgs(["--shards", "0"]); } catch { threw = true; }
check("zero shards rejected", threw);
const s = parseSummary("  1 failed\n    [chromium] > a.spec.mjs:3 > x\n  2 flaky\n    [chromium] > b.spec.mjs:4 > y\n  8 skipped\n  539 passed (10.0m)\n");
check("summary counts", s.passed === 539 && s.skipped === 8 && s.failed === 1 && s.flaky === 2);
check("summary names", s.failedNames.length === 1 && s.flakyNames.length === 1);

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
