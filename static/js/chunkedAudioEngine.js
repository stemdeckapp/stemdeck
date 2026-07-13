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

// ---------------------------------------------------------------------------
// WAV parsing
// ---------------------------------------------------------------------------

function _parseWavHeader(buf) {
  const view = new DataView(buf);
  const tag = (off) => String.fromCharCode(...new Uint8Array(buf, off, 4));

  if (tag(0) !== "RIFF" || tag(8) !== "WAVE") return null;

  let audioFormat = 1, channels = 2, sampleRate = 44100, bitsPerSample = 16;
  let dataOffset = -1, dataSize = 0;

  let off = 12;
  while (off + 8 <= buf.byteLength) {
    const id = tag(off);
    const size = view.getUint32(off + 4, true);
    if (id === "fmt ") {
      audioFormat   = view.getUint16(off + 8,  true);
      channels      = view.getUint16(off + 10, true);
      sampleRate    = view.getUint32(off + 12, true);
      bitsPerSample = view.getUint16(off + 22, true);
    } else if (id === "data") {
      dataOffset = off + 8;
      dataSize   = size;
      break;
    }
    off += 8 + size + (size & 1); // chunks are word-aligned
  }

  if (dataOffset < 0) return null;

  const bytesPerFrame = channels * (bitsPerSample >> 3);
  return {
    audioFormat, channels, sampleRate, bitsPerSample,
    dataOffset, dataSize, bytesPerFrame,
    duration: dataSize / (bytesPerFrame * sampleRate),
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
 * @param {{name:string,url:string}[]} stems  Active stems (WAV URLs).
 * @param {{onTime?:(t:number)=>void, onEnded?:()=>void, context?:AudioContext}} opts
 */
export function createChunkedAudioEngine(stems, { onTime, onEnded, context } = {}) {
  const AC = window.AudioContext || window.webkitAudioContext;
  const ctx = context || new AC();
  const ownsCtx = !context;
  const master = ctx.createGain();

  let stNode = null;
  let _playbackRate = 1.0;
  const _workletReady = (ctx.audioWorklet
    ? ctx.audioWorklet.addModule('/vendor/soundtouch-processor.js').then(() => {
        stNode = new AudioWorkletNode(ctx, 'soundtouch-processor');
        master.connect(stNode);
        stNode.connect(ctx.destination);
      }).catch((err) => {
        console.warn('[chunkedEngine] SoundTouch worklet failed, tape-effect fallback:', err);
        master.connect(ctx.destination);
      })
    : Promise.resolve().then(() => { master.connect(ctx.destination); }));

  // Per-stem state: url, parsed WAV header, gain node, analyser (VU tap), and
  // currently playing nodes. Graph per stem: sources -> gain -> analyser -> master.
  // The analyser sits post-gain so VU meters reflect volume/mute/solo.
  const stemMap = new Map();
  for (const s of stems) {
    if (!s?.url) continue;
    const gain = ctx.createGain();
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 1024;
    gain.connect(analyser);
    analyser.connect(master);
    stemMap.set(s.name, { url: s.url, header: null, gain, analyser, activeNodes: [] });
  }

  let _duration = 0;
  let playing = false;
  let destroyed = false;
  let rafId = null;

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
  let loop = { enabled: false, start: 0, end: 0 };
  // Chunk index that must survive cache eviction while looping (the loop-start
  // chunk), so every pass around the loop replays from cache with no refetch.
  const _loopPinChunk = () =>
    (loop.enabled && loop.end > loop.start) ? Math.floor(loop.start / CHUNK_SEC) : -1;

  // Chunk cache: chunkIdx -> { promise: Promise<Map>, result: Map|null }
  // result is set synchronously once the promise resolves so play() can
  // schedule chunk 0 without an async await after ready() completes.
  const _cache = new Map();

  function _getCurrentTime() {
    if (!playing || !_audioStarted) return _startOffset;
    return Math.min((ctx.currentTime - _startCtxTime) * _playbackRate + _startOffset, _duration);
  }

  // --- fetch helpers ---

  async function _fetchHeader(url) {
    const res = await fetch(url, { headers: { Range: "bytes=0-1023" } });
    const buf = await res.arrayBuffer();
    return _parseWavHeader(buf);
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

  function play() {
    if (playing || destroyed) return;
    if (ctx.state === "suspended") ctx.resume().catch(() => {});
    playing = true;

    const chunkIdx = Math.floor(_startOffset / CHUNK_SEC);
    const offsetWithin = _startOffset - chunkIdx * CHUNK_SEC;

    // `lead` = scheduling safety margin. The cached (sync) path uses 10 ms —
    // tight enough that loop jumps are near-seamless — while the async path
    // keeps 50 ms headroom since a fetch/decode just finished.
    const startWith = (buffers, lead) => {
      if (!playing || destroyed) return;
      const when = ctx.currentTime + lead;
      _startCtxTime = when;
      const dur = _scheduleChunk(buffers, when, offsetWithin);
      _scheduledTo = _startOffset + dur;
      _audioStarted = true;
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
    playing = false;
    _audioStarted = false;
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
    _startOffset = clamped;
    _scheduledTo = clamped;
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
    if (!stemMap.size) return false;

    await Promise.all([
      _workletReady,
      ...[...stemMap.values()].map(async (stem) => {
        try { stem.header = await _fetchHeader(stem.url); }
        catch (e) { console.warn("[chunked] header fetch failed:", e); }
      }),
    ]);

    for (const stem of stemMap.values()) {
      if (stem.header) _duration = Math.max(_duration, stem.header.duration);
    }
    if (!_duration) return false;

    // Kick off chunk 0 and 1 in the background; play() picks up the cached result.
    _fetchChunk(0);
    _fetchChunk(1);
    return true;
  })();

  return {
    ready,
    play,
    pause,
    seek,
    setTime: seek,
    isPlaying: () => playing,
    getCurrentTime: _getCurrentTime,
    getDuration: () => _duration,
    setLoop: (enabled, start, end) => { loop = { enabled, start, end }; },
    setGain(name, v) {
      const stem = stemMap.get(name);
      if (stem) stem.gain.gain.setTargetAtTime(Math.max(0, v), ctx.currentTime, 0.01);
    },
    setMasterGain(v) {
      master.gain.setTargetAtTime(Math.max(0, v), ctx.currentTime, 0.01);
    },
    setPlaybackRate(rate) {
      const t = _getCurrentTime(); // capture before updating rate
      _playbackRate = rate;
      if (stNode) {
        stNode.parameters.get('tempo').value = rate;
      } else if (playing) {
        // Tape-effect fallback: seek to current position so new source nodes
        // are created with the updated playbackRate and scheduling math resets.
        seek(t);
      }
    },
    getAnalyser: (name) => stemMap.get(name)?.analyser ?? null,
    getBuffers: () => new Map(),
    destroy() {
      destroyed = true;
      if (playing) pause();
      if (stNode) { try { stNode.disconnect(); } catch { /* noop */ } }
      _cache.clear();
      stemMap.clear();
      if (ownsCtx) ctx.close().catch(() => {});
    },
    audioContext: ctx,
  };
}
