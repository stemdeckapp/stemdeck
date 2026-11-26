// Crash reports (desktop only, off by default): the page's forwarding guard.
// Nothing is forwarded while reports are off; at most ten a minute; the same
// error once per page load; every field cut to size; and the Settings text in
// every language. The shell gates and scrubs again in Rust
// (desktop/src-tauri/src/telemetry.rs, whose own tests cover the scrubber).
//
// Run:  node tests/js/crash-reports.test.mjs

import {
  CRASH_CONSENT_VERSION,
  MAX_MESSAGE_CHARS,
  MAX_PER_MINUTE,
  MAX_STACK_CHARS,
  createForwarder,
  crashReportsAvailable,
  installCrashForwarding,
  readCrashReportExample,
  readCrashReports,
  reportKey,
  saveCrashReports,
  webReportFrom,
} from "../../static/js/crashReports.js";
import { TRANSLATIONS } from "../../static/js/i18n.js";

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

const report = (message, stack = "") => ({ kind: "error", name: "Error", message, stack });

// ── off means nothing leaves the page ──
{
  const sent = [];
  const forward = createForwarder({ send: (r) => sent.push(r), isOn: () => false });
  check("off: a report is not forwarded", forward(report("boom")) === false);
  check("off: send is never called", sent.length === 0);
}

// ── on: forwarded once, duplicates dropped ──
{
  const sent = [];
  let on = true;
  const forward = createForwarder({ send: (r) => sent.push(r), isOn: () => on, now: () => 1000 });
  check("on: a report is forwarded", forward(report("boom")) === true && sent.length === 1);
  check("the same report twice is one report", forward(report("boom")) === false && sent.length === 1);
  on = false;
  check("turning off stops forwarding at once", forward(report("other")) === false && sent.length === 1);
}

// ── rate limit: ten in a rolling minute ──
{
  const sent = [];
  let t = 0;
  const forward = createForwarder({ send: (r) => sent.push(r), now: () => t });
  for (let i = 0; i < 25; i++) forward(report(`loop ${i}`));
  check(`at most ${MAX_PER_MINUTE} in a minute`, sent.length === MAX_PER_MINUTE, String(sent.length));
  t = 59_999;
  check("still capped just before the minute ends", forward(report("late")) === false);
  t = 60_000;
  check("allowed again once the minute has passed", forward(report("next minute")) === true);
}

// ── a send that throws does not escape the guard ──
{
  const forward = createForwarder({ send: () => { throw new Error("ipc gone"); } });
  let threw = false;
  const warn = console.warn;
  console.warn = () => {};
  try {
    forward(report("x"));
  } catch {
    threw = true;
  }
  console.warn = warn;
  check("a failing send does not throw back into the error handler", !threw);
}

// ── what is taken from an event ──
{
  const err = new TypeError("x".repeat(5000));
  err.stack = "y".repeat(20_000);
  const r = webReportFrom("error", { message: "ignored", error: err, filename: "http://127.0.0.1/static/js/a.js?token=1" });
  check("error: kind", r.kind === "error");
  check("error: name", r.name === "TypeError");
  check("error: message cut", r.message.length === MAX_MESSAGE_CHARS);
  check("error: stack cut", r.stack.length === MAX_STACK_CHARS);
  check("error: only kind, name, message, stack", JSON.stringify(Object.keys(r).sort()) === JSON.stringify(["kind", "message", "name", "stack"]));
  check("error: the event's file name is not taken", !JSON.stringify(r).includes("token"));

  const plain = webReportFrom("error", { message: "Script error.", error: null });
  check("a cross-origin error keeps its message", plain.message === "Script error." && plain.name === "Error" && plain.stack === "");

  const rej = webReportFrom("rejection", { reason: "plain string reason" });
  check("rejection of a string", rej.kind === "rejection" && rej.message === "plain string reason");

  const rejErr = webReportFrom("rejection", { reason: new RangeError("bad") });
  check("rejection of an Error", rejErr.name === "RangeError" && rejErr.message === "bad");

  const none = webReportFrom("rejection", { reason: undefined });
  check("rejection with no reason", none.message === "");

  check("an unknown kind reads as error", webReportFrom("weird", {}).kind === "error");
}

// ── the duplicate key ──
{
  const a = reportKey(report("m", "TypeError: m\n    at f (/static/js/a.js:1:2)\n    at g (/static/js/b.js:3:4)"));
  const b = reportKey(report("m", "TypeError: m\n    at f (/static/js/a.js:1:2)\n    at h (/static/js/c.js:9:9)"));
  const c = reportKey(report("m", "TypeError: m\n    at k (/static/js/z.js:1:2)"));
  check("same message and first frame: same report", a === b);
  check("another first frame: another report", a !== c);
}

// ── outside the desktop app nothing exists ──
{
  globalThis.window = globalThis.window || {};
  check("no Tauri: reports are not available", crashReportsAvailable() === false);
  check("no Tauri: status is null", (await readCrashReports()) === null);
  check("no Tauri: saving does nothing", (await saveCrashReports(true)) === null);
  check("no Tauri: there is no example", (await readCrashReportExample()) === null);
  let added = 0;
  window.addEventListener = () => { added++; };
  installCrashForwarding();
  check("no Tauri: no error listener is installed", added === 0);
}

// ── the shell is asked with this text version ──
{
  const calls = [];
  window.__TAURI__ = { core: { invoke: async (cmd, args) => { calls.push({ cmd, args }); return { on: args?.on === true }; } } };
  const status = await saveCrashReports(true);
  check("save sends the consent text version", calls[0]?.cmd === "set_crash_reports" && calls[0]?.args?.version === CRASH_CONSENT_VERSION);
  check("save answers the shell's status", status?.on === true);
  calls.length = 0;
  await readCrashReportExample();
  check("the example is asked of the shell, read-only", calls.length === 1 && calls[0].cmd === "crash_report_example" && calls[0].args === undefined);
  delete window.__TAURI__;
}

// ── Settings text in every language ──
const KEYS = [
  "settings.tab.privacy",
  "settings.desc.privacy",
  "settings.group.crashReports",
  "settings.crashReports.title",
  "settings.crashReports.desc",
  "settings.crashReports.on",
  "settings.crashReports.off",
  "settings.crashReports.saveFailed",
  "settings.crashReports.sentTitle",
  "settings.crashReports.sentError",
  "settings.crashReports.sentVersion",
  "settings.crashReports.sentSystem",
  "settings.crashReports.sentGpu",
  "settings.crashReports.sentPart",
  "settings.crashReports.neverTitle",
  "settings.crashReports.neverPerson",
  "settings.crashReports.neverFiles",
  "settings.crashReports.neverActivity",
  "settings.crashReports.neverIp",
  "settings.crashReports.whereTitle",
  "settings.crashReports.where",
  "settings.crashReports.notice",
  "settings.crashReports.deleteTitle",
  "settings.crashReports.delete",
  "settings.crashReports.lastTitle",
  "settings.crashReports.lastDesc",
  "settings.crashReports.lastEmpty",
  "settings.crashReports.lastAria",
  "settings.crashReports.serverNote",
  "help.set.crashReports",
  "help.set.crashReportsLast",
  "settings.crashReports.exampleTitle",
  "settings.crashReports.exampleDesc",
  "settings.crashReports.exampleAria",
  "settings.crashReports.exampleFailed",
  "help.set.crashReportsExample",
  "settings.crashReports.unavailable",
];
const EM_DASH = String.fromCharCode(0x2014);
for (const [lang, table] of Object.entries(TRANSLATIONS)) {
  if (lang === "pt-PT") continue; // a variant: falls back to pt
  for (const key of KEYS) {
    check(`${lang} has ${key}`, typeof table[key] === "string" && table[key].trim() !== "");
    check(`${lang} ${key} has no em dash`, !String(table[key]).includes(EM_DASH));
  }
}
for (const [key, value] of Object.entries(TRANSLATIONS["pt-PT"])) {
  if (KEYS.includes(key)) check(`pt-PT ${key} has no em dash`, !String(value).includes(EM_DASH));
}

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
