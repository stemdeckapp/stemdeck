// The reason a failed request or a caught error gives (errorReason.js). The
// "Failed to restore track:" toast ended at the colon when either came back
// empty, so every shape that used to give "" is checked here.
//
// Run:  node tests/js/error-reason.test.mjs

import { codeMessage, errorReason, JOB_BUSY, RESTART_REQUIRED, responseErrorMessage } from "../../static/js/errorReason.js";
import { TRANSLATIONS } from "../../static/js/i18n.js";

import { failureCause, importFailure } from "../../static/js/errorReason.js";

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
const eq = (name, actual, expected) => check(name, actual === expected, `got ${JSON.stringify(actual)}, want ${JSON.stringify(expected)}`);

const res = (status, statusText) => ({ status, statusText, ok: false });

// responseErrorMessage
eq("string detail wins", responseErrorMessage(res(503, "Service Unavailable"), { detail: "Queue is full" }), "Queue is full");
eq("blank detail falls to the status text", responseErrorMessage(res(422, "Unprocessable Entity"), { detail: "  " }), "Unprocessable Entity");
eq(
  "a list detail (FastAPI validation) gives its messages",
  responseErrorMessage(res(422, ""), { detail: [{ msg: "field required" }, { msg: "bad url" }] }),
  "field required; bad url",
);
// The old code: new Error([]) has the message "".
check("old code: an empty list detail gave an empty message", new Error([]).message === "");
eq("an empty list detail falls to the status text", responseErrorMessage(res(422, "Unprocessable Entity"), { detail: [] }), "Unprocessable Entity");
eq("no detail and no status text (HTTP/2) gives the code", responseErrorMessage(res(502, ""), {}), "HTTP 502");
eq("a body that was not JSON gives the code", responseErrorMessage(res(500, ""), null), "HTTP 500");
eq("an object detail is not shown as [object Object]", responseErrorMessage(res(400, ""), { detail: { code: 1 } }), "HTTP 400");
eq("no response at all is empty", responseErrorMessage(undefined, undefined), "");

// A write refused between a stems folder move and the restart: the sentence
// comes from the language tables, not the server's English detail.
const refused = { detail: "server words", code: "restart_required" };
eq("the code the server sends", RESTART_REQUIRED, "restart_required");
eq(
  "a refused write after a move gives the translated sentence",
  responseErrorMessage(res(409, "Conflict"), refused),
  TRANSLATIONS.en["settings.stemsLocation.restartToSave"],
);
eq("an unknown code still gives the detail", responseErrorMessage(res(409, ""), { detail: "busy", code: "other" }), "busy");

// A delete, a library move or a reset refused while a stem fill or a vocal
// split runs: its own sentence, never the restart one.
const busy = { detail: "server words", code: "job_busy" };
eq("the busy code the server sends", JOB_BUSY, "job_busy");
eq("a busy refusal gives the translated sentence", responseErrorMessage(res(409, "Conflict"), busy), TRANSLATIONS.en["library.songBusy"]);
eq("codeMessage gives the same sentence", codeMessage(busy), TRANSLATIONS.en["library.songBusy"]);
check("the busy sentence is not the restart one", codeMessage(busy) !== codeMessage(refused));
eq("codeMessage is empty for an unknown code", codeMessage({ detail: "busy", code: "other" }), "");
eq("codeMessage is empty for no body", codeMessage(null), "");
for (const lang of ["pl", "ja", "zh-Hans", "de", "pt", "pt-PT", "id", "fr", "es", "ko"]) {
  const text = TRANSLATIONS[lang]?.["library.songBusy"];
  check(`the busy sentence is translated in ${lang}`, typeof text === "string" && text && text !== TRANSLATIONS.en["library.songBusy"]);
}
check("pt-PT has its own busy sentence", TRANSLATIONS["pt-PT"]["library.songBusy"] !== TRANSLATIONS.pt["library.songBusy"]);

// errorReason
eq("an Error's message", errorReason(new Error("Failed to fetch")), "Failed to fetch");
eq("an Error with no message is empty", errorReason(new Error("")), "");
eq("a thrown string", errorReason("  offline "), "offline");
eq("a TypeError with no message is empty, not its name", errorReason(new TypeError("")), "");
eq("undefined is empty", errorReason(undefined), "");
eq("null is empty", errorReason(null), "");
eq("a number is empty", errorReason(42), "");

// A failed job's error_detail (_quarantine_failed_job in app/pipeline/runner.py)
// is the classified cause, a dash, then the server's own redacted words. The
// cause key was shown as it is, in English ("source-blocked"), with no
// sentence a person could act on (#175).
const DASH = String.fromCharCode(0x2014);
const detail = (cause, words) => `${cause} ${DASH} ${words}`;
const parsed = (value) => JSON.stringify(failureCause(value));
const same = (name, value, want) => eq(name, parsed(value), JSON.stringify(want));

same("a detail gives its cause and its words", detail("source-blocked", "Sign in to confirm"), { cause: "source-blocked", message: "Sign in to confirm" });
same("a dash inside the words is kept", detail("bad-input", "ffmpeg - exit 1"), { cause: "bad-input", message: "ffmpeg - exit 1" });
same("a bare cause has no words", "unknown", { cause: "unknown", message: "" });
same("a detail that names no known cause is all words", "something broke", { cause: "", message: "something broke" });
same("an unknown word in the cause place is not a cause", detail("gremlins", "x"), { cause: "", message: detail("gremlins", "x") });
same("an object's own property name is not a cause", "constructor", { cause: "", message: "constructor" });
same("no detail", null, { cause: "", message: "" });
same("a blank detail", "  ", { cause: "", message: "" });
same("a number is no detail", 42, { cause: "", message: "" });

const CAUSE_KEYS = {
  "out-of-memory": "job.cause.outOfMemory",
  "unsupported-device": "job.cause.unsupportedDevice",
  "disk-full": "job.cause.diskFull",
  "source-blocked": "job.cause.sourceBlocked",
  "source-unavailable": "job.cause.sourceUnavailable",
  "bad-input": "job.cause.badInput",
  unknown: "job.cause.unknown",
};
const LINK = "https://www.youtube.com/watch?v=dQw4w9WgXcQ";
const SERVER_WORDS = "Audio processing failed. Please try another video.";
const failedLink = (errorDetail, extra = {}) => ({ status: "error", error: SERVER_WORDS, error_detail: errorDetail, source_url: LINK, ...extra });
const en = TRANSLATIONS.en;

for (const [cause, key] of Object.entries(CAUSE_KEYS)) {
  const out = importFailure(failedLink(detail(cause, "the last line")));
  eq(`a link import that failed with ${cause} says its sentence`, out.message, en[key]);
  eq(`a link import that failed with ${cause} keeps the words as its detail`, out.detail, "the last line");
  for (const lang of ["pl", "ja", "zh-Hans", "de", "pt", "id", "fr", "es", "ko"]) {
    const text = TRANSLATIONS[lang]?.[key];
    check(`${key} is translated in ${lang}`, typeof text === "string" && text && text !== en[key]);
  }
}
check("the server's own words are never the sentence", !Object.values(CAUSE_KEYS).some((key) => en[key] === SERVER_WORDS));

eq("a bare unknown still says a sentence", importFailure(failedLink("unknown")).message, en["job.cause.unknown"]);
eq("a bare unknown has no detail line", importFailure(failedLink("unknown")).detail, "");

const upload = importFailure(failedLink(detail("out-of-memory", "CUDA out of memory"), { source_url: "local:My Song" }));
eq("a failed upload says its cause and to add the file again", upload.message, en["job.uploadFailed"].replace("{cause}", en["job.cause.outOfMemory"]));
eq("a failed upload keeps the words as its detail", upload.detail, "CUDA out of memory");
for (const lang of ["pl", "ja", "zh-Hans", "de", "pt", "id", "fr", "es", "ko"]) {
  const text = TRANSLATIONS[lang]?.["job.uploadFailed"];
  check(`job.uploadFailed is translated in ${lang}, with its {cause}`, typeof text === "string" && text.includes("{cause}") && text !== en["job.uploadFailed"]);
}
eq("a job with no source is not called an upload", importFailure(failedLink(detail("disk-full", "x"), { source_url: null })).message, en["job.cause.diskFull"]);
check("pt-PT says ficheiro, not arquivo", /ficheiro/.test(TRANSLATIONS["pt-PT"]["job.uploadFailed"] || ""));

// A failure the pipeline did not classify (a job lost to two restarts, an
// upload whose file went missing) has no error_detail: the server's words stay.
const interrupted = "Interrupted by a restart twice. Import it again to retry.";
eq("no detail keeps the server's words", importFailure({ status: "error", error: interrupted, source_url: LINK }).message, interrupted);
eq("no detail has no detail line", importFailure({ status: "error", error: interrupted, source_url: LINK }).detail, "");
const missing = "The uploaded file is no longer available. Re-import it to finish.";
eq("an unclassified upload failure keeps the server's words", importFailure({ status: "error", error: missing, source_url: "local:x" }).message, missing);
eq("no words at all says processing failed", importFailure({ status: "error" }).message, en["job.audioProcessingFailed"]);
eq("a detail with no known cause is shown as the detail", importFailure(failedLink("something broke")).detail, "something broke");
eq("a detail with no known cause keeps the server's words", importFailure(failedLink("something broke")).message, SERVER_WORDS);

console.log(`\n${pass}/${pass + fail} checks passed`);
process.exit(fail ? 1 : 0);
