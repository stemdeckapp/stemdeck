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
//   engine's master bus rather than straight to ctx.destination. So a click
//   and the audio sample it belongs with enter the graph in the same frame and
//   travel an identical path to the speakers. Whatever SoundTouch does
//   downstream, it does to both equally.
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
const ACCENT_FREQ = 1500;
const CLICK_DECAY = 0.035;
const CLICK_ATTACK = 0.001;

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
 * @returns {{leadIn:number, clicks:{offset:number, accent:boolean}[]}}
 *   `leadIn` seconds of pre-roll, and clicks at offsets in `[0, leadIn)`.
 */
export function computeCountIn(
  beats,
  bars,
  { countBars = 1, multiplier = 1, accentMode = -1, start = 0 } = {},
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
  const clicks = [];
  for (let j = 0; j < n; j++) clicks.push({ offset: j * interval, accent: j % bpb === 0 });
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
  function _scheduleClick(when, accent, sink = queued) {
    const osc = ctx.createOscillator();
    const env = ctx.createGain();
    osc.type = "sine";
    osc.frequency.value = accent ? ACCENT_FREQ : CLICK_FREQ;

    // Ramp from near-silence rather than 0: setValueAtTime(0) followed by an
    // exponential ramp is a no-op in the spec (exponential ramps cannot start
    // at zero), which would leave the click at full level and click twice.
    env.gain.setValueAtTime(0.0001, when);
    env.gain.exponentialRampToValueAtTime(accent ? 1.0 : 0.7, when + CLICK_ATTACK);
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
        const accent = _isDownbeat
          ? src !== null && !!_isDownbeat(src)
          : (_beatsPerBar > 0 && src !== null && src % _beatsPerBar === 0);
        _scheduleClick(when, accent);
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
        if (when > ctx.currentTime) _scheduleClick(when, !!c.accent, countInQueued);
      }
    },
    /** Tear down a count-in already handed to the audio clock (pause/stop). */
    cancelCountIn() {
      if (!destroyed) _cancelCountIn();
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
