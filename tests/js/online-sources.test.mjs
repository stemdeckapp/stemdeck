// Online sources (#1089): the page's copy of the setting, and the two answers
// it reads it from. Off unless the server says a JSON true; a refusal is told
// apart by its code alone; a link is told apart from a search the way the
// search box always has.
//
// Run:  node tests/js/online-sources.test.mjs

import { codeMessage, importFailure, ONLINE_SOURCES_OFF, ONLINE_SOURCES_OFF_CAUSE, stoppedForOnlineSources } from "../../static/js/errorReason.js";
import { TRANSLATIONS } from "../../static/js/i18n.js";
import {
  isOnlineSourcesRefusal,
  looksLikeLink,
  onOnlineSourcesChange,
  onlineSourcesFrom,
  onlineSourcesKnown,
  onlineSourcesOn,
  setOnlineSourcesState,
} from "../../static/js/onlineSources.js";

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

// onlineSourcesFrom: only a JSON true is on.
eq("true is on", onlineSourcesFrom({ online_sources: true }), true);
eq("false is off", onlineSourcesFrom({ online_sources: false }), false);
eq("a settings answer without the key is off", onlineSourcesFrom({ cookies_file: null }), false);
eq("the string true is off", onlineSourcesFrom({ online_sources: "true" }), false);
eq("1 is off", onlineSourcesFrom({ online_sources: 1 }), false);
eq("no answer at all is off", onlineSourcesFrom(null), false);
eq("undefined is off", onlineSourcesFrom(undefined), false);

// isOnlineSourcesRefusal: the server's code, nothing else.
eq("the code is the server's", ONLINE_SOURCES_OFF, "online_sources_off");
eq("the refusal is recognised", isOnlineSourcesRefusal({ detail: "x", code: "online_sources_off" }), true);
eq("another code is not it", isOnlineSourcesRefusal({ code: "job_busy" }), false);
eq("the words alone are not it", isOnlineSourcesRefusal({ detail: "Online search and link import are off." }), false);
eq("a body that was not JSON is not it", isOnlineSourcesRefusal(null), false);

// looksLikeLink: what the box hands to the import rather than to search.
for (const text of [
  "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
  "  http://example.com/song",
  "www.youtube.com/watch?v=abc",
  "youtu.be/dQw4w9WgXcQ",
  "soundcloud.com/artist/track",
  "see youtube.com",
]) {
  eq(`a link: ${text}`, looksLikeLink(text), true);
}
for (const text of ["daft punk around the world", "", "   ", "youtubers react", null, undefined]) {
  eq(`a search: ${JSON.stringify(text)}`, looksLikeLink(text), false);
}

// The page's copy: off until told, and listeners hear each change once.
eq("off until the server says otherwise", onlineSourcesOn(), false);
const heard = [];
const stop = onOnlineSourcesChange((on) => heard.push(on));
setOnlineSourcesState(true);
setOnlineSourcesState(true);
setOnlineSourcesState("true");
eq("a string does not count as on", onlineSourcesOn(), false);
setOnlineSourcesState(false);
setOnlineSourcesState(true);
check("each change is heard once, a repeat is not", JSON.stringify(heard) === JSON.stringify([true, false, true]), JSON.stringify(heard));
const throwing = onOnlineSourcesChange(() => {
  throw new Error("listener failed");
});
const origWarn = console.warn;
console.warn = () => {};
setOnlineSourcesState(false);
console.warn = origWarn;
check("a listener that throws does not stop the others", JSON.stringify(heard) === JSON.stringify([true, false, true, false]), JSON.stringify(heard));
throwing();
stop();
setOnlineSourcesState(true);
eq("a stopped listener hears nothing more", heard.length, 4);
setOnlineSourcesState(false);

// The words the page shows for the refusal and for a job it failed.
eq("the refusal is said in the chosen language", codeMessage({ code: "online_sources_off" }), TRANSLATIONS.en["onlineSources.off"]);
const failed = importFailure({
  error: "Audio processing failed.",
  error_detail: "online-sources-off - Online search and link import are off. Turn them on in Settings, Experimental.",
  source_url: "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
});
eq("a queued link refused by the pipeline names the cause", failed.message, TRANSLATIONS.en["job.cause.onlineSourcesOff"]);

// A link whose turn came while they were off waits as stopped, with the
// cause code alone (app/pipeline/runner.py _wait_for_online_sources).
eq("the cause is the server's", ONLINE_SOURCES_OFF_CAUSE, "online-sources-off");
eq("a stopped link with the cause is one", stoppedForOnlineSources({ status: "stopped", error_detail: "online-sources-off" }), true);
eq("a stopped job without it is not", stoppedForOnlineSources({ status: "stopped", error_detail: null }), false);
eq("a failed job with it is not", stoppedForOnlineSources({ status: "error", error_detail: "online-sources-off" }), false);
eq("no state is not", stoppedForOnlineSources(null), false);

// onlineSourcesKnown: asks the server once, and later calls wait for that
// first answer instead of asking again, or refusing before it arrives.
let asked = 0;
globalThis.fetch = async () => {
  asked += 1;
  return { ok: true, json: async () => ({ online_sources: true }) };
};
eq("the first answer is waited for", await onlineSourcesKnown(), true);
eq("the server is asked once", asked, 1);
await onlineSourcesKnown();
eq("and not again", asked, 1);
setOnlineSourcesState(false);
eq("a later change is what it answers", await onlineSourcesKnown(), false);

// Every new sentence exists in every table, and none carries an em dash.
const EM_DASH = String.fromCharCode(0x2014);
const KEYS = [
  "onlineSources.off",
  "job.cause.onlineSourcesOff",
  "topbar.urlPlaceholderOff",
  "settings.tab.experimental",
  "settings.onlineSources.disclaimer",
  "settings.onlineSources.accept",
  "settings.onlineSources.acceptFirst",
  "mobile.onlineSourcesOff",
  "mobile.chooseFileFirst",
];
for (const [lang, table] of Object.entries(TRANSLATIONS)) {
  if (lang === "pt-PT") continue; // a variant: falls back to pt
  for (const key of KEYS) {
    check(`${lang} has ${key}`, typeof table[key] === "string" && table[key].trim() !== "");
    check(`${lang} ${key} has no em dash`, !String(table[key]).includes(EM_DASH));
  }
}

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
