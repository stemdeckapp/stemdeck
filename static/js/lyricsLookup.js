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

/**
 * Best first, for a track `duration` seconds long (0 when unknown):
 * closest in length, then synced over plain among versions within
 * SAME_LENGTH_SEC of each other, then whatever LRCLIB ranked first.
 */
export function rankMatches(rows, duration = 0) {
  const matches = (Array.isArray(rows) ? rows : [])
    .map(normalise)
    .filter((m) => m.id && (m.synced || m.plain || m.instrumental));
  const off = (m) => (duration && m.duration ? Math.abs(m.duration - duration) : 0);
  return matches
    .map((m, rank) => ({ m, rank }))
    .sort((a, b) => {
      const da = off(a.m);
      const db = off(b.m);
      if (Math.abs(da - db) > SAME_LENGTH_SEC) return da - db;
      if (Boolean(a.m.synced) !== Boolean(b.m.synced)) return a.m.synced ? -1 : 1;
      return da - db || a.rank - b.rank;
    })
    .map(({ m }) => m);
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
  for (const raw of String(text || "").split(/\r?\n/)) {
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
    for (const time of stamps) lines.push(words ? { time, text: plain, words } : { time, text: plain });
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

/** Word stamps in a line's text, [{ time, text }], or null when it has none. */
function wordStamps(text) {
  const pieces = [];
  let last = null;
  let cursor = 0;
  for (const m of text.matchAll(WORD_STAMP)) {
    if (last) last.text = text.slice(cursor, m.index);
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
// character at a time instead.
const UNSPACED = /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}]/u;

/** A line's text as the pieces the wipe fills: words, or characters. */
function lineTokens(text) {
  return UNSPACED.test(text) && !/\s/.test(text) ? Array.from(text) : text.match(/\S+\s*/g) || [];
}

/**
 * About how many syllables `token` is sung in: vowel groups in Latin
 * script, less a silent final e ("home", "'cause", but not "table"), and
 * for other scripts one per two or three letters. At least one.
 */
export function syllables(token) {
  const word = token.trim();
  if (!word) return 0;
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
