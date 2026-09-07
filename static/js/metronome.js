// Click track scheduler.
//
// Plays a click at each beat time from the backend's beat grid
// (stems/beats.json), locked to the audio engine's clock.
//
// The alignment contract, which is the whole point of this module:
//
//   Clicks are scheduled in the engine's *source* time domain via
//   `engine.sourceTimeToCtxTime(mediaTime)` -- the exact inverse of the maths
//   the engine uses to start its own stem sources -- and are connected to the
//   engine's unpitched bus rather than straight to ctx.destination. So a click
//   and the drum sample it belongs with enter the graph in the same frame and
//   share the tempo stage. Key transposition never resamples either one.
//
// Timers cannot be trusted for audio, so nothing here decides *when* a click
// sounds: setInterval only wakes us up to hand future clicks to the Web Audio
// clock, which is sample-accurate. Late or jittery wake-ups cost scheduling
// headroom, never accuracy.

// How far ahead clicks are handed to the audio clock, and how often we wake to
// do it. The gap between them is the jitter budget for the timer: a wake-up up
// to ~200 ms late still schedules on time. Background tabs throttle timers to
// ~1 s, which exceeds that -- see the catch-up handling in _tick.
const LOOKAHEAD_SEC = 0.25;
const TICK_MS = 50;

// Click voice. Two sine partials give the tick some body without a sample
// file; the short exponential decay keeps the transient tight so it reads as
// percussive rather than tonal.
const CLICK_FREQ = 1000;
// Group starts sit between the downbeat and a plain beat in pitch and level, so
// a grouped bar reads as strong/medium/weak rather than as identical accents
// (#595). Geometric middle of the two: the ear hears pitch ratios, so the
// midpoint of 1000 and 1500 is 1225, not 1250. Mirrored in click_render.py.
const GROUP_FREQ = 1225;
const ACCENT_FREQ = 1500;
const CLICK_DECAY = 0.035;
const CLICK_ATTACK = 0.001;

// Click strength at one beat. click_render.py defines the same three.
export const LEVEL_WEAK = 0;
export const LEVEL_GROUP = 1;
export const LEVEL_DOWNBEAT = 2;

/**
 * How a bar of `beatsPerBar` divides when the user has not said.
 *
 * Mirror of default_grouping() in app/pipeline/click_render.py -- keep both in
 * step. 2, 3 and 4 deliberately return one group so they sound exactly as they
 * did; 4/4's real secondary stress on beat 3 is a choice for the user, not a
 * default that changes the commonest meter for everyone.
 * @param {number} beatsPerBar
 * @returns {number[]}
 */
export function defaultGrouping(beatsPerBar) {
  if (!Number.isInteger(beatsPerBar) || beatsPerBar < 1) return [];
  if (beatsPerBar === 5 || beatsPerBar === 7) {
    // Long group first: Take Five is 3+2, and 7/8 is more often 3+2+2.
    return [3, ...Array((beatsPerBar - 3) >> 1).fill(2)];
  }
  // Compound: 6, 9 and 12 are felt in dotted-quarter groups of three.
  if (beatsPerBar >= 6 && beatsPerBar % 3 === 0) return Array(beatsPerBar / 3).fill(3);
  return [beatsPerBar];
}

/**
 * A grouping safe to index a bar with, or the default. Anything that is not a
 * list of positive integers summing to the bar length is rejected wholesale
 * rather than repaired: a half-understood grouping would accent beats the user
 * never asked for, so falling back to the default is the honest failure.
 * @param {number[]|null|undefined} groups
 * @param {number} beatsPerBar
 * @returns {number[]}
 */
export function normaliseGrouping(groups, beatsPerBar) {
  if (!Number.isInteger(beatsPerBar) || beatsPerBar < 1) return [];
  if (!Array.isArray(groups) || !groups.length) return defaultGrouping(beatsPerBar);
  if (!groups.every((g) => Number.isInteger(g) && g >= 1)) return defaultGrouping(beatsPerBar);
  if (groups.reduce((a, b) => a + b, 0) !== beatsPerBar) return defaultGrouping(beatsPerBar);
  return groups.slice();
}

/** Beat offsets inside a bar that start a group, excluding the downbeat. */
export function groupOffsets(groups) {
  const out = new Set();
  let at = 0;
  for (let i = 0; i < groups.length - 1; i++) {
    at += groups[i];
    out.add(at);
  }
  return out;
}

/** The click level for a beat `offset` into a bar of `beatsPerBar`. */
export function levelAt(offset, beatsPerBar, groups) {
  if (offset === 0) return LEVEL_DOWNBEAT;
  return groupOffsets(normaliseGrouping(groups, beatsPerBar)).has(offset)
    ? LEVEL_GROUP
    : LEVEL_WEAK;
}

// ─── Count-in (issue #269) ───────────────────────────────────────────────
//
// A count-in is one bar of click *before* playback, leading into the start
// position. The maths below is a literal mirror of count_in_beats() in
// app/pipeline/click_render.py so the live count-in and the exported one agree
// beat-for-beat -- the same parity discipline that pins the click voice across
// the two files. Kept pure and module-level so it can be unit-tested and reused
// by the export URL builder without a live AudioContext.

function _rescaleForCount(beats, mult) {
  if (mult === 2) {
    const out = [];
    for (let i = 0; i < beats.length - 1; i++) out.push(beats[i], (beats[i] + beats[i + 1]) / 2);
    if (beats.length) out.push(beats[beats.length - 1]);
    return out;
  }
  if (mult === 0.5) return beats.filter((_, i) => i % 2 === 0);
  return beats.slice();
}

function _countInBeatsPerBar(bars, accentMode, startIndex) {
  if (accentMode > 0) return accentMode;
  // Auto / off: the detected meter in force at the start beat, else 4. A
  // count-in always needs a bar length, even with accents switched off.
  let mark = null;
  for (const b of bars) {
    if (Number.isInteger(b.beat) && b.beat <= startIndex) mark = b;
    else break;
  }
  if (mark && Number.isInteger(mark.beats_per_bar) && mark.beats_per_bar >= 1) {
    return mark.beats_per_bar;
  }
  return 4;
}

function _intervalNear(grid, start, span) {
  if (grid.length < 2) return null;
  let i = 0;
  while (i < grid.length && grid[i] < start) i++;
  i = Math.min(i, grid.length - 2);
  const diffs = [];
  for (let k = i; k < Math.min(i + Math.max(1, span), grid.length - 1); k++) {
    const d = grid[k + 1] - grid[k];
    if (d > 0) diffs.push(d);
  }
  if (!diffs.length) return null;
  diffs.sort((a, b) => a - b);
  return diffs[diffs.length >> 1];
}

/**
 * The count-in that leads into playback at `start`.
 * @returns {{leadIn:number, clicks:{offset:number, level:number, accent:boolean}[]}}
 *   `leadIn` seconds of pre-roll, and clicks at offsets in `[0, leadIn)`.
 *   `level` is LEVEL_WEAK/GROUP/DOWNBEAT; `accent` stays as the downbeat-only
 *   boolean so callers that only care about "is this the 1" keep working.
 */
export function computeCountIn(
  beats,
  bars,
  { countBars = 1, multiplier = 1, accentMode = -1, start = 0, groups = null } = {},
) {
  if (countBars < 1) return { leadIn: 0, clicks: [] };
  const clean = Array.isArray(beats) ? beats.filter((b) => Number.isFinite(b)) : [];
  const grid = _rescaleForCount(clean, multiplier);
  let startIndex = 0;
  for (let k = 0; k < clean.length; k++) {
    if (clean[k] <= start) startIndex = k;
    else break;
  }
  const bpb = _countInBeatsPerBar(Array.isArray(bars) ? bars : [], accentMode, startIndex);
  const interval = _intervalNear(grid, start, bpb);
  if (interval === null) return { leadIn: 0, clicks: [] };
  const n = countBars * bpb;
  // The count-in is its own run of bars, so the grouping comes from bpb rather
  // than from the bar marks: there is no detected bar in front of the audio.
  // A user grouping only applies to an explicit meter, matching count_in_beats.
  const barGroups = accentMode > 0 ? normaliseGrouping(groups, bpb) : defaultGrouping(bpb);
  const starts = groupOffsets(barGroups);
  const clicks = [];
  for (let j = 0; j < n; j++) {
    const offset = j % bpb;
    const level = offset === 0 ? LEVEL_DOWNBEAT : starts.has(offset) ? LEVEL_GROUP : LEVEL_WEAK;
    clicks.push({ offset: j * interval, level, accent: offset === 0 });
  }
  return { leadIn: n * interval, clicks };
}

/**
 * @param {object} engine        Audio engine exposing sourceTimeToCtxTime,
 *                               ctxTimeToSourceTime, getScheduleEpoch,
 *                               isClockReady, getMasterNode, audioContext.
 * @param {number[]} beats       Ascending beat times in seconds.
 * @param {{volume?:number, beatsPerBar?:number}} opts
 */
export function createMetronome(engine, beats, { volume = 0.6, beatsPerBar = 0 } = {}) {
  const ctx = engine?.audioContext;
  const master = engine?.getMasterNode?.();
  if (!ctx || !master
      || typeof engine.sourceTimeToCtxTime !== "function"
      || typeof engine.ctxTimeToSourceTime !== "function") {
    console.warn("[metronome] engine does not support click scheduling");
    return null;
  }

  let base = Array.isArray(beats) ? beats.filter((b) => Number.isFinite(b)) : [];
  if (!base.length) {
    console.warn("[metronome] empty beat grid; click track unavailable");
    return null;
  }

  // Which pulse counts as "the beat" is a judgement the tracker cannot always
  // make correctly -- a half-time grid lands a click on a real drum hit every
  // single time and still clicks half as often as the music. Rather than
  // guessing (a midpoint-energy heuristic fires on any song with 8th-note
  // hats, i.e. most of them), the grid is rescalable at playback: halving
  // takes every other beat, doubling inserts midpoints. One click either way.
  let grid = base;
  let _multiplier = 1;
  // Optional accent predicate supplied by the grid editor: given a beat index
  // into the *current* grid, is it a downbeat? When null, accents fall back to
  // a fixed beats-per-bar count from the start of the track.
  let _isDownbeat = null;
  // Bar-position lookup (offset, barLength) so bar marks can be grouped, not
  // merely accented on the 1. Falls back to _isDownbeat when absent (#595).
  let _barPosition = null;
  let _groups = null;

  // Map an index in the rescaled grid back to the original beat it came from.
  // Bar marks are recorded against the *detected* beats, so accents have to be
  // decided in that index space: at x2 the odd entries are inserted midpoints
  // belonging to no original beat, and at /2 every entry is two apart. Without
  // this the accent lands on the wrong beat whenever the rate is not 1x.
  // Mirrored by source_index() in app/pipeline/click_render.py so exports agree.
  function _sourceIndex(i) {
    if (_multiplier === 2) return i % 2 === 0 ? i / 2 : null;
    if (_multiplier === 0.5) return i * 2;
    return i;
  }

  function _rescale(mult) {
    if (mult === 2) {
      const out = [];
      for (let i = 0; i < base.length - 1; i++) {
        out.push(base[i], (base[i] + base[i + 1]) / 2);
      }
      out.push(base[base.length - 1]);
      return out;
    }
    if (mult === 0.5) return base.filter((_, i) => i % 2 === 0);
    return base;
  }

  const gain = ctx.createGain();
  gain.gain.value = Math.max(0, volume);
  gain.connect(master);

  let enabled = false;
  let destroyed = false;
  let timerId = null;
  let _beatsPerBar = beatsPerBar;

  // Index of the next beat to schedule, and the epoch that index belongs to.
  // A mismatch means the engine seeked/looped/changed rate under us and both
  // the cursor and every queued click are stale.
  let cursor = 0;
  let epoch = -1;
  /** @type {{osc:OscillatorNode, env:GainNode}[]} */
  let queued = [];
  // Count-in one-shots are tracked separately from the running click's `queued`:
  // they are scheduled outside the _tick loop (which may not even be running when
  // the click track is off) and must survive until they sound or the transport
  // cancels them. See playCountIn / cancelCountIn.
  /** @type {{osc:OscillatorNode, env:GainNode}[]} */
  let countInQueued = [];

  // First beat at or after `t`. Binary search rather than a scan: tracks run to
  // thousands of beats and this runs on every seek.
  function _indexAtOrAfter(t) {
    let lo = 0;
    let hi = grid.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (grid[mid] < t) lo = mid + 1;
      else hi = mid;
    }
    return lo;
  }

  function _cancelQueued() {
    for (const { osc, env } of queued) {
      try { osc.stop(); } catch { /* already stopped */ }
      try { env.disconnect(); } catch { /* noop */ }
    }
    queued = [];
  }

  function _cancelCountIn() {
    for (const { osc, env } of countInQueued) {
      try { osc.stop(); } catch { /* already stopped */ }
      try { env.disconnect(); } catch { /* noop */ }
    }
    countInQueued = [];
  }

  // Schedule one click to sound at AudioContext time `when`. `sink` is the list
  // it registers itself in so the right batch can be torn down independently
  // (the running click's `queued`, or the count-in's `countInQueued`).
  function _scheduleClick(when, level, sink = queued) {
    // Booleans still arrive from callers that only distinguish the downbeat.
    const lv = level === true ? LEVEL_DOWNBEAT : level === false ? LEVEL_WEAK : Number(level) || 0;
    const osc = ctx.createOscillator();
    const env = ctx.createGain();
    osc.type = "sine";
    osc.frequency.value =
      lv === LEVEL_DOWNBEAT ? ACCENT_FREQ : lv === LEVEL_GROUP ? GROUP_FREQ : CLICK_FREQ;

    // Ramp from near-silence rather than 0: setValueAtTime(0) followed by an
    // exponential ramp is a no-op in the spec (exponential ramps cannot start
    // at zero), which would leave the click at full level and click twice.
    env.gain.setValueAtTime(0.0001, when);
    env.gain.exponentialRampToValueAtTime(
      lv === LEVEL_DOWNBEAT ? 1.0 : lv === LEVEL_GROUP ? 0.85 : 0.7,
      when + CLICK_ATTACK,
    );
    env.gain.exponentialRampToValueAtTime(0.0001, when + CLICK_DECAY);

    osc.connect(env);
    env.connect(gain);
    osc.start(when);
    osc.stop(when + CLICK_DECAY + 0.01);

    const entry = { osc, env };
    sink.push(entry);
    osc.onended = () => {
      try { env.disconnect(); } catch { /* noop */ }
      const i = sink.indexOf(entry);
      if (i >= 0) sink.splice(i, 1);
    };
  }

  function _tick() {
    if (destroyed || !enabled) return;

    if (!engine.isClockReady?.()) {
      // Paused, or mid-start before the clock is valid. Drop anything queued so
      // a click cannot fire into silence, and re-sync on the next tick.
      if (queued.length) _cancelQueued();
      epoch = -1;
      return;
    }

    const currentEpoch = engine.getScheduleEpoch();
    if (currentEpoch !== epoch) {
      // Seek, loop jump, or rate change: the mapping moved, so everything
      // already handed to the audio clock is wrong. Tear it down and re-anchor
      // the cursor to wherever the playhead now is.
      _cancelQueued();
      epoch = currentEpoch;
      // Anchor in the source domain, not on getCurrentTime(): the latter is the
      // output playhead the UI draws, which is a different quantity once
      // SoundTouch is stretching. Beats are scheduled in source time, so the
      // cursor has to be found in source time or the two disagree by the
      // stretcher's buffer depth.
      cursor = _indexAtOrAfter(engine.ctxTimeToSourceTime(ctx.currentTime));
    }

    const horizon = ctx.currentTime + LOOKAHEAD_SEC;
    while (cursor < grid.length) {
      const when = engine.sourceTimeToCtxTime(grid[cursor]);
      if (when > horizon) break;
      // A beat whose time has already passed cannot be scheduled -- Web Audio
      // would fire it immediately, which is worse than silence because it lands
      // off the beat. Skip it. This is the background-tab case: a throttled
      // timer wakes late and we drop the clicks it slept through rather than
      // machine-gunning them.
      if (when > ctx.currentTime) {
        // Bar marks from the editor win over the fixed beats-per-bar count:
        // on a track whose meter changes, a fixed count is meaningless.
        const src = _sourceIndex(cursor);
        let level = LEVEL_WEAK;
        if (src !== null) {
          const pos = _barPosition ? _barPosition(src) : null;
          if (pos) {
            // Bar marks: each bar is grouped by its own length, so a detected
            // 6/8 passage groups in threes with nothing configured. A user
            // grouping is not applied here -- under bar marks the length can
            // change bar to bar, so one grouping cannot be assumed to fit.
            level = levelAt(pos[0], pos[1], null);
          } else if (_isDownbeat) {
            level = _isDownbeat(src) ? LEVEL_DOWNBEAT : LEVEL_WEAK;
          } else if (_beatsPerBar > 0) {
            level = levelAt(src % _beatsPerBar, _beatsPerBar, _groups);
          }
        }
        _scheduleClick(when, level);
      }
      cursor++;
    }
  }

  function _start() {
    if (timerId !== null) return;
    epoch = -1; // force a re-anchor on the first tick
    timerId = setInterval(_tick, TICK_MS);
    _tick();
  }

  function _stop() {
    if (timerId !== null) { clearInterval(timerId); timerId = null; }
    _cancelQueued();
    epoch = -1;
  }

  return {
    setEnabled(on) {
      if (destroyed || enabled === !!on) return;
      enabled = !!on;
      if (enabled) _start();
      else _stop();
    },
    isEnabled: () => enabled,
    setVolume(v) {
      if (destroyed) return;
      gain.gain.setTargetAtTime(Math.max(0, v), ctx.currentTime, 0.01);
    },
    /**
     * Play a one-shot count-in: clicks at the given *source* times (typically
     * negative -- before the audio), routed through the same voice and bus as
     * the running click so they inherit its volume and the SoundTouch path.
     * Independent of `enabled`, so a count-in can precede a clean (click-off)
     * playback. The engine must already have been told to start late (see
     * audioEngine.play(leadIn)) so these map into the silent lead-in gap.
     * @param {{time:number, accent:boolean}[]} clicks
     */
    playCountIn(clicks) {
      if (destroyed || !Array.isArray(clicks) || !clicks.length) return;
      _cancelCountIn();
      for (const c of clicks) {
        const when = engine.sourceTimeToCtxTime(c.time);
        if (when > ctx.currentTime) _scheduleClick(when, c.level ?? (c.accent ? LEVEL_DOWNBEAT : LEVEL_WEAK), countInQueued);
      }
    },
    /** Tear down a count-in already handed to the audio clock (pause/stop). */
    cancelCountIn() {
      if (!destroyed) _cancelCountIn();
    },
    /** Bar-position lookup used to group bar-marked bars; null to drop it. */
    setBarPositionFn(fn) {
      _barPosition = typeof fn === "function" ? fn : null;
    },
    /** Grouping for the explicit meter, e.g. [3,2,2]; null for the default. */
    setGrouping(groups) {
      _groups = Array.isArray(groups) && groups.length ? groups.slice() : null;
    },
    /** 0 disables accents; otherwise accent every Nth beat from the grid start. */
    setBeatsPerBar(n) {
      _beatsPerBar = Number.isFinite(n) && n > 0 ? Math.round(n) : 0;
      // Re-anchor so the change is heard from the next beat, not the next seek.
      if (enabled) { _cancelQueued(); epoch = -1; }
    },
    getBeatCount: () => grid.length,
    /**
     * Replace the beat grid in place -- used by the editor so a dragged beat
     * is audible on the next click rather than after a reload. Re-anchors so
     * queued clicks scheduled against the old grid are torn down.
     */
    setBeats(next) {
      const clean = Array.isArray(next) ? next.filter((b) => Number.isFinite(b)) : [];
      if (!clean.length) return;
      base = clean;
      grid = _rescale(_multiplier);
      if (enabled) { _cancelQueued(); epoch = -1; }
    },
    /** Accent predicate over beat indices, or null to use beatsPerBar. */
    setDownbeatFn(fn) {
      _isDownbeat = typeof fn === "function" ? fn : null;
      if (enabled) { _cancelQueued(); epoch = -1; }
    },
    /**
     * Rescale the grid to a different metrical level: 0.5 halves the click
     * rate, 2 doubles it, 1 restores the detected grid. Always derived from
     * the original beats, so switching between levels never compounds.
     */
    setMultiplier(mult) {
      const m = mult === 0.5 || mult === 2 ? mult : 1;
      if (m === _multiplier) return;
      _multiplier = m;
      grid = _rescale(m);
      // Re-anchor from the next tick so the change is heard immediately
      // rather than after the already-queued clicks drain.
      if (enabled) { _cancelQueued(); epoch = -1; }
    },
    getMultiplier: () => _multiplier,
    /** Effective BPM at the current level, from the median interval. */
    getEffectiveBpm() {
      if (grid.length < 2) return null;
      const d = [];
      for (let i = 1; i < grid.length; i++) d.push(grid[i] - grid[i - 1]);
      d.sort((a, b) => a - b);
      const med = d[d.length >> 1];
      return med > 0 ? 60 / med : null;
    },
    destroy() {
      destroyed = true;
      _stop();
      _cancelCountIn();
      try { gain.disconnect(); } catch { /* noop */ }
    },
  };
}
