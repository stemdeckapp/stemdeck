// Sound and picture sync in the export step (static/js/video/avSync.js): the
// nudge's range and grid, the state in words for each source and reason, the
// offset sentence, and the POST .../sync client with a stubbed fetch.
import {
  NUDGE_MIN_MS, NUDGE_MAX_MS, NUDGE_STEP_MS,
  clampNudgeMs, readSync, reasonKey, signedMs, stateText, offsetText, syncView, syncUrl, postSync, refusalText,
} from "../../static/js/video/avSync.js";
import { TRANSLATIONS } from "../../static/js/i18n.js";

let failed = 0;
let passed = 0;
function check(name, ok, detail = "") {
  if (ok) { passed += 1; console.log(`PASS  ${name}`); } else { failed += 1; console.log(`FAIL  ${name} ${detail}`); }
}

// A t() that shows the key and its values, so a test sees which sentence was picked.
const t = (key, vars) => (vars ? `${key}${JSON.stringify(vars)}` : key);

// ── Nudge range ──
check("range is a second either way in 5 ms steps", NUDGE_MIN_MS === -1000 && NUDGE_MAX_MS === 1000 && NUDGE_STEP_MS === 5);
check("rounds to the 5 ms grid", clampNudgeMs(12) === 10 && clampNudgeMs(13) === 15 && clampNudgeMs(-12) === -10);
check("clamps to the range", clampNudgeMs(1500) === 1000 && clampNudgeMs(-2500) === -1000);
check("not a number is 0", clampNudgeMs("x") === 0 && clampNudgeMs(NaN) === 0 && clampNudgeMs(undefined) === 0);
check("a string number is read", clampNudgeMs("40") === 40);
check("never -0", Object.is(clampNudgeMs(-1), 0));

// ── Reading the take entry ──
const entry = (sync, extra = {}) => ({ id: "t1", has_camera: true, camera_offset_ms: 0, video_sync: sync, ...extra });
check("no camera: nothing to show", readSync({ has_camera: false, video_sync: { source: "measured" } }) === null);
check("an older server without video_sync: nothing to show", readSync({ has_camera: true }) === null);
let s = readSync(entry({ source: "measured", base: "measured", offsetMs: -70.4, nudgeMs: 0, confidence: 0.93, reason: "ok", pictureOffsetMs: -70.4 }));
check("measured entry is read", s.source === "measured" && s.base === "measured" && s.offsetMs === -70.4 && s.confidence === 0.93 && s.pictureOffsetMs === -70.4, JSON.stringify(s));
s = readSync(entry({ source: "measured", base: "measured", offsetMs: 10, nudgeMs: 25, confidence: 0.8, reason: "ok" }));
check("a nudge makes it manual, whatever source says", s.source === "manual");
check("pictureOffsetMs falls back to the contract's sum", s.pictureOffsetMs === 35, String(s.pictureOffsetMs));
s = readSync(entry({ source: "manual", base: "estimated", offsetMs: 400, nudgeMs: 0, confidence: null, reason: "silent" }));
check("manual with no nudge is its base", s.source === "estimated");
check("null confidence stays null", s.confidence === null);
s = readSync(entry({ source: "bogus", base: "bogus", offsetMs: "x", nudgeMs: null, confidence: 7 }));
check("unknown values fall back safely", s.source === "estimated" && s.base === "estimated" && s.offsetMs === 0 && s.nudgeMs === 0 && s.confidence === 1, JSON.stringify(s));

// ── Reasons ──
const codes = ["no-camera-audio", "no-camera-mic", "camera-mic-denied", "camera-mic-failed", "silent", "low-confidence", "measure-failed", "legacy"];
const keys = codes.map(reasonKey);
check("each reason code has its own sentence", new Set(keys).size === codes.length && keys.every((k) => k.startsWith("avsync.reason.") && k !== "avsync.reason.other"));
check("an unknown code gets the general sentence", reasonKey("new-code") === "avsync.reason.other" && reasonKey(undefined) === "avsync.reason.other");
check("ok is not an estimate reason", reasonKey("ok") === "avsync.reason.other");
// Every sentence exists in all ten tables.
const tables = ["en", "pl", "ja", "zh-Hans", "de", "pt", "id", "fr", "es", "ko"];
const needed = [...keys, "avsync.reason.other", "avsync.title", "avsync.measured", "avsync.measuredMatch", "avsync.manualMeasured", "avsync.manualEstimated",
  "avsync.offsetAfter", "avsync.offsetBefore", "avsync.offsetTogether", "avsync.warn", "avsync.nudge", "avsync.nudgeHint", "avsync.ms",
  "avsync.earlier", "avsync.later", "avsync.reset", "avsync.resetTitle", "avsync.measureAgain", "avsync.saving", "avsync.measuring", "avsync.saved",
  "avsync.exportAgain", "avsync.err.running", "avsync.err.notFinished", "avsync.err.noCamera", "avsync.err.gone", "avsync.err.offline",
  "avsync.err.failed", "avsync.err.failedWhy", "help.avSync", "help.avSyncNudge", "help.avSyncReset", "help.avSyncMeasure"];
for (const lang of tables) {
  const missing = needed.filter((k) => typeof TRANSLATIONS[lang]?.[k] !== "string");
  check(`${lang}: every sync sentence is translated`, !missing.length, missing.join(", "));
  const dashed = needed.filter((k) => /\u2014/.test(TRANSLATIONS[lang]?.[k] || ""));
  check(`${lang}: no em dash in the sync sentences`, !dashed.length, dashed.join(", "));
}
check("English never says we", needed.every((k) => !/\b(we|our)\b/i.test(TRANSLATIONS.en[k] || "")));
check("the nudge label says what moves", /picture/i.test(TRANSLATIONS.en["avsync.nudge"]) && /earlier/i.test(TRANSLATIONS.en["avsync.nudge"]) && /later/i.test(TRANSLATIONS.en["avsync.nudge"]));
check("the hint states the direction of plus", /plus/i.test(TRANSLATIONS.en["avsync.nudgeHint"]) && /later/i.test(TRANSLATIONS.en["avsync.nudgeHint"]));

// ── State in words ──
check("measured with a match percent", stateText(readSync(entry({ source: "measured", base: "measured", confidence: 0.934, nudgeMs: 0 })), t) === 'avsync.measuredMatch{"pct":93}');
check("measured without a confidence", stateText(readSync(entry({ source: "measured", base: "measured", confidence: null, nudgeMs: 0 })), t) === "avsync.measured");
check("estimated says its reason", stateText(readSync(entry({ source: "estimated", base: "estimated", reason: "silent", nudgeMs: 0 })), t) === "avsync.reason.silent");
check("manual from measured", stateText(readSync(entry({ base: "measured", nudgeMs: 20 })), t) === "avsync.manualMeasured");
check("manual from estimated", stateText(readSync(entry({ base: "estimated", nudgeMs: -20 })), t) === "avsync.manualEstimated");
check("no sync: no text", stateText(null, t) === "" && offsetText(null, t) === "");

// ── Offset ──
check("picture after the sound", offsetText({ pictureOffsetMs: 456.4 }, t) === 'avsync.offsetAfter{"n":456}');
check("picture before the sound", offsetText({ pictureOffsetMs: -70.4 }, t) === 'avsync.offsetBefore{"n":70}');
check("together", offsetText({ pictureOffsetMs: 0.3 }, t) === "avsync.offsetTogether");
check("signed ms", signedMs(40) === "+40" && signedMs(-15) === "-15" && signedMs(0) === "0" && signedMs(-0.2) === "0");

// ── The whole view ──
let v = syncView(null, t);
check("no take with a picture: hidden", v.show === false);
v = syncView(readSync(entry({ source: "estimated", base: "estimated", reason: "no-camera-audio", offsetMs: 456, nudgeMs: 0 })), t);
check("estimated: shown with the warning", v.show && v.warn === "avsync.warn" && v.state === "avsync.reason.noCameraAudio");
check("estimated, no nudge: no reset", v.canReset === false && v.nudge === 0 && v.value === 'avsync.ms{"n":"0"}');
check("a reason the same files answer again: no Measure again", v.canRemeasure === false);
v = syncView(readSync(entry({ source: "estimated", base: "estimated", reason: "measure-failed", nudgeMs: 0 })), t);
check("measure-failed offers Measure again", v.canRemeasure === true);
v = syncView(readSync(entry({ source: "measured", base: "measured", confidence: 0.9, offsetMs: 10, nudgeMs: 40 })), t);
check("manual: no warning, reset offered, value signed", v.warn === "" && v.canReset && v.nudge === 40 && v.value === 'avsync.ms{"n":"+40"}');
v = syncView(readSync(entry({ base: "measured", nudgeMs: 1500 })), t);
check("a nudge beyond the slider shows as it is, the slider at its end", v.nudge === 1000 && v.value === 'avsync.ms{"n":"+1500"}');
v = syncView(readSync(entry({ source: "measured", base: "measured", confidence: 0.9, nudgeMs: 0 })), t);
check("measured: no warning", v.warn === "");

// ── The route ──
check("url encodes ids", syncUrl("a b", "t/1") === "/api/jobs/a%20b/takes/t%2F1/sync");

const calls = [];
const stubFetch = (status, body, { throws = false, notJson = false } = {}) => async (url, init) => {
  calls.push({ url, init });
  if (throws) throw new TypeError("Failed to fetch");
  return { ok: status >= 200 && status < 300, status, json: async () => { if (notJson) throw new SyntaxError("x"); return body; } };
};

const okEntry = entry({ source: "manual", base: "measured", nudgeMs: 40, pictureOffsetMs: -30 }, { camera_start_sec: 12.3 });
let got = await postSync("job1", "t1", { nudgeMs: 40 }, stubFetch(200, okEntry));
const last = calls.at(-1);
check("POST to the sync route", last.url === "/api/jobs/job1/takes/t1/sync" && last.init.method === "POST");
check("JSON body with the content type the CSRF check needs", last.init.headers["Content-Type"] === "application/json" && last.init.body === '{"nudgeMs":40}');
check("resolves with the take entry", got === okEntry);
got = await postSync("job1", "t1", { remeasure: true }, stubFetch(200, okEntry));
check("remeasure body", calls.at(-1).init.body === '{"remeasure":true}');

async function refusal(status, body, opts) {
  try {
    await postSync("job1", "t1", { nudgeMs: 5 }, stubFetch(status, body, opts));
    return null;
  } catch (err) {
    return err;
  }
}
let err = await refusal(409, { detail: "video render running" });
check("409 render running is refused with its detail", err?.status === 409 && err.detail === "video render running");
check("render running in words", refusalText(err, t) === "avsync.err.running");
check("take not finished in words", refusalText(await refusal(409, { detail: "take not finished" }), t) === "avsync.err.notFinished");
check("no camera in words", refusalText(await refusal(422, { detail: "take has no camera" }), t) === "avsync.err.noCamera");
check("404 take not found: gone", refusalText(await refusal(404, { detail: "take not found" }), t) === "avsync.err.gone");
check("404 job not ready: gone", refusalText(await refusal(404, { detail: "job not ready" }), t) === "avsync.err.gone");
check("422 range error keeps the server's reason", refusalText(await refusal(422, { detail: "nudgeMs: out of range" }), t) === 'avsync.err.failedWhy{"reason":"nudgeMs: out of range"}');
err = await refusal(500, null, { notJson: true });
check("500 without a JSON body: the general sentence", err?.status === 500 && refusalText(err, t) === "avsync.err.failed");
err = await refusal(0, null, { throws: true });
check("server not reached: status 0, offline sentence", err?.status === 0 && refusalText(err, t) === "avsync.err.offline");

console.log(`\n${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
