// Tags (#1075): named points on the timeline that a click seeks to.
//
// The pure parts (cleaning a stored list, which tag a digit key reaches, the
// same-spot rule for Add) and the save queue, with a document stub just large
// enough for the module, the way sections.test.mjs does it.
//
// Run:  node tests/js/timeline-tags.test.mjs

import { LANGUAGES, TRANSLATIONS } from "../../static/js/i18n.js";
import {
  SAME_SPOT_SEC,
  SHORTCUT_COUNT,
  destroyTimelineTags,
  flushTimelineTagsSave,
  formatTagTime,
  getTimelineTags,
  initTimelineTags,
  loopSpanForTag,
  matchingTagId,
  normalizeTags,
  seekToTagForKey,
  setTimelineTagsBridge,
  shortcutIndex,
  tagForKey,
  tagNear,
  tagShortcutIndex,
} from "../../static/js/timelineTags.js";

let pass = 0;
let fail = 0;
const check = (name, condition, detail = "") => {
  if (condition) {
    pass++;
    console.log(`PASS  ${name}`);
  } else {
    fail++;
    console.log(`FAIL  ${name}${detail ? "  -- " + detail : ""}`);
  }
};

// ── strings ──

const KEYS = [
  "tags.title", "tags.addTitle", "tags.defaultName", "tags.seekAria", "tags.seekKeyAria",
  "tags.deleteAria", "tags.renameAria", "tags.saveFailed",
  "tags.loopAria", "tags.loopEndAria", "tags.loopTooShort",
  "help.addTag", "help.tag", "help.tagDelete", "help.tagLoop",
];
for (const { code } of LANGUAGES) {
  const table = { ...TRANSLATIONS.pt, ...TRANSLATIONS[code] };
  const missing = KEYS.filter((key) => !table[key]);
  check(`${code} has every Tags string`, missing.length === 0, missing.join(", "));
  check(
    `${code} keeps the placeholders of the tag's label`,
    ["{name}", "{time}"].every((p) => table["tags.seekAria"].includes(p))
      && ["{name}", "{time}", "{key}"].every((p) => table["tags.seekKeyAria"].includes(p)),
  );
  check(
    `${code} keeps {name} in the loop button's labels`,
    ["tags.loopAria", "tags.loopEndAria", "tags.loopTooShort"].every((k) => table[k].includes("{name}")),
  );
}

// ── cleaning a stored list ──

const cleaned = normalizeTags([
  { id: "b", name: "Solo", time: 4.5 },
  { id: "a", name: "Verse two", time: 1.25 },
  { id: "a", name: "Same id again", time: 2 },
  { id: "bad id", name: "x", time: 1 },
  { id: "n", name: "Not a number", time: "soon" },
  { id: "neg", name: "Before the start", time: -1 },
  { id: "inf", name: "Forever", time: Infinity },
  null,
  "t1",
  { id: "u", time: 3 },
]);
check(
  "a stored list comes back in time order",
  cleaned.map((t) => t.id).join() === "a,u,b",
  JSON.stringify(cleaned),
);
check("a second entry with an id already seen is dropped", cleaned.filter((t) => t.id === "a").length === 1);
check("a tag with no name keeps an empty name for the bar to fill", cleaned.find((t) => t.id === "u").name === "");
check("anything that is not a list is no tags", normalizeTags(null).length === 0 && normalizeTags({}).length === 0);

// ── which tag a key reaches ──

check("1 is the first tag, 9 the ninth", shortcutIndex("Digit1") === 0 && shortcutIndex("Digit9") === 8);
check("the digit row reads the code, so AZERTY's & still counts", shortcutIndex("Digit1", "&") === 0);
check("the number pad counts too", shortcutIndex("Numpad3", "3") === 2);
check(
  "the number pad with NumLock off (End, ArrowDown, PageDown, ArrowLeft) reaches no tag",
  shortcutIndex("Numpad1", "End") === -1 && shortcutIndex("Numpad2", "ArrowDown") === -1
    && shortcutIndex("Numpad3", "PageDown") === -1 && shortcutIndex("Numpad4", "ArrowLeft") === -1,
);
check("0 reaches no tag", shortcutIndex("Digit0") === -1 && shortcutIndex("Numpad0") === -1);
check(
  "letters and other keys reach no tag",
  ["KeyL", "Space", "BracketLeft", "", undefined, "Digit10"].every((c) => shortcutIndex(c) === -1),
);
check("nine keys, no more", SHORTCUT_COUNT === 9);
const ordered = normalizeTags([
  { id: "late", name: "Late", time: 5 },
  { id: "early", name: "Early", time: 1 },
]);
check("keys follow time, not the order the tags were added", tagForKey(ordered, "Digit1").id === "early");
check("a key past the last tag reaches nothing", tagForKey(ordered, "Digit3") === null);

// ── the same spot ──

// ── when the digits act at all ──

const key = (code, extra = {}) => ({ code, key: code.replace(/^(Digit|Numpad)/, ""), target: null, ...extra });
check("a plain digit in the studio reaches its tag", tagShortcutIndex(key("Digit2")) === 1);
check("Shift with a digit reaches no tag", tagShortcutIndex(key("Digit1", { shiftKey: true, key: "!" })) === -1);
check("a held digit repeating reaches no tag", tagShortcutIndex(key("Digit1", { repeat: true })) === -1);
check("Ctrl, Cmd or Alt with a digit reaches no tag", ["ctrlKey", "metaKey", "altKey"].every(
  (mod) => tagShortcutIndex(key("Digit1", { [mod]: true })) === -1,
));
check("NumLock off reaches no tag", tagShortcutIndex(key("Numpad1", { key: "End" })) === -1);
check("NumLock on, the number pad reaches its tag", tagShortcutIndex(key("Numpad1")) === 0);
const field = { tagName: "INPUT", closest: (sel) => (sel.includes("input") ? field : null) };
check("a digit typed in a text field reaches no tag", tagShortcutIndex(key("Digit1", { target: field })) === -1);
for (const [what, state] of [
  ["a take is recording", { recording: true }],
  ["a count-in is running", { countIn: true }],
  ["Karaoke is open", { karaokeOpen: true }],
  ["Performance is open", { streamerOpen: true }],
  ["a dialog is open", { dialogOpen: true }],
  ["the Guide is open", { guideOpen: true }],
]) {
  check(`while ${what}, a digit reaches no tag`, tagShortcutIndex(key("Digit1"), state) === -1);
}
check(
  "with every state at rest, the digit still reaches its tag",
  tagShortcutIndex(key("Digit1"), {
    karaokeOpen: false, streamerOpen: false, dialogOpen: false, guideOpen: false, recording: false, countIn: false,
  }) === 0,
);

check("a tag within the tolerance is the same spot", tagNear(ordered, 1 + SAME_SPOT_SEC / 2)?.id === "early");
check("a tag just past it is not", tagNear(ordered, 1 + SAME_SPOT_SEC * 2) === null);

check("the time reads minutes, seconds and hundredths", formatTagTime(64.25) === "1:04.25");
check("a bad time reads as zero", formatTagTime("x") === "0:00.00");

// ── the DOM part, with a stub ──

globalThis.document = { getElementById: () => null };
const seeks = [];
setTimelineTagsBridge({ getTime: () => 0, seek: (time) => seeks.push(time) });

initTimelineTags("abcdefabcdef", [
  { id: "t2", name: "Chorus", time: 3 },
  { id: "t1", name: "Intro", time: 0.5 },
], 6);
check("the open song's tags are read back in time order", getTimelineTags().map((t) => t.id).join() === "t1,t2");
check("a digit with a tag is taken and seeks to it", seekToTagForKey(key("Digit2")) === true && seeks.at(-1) === 3);
check("a digit with no tag is left for anything else", seekToTagForKey(key("Digit5")) === false);
check(
  "a digit while recording is left alone and does not seek",
  seekToTagForKey(key("Digit1"), { recording: true }) === false && seeks.at(-1) === 3,
);
destroyTimelineTags();
check("with no song open, no digit is taken", seekToTagForKey(key("Digit1")) === false);

// Saves go out one after another, each with the list as it was.
const requests = [];
const complete = [];
globalThis.fetch = (url, options) => new Promise((resolve) => {
  requests.push({ url, body: JSON.parse(options.body) });
  complete.push(resolve);
});
initTimelineTags("abcdefabcdef", [{ id: "t1", name: "Intro", time: 0.5 }], 6);
const first = flushTimelineTagsSave();
await Promise.resolve();
initTimelineTags("abcdefabcdef", [{ id: "t9", name: "Bridge", time: 4 }], 6);
const second = flushTimelineTagsSave();
await Promise.resolve();
check("a second save waits for the first", requests.length === 1);
check("it goes to the song's own tags endpoint", requests[0].url === "/api/jobs/abcdefabcdef/timeline-tags");
complete[0]({ ok: true });
await first;
await Promise.resolve();
check("the newer list goes out after the first is answered", requests.length === 2);
check(
  "each save carries the list as it was when it was queued",
  requests[0].body.timeline_tags[0].id === "t1" && requests[1].body.timeline_tags[0].id === "t9",
);
complete[1]({ ok: true });
await second;
destroyTimelineTags();

// A marker added on song A, then song B opened from the library inside the
// 600 ms debounce. A library switch reaches initTimelineTags through
// wireUpAudio and never calls destroyPlayer, so only init can send A's edit.
// Before the fix the timer fired after the switch and sent B's own list for
// B, and A's new marker was never saved.
const sent = [];
globalThis.fetch = (url, options) => {
  sent.push({ url, body: JSON.parse(options.body) });
  return Promise.resolve({ ok: true });
};
let addClick = null;
const addBtn = { dataset: {}, addEventListener: (type, fn) => { if (type === "click") addClick = fn; } };
globalThis.document = { getElementById: (id) => (id === "tagsAddBtn" ? addBtn : null) };
setTimelineTagsBridge({ getTime: () => 2.5, seek: () => {} });
initTimelineTags("aaaaaaaaaaaa", [{ id: "a1", name: "Verse", time: 1 }], 6);
addClick();
check("Add on song A waits out the debounce", sent.length === 0);
initTimelineTags("bbbbbbbbbbbb", [{ id: "b1", name: "Solo", time: 3 }], 6);
// Longer than the debounce: a timer left running would have fired by now.
await new Promise((resolve) => setTimeout(resolve, 700));
check("opening song B sends A's pending save once", sent.length === 1, JSON.stringify(sent));
check("that save goes to song A's id", sent[0]?.url === "/api/jobs/aaaaaaaaaaaa/timeline-tags");
check(
  "it carries A's list with the new marker",
  sent[0]?.body.timeline_tags.map((tag) => `${tag.id === "a1" ? "a1" : "new"}@${tag.time}`).join() === "a1@1,new@2.5",
  JSON.stringify(sent[0]?.body),
);
check(
  "B's list is never sent for A",
  !sent.some((s) => s.url.includes("aaaaaaaaaaaa") && s.body.timeline_tags.some((tag) => tag.id === "b1")),
);
check("song B is open with its own list", getTimelineTags().map((tag) => tag.id).join() === "b1");
destroyTimelineTags();

// ── loop between markers ──

const loopTags = normalizeTags([
  { id: "m3", name: "Outro", time: 5 },
  { id: "m1", name: "Verse", time: 1 },
  { id: "m2", name: "Chorus", time: 3 },
]);
check(
  "a marker loops to the next one in time order",
  JSON.stringify(loopSpanForTag(loopTags, "m1", 6)) === JSON.stringify({ start: 1, end: 3, toEnd: false }),
);
check("the middle marker loops to the one after it", loopSpanForTag(loopTags, "m2", 6).end === 5);
check(
  "the last marker loops to the end of the song",
  JSON.stringify(loopSpanForTag(loopTags, "m3", 6)) === JSON.stringify({ start: 5, end: 6, toEnd: true }),
);
check(
  "a single marker loops to the end of the song",
  loopSpanForTag([{ id: "only", name: "", time: 2 }], "only", 6).end === 6,
);
check("a single marker on the first second still loops to the end", loopSpanForTag([{ id: "o", name: "", time: 0 }], "o", 6).start === 0);
check("an unknown id has no span", loopSpanForTag(loopTags, "nope", 6) === null);
check(
  "the last marker at the end of the song has no span to loop",
  loopSpanForTag([{ id: "e", name: "", time: 6 }], "e", 6) === null,
);
check(
  "the last marker past the end of the song has no span to loop",
  loopSpanForTag([{ id: "e", name: "", time: 7 }], "e", 6) === null,
);
check(
  "two markers closer than the shortest loop have no span between them",
  loopSpanForTag(normalizeTags([{ id: "a", name: "", time: 1 }, { id: "b", name: "", time: 1.1 }]), "a", 6) === null,
);
check(
  "a marker sharing another's exact time loops to the first one that is later",
  loopSpanForTag(normalizeTags([
    { id: "a", name: "", time: 1 }, { id: "b", name: "", time: 1 }, { id: "c", name: "", time: 4 },
  ]), "a", 6).end === 4,
);
check("a missing duration gives the last marker no span", loopSpanForTag(loopTags, "m3", NaN) === null);

const armed = (start, end, enabled = true) => ({ enabled, start, end });
check("the loop over a marker's span is that marker's loop", matchingTagId(loopTags, armed(1, 3), 6) === "m1");
check("the loop over the last marker's span is that marker's loop", matchingTagId(loopTags, armed(5, 6), 6) === "m3");
check("a loop that is off is no marker's loop", matchingTagId(loopTags, armed(1, 3, false), 6) === null);
check("a section's loop over other bounds is no marker's loop", matchingTagId(loopTags, armed(1, 4), 6) === null);
check("a loop edge dragged a little is no marker's loop", matchingTagId(loopTags, armed(1.5, 3), 6) === null);
check("a loop read back a hair off still matches", matchingTagId(loopTags, armed(1.0004, 2.9996), 6) === "m1");
check("no loop state is no marker's loop", matchingTagId(loopTags, undefined, 6) === null);

// The loop follows the markers: the bridge records what the bar asks of the
// transport, and getLoop answers with whatever it armed last.
{
  let loop = armed(1, 3);
  const calls = [];
  let nowTime = 2;
  globalThis.document = { getElementById: (id) => (id === "tagsAddBtn" ? addBtn : null) };
  setTimelineTagsBridge({
    getTime: () => nowTime,
    seek: () => {},
    getLoop: () => loop,
    setLoopRange: (start, end) => { calls.push(["set", start, end]); loop = armed(start, end); return true; },
    clearLoop: () => { calls.push(["clear"]); loop = armed(loop.start, loop.end, false); },
  });
  initTimelineTags("cccccccccccc", [
    { id: "m1", name: "Verse", time: 1 },
    { id: "m2", name: "Chorus", time: 3 },
  ], 6);
  addClick();
  check(
    "a marker added inside the looped span shortens the loop to it",
    calls.length === 1 && calls[0].join() === "set,1,2",
    JSON.stringify(calls),
  );
  check("the loop now matches the first marker's new span", matchingTagId(getTimelineTags(), loop, 6) === "m1");

  // A section's loop is not the marker bar's: adding a marker leaves it be.
  calls.length = 0;
  loop = armed(0.5, 2.5);
  nowTime = 4;
  addClick();
  check("a marker added while a section's loop is armed leaves that loop alone", calls.length === 0, JSON.stringify(calls));

  // Opening another song puts away a marker loop of this one, and only that.
  loop = armed(1, 2);
  calls.length = 0;
  initTimelineTags("dddddddddddd", [{ id: "x", name: "", time: 0 }], 6);
  check("opening another song clears a marker loop of the song before", calls.join("|") === "clear", JSON.stringify(calls));
  calls.length = 0;
  loop = armed(0.5, 2.5);
  initTimelineTags("eeeeeeeeeeee", [{ id: "y", name: "", time: 2 }], 6);
  check("opening another song leaves a loop that was not a marker's", calls.length === 0, JSON.stringify(calls));
  loop = armed(2, 6);
  calls.length = 0;
  destroyTimelineTags();
  check("closing the studio clears a marker loop", calls.join("|") === "clear", JSON.stringify(calls));
}
setTimelineTagsBridge(null);

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
