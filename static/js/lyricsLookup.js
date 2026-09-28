// Lyrics for the open track, from LRCLIB (lrclib.net), for the Lyrics tab in
// the sidebar (#699).
//
// LRCLIB because it is free, open and needs no key, and because most of what
// it holds is time-synced: each line carries the moment it is sung, so the tab
// can follow playback and a line can be clicked to jump there, which is what
// practising against the stems wants. The lyrics themselves are copyright of
// their writers; they are fetched when the Lyrics tab is opened on a track
// that has none kept, and kept on this machine for that track, never sent on.
//
// What is sent is the artist and song the track is known to be (its tags, or
// the saved band and the song's name from the title), nothing else.
//
// No DOM here, and fetch is passed in, so this runs under node for the unit
// tests (tests/js/lyrics-lookup.test.mjs).

import { artistNameKey } from "./artistLookup.js";
import { toSimplified } from "./zhVariants.js";

const LRCLIB_SEARCH = "https://lrclib.net/api/search";
const TIMEOUT_MS = 12000;

// Among versions this far apart or closer, prefer the one with synced lyrics.
// LRCLIB holds the same song many times over (live cuts, remasters, a rip with
// a second of silence trimmed), and a synced copy a second off beats a plain
// one that matches to the frame.
const SAME_LENGTH_SEC = 3;

/**
 * One LRCLIB row, reduced to what the tab uses. Rows with neither kind of
 * lyrics and no instrumental flag say nothing, and are dropped by the caller.
 */
function normalise(row) {
  return {
    id: Number(row?.id) || 0,
    track: String(row?.trackName || ""),
    artist: String(row?.artistName || ""),
    album: String(row?.albumName || ""),
    duration: Number(row?.duration) || 0,
    instrumental: Boolean(row?.instrumental),
    synced: typeof row?.syncedLyrics === "string" ? row.syncedLyrics : "",
    plain: typeof row?.plainLyrics === "string" ? row.plainLyrics : "",
  };
}

const SERVER_SOURCES = new Set(["lrclib", "file", "whisper"]);

/** One version from the server's lyrics.json in the tab's shape, or null. */
function fromServerVersion(v) {
  if (!v || typeof v !== "object" || !SERVER_SOURCES.has(v.source)) return null;
  const text = (x) => (typeof x === "string" ? x : "");
  const version = {
    id: Number(v.lrclib_id) || 0,
    source: v.source,
    track: text(v.track),
    artist: text(v.artist),
    album: text(v.album),
    duration: Number(v.duration) || 0,
    instrumental: v.instrumental === true,
    synced: text(v.synced),
    plain: text(v.plain),
  };
  return version.synced || version.plain || version.instrumental ? version : null;
}

/**
 * The lyrics the server found for a track (GET /api/jobs/{id}/lyrics) as the
 * tab keeps them: { entry, others }, `others` being the LRCLIB versions to
 * offer. `entry` is null when the server kept none but has versions to offer
 * (its 404 carries them); null when the answer holds neither.
 */
export function fromServerLyrics(data) {
  if (!data || typeof data !== "object") return null;
  const found = fromServerVersion(data);
  const others = (Array.isArray(data.others) ? data.others : [])
    .map(fromServerVersion)
    .filter((m) => m?.id && m.id !== found?.id);
  // The user's alignment (the Align panel), on the kept version only.
  const offsetSec = clampOffset(data.offset_sec);
  if (found) return { entry: { v: 1, ...found, ...(offsetSec ? { offsetSec } : {}) }, others };
  return others.length ? { entry: null, others } : null;
}

/**
 * Best first, for a track `duration` seconds long (0 when unknown):
 * closest in length, then synced over plain among versions within
 * SAME_LENGTH_SEC of each other, then intact over a copy stripped of its
 * accents (strippedCopy), then whatever LRCLIB ranked first.
 */
export function rankMatches(rows, duration = 0) {
  const matches = (Array.isArray(rows) ? rows : [])
    .map(normalise)
    .filter((m) => m.id && (m.synced || m.plain || m.instrumental));
  return rankVersions(matches, duration);
}

/** rankMatches' order over versions it already gave, say from two searches.
 * Each version once, by id. */
export function rankVersions(versions, duration = 0) {
  const seen = new Set();
  const matches = versions.filter((m) => m?.id && !seen.has(m.id) && seen.add(m.id));
  const off = (m) => (duration && m.duration ? Math.abs(m.duration - duration) : 0);
  const stripped = strippedIndexes(matches);
  return matches
    .map((m, rank) => ({ m, rank }))
    .sort((a, b) => {
      const da = off(a.m);
      const db = off(b.m);
      if (Math.abs(da - db) > SAME_LENGTH_SEC) return da - db;
      if (Boolean(a.m.synced) !== Boolean(b.m.synced)) return a.m.synced ? -1 : 1;
      const sa = stripped.has(a.rank);
      if (sa !== stripped.has(b.rank)) return sa ? 1 : -1;
      return da - db || a.rank - b.rank;
    })
    .map(({ m }) => m);
}

// Stripped copies, as _Words in app/pipeline/lyrics_lookup.py. LRCLIB holds
// many songs more than once, and some copies lost every letter outside ASCII
// on their way in: "Niewinnoci biaym niegiem" for "Niewinnością białym
// śniegiem" (Kayah, lrclib 5470091 beside the intact 10910419). Each such
// letter was either dropped or folded to its base ("się" as "sie"). Such a copy
// cannot be told from a song written without accents on its own, only beside
// the copy it was stripped from.
const LRC_TAGS = /\[[^\]\n]*\]|<\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?>/g;
const NOT_ASCII = /[^\p{ASCII}]/gu;
const isAscii = (word) => !/[^\p{ASCII}]/u.test(word);
// The intact copy has at least this many words with a letter outside ASCII,
// so a stray "café" proves nothing; the stripped one keeps at most a quarter
// of them; at least 60% of the intact copy's accented words appear in it with
// those letters dropped or folded; at least 80% of its words are the intact
// copy's.
const STRIPPED_MIN_WORDS = 5;
const STRIPPED_KEPT_MAX = 0.25;
const STRIPPED_FOUND_MIN = 0.6;
const STRIPPED_SAME_MIN = 0.8;

/** What an accented word becomes with its letters outside ASCII dropped
 * ("każe" as "kae") or folded to their base first ("się" as "sie"). */
function strippedForms(word) {
  const forms = [word.replace(NOT_ASCII, ""), word.normalize("NFD").replace(NOT_ASCII, "")];
  return forms.filter(Boolean);
}

/** A version's words, lowercased, time stamps left out, with what they would
 * be stripped: worked out once per version, compared many times. */
function wordsOf(match) {
  const text = String(match?.synced || match?.plain || "").normalize("NFC").replace(LRC_TAGS, " ");
  const words = text.toLowerCase().match(/[\p{L}\p{M}]+/gu) || [];
  const accented = words.filter((w) => !isAscii(w));
  const forms = accented.map(strippedForms);
  return { words, have: new Set(words), accented, forms, known: new Set([...words, ...forms.flat()]) };
}

function strippedFrom(copy, intact) {
  const accented = intact.accented.length;
  if (accented < STRIPPED_MIN_WORDS || !copy.words.length) return false;
  if (copy.accented.length > accented * STRIPPED_KEPT_MAX) return false;
  const found = intact.forms.filter((forms) => forms.some((f) => copy.have.has(f))).length;
  if (found < accented * STRIPPED_FOUND_MIN) return false;
  const same = copy.words.filter((w) => intact.known.has(w)).length;
  return same >= copy.words.length * STRIPPED_SAME_MIN;
}

/**
 * Whether `match` is `other`'s lyrics with the letters outside ASCII lost,
 * dropped or folded to their base letter. Both are versions as rankMatches
 * gives them ({ synced, plain }).
 */
export function strippedCopy(match, other) {
  return strippedFrom(wordsOf(match), wordsOf(other));
}

/** The indexes in `matches` of versions that are a stripped copy of another. */
function strippedIndexes(matches) {
  const words = matches.map(wordsOf);
  const intact = words.map((w, j) => [j, w]).filter(([, w]) => w.accented.length >= STRIPPED_MIN_WORDS);
  const out = new Set();
  words.forEach((w, i) => {
    if (intact.some(([j, other]) => j !== i && strippedFrom(w, other))) out.add(i);
  });
  return out;
}

/**
 * Timed lines from LRC text, earliest first: [{ time, text }], time in
 * seconds. A line sung more than once carries several stamps
 * ("[00:12.00][01:40.00]Chorus") and appears once per stamp. Header tags
 * ("[ar:...]") are skipped, and "[offset:+250]" shifts every line by that many
 * milliseconds, earlier for a positive value, as the format defines.
 *
 * Enhanced LRC times words too ("[00:12.00]<00:12.00>Hello <00:12.60>world"),
 * and a line that does gets `words`: [{ time, text }], each piece of text up
 * to the next stamp. Those are what the karaoke wipe follows exactly; a line
 * without them has its time shared out by wordTimings().
 */
export function parseLrc(text) {
  const lines = [];
  let offset = 0;
  // Composed, as most fonts expect: text typed on a Mac can arrive with each
  // accent a mark of its own ("ś" as "s" and a combining acute).
  for (const raw of String(text || "").normalize("NFC").split(/\r?\n/)) {
    const tag = /^\[offset:\s*([+-]?\d+)\s*\]/i.exec(raw.trim());
    if (tag) {
      offset = Number(tag[1]) / 1000;
      continue;
    }
    const stamps = [];
    let rest = raw;
    let m;
    while ((m = /^\s*\[(\d{1,3}):(\d{1,2}(?:[.:]\d{1,3})?)\]/.exec(rest))) {
      stamps.push(Number(m[1]) * 60 + Number(m[2].replace(":", ".")));
      rest = rest.slice(m[0].length);
    }
    if (!stamps.length) continue;
    const words = wordStamps(rest);
    const plain = rest.replace(WORD_STAMP, "").replace(/\s+/g, " ").trim();
    for (const time of stamps) {
      // Words sung before the first word stamp start with the line.
      const timed = words?.[0].time == null ? words?.map((w, i) => (i ? w : { ...w, time })) : words;
      lines.push(timed ? { time, text: plain, words: timed } : { time, text: plain });
    }
  }
  const shift = (t) => Math.max(0, t - offset);
  return lines
    .map((line) => ({
      ...line,
      time: shift(line.time),
      ...(line.words ? { words: line.words.map((w) => ({ ...w, time: shift(w.time) })) } : {}),
    }))
    .sort((a, b) => a.time - b.time);
}

const WORD_STAMP = /<(\d{1,3}):(\d{1,2}(?:[.:]\d{1,3})?)>/g;

/** Word stamps in a line's text, [{ time, text }], or null when it has none.
 * Text before the first stamp is a piece of its own with no time (null),
 * which is the line's. */
function wordStamps(text) {
  const pieces = [];
  let last = null;
  let cursor = 0;
  for (const m of text.matchAll(WORD_STAMP)) {
    if (last) last.text = text.slice(cursor, m.index);
    else if (text.slice(0, m.index).trim()) pieces.push({ time: null, text: text.slice(0, m.index) });
    last = { time: Number(m[1]) * 60 + Number(m[2].replace(":", ".")), text: "" };
    pieces.push(last);
    cursor = m.index + m[0].length;
  }
  if (!last) return null;
  last.text = text.slice(cursor);
  return pieces;
}

// Most LRC stamps only when each line starts, so when its words are sung has
// to be worked out. With the vocals stem's level over time (the envelope,
// /api/jobs/{id}/vocal-envelope, a level every 40ms) the words are laid over
// the stretches of the line where the singer is actually singing; without it,
// over most of the time until the next line.
//
// A sung syllable is about a quarter of a second. Words take that, stretched
// to fill a phrase up to twice over; what a phrase has left beyond that is a
// note held on its last word.
const SYLLABLE_SECONDS = 0.25;
const STRETCH_MAX = 2;

// Without the envelope: a line is sung over 90% of the time until the next
// one, but never slower than 0.6s a syllable (so a line before an
// instrumental break does not crawl across it) and never under 1.2s.
const FALLBACK_SHARE = 0.9;
const FALLBACK_SECONDS_PER_SYLLABLE = 0.6;
const MIN_LINE_SECONDS = 1.2;
// The last line has no next one to stop at.
const LAST_LINE_SECONDS = 12;

// With the envelope, what counts as singing within a line: within 20dB of the
// line's loud parts (its 90th percentile), and never below -50dBFS, where a
// separated vocals stem holds only what bled from the other instruments. A
// line whose loud parts are under -45dBFS has no singing to follow, and is
// timed as if there were no envelope.
const VOICE_RANGE_DB = 20;
const VOICE_FLOOR_DB = -50;
const SILENT_LINE_DB = -45;
const LOUD_PERCENTILE = 0.9;
// A dip shorter than this is a consonant or a quick breath, not a pause, so
// the wipe carries on through it; a burst shorter than this is not a word.
const BRIDGE_SECONDS = 0.25;
const MIN_PHRASE_SECONDS = 0.12;
// How far past its stamp a line's singing is looked for: a second a syllable,
// at least six, so backing vocals deep in an instrumental break are not taken
// for the line's last word.
const WINDOW_SECONDS_PER_SYLLABLE = 1;
const MIN_WINDOW_SECONDS = 6;

// Scripts written without spaces between words, where the wipe moves a
// character at a time instead, the long vowel mark ("ー") with them. Korean
// is written with spaces, and goes a word at a time as Latin does.
const UNSPACED = /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}ー]/u;
// Characters sung one to a syllable: a Chinese character, a kana (a Japanese
// mora), a Korean syllable block. The small kana that bend the one before
// them ("しゃ", "ティ") are not one of their own.
const SYLLABIC = /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}ー가-힣]/gu;
const SMALL_KANA = /[ぁぃぅぇぉゃゅょゎァィゥェォャュョヮ]/gu;
const SMALL_KANA_ONE = /^[ぁぃぅぇぉゃゅょゎァィゥェォャュョヮ]$/u;
// Punctuation that opens ("「", "(", "“"), which goes with the character
// after it; any other goes with the one before.
const OPENING = /^[\p{Ps}\p{Pi}]$/u;
const MARKS = /^[\p{P}\p{S}]+$/u;

/** A line's text as the pieces the wipe fills: words, and in Chinese and
 * Japanese each character, the texts joined giving back the line. */
function lineTokens(text) {
  const words = text.match(/\S+\s*/g) || [];
  return UNSPACED.test(text) ? words.flatMap(unspacedPieces) : words;
}

/**
 * One spaced word of a line as the pieces the wipe fills: a Chinese or
 * Japanese character each on its own, a run of anything else ("Lemon", "2",
 * a Korean word) together, punctuation with its neighbour so no piece is only
 * a mark ("「夜", "る」"), and the space after the word on the last piece.
 */
function unspacedPieces(word) {
  if (!UNSPACED.test(word)) return [word];
  const pieces = [];
  let run = false; // whether the last piece is a run that may go on
  let opening = ""; // opening marks waiting for the character after them
  for (const ch of characters(word)) {
    if (/^\s+$/u.test(ch)) {
      if (pieces.length) pieces[pieces.length - 1] += ch;
      else opening += ch;
      run = false;
    } else if (MARKS.test(ch)) {
      if (OPENING.test(ch) || !pieces.length) {
        opening += ch;
      } else {
        pieces[pieces.length - 1] += ch;
      }
      run = false;
    } else if (SMALL_KANA_ONE.test(ch) && pieces.length && !run && !opening) {
      // "しゃ", "ティ": one sound, one piece.
      pieces[pieces.length - 1] += ch;
    } else if (UNSPACED.test(ch)) {
      pieces.push(opening + ch);
      opening = "";
      run = false;
    } else if (run && !opening) {
      pieces[pieces.length - 1] += ch;
    } else {
      pieces.push(opening + ch);
      opening = "";
      run = true;
    }
  }
  if (opening) {
    if (pieces.length) pieces[pieces.length - 1] += opening;
    else pieces.push(opening);
  }
  return pieces;
}

/** `text` as the characters a reader sees: a base letter with the marks on it
 * stays one piece ("か" with its voicing mark, when the text came decomposed),
 * so no mark is left in a span of its own. */
function characters(text) {
  return text.match(/\P{M}\p{M}*|\p{M}+/gu) || [];
}

/**
 * About how many syllables `token` is sung in: a Chinese character, kana or
 * Korean syllable block each, vowel groups in Latin script, less a silent
 * final e ("home", "'cause", but not "table"), and for other scripts one per
 * two or three letters. At least one.
 */
export function syllables(token) {
  const word = token.trim();
  if (!word) return 0;
  const composed = word.normalize("NFC");
  const syllabic = (composed.match(SYLLABIC) || []).length - (composed.match(SMALL_KANA) || []).length;
  if (syllabic > 0) {
    const latin = composed.normalize("NFD").toLowerCase().match(/[aeiouy]+/g) || [];
    return syllabic + latin.length;
  }
  // The silent e is looked for before accents are stripped: "cafe" with an
  // acute is two syllables.
  const lower = word.normalize("NFC").toLowerCase().replace(/[^\p{L}]+$/u, "");
  const bare = lower.normalize("NFD").replace(/\p{M}/gu, "");
  if (/[a-z]/.test(bare)) {
    let count = (bare.match(/[aeiouy]+/g) || []).length;
    if (count > 1 && /[^aeiouy]e$/.test(lower) && !/[^aeiouy]le$/.test(lower)) count -= 1;
    return Math.max(1, count);
  }
  const letters = Array.from(word.replace(/[\p{P}\p{S}]/gu, "")).length;
  return Math.max(1, Math.round(letters / 2.5));
}

/**
 * The stretches of [from, to) where the envelope says someone is singing:
 * [{ start, end }] in seconds, earliest first, [] when the window holds no
 * singing. `envelope` is { hop, db }: a level in dBFS every `hop` seconds.
 */
export function voicedPhrases(envelope, from, to) {
  const { hop, db } = envelope || {};
  if (!(hop > 0) || !db?.length) return [];
  const first = Math.max(0, Math.ceil(from / hop - 1e-9));
  const last = Math.min(db.length, Math.floor(to / hop + 1e-9));
  if (last - first < 2) return [];
  const levels = Array.from(db.slice(first, last));
  const sorted = levels.slice().sort((a, b) => a - b);
  const loud = sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * LOUD_PERCENTILE))];
  if (loud < SILENT_LINE_DB) return [];
  const threshold = Math.max(loud - VOICE_RANGE_DB, VOICE_FLOOR_DB);
  // Frame by frame; a quiet frame inside a word is closed over by the
  // bridge below, and a loud one alone is dropped as too short.
  const n = levels.length;
  const runs = [];
  let open = -1;
  for (let i = 0; i <= n; i++) {
    const voiced = i < n && levels[i] >= threshold;
    if (voiced && open < 0) open = i;
    if (!voiced && open >= 0) {
      runs.push({ start: (first + open) * hop, end: (first + i) * hop });
      open = -1;
    }
  }
  const phrases = [];
  for (const run of runs) {
    const prev = phrases.at(-1);
    if (prev && run.start - prev.end < BRIDGE_SECONDS) prev.end = run.end;
    else phrases.push(run);
  }
  return phrases
    .filter((p) => p.end - p.start >= MIN_PHRASE_SECONDS)
    .map((p) => ({ start: Math.max(p.start, from), end: Math.min(p.end, to) }));
}

// How badly `nominal` seconds of words fit a phrase `length` long. Words
// sung faster than a quarter second a syllable cost four times what the same
// factor slower does, since a held note explains slow but nothing explains
// fast. A phrase given no words costs its length: a breath the bridge missed
// is cheap, a sung phrase skipped is not.
function phraseCost(length, nominal) {
  if (!nominal) return length * 1.5;
  const r = Math.log(nominal / length);
  return r > 0 ? 4 * r * r : r * r;
}

/**
 * Which words go in which phrase, as the index of each phrase's first word:
 * the contiguous split whose word lengths best fit the phrases' lengths.
 * A phrase may get none. Lines are short (tens of words, a few phrases), so
 * trying every split costs nothing.
 */
function assignWords(phrases, nominal) {
  const prefix = [0];
  for (const n of nominal) prefix.push(prefix.at(-1) + n);
  const words = nominal.length;
  const best = phrases.map(() => new Array(words + 1).fill(Infinity));
  const from = phrases.map(() => new Array(words + 1).fill(0));
  phrases.forEach((phrase, j) => {
    const length = phrase.end - phrase.start;
    for (let k = 0; k <= words; k++) {
      // The last phrase takes every word left.
      if (j === phrases.length - 1 && k !== words) continue;
      for (let a = 0; a <= k; a++) {
        const before = j === 0 ? (a === 0 ? 0 : Infinity) : best[j - 1][a];
        const cost = before + phraseCost(length, prefix[k] - prefix[a]);
        if (cost < best[j][k]) {
          best[j][k] = cost;
          from[j][k] = a;
        }
      }
    }
  });
  const starts = new Array(phrases.length).fill(0);
  let k = words;
  for (let j = phrases.length - 1; j >= 0; j--) {
    starts[j] = from[j][k];
    k = starts[j];
  }
  return starts;
}

/** Lay `tokens` over one phrase: their own length, stretched at most
 * STRETCH_MAX times, and the last word held for whatever is left. */
function layPhrase(tokens, weights, phrase, out) {
  const length = phrase.end - phrase.start;
  const nominal = weights.reduce((sum, w) => sum + w * SYLLABLE_SECONDS, 0);
  const scale = nominal >= length ? length / nominal : Math.min(STRETCH_MAX, length / nominal);
  let at = phrase.start;
  tokens.forEach((text, i) => {
    const end = i === tokens.length - 1 ? phrase.end : at + weights[i] * SYLLABLE_SECONDS * scale;
    out.push({ text, start: at, end });
    at = end;
  });
}

/**
 * When each word of `line` is sung, for the karaoke wipe: [{ text, start,
 * end }], the texts joined giving back the line. From the line's own word
 * stamps when it has them. Otherwise from `envelope`, the vocals stem's level
 * ({ hop, db }), when it shows singing in the line: the words fill only while
 * the singer sings, wait through pauses, and finish when the voice stops.
 * Otherwise spread over most of the time until the next line. `nextTime` is
 * when the next line starts, or null for the last.
 */
export function wordTimings(line, nextTime, envelope = null) {
  const limit = nextTime != null && nextTime > line.time ? nextTime : line.time + LAST_LINE_SECONDS;
  if (line.words?.length) {
    const out = [];
    line.words.forEach((word, i) => {
      if (!word.text) return;
      const end = line.words[i + 1]?.time ?? Math.min(limit, word.time + 1.5);
      out.push({ text: word.text, start: word.time, end: Math.max(end, word.time + 0.05) });
    });
    if (out.length) return out;
  }
  const tokens = lineTokens(line.text || "");
  const weights = tokens.map(syllables);
  const total = weights.reduce((a, b) => a + b, 0);
  if (!total) return [];

  if (envelope) {
    const reach = Math.max(MIN_WINDOW_SECONDS, total * WINDOW_SECONDS_PER_SYLLABLE);
    const phrases = voicedPhrases(envelope, line.time, Math.min(limit, line.time + reach));
    if (phrases.length) {
      const starts = assignWords(phrases, weights.map((w) => w * SYLLABLE_SECONDS));
      const out = [];
      phrases.forEach((phrase, j) => {
        const end = starts[j + 1] ?? tokens.length;
        if (end > starts[j]) layPhrase(tokens.slice(starts[j], end), weights.slice(starts[j], end), phrase, out);
      });
      return out;
    }
  }

  const sung = Math.min(
    (limit - line.time) * FALLBACK_SHARE,
    Math.max(MIN_LINE_SECONDS, total * FALLBACK_SECONDS_PER_SYLLABLE),
  );
  let at = line.time;
  return tokens.map((token, i) => {
    const length = (weights[i] / total) * sung;
    const word = { text: token, start: at, end: at + length };
    at += length;
    return word;
  });
}

// Noise a video title adds around the song's name, dropped when it sits in
// brackets: "(Official Video)", "[HD]", "(Lyric Video)", "(Remastered 2009)".
// Lookarounds rather than word boundaries so "hd" inside "(Shadow Remix)"
// is left alone.
const TITLE_NOISE = /[([][^)\]]*(?<!\p{L})(official|video|audio|lyrics?|visuali[sz]er|hd|hq|4k|remaster(?:ed)?|m\/?v|explicit|clean|full album)(?!\p{L})[^)\]]*[)\]]/giu;
// Any dash: hyphen, en dash, em dash and the rest of Unicode's dash
// punctuation, since titles use all of them between artist and song.
const DASH = "[\\p{Pd}:|]";

/**
 * The song's name from a track title, for searching lyrics: the artist taken
 * off either end ("Queen - Bohemian Rhapsody", "Bohemian Rhapsody - Queen"),
 * bracketed video noise and featured artists dropped, quotes removed.
 * `artist` may be empty, and then only the noise goes.
 */
export function songFromTitle(title, artist = "") {
  let song = String(title || "")
    .replace(TITLE_NOISE, " ")
    // The featured artist, up to the next dash or bracket: in "Jay-Z ft.
    // Alicia Keys - Empire State of Mind" the song is after it. Before the
    // artist is looked for, which it would otherwise stand between.
    .replace(/\s+(?:ft\.?|feat\.?|featuring)\s[^\p{Pd}([]*/giu, " ");
  const name = String(artist || "").trim();
  if (name) {
    const literal = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    song = song
      .replace(new RegExp(`^\\s*${literal}\\s*${DASH}\\s*`, "iu"), "")
      .replace(new RegExp(`\\s*${DASH}\\s*${literal}\\s*$`, "iu"), "");
  }
  return song
    .replace(/["\p{Pi}\p{Pf}]/gu, "")
    .replace(/\s+/g, " ")
    .replace(/^[\s\p{Pd}:|]+|[\s\p{Pd}:|]+$/gu, "")
    .trim();
}

// Whose song a version is: belongs_to in app/pipeline/lyrics_lookup.py.
//
// LRCLIB's search is fuzzy: asked for one artist's song it also answers with
// other artists' songs of a similar name. Lyrics that may be another song's are
// worse than none, so a version is kept only when its song is the one asked for
// and its artist is one of the names the track is known by: the artist itself,
// one of the artists credited with it ("Keala Settle" of "Keala Settle & The
// Greatest Showman Ensemble"), the show a cast recording is filed under, or
// another name the band goes by (its name in the artist box). Names are
// compared folded (fold): full-width letters as half-width, traditional
// Chinese as simplified.
const NAME_LIST = /\s*(?:[&,+/;]|\band\b|\bwith\b|\bfeat\.?|\bft\.?|\bfeaturing\b|\bvs\.?)\s*/iu;
const LEADING_THE = /^\s*the\s+/iu;
const SONG_FEATURING = /\s+(?:ft\.?|feat\.?|featuring)\s.*$/iu;
const SONG_BRACKETS = /[([{【][^()[\]{}【】]*[)\]}】]/gu;
const SONG_TAIL = /\s+\p{Pd}+\s+.*$/u;
// What stands between the names a name is written in at once: "周杰倫 Jay
// Chou", "IU (아이유)", "五月天 (Mayday)".
const SCRIPT_BREAK = /[\s()[\]{}【】「」『』〈〉《》]+/u;
// One credited artist matches only when its name weighs at least this much
// (nameWeight: an ideograph counts two), so an initial or a stray "DJ" names
// nobody while 王菲 does; a run of words inside a longer name ("The Greatest
// Showman" in "The Greatest Showman Cast") needs this many.
const PART_MIN_CHARS = 4;
const RUN_MIN_WORDS = 2;

// Latin letters Unicode does not build from a base letter and an accent, as a
// name typed without them has them: "Podsiadlo" for Podsiadło.
const LATIN_LETTERS = { ł: "l", Ł: "L", ø: "o", Ø: "O", đ: "d", Đ: "D", ð: "d", Ð: "D", ß: "ss", æ: "ae", Æ: "AE", œ: "oe", Œ: "OE", ı: "i", þ: "th", Þ: "TH" };
const LATIN_LETTER = /[łŁøØđĐðÐßæÆœŒıþÞ]/gu;

/** A name as it is compared, fold() in app/pipeline/name_aliases.py:
 * compatibility forms unified (full-width Latin, half-width kana),
 * traditional Chinese as simplified, and the Latin letters above plain. */
export function fold(text) {
  return toSimplified(String(text || "").normalize("NFKC").replace(LATIN_LETTER, (ch) => LATIN_LETTERS[ch]));
}

const isHan = (cp) =>
  (cp >= 0x3400 && cp <= 0x4dbf) || (cp >= 0x4e00 && cp <= 0x9fff) || (cp >= 0xf900 && cp <= 0xfaff) || (cp >= 0x20000 && cp <= 0x3ffff);

/** A Chinese, Japanese or Korean letter: an ideograph, kana or Hangul. */
function isCjk(ch) {
  const cp = ch.codePointAt(0);
  return isHan(cp)
    || (cp >= 0x3040 && cp <= 0x30ff) || (cp >= 0x31f0 && cp <= 0x31ff) || (cp >= 0xff66 && cp <= 0xff9f)
    || (cp >= 0x1100 && cp <= 0x11ff) || (cp >= 0x3130 && cp <= 0x318f) || (cp >= 0xac00 && cp <= 0xd7af);
}

/** How much of a name a name key is, in Latin letters: name_weight in
 * name_aliases.py. An ideograph counts two, marks nothing. */
function nameWeight(key) {
  let weight = 0;
  for (const ch of key) weight += isHan(ch.codePointAt(0)) ? 2 : /\p{M}/u.test(ch) ? 0 : 1;
  return weight;
}

const nameKey = (name) => artistNameKey(fold(name).replace(LEADING_THE, ""));
const nameWords = (name) => (fold(name).match(/[\p{L}\p{M}\p{N}]+/gu) || []).map(artistNameKey).filter(Boolean);
const nameParts = (name) => fold(name).split(NAME_LIST).map(nameKey).filter(Boolean);

function scriptOf(token) {
  const letters = token.match(/\p{L}/gu) || [];
  if (!letters.length) return "";
  const cjk = letters.filter(isCjk).length;
  return cjk === letters.length ? "cjk" : cjk ? "mixed" : "other";
}

/**
 * The names a name gives in two scripts at once, each on its own: "周杰倫 Jay
 * Chou" as 周杰倫 and "Jay Chou", "IU (아이유)" as IU and 아이유. [] for a name
 * in one script, or with a word that mixes them ("Official髭男dism").
 * script_names in app/pipeline/lyrics_lookup.py.
 */
export function scriptNames(name) {
  const tokens = fold(name).split(SCRIPT_BREAK).filter(Boolean);
  const kinds = tokens.map(scriptOf);
  if (kinds.includes("mixed") || new Set(kinds.filter(Boolean)).size < 2) return [];
  const groups = [];
  let last = "";
  tokens.forEach((token, i) => {
    if (!kinds[i]) {
      last = "";
      return;
    }
    if (kinds[i] !== last) groups.push([]);
    groups.at(-1).push(token);
    last = kinds[i];
  });
  return groups.map((group) => group.join(" "));
}

const wholeNames = (name) => [nameKey(name), ...scriptNames(name).map(nameKey)].filter(Boolean);

function containsRun(words, run) {
  if (run.length < RUN_MIN_WORDS || run.length > words.length) return false;
  for (let i = 0; i + run.length <= words.length; i++) {
    if (run.every((w, j) => words[i + j] === w)) return true;
  }
  return false;
}

/** Whether `found`, a version's artist, is one of `names`. */
export function sameArtist(found, names) {
  if (!nameKey(found)) return false;
  const theirs = new Set(wholeNames(found));
  const theirParts = nameParts(found);
  const theirWords = nameWords(found);
  return names.some((name) => {
    if (!String(name || "").trim()) return false;
    if (wholeNames(name).some((key) => theirs.has(key))) return true;
    if (nameParts(name).some((p) => nameWeight(p) >= PART_MIN_CHARS && theirParts.includes(p))) return true;
    const mine = nameWords(name);
    return containsRun(mine, theirWords) || containsRun(theirWords, mine);
  });
}

function songKeys(name) {
  const text = fold(name);
  let full = text.replace(SONG_BRACKETS, " ").replace(SONG_FEATURING, "");
  // A name all in brackets (【白日】) is the name, not a note on it.
  if (!artistNameKey(full)) full = text;
  return { full: artistNameKey(full), head: artistNameKey(full.replace(SONG_TAIL, "")) };
}

/**
 * Whether `found`, a version's song, is `song`: the same name less brackets
 * and featured artists, or one of them the other with a " - ..." tail ("This
 * Is Me - From The Greatest Showman"). Two tails never make a match: "Part I -
 * Dawn" is not "Part I - Dusk". Compared folded: "紅豆" is "红豆".
 */
export function sameSong(found, song) {
  const a = songKeys(found);
  const b = songKeys(song);
  if (!a.full || !b.full) return false;
  return a.full === b.full || a.head === b.full || a.full === b.head;
}

/** Whether a version is `song` by `artist` or one of `names` (a show's, the
 * band's other names). */
export function belongsTo(match, { artist = "", song = "", names = [] } = {}) {
  return sameSong(match?.track, song) && sameArtist(match?.artist, [artist, ...names]);
}

/**
 * Who else to search LRCLIB by when the artist's own name found no version
 * the track's length: the names it gives at once ("周杰倫 Jay Chou"), then
 * `names` (the band's in the artist box), each once and never the artist's
 * own. An uploader files a song under whichever one they write.
 */
export function otherNames(artist, names = []) {
  const asked = new Set([String(artist || "").trim().toLowerCase()]);
  const out = [];
  for (const name of [...scriptNames(artist), ...names]) {
    const text = String(name || "").trim();
    if (text && !asked.has(text.toLowerCase())) {
      asked.add(text.toLowerCase());
      out.push(text);
    }
  }
  return out;
}

// How far the Align panel moves lyrics either way, as the server bounds it
// (LYRICS_OFFSET_MAX_SEC in app/core/config.py).
export const MAX_OFFSET_SEC = 600;

/** An offset in seconds, to the hundredth and within MAX_OFFSET_SEC; 0 for
 * anything that is not a finite number (a string included). */
export function clampOffset(seconds) {
  if (typeof seconds !== "number" || !Number.isFinite(seconds)) return 0;
  // + 0 turns -0 into 0, so an offset back at zero reads as none.
  return Math.round(Math.min(MAX_OFFSET_SEC, Math.max(-MAX_OFFSET_SEC, seconds)) * 100) / 100 + 0;
}

/**
 * parseLrc's lines `offset` seconds later (earlier when negative), word stamps
 * too. Not clamped at zero, unlike the [offset:] tag: a line moved before the
 * track starts is simply never reached, and moving it back finds it where it
 * was.
 */
export function shiftLines(lines, offset) {
  if (!offset) return lines;
  return lines.map((line) => ({
    ...line,
    time: line.time + offset,
    ...(line.words ? { words: line.words.map((w) => (w.time == null ? w : { ...w, time: w.time + offset })) } : {}),
  }));
}

/** The first line with words, which "Start lyrics here" moves by default; -1
 * when there is none. */
export function firstSungIndex(lines) {
  return lines.findIndex((line) => line.text.trim() !== "");
}

/** The offset that puts line `index` of `lines` (at their own timing) at
 * `seconds` into the track. */
export function offsetToStart(lines, index, seconds) {
  const line = lines[index];
  return line ? clampOffset(seconds - line.time) : 0;
}

/** Index of the line being sung at `seconds`, or -1 before the first. */
export function currentLineIndex(lines, seconds) {
  let lo = 0;
  let hi = lines.length - 1;
  let found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (lines[mid].time <= seconds) {
      found = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  return found;
}

/** The request for a search, as a URL. The song is required, the artist not. */
export function searchUrl({ artist = "", song = "" }) {
  const url = new URL(LRCLIB_SEARCH);
  const a = String(artist).trim();
  const s = String(song).trim();
  if (a) {
    url.searchParams.set("track_name", s);
    url.searchParams.set("artist_name", a);
  } else {
    // No artist: the free-text search, which matches the song name against
    // titles and artists both, rather than an empty artist filter.
    url.searchParams.set("q", s);
  }
  return url.toString();
}

async function defaultFetchJson(url, signal) {
  const res = await fetch(url, { signal, headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`HTTP ${res.status} from ${new URL(url).host}`);
  return res.json();
}

/**
 * Search LRCLIB and rank what comes back for a track `duration` seconds long.
 * Resolves to [] when nothing matches, and rejects when LRCLIB cannot be
 * reached, so the tab can tell the two apart.
 */
export async function searchLyrics({ artist = "", song = "", duration = 0 }, { fetchJson = defaultFetchJson, signal } = {}) {
  if (!String(song).trim()) return [];
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(new Error("timeout")), TIMEOUT_MS);
  signal?.addEventListener("abort", () => controller.abort(signal.reason), { once: true });
  try {
    const rows = await fetchJson(searchUrl({ artist, song }), controller.signal);
    return rankMatches(rows, duration);
  } finally {
    clearTimeout(timer);
  }
}
