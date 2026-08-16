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
 * @param {{name:string,url:string}[]} stems  Active stems only (caller filters).
 * @param {{onTime?:(t:number)=>void, onEnded?:()=>void}} cbs
 */
export function createAudioEngine(stems, { onTime, onEnded, context } = {}) {
  // Mobile/iOS only starts audio from a context resumed inside a user gesture.
  // Callers can pass a shared, gesture-unlocked `context` (the mobile UI does);
  // desktop passes none and we own a fresh one. We only close contexts we own.
  const ctx = context || new AudioCtx();
  const ownsCtx = !context;
  const master = ctx.createGain();

  // SoundTouch pitch-preserving time-stretch on the master bus.
  // Falls back to tape-effect (playbackRate) if AudioWorklet is unavailable.
  let stNode = null;
  const _workletReady = (ctx.audioWorklet
    ? ctx.audioWorklet.addModule('/vendor/soundtouch-processor.js').then(() => {
        stNode = new AudioWorkletNode(ctx, 'soundtouch-processor');
        master.connect(stNode);
        stNode.connect(ctx.destination);
      }).catch((err) => {
        console.warn('[audioEngine] SoundTouch worklet load failed, using tape-effect fallback:', err);
        master.connect(ctx.destination);
      })
    : Promise.resolve().then(() => { master.connect(ctx.destination); }));

  /** @type {Map<string,{buffer:AudioBuffer,gain:GainNode,analyser:AnalyserNode,source:AudioBufferSourceNode|null}>} */
  const tracks = new Map();
  let duration = 0;
  let playing = false;
  let startCtxTime = 0; // ctx.currentTime at playback start
  let startOffset = 0; // media offset at that moment
  let rafId = null;
  let destroyed = false;
  let loop = { enabled: false, start: 0, end: 0 };
  let _playbackRate = 1.0;
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
          const gain = ctx.createGain();
          const analyser = ctx.createAnalyser();
          analyser.fftSize = 1024;
          gain.connect(analyser);
          analyser.connect(master);
          tracks.set(s.name, { buffer, gain, analyser, source: null });
          duration = Math.max(duration, buffer.duration);
        } catch (e) {
          undecodable++;
          console.warn(`[audioEngine] decode failed for ${s.name}:`, e);
        }
      }),
    ]);

    if (tracks.size === 0) {
      _loadError = undecodable
        ? "This track's audio files are in a format StemDeck could not read."
        : unreachable
          ? "Could not load this track's audio files."
          : "This track has no stem files to play.";
    }
    return tracks.size > 0;
  })();

  // Clamped so the reported playhead never reads before the start position.
  // During a count-in the sources are scheduled to begin in the future
  // (startCtxTime > ctx.currentTime), which would otherwise make this go
  // negative -- the playhead must sit still at the start until the audio enters.
  // A no-op for a normal start, where startCtxTime == the moment play() ran.
  const now = () =>
    playing ? Math.max(startOffset, (ctx.currentTime - startCtxTime) * _playbackRate + startOffset) : startOffset;

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

  function startSources(offset, when = ctx.currentTime) {
    for (const t of tracks.values()) {
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
  // the same sample frame as the stems, which is what keeps the two locked
  // together -- and because it describes the *input* to SoundTouch, it holds
  // whatever the worklet does downstream, since the click goes through it too.
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
      startOffset = clamped;
      _epoch++;
    }
    onTime?.(clamped);
  }

  function setGain(name, v) {
    const t = tracks.get(name);
    if (t) t.gain.gain.setTargetAtTime(Math.max(0, v), ctx.currentTime, 0.01);
  }

  function setMasterGain(v) {
    master.gain.setTargetAtTime(Math.max(0, v), ctx.currentTime, 0.01);
  }

  function destroy() {
    destroyed = true;
    stopSources();
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
    // The click connects here, not to ctx.destination: same bus as the stems,
    // so it inherits master gain and the identical SoundTouch path.
    getMasterNode: () => master,
    setPlaybackRate(rate) {
      if (stNode) {
        // Pitch-preserving: update SoundTouch tempo parameter. The sources keep
        // running at 1.0x, so the source-domain clock anchor is untouched.
        _playbackRate = rate;
        _epoch++;
        stNode.parameters.get('tempo').value = rate;
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
    getAnalyser: (name) => tracks.get(name)?.analyser ?? null,
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
