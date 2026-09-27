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
  fromServerLyrics,
  strippedCopy,
  belongsTo,
  sameArtist,
  sameSong,
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

// ── fromServerLyrics: lyrics.json from GET /api/jobs/{id}/lyrics ──
const version = (id, source = "lrclib", extra = {}) => ({
  v: 1,
  source,
  track: "Metropolis",
  artist: "Dream Theater",
  album: "Images and Words",
  duration: 572,
  synced: "[00:01.00]x",
  plain: "x",
  instrumental: false,
  lrclib_id: id,
  ...extra,
});
const served = fromServerLyrics({ ...version(3), others: [version(3), version(5), version(null), { source: "x" }] });
check(
  "the server's answer in the tab's shape",
  same(served.entry, {
    v: 1,
    id: 3,
    source: "lrclib",
    track: "Metropolis",
    artist: "Dream Theater",
    album: "Images and Words",
    duration: 572,
    instrumental: false,
    synced: "[00:01.00]x",
    plain: "x",
  }),
  JSON.stringify(served.entry),
);
check(
  "other versions are LRCLIB's, without the one shown or anything malformed",
  same(served.others.map((m) => m.id), [5]),
  JSON.stringify(served.others),
);
const offered = fromServerLyrics({ detail: "no lyrics", others: [version(9), version(null)] });
check(
  "versions the server kept none of come as others to offer, with no entry",
  offered.entry === null && same(offered.others.map((m) => m.id), [9]),
  JSON.stringify(offered),
);
check("a transcription keeps its source", fromServerLyrics(version(null, "whisper")).entry.source === "whisper");
check(
  "an answer that is not lyrics is nothing",
  fromServerLyrics(null) === null
    && fromServerLyrics({ detail: "no lyrics" }) === null
    && fromServerLyrics(version(3, "lrclib", { synced: "", plain: "" })) === null,
);

// ── letters outside ASCII ──
// LRCLIB holds some songs as copies that lost those letters on the way in
// (Kayah's "Nie ma, nie ma ciebie": eleven copies read "Niewinnoci biaym
// niegiem", one "Niewinnością białym śniegiem"). The anthem stands in for a
// song, being in the public domain. The same cases as tests/test_lyrics_lookup.py.
const ANTHEM = [
  "[00:01.00]Jeszcze Polska nie zginęła,",
  "[00:04.00]Kiedy my żyjemy.",
  "[00:07.00]Co nam obca przemoc wzięła,",
  "[00:10.00]Szablą odbierzemy.",
  "[00:13.00]Marsz, marsz, Dąbrowski,",
  "[00:16.00]Z ziemi włoskiej do Polski.",
  "[00:19.00]Za twoim przewodem",
  "[00:22.00]Złączym się z narodem.",
].join("\n");
const drop = (s) => s.replace(/[^\p{ASCII}]/gu, "");
const fold = (s) => drop(s.normalize("NFD"));
const asLrclibDropsThem = (s) => drop(s.replaceAll("ę", "e"));
const textRow = (id, duration, text, synced = true) => ({
  id,
  trackName: `T${id}`,
  artistName: "A",
  albumName: "",
  duration,
  instrumental: false,
  syncedLyrics: synced ? text : null,
  plainLyrics: text.replace(/\[[^\]\n]*\]/g, ""),
});
const lyricsOf = (text) => ({ synced: text, plain: "" });

for (const [name, strip] of [["dropped", drop], ["folded", fold], ["as LRCLIB drops them", asLrclibDropsThem]]) {
  check(`a copy whose Polish letters were ${name} is told from its source`, strippedCopy(lyricsOf(strip(ANTHEM)), lyricsOf(ANTHEM)));
}
check("the intact copy is not a stripped one", !strippedCopy(lyricsOf(ANTHEM), lyricsOf(asLrclibDropsThem(ANTHEM))));
check("a copy is not a stripped copy of itself", !strippedCopy(lyricsOf(ANTHEM), lyricsOf(ANTHEM)));
for (const [intact, other] of [
  ["[00:01.00]Группа крови на рукаве, мой порядковый номер на рукаве", "[00:01.00]Gruppa krovi na rukave, moy poryadkovyy nomer na rukave"],
  ["[00:01.00]夢ならばどれほどよかったでしょう 未だにあなたのことを夢にみる", "[00:01.00]Yume naraba dore hodo yokatta deshou imada ni anata no koto wo yume ni miru"],
  ["[00:01.00]A café, a naïve smile, and the rest in plain English words", "[00:01.00]A cafe, a naive smile, and the rest in plain English words"],
  [ANTHEM, "[00:01.00]Jeszcze nic nie jest stracone, moja mila, gdy jestem z toba"],
]) {
  check(
    `not a stripped copy: ${other.slice(10, 30)}`,
    !strippedCopy(lyricsOf(other), lyricsOf(intact)) && !strippedCopy(lyricsOf(intact), lyricsOf(other)),
  );
}
{
  const rows = [
    textRow(5470091, 230, asLrclibDropsThem(ANTHEM)),
    textRow(28700266, 230, asLrclibDropsThem(ANTHEM)),
    textRow(10910419, 229.93, ANTHEM),
    textRow(4291789, 230, ANTHEM, false),
  ];
  const want = [10910419, 5470091, 28700266, 4291789];
  check("the intact copy ranks before stripped ones LRCLIB listed first", same(rankMatches(rows, 230).map((m) => m.id), want));
  check("and with the length unknown", same(rankMatches(rows).map((m) => m.id), want));
  const stripped = textRow(1, 230, asLrclibDropsThem(ANTHEM));
  check(
    "synced still beats plain, and length beats both",
    same(rankMatches([stripped, textRow(2, 230, ANTHEM, false)], 230).map((m) => m.id), [1, 2])
      && same(rankMatches([stripped, textRow(3, 250, ANTHEM)], 230).map((m) => m.id), [1, 3]),
  );
}

// Polish through parsing and the wipe: nothing dropped, nothing split.
{
  const polish = parseLrc("[00:31.72]<00:31.72>Śpiewałem <00:32.40>głośno <00:32.90>pod <00:33.10>prysznicem\n[00:33.97]Ten mój małomiasteczkowy hit");
  check("Polish lines keep every letter", polish[0].text === "Śpiewałem głośno pod prysznicem" && polish[1].text === "Ten mój małomiasteczkowy hit");
  check("Polish word stamps keep every letter", same(polish[0].words.map((w) => w.text.trim()), ["Śpiewałem", "głośno", "pod", "prysznicem"]));
  const words = wordTimings(polish[1], 36.76);
  check("the wipe splits Polish only at spaces", same(words.map((w) => w.text), ["Ten ", "mój ", "małomiasteczkowy ", "hit"]), JSON.stringify(words.map((w) => w.text)));
  check("a Polish word is weighed by its vowels", syllables("małomiasteczkowy") === 6 && syllables("żółć") === 1 && syllables("gęś") === 1);
  // Decomposed text, as a Mac can type it: one letter per accent, not two.
  const decomposed = parseLrc("[00:01.00]Śpiewałem głośno".normalize("NFD"));
  check("decomposed text is composed", decomposed[0].text === "Śpiewałem głośno" && decomposed[0].text.length === 16);
  const kana = wordTimings({ time: 0, text: "が夢" + String.fromCodePoint(0x845b, 0xe0100) }, 4);
  check(
    "an unspaced line splits into characters with their marks",
    same(kana.map((w) => w.text), ["が", "夢", String.fromCodePoint(0x845b, 0xe0100)]),
    JSON.stringify(kana.map((w) => w.text)),
  );
  const cyrillic = wordTimings(parseLrc("[00:01.00]Группа крови на рукаве")[0], 5);
  check("Cyrillic words stay whole", same(cyrillic.map((w) => w.text), ["Группа ", "крови ", "на ", "рукаве"]));
}

// Whose song a version is: only this song by this artist is ever kept.
{
  const ask = { artist: "Keala Settle & The Greatest Showman Ensemble", song: "This Is Me" };
  const row = (artist, track) => ({ artist, track });
  check("the same artist and song belong", belongsTo(row("Keala Settle & The Greatest Showman Ensemble", "This Is Me"), ask));
  check("one credited artist belongs", belongsTo(row("Keala Settle", "This Is Me"), ask));
  check("the cast filed as a run of the name belongs", belongsTo(row("The Greatest Showman Ensemble", "This Is Me"), ask));
  check("a song tail belongs", belongsTo(row("Keala Settle", "This Is Me - From The Greatest Showman"), ask));
  check("brackets and featuring are ignored", sameSong("This Is Me (feat. Someone) [Live]", "This Is Me"));
  check("another artist's song of that name does not", !belongsTo(row("Kesha", "This Is Me"), ask));
  check("the artist's other song does not", !belongsTo(row("Keala Settle", "This Is Not Me"), ask));
  check("two different tails are two songs", !sameSong("Part I - Dawn", "Part I - Dusk"));
  check("no artist known, nothing belongs", !belongsTo(row("Keala Settle", "This Is Me"), { artist: "", song: "This Is Me" }));
  check("a show's name counts when given", belongsTo(row("Wicked", "Popular"), { artist: "Kristin Chenoweth", song: "Popular", names: ["Wicked"] }));
  check("a show's name does not count unless given", !belongsTo(row("Wicked", "Popular"), { artist: "Kristin Chenoweth", song: "Popular" }));
  check("case, accents and a leading The do not matter", sameArtist("the beatles", ["The Beatles"]) && sameArtist("Beyonce", ["Beyoncé"]) && sameArtist("Beatles", ["The Beatles"]));
  check("Polish names compare letter for letter", sameArtist("Dawid Podsiadło", ["Dawid Podsiadło"]) && !sameArtist("Dawid Podsiadło", ["Dawid Kwiatkowski"]));
  check("a two-letter credit names nobody", !sameArtist("DJ", ["DJ & Someone Else"]));
  check("one shared word is not a shared name", !sameArtist("Pink", ["Pink Floyd"]));
}

console.log(`${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
