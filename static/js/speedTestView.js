// Settings > Separation, "Benchmark": what its lines and table say and what
// "Apply recommendation" sends. Pure functions over the GET /api/speed-test
// answer and its result (also `speed_test` in GET /api/settings), with `t`
// (i18n.t) passed in, so tests/js/speed-test-view.test.mjs runs them without
// a browser. separationSettings.js draws what these return.
//
// The server side is app/core/speed_test.py. A result measures htdemucs_6s on
// each device and estimates the models that are not downloaded by scaling
// their registry speed with the same machine factor; `measured: false` marks
// those, and the table says "estimate" so a guess never reads as a fact.
//
// Every time is said for a typical 4-minute song, in plain words ("about 15
// seconds", "about 3 minutes"), with a rating word beside it: the speed
// factor itself is only the cell's tooltip and accessible name.

import { CHOICE_STEMS, SONG_SECONDS, formatNumber } from "./stemModelsView.js";

/** Under this many seconds for the 4-minute song a row rates Fast. */
export const FAST_MAX_SECONDS = 30;
/** Up to this many seconds a row rates OK; over it, Slow. */
export const OK_MAX_SECONDS = 180;
/** Under this many seconds the time reads "a few seconds". */
export const FEW_SECONDS = 5;
/** From this many seconds the time rounds to whole minutes. */
export const MINUTES_FROM_SECONDS = 90;
/** From this many seconds (and under a minute and a half) it rounds to 5 s. */
export const FIVES_FROM_SECONDS = 30;
/** The model whose pass makes a plan "the best vocal model plus the standard one". */
const KIM = "kim_mel_roformer";

/** Devices in the order the table lists them. */
export const DEVICE_ORDER = ["cuda", "mps", "cpu"];
/** States the poll stops on. */
export const FINAL_STATES = ["idle", "done", "failed", "cancelled"];
/** How often the page asks how a running test is going. */
export const POLL_MS = 1000;
/** Polls in a row that may fail before the page stops asking and says so. */
export const POLL_FAIL_LIMIT = 8;

const isObj = (v) => v !== null && typeof v === "object" && !Array.isArray(v);

/** Whether a GET /api/speed-test answer is one this page can read. */
export function isSpeedTestState(s) {
  return isObj(s) && typeof s.state === "string";
}

export function isRunning(s) {
  return isSpeedTestState(s) && s.state === "running";
}

/** The progress, 0 to 100, whole numbers. */
export function progressPct(s) {
  const pct = Number(s?.pct);
  if (!Number.isFinite(pct)) return 0;
  return Math.max(0, Math.min(100, Math.round(pct)));
}

/** A device as the Compute device select names it. Not translated: names. */
export function deviceLabel(device, rocm = false) {
  if (device === "cuda") return rocm ? "ROCm (AMD)" : "CUDA (NVIDIA)";
  if (device === "mps") return "MPS (Apple Silicon)";
  if (device === "cpu") return "CPU";
  return String(device ?? "");
}

/** "Benchmarking CUDA (NVIDIA)", "Benchmarking the StemData folder", or "" before a step starts. */
export function runningLine(s, t, rocm = false) {
  if (!isRunning(s) || !s.step) return "";
  const step = s.step === "disk" ? t("settings.speedTest.stepDisk") : deviceLabel(s.step, rocm);
  return t("settings.speedTest.running", { step });
}

/** Why a whole test failed (GET /api/speed-test error_code), as its sentence key. */
const ERROR_KEY = {
  "model-missing": "settings.speedTest.error.modelMissing",
  "torch-missing": "settings.speedTest.error.torchMissing",
  timeout: "settings.speedTest.error.timeout",
  stalled: "settings.speedTest.error.stalled",
  failed: "settings.speedTest.error.failed",
};

/** The status line once a test has stopped: failed (with the server's reason
 *  when this page knows its code) or cancelled, else null. */
export function endLine(s, t) {
  if (s?.state === "failed") {
    const key = ERROR_KEY[s.error_code] || "settings.speedTest.failed";
    return { text: t(key), error: true };
  }
  if (s?.state === "cancelled") return { text: t("settings.speedTest.cancelled"), error: false };
  return null;
}

/** Whether a stored result can be shown at all. */
export function isResult(r) {
  return isObj(r) && isObj(r.devices) && isObj(r.recommendation);
}

export function isStale(r) {
  return isResult(r) && r.stale === true;
}

/** Whether "Apply recommendation" is offered: a current result naming a device. */
export function canApply(r) {
  return isResult(r) && !isStale(r) && typeof r.recommendation.device === "string" && r.recommendation.device !== "";
}

/** The body of "Apply recommendation": the device, and every stem on "auto",
 *  which then resolves through this result on this hardware. */
export function applyPatch(r) {
  return {
    demucs_device: r.recommendation.device,
    stem_models: Object.fromEntries(CHOICE_STEMS.map((s) => [s, "auto"])),
  };
}

/** Minutes a 4 minute song takes at a real-time factor. */
export function minutesFor(rtf) {
  return (rtf * SONG_SECONDS) / 60;
}

/** Seconds a 4 minute song takes at a real-time factor. */
export function secondsFor(rtf) {
  return rtf * SONG_SECONDS;
}

/**
 * A duration rounded the way it is said: null under FEW_SECONDS ("a few
 * seconds"), whole seconds up to FIVES_FROM_SECONDS, the nearest 5 seconds
 * up to MINUTES_FROM_SECONDS, then the nearest whole minute.
 */
export function roundDuration(seconds) {
  const s = Number(seconds);
  if (!Number.isFinite(s) || s < FEW_SECONDS) return null;
  if (s < FIVES_FROM_SECONDS) return { unit: "second", value: Math.round(s) };
  if (s < MINUTES_FROM_SECONDS) return { unit: "second", value: Math.round(s / 5) * 5 };
  return { unit: "minute", value: Math.round(s / 60) };
}

/**
 * The rounded duration in `lang`, with the unit in words and its plural form
 * from Intl ("15 seconds", "2 minuty", "15 秒"), or null under FEW_SECONDS.
 */
export function formatDuration(seconds, lang) {
  const d = roundDuration(seconds);
  if (!d) return null;
  try {
    return new Intl.NumberFormat(lang || undefined, { style: "unit", unit: d.unit, unitDisplay: "long" }).format(d.value);
  } catch {
    return `${d.value} ${d.unit}${d.value === 1 ? "" : "s"}`;
  }
}

/** "a few seconds", "about 15 seconds", or with `estimated` "an estimated 15 seconds". */
export function durationText(seconds, t, lang, { estimated = false } = {}) {
  const duration = formatDuration(seconds, lang);
  if (duration === null) return t("settings.speedTest.duration.few");
  return t(estimated ? "settings.speedTest.duration.estimated" : "settings.speedTest.duration.about", { duration });
}

/** "fast" under FAST_MAX_SECONDS, "ok" up to OK_MAX_SECONDS, else "slow". */
export function ratingFor(seconds) {
  if (seconds < FAST_MAX_SECONDS) return "fast";
  if (seconds <= OK_MAX_SECONDS) return "ok";
  return "slow";
}

/** The speed factor in words: "38.5 times faster than the song's length". */
export function factorText(rtf, t, locale) {
  if (rtf < 1) return t("settings.speedTest.factor.faster", { x: formatNumber(1 / rtf, locale, 1) });
  return t("settings.speedTest.factor.slower", { x: formatNumber(rtf, locale, 1) });
}

/** A device for a person: "your graphics card (NVIDIA)", "your processor". */
export function friendlyDevice(device, t, rocm = false) {
  if (device === "cuda") return t(rocm ? "settings.speedTest.device.rocm" : "settings.speedTest.device.cuda");
  if (device === "mps") return t("settings.speedTest.device.mps");
  if (device === "cpu") return t("settings.speedTest.device.cpu");
  return String(device ?? "");
}

/** A device's name in the table: "Graphics card", "Processor". */
export function deviceName(device, t) {
  if (device === "cuda") return t("settings.speedTest.deviceName.cuda");
  if (device === "mps") return t("settings.speedTest.deviceName.mps");
  if (device === "cpu") return t("settings.speedTest.deviceName.cpu");
  return String(device ?? "");
}

/** The models the recommendation uses, as a set of ids. */
function recommendedModels(r) {
  return new Set(Object.values(r.recommendation?.stems || {}).filter((v) => typeof v === "string"));
}

/**
 * The table rows: one per device and model in the result, the recommended
 * device first, models in the catalog's order.
 *   { device, deviceText, deviceName, model, modelText, measured, recommended,
 *     other, rtf, seconds, timeText, rating, ratingText, factorText }
 * `other` marks a device that is not the recommended one: the page hides
 * those rows until "Show all devices". `catalog` is stem_model_catalog (for
 * model names); either may be absent.
 */
export function resultRows(r, { t, locale, catalog = null, rocm = false } = {}) {
  if (!isResult(r)) return [];
  const names = catalog?.models || {};
  const order = Object.keys(names);
  const rank = (id) => {
    const i = order.indexOf(id);
    return i < 0 ? order.length : i;
  };
  const recDevice = r.recommendation.device;
  const recModels = recommendedModels(r);
  const devices = Object.keys(r.devices).sort((a, b) => {
    if (a === recDevice) return -1;
    if (b === recDevice) return 1;
    const ia = DEVICE_ORDER.indexOf(a), ib = DEVICE_ORDER.indexOf(b);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
  });
  const rows = [];
  for (const device of devices) {
    const models = r.devices[device];
    if (!isObj(models)) continue;
    const ids = Object.keys(models).sort((a, b) => rank(a) - rank(b));
    for (const model of ids) {
      const m = models[model];
      const rtf = Number(m?.rtf);
      if (!Number.isFinite(rtf) || rtf <= 0) continue;
      const measured = m.measured === true;
      const recommended = device === recDevice && recModels.has(model);
      const tags = [t(measured ? "settings.speedTest.measured" : "settings.speedTest.estimated")];
      if (recommended) tags.unshift(t("settings.stemModels.recommendedTag"));
      const name = names[model]?.name || model;
      const seconds = secondsFor(rtf);
      const rating = ratingFor(seconds);
      rows.push({
        device,
        deviceText: deviceLabel(device, rocm),
        deviceName: deviceName(device, t),
        model,
        modelText: `${name} (${tags.join(", ")})`,
        measured,
        recommended,
        other: typeof recDevice === "string" && recDevice !== "" && device !== recDevice,
        rtf,
        seconds,
        timeText: durationText(seconds, t, locale),
        rating,
        ratingText: t(`settings.speedTest.rating.${rating}`),
        factorText: factorText(rtf, t, locale),
      });
    }
  }
  return rows;
}

/** Why one device could not be tested (result.failed), as its sentence key. */
const DEVICE_FAIL_KEY = {
  timeout: "settings.speedTest.failedReason.timeout",
  stalled: "settings.speedTest.failedReason.stalled",
  "device-unavailable": "settings.speedTest.failedReason.deviceUnavailable",
  "model-missing": "settings.speedTest.failedReason.modelMissing",
  "torch-missing": "settings.speedTest.failedReason.torchMissing",
  failed: "settings.speedTest.failedReason.failed",
};

/** The devices the test could not measure: { device, deviceText, code, text }. */
export function failedRows(r, t, rocm = false) {
  if (!isObj(r?.failed)) return [];
  const devices = Object.keys(r.failed).sort((a, b) => {
    const ia = DEVICE_ORDER.indexOf(a), ib = DEVICE_ORDER.indexOf(b);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
  });
  return devices.map((device) => {
    const code = String(r.failed[device] ?? "");
    const deviceText = deviceLabel(device, rocm);
    const reason = t(DEVICE_FAIL_KEY[code] || DEVICE_FAIL_KEY.failed);
    return { device, deviceText, code, text: t("settings.speedTest.failedDevice", { device: deviceText, reason }) };
  });
}

/** Whether a device other than the processor failed in this result. */
export function gpuFailed(r) {
  return isObj(r?.failed) && Object.keys(r.failed).some((d) => d !== "cpu");
}

/** Whether the recommended plan rests on a model the test did not time on
 *  that device: then its time is an estimate and is said as one. */
export function planEstimated(r) {
  if (!isResult(r)) return false;
  const table = r.devices[r.recommendation.device];
  const models = recommendedModels(r);
  if (!isObj(table) || !models.size) return true;
  return [...models].some((id) => table[id]?.measured !== true);
}

/**
 * The headline: how long a 4-minute song takes with the recommendation, and
 * what is recommended, in words: "Your computer separates a 4-minute song in
 * about 16 seconds. Recommended: your graphics card (NVIDIA), with the best
 * vocal model plus the standard model." Null without a device or a time.
 */
export function headlineLine(r, t, locale, rocm = false) {
  if (!isResult(r)) return null;
  const rec = r.recommendation;
  // Number(null) is 0: a missing time must not read as "a few seconds".
  const minutes = typeof rec.estimate_minutes_4min === "number" ? rec.estimate_minutes_4min : NaN;
  if (typeof rec.device !== "string" || rec.device === "" || !Number.isFinite(minutes) || minutes < 0) return null;
  const models = recommendedModels(r);
  return t("settings.speedTest.headline", {
    duration: durationText(minutes * 60, t, locale, { estimated: planEstimated(r) }),
    device: friendlyDevice(rec.device, t, rocm),
    models: t(models.has(KIM) ? "settings.speedTest.models.best" : "settings.speedTest.models.standard"),
  });
}

const REASON_KEY = {
  "over-budget": "settings.speedTest.reason.overBudget",
  "gpu-slower-than-cpu": "settings.speedTest.reason.gpuSlower",
  "disk-slow": "settings.speedTest.reason.diskSlow",
  "no-gpu": "settings.speedTest.reason.noGpu",
  "gpu-failed": "settings.speedTest.reason.gpuFailed",
  "cpu-over-budget": "settings.speedTest.reason.cpuOverBudget",
};

/**
 * Why the recommendation is what it is, one sentence per reason, under the
 * headline: none when the plan simply fits. Reasons this page does not know
 * are left out.
 */
export function reasonLines(r, t, locale) {
  if (!isResult(r)) return [];
  const rec = r.recommendation;
  const minutes = Number(rec.estimate_minutes_4min);
  const budget = Number(rec.budget_minutes);
  const codes = Array.isArray(rec.reasons) && rec.reasons.length ? rec.reasons : [rec.reason];
  const out = [];
  for (let code of codes) {
    // "No graphics card" is false when one was there and failed.
    if (code === "no-gpu" && gpuFailed(r)) code = "gpu-failed";
    // "Over the goal with the best vocal model, the standard one fits" only
    // when the plan it recommends does fit; otherwise even that is over.
    if (code === "over-budget" && Number.isFinite(minutes) && Number.isFinite(budget) && minutes > budget) code = "cpu-over-budget";
    const key = REASON_KEY[code];
    if (!key) continue;
    const line = t(key, { budget: Number.isFinite(budget) ? formatDuration(budget * 60, locale) ?? "?" : "?" });
    if (!out.includes(line)) out.push(line);
  }
  return out;
}

/** The jobs folder line: its write speed and whether it is slow, or why it was not measured. */
const DISK_SKIP_KEY = {
  "low-space": "settings.speedTest.diskSkip.lowSpace",
  unavailable: "settings.speedTest.diskSkip.unavailable",
  "name-taken": "settings.speedTest.diskSkip.nameTaken",
  failed: "settings.speedTest.diskSkip.failed",
  timeout: "settings.speedTest.diskSkip.failed",
};

export function diskLines(r, t, locale) {
  const disk = r?.disk;
  if (!isObj(disk)) return [];
  const mbs = Number(disk.write_mb_s);
  if (disk.skipped || !Number.isFinite(mbs)) {
    return [{ text: t(DISK_SKIP_KEY[disk.skipped] || "settings.speedTest.diskSkipped"), warn: false }];
  }
  const lines = [{ text: t("settings.speedTest.disk", { mbs: formatNumber(mbs, locale, 0) }), warn: false }];
  if (disk.slow === true) lines.push({ text: t("settings.speedTest.diskSlow"), warn: true });
  return lines;
}
