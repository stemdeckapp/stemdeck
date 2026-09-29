// The run's report: report.md and report.json in acceptance-results/, one row
// per check of the manual checklist in its order, plus copy-results.txt in the
// shape the manual page's "Copy results" button produces.
//
// Results:
//   pass                 everything the check expects was verified
//   partly               what can be measured passed; the rest needs eyes
//   fail                 StemDeck did not do what the check expects
//   service unavailable  failed because YouTube, MusicBrainz, LRCLIB, AcoustID
//                        or Wikimedia did not answer; evidence attached
//   skipped              not run, with the reason
//   not run              the run stopped before it

import fs from "node:fs";
import path from "node:path";
import { BUILD_LABEL, CHECKS } from "./checks.mjs";
import { PACKAGE_DIR, RESULTS_DIR, readState } from "./harness.mjs";
import { fingerprint, realMirrorPath } from "./global-setup.mjs";

const KEY = (process.env.STEMDECK_ACCEPTANCE_ACOUSTID_KEY || "").trim();

// Belt and braces: nothing a check wrote may carry the key into the report.
const clean = (text) => {
  let out = String(text ?? "");
  if (KEY) out = out.split(KEY).join("[key]");
  // Terminal colours in an assertion message.
  // eslint-disable-next-line no-control-regex
  return out.replace(/\x1b\[[0-9;]*m/g, "");
};

const firstLines = (text, n = 3) => clean(text).split("\n").map((l) => l.trim()).filter(Boolean).slice(0, n).join(" ");

export default class AcceptanceReporter {
  constructor() {
    this.results = new Map();
    this.startedAt = Date.now();
  }

  onBegin() {
    this.startedAt = Date.now();
  }

  onTestEnd(test, result) {
    const id = test.title.split(" ")[0];
    // The same annotation can arrive on both the test and the result.
    const notes = (type) => [...new Set([...test.annotations, ...result.annotations]
      .filter((a) => a.type === type).map((a) => clean(a.description)))];
    let outcome;
    const failed = result.status === "failed" || result.status === "timedOut" || result.status === "interrupted";
    if (result.status === "skipped") outcome = "skipped";
    else if (failed && notes("service").length) outcome = "service unavailable";
    else if (failed) outcome = "fail";
    else if (notes("partly").length) outcome = "partly";
    else outcome = "pass";

    const reason = [];
    if (outcome === "skipped") reason.push(...notes("skip"), ...notes("fixme"));
    if (failed) reason.push(firstLines(result.error?.message || result.errors?.[0]?.message || result.status, 4));
    if (outcome === "service unavailable") reason.push(`Evidence: ${notes("service").join(" | ")}`);
    if (outcome === "partly") reason.push(`Needs eyes: ${notes("partly").join(" ")}`);
    const extra = notes("note");

    this.results.set(id, {
      id,
      result: outcome,
      reason: reason.filter(Boolean).join(" "),
      notes: extra,
      seconds: Math.round(result.duration / 1000),
      screenshots: notes("screenshot").map((p) => path.relative(RESULTS_DIR, p).split(path.sep).join("/")),
    });
  }

  onEnd(fullResult) {
    // A dry listing (--list) runs nothing: the last run's report stays.
    if (process.argv.includes("--list")) return;
    fs.mkdirSync(RESULTS_DIR, { recursive: true });
    const state = readState() || {};
    const rows = CHECKS.map((c) => ({
      ...c,
      ...(this.results.get(c.id) || { result: "not run", reason: "The run stopped before this check.", notes: [], screenshots: [] }),
    }));
    const count = (r) => rows.filter((x) => x.result === r).length;
    const runSec = Math.round((Date.now() - this.startedAt) / 1000);
    const version = readVersion();
    const build = `${version || "unknown version"} · ${state.mode === "server" ? `server ${state.url}` : BUILD_LABEL}`;
    const mirrorAfter = state.mode === "desktop" ? fingerprint(realMirrorPath()) : null;
    const isolation = state.mode === "desktop"
      ? {
          realMirrorUnchanged: JSON.stringify(state.realMirror) === JSON.stringify(mirrorAfter),
          realMirrorPath: realMirrorPath(),
        }
      : null;

    const json = {
      build,
      target: state.mode,
      status: fullResult.status,
      startedAt: new Date(this.startedAt).toISOString(),
      runSeconds: runSec,
      appStartupSeconds: state.startupSec ?? null,
      isolation,
      counts: {
        pass: count("pass"),
        partly: count("partly"),
        fail: count("fail"),
        serviceUnavailable: count("service unavailable"),
        skipped: count("skipped"),
        notRun: count("not run"),
      },
      checks: rows.map(({ id, area, title, refs, result, reason, notes, screenshots, seconds }) => ({
        id, area, title, refs, result, reason, notes, screenshots, seconds: seconds ?? null,
      })),
    };
    fs.writeFileSync(path.join(RESULTS_DIR, "report.json"), `${JSON.stringify(json, null, 2)}\n`);

    const cell = (t) => clean(t).replace(/\|/g, "\\|").replace(/\n/g, " ");
    const md = [
      `# StemDeck acceptance run`,
      "",
      `- Build: ${build}`,
      `- Target: ${state.mode === "server" ? `server at ${state.url}` : "the desktop app (StemDeck.exe over the DevTools protocol)"}`,
      `- Started: ${json.startedAt}, took ${Math.floor(runSec / 60)} min ${runSec % 60} s`
        + (state.startupSec != null ? ` (the app took ${state.startupSec} s to reach the studio)` : ""),
      isolation
        ? `- Isolation: the real ${isolation.realMirrorPath} ${isolation.realMirrorUnchanged ? "was not changed" : "CHANGED during the run"}`
        : "",
      `- Pass ${json.counts.pass}, partly ${json.counts.partly}, fail ${json.counts.fail}, service unavailable ${json.counts.serviceUnavailable}, skipped ${json.counts.skipped}, not run ${json.counts.notRun}`,
      "",
      "| Id | Check | Result | Reason and notes | Screenshots |",
      "|---|---|---|---|---|",
      ...rows.map((r) => `| ${r.id} | ${cell(r.title)} | **${r.result}** | ${cell([r.reason, ...(r.notes || [])].filter(Boolean).join(" "))} | ${r.screenshots.map((s) => `[${path.basename(s, ".png")}](${s})`).join(" ")} |`),
      "",
      "## Copy results",
      "",
      "```",
      copyText(rows, build),
      "```",
      "",
    ].filter((l) => l !== null).join("\n");
    fs.writeFileSync(path.join(RESULTS_DIR, "report.md"), md);
    fs.writeFileSync(path.join(RESULTS_DIR, "copy-results.txt"), `${copyText(rows, build)}\n`);
    console.log(`\nAcceptance report: ${path.join(RESULTS_DIR, "report.md")}`);
  }
}

// The manual page's summary, with the two results only an automated run has
// (partly, service unavailable) listed where a person would read them.
function copyText(rows, build) {
  const by = (r) => rows.filter((x) => x.result === r);
  const pass = [...by("pass"), ...by("partly")];
  const na = [...by("skipped"), ...by("service unavailable")];
  const lines = [
    `StemDeck ${build}: automated acceptance run, ${new Date().toISOString().slice(0, 10)}`,
    `Pass ${pass.length}, Fail ${by("fail").length}, N/A ${na.length}, not run ${by("not run").length}`,
  ];
  const refs = (c) => (c.refs.length ? ` (${c.refs.map((n) => `#${n}`).join(", ")})` : "");
  if (by("fail").length) {
    lines.push("", "Failed:");
    for (const c of by("fail")) lines.push(`- ${c.id} ${c.title}${refs(c)}`, `  ${clean(c.reason)}`);
  }
  if (by("service unavailable").length) {
    lines.push("", "Service unavailable (not a StemDeck result):");
    for (const c of by("service unavailable")) lines.push(`- ${c.id} ${c.title}${refs(c)}`, `  ${clean(c.reason)}`);
  }
  if (by("partly").length) {
    lines.push("", "Passed what a program can check; still needs eyes:");
    for (const c of by("partly")) lines.push(`- ${c.id} ${clean(c.reason).replace(/^Needs eyes: /, "")}`);
  }
  if (by("skipped").length) lines.push("", `N/A: ${by("skipped").map((c) => `${c.id} (${clean(c.reason)})`).join("; ")}`);
  if (by("not run").length) lines.push("", `Not run: ${by("not run").map((c) => c.id).join(", ")}`);
  return lines.join("\n");
}

function readVersion() {
  const state = readState() || {};
  if (state.version) return state.version;
  try {
    return `v${JSON.parse(fs.readFileSync(path.join(PACKAGE_DIR, "backend", "static", "version.json"), "utf8")).version}`;
  } catch {
    return "";
  }
}
