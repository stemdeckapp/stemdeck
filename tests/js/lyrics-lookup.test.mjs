// The Lyrics tab's reading of lyrics (#699): how LRC text becomes timed lines,
// which line is being sung at a given moment, and what the server's answer
// becomes. Which of LRCLIB's versions is the track's is the server's to decide
// (#719), and is tested in tests/test_lyrics_lookup.py.
//
// Run:  node tests/js/lyrics-lookup.test.mjs

import {
  parseLrc,
  currentLineIndex,
  wordTimings,
  voicedPhrases,
  syllables,
  songFromTitle,
  fromServerLyrics,
  sameSong,
  fold as foldName,
  clampOffset,
  shiftLines,
  firstSungIndex,
  offsetToStart,
  MAX_OFFSET_SEC,
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

// Whose song kept lyrics are: the check on what the tab kept before the
// server held versions to the track's song (#719). The rest of the rule is
// the server's alone (tests/test_lyrics_lookup.py).
{
  check("brackets and featuring are ignored", sameSong("This Is Me (feat. Someone) [Live]", "This Is Me"));
  check("a song tail belongs", sameSong("This Is Me - From The Greatest Showman", "This Is Me"));
  check("another song is not this one", !sameSong("This Is Not Me", "This Is Me"));
  check("two different tails are two songs", !sameSong("Part I - Dawn", "Part I - Dusk"));
}

{
  const songs = [
    ["红豆", "紅豆", true],
    ["晴天 (Sunny Day)", "晴天", true],
    ["晴天（Sunny Day）", "晴天", true],
    ["「白日」", "白日", true],
    ["【白日】", "白日", true],
    ["밤편지 (Through the Night)", "밤편지", true],
    ["月亮代表我的心 - 劇集 “黃金有罪” 插曲", "月亮代表我的心", true],
    ["Malomiasteczkowy", "Małomiasteczkowy", true],
    ["晴れの日(晴天)", "晴天", false],
    ["雨天", "晴天", false],
    ["The Moon Represents My Heart - 月亮代表我的心", "月亮代表我的心", false],
  ];
  for (const [found, song, want] of songs) {
    check(`sameSong(${found}, ${song}) is ${want}`, sameSong(found, song) === want);
  }
  check("folding: traditional, width, plain Latin", foldName("鄧麗君 ＩＵ Podsiadło") === "邓丽君 IU Podsiadlo");
}

// Karaoke in Chinese, Japanese and Korean.
{
  const texts = (line, next = 20) => wordTimings(line, next).map((w) => w.text);
  const zh = texts({ time: 0, text: "故事的小黄花 从出生那年" });
  check("Chinese with a space between phrases still goes a character at a time", same(zh, ["故", "事", "的", "小", "黄", "花 ", "从", "出", "生", "那", "年"]), JSON.stringify(zh));
  const ja = texts({ time: 0, text: "「夢ならば」Lemon しゃべらない" });
  check(
    "Japanese: brackets with their character, Latin words whole, small kana with the one before",
    same(ja, ["「夢", "な", "ら", "ば」", "Lemon ", "しゃ", "べ", "ら", "な", "い"]),
    JSON.stringify(ja),
  );
  check("the texts give the line back", ja.join("") === "「夢ならば」Lemon しゃべらない");
  const ko = wordTimings({ time: 0, text: "나는 너를 사랑해요" }, 20);
  check("Korean goes a word at a time", same(ko.map((w) => w.text), ["나는 ", "너를 ", "사랑해요"]));
  check("a Korean word takes as long as its syllables", Math.abs((ko[2].end - ko[2].start) / (ko[0].end - ko[0].start) - 2) < 1e-9);
  check("syllables: a block, a character, a kana each", syllables("사랑해요") === 4 && syllables("晴天") === 2 && syllables("しゃ") === 1 && syllables("ティー") === 2);
  const enhanced = parseLrc("[00:10.00]<00:10.00>夜<00:10.40>に<00:10.80>駆ける\n[00:14.00]前に <00:15.00>君が");
  check(
    "enhanced LRC in Japanese: each stamped piece filled at its stamp",
    same(wordTimings(enhanced[0], 14).map((w) => [w.text, w.start]), [["夜", 10], ["に", 10.4], ["駆ける", 10.8]]),
  );
  check(
    "words before the first word stamp are kept, from the line's own stamp",
    enhanced[1].text === "前に 君が" && same(wordTimings(enhanced[1], 20).map((w) => [w.text, w.start]), [["前に ", 14], ["君が", 15]]),
    JSON.stringify(enhanced[1]),
  );
}

// The Align panel: one offset over the lines' own timing.
{
  check("an offset is kept to the hundredth", clampOffset(15.936) === 15.94);
  check("an offset is bounded both ways", clampOffset(9999) === MAX_OFFSET_SEC && clampOffset(-9999) === -MAX_OFFSET_SEC);
  check(
    "anything not a finite number is no offset",
    [NaN, Infinity, "12", null, undefined, true, {}].every((x) => clampOffset(x) === 0),
  );
  check("-0 reads as none", Object.is(clampOffset(-0.001), 0));

  const near = (a, b) => a.length === b.length && a.every((x, i) => Math.abs(x - b[i]) < 1e-9);
  const base = parseLrc("[00:00.05]<00:00.05>Do you <00:01.00>have\n[00:04.00]\n[00:05.00]Second");
  const moved = shiftLines(base, 15.9);
  check("lines and word stamps move together", near(moved.map((l) => l.time), [15.95, 19.9, 20.9]) && near([moved[0].words[1].time], [16.9]), JSON.stringify(moved));
  check("the lines at their own timing are left as they were", base[0].time === 0.05 && base[0].words[1].time === 1);
  check("no offset is the same lines", shiftLines(base, 0) === base);
  const early = shiftLines(base, -3);
  check("moved before the track starts rather than piled at zero", early[0].time < 0 && near(shiftLines(early, 3).map((l) => l.time), base.map((l) => l.time)));
  check("the line being sung follows the offset", currentLineIndex(moved, 10) === -1 && currentLineIndex(moved, 16) === 0);

  const gapFirst = parseLrc("[00:01.00]\n[00:03.00]First words\n[00:06.00]Next");
  check("Start lyrics here skips an instrumental gap", firstSungIndex(gapFirst) === 1);
  check("no sung line is -1", firstSungIndex(parseLrc("[00:01.00]")) === -1 && firstSungIndex([]) === -1);
  check("the offset that puts a line at the playhead", offsetToStart(gapFirst, 1, 19) === 16 && offsetToStart(gapFirst, 2, 1) === -5);
  check("no such line is no offset", offsetToStart(gapFirst, 7, 19) === 0);

  const lyricsJson = { source: "lrclib", lrclib_id: 3, synced: "[00:01.00]Hi", duration: 180, offset_sec: 15.9, others: [{ source: "lrclib", lrclib_id: 4, synced: "[00:01.00]Hi", offset_sec: 3 }] };
  const found = fromServerLyrics(lyricsJson);
  check("the server's offset comes with the lyrics it keeps", found.entry.offsetSec === 15.9);
  check("never with the versions offered", found.others.every((o) => !("offsetSec" in o)));
  check("an old lyrics.json has none", !("offsetSec" in fromServerLyrics({ ...lyricsJson, offset_sec: undefined }).entry));
}

console.log(`${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
