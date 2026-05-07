import { TRACK_NAMES } from "./constants.js";
import {
  multitrack, mixerEl, audioContext, trackAnalysers,
  vuRafId, trackIndex,
  setAudioContext, setVuRafId,
} from "./state.js";

export function attachAnalysers() {
  if (!multitrack) return;
  if (trackAnalysers.length) return;
  const wsArr = multitrack.wavesurfers || multitrack._wavesurfers;
  const audios = multitrack.audios;
  if (!wsArr?.length || !audios?.length) return;

  const ctx = multitrack.audioContext;
  if (!ctx) return;
  if (ctx.state === "suspended") ctx.resume().catch(() => {});
  setAudioContext(ctx);

  for (const stemName of TRACK_NAMES) {
    const idx = trackIndex[stemName];
    if (idx === undefined) continue;
    const audioEl = audios[idx];
    if (!audioEl) continue;

    const isHtmlMedia = audioEl instanceof HTMLMediaElement;
    const isWebAudio = typeof audioEl.getGainNode === "function";
    if (!isHtmlMedia && !isWebAudio) continue;
    if (isHtmlMedia && !audioEl.src) continue;

    let analyser;
    try {
      analyser = ctx.createAnalyser();
      analyser.fftSize = 256;
      analyser.smoothingTimeConstant = 0.5;
      if (isWebAudio) {
        // wavesurfer's audio wrapper exposes its internal gain node;
        // analyser taps the post-gain signal.
        audioEl.getGainNode().connect(analyser);
      } else {
        // Bare HTMLAudioElement: route through Web Audio so we can tap
        // the post-gain signal. applyMix() may have already created the
        // MediaElementSource + GainNode chain; re-use it if so (the spec
        // only allows createMediaElementSource once per element).
        if (!audioEl._stMediaSource) {
          audioEl._stMediaSource = ctx.createMediaElementSource(audioEl);
          audioEl._stGainNode = ctx.createGain();
          audioEl._stMediaSource.connect(audioEl._stGainNode);
          audioEl._stGainNode.connect(ctx.destination);
          audioEl.volume = 1;
        } else if (!audioEl._stGainNode) {
          audioEl._stGainNode = ctx.createGain();
          audioEl._stMediaSource.connect(audioEl._stGainNode);
          audioEl._stGainNode.connect(ctx.destination);
          audioEl.volume = 1;
        }
        audioEl._stGainNode.connect(analyser);
      }
    } catch (err) {
      console.warn("VU analyser hookup failed for stem", stemName, err);
      continue;
    }
    // Time-domain buffer must be sized to fftSize (not frequencyBinCount,
    // which is fftSize/2). With a too-small buffer, getByteTimeDomainData
    // only writes the first N samples and trailing entries keep stale
    // values, biasing RMS toward whatever was last left there.
    const data = new Uint8Array(analyser.fftSize);
    const vuEl = mixerEl.querySelector(`.lane-vu[data-stem="${stemName}"]`);
    const miniMeterEl = document.querySelector(`.stem-list .${stemName} .mini-meter`);
    trackAnalysers.push({ analyser, data, vuEl, miniMeterEl, peak: 0 });
  }

  const tick = () => {
    for (const t of trackAnalysers) {
      t.analyser.getByteTimeDomainData(t.data);
      let sum = 0;
      for (let i = 0; i < t.data.length; i++) {
        const v = (t.data[i] - 128) / 128;
        sum += v * v;
      }
      const rms = Math.sqrt(sum / t.data.length);
      // Linear RMS with 2.5x gain. Compensates for the 0.5 master
      // attenuation the analyser sees post-gain via createMediaElementSource,
      // so typical -20 dBFS music lands in the 25-50% bar range, drum
      // hits push toward 80-100%, silence falls to 0. Linear (not dB)
      // gives the meter punch on transients that dB scaling smooths out.
      const level = Math.min(1, rms * 2.5);
      // Peak hold: bar follows level on the way up, falls slowly on
      // the way down. That's what makes a VU look alive -- it lifts
      // sharply on each drum hit / vocal phrase and drifts down in
      // between rather than tracking instantaneous RMS jitter.
      const prevPeak = t.peak || 0;
      const nextPeak = level > prevPeak ? level : Math.max(0, prevPeak - 0.012);
      t.peak = nextPeak;
      // Separate peak-hold marker that holds the recent max for
      // ~600 ms before falling, so a thin tick visually marks
      // "loudest moment in the last second" above the bar.
      const prevHold = t.peakHold || 0;
      let nextHold = prevHold;
      let holdFrames = t.holdFrames || 0;
      if (level > prevHold) {
        nextHold = level;
        holdFrames = 36;
      } else if (holdFrames > 0) {
        holdFrames -= 1;
      } else {
        nextHold = Math.max(0, prevHold - 0.02);
      }
      t.peakHold = nextHold;
      t.holdFrames = holdFrames;
      const lvlPct = Math.round(level * 100);
      const peakPct = Math.round(nextPeak * 100);
      const holdPct = Math.round(nextHold * 100);
      if (t.vuEl) {
        if (lvlPct !== t.lastLevelPct) {
          t.vuEl.style.setProperty("--vu-level", `${lvlPct}%`);
        }
        if (holdPct !== t.lastHoldPct) {
          t.vuEl.style.setProperty("--vu-peak", `${holdPct}%`);
        }
      }
      if (t.miniMeterEl) {
        if (peakPct !== t.lastPeakPct) {
          t.miniMeterEl.style.setProperty("--vu-scale", nextPeak.toFixed(3));
        }
        if (holdPct !== t.lastHoldPct) {
          t.miniMeterEl.style.setProperty("--vu-peak-pct", String(holdPct));
          t.miniMeterEl.style.setProperty(
            "--vu-peak-opacity",
            nextHold > 0.04 ? "1" : "0",
          );
        }
      }
      t.lastLevelPct = lvlPct;
      t.lastPeakPct = peakPct;
      t.lastHoldPct = holdPct;
    }

    setVuRafId(requestAnimationFrame(tick));
  };
  setVuRafId(requestAnimationFrame(tick));
}

export function stopVuLoop() {
  if (vuRafId) {
    cancelAnimationFrame(vuRafId);
    setVuRafId(null);
  }
}