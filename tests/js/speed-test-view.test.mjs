// Settings > Separation, "Benchmark": the table, the lines and what Apply
// sends (static/js/speedTestView.js). Pure, so it runs without a browser;
// tests/e2e/speed-test.spec.mjs checks the drawing and the polling.
//
// Run:  node tests/js/speed-test-view.test.mjs

import { TRANSLATIONS } from "../../static/js/i18n.js";
import {
  applyPatch,
  canApply,
  deviceLabel,
  diskLines,
  endLine,
  failedRows,
  gpuFailed,
  isResult,
  isRunning,
  isSpeedTestState,
  isStale,
  minutesFor,
  FAST_MAX_SECONDS,
  OK_MAX_SECONDS,
  durationText,
  formatDuration,
  friendlyDevice,
  headlineLine,
  planEstimated,
  progressPct,
  ratingFor,
  reasonLines,
  resultRows,
  roundDuration,
  runningLine,
  secondsFor,
} from "../../static/js/speedTestView.js";

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
const eq = (name, actual, expected) =>
  check(name, JSON.stringify(actual) === JSON.stringify(expected), `got ${JSON.stringify(actual)}, want ${JSON.stringify(expected)}`);

const en = TRANSLATIONS.en;
const t = (key, vars = {}) => String(en[key] ?? key).replace(/\{(\w+)\}/g, (_, k) => (k in vars ? String(vars[k]) : `{${k}}`));

const catalog = {
  models: {
    htdemucs_6s: { name: "HTDemucs 6s" },
    htdemucs_ft: { name: "HTDemucs FT" },
    kim_mel_roformer: { name: "Kim Mel-RoFormer" },
  },
};

function result(over = {}) {
  return {
    version: 1,
    ts: 1_790_000_000,
    stale: false,
    hw: { cpu: "Test CPU", cores: 8, gpu: "Test GPU", vram_mb: 8192, devices: ["cpu", "cuda"], torch: "2.6.0" },
    disk: { write_mb_s: 812.4, skipped: null, slow: false },
    devices: {
      cpu: {
        htdemucs_6s: { rtf: 0.5, peak_mb: 900, measured: true },
        kim_mel_roformer: { rtf: 3.4, measured: false, estimated_from: "htdemucs_6s" },
        htdemucs_ft: { rtf: 1.84, measured: false, estimated_from: "htdemucs_6s" },
      },
      cuda: {
        htdemucs_6s: { rtf: 0.068, peak_mb: 619, measured: true },
        kim_mel_roformer: { rtf: 0.105, measured: true },
        htdemucs_ft: { rtf: 0.145, measured: false, estimated_from: "htdemucs_6s" },
      },
    },
    recommendation: {
      device: "cuda",
      stems: { vocals: "kim_mel_roformer", drums: "htdemucs_6s", bass: "htdemucs_6s", guitar: "htdemucs_6s", piano: "htdemucs_6s" },
      estimate_minutes_4min: 0.69,
      budget_minutes: 3.0,
      reason: "fits",
    },
    ...over,
  };
}

// States.
check("a state answer is read", isSpeedTestState({ state: "idle", step: null, pct: 0, error: null, result: null }));
check("a 404 body is not a state", !isSpeedTestState({ detail: "Not Found" }));
check("running is running", isRunning({ state: "running", step: "cpu", pct: 40 }));
check("done is not running", !isRunning({ state: "done" }));
eq("progress is clamped and whole", [progressPct({ pct: 41.6 }), progressPct({ pct: 140 }), progressPct({ pct: -3 }), progressPct({})], [42, 100, 0, 0]);
eq("the running line names the device", runningLine({ state: "running", step: "cuda", pct: 50 }, t), "Benchmarking CUDA (NVIDIA).");
eq("on ROCm it names AMD", runningLine({ state: "running", step: "cuda", pct: 50 }, t, true), "Benchmarking ROCm (AMD).");
eq("the disk step names the folder", runningLine({ state: "running", step: "disk", pct: 5 }, t), "Benchmarking the StemData folder.");
eq("no step yet, no line", runningLine({ state: "running", step: null, pct: 0 }, t), "");
eq("failed without a code is the plain error line", endLine({ state: "failed" }, t), { text: en["settings.speedTest.failed"], error: true });
for (const [code, key] of [["model-missing", "modelMissing"], ["torch-missing", "torchMissing"], ["timeout", "timeout"], ["stalled", "stalled"], ["failed", "failed"]]) {
  eq(`failed with ${code} says why`, endLine({ state: "failed", error_code: code }, t), { text: en[`settings.speedTest.error.${key}`], error: true });
}
eq("an unknown code falls back to the plain line", endLine({ state: "failed", error_code: "new-code" }, t).text, en["settings.speedTest.failed"]);
eq("model missing says to separate a song first", endLine({ state: "failed", error_code: "model-missing" }, t).text,
  "The separation model is not downloaded yet. Separate one song first, then run the benchmark again.");
eq("cancelled is a plain line", endLine({ state: "cancelled" }, t), { text: en["settings.speedTest.cancelled"], error: false });
eq("done has no end line", endLine({ state: "done" }, t), null);
eq("device names", ["cpu", "cuda", "mps"].map((d) => deviceLabel(d)), ["CPU", "CUDA (NVIDIA)", "MPS (Apple Silicon)"]);

// Result and staleness.
check("a result is a result", isResult(result()));
check("null is no result (never run)", !isResult(null));
check("a fresh result can be applied", canApply(result()));
check("a stale result says so", isStale(result({ stale: true })));
check("a stale result cannot be applied", !canApply(result({ stale: true })));
eq("Apply sends the device and all five on auto", applyPatch(result()), {
  demucs_device: "cuda",
  stem_models: { vocals: "auto", drums: "auto", bass: "auto", guitar: "auto", piano: "auto" },
});
eq("Apply on a processor result sends cpu", applyPatch(result({ recommendation: { ...result().recommendation, device: "cpu" } })).demucs_device, "cpu");

// Table rows.
const rows = resultRows(result(), { t, locale: "en", catalog });
eq("recommended device first, models in catalog order", rows.map((r) => `${r.device}:${r.model}`), [
  "cuda:htdemucs_6s", "cuda:htdemucs_ft", "cuda:kim_mel_roformer",
  "cpu:htdemucs_6s", "cpu:htdemucs_ft", "cpu:kim_mel_roformer",
]);
eq("the recommended plan's rows are marked", rows.filter((r) => r.recommended).map((r) => `${r.device}:${r.model}`), ["cuda:htdemucs_6s", "cuda:kim_mel_roformer"]);
eq("an estimate says estimate", rows.find((r) => r.device === "cpu" && r.model === "kim_mel_roformer").modelText, "Kim Mel-RoFormer (estimate)");
eq("a measured row says measured", rows.find((r) => r.device === "cpu" && r.model === "htdemucs_6s").modelText, "HTDemucs 6s (measured)");
eq("a recommended measured row carries both tags", rows.find((r) => r.device === "cuda" && r.model === "kim_mel_roformer").modelText, "Kim Mel-RoFormer (Recommended, measured)");
eq("minutesFor: rtf 0.5 is 2 minutes", minutesFor(0.5), 2);
eq("secondsFor: rtf 0.5 is 120 seconds", secondsFor(0.5), 120);
eq("no catalog: the id stands in for the name", resultRows(result(), { t, locale: "en" })[0].modelText, "htdemucs_6s (Recommended, measured)");
eq("a row with no speed is left out", resultRows(result({ devices: { cpu: { htdemucs_6s: { rtf: null, measured: true } } } }), { t, locale: "en", catalog }), []);

// Times in words, for a typical 4-minute song.
const row = (device, model) => rows.find((r) => r.device === device && r.model === model);
eq("each row's time in words", rows.map((r) => `${r.device}:${r.model}=${r.timeText}`), [
  "cuda:htdemucs_6s=about 16 seconds", "cuda:htdemucs_ft=about 35 seconds", "cuda:kim_mel_roformer=about 25 seconds",
  "cpu:htdemucs_6s=about 2 minutes", "cpu:htdemucs_ft=about 7 minutes", "cpu:kim_mel_roformer=about 14 minutes",
]);
eq("each row's rating", rows.map((r) => r.rating), ["fast", "ok", "fast", "ok", "slow", "slow"]);
eq("the rating is a word too", row("cpu", "htdemucs_ft").ratingText, "Slow");
eq("the speed factor is a sentence, faster than the song", row("cuda", "htdemucs_6s").factorText, "14.7 times faster than the song's length");
eq("and slower than the song", row("cpu", "kim_mel_roformer").factorText, "takes 3.4 times the song's length");
eq("the table names the device for a person, the technical name beside it", [row("cuda", "htdemucs_6s").deviceName, row("cuda", "htdemucs_6s").deviceText], ["Graphics card", "CUDA (NVIDIA)"]);
eq("rows of the other devices are marked other", rows.map((r) => r.other), [false, false, false, true, true, true]);
eq("without a recommended device nothing is other", resultRows(result({ recommendation: { ...result().recommendation, device: "" } }), { t, locale: "en", catalog }).some((r) => r.other), false);

eq("roundDuration: under 5 s is a few seconds", [roundDuration(0), roundDuration(4.99)], [null, null]);
eq("roundDuration: 5 to 29 s in whole seconds", [roundDuration(5), roundDuration(16.3), roundDuration(29.4)], [{ unit: "second", value: 5 }, { unit: "second", value: 16 }, { unit: "second", value: 29 }]);
eq("roundDuration: 30 to 89 s to the nearest 5", [roundDuration(30), roundDuration(32.4), roundDuration(32.5), roundDuration(89)], [
  { unit: "second", value: 30 }, { unit: "second", value: 30 }, { unit: "second", value: 35 }, { unit: "second", value: 90 }]);
eq("roundDuration: from 90 s whole minutes, never a decimal", [roundDuration(90), roundDuration(149), roundDuration(150), roundDuration(816)], [
  { unit: "minute", value: 2 }, { unit: "minute", value: 2 }, { unit: "minute", value: 3 }, { unit: "minute", value: 14 }]);
eq("roundDuration: not a number is a few seconds", roundDuration(NaN), null);
eq("formatDuration in English", [formatDuration(16, "en"), formatDuration(60.4, "en"), formatDuration(180, "en"), formatDuration(3, "en")], ["16 seconds", "60 seconds", "3 minutes", null]);
eq("formatDuration: Polish plural forms from Intl", [formatDuration(120, "pl"), formatDuration(300, "pl"), formatDuration(22, "pl")], ["2 minuty", "5 minut", "22 sekundy"]);
eq("formatDuration: Japanese", formatDuration(16, "ja"), "16 秒");
eq("durationText says about, a few, or an estimated", [
  durationText(16, t, "en"), durationText(4, t, "en"), durationText(16, t, "en", { estimated: true }), durationText(4, t, "en", { estimated: true }),
], ["about 16 seconds", "a few seconds", "an estimated 16 seconds", "a few seconds"]);

// The rating thresholds, at their boundaries.
eq("rating constants", [FAST_MAX_SECONDS, OK_MAX_SECONDS], [30, 180]);
eq("rating boundaries", [ratingFor(0), ratingFor(29.99), ratingFor(30), ratingFor(180), ratingFor(180.01), ratingFor(900)], ["fast", "fast", "ok", "ok", "slow", "slow"]);

// The headline.
eq("headline: measured plan with Kim", headlineLine(result(), t, "en"),
  "Your computer separates a 4-minute song in about 40 seconds. Recommended: your graphics card (NVIDIA), with the best vocal model plus the standard model.");
const onEstimate = result({ devices: { ...result().devices, cuda: { ...result().devices.cuda, kim_mel_roformer: { rtf: 0.105, measured: false } } } });
check("a plan on an estimated row is estimated", planEstimated(onEstimate));
check("a plan on measured rows is not", !planEstimated(result()));
eq("headline: an estimated plan never reads as a fact", headlineLine(onEstimate, t, "en"),
  "Your computer separates a 4-minute song in an estimated 40 seconds. Recommended: your graphics card (NVIDIA), with the best vocal model plus the standard model.");
const cpuStandard = result({ recommendation: { ...result().recommendation, device: "cpu", stems: { vocals: "htdemucs_6s", drums: "htdemucs_6s", bass: "htdemucs_6s", guitar: "htdemucs_6s", piano: "htdemucs_6s" }, estimate_minutes_4min: 2, reason: "no-gpu" } });
eq("headline: the processor with the standard model", headlineLine(cpuStandard, t, "en"),
  "Your computer separates a 4-minute song in about 2 minutes. Recommended: your processor, with the standard model.");
eq("headline: ROCm names AMD", headlineLine(result(), t, "en", true).includes("your graphics card (AMD)"), true);
eq("headline: a few seconds", headlineLine(result({ recommendation: { ...result().recommendation, estimate_minutes_4min: 0.05 } }), t, "en").startsWith("Your computer separates a 4-minute song in a few seconds."), true);
eq("headline: no device, no headline", headlineLine(result({ recommendation: { ...result().recommendation, device: "" } }), t, "en"), null);
eq("headline: no time, no headline", headlineLine(result({ recommendation: { ...result().recommendation, estimate_minutes_4min: null } }), t, "en"), null);
eq("friendly names", ["cuda", "mps", "cpu"].map((d) => friendlyDevice(d, t)), ["your graphics card (NVIDIA)", "your graphics chip (Apple)", "your processor"]);

// Why, under the headline: nothing when the plan simply fits.
const why = (rec) => reasonLines(result({ recommendation: { ...result().recommendation, ...rec } }), t, "en");
eq("fits says nothing more", reasonLines(result(), t, "en"), []);
// A failed GPU: its own table line, and never "no graphics card".
const gpuDown = result({
  devices: { cpu: result().devices.cpu },
  failed: { cuda: "timeout" },
  recommendation: { ...result().recommendation, device: "cpu", estimate_minutes_4min: 2, reason: "no-gpu" },
});
eq("a failed device gets a line with its reason", failedRows(gpuDown, t), [
  { device: "cuda", deviceText: "CUDA (NVIDIA)", code: "timeout", text: "CUDA (NVIDIA) could not be tested. It took too long." },
]);
eq("an unknown failure code reads as an error", failedRows(result({ failed: { mps: "weird" } }), t)[0].text, "MPS (Apple Silicon) could not be tested. It stopped with an error.");
for (const code of ["stalled", "device-unavailable", "model-missing", "torch-missing", "failed"]) {
  const text = failedRows(result({ failed: { cuda: code } }), t)[0].text;
  check(`failed device ${code} has its own sentence`, !text.includes("{") && text !== failedRows(result({ failed: { cuda: "timeout" } }), t)[0].text, text);
}
check("a GPU that failed counts as failed", gpuFailed(gpuDown));
check("a CPU that failed alone is not a GPU failure", !gpuFailed(result({ failed: { cpu: "timeout" } })));
eq("no-gpu with a failed GPU reads as gpu-failed", reasonLines(gpuDown, t, "en"), ["The graphics card could not be tested, so the processor is used."]);
eq("gpu-failed from the server", why({ device: "cpu", estimate_minutes_4min: 2, reason: "gpu-failed" }), ["The graphics card could not be tested, so the processor is used."]);
eq("cpu-over-budget names the goal in words", why({ device: "cpu", estimate_minutes_4min: 4.5, reason: "cpu-over-budget" }), ["Even with the standard model this is more than the goal of 3 minutes."]);
eq("over-budget whose plan is still over the goal reads as cpu-over-budget", why({ device: "cpu", estimate_minutes_4min: 4.5, reason: "over-budget" }),
  ["Even with the standard model this is more than the goal of 3 minutes."]);
eq("over-budget whose standard plan fits keeps its sentence", why({ device: "cpu", estimate_minutes_4min: 2, reason: "over-budget" }),
  ["The best vocal model would take longer than the goal of 3 minutes for a 4-minute song, so the standard model is recommended."]);
eq("no GPU", why({ device: "cpu", estimate_minutes_4min: 2, reason: "no-gpu" }), ["No graphics card that can separate was found."]);
eq("an unknown reason shows nothing", why({ reason: "new-code" }), []);
eq("every reason in reasons has its line", why({ reason: "over-budget", reasons: ["over-budget", "disk-slow"], estimate_minutes_4min: 2 }), [
  "The best vocal model would take longer than the goal of 3 minutes for a 4-minute song, so the standard model is recommended.",
  "The StemData folder is slow to write to, which adds time after each separation.",
]);
eq("fits with a slow disk says only the disk", why({ reason: "disk-slow", reasons: ["fits", "disk-slow"] }), [en["settings.speedTest.reason.diskSlow"]]);
eq("gpu-slower-than-cpu reads its own sentence", why({ device: "cpu", reason: "gpu-slower-than-cpu" }), [en["settings.speedTest.reason.gpuSlower"]]);
check("no reason line says min", Object.entries(en).filter(([k]) => k.startsWith("settings.speedTest.reason.")).every(([, v]) => !/\bmin\b|\{minutes\}/.test(v)));
eq("disk speed", diskLines(result(), t, "en"), [{ text: "The StemData folder writes 812 MB/s.", warn: false }]);
eq("a slow disk adds a warning", diskLines(result({ disk: { write_mb_s: 22, skipped: null, slow: true } }), t, "en").map((l) => l.warn), [false, true]);
eq("an unknown skip code says it was not measured", diskLines(result({ disk: { write_mb_s: null, skipped: "new-code", slow: false } }), t, "en"),
  [{ text: en["settings.speedTest.diskSkipped"], warn: false }]);
eq("low space names the 512 MB floor", diskLines(result({ disk: { write_mb_s: null, skipped: "low-space", slow: false } }), t, "en")[0].text,
  "The StemData folder was not measured: it has less than 512 MB free.");
for (const [code, key] of [["unavailable", "unavailable"], ["name-taken", "nameTaken"], ["failed", "failed"], ["timeout", "failed"]]) {
  eq(`skip ${code}`, diskLines(result({ disk: { write_mb_s: null, skipped: code, slow: false } }), t, "en")[0].text, en[`settings.speedTest.diskSkip.${key}`]);
}

// Every key is in every table.
const KEYS = Object.keys(en).filter((k) => k.startsWith("settings.speedTest.") || k.startsWith("help.set.speedTest"));
check("the speed test has its keys", KEYS.length >= 52, String(KEYS.length));
for (const [lang, table] of Object.entries(TRANSLATIONS)) {
  if (lang === "pt-PT") continue;
  const missing = KEYS.filter((k) => !(k in table));
  check(`${lang} has every speed test key`, missing.length === 0, missing.join(", "));
}

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
