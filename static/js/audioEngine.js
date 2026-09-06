// Web Audio decode-and-mix playback engine.
//
// Safari/WKWebView goes choppy when playing N streaming <audio> elements (one per
// stem) over HTTP/1.1: the 6-connection-per-origin cap + small media buffers + the
// multitrack's per-element currentTime nudging cause underruns. This engine instead
// decodes each active stem once into an AudioBuffer and plays them all from a single
// AudioContext clock — sample-accurate, zero streaming connections during playback,
// no drift. Works identically on WKWebView, Safari, and Chrome.
//
// Graph:  per-stem AudioBufferSourceNode -> GainNode (vol/mute/solo) -> AnalyserNode (VU)
//                                                                    -> masterGain -> SoundTouchNode -> destination
//
// Used behind a feature flag (see player.js) so it can be A/B'd against the legacy
// streaming path before cutover.

const AudioCtx = window.AudioContext || window.webkitAudioContext;

/**
 * @param {{name:string,url:string,controlName?:string,pitched?:boolean,visualOnly?:boolean}[]} stems
 * @param {{onTime?:(t:number)=>void, onEnded?:()=>void}} cbs
 */
import {
  INPUT_COUNT, ZERO_INPUT, clampPitch, effectivePitch, inputForPitch,
} from "./pitchBus.js";

export function createAudioEngine(stems, { onTime, onEnded, context } = {}) {
  // Mobile/iOS only starts audio from a context resumed inside a user gesture.
  // Callers can pass a shared, gesture-unlocked `context` (the mobile UI does);
  // desktop passes none and we own a fresh one. We only close contexts we own.
  const ctx = context || new AudioCtx();
  const ownsCtx = !context;
  const master = ctx.createGain();
  // One bus per semitone the worklet offers. A lane's transpose is expressed
  // as which bus it is connected to, so several lanes can sit in different
  // keys at once while still sharing the worklet's single tempo stage.
  const buses = Array.from({ length: INPUT_COUNT }, () => ctx.createGain());
  // How many tracks sit on each bus. A pitch bus is wired into the worklet only
  // while this is non-zero; the unpitched bus is wired for good. See the same
  // field in chunkedAudioEngine.js for why: a bus wired with nothing playing
  // into it still reaches the processor as a channel of silence, which it took
  // as a lane to transpose, so twelve pitch chains ran on silence for every
  // track at zero transpose (#576, #575).
  const busLanes = new Array(INPUT_COUNT).fill(0);
  master.connect(ctx.destination);

  let _playbackRate = 1.0;

  // SoundTouch takes one input per semitone. Input ZERO_INPUT is the plain
  // delay line: drums, the click, and any lane sitting at its own key.
  let stNode = null;
  // Reported by the processor, because deriving it here would mean keeping a
  // copy of its buffering constants in sync by hand.
  let _workletLatencyFrames = 0;
  const _workletReady = (ctx.audioWorklet
    ? ctx.audioWorklet.addModule('/vendor/soundtouch-processor.js').then(() => {
        stNode = new AudioWorkletNode(ctx, 'soundtouch-processor', {
          numberOfInputs: INPUT_COUNT,
          numberOfOutputs: 1,
          outputChannelCount: [2],
        });
        stNode.port.onmessage = (event) => {
          if (event?.data?.type === 'latency') _workletLatencyFrames = event.data.frames || 0;
        };
        // The worklet loads asynchronously, so anything set before it arrived
        // would otherwise be dropped. Re-apply the current value now.
        stNode.parameters.get('tempo').value = _playbackRate;
        // Tracks decoded before the worklet arrived are already routed, so
        // wire whichever pitch buses they occupy.
        buses[ZERO_INPUT].connect(stNode, 0, ZERO_INPUT);
        for (let k = 0; k < INPUT_COUNT; k++) {
          if (k !== ZERO_INPUT && busLanes[k] > 0) buses[k].connect(stNode, 0, k);
        }
        stNode.connect(master);
      }).catch((err) => {
        console.warn('[audioEngine] SoundTouch worklet load failed, using tape-effect fallback:', err);
        for (const bus of buses) bus.connect(master);
      })
    : Promise.resolve().then(() => {
        for (const bus of buses) bus.connect(master);
      }));

  /** @type {Map<string,{buffer:AudioBuffer,gain:GainNode,analyser:AnalyserNode,source:AudioBufferSourceNode|null,controlName:string,pitched:boolean,visualOnly:boolean}>} */
  const tracks = new Map();
  let duration = 0;
  let playing = false;
  let startCtxTime = 0; // ctx.currentTime at playback start
  let startOffset = 0; // media offset at that moment
  let rafId = null;
  let destroyed = false;
  let loop = { enabled: false, start: 0, end: 0 };
  // Bumped whenever the media-time -> ctx-time mapping below changes (start,
  // seek, loop jump, rate change, pause). The metronome watches this to know
  // when its already-scheduled clicks are stale and must be torn down.
  let _epoch = 0;
  // Why ready() resolved false, in words fit to show a user. Mirrors the same
  // accessor on the chunked engine so callers need not know which one they hold.
  let _loadError = null;

  // Decode all stems up front AND load the SoundTouch worklet in parallel.
  // Resolves true once at least one stem is ready (worklet load is best-effort).
  const ready = (async () => {
    // Counted so the failure can name a cause rather than arriving as a silent
    // console warning (#359). A fetch that never landed and a file the decoder
    // rejected are different problems for the user.
    let unreachable = 0;
    let undecodable = 0;

    await Promise.all([
      _workletReady,
      ...stems.map(async (s) => {
        if (!s?.url) return;
        let bytes;
        try {
          const res = await fetch(s.url);
          if (!res.ok) throw new Error(`fetch ${res.status}`);
          bytes = await res.arrayBuffer();
        } catch (e) {
          unreachable++;
          console.warn(`[audioEngine] fetch failed for ${s.name}:`, e);
          return;
        }
        try {
          const buffer = await ctx.decodeAudioData(bytes);
          if (destroyed) return;
          const visualOnly = s.visualOnly === true;
          const pitchable = s.name !== "drums" && s.pitched !== false;
          const gain = ctx.createGain();
          const analyser = ctx.createAnalyser();
          analyser.fftSize = 1024;
          if (!visualOnly) gain.connect(analyser);
          const track = {
            buffer,
            gain,
            analyser,
            source: null,
            name: s.name,
            controlName: s.controlName || s.name,
            pitchable,
            pitched: pitchable,
            pitch: 0,
            bus: null,
            level: 1,
            visualOnly,
          };
          tracks.set(s.name, track);
          routeTrack(track, true);
          duration = Math.max(duration, buffer.duration);
        } catch (e) {
          undecodable++;
          console.warn(`[audioEngine] decode failed for ${s.name}:`, e);
        }
      }),
    ]);

    const hasPlaybackTrack = [...tracks.values()].some((track) => !track.visualOnly);
    if (!hasPlaybackTrack) {
      _loadError = undecodable
        ? "This track's audio files are in a format StemDeck could not read."
        : unreachable
          ? "Could not load this track's audio files."
          : "This track has no stem files to play.";
    }
    return hasPlaybackTrack;
  })();

  // Clamped so the reported playhead never reads before the start position.
  // During a count-in the sources are scheduled to begin in the future
  // (startCtxTime > ctx.currentTime), which would otherwise make this go
  // negative -- the playhead must sit still at the start until the audio enters.
  // A no-op for a normal start, where startCtxTime == the moment play() ran.
  const wsolaLatencySeconds = () => {
    const needed = Math.round(0.012 * ctx.sampleRate)
      + Math.round(0.028 * ctx.sampleRate)
      + Math.round(0.082 * ctx.sampleRate);
    return Math.floor(needed / 128) * 128 / ctx.sampleRate;
  };
  const anyLaneTransposed = () => {
    for (const t of tracks.values()) {
      if (t.visualOnly) continue;
      if (effectivePitch(t.name, t.pitch, t.pitchable) !== 0) return true;
    }
    return false;
  };
  const pipelineLatencySeconds = () => {
    if (!stNode) return 0;
    // The pitch buses only buffer once something is actually transposed. Until
    // then the worklet hands its input straight back, with no delay to correct.
    const pitchLatency = anyLaneTransposed() ? _workletLatencyFrames / ctx.sampleRate : 0;
    const tempoStages = Math.abs(_playbackRate - 1) >= 1e-3 ? 1 : 0;
    return pitchLatency + tempoStages * wsolaLatencySeconds();
  };
  const now = () => playing
    ? Math.max(
        startOffset,
        Math.max(0, ctx.currentTime - startCtxTime - pipelineLatencySeconds())
          * _playbackRate + startOffset,
      )
    : startOffset;

  // Extra headroom folded into a count-in's lead so every count click lands
  // safely in the future even after the small gap between scheduling the
  // sources and handing the clicks to the audio clock.
  const COUNT_IN_MARGIN = 0.06;

  function stopSources() {
    for (const t of tracks.values()) {
      if (t.source) {
        try { t.source.stop(); } catch { /* already stopped */ }
        try { t.source.disconnect(); } catch { /* noop */ }
        t.source = null;
      }
    }
  }

  function resetProcessor() {
    try { stNode?.port?.postMessage({ type: "reset" }); } catch { /* processor is gone */ }
  }

  function startSources(offset, when = ctx.currentTime) {
    resetProcessor();
    for (const t of tracks.values()) {
      if (t.visualOnly) continue;
      const src = ctx.createBufferSource();
      src.buffer = t.buffer;
      // SoundTouch handles time-stretch; playbackRate stays 1.0.
      // Falls back to tape-effect only when the worklet is unavailable.
      if (!stNode) src.playbackRate.value = _playbackRate;
      src.connect(t.gain);
      src.start(when, Math.max(0, Math.min(offset, t.buffer.duration)));
      t.source = src;
    }
    startCtxTime = when;
    startOffset = offset;
    _epoch++;
  }

  // Rate at which the source nodes consume their buffers. With SoundTouch
  // mounted they always run at 1.0 and the worklet does the stretching; the
  // tape-effect fallback resamples the sources themselves.
  const srcRate = () => (stNode ? 1 : _playbackRate);

  // Inverse of the scheduling in startSources: the AudioContext time at which
  // media time `t` is fed into the graph. Scheduling a click here puts it in
  // the same sample frame as the stems. The click uses the unpitched input and
  // shares the worklet's tempo stage with the combined audio.
  const sourceTimeToCtxTime = (t) => startCtxTime + (t - startOffset) / srcRate();

  // True inverse of the above. The metronome re-anchors with this rather than
  // getCurrentTime(): that reports the *output* playhead for the UI, which with
  // SoundTouch mounted is a different quantity from where the sources have
  // actually been read to. Anchoring the click cursor in the source domain
  // keeps it consistent with the times it schedules against.
  const ctxTimeToSourceTime = (c) => startOffset + (c - startCtxTime) * srcRate();

  function tick() {
    if (!playing) return;
    let t = now();
    if (loop.enabled && loop.end > loop.start && t >= loop.end) {
      seek(loop.start);
      t = loop.start;
    } else if (t >= duration) {
      pause();
      startOffset = duration;
      onTime?.(duration);
      onEnded?.();
      return;
    }
    onTime?.(t);
    rafId = requestAnimationFrame(tick);
  }

  // `leadIn` (source seconds, default 0) delays the moment the stems begin so a
  // count-in can sound in the gap first. The sources are scheduled at a future
  // ctx time; the count-in clicks (negative source time) map into `[now, when]`
  // through the same sourceTimeToCtxTime the metronome uses, so they stay locked
  // to the audio. See transport.togglePlayPause + metronome.playCountIn.
  function play(leadIn = 0) {
    if (playing || destroyed || !tracks.size) return;
    // Safari: resume the context fire-and-forget within the user-gesture tick.
    if (ctx.state === "suspended") ctx.resume().catch(() => {});
    let off = startOffset;
    if (off >= duration) off = 0;
    const lead = Math.max(0, leadIn);
    const when = ctx.currentTime + (lead > 0 ? (lead + COUNT_IN_MARGIN) / srcRate() : 0);
    startSources(off, when);
    playing = true;
    rafId = requestAnimationFrame(tick);
  }

  function pause() {
    if (!playing) return;
    const t = now();
    stopSources();
    resetProcessor();
    playing = false;
    startOffset = Math.max(0, Math.min(t, duration));
    _epoch++;
    if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
  }

  function seek(t) {
    const clamped = Math.max(0, Math.min(t, duration || 0));
    if (playing) {
      stopSources();
      startSources(clamped); // bumps _epoch
    } else {
      resetProcessor();
      startOffset = clamped;
      _epoch++;
    }
    onTime?.(clamped);
  }

  function setGain(name, v) {
    for (const t of tracks.values()) {
      if (!t.visualOnly && t.controlName === name) {
        t.level = Math.max(0, v);
        t.gain.gain.setTargetAtTime(t.level, ctx.currentTime, 0.01);
      }
    }
  }

  // Ducking either side of a bus change. Both buses are delayed by the same
  // amount, so a dip scheduled at the source lands at the output as one short
  // dip rather than as a click from splicing two different keys together.
  const ROUTE_FADE = 0.006;
  const ROUTE_HOLD = 0.02;

  // A track arriving on, or leaving, a bus. Mirrors chunkedAudioEngine.js:
  // only the first arrival and the last departure touch the worklet, the
  // unpitched bus stays wired whatever its count because the click is
  // scheduled onto it directly, and arrival is announced before the track
  // connects so the bus is live by the time anything reaches it. No worklet
  // means the tape-effect fallback, where every bus already feeds master.
  function _laneArriving(bus) {
    const k = buses.indexOf(bus);
    if (busLanes[k]++ === 0 && k !== ZERO_INPUT && stNode) buses[k].connect(stNode, 0, k);
  }
  function _laneLeft(bus) {
    if (!bus) return;
    const k = buses.indexOf(bus);
    if (--busLanes[k] === 0 && k !== ZERO_INPUT && stNode) {
      try { buses[k].disconnect(stNode, 0, k); } catch { /* worklet already gone */ }
    }
  }

  /** Connect a track to the bus for its current transpose. */
  function routeTrack(t, immediate = false) {
    if (t.visualOnly) return;
    const target = buses[inputForPitch(effectivePitch(t.name, t.pitch, t.pitchable))];
    if (t.bus === target) return;
    const swap = () => {
      if (destroyed) return;
      const previous = t.bus;
      _laneArriving(target);
      try { t.analyser.disconnect(); } catch { /* was not connected yet */ }
      t.analyser.connect(target);
      t.bus = target;
      _laneLeft(previous);
    };
    if (immediate || !playing) { swap(); return; }
    const g = t.gain.gain;
    const at = ctx.currentTime;
    g.cancelScheduledValues(at);
    g.setValueAtTime(g.value, at);
    g.linearRampToValueAtTime(0, at + ROUTE_FADE);
    g.setValueAtTime(0, at + ROUTE_FADE + ROUTE_HOLD);
    g.linearRampToValueAtTime(t.level, at + ROUTE_FADE + ROUTE_HOLD + ROUTE_FADE);
    setTimeout(swap, (ROUTE_FADE + ROUTE_HOLD / 2) * 1000);
  }

  function setMasterGain(v) {
    master.gain.setTargetAtTime(Math.max(0, v), ctx.currentTime, 0.01);
  }

  function destroy() {
    destroyed = true;
    stopSources();
    resetProcessor();
    if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
    tracks.clear();
    if (stNode) { try { stNode.disconnect(); } catch { /* noop */ } }
    if (ownsCtx) ctx.close().catch(() => {});
  }

  return {
    ready,
    getLoadError: () => _loadError,
    play,
    pause,
    seek,
    setTime: seek, // alias to match the multitrack interface used by transport.js
    isPlaying: () => playing,
    // This engine honours play(leadIn) for a count-in; the streaming/chunked
    // paths do not, so the transport checks this before scheduling one.
    supportsCountIn: true,
    getCurrentTime: now,
    getDuration: () => duration,
    setLoop: (enabled, start, end) => { loop = { enabled, start, end }; },
    // Metronome support. sourceTimeToCtxTime is the contract that keeps the
    // click locked to the stems; the epoch tells the scheduler when to discard
    // clicks it already queued, and isClockReady guards the window where
    // `playing` is set but the mapping is not yet valid.
    sourceTimeToCtxTime,
    ctxTimeToSourceTime,
    getScheduleEpoch: () => _epoch,
    isClockReady: () => playing,
    // The click shares the unpitched input with drums. It still passes through
    // the common tempo stage and master gain.
    getMasterNode: () => buses[ZERO_INPUT],
    /**
     * Put one mixer lane in a given key.
     *
     * Nothing restarts and nothing is flushed. Every bus is delayed by the same
     * amount, so the lane's old bus plays out the couple of hundred
     * milliseconds it has already buffered while its new bus, primed with
     * exactly that much silence, takes over at the instant the old one runs
     * dry. The handover is a duck, not a gap.
     */
    setStemPitch(name, semitones) {
      if (!stNode) return false;
      const next = clampPitch(semitones);
      let found = false;
      for (const t of tracks.values()) {
        if (t.visualOnly || t.controlName !== name) continue;
        found = true;
        if (t.pitch === next) continue;
        t.pitch = next;
        routeTrack(t);
      }
      return found;
    },
    getStemPitch(name) {
      for (const t of tracks.values()) {
        if (!t.visualOnly && t.controlName === name) return t.pitch;
      }
      return 0;
    },
    /** False for lanes that must never be resampled, so the UI can say so. */
    isStemPitchable(name) {
      for (const t of tracks.values()) {
        if (!t.visualOnly && t.controlName === name) return t.pitchable;
      }
      return false;
    },
    supportsPitchShift: () => !!stNode,
    setPlaybackRate(rate) {
      if (stNode) {
        const t = now();
        _playbackRate = rate;
        stNode.parameters.get('tempo').value = rate;
        if (playing) {
          stopSources();
          startSources(Math.max(0, Math.min(t, duration)));
        } else {
          resetProcessor();
          _epoch++;
        }
        return;
      }
      // Tape-effect fallback: the sources themselves resample, so the new rate
      // only applies from this instant -- but startCtxTime/startOffset still
      // describe the old one. Re-anchoring at the current position keeps
      // sourceTimeToCtxTime a true inverse of the running sources; without it
      // every click scheduled after a speed change drifts. chunkedAudioEngine
      // re-seeks here for exactly the same reason.
      const t = now();
      _playbackRate = rate;
      if (playing) {
        stopSources();
        startSources(Math.max(0, Math.min(t, duration))); // bumps _epoch
      } else {
        startOffset = t;
        _epoch++;
      }
    },
    setGain,
    setMasterGain,
    getAnalysers: (name) => [...tracks.values()]
      .filter((track) => !track.visualOnly && track.controlName === name)
      .map((track) => track.analyser),
    getAnalyser: (name) => [...tracks.values()]
      .find((track) => !track.visualOnly && track.controlName === name)?.analyser ?? null,
    // Decoded AudioBuffers keyed by stem name — reused by the visuals (overview
    // waveforms, mini-waves, VU envelopes, energy bars) so they don't need the
    // multitrack to also decode the audio. Map<name, AudioBuffer>.
    getBuffers: () => {
      const m = new Map();
      for (const [name, t] of tracks) m.set(name, t.buffer);
      return m;
    },
    destroy,
    audioContext: ctx,
  };
}

// Rough decoded-PCM memory estimate (Float32 = 4 bytes/sample/channel) used by the
// caller's guard to fall back to streaming for very long / many-stem tracks.
export function estimateDecodedBytes(durationSec, stemCount, channels = 2, sampleRate = 44100) {
  return Math.round(durationSec * stemCount * channels * sampleRate * 4);
}
