// Chunked audio engine for the mobile player.
//
// Fetches WAV stems in fixed-size windows via HTTP Range requests and chains
// AudioBufferSourceNodes back-to-back for gapless playback. Compared to the
// full-decode engine (audioEngine.js):
//   - First audio after ~7 MB download (one 10-second chunk per 4 stems on WiFi)
//     instead of waiting for the complete file
//   - Peak RAM ~28 MB vs ~420 MB for a 5-minute 4-stem track
//   - No track-length cap
//   - Same glitch-free behavior on Safari/WKWebView: AudioBufferSourceNode,
//     no streaming elements, no HTTP/1.1 connection-cap underruns
//
// The backend's FileResponse already handles Range requests natively (Starlette
// 1.3.x), so no server-side changes are needed.
//
// Graph: per-stem AudioBufferSourceNode -> GainNode -> masterGain -> SoundTouchNode -> destination

const CHUNK_SEC = 5;      // seconds of audio per chunk
const LOOKAHEAD_SEC = 12; // schedule next chunk this far ahead of playhead
// Extra headroom folded into a count-in's lead so every count click lands
// safely in the future even after the small gap between scheduling the first
// chunk and handing the clicks to the audio clock. Mirrors audioEngine.js.
const COUNT_IN_MARGIN = 0.06;

// First probe covers the common case: a 44-byte canonical header, or one with a
// modest LIST/INFO block. Anything larger costs a second round trip rather than
// silently failing.
const HEADER_PROBE_BYTES = 1024;
// Chase the chunk table this far before declaring the file unreadable. Writers
// pad with JUNK for sector alignment (commonly 4 KB) or embed cover art, but a
// file that has not declared `data` within 1 MB is not one we can stream.
const HEADER_MAX_BYTES = 1 << 20;
const HEADER_MAX_ATTEMPTS = 5;

// ---------------------------------------------------------------------------
// WAV parsing
// ---------------------------------------------------------------------------

/**
 * Walk the RIFF chunk table looking for `fmt ` and `data`.
 *
 * The table is a linked list, so `data` can sit behind any amount of metadata:
 * a LIST/INFO block, or a JUNK chunk written for sector alignment. Parsing a
 * fixed prefix and giving up is what disabled playback outright on files whose
 * writer emitted more than the usual 44 bytes (#358), so running off the end of
 * the buffer is reported as "need more bytes" and not as a parse failure. Only
 * the caller knows whether more bytes can be had.
 *
 * @param {ArrayBuffer} buf   A prefix of the file, starting at byte 0.
 * @param {number} fileSize   Total file length if known, else 0.
 * @returns {{header:object}|{needBytes:number}|{invalid:true}}
 */
function _parseWavHeader(buf, fileSize = 0) {
  const view = new DataView(buf);
  const tag = (off) => String.fromCharCode(...new Uint8Array(buf, off, 4));

  if (buf.byteLength < 12) return { needBytes: 12 };
  if (tag(0) !== "RIFF" || tag(8) !== "WAVE") return { invalid: true };

  let audioFormat = 1, channels = 2, sampleRate = 44100, bitsPerSample = 16;
  let dataOffset = -1, dataSize = 0;
  let sawFmt = false;

  let off = 12;
  for (;;) {
    if (off + 8 > buf.byteLength) return { needBytes: off + 8 };
    const id = tag(off);
    const size = view.getUint32(off + 4, true);

    if (id === "data") {
      dataOffset = off + 8;
      dataSize = size;
      break;
    }

    if (id === "fmt ") {
      if (off + 24 > buf.byteLength) return { needBytes: off + 24 };
      audioFormat   = view.getUint16(off + 8,  true);
      channels      = view.getUint16(off + 10, true);
      sampleRate    = view.getUint32(off + 12, true);
      bitsPerSample = view.getUint16(off + 22, true);
      // WAVE_FORMAT_EXTENSIBLE keeps the real format code in the first field of
      // the SubFormat GUID. Without reading it, a float32 extensible file parses
      // cleanly and then decodes to silence, because _pcmToAudioBuffer only
      // recognises 1 (PCM) and 3 (float).
      if (audioFormat === 0xfffe && size >= 40) {
        if (off + 34 > buf.byteLength) return { needBytes: off + 34 };
        audioFormat = view.getUint16(off + 32, true);
      }
      sawFmt = true;
    }

    const next = off + 8 + size + (size & 1); // chunks are word-aligned
    // A chunk that fails to advance, or that claims to run past the end of the
    // file, means the table is corrupt. Without this the caller's widening loop
    // would keep asking for bytes that will never resolve anything.
    if (next <= off) return { invalid: true };
    if (fileSize && next > fileSize) return { invalid: true };
    off = next;
  }

  if (!sawFmt || !channels || !sampleRate || !bitsPerSample) return { invalid: true };
  const bytesPerFrame = channels * (bitsPerSample >> 3);
  if (!bytesPerFrame) return { invalid: true };

  // Reject sample formats _pcmToAudioBuffer cannot turn into samples, rather
  // than accepting the file on the strength of a readable header. Measuring a
  // file we cannot decode is worse than rejecting it: every chunk comes back
  // empty, _scheduledTo never advances, and because an empty result is treated
  // as a transient failure and evicted from the cache, the scheduler re-fetches
  // the same range on every animation frame. Rejecting hands the file to the
  // full-decode fallback, whose decoder handles 24-bit and integer formats.
  if (!(bitsPerSample === 16 || (audioFormat === 3 && bitsPerSample === 32))) {
    return { invalid: true };
  }

  // `data` may declare a size the file does not actually have: 0 and 0xffffffff
  // are both used by writers that stream to a non-seekable target and never go
  // back to patch the length. Either would yield a nonsense duration, and a
  // duration of 0 reads downstream as "no usable audio". Trust the file length.
  const available = fileSize ? Math.max(0, fileSize - dataOffset) : 0;
  if (available && (dataSize === 0 || dataSize > available)) dataSize = available;

  return {
    header: {
      audioFormat, channels, sampleRate, bitsPerSample,
      dataOffset, dataSize, bytesPerFrame,
      duration: dataSize / (bytesPerFrame * sampleRate),
    },
  };
}

// Convert raw interleaved PCM bytes to an AudioBuffer.
// Fast paths for the common cases (stereo 16-bit, stereo float32).
function _pcmToAudioBuffer(ctx, pcmData, header) {
  const { channels, sampleRate, bitsPerSample, audioFormat } = header;
  const totalSamples = Math.floor(pcmData.byteLength / (channels * (bitsPerSample >> 3)));
  if (totalSamples === 0) return null;

  const ab = ctx.createBuffer(channels, totalSamples, sampleRate);

  if (bitsPerSample === 16) {
    const src = new Int16Array(pcmData);
    const scale = 1 / 32768;
    if (channels === 2) {
      const ch0 = ab.getChannelData(0);
      const ch1 = ab.getChannelData(1);
      for (let i = 0, j = 0; i < totalSamples; i++, j += 2) {
        ch0[i] = src[j] * scale;
        ch1[i] = src[j + 1] * scale;
      }
    } else {
      for (let ch = 0; ch < channels; ch++) {
        const out = ab.getChannelData(ch);
        for (let i = 0; i < totalSamples; i++) out[i] = src[i * channels + ch] * scale;
      }
    }
  } else if (audioFormat === 3 && bitsPerSample === 32) {
    const src = new Float32Array(pcmData);
    if (channels === 2) {
      const ch0 = ab.getChannelData(0);
      const ch1 = ab.getChannelData(1);
      for (let i = 0, j = 0; i < totalSamples; i++, j += 2) {
        ch0[i] = src[j];
        ch1[i] = src[j + 1];
      }
    } else {
      for (let ch = 0; ch < channels; ch++) {
        const out = ab.getChannelData(ch);
        for (let i = 0; i < totalSamples; i++) out[i] = src[i * channels + ch];
      }
    }
  } else {
    return null; // unsupported format
  }

  return ab;
}

// ---------------------------------------------------------------------------
// Engine factory
// ---------------------------------------------------------------------------

/**
 * @param {{name:string,url:string,controlName?:string,pitched?:boolean}[]} stems
 * @param {{onTime?:(t:number)=>void, onEnded?:()=>void, context?:AudioContext}} opts
 */
import {
  INPUT_COUNT, ZERO_INPUT, clampPitch, effectivePitch, inputForPitch,
} from "./pitchBus.js";

export function createChunkedAudioEngine(stems, { onTime, onEnded, context } = {}) {
  const AC = window.AudioContext || window.webkitAudioContext;
  const ctx = context || new AC();
  const ownsCtx = !context;
  const master = ctx.createGain();
  // One bus per semitone the worklet offers. A lane's transpose is expressed
  // as which bus it is connected to, so several lanes can sit in different
  // keys at once while still sharing the worklet's single tempo stage.
  const buses = Array.from({ length: INPUT_COUNT }, () => ctx.createGain());
  master.connect(ctx.destination);

  let stNode = null;
  let _playbackRate = 1.0;
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
        for (let k = 0; k < INPUT_COUNT; k++) buses[k].connect(stNode, 0, k);
        stNode.connect(master);
      }).catch((err) => {
        console.warn('[chunkedEngine] SoundTouch worklet failed, tape-effect fallback:', err);
        for (const bus of buses) bus.connect(master);
      })
    : Promise.resolve().then(() => {
        for (const bus of buses) bus.connect(master);
      }));

  // Declared ahead of the stem loop below, which routes each stem to its bus
  // as it is built and would otherwise read these before initialisation.
  let playing = false;
  let destroyed = false;

  // Per-stem state: url, parsed WAV header, gain node, analyser (VU tap), and
  // currently playing nodes. Graph per stem: sources -> gain -> analyser -> master.
  // The analyser sits post-gain so VU meters reflect volume/mute/solo.
  const stemMap = new Map();
  for (const s of stems) {
    if (!s?.url || s.visualOnly) continue;
    const pitchable = s.name !== "drums" && s.pitched !== false;
    const gain = ctx.createGain();
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 1024;
    gain.connect(analyser);
    const stem = {
      url: s.url,
      header: null,
      gain,
      analyser,
      activeNodes: [],
      name: s.name,
      controlName: s.controlName || s.name,
      pitchable,
      pitched: pitchable,
      pitch: 0,
      bus: null,
      level: 1,
    };
    stemMap.set(s.name, stem);
    routeStem(stem, true);
  }

  let _duration = 0;
  let rafId = null;
  // Why ready() resolved false, in words fit to show a user. Read via
  // getLoadError() by the caller that decides what to put on screen.
  let _loadError = null;

  // Playback clock: getCurrentTime = ctx.currentTime - _startCtxTime + _startOffset
  let _startCtxTime = 0;
  let _startOffset = 0;
  // _scheduledTo: track position (seconds) up to which AudioBufferSourceNodes
  // have already been scheduled. Always sits at a chunk boundary after play().
  let _scheduledTo = 0;
  // True once the first AudioBufferSourceNode is actually queued; guards
  // getCurrentTime() from advancing during an async chunk fetch.
  let _audioStarted = false;
  let _filling = false; // prevents concurrent _scheduleNext() calls
  // Bumped whenever the media-time -> ctx-time mapping changes (start, seek,
  // loop jump, rate change, pause), so the metronome knows its queued clicks
  // are stale. See sourceTimeToCtxTime below.
  let _epoch = 0;
  let loop = { enabled: false, start: 0, end: 0 };
  // Chunk index that must survive cache eviction while looping (the loop-start
  // chunk), so every pass around the loop replays from cache with no refetch.
  const _loopPinChunk = () =>
    (loop.enabled && loop.end > loop.start) ? Math.floor(loop.start / CHUNK_SEC) : -1;

  // Chunk cache: chunkIdx -> { promise: Promise<Map>, result: Map|null }
  // result is set synchronously once the promise resolves so play() can
  // schedule chunk 0 without an async await after ready() completes.
  const _cache = new Map();

  const _wsolaLatencySeconds = () => {
    const needed = Math.round(0.012 * ctx.sampleRate)
      + Math.round(0.028 * ctx.sampleRate)
      + Math.round(0.082 * ctx.sampleRate);
    return Math.floor(needed / 128) * 128 / ctx.sampleRate;
  };
  const _anyLaneTransposed = () => {
    for (const stem of stemMap.values()) {
      if (effectivePitch(stem.name, stem.pitch, stem.pitchable) !== 0) return true;
    }
    return false;
  };
  const _pipelineLatencySeconds = () => {
    if (!stNode) return 0;
    // The pitch buses only buffer once something is actually transposed. Until
    // then the worklet hands its input straight back, with no delay to correct.
    const pitchLatency = _anyLaneTransposed() ? _workletLatencyFrames / ctx.sampleRate : 0;
    const tempoStages = Math.abs(_playbackRate - 1) >= 1e-3 ? 1 : 0;
    return pitchLatency + tempoStages * _wsolaLatencySeconds();
  };

  // Ducking either side of a bus change. Both buses are delayed by the same
  // amount, so a dip scheduled at the source lands at the output as one short
  // dip rather than as a click from splicing two different keys together.
  const ROUTE_FADE = 0.006;
  const ROUTE_HOLD = 0.02;

  /** Connect a stem to the bus for its current transpose. */
  function routeStem(stem, immediate = false) {
    const target = buses[inputForPitch(effectivePitch(stem.name, stem.pitch, stem.pitchable))];
    if (stem.bus === target) return;
    const swap = () => {
      if (destroyed) return;
      try { stem.analyser.disconnect(); } catch { /* was not connected yet */ }
      stem.analyser.connect(target);
      stem.bus = target;
    };
    if (immediate || !playing) { swap(); return; }
    const g = stem.gain.gain;
    const at = ctx.currentTime;
    g.cancelScheduledValues(at);
    g.setValueAtTime(g.value, at);
    g.linearRampToValueAtTime(0, at + ROUTE_FADE);
    g.setValueAtTime(0, at + ROUTE_FADE + ROUTE_HOLD);
    g.linearRampToValueAtTime(stem.level, at + ROUTE_FADE + ROUTE_HOLD + ROUTE_FADE);
    setTimeout(swap, (ROUTE_FADE + ROUTE_HOLD / 2) * 1000);
  }

  function _getCurrentTime() {
    if (!playing || !_audioStarted) return _startOffset;
    // Clamped to >= _startOffset: during a count-in the first chunk is
    // scheduled at a future ctx time (_startCtxTime > ctx.currentTime), which
    // would otherwise read as negative before the audio actually starts.
    return Math.max(
      _startOffset,
      Math.min(
        Math.max(0, ctx.currentTime - _startCtxTime - _pipelineLatencySeconds())
          * _playbackRate + _startOffset,
        _duration,
      ),
    );
  }

  // Rate at which source nodes consume their buffers -- 1.0 whenever SoundTouch
  // is doing the stretching, _playbackRate only in the tape-effect fallback.
  // Mirrors `playFactor` in _scheduleNext, which must stay in step with this.
  const _srcRate = () => (stNode ? 1 : _playbackRate);

  // Inverse of the chunk scheduling: the AudioContext time at which media time
  // `t` enters the graph. A click scheduled here shares a sample frame with the
  // stems. The click uses the unpitched input and shares the worklet's tempo
  // stage with the combined audio.
  const sourceTimeToCtxTime = (t) => _startCtxTime + (t - _startOffset) / _srcRate();

  // True inverse of the above -- see the matching note in audioEngine.js. The
  // metronome anchors its cursor here rather than on _getCurrentTime, which
  // reports the output playhead for the UI.
  const ctxTimeToSourceTime = (c) => _startOffset + (c - _startCtxTime) * _srcRate();

  // --- fetch helpers ---

  // Read enough of the file to locate the `data` chunk, widening the request
  // when the chunk table runs past what we asked for. Returns null when the file
  // is not readable as a WAV, which the caller reports rather than swallows.
  async function _fetchHeader(url) {
    let want = HEADER_PROBE_BYTES;
    let fileSize = 0;

    for (let attempt = 0; attempt < HEADER_MAX_ATTEMPTS; attempt++) {
      const res = await fetch(url, { headers: { Range: `bytes=0-${want - 1}` } });
      if (!res.ok && res.status !== 206) throw new Error(`header fetch ${res.status}`);

      // "bytes 0-1023/5242880" gives us the real length without a second request.
      const total = Number(/\/(\d+)\s*$/.exec(res.headers.get("Content-Range") || "")?.[1]);
      if (Number.isFinite(total) && total > 0) fileSize = total;

      const buf = await res.arrayBuffer();
      const out = _parseWavHeader(buf, fileSize);
      if (out.header) return out.header;
      if (out.invalid) return null;

      // A 200 means the server ignored Range and already sent the whole file, so
      // asking for a wider window cannot produce anything new.
      if (res.status === 200 || (fileSize && buf.byteLength >= fileSize)) return null;

      // Grow past what the table says it needs, geometrically, so a file with
      // several metadata chunks converges in a couple of round trips instead of
      // one per chunk.
      const next = Math.min(
        Math.max(out.needBytes, buf.byteLength * 4),
        HEADER_MAX_BYTES,
        fileSize || HEADER_MAX_BYTES,
      );
      if (next <= buf.byteLength) return null; // cannot grow; give up
      want = next;
    }
    return null;
  }

  async function _fetchPcm(stem, chunkIdx) {
    const { url, header } = stem;
    const { dataOffset, dataSize, bytesPerFrame, sampleRate } = header;
    const chunkBytes = Math.floor(CHUNK_SEC * sampleRate) * bytesPerFrame;
    const byteStart = dataOffset + chunkIdx * chunkBytes;
    if (byteStart >= dataOffset + dataSize) return null; // past end of file
    const byteEnd = Math.min(byteStart + chunkBytes, dataOffset + dataSize) - 1;
    const res = await fetch(url, { headers: { Range: `bytes=${byteStart}-${byteEnd}` } });
    if (!res.ok && res.status !== 206) throw new Error(`Range fetch ${res.status}`);
    return res.arrayBuffer();
  }

  // Returns a Promise<Map<name, AudioBuffer>>. Deduplicates: if a fetch for
  // chunkIdx is already in flight, returns the same promise.
  function _fetchChunk(chunkIdx) {
    const hit = _cache.get(chunkIdx);
    if (hit) return hit.promise;

    const entry = { promise: null, result: null };
    entry.promise = (async () => {
      const pairs = await Promise.all(
        [...stemMap.entries()]
          .filter(([, s]) => s.header)
          .map(async ([name, stem]) => {
            try {
              const pcm = await _fetchPcm(stem, chunkIdx);
              if (!pcm) return [name, null];
              return [name, _pcmToAudioBuffer(ctx, pcm, stem.header)];
            } catch (e) {
              console.warn(`[chunked] chunk ${chunkIdx} stem ${name}:`, e);
              return [name, null];
            }
          })
      );
      const map = new Map(pairs.filter(([, b]) => b));
      entry.result = map;
      // Keep at most the previous chunk + current in cache to bound memory.
      // The loop-start chunk is pinned so loop passes replay from cache.
      const pin = _loopPinChunk();
      for (const k of _cache.keys()) {
        if (k < chunkIdx - 1 && k !== pin) _cache.delete(k);
      }
      // An all-stems-empty result means every fetch failed (e.g. a transient
      // network drop) — drop the entry so a later scheduler pass retries
      // instead of caching permanent silence. Past-EOF chunks are never
      // re-requested: _maybeSchedule stops at duration.
      if (map.size === 0) _cache.delete(chunkIdx);
      return map;
    })();

    _cache.set(chunkIdx, entry);
    return entry.promise;
  }

  // --- node lifecycle ---

  function _stopNodes() {
    for (const stem of stemMap.values()) {
      for (const node of stem.activeNodes) {
        try { node.stop(); } catch { /* already stopped */ }
        try { node.disconnect(); } catch { /* noop */ }
      }
      stem.activeNodes = [];
    }
  }

  function _resetProcessor() {
    try { stNode?.port?.postMessage({ type: "reset" }); } catch { /* processor is gone */ }
  }

  // Schedule all stems' AudioBufferSourceNodes to start at `when` (AudioContext
  // time), beginning `startSecs` into each buffer. Returns the duration of audio
  // that will play (max over stems of buffer.duration - startSecs).
  function _scheduleChunk(buffers, when, startSecs) {
    let playDur = 0;
    for (const [name, stem] of stemMap) {
      const buf = buffers.get(name);
      if (!buf) continue;
      const node = ctx.createBufferSource();
      node.buffer = buf;
      if (!stNode) node.playbackRate.value = _playbackRate; // tape-effect fallback
      node.connect(stem.gain);
      const offset = Math.max(0, Math.min(startSecs, buf.duration - 0.001));
      node.start(when, offset);
      stem.activeNodes.push(node);
      playDur = Math.max(playDur, buf.duration - offset);
    }
    return playDur;
  }

  // --- lookahead scheduler ---

  async function _scheduleNext() {
    const chunkIdx = Math.floor(_scheduledTo / CHUNK_SEC);
    _fetchChunk(chunkIdx + 1); // fire-and-forget pre-fetch of the chunk after

    // Use the synchronous result if already decoded, otherwise await.
    const hit = _cache.get(chunkIdx);
    const buffers = hit?.result ?? await _fetchChunk(chunkIdx);
    if (!playing || destroyed) return;
    if (!buffers || buffers.size === 0) return; // past end; tick() handles onEnded

    // With SoundTouch, sources play at 1.0x so scheduled wall-clock time equals
    // audio time. With tape-effect (_playbackRate != 1, no stNode), sources play
    // faster/slower so wall-clock time = audio seconds / _playbackRate.
    const playFactor = stNode ? 1.0 : _playbackRate;
    const idealWhen = _startCtxTime + (_scheduledTo - _startOffset) / playFactor;
    const when = Math.max(idealWhen, ctx.currentTime + 0.01);
    // If we're late (slow network), skip the audio portion that already "passed".
    const firstBuf = buffers.values().next().value;
    const maxSkip = firstBuf ? Math.max(0, firstBuf.duration - 0.001) : 0;
    const bufOffset = Math.min(Math.max(0, when - idealWhen) * playFactor, maxSkip);

    const dur = _scheduleChunk(buffers, when, bufOffset);
    _scheduledTo += bufOffset + dur; // always advances by ~CHUNK_SEC
  }

  function _maybeSchedule() {
    if (_filling || !playing || destroyed) return;
    // While looping, don't schedule past loop.end — _tick jumps back to
    // loop.start when the playhead crosses it (bounded by one rAF frame).
    const limit = (loop.enabled && loop.end > loop.start) ? loop.end : _duration;
    if (_scheduledTo >= limit) return;
    if (_scheduledTo - _getCurrentTime() < LOOKAHEAD_SEC) {
      _filling = true;
      _scheduleNext().finally(() => { _filling = false; });
    }
  }

  function _tick() {
    if (!playing) return;
    const t = _getCurrentTime();
    if (loop.enabled && loop.end > loop.start) {
      // Warm the loop-start chunk while approaching the end so the jump back
      // schedules from cache instead of paying a fetch (deduped by _cache and
      // pinned against eviction while the loop stays active).
      if (loop.end - t < LOOKAHEAD_SEC) _fetchChunk(_loopPinChunk());
      if (t >= loop.end) {
        seek(loop.start); // re-arms playback + tick from the loop start
        return;
      }
    }
    if (t >= _duration) {
      playing = false;
      _audioStarted = false;
      _startOffset = _duration;
      if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
      onTime?.(_duration);
      onEnded?.();
      return;
    }
    _maybeSchedule();
    onTime?.(t);
    rafId = requestAnimationFrame(_tick);
  }

  // --- public API ---

  // `leadIn` (source seconds, default 0) delays the moment the stems begin so
  // a count-in can sound in the gap first -- see audioEngine.js's play() for
  // the full contract. Only takes effect on the common cached-chunk-0 path
  // (see the sync/async branch below); by the time count-in can even be
  // armed the track has been loaded long enough that chunk 0 is virtually
  // always already cached.
  function play(leadIn = 0) {
    if (playing || destroyed) return;
    if (ctx.state === "suspended") ctx.resume().catch(() => {});
    playing = true;

    const chunkIdx = Math.floor(_startOffset / CHUNK_SEC);
    const offsetWithin = _startOffset - chunkIdx * CHUNK_SEC;
    const countInLead = leadIn > 0 ? leadIn / _srcRate() + COUNT_IN_MARGIN : 0;

    // `lead` = scheduling safety margin. The cached (sync) path uses 10 ms —
    // tight enough that loop jumps are near-seamless — while the async path
    // keeps 50 ms headroom since a fetch/decode just finished. A count-in's
    // lead overrides either when it asks for more room than that.
    const startWith = (buffers, lead) => {
      if (!playing || destroyed) return;
      _resetProcessor();
      const when = ctx.currentTime + Math.max(lead, countInLead);
      _startCtxTime = when;
      const dur = _scheduleChunk(buffers, when, offsetWithin);
      _scheduledTo = _startOffset + dur;
      _audioStarted = true;
      _epoch++; // mapping is valid from here; see isClockReady
      _fetchChunk(chunkIdx + 1); // pre-fetch next chunk
      rafId = requestAnimationFrame(_tick);
    };

    // chunk 0 is pre-decoded during ready(), so the sync path is the hot path.
    const hit = _cache.get(chunkIdx);
    if (hit?.result) {
      startWith(hit.result, 0.01);
    } else {
      _fetchChunk(chunkIdx)
        .then((buffers) => startWith(buffers, 0.05))
        .catch((e) => {
          console.warn("[chunked] play fetch failed:", e);
          playing = false;
          _audioStarted = false;
        });
    }
  }

  function pause() {
    if (!playing) return;
    _startOffset = _getCurrentTime();
    _stopNodes();
    _resetProcessor();
    playing = false;
    _audioStarted = false;
    _epoch++;
    _scheduledTo = _startOffset;
    if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
  }

  function seek(t) {
    const clamped = Math.max(0, Math.min(t, _duration || 0));
    const wasPlaying = playing;
    if (wasPlaying) {
      _stopNodes();
      playing = false;
      _audioStarted = false;
      if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
    }
    _resetProcessor();
    _startOffset = clamped;
    _scheduledTo = clamped;
    _epoch++;
    // Evict cache for chunks before the new position (except the pinned
    // loop-start chunk — a loop jump seeks backward *to* that chunk).
    const newIdx = Math.floor(clamped / CHUNK_SEC);
    const pin = _loopPinChunk();
    for (const k of _cache.keys()) {
      if (k < newIdx && k !== pin) _cache.delete(k);
    }
    onTime?.(clamped);
    if (wasPlaying) play();
  }

  // Initialize: fetch all WAV headers in parallel and load the SoundTouch worklet.
  // Chunk 0 is kicked off in the background so ready() resolves quickly (headers
  // only, ~6 x 1 KB) instead of blocking on the full first-chunk download (~5 MB).
  // play() handles the case where chunk 0 is not yet cached.
  const ready = (async () => {
    if (!stemMap.size) {
      _loadError = "This track has no stem files to play.";
      return false;
    }

    // Counted so the failure can name a cause. "Could not download" and "could
    // not read" send the user somewhere completely different, and until #359
    // both arrived as the same silent console warning.
    let unreachable = 0;
    let unreadable = 0;

    await Promise.all([
      _workletReady,
      ...[...stemMap.entries()].map(async ([name, stem]) => {
        try {
          stem.header = await _fetchHeader(stem.url);
          if (!stem.header) {
            unreadable++;
            console.warn(`[chunked] unreadable WAV header for stem "${name}"`);
          }
        } catch (e) {
          unreachable++;
          console.warn(`[chunked] header fetch failed for stem "${name}":`, e);
        }
      }),
    ]);

    for (const stem of stemMap.values()) {
      if (stem.header) _duration = Math.max(_duration, stem.header.duration);
    }
    if (!_duration) {
      _loadError = unreadable
        ? "This track's audio files are in a format StemDeck could not read."
        : unreachable
          ? "Could not load this track's audio files."
          : "This track's audio files contain no audio.";
      return false;
    }

    // Kick off chunk 0 and 1 in the background; play() picks up the cached result.
    _fetchChunk(0);
    _fetchChunk(1);
    return true;
  })();

  return {
    ready,
    getLoadError: () => _loadError,
    play,
    pause,
    seek,
    setTime: seek,
    isPlaying: () => playing,
    // This engine honours play(leadIn) for a count-in (see play() above); the
    // transport checks this before scheduling one.
    supportsCountIn: true,
    getCurrentTime: _getCurrentTime,
    getDuration: () => _duration,
    setLoop: (enabled, start, end) => { loop = { enabled, start, end }; },
    setGain(name, v) {
      for (const stem of stemMap.values()) {
        if (stem.controlName === name) {
          stem.level = Math.max(0, v);
          stem.gain.gain.setTargetAtTime(stem.level, ctx.currentTime, 0.01);
        }
      }
    },
    setMasterGain(v) {
      master.gain.setTargetAtTime(Math.max(0, v), ctx.currentTime, 0.01);
    },
    // The click shares the unpitched input with drums while still using the
    // common tempo stage and master gain.
    sourceTimeToCtxTime,
    ctxTimeToSourceTime,
    getScheduleEpoch: () => _epoch,
    // `playing` flips before the async chunk fetch resolves, so the clock is
    // only trustworthy once _audioStarted is set.
    isClockReady: () => playing && _audioStarted,
    getMasterNode: () => buses[ZERO_INPUT],
    /**
     * Put one mixer lane in a given key.
     *
     * Nothing seeks and nothing is flushed. Every bus is delayed by the same
     * amount, so the lane's old bus plays out the couple of hundred
     * milliseconds it has already buffered while its new bus, primed with
     * exactly that much silence, takes over at the instant the old one runs
     * dry. The handover is a duck, not a gap.
     */
    setStemPitch(name, semitones) {
      if (!stNode) return false;
      const next = clampPitch(semitones);
      let found = false;
      for (const stem of stemMap.values()) {
        if (stem.controlName !== name) continue;
        found = true;
        if (stem.pitch === next) continue;
        stem.pitch = next;
        routeStem(stem);
      }
      return found;
    },
    getStemPitch(name) {
      for (const stem of stemMap.values()) {
        if (stem.controlName === name) return stem.pitch;
      }
      return 0;
    },
    /** False for lanes that must never be resampled, so the UI can say so. */
    isStemPitchable(name) {
      for (const stem of stemMap.values()) {
        if (stem.controlName === name) return stem.pitchable;
      }
      return false;
    },
    supportsPitchShift: () => !!stNode,
    setPlaybackRate(rate) {
      const t = _getCurrentTime(); // capture before updating rate
      _playbackRate = rate;
      if (stNode) {
        stNode.parameters.get('tempo').value = rate;
        if (playing) seek(t);
        else {
          _resetProcessor();
          _epoch++;
        }
      } else if (playing) {
        // Tape-effect fallback: seek to current position so new source nodes
        // are created with the updated playbackRate and scheduling math resets.
        seek(t);
      } else {
        _epoch++;
      }
    },
    getAnalysers: (name) => [...stemMap.values()]
      .filter((stem) => stem.controlName === name)
      .map((stem) => stem.analyser),
    getAnalyser: (name) => [...stemMap.values()]
      .find((stem) => stem.controlName === name)?.analyser ?? null,
    getBuffers: () => new Map(),
    destroy() {
      destroyed = true;
      if (playing) pause();
      else _resetProcessor();
      if (stNode) { try { stNode.disconnect(); } catch { /* noop */ } }
      _cache.clear();
      stemMap.clear();
      if (ownsCtx) ctx.close().catch(() => {});
    },
    audioContext: ctx,
  };
}
