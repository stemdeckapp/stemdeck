// Seeing, hearing and downloading the MIDI of a lane (Advanced mode), the
// pure part: the piano roll's rectangles (on the Drums lane one row per kind
// of drum, each named), the lane's source switch (Stem, MIDI, Both), the
// download choices of the lane's download button and the synth voice each
// lane is played with. No DOM and no Web Audio, so
// tests/js/midi-notes-view.test.mjs runs it in node. laneMidiRows.js draws,
// midiSynth.js renders the sound.
//
// The notes come from GET /api/jobs/{id}/midi/{stem}/notes:
// { notes: [[start s, end s, pitch, velocity], ...], bpm, tempo_source }.

const isNum = (v) => typeof v === "number" && Number.isFinite(v);

/** The usable notes of a notes answer, each [start, end, pitch, velocity], by start. */
export function cleanNotes(answer) {
  const list = Array.isArray(answer?.notes) ? answer.notes : [];
  const out = [];
  for (const n of list) {
    if (!Array.isArray(n) || n.length < 4) continue;
    const [s, e, p, v] = n;
    if (!isNum(s) || !isNum(e) || !Number.isInteger(p) || p < 0 || p > 127 || e <= s || s < 0) continue;
    out.push([s, e, p, isNum(v) ? Math.max(0, Math.min(127, Math.round(v))) : 100]);
  }
  out.sort((a, b) => a[0] - b[0] || a[2] - b[2]);
  return out;
}

// ─── The piano roll ───

/** Pitches kept free above and below the lane's notes. */
export const ROLL_PAD = 2;
/** The smallest pitch range a roll spans, so a few notes are not each a lane tall. */
export const ROLL_MIN_SPAN = 12;
/** Each note's height, of its pitch row, and the gap the rest leaves. */
export const ROLL_NOTE_H = 0.8;

/**
 * The roll of `notes` over a song of `duration` seconds, in the units of an
 * SVG viewBox stretched over the lane (preserveAspectRatio none): x is
 * seconds, so it follows the waveform's timeline and zoom; y is pitch rows,
 * highest at the top, over the lane's own note range with ROLL_PAD rows
 * spare. Velocity is the note's opacity, 0.35 to 1, behind the waveform
 * (Sound: Stem) and beside it (Both); when the lane plays its MIDI every
 * note is drawn at full strength (waves.css), as bright as the waveform.
 * Returns { width, height, lo, hi, rects: [{ x, y, w, h, o }] }, or null
 * when there is nothing to draw.
 */
export function rollGeometry(notes, duration) {
  if (!Array.isArray(notes) || !notes.length || !isNum(duration) || duration <= 0) return null;
  let min = 127;
  let max = 0;
  for (const n of notes) {
    if (n[2] < min) min = n[2];
    if (n[2] > max) max = n[2];
  }
  let lo = min - ROLL_PAD;
  let hi = max + ROLL_PAD;
  if (hi - lo + 1 < ROLL_MIN_SPAN) {
    const grow = ROLL_MIN_SPAN - (hi - lo + 1);
    lo -= Math.floor(grow / 2);
    hi += Math.ceil(grow / 2);
  }
  const height = hi - lo + 1;
  const off = (1 - ROLL_NOTE_H) / 2;
  const rects = [];
  for (const [s, e, p, v] of notes) {
    if (s >= duration) continue;
    rects.push({
      x: s,
      y: hi - p + off,
      w: Math.min(e, duration) - s,
      h: ROLL_NOTE_H,
      o: Math.round((0.35 + 0.65 * (v / 127)) * 100) / 100,
    });
  }
  return { width: duration, height, lo, hi, rects };
}

// ─── The drums' roll ───

/**
 * The rows of the Drums lane's roll, top to bottom: cymbals, hi-hat, toms,
 * snare, kick, and every other General MIDI percussion sound in one row. Each
 * with the i18n key of its name.
 */
export const DRUM_ROWS = Object.freeze(["cymbal", "hihat", "tom", "snare", "kick", "perc"]);
// General MIDI percussion (channel 10) note numbers by row, as the engine
// comparison scored them (data/midi-bench/engine-scripts/common.py).
const GM_DRUM_ROW = {
  35: "kick", 36: "kick",
  37: "snare", 38: "snare", 39: "snare", 40: "snare",
  42: "hihat", 44: "hihat", 46: "hihat",
  41: "tom", 43: "tom", 45: "tom", 47: "tom", 48: "tom", 50: "tom",
  49: "cymbal", 51: "cymbal", 52: "cymbal", 53: "cymbal", 55: "cymbal", 57: "cymbal", 59: "cymbal",
};
/** The row a General MIDI percussion note is drawn in. */
export function drumRow(pitch) {
  return GM_DRUM_ROW[pitch] || "perc";
}

/** The i18n key of a drum row's name. */
export function drumRowKey(row) {
  return `laneMidi.drum.${DRUM_ROWS.includes(row) ? row : "perc"}`;
}

/**
 * The Drums lane's roll: like rollGeometry, but a row per kind of drum the
 * file has (DRUM_ROWS order, the others left out), and `rows` naming each row
 * and where it is: [{ row, key, y }], y its top in rows. A hit lasts about
 * 10 ms; it is drawn NOTE_MIN_PX wide (drawnWidth). null when there is
 * nothing to draw.
 */
export function drumGeometry(notes, duration) {
  if (!Array.isArray(notes) || !notes.length || !isNum(duration) || duration <= 0) return null;
  const present = new Set(notes.map((n) => drumRow(n[2])));
  const order = DRUM_ROWS.filter((r) => present.has(r));
  const off = (1 - ROLL_NOTE_H) / 2;
  const rects = [];
  for (const [s, e, p, v] of notes) {
    if (s >= duration) continue;
    rects.push({
      x: s,
      y: order.indexOf(drumRow(p)) + off,
      w: Math.min(e, duration) - s,
      h: ROLL_NOTE_H,
      o: Math.round((0.35 + 0.65 * (v / 127)) * 100) / 100,
    });
  }
  return {
    width: duration,
    height: order.length,
    lo: 0,
    hi: order.length - 1,
    rects,
    rows: order.map((row, y) => ({ row, key: drumRowKey(row), y })),
  };
}

/** The roll of a lane: the drums' rows for the Drums lane, pitch rows for any other. */
export function laneGeometry(lane, notes, duration) {
  return lane === "drums" ? drumGeometry(notes, duration) : rollGeometry(notes, duration);
}

const f = (n) => String(Math.round(n * 10000) / 10000);

/**
 * The narrowest a note is drawn, in screen pixels. The roll's x is seconds,
 * so a note's width on screen is its length times the lane's pixels per
 * second: at a whole song's zoom (about 4 px/s for a four minute song) a
 * 10 ms drum hit is 0.04 px and a 100 ms bass note 0.4 px, which the browser
 * paints as a faint, darkened hair instead of the lane's colour. A minimum in
 * seconds cannot hold at every zoom; this one is in pixels.
 */
export const NOTE_MIN_PX = 2;

/**
 * The width, in seconds, a note of `geometry` is drawn with when the lane
 * shows `pxPerSec` pixels a second: its length, or NOTE_MIN_PX on screen when
 * that is longer, never past the song's end. Its length as it is when the
 * scale is not known (0).
 */
export function drawnWidth(rect, geometry, pxPerSec) {
  const min = pxPerSec > 0 ? NOTE_MIN_PX / pxPerSec : 0;
  return Math.max(rect.w, Math.min(min, geometry.width - rect.x));
}

/** drawnWidth as the rect's width attribute. */
export function widthAttr(rect, geometry, pxPerSec) {
  return f(drawnWidth(rect, geometry, pxPerSec));
}

/** The roll as the inside of an SVG (its <rect>s), for one innerHTML write. */
export function rollRects(geometry, pxPerSec = 0) {
  if (!geometry) return "";
  return geometry.rects
    .map((r) => `<rect x="${f(r.x)}" y="${f(r.y)}" width="${widthAttr(r, geometry, pxPerSec)}" height="${f(r.h)}" fill-opacity="${r.o}"></rect>`)
    .join("");
}

/**
 * Whether a roll drawn at `drawnPxPerSec` must be widened again for
 * `pxPerSec`: when the scale changed by more than a tenth, so a zoom drag
 * rewrites the widths a few times, not on every frame, and a note never
 * drops under about 1.8 px.
 */
export function needsRefit(drawnPxPerSec, pxPerSec) {
  if (!(pxPerSec > 0)) return false;
  if (!(drawnPxPerSec > 0)) return true;
  return Math.abs(Math.log(pxPerSec / drawnPxPerSec)) > Math.log(1.1);
}

// ─── The lane's source switch ───

/** What a lane plays: its stem (the default), its MIDI, or both together. */
export const SOURCES = Object.freeze(["stem", "midi", "both"]);

/** A stored or asked-for source, made one of SOURCES. */
export function normalSource(value) {
  return SOURCES.includes(value) ? value : "stem";
}

/** The engine's name for a source (audioEngine.setLaneSource): the MIDI is its "alt". */
export function engineSource(source) {
  const s = normalSource(source);
  return s === "midi" ? "alt" : s;
}

/** Whether the MIDI sound is needed (it is rendered and added to the lane). */
export function needsMidiSound(source) {
  return normalSource(source) !== "stem";
}

/**
 * What the switch shows: hidden without a MIDI file; off with its reason
 * when no audio engine plays the song (it failed to load; both engines can
 * add the source, and the legacy <audio> path shows no MIDI rows at all);
 * off while the sound is being prepared.
 */
export function sourceSwitch({ hasFile, engineSupports, preparing = false }) {
  if (!hasFile) return { show: false, disabled: false, reasonKey: "" };
  if (!engineSupports) return { show: true, disabled: true, reasonKey: "laneMidi.source.unsupported" };
  return { show: true, disabled: preparing, reasonKey: "" };
}

// ─── The lane's download button ───

/** The choices of a lane's download button, or [] for the plain download. */
export function downloadChoices({ hasMidi }) {
  if (!hasMidi) return [];
  return [
    { id: "audio", key: "laneMidi.dl.audio" },
    { id: "midi", key: "laneMidi.dl.midi" },
    { id: "both", key: "laneMidi.dl.both" },
  ];
}

/** The audio format of a stem URL ("wav", "mp3", ...), as the zip asks for it. */
export function audioFormat(url) {
  const m = /\.([a-z0-9]+)(?:[?#]|$)/i.exec(String(url || ""));
  const ext = m ? m[1].toLowerCase() : "";
  return ["wav", "mp3", "flac", "ogg"].includes(ext) ? ext : "wav";
}

/** The "Audio and MIDI" zip: the one stem in its own format, and its .mid. */
export function bothZipUrl(jobId, stem, audioUrl) {
  const q = new URLSearchParams({ format: audioFormat(audioUrl), stems: stem, midi: "1" });
  return `/api/jobs/${encodeURIComponent(jobId)}/stems/all.zip?${q}`;
}

/** Its file name: "<song>_<stem>_audio_midi.zip" (the safe title of laneMidiView.js). */
export function bothZipName(safeTitle, stem) {
  return safeTitle ? `${safeTitle}_${stem}_audio_midi.zip` : `${stem}_audio_midi.zip`;
}

// ─── The synth voice ───

/**
 * The voice a lane's MIDI is played with: a soft electric-piano tone for
 * Keys (a sine and a quieter octave, a quick decay to a low sustain), a round
 * bass for Bass (a sine and a quiet saw through a low-pass), a plucked tone
 * for Guitar, a soft held tone for Vocals and a mellow pad for Other. Short
 * attack and release, so notes start on time and do not click. `gain` keeps a
 * full chord under clipping. The Drums lane is { kind: "drums" }: each hit
 * is a drum sound of its own (midiRender.renderDrums), not a pitch.
 */
export function voiceFor(stem) {
  if (stem === "drums") return { kind: "drums", partials: [], lowpass: 0, attack: 0.001, decay: 0, sustain: 0, release: 0.5, gain: 0.5 };
  if (stem === "bass") {
    return {
      partials: [{ type: "sine", ratio: 1, gain: 1 }, { type: "sawtooth", ratio: 1, gain: 0.3 }],
      lowpass: 900,
      attack: 0.004,
      decay: 0.25,
      sustain: 0.7,
      release: 0.08,
      gain: 0.5,
    };
  }
  if (stem === "guitar") {
    return {
      partials: [{ type: "triangle", ratio: 1, gain: 1 }, { type: "sine", ratio: 2, gain: 0.35 }, { type: "sine", ratio: 3, gain: 0.12 }],
      lowpass: 3500,
      attack: 0.003,
      decay: 0.6,
      sustain: 0.25,
      release: 0.15,
      gain: 0.3,
    };
  }
  if (stem === "vocals") {
    return {
      partials: [{ type: "sine", ratio: 1, gain: 1 }, { type: "sine", ratio: 2, gain: 0.2 }, { type: "sine", ratio: 3, gain: 0.08 }],
      lowpass: 0,
      attack: 0.03,
      decay: 0.3,
      sustain: 0.8,
      release: 0.12,
      gain: 0.35,
    };
  }
  if (stem === "other") {
    return {
      partials: [{ type: "sawtooth", ratio: 1, gain: 0.5 }, { type: "sine", ratio: 1, gain: 0.6 }],
      lowpass: 1800,
      attack: 0.04,
      decay: 0.4,
      sustain: 0.7,
      release: 0.3,
      gain: 0.2,
    };
  }
  return {
    partials: [{ type: "sine", ratio: 1, gain: 1 }, { type: "sine", ratio: 2, gain: 0.28 }, { type: "triangle", ratio: 4, gain: 0.05 }],
    lowpass: 0,
    attack: 0.005,
    decay: 0.9,
    sustain: 0.3,
    release: 0.25,
    gain: 0.22,
  };
}

/** A MIDI pitch in hertz (A4 = 69 = 440 Hz). */
export function pitchHz(pitch) {
  return 440 * 2 ** ((pitch - 69) / 12);
}

/** Note velocity as a level, 0 to 1, a little curved so soft notes stay audible. */
export function velocityGain(velocity) {
  const v = Math.max(0, Math.min(127, Number(velocity) || 0)) / 127;
  return Math.round(Math.sqrt(v) * 1000) / 1000;
}

/** The rendered sound's length: the song, or the last release, whichever is longer. */
export function renderSeconds(notes, duration, voice) {
  let end = isNum(duration) && duration > 0 ? duration : 0;
  for (const n of notes) end = Math.max(end, n[1] + voice.release + 0.05);
  return end;
}

/**
 * The gain that puts the rendered MIDI at the loudness of the lane's stem:
 * the ratio of their RMS levels, kept within 0.05 to 4 and under a peak of
 * 0.98, so switching Stem and MIDI does not jump in level. 1 without a stem
 * level to match (a silent stem, a rendering of silence).
 */
export function levelScale(renderRms, stemRms, renderPeak) {
  if (!(renderRms > 0) || !(stemRms > 0)) return 1;
  let k = Math.max(0.05, Math.min(4, stemRms / renderRms));
  if (renderPeak > 0) k = Math.min(k, 0.98 / renderPeak);
  return Math.round(k * 10000) / 10000;
}

/** RMS and peak of `samples`, every `stride`th sample (a fast estimate). */
export function levelOf(samples, stride = 4) {
  let sum = 0;
  let n = 0;
  let peak = 0;
  for (let i = 0; i < samples.length; i += stride) {
    const v = samples[i];
    sum += v * v;
    n++;
    const a = v < 0 ? -v : v;
    if (a > peak) peak = a;
  }
  return { rms: n ? Math.sqrt(sum / n) : 0, peak };
}

/** Which rendering a lane can reuse: the song, the lane, its file and the rate. */
export function soundKey(jobId, stem, at, sampleRate) {
  return `${jobId}|${stem}|${at || ""}|${sampleRate}`;
}
