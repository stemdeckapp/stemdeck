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
//                                                                    -> masterGain -> destination
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
  master.connect(ctx.destination);

  /** @type {Map<string,{buffer:AudioBuffer,gain:GainNode,analyser:AnalyserNode,source:AudioBufferSourceNode|null}>} */
  const tracks = new Map();
  let duration = 0;
  let playing = false;
  let startCtxTime = 0; // ctx.currentTime at playback start
  let startOffset = 0; // media offset at that moment
  let rafId = null;
  let destroyed = false;
  let loop = { enabled: false, start: 0, end: 0 };

  // Decode all stems up front. Resolves true once at least one stem is ready.
  const ready = (async () => {
    await Promise.all(
      stems.map(async (s) => {
        if (!s?.url) return;
        try {
          const res = await fetch(s.url);
          if (!res.ok) throw new Error(`fetch ${res.status}`);
          const buffer = await ctx.decodeAudioData(await res.arrayBuffer());
          if (destroyed) return;
          const gain = ctx.createGain();
          const analyser = ctx.createAnalyser();
          analyser.fftSize = 1024;
          gain.connect(analyser);
          analyser.connect(master);
          tracks.set(s.name, { buffer, gain, analyser, source: null });
          duration = Math.max(duration, buffer.duration);
        } catch (e) {
          console.warn(`[audioEngine] decode failed for ${s.name}:`, e);
        }
      }),
    );
    return tracks.size > 0;
  })();

  const now = () => (playing ? ctx.currentTime - startCtxTime + startOffset : startOffset);

  function stopSources() {
    for (const t of tracks.values()) {
      if (t.source) {
        try { t.source.stop(); } catch { /* already stopped */ }
        try { t.source.disconnect(); } catch { /* noop */ }
        t.source = null;
      }
    }
  }

  function startSources(offset) {
    const when = ctx.currentTime;
    for (const t of tracks.values()) {
      const src = ctx.createBufferSource();
      src.buffer = t.buffer;
      src.connect(t.gain);
      src.start(when, Math.max(0, Math.min(offset, t.buffer.duration)));
      t.source = src;
    }
    startCtxTime = when;
    startOffset = offset;
  }

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

  function play() {
    if (playing || destroyed || !tracks.size) return;
    // Safari: resume the context fire-and-forget within the user-gesture tick.
    if (ctx.state === "suspended") ctx.resume().catch(() => {});
    let off = startOffset;
    if (off >= duration) off = 0;
    startSources(off);
    playing = true;
    rafId = requestAnimationFrame(tick);
  }

  function pause() {
    if (!playing) return;
    const t = now();
    stopSources();
    playing = false;
    startOffset = Math.max(0, Math.min(t, duration));
    if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
  }

  function seek(t) {
    const clamped = Math.max(0, Math.min(t, duration || 0));
    if (playing) {
      stopSources();
      startSources(clamped);
    } else {
      startOffset = clamped;
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
    if (ownsCtx) ctx.close().catch(() => {});
  }

  return {
    ready,
    play,
    pause,
    seek,
    setTime: seek, // alias to match the multitrack interface used by transport.js
    isPlaying: () => playing,
    getCurrentTime: now,
    getDuration: () => duration,
    setLoop: (enabled, start, end) => { loop = { enabled, start, end }; },
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
