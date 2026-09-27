// The Lyrics tab's lookup (#699): how LRC text becomes timed lines, which line
// is being sung at a given moment, which of LRCLIB's many versions of a song
// is taken as the one, and what is sent to search for it.
//
// No network. searchLyrics takes its fetch as an argument.
//
// Run:  node tests/js/lyrics-lookup.test.mjs

import {
  parseLrc,
  currentLineIndex,
  rankMatches,
  searchUrl,
  searchLyrics,
  wordTimings,
  voicedPhrases,
  syllables,
  songFromTitle,
} from "../../static/js/lyricsLookup.js";

let pass = 0,
  fail = 0;
const check = (name, cond, detail = "") => {
  if (cond) {
    pass++;
  } else {
    fail++;
    console.error(`FAIL  ${name}${detail ? `\n      ${detail}` : ""}`);
  }
};
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// ── parseLrc ──
const lrc = [
  "[ar:Dream Theater]",
  "[ti:Metropolis]",
  "[01:58.63] Arrived early May",
  "[01:55.10] The smile of dawn",
  "[00:12.00][01:40.5]Chorus",
  "[02:01] ",
  "no stamp, not a line",
].join("\n");
const parsed = parseLrc(lrc);
check(
  "timed lines, earliest first, one per stamp, header tags and bare text skipped",
  same(parsed, [
    { time: 12, text: "Chorus" },
    { time: 100.5, text: "Chorus" },
    { time: 115.1, text: "The smile of dawn" },
    { time: 118.63, text: "Arrived early May" },
    { time: 121, text: "" },
  ]),
  JSON.stringify(parsed),
);
check(
  "a positive offset makes every line earlier",
  same(parseLrc("[offset:+500]\n[00:10.00]Line").map((l) => l.time), [9.5]),
);
check("a stamp before zero after the offset stays at zero", parseLrc("[offset:+2000]\n[00:01.00]x")[0].time === 0);
check("CRLF line endings", parseLrc("[00:01.00]a\r\n[00:02.00]b").length === 2);
check("nothing gives nothing", same(parseLrc(""), []) && same(parseLrc(null), []));

// ── enhanced LRC: word stamps ──
const enhanced = parseLrc("[00:12.00]<00:12.00>Hello <00:12.60>world<00:13.20>");
check("a line with word stamps keeps its plain text", enhanced[0].text === "Hello world");
check(
  "and its words, each with its stamp",
  same(enhanced[0].words.map((w) => [w.time, w.text]), [[12, "Hello "], [12.6, "world"], [13.2, ""]]),
);
check(
  "word stamps follow the offset too",
  parseLrc(["[offset:+1000]", "[00:12.00]<00:12.00>a <00:13.00>b"].join("\n"))[0].words[1].time === 12,
);

// ── wordTimings ──
check(
  "stamped words run from their stamp to the next",
  same(wordTimings(enhanced[0], 20), [
    { text: "Hello ", start: 12, end: 12.6 },
    { text: "world", start: 12.6, end: 13.2 },
  ]),
);
const even = wordTimings({ time: 10, text: "The smile of dawn" }, 20);
check("unstamped words give back the line", even.map((w) => w.text).join("") === "The smile of dawn");
check("start where the line does, in order", even[0].start === 10 && even.every((w, i) => i === 0 || w.start >= even[i - 1].end - 1e-9));
const shared = wordTimings({ time: 10, text: "I remember" }, 20);
check("and are shared by syllables", Math.abs((shared[1].end - shared[1].start) - 3 * (shared[0].end - shared[0].start)) < 1e-9);
const beforeBreak = wordTimings({ time: 10, text: "Short" }, 60);
check("a line before a long gap is not stretched across it", beforeBreak.at(-1).end - 10 <= 1.2 + 1e-9);
const crowded = wordTimings({ time: 10, text: "A long line of many many words sung quickly" }, 10.5);
check("and never runs into the next line", crowded.at(-1).end <= 10.5 + 1e-9);
check("unspaced scripts fill a character at a time", wordTimings({ time: 0, text: String.fromCodePoint(0x591c, 0x306b, 0x99c6, 0x3051, 0x308b) }, 5).length === 5);
check("an empty line has nothing to fill", same(wordTimings({ time: 0, text: "" }, 5), []));

// Without the vocals envelope: most of the time until the next line, which is
// what a singer takes on ordinary consecutive lines. The old guess, 90ms a
// character, finished this one at 13.24 and ran ahead of the voice.
const happy = { time: 10, text: "I'm so happy 'cause today I found my friends" };
const guessed = wordTimings(happy, 14);
check("without the envelope, a line fills 90% of the way to the next", Math.abs(guessed.at(-1).end - 13.6) < 1e-9, String(guessed.at(-1).end));
check("with the words in order and back to back", guessed.every((w, i) => i === 0 || Math.abs(w.start - guessed[i - 1].end) < 1e-9));
check("the last line, with no next one, still ends", wordTimings({ time: 10, text: "The end" }, null).at(-1).end <= 10 + 1.2 + 1e-9);

// ── wordTimings with the vocals envelope ──
// A synthetic envelope, 40ms a level: silence (-90dBFS) except where given.
const HOP = 0.04;
function envelopeOf(spans, seconds = 70, silence = -90) {
  const db = new Array(Math.round(seconds / HOP)).fill(silence);
  for (const [from, to, level] of spans) {
    for (let i = Math.round(from / HOP); i < Math.round(to / HOP); i++) db[i] = level;
  }
  return { hop: HOP, db };
}
const near = (a, b, tol = HOP + 1e-9) => Math.abs(a - b) <= tol;
const inside = (w, spans) => spans.some(([a, b]) => w.start >= a - HOP - 1e-9 && w.end <= b + HOP + 1e-9);

const twoPhrases = [[10.5, 12, -15], [13, 14, -15]];
const phrased = wordTimings({ time: 10, text: "one two three four five" }, 16, envelopeOf(twoPhrases));
check("with the envelope, every word is still there, in order", phrased.map((w) => w.text).join("") === "one two three four five");
check("words land only where the voice is", phrased.every((w) => inside(w, twoPhrases)), JSON.stringify(phrased));
check("the first waits for the voice, not the stamp", near(phrased[0].start, 10.5));
check("and the last finishes when the voice stops", near(phrased.at(-1).end, 14));
const gapAt = phrased.findIndex((w) => w.start >= 12.9);
check(
  "a silence mid-line pauses the fill between two words",
  gapAt > 0 && phrased[gapAt - 1].end <= 12 + HOP && near(phrased[gapAt].start, 13),
  JSON.stringify(phrased),
);
check("no word is filling during the silence", phrased.every((w) => w.end <= 12 + HOP || w.start >= 13 - HOP));

const held = wordTimings({ time: 10, text: "To this mind" }, 20, envelopeOf([[10, 14.5, -15]]));
const len = (w) => w.end - w.start;
check("a held note stretches the last word", near(held.at(-1).end, 14.5) && len(held[2]) > 2 * (len(held[0]) + len(held[1])), JSON.stringify(held));
check("rather than slowing the words before it", len(held[0]) <= 0.5 + 1e-9 && len(held[1]) <= 0.5 + 1e-9);

const quick = wordTimings({ time: 10, text: "A long line of many many words sung quickly" }, 16, envelopeOf([[10, 12, -15]]));
check("a fast line is compressed into its singing", near(quick[0].start, 10) && near(quick.at(-1).end, 12));

const bled = envelopeOf([[10, 16, -48], [11, 12.5, -12]]);
const overBleed = wordTimings({ time: 10, text: "Only this" }, 16, bled);
check("what bled into the vocals stem, 20dB under the voice, is not singing", near(overBleed[0].start, 11) && near(overBleed.at(-1).end, 12.5), JSON.stringify(overBleed));

const breath = wordTimings({ time: 10, text: "Carry on my wayward son" }, 16, envelopeOf([[10, 11.2, -15], [11.36, 13, -15]]));
check("a breath shorter than a quarter second does not stall the wipe", breath.every((w, i) => i === 0 || near(w.start, breath[i - 1].end, 1e-9)), JSON.stringify(breath));
check(
  "and a blip shorter than a word is not taken for one",
  same(voicedPhrases(envelopeOf([[10, 11, -15], [13, 13.08, -15]]), 10, 16).length, 1),
);

const breakAfter = { time: 10, text: "Before the solo" };
const backing = envelopeOf([[10, 11.5, -15], [40, 45, -15]]);
const beforeSolo = wordTimings(breakAfter, 60, backing);
check("a long instrumental gap is not filled, even with backing vocals in it", near(beforeSolo.at(-1).end, 11.5), JSON.stringify(beforeSolo));
// Five syllables at the ceiling of 0.6s each: 3 seconds, not 45.
check("and not by the estimate either", near(wordTimings(breakAfter, 60).at(-1).end, 13, 1e-9));

check(
  "a line with no singing in the envelope falls back to the estimate",
  same(wordTimings(happy, 14, envelopeOf([])), wordTimings(happy, 14)),
);
check(
  "word stamps still win over the envelope",
  same(wordTimings(enhanced[0], 20, envelopeOf([[15, 19, -15]])), wordTimings(enhanced[0], 20)),
);

// ── syllables ──
// "cafe" with a combining acute on the e: two syllables, the e not silent.
const CAFE = `caf${String.fromCodePoint(0x65, 0x301)}`;
check(
  "syllables: vowel groups, a silent final e dropped, at least one",
  same(["happy", "home", "'cause", "table", "Yeah,", "I", "everyday", "friends", CAFE].map(syllables), [2, 1, 1, 2, 1, 1, 4, 1, 2]),
  JSON.stringify(["happy", "home", "'cause", "table", "Yeah,", "I", "everyday", "friends", CAFE].map(syllables)),
);

// ── songFromTitle ──
const cases = [
  [["Dream Theater - Pull Me Under (Official Video)", "Dream Theater"], "Pull Me Under"],
  [[`Bohemian Rhapsody ${String.fromCodePoint(0x2013)} Queen [HD]`, "Queen"], "Bohemian Rhapsody"],
  [['Metropolis - Part I: "The Miracle and the Sleeper"', "Dream Theater"], "Metropolis - Part I: The Miracle and the Sleeper"],
  [["Jay-Z ft. Alicia Keys - Empire State of Mind", "Jay-Z"], "Empire State of Mind"],
  [["Shadow Of The Day (Shadow Remix)", ""], "Shadow Of The Day (Shadow Remix)"],
  [["Hello (Official Music Video) (Remastered 2015)", ""], "Hello"],
];
for (const [[title, artist], want] of cases) {
  const got = songFromTitle(title, artist);
  check(`song from "${title}"`, got === want, `got ${JSON.stringify(got)}`);
}

// ── currentLineIndex ──
const lines = [{ time: 10 }, { time: 20 }, { time: 30 }];
check("before the first line", currentLineIndex(lines, 5) === -1);
check("on a line's stamp", currentLineIndex(lines, 20) === 1);
check("between lines, the one before", currentLineIndex(lines, 25) === 1);
check("after the last", currentLineIndex(lines, 99) === 2);
check("no lines", currentLineIndex([], 5) === -1);

// ── rankMatches ──
const row = (id, duration, kind) => ({
  id,
  trackName: `T${id}`,
  artistName: "A",
  albumName: "",
  duration,
  instrumental: kind === "instrumental",
  syncedLyrics: kind === "synced" ? "[00:01.00]x" : null,
  plainLyrics: kind === "synced" || kind === "plain" ? "x" : null,
});
const ranked = rankMatches(
  [row(1, 769, "synced"), row(2, 571, "plain"), row(3, 573, "synced"), row(4, 572, "none"), row(5, 590, "synced")],
  572,
);
check(
  "closest length first, synced preferred within a few seconds, empty rows dropped",
  same(ranked.map((m) => m.id), [3, 2, 5, 1]),
  JSON.stringify(ranked.map((m) => m.id)),
);
check("an instrumental row is kept", rankMatches([row(9, 100, "instrumental")], 100).length === 1);
check(
  "with no length known, LRCLIB's order, synced first",
  same(rankMatches([row(1, 10, "plain"), row(2, 500, "synced")], 0).map((m) => m.id), [2, 1]),
);
check("not an array gives nothing", same(rankMatches(null), []) && same(rankMatches({ error: 1 }), []));

// ── searchUrl ──
const withArtist = new URL(searchUrl({ artist: " Dream Theater ", song: " Metropolis " }));
check("LRCLIB's search", withArtist.origin + withArtist.pathname === "https://lrclib.net/api/search");
check(
  "artist and song as their own fields, trimmed",
  withArtist.searchParams.get("artist_name") === "Dream Theater" && withArtist.searchParams.get("track_name") === "Metropolis",
);
const songOnly = new URL(searchUrl({ song: "Metropolis" }));
check(
  "no artist: the free-text search, not an empty artist filter",
  songOnly.searchParams.get("q") === "Metropolis" && !songOnly.searchParams.has("artist_name"),
);

// ── searchLyrics ──
const asked = [];
const found = await searchLyrics(
  { artist: "Dream Theater", song: "Metropolis", duration: 572 },
  { fetchJson: async (url) => { asked.push(url); return [row(1, 769, "synced"), row(3, 573, "synced")]; } },
);
check("ranked for the track's length", same(found.map((m) => m.id), [3, 1]));
check("one request, to LRCLIB only", asked.length === 1 && asked[0].startsWith("https://lrclib.net/"));
check(
  "no song asks nothing",
  same(await searchLyrics({ artist: "X", song: " " }, { fetchJson: () => { throw new Error("asked"); } }), []),
);
let rejected = false;
try {
  await searchLyrics({ song: "x" }, { fetchJson: async () => { throw new Error("offline"); } });
} catch {
  rejected = true;
}
check("no connection rejects, rather than reading as nothing found", rejected);

console.log(`${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
