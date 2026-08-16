// The pre-filled bug-report URL (notification centre → "Report on GitHub").
//
// Two classes of bug live here and neither is visible by eye:
//
//  1. GitHub issue-form dropdowns prefill only on an EXACT match with an option
//     string. "macOS" instead of "macOS (Apple Silicon)" is silently dropped --
//     the form opens with an empty OS field and nobody notices until a reporter
//     leaves it blank. These strings must track .github/ISSUE_TEMPLATE/
//     bug_report.yml.
//  2. The report must never carry the track title or source URL. That is a
//     product decision (issues are public), and a regression would leak it
//     quietly, once per report, forever.
//
// Run:  node tests/js/report-url.test.mjs

import { buildReportUrl, osOption, installOption } from "../../static/js/notifications.js";

let pass = 0,
  fail = 0;
const check = (name, cond, detail = "") => {
  if (cond) {
    pass++;
    console.log(`PASS  ${name}`);
  } else {
    fail++;
    console.log(`FAIL  ${name}${detail ? "  -- " + detail : ""}`);
  }
};

const params = (url) => new URL(url).searchParams;

// The exact option strings from bug_report.yml. If the template changes, this
// list changes with it -- that is the point.
const OS_OPTIONS = ["macOS (Apple Silicon)", "macOS (Intel)", "Windows", "Linux", "Other"];
const INSTALL_OPTIONS = ["macOS DMG", "Windows ZIP", "Linux tar.gz", "From source", "Docker / self-hosted"];

// ─── dropdown mapping ───

for (const [target, expected] of [
  [{ os: "macos", arch: "arm64" }, "macOS (Apple Silicon)"],
  [{ os: "macos", arch: "x64" }, "macOS (Intel)"],
  [{ os: "windows", arch: "x64" }, "Windows"],
  [{ os: "linux", arch: "x64" }, "Linux"],
  [{ os: "freebsd", arch: "x64" }, "Other"],
  [null, "Other"],
]) {
  const got = osOption(target);
  check(`os: ${JSON.stringify(target)} -> ${expected}`, got === expected, got);
  check(`os option is one the form offers: ${got}`, OS_OPTIONS.includes(got));
}

for (const [target, desktop, expected] of [
  [{ os: "macos", arch: "arm64" }, true, "macOS DMG"],
  [{ os: "windows", arch: "x64" }, true, "Windows ZIP"],
  [{ os: "linux", arch: "x64" }, true, "Linux tar.gz"],
  [{ os: "freebsd", arch: "x64" }, true, "From source"],
  // No Tauri means the UI is served by a backend the user runs themselves.
  [{ os: "linux", arch: "x64" }, false, "Docker / self-hosted"],
]) {
  const got = installOption(target, desktop);
  check(`install: ${target.os}/${desktop ? "desktop" : "served"} -> ${expected}`, got === expected, got);
  check(`install option is one the form offers: ${got}`, INSTALL_OPTIONS.includes(got));
}

// ─── a realistic report ───

const RECORD = {
  kind: "import",
  message: "Audio processing failed. Please try another video.",
  detail: "out-of-memory — torch.OutOfMemoryError: CUDA out of memory.",
  cause: "out-of-memory",
  context: {
    jobId: "abcdefabcdef",
    stage: "Error: Processing failed",
    device: "cuda",
    gpuFallback: true,
    timings: '{"download": 4.2, "separate": 61.0}',
  },
  tail: ["torch.OutOfMemoryError: CUDA out of memory.", "Tried to allocate 2.40 GiB"],
};

const DIAG = {
  version: "0.9.1",
  model: "htdemucs_6s",
  ffmpegConfigured: true,
  buildTarget: { os: "windows", arch: "x64", gpu: "nvidia" },
  isDesktop: true,
};

const url = buildReportUrl(RECORD, DIAG);
const q = params(url);

check("targets the bug form", q.get("template") === "bug_report.yml");
check(
  "blank issues are disabled, so template must be present",
  url.startsWith("https://github.com/stemdeckapp/stemdeck/issues/new?"),
);
check("title leads with the cause", q.get("title") === "[Bug]: Import failed — out-of-memory");
check("version is prefixed with v", q.get("version") === "v0.9.1");
check("os matches the dropdown", q.get("os") === "Windows");
check("install matches the dropdown", q.get("install") === "Windows ZIP");
check("what carries StemDeck's own message", q.get("what").includes("Audio processing failed"));
check("what carries the classified detail", q.get("what").includes("out-of-memory"));
check("steps names the stage it died at", q.get("steps").includes("Error: Processing failed"));

const extra = q.get("extra");
check("extra reports the device and the CPU fallback", extra.includes("cuda (fell back to CPU)"));
check("extra reports the model", extra.includes("htdemucs_6s"));
check("extra carries the stderr tail", extra.includes("Tried to allocate 2.40 GiB"));
check("tail is fenced so GitHub renders it as code", extra.includes("```"));

// preflight is a checkboxes field: GitHub cannot prefill it, and we must not
// pretend otherwise -- the user ticking it is the "I searched for duplicates"
// promise.
check("preflight is not faked", q.get("preflight") === null);

// ─── privacy ───

const PRIVATE = {
  ...RECORD,
  context: {
    ...RECORD.context,
    title: "Someone's Private Demo Take 3",
    sourceUrl: "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
  },
};
const privateUrl = buildReportUrl(PRIVATE, DIAG);
check("never carries a track title", !privateUrl.includes("Private%20Demo") && !privateUrl.includes("Private Demo"));
check("never carries a source URL", !privateUrl.includes("youtube.com") && !privateUrl.includes("dQw4w9WgXcQ"));

// ─── length ceiling ───

const HUGE = {
  ...RECORD,
  tail: Array.from({ length: 400 }, (_, i) => `line ${i}: ${"x".repeat(120)}`),
};
const hugeUrl = buildReportUrl(HUGE, DIAG);
check("stays inside the URL ceiling", hugeUrl.length <= 6000, `${hugeUrl.length} chars`);
const hugeExtra = params(hugeUrl).get("extra");
check("truncation keeps the END of the tail, where the error is", hugeExtra.includes("line 399"));
check("truncation says so and points at the logs", hugeExtra.includes("Export logs"));
check("truncation does not eat the report body", params(hugeUrl).get("what").includes("Audio processing failed"));

// ─── degenerate input ───

const bare = buildReportUrl({ kind: "playback", message: "This track's audio could not be loaded." }, {});
check("survives no diagnostics at all", bare.includes("template=bug_report.yml"));
check("unknown OS falls back to Other", params(bare).get("os") === "Other");
check("no version is empty, not 'vundefined'", params(bare).get("version") === "");

console.log(`\n${pass}/${pass + fail} checks passed`);
process.exit(fail ? 1 : 0);
