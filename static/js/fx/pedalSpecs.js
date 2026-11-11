// The pedal types of the Advanced mode effects chain, as data: each control
// with its range in real units, its taper, its default per instrument, and
// the built-in presets per instrument. No DOM, no Web Audio and no imports,
// so tests/js/fx-chain.test.mjs runs it in node.
//
// Control ids are stable and never renamed: a song's saved chain is keyed by
// them (mixerState[lane].fx, chainModel.js).
//
// Product names appear in code comments only, never in the UI: the presets
// are "modelled on" a pedal, and the names a user reads are generic.
// Every value without a citation is a design proposal, to be tuned by ear.
// The figures and their sources: research-effects-chains-voice-guitar-bass.md
// (the maintainer's design report), section 4.

/** The instruments a chain can be voiced for (a lane's Instrument choice). */
export const INSTRUMENTS = Object.freeze(["guitar", "bass", "voice"]);

/**
 * The suggested order of pedal types per instrument (report section 3.5). A
 * new pedal goes where this order puts it among the pedals already there;
 * the user can then move it anywhere. Types listed here but not in PEDALS do
 * not exist yet and only hold their place in the order.
 */
export const DEFAULT_ORDER = Object.freeze({
  guitar: ["compressor", "envfilter", "drive", "eqpedal", "gate", "phaser", "chorus", "tremolo", "delay", "reverb"],
  bass: ["eqpedal", "compressor", "octave", "envfilter", "drive", "gate", "chorus", "phaser", "delay", "reverb"],
  voice: ["deesser", "eqpedal", "compressor", "drive", "chorus", "phaser", "delay", "reverb"],
});

/**
 * How a pedal's Mix sets its dry and wet levels.
 *   "pedal": an equal-power crossfade, dry cos(m pi/2), wet sin(m pi/2), so
 *            the loudness stays about level across the knob for a wet signal
 *            that is not correlated with the dry one (proposal).
 *   "send":  the dry stays at 100 % and the wet adds, as a studio aux send
 *            (report D10: delay and reverb on a voice).
 * Mix 0 is exactly dry 1 and wet 0 either way.
 */
export function mixGains(mix, law) {
  const m = Math.max(0, Math.min(1, Number(mix) || 0));
  if (law === "send") return { dry: 1, wet: m };
  if (m === 0) return { dry: 1, wet: 0 };
  if (m === 1) return { dry: 0, wet: 1 };
  return { dry: Math.cos((m * Math.PI) / 2), wet: Math.sin((m * Math.PI) / 2) };
}

/**
 * Tempo divisions for a synced time, in quarter notes (report 4.4: 1/4,
 * dotted 1/8, 1/8, 1/8 triplet, 1/16). "off" means the free Time knob.
 */
export const SYNC_FACTORS = Object.freeze({
  "1/4": 1,
  "1/8d": 0.75,
  "1/8": 0.5,
  "1/8t": 1 / 3,
  "1/16": 0.25,
});

// ─── Delay ───
//
// References (report 4.4, verified manuals unless marked):
//   MXR Carbon Copy M169: 20 to 600 ms, modulation 0.2 to 2.2 Hz, dark analog
//     repeats.
//   Empress Echosystem: Digital, Tape (Old Tape: wow and flutter), Analog.
//   Empress Superdelay: 2.8 s, tap with ratios, "dynamic" mode (ducking; its
//     exact behaviour not found).
// Vocal practice (reported): slapback 80 to 120 ms, throws at 1/4 or dotted
// 1/8, repeats high-passed 200 to 500 Hz and low-passed 3 to 8 kHz, ducking.
//
// Characters: digital (clean repeats; a time change crossfades to a new delay
// line), analog (each repeat saturates softly; a time change glides, the
// pitch bend a bucket-brigade delay makes), tape (analog plus a fixed flutter
// on top of the Modulation, which plays the slow wow).

const DELAY = {
  type: "delay",
  name: "fx.pedal.delay",
  // The tails ring when the pedal is switched off or the transport stops,
  // and are flushed on a seek (report D7).
  trails: true,
  law: { guitar: "pedal", bass: "pedal", voice: "send" },
  controls: [
    { id: "char", kind: "select", name: "fx.ctl.character", help: "help.fxDelayCharacter",
      options: [
        { v: "digital", name: "fx.opt.digital" },
        { v: "analog", name: "fx.opt.analog" },
        { v: "tape", name: "fx.opt.tape" },
      ],
      def: { guitar: "analog", bass: "digital", voice: "digital" } },
    { id: "sync", kind: "select", name: "fx.ctl.sync", help: "help.fxDelaySync",
      options: [
        { v: "off", name: "fx.opt.syncOff" },
        { v: "1/4", name: "fx.opt.sync14" },
        { v: "1/8d", name: "fx.opt.sync18d" },
        { v: "1/8", name: "fx.opt.sync18" },
        { v: "1/8t", name: "fx.opt.sync18t" },
        { v: "1/16", name: "fx.opt.sync116" },
      ],
      def: "off" },
    { id: "stereo", kind: "select", name: "fx.ctl.stereo", help: "help.fxDelayStereo",
      options: [
        { v: "mono", name: "fx.opt.mono" },
        { v: "pingpong", name: "fx.opt.pingpong" },
      ],
      def: "mono" },
    // Seconds. 20 ms to 2 s (report 4.4 controls).
    { id: "time", kind: "knob", name: "fx.ctl.time", help: "help.fxDelayTime",
      min: 0.02, max: 2, unit: "s", taper: "log", def: { guitar: 0.35, bass: 0.25, voice: 0.3 } },
    // Fraction of each repeat fed back, capped at 98 % (report 3.6).
    { id: "fb", kind: "knob", name: "fx.ctl.feedback", help: "help.fxDelayFeedback",
      min: 0, max: 0.98, unit: "%", taper: "lin", def: { guitar: 0.35, bass: 0.2, voice: 0.25 } },
    // A high-pass on the repeats. Bass: the repeats must not double the
    // fundamental (body 80 to 250 Hz), so its default is high (report 4.4).
    { id: "lowcut", kind: "knob", name: "fx.ctl.lowCut", help: "help.fxDelayLowCut",
      min: 20, max: 800, unit: "Hz", taper: "log", def: { guitar: 80, bass: 200, voice: 250 } },
    { id: "highcut", kind: "knob", name: "fx.ctl.highCut", help: "help.fxDelayHighCut",
      min: 1000, max: 12000, unit: "Hz", taper: "log", def: { guitar: 4000, bass: 3000, voice: 6000 } },
    // Modulation depth of the delay time, in milliseconds (proposal: 0 to 5).
    { id: "mod", kind: "knob", name: "fx.ctl.modDepth", help: "help.fxDelayMod",
      min: 0, max: 5, unit: "ms", taper: "lin", def: 0 },
    // Modulation rate, 0.2 to 2.2 Hz (Carbon Copy, verified).
    { id: "rate", kind: "knob", name: "fx.ctl.modRate", help: "help.fxDelayRate",
      min: 0.2, max: 2.2, unit: "rateHz", taper: "log", def: 1 },
    // How far the repeats drop while the voice sings, in dB (voice only).
    { id: "duck", kind: "knob", name: "fx.ctl.ducking", help: "help.fxDelayDuck",
      min: 0, max: 24, unit: "dB", taper: "lin", def: 0, only: ["voice"] },
    { id: "mix", kind: "knob", name: "fx.ctl.mix", help: "help.fxMix",
      min: 0, max: 1, unit: "%", taper: "lin", def: { guitar: 0.3, bass: 0.2, voice: 0.2 } },
  ],
  presets: [
    // Guitar
    // Modelled on the MXR Carbon Copy (report 4.4): analog, 450 ms, 40 %,
    // high cut 2.5 kHz, modulation 1 Hz, mix 35 %. Depth: proposal.
    { id: "guitar.delay.darkAnalog", inst: "guitar", name: "fx.preset.delay.darkAnalog",
      p: { char: "analog", sync: "off", time: 0.45, fb: 0.4, lowcut: 80, highcut: 2500, mod: 1.5, rate: 1, mix: 0.35 } },
    // Modelled on the Empress Echosystem Old Tape: tape, 380 ms, 45 %, wow
    // and flutter on, high cut 3 kHz. Mix: proposal.
    { id: "guitar.delay.oldTape", inst: "guitar", name: "fx.preset.delay.oldTape",
      p: { char: "tape", sync: "off", time: 0.38, fb: 0.45, lowcut: 100, highcut: 3000, mod: 2, rate: 0.5, mix: 0.35 } },
    // A digital delay with a tap ratio: dotted 1/8, 30 %, mix 30 %.
    { id: "guitar.delay.dotted", inst: "guitar", name: "fx.preset.delay.dotted",
      p: { char: "digital", sync: "1/8d", time: 0.35, fb: 0.3, lowcut: 80, highcut: 6000, mod: 0, mix: 0.3 } },
    // Bass (report 4.4)
    { id: "bass.delay.slapback", inst: "bass", name: "fx.preset.delay.bassSlapback",
      p: { char: "digital", sync: "off", time: 0.09, fb: 0.1, lowcut: 200, highcut: 5000, mod: 0, mix: 0.2 } },
    { id: "bass.delay.dub", inst: "bass", name: "fx.preset.delay.dub",
      p: { char: "analog", sync: "1/4", time: 0.5, fb: 0.55, lowcut: 150, highcut: 2000, mod: 0, mix: 0.3 } },
    { id: "bass.delay.ambient", inst: "bass", name: "fx.preset.delay.ambientBass",
      p: { char: "tape", sync: "off", time: 0.5, fb: 0.45, lowcut: 250, highcut: 3000, mod: 1.5, rate: 0.5, mix: 0.25 } },
    // Voice, send law (report 4.4)
    { id: "voice.delay.slapback", inst: "voice", name: "fx.preset.delay.vocalSlapback",
      p: { char: "digital", sync: "off", time: 0.1, fb: 0, lowcut: 300, highcut: 5000, mod: 0, duck: 0, mix: 0.2 } },
    { id: "voice.delay.quarter", inst: "voice", name: "fx.preset.delay.quarterThrow",
      p: { char: "digital", sync: "1/4", time: 0.5, fb: 0.3, lowcut: 200, highcut: 5500, mod: 0, duck: 0, mix: 0.25 } },
    { id: "voice.delay.ducked", inst: "voice", name: "fx.preset.delay.duckedDotted",
      p: { char: "digital", sync: "1/8d", time: 0.35, fb: 0.35, lowcut: 250, highcut: 6000, mod: 0, duck: 10, mix: 0.25 } },
  ],
};

// ─── Chorus, doubler, vibrato, flanger ───
//
// References (report 4.3, verified manuals unless marked):
//   MXR M234 Analog Chorus: LOW 70 to 800 Hz shelf and HIGH tone on the
//     effect only; rate and delay ranges not found.
//   Boss CE-2 (reported): 8.5 to 10.75 ms sweep, triangle LFO.
//   Roland Juno-60 chorus I (reported): 0.5 Hz triangle, L and R inverted,
//     wet about -1.62 dB against dry.
//   MXR M83 Bass Chorus Deluxe: rate 0.1 to 10 Hz, delay 0.4 to 12 ms,
//     X-OVER keeps the lows dry, BASS and TREBLE on the wet only, flanger
//     mode with regeneration.
//   Voice: a doubler, not a chorus: about 10 to 20 ms, a slow random drift
//     (Abbey Road ADT, Sound On Sound, Synchro Arts; reported).
//
// Depth is the delay sweep: 100 % = +/-3 ms around Time (proposal; the
// CE-2's 8.5 to 10.75 ms is about 40 %). Feedback turns it into a flanger;
// in Chromium a feedback loop cannot be shorter than one render quantum
// (128 frames, 2.67 ms at 48 kHz), so with feedback the sweep never goes
// below that (dspBlocks.loopOverheadFrames). The M83's 0.4 ms flanger floor
// needs an AudioWorklet and is not done.

const CHORUS = {
  type: "chorus",
  name: "fx.pedal.chorus",
  trails: false,
  law: { guitar: "pedal", bass: "pedal", voice: "pedal" },
  controls: [
    { id: "shape", kind: "select", name: "fx.ctl.shape", help: "help.fxChorusShape",
      options: [
        { v: "triangle", name: "fx.opt.triangle" },
        { v: "sine", name: "fx.opt.sine" },
        { v: "drift", name: "fx.opt.drift" },
      ],
      def: { guitar: "triangle", bass: "triangle", voice: "drift" } },
    { id: "stereo", kind: "select", name: "fx.ctl.stereo", help: "help.fxChorusStereo",
      options: [
        { v: "mono", name: "fx.opt.mono" },
        { v: "wide", name: "fx.opt.wide" },
      ],
      def: { guitar: "mono", bass: "mono", voice: "wide" } },
    // Base delay, ms.
    { id: "dtime", kind: "knob", name: "fx.ctl.time", help: "help.fxChorusTime",
      min: 0.4, max: 20, unit: "ms", taper: "log", def: { guitar: 9.6, bass: 8, voice: 12 } },
    // The right side's delay is Time plus this (a wide double), ms.
    { id: "offset", kind: "knob", name: "fx.ctl.offset", help: "help.fxChorusOffset",
      min: 0, max: 10, unit: "ms", taper: "lin", def: 0 },
    { id: "rate", kind: "knob", name: "fx.ctl.rate", help: "help.fxChorusRate",
      min: 0.05, max: 10, unit: "rateHz", taper: "log", def: { guitar: 0.8, bass: 1, voice: 0.3 } },
    { id: "depth", kind: "knob", name: "fx.ctl.depth", help: "help.fxChorusDepth",
      min: 0, max: 1, unit: "%", taper: "lin", def: { guitar: 0.4, bass: 0.4, voice: 0.05 } },
    { id: "fb", kind: "knob", name: "fx.ctl.feedback", help: "help.fxChorusFeedback",
      min: 0, max: 0.9, unit: "%", taper: "lin", def: 0 },
    // A low shelf at 200 Hz on the effect only, dB (M234 LOW, M83 BASS).
    { id: "wetlow", kind: "knob", name: "fx.ctl.wetBass", help: "help.fxChorusWetBass",
      min: -12, max: 8, unit: "dB", taper: "lin", def: { guitar: -3, bass: -2, voice: 0 } },
    { id: "highcut", kind: "knob", name: "fx.ctl.highCut", help: "help.fxChorusHighCut",
      min: 1000, max: 12000, unit: "Hz", taper: "log", def: { guitar: 6000, bass: 8000, voice: 9000 } },
    // Lows below this stay dry (M83 X-OVER); the lowest setting is off.
    { id: "xover", kind: "knob", name: "fx.ctl.crossover", help: "help.fxCrossover",
      min: 30, max: 300, unit: "Hz", taper: "log", offAtMin: true, def: { guitar: 30, bass: 160, voice: 30 } },
    { id: "mix", kind: "knob", name: "fx.ctl.mix", help: "help.fxMix",
      min: 0, max: 1, unit: "%", taper: "lin", def: { guitar: 0.5, bass: 0.5, voice: 0.4 } },
  ],
  presets: [
    // Modelled on the MXR M234 and Boss CE-2 (report 4.3).
    { id: "guitar.chorus.warm", inst: "guitar", name: "fx.preset.chorus.warm",
      p: { shape: "triangle", stereo: "mono", dtime: 9.6, offset: 0, rate: 0.8, depth: 0.4, fb: 0, wetlow: -6, highcut: 5000, xover: 30, mix: 0.5 } },
    // Modelled on the Juno-60 chorus I: wet -1.62 dB against dry is mix
    // 0.441 on the pedal law (tan(pi m / 2) = 0.83).
    { id: "guitar.chorus.ensemble", inst: "guitar", name: "fx.preset.chorus.ensemble",
      p: { shape: "triangle", stereo: "wide", dtime: 5, offset: 0, rate: 0.5, depth: 0.4, fb: 0, wetlow: 0, highcut: 8000, xover: 30, mix: 0.441 } },
    // Modelled on the Empress Nebulus vibrato: all wet (rate: proposal).
    { id: "guitar.chorus.vibrato", inst: "guitar", name: "fx.preset.chorus.vibrato",
      p: { shape: "sine", stereo: "mono", dtime: 5, offset: 0, rate: 5, depth: 0.3, fb: 0, wetlow: 0, highcut: 10000, xover: 30, mix: 1 } },
    // Modelled on the MXR M83 with X-OVER on.
    { id: "bass.chorus.anchored", inst: "bass", name: "fx.preset.chorus.anchored",
      p: { shape: "triangle", stereo: "mono", dtime: 8, offset: 0, rate: 1, depth: 0.4, fb: 0, wetlow: -2, highcut: 12000, xover: 160, mix: 0.5 } },
    // The PedalBoardManager "Fretless Ballad" seed: 3 Hz, width 4/10,
    // intensity 5/10, crossover on (its dB mapping is unverified there too).
    { id: "bass.chorus.fretless", inst: "bass", name: "fx.preset.chorus.fretless",
      p: { shape: "triangle", stereo: "wide", dtime: 7, offset: 0, rate: 3, depth: 0.45, fb: 0, wetlow: -2, highcut: 12000, xover: 160, mix: 0.5 } },
    // Modelled on the M83 flanger mode: 0.3 Hz, 50 % feedback, crossover
    // 160 Hz. The sweep stays above the browser's 128-frame loop floor.
    { id: "bass.chorus.flanger", inst: "bass", name: "fx.preset.chorus.flanger",
      p: { shape: "triangle", stereo: "mono", dtime: 6, offset: 0, rate: 0.3, depth: 0.8, fb: 0.5, wetlow: 0, highcut: 12000, xover: 160, mix: 0.5 } },
    // Voice doublers (report 4.3).
    { id: "voice.chorus.natural", inst: "voice", name: "fx.preset.chorus.natural",
      p: { shape: "drift", stereo: "wide", dtime: 12, offset: 0, rate: 0.3, depth: 0.02, fb: 0, wetlow: 0, highcut: 9000, xover: 30, mix: 0.4 } },
    { id: "voice.chorus.wide", inst: "voice", name: "fx.preset.chorus.wide",
      p: { shape: "drift", stereo: "wide", dtime: 10, offset: 5, rate: 0.2, depth: 0.03, fb: 0, wetlow: 0, highcut: 9000, xover: 30, mix: 0.45 } },
    { id: "voice.chorus.eighties", inst: "voice", name: "fx.preset.chorus.eighties",
      p: { shape: "sine", stereo: "wide", dtime: 7, offset: 0, rate: 0.6, depth: 0.2, fb: 0, wetlow: 0, highcut: 9000, xover: 30, mix: 0.25 } },
  ],
};

// ─── Phaser ───
//
// References (report 4.2, verified unless marked): MXR Phase 45 (two stages,
// "mellower") and Phase 90 (four); the modern block-logo Phase 90 relies on
// feedback, the Script version removes it (MXR Phase 95 page), so only the
// Script sound is faithful natively. Empress Phaser: 2, 3 or 4 poles,
// resonance. Boss PH-3: 4 to 12 stages (reported). Phase 90 rate range: not
// found. Built natively here: even stage counts, no feedback. The 3-stage
// and resonant (feedback) phasers need an AudioWorklet and are not done.

const PHASER = {
  type: "phaser",
  name: "fx.pedal.phaser",
  trails: false,
  law: { guitar: "pedal", bass: "pedal", voice: "pedal" },
  controls: [
    { id: "stages", kind: "select", name: "fx.ctl.stages", help: "help.fxPhaserStages",
      options: [
        { v: "2", name: "fx.opt.stages2" },
        { v: "4", name: "fx.opt.stages4" },
        { v: "6", name: "fx.opt.stages6" },
        { v: "8", name: "fx.opt.stages8" },
        { v: "12", name: "fx.opt.stages12" },
      ],
      def: "4" },
    { id: "shape", kind: "select", name: "fx.ctl.shape", help: "help.fxPhaserShape",
      options: [
        { v: "sine", name: "fx.opt.sine" },
        { v: "triangle", name: "fx.opt.triangle" },
      ],
      def: "sine" },
    { id: "sync", kind: "select", name: "fx.ctl.sync", help: "help.fxPhaserSync",
      options: [
        { v: "off", name: "fx.opt.syncOff" },
        { v: "bar", name: "fx.opt.syncBar" },
      ],
      def: "off" },
    { id: "stereo", kind: "select", name: "fx.ctl.stereo", help: "help.fxPhaserStereo",
      options: [
        { v: "mono", name: "fx.opt.mono" },
        { v: "wide", name: "fx.opt.wide" },
      ],
      def: "mono" },
    { id: "rate", kind: "knob", name: "fx.ctl.rate", help: "help.fxPhaserRate",
      min: 0.05, max: 10, unit: "rateHz", taper: "log", def: { guitar: 0.5, bass: 0.3, voice: 0.3 } },
    // Sweep width in octaves, centred on Centre.
    { id: "depth", kind: "knob", name: "fx.ctl.depth", help: "help.fxPhaserDepth",
      min: 0, max: 4, unit: "oct", taper: "lin", def: { guitar: 3, bass: 2, voice: 2 } },
    { id: "centre", kind: "knob", name: "fx.ctl.centre", help: "help.fxPhaserCentre",
      min: 100, max: 4000, unit: "Hz", taper: "log", def: { guitar: 600, bass: 400, voice: 800 } },
    { id: "xover", kind: "knob", name: "fx.ctl.crossover", help: "help.fxCrossover",
      min: 30, max: 300, unit: "Hz", taper: "log", offAtMin: true, def: { guitar: 30, bass: 120, voice: 30 } },
    // 50 % gives the deepest notches.
    { id: "mix", kind: "knob", name: "fx.ctl.mix", help: "help.fxMix",
      min: 0, max: 1, unit: "%", taper: "lin", def: { guitar: 0.5, bass: 0.5, voice: 0.35 } },
  ],
  presets: [
    // Modelled on the MXR Phase 90 Script (no feedback).
    { id: "guitar.phaser.orange", inst: "guitar", name: "fx.preset.phaser.orange",
      p: { stages: "4", shape: "sine", sync: "off", stereo: "mono", rate: 0.5, depth: 3, centre: 600, xover: 30, mix: 0.5 } },
    // Modelled on the MXR Phase 45.
    { id: "guitar.phaser.mellow", inst: "guitar", name: "fx.preset.phaser.mellow",
      p: { stages: "2", shape: "sine", sync: "off", stereo: "mono", rate: 0.7, depth: 2.5, centre: 700, xover: 30, mix: 0.5 } },
    // In place of the report's "Resonant jet", which needs feedback (a
    // worklet): eight stages and a wide stereo sweep (Boss PH-3 at 8 stages,
    // reported; values: proposal).
    { id: "guitar.phaser.wide8", inst: "guitar", name: "fx.preset.phaser.wide8",
      p: { stages: "8", shape: "triangle", sync: "off", stereo: "wide", rate: 0.3, depth: 3, centre: 800, xover: 30, mix: 0.5 } },
    // Bass (report 4.2): the same circuit with the lows kept dry.
    { id: "bass.phaser.slow", inst: "bass", name: "fx.preset.phaser.slow",
      p: { stages: "4", shape: "sine", sync: "off", stereo: "mono", rate: 0.3, depth: 2, centre: 400, xover: 120, mix: 0.5 } },
    { id: "bass.phaser.funk", inst: "bass", name: "fx.preset.phaser.funk",
      p: { stages: "2", shape: "sine", sync: "off", stereo: "mono", rate: 1.2, depth: 2, centre: 500, xover: 100, mix: 0.5 } },
    { id: "bass.phaser.deep12", inst: "bass", name: "fx.preset.phaser.deep12",
      p: { stages: "12", shape: "sine", sync: "off", stereo: "mono", rate: 0.2, depth: 2.5, centre: 500, xover: 150, mix: 0.5 } },
    // Voice (report 4.2): gentle.
    { id: "voice.phaser.swirl", inst: "voice", name: "fx.preset.phaser.swirl",
      p: { stages: "2", shape: "sine", sync: "off", stereo: "mono", rate: 0.2, depth: 2, centre: 800, xover: 30, mix: 0.25 } },
    { id: "voice.phaser.psych", inst: "voice", name: "fx.preset.phaser.psych",
      p: { stages: "4", shape: "sine", sync: "off", stereo: "wide", rate: 0.5, depth: 3, centre: 800, xover: 30, mix: 0.5 } },
    { id: "voice.phaser.bar", inst: "voice", name: "fx.preset.phaser.bar",
      p: { stages: "4", shape: "sine", sync: "bar", stereo: "mono", rate: 0.3, depth: 2.5, centre: 800, xover: 30, mix: 0.35 } },
  ],
};

// ─── Distortion (type "drive") ───
//
// References (report 4.1, verified manuals unless marked):
//   MXR Timmy CSP027: BASS cuts lows before the drive, TREBLE cuts highs
//     after; clip left asymmetric, middle symmetric, right heavy; 3 to 43 dB.
//   MXR Distortion+ M104: 10 to 45 dB at 1 kHz.
//   Empress Heavy Menace: Weight (a high-pass inside the circuit), mid sweep
//     200 Hz to 2.5 kHz, adaptive gate.
//   Bass: MXR M84 (separate dry and fuzz levels, fuzz gain range 40 dB),
//   Darkglass B7K (Blend, mids at 250/500/1k), Empress Bass Compressor colour.
//   Voice: Boss VE-500 DISTORTION and RADIO types; telephone band about 300
//   to 3400 Hz (reported).
// Verdict (report): NOT faithful to any specific circuit; a memoryless
// waveshaper misses frequency-dependent clipping, op-amp slew and bias
// shift. The UI says "modelled on" only in code. Circuit models (ADAA plus
// wave digital filters) and neural captures need an AudioWorklet: not done.

const DRIVE = {
  type: "drive",
  name: "fx.pedal.drive",
  trails: false,
  law: { guitar: "pedal", bass: "pedal", voice: "pedal" },
  controls: [
    { id: "clip", kind: "select", name: "fx.ctl.clip", help: "help.fxDriveClip",
      options: [
        { v: "softsym", name: "fx.opt.softsym" },
        { v: "softasym", name: "fx.opt.softasym" },
        { v: "hard", name: "fx.opt.hard" },
        { v: "fuzz", name: "fx.opt.fuzz" },
      ],
      def: { guitar: "softsym", bass: "hard", voice: "softsym" } },
    { id: "cab", kind: "select", name: "fx.ctl.cab", help: "help.fxDriveCab", only: ["guitar"],
      options: [
        { v: "off", name: "fx.opt.cabOff" },
        { v: "open1x12", name: "fx.opt.cab1x12" },
        { v: "2x12", name: "fx.opt.cab2x12" },
        { v: "4x12", name: "fx.opt.cab4x12" },
      ],
      def: { guitar: "2x12", bass: "off", voice: "off" } },
    // dB of gain into the clipper: spans the Timmy's 3-43 and the
    // Distortion+'s 10-45 dB (verified).
    { id: "drive", kind: "knob", name: "fx.ctl.drive", help: "help.fxDriveDrive",
      min: 0, max: 45, unit: "dB", taper: "lin", def: { guitar: 15, bass: 20, voice: 10 } },
    // Before the clipper: what clips (Timmy BASS, Heavy Weight; voice band).
    { id: "lowcut", kind: "knob", name: "fx.ctl.lowCut", help: "help.fxDriveLowCut",
      min: 20, max: 1000, unit: "Hz", taper: "log", def: { guitar: 80, bass: 30, voice: 120 } },
    { id: "midf", kind: "knob", name: "fx.ctl.midFreq", help: "help.fxDriveMidFreq", only: ["guitar", "bass"],
      min: 200, max: 2500, unit: "Hz", taper: "log", def: { guitar: 800, bass: 500, voice: 1000 } },
    { id: "midg", kind: "knob", name: "fx.ctl.midGain", help: "help.fxDriveMidGain", only: ["guitar", "bass"],
      min: -12, max: 12, unit: "dB", taper: "lin", def: 0 },
    // After the clipper (Timmy TREBLE; voice band top).
    { id: "highcut", kind: "knob", name: "fx.ctl.highCut", help: "help.fxDriveHighCut",
      min: 1000, max: 12000, unit: "Hz", taper: "log", def: { guitar: 6000, bass: 5000, voice: 8000 } },
    { id: "low", kind: "knob", name: "fx.ctl.bassLow", help: "help.fxDriveLow", only: ["bass"],
      min: -12, max: 12, unit: "dB", taper: "lin", def: 0 },
    { id: "xover", kind: "knob", name: "fx.ctl.crossover", help: "help.fxCrossover", only: ["bass"],
      min: 30, max: 300, unit: "Hz", taper: "log", offAtMin: true, def: { guitar: 30, bass: 150, voice: 30 } },
    // Clean against driven (Empress Drive clean blend, M84 dry level).
    { id: "blend", kind: "knob", name: "fx.ctl.blend", help: "help.fxDriveBlend",
      min: 0, max: 1, unit: "%", taper: "lin", def: { guitar: 1, bass: 0.5, voice: 0.3 } },
    { id: "level", kind: "knob", name: "fx.ctl.level", help: "help.fxDriveLevel",
      min: -24, max: 12, unit: "dB", taper: "lin", def: 0 },
  ],
  presets: [
    // Modelled on the MXR Timmy, middle clip position.
    { id: "guitar.drive.push", inst: "guitar", name: "fx.preset.drive.push",
      p: { clip: "softsym", cab: "2x12", drive: 12, lowcut: 120, midf: 800, midg: 0, highcut: 7000, blend: 1, level: 0 } },
    // Modelled on the MXR Distortion+.
    { id: "guitar.drive.classic", inst: "guitar", name: "fx.preset.drive.classic",
      p: { clip: "hard", cab: "4x12", drive: 35, lowcut: 60, midf: 800, midg: 0, highcut: 4500, blend: 1, level: -6 } },
    // Modelled on the Empress Heavy Menace (its gate is a separate pedal, not built).
    { id: "guitar.drive.tight", inst: "guitar", name: "fx.preset.drive.tight",
      p: { clip: "hard", cab: "4x12", drive: 42, lowcut: 180, midf: 800, midg: 3, highcut: 5000, blend: 1, level: -8 } },
    // Modelled on the MXR M84: fuzz and dry both up.
    { id: "bass.drive.fuzz", inst: "bass", name: "fx.preset.drive.fuzz",
      p: { clip: "fuzz", drive: 38, lowcut: 30, midf: 500, midg: 0, highcut: 5000, low: 0, xover: 30, blend: 0.5, level: -6 } },
    // Modelled on the Darkglass B7K.
    { id: "bass.drive.grit", inst: "bass", name: "fx.preset.drive.grit",
      p: { clip: "hard", drive: 25, lowcut: 30, midf: 500, midg: 3, highcut: 5000, low: 0, xover: 150, blend: 0.5, level: -3 } },
    // Modelled on the Empress Bass Compressor's colour circuit.
    { id: "bass.drive.warm", inst: "bass", name: "fx.preset.drive.warm",
      p: { clip: "softasym", drive: 6, lowcut: 30, midf: 500, midg: -3, highcut: 8000, low: 0, xover: 30, blend: 1, level: 0 } },
    // Modelled on the Boss VE-500 RADIO.
    { id: "voice.drive.radio", inst: "voice", name: "fx.preset.drive.radio",
      p: { clip: "softsym", drive: 18, lowcut: 300, highcut: 3400, blend: 1, level: -3 } },
    // Parallel saturation (studio practice).
    { id: "voice.drive.grit", inst: "voice", name: "fx.preset.drive.voiceGrit",
      p: { clip: "softasym", drive: 10, lowcut: 120, highcut: 8000, blend: 0.25, level: 0 } },
    { id: "voice.drive.megaphone", inst: "voice", name: "fx.preset.drive.megaphone",
      p: { clip: "hard", drive: 28, lowcut: 500, highcut: 2500, blend: 1, level: -6 } },
  ],
};

// ─── Reverb ───
//
// Best reverb per instrument (report 4.5): guitar a spring (amp tank), plate
// for leads; bass a short room with the wet high-passed; voice a plate with
// pre-delay and a de-essed tail. References (verified manuals unless
// marked): Empress Reverb (Bright Spring "Fender Twin realm", Dark Spring
// "Fender Deluxe realm", Studio and Classic Plate, L.A. Studio room "great at
// short decays"); MXR M300 (Plate, Spring, Room "small room"); spring tanks
// 33, 37, 41 ms (reported); vocal plate pre-delay 60 to 80 ms with about
// 1.2 s decay (Sound On Sound, reported). Static reverbs only: modulated,
// shimmer, gated and reverse reverbs need an AudioWorklet and are not done.

const REVERB = {
  type: "reverb",
  name: "fx.pedal.reverb",
  trails: true,
  law: { guitar: "pedal", bass: "pedal", voice: "send" },
  controls: [
    { id: "type", kind: "select", name: "fx.ctl.space", help: "help.fxReverbType",
      options: [
        { v: "room", name: "fx.opt.room" },
        { v: "hall", name: "fx.opt.hall" },
        { v: "plate", name: "fx.opt.plate" },
        { v: "spring", name: "fx.opt.spring" },
      ],
      def: { guitar: "spring", bass: "room", voice: "plate" } },
    { id: "deess", kind: "select", name: "fx.ctl.deess", help: "help.fxReverbDeess", only: ["voice"],
      options: [{ v: "off", name: "fx.off" }, { v: "on", name: "fx.on" }],
      def: { guitar: "off", bass: "off", voice: "on" } },
    // RT60, seconds.
    { id: "decay", kind: "knob", name: "fx.ctl.decay", help: "help.fxReverbDecay",
      min: 0.2, max: 10, unit: "s", taper: "log", def: { guitar: 2.5, bass: 1, voice: 1.5 } },
    { id: "predelay", kind: "knob", name: "fx.ctl.predelay", help: "help.fxReverbPredelay",
      min: 0, max: 200, unit: "ms", taper: "lin", def: { guitar: 0, bass: 10, voice: 60 } },
    { id: "highdamp", kind: "knob", name: "fx.ctl.damping", help: "help.fxReverbDamping",
      min: 1000, max: 20000, unit: "Hz", taper: "log", def: { guitar: 6000, bass: 8000, voice: 10000 } },
    // A low cut on the wet only; the lowest setting is off.
    { id: "lowcut", kind: "knob", name: "fx.ctl.lowCut", help: "help.fxReverbLowCut",
      min: 30, max: 500, unit: "Hz", taper: "log", offAtMin: true, def: { guitar: 30, bass: 200, voice: 200 } },
    { id: "drip", kind: "knob", name: "fx.ctl.drip", help: "help.fxReverbDrip",
      min: 0, max: 1, unit: "%", taper: "lin", def: { guitar: 0.5, bass: 0, voice: 0 } },
    { id: "mix", kind: "knob", name: "fx.ctl.mix", help: "help.fxMix",
      min: 0, max: 1, unit: "%", taper: "lin", def: { guitar: 0.25, bass: 0.15, voice: 0.18 } },
  ],
  presets: [
    // Modelled on the Empress Bright Spring (Fender Twin realm).
    { id: "guitar.reverb.bright", inst: "guitar", name: "fx.preset.reverb.brightSpring",
      p: { type: "spring", decay: 2.5, predelay: 0, highdamp: 6000, lowcut: 30, drip: 0.6, mix: 0.25 } },
    // Modelled on the Empress Dark Spring (Fender Deluxe realm).
    { id: "guitar.reverb.dark", inst: "guitar", name: "fx.preset.reverb.darkSpring",
      p: { type: "spring", decay: 2, predelay: 0, highdamp: 3500, lowcut: 30, drip: 0.4, mix: 0.25 } },
    // Modelled on the MXR M300 Plate.
    { id: "guitar.reverb.plate", inst: "guitar", name: "fx.preset.reverb.leadPlate",
      p: { type: "plate", decay: 2, predelay: 30, highdamp: 8000, lowcut: 30, drip: 0, mix: 0.2 } },
    // Modelled on the Empress L.A. Studio room and the M300 Room.
    { id: "bass.reverb.tight", inst: "bass", name: "fx.preset.reverb.tightRoom",
      p: { type: "room", decay: 0.8, predelay: 10, highdamp: 8000, lowcut: 250, drip: 0, mix: 0.12 } },
    { id: "bass.reverb.studio", inst: "bass", name: "fx.preset.reverb.studioRoom",
      p: { type: "room", decay: 1.2, predelay: 15, highdamp: 8000, lowcut: 200, drip: 0, mix: 0.15 } },
    { id: "bass.reverb.plate", inst: "bass", name: "fx.preset.reverb.fretlessPlate",
      p: { type: "plate", decay: 2.5, predelay: 20, highdamp: 9000, lowcut: 300, drip: 0, mix: 0.2 } },
    // Voice, send law (Sound On Sound and UAD, reported).
    { id: "voice.reverb.plate", inst: "voice", name: "fx.preset.reverb.vocalPlate",
      p: { type: "plate", deess: "on", decay: 1.2, predelay: 70, highdamp: 10000, lowcut: 200, drip: 0, mix: 0.18 } },
    { id: "voice.reverb.ballad", inst: "voice", name: "fx.preset.reverb.balladPlate",
      p: { type: "plate", deess: "on", decay: 2.5, predelay: 40, highdamp: 10000, lowcut: 30, drip: 0, mix: 0.2 } },
    { id: "voice.reverb.room", inst: "voice", name: "fx.preset.reverb.closeRoom",
      p: { type: "room", deess: "off", decay: 0.6, predelay: 0, highdamp: 10000, lowcut: 30, drip: 0, mix: 0.1 } },
  ],
};

/** Every pedal type that exists, by type id. */
export const PEDALS = Object.freeze({ delay: DELAY, chorus: CHORUS, phaser: PHASER, drive: DRIVE, reverb: REVERB });

export function pedalSpec(type) {
  return Object.prototype.hasOwnProperty.call(PEDALS, type) ? PEDALS[type] : null;
}

/** The controls a pedal type shows for an instrument (some are voice only). */
export function controlsFor(type, inst) {
  const spec = pedalSpec(type);
  if (!spec) return [];
  return spec.controls.filter((c) => !c.only || c.only.includes(inst));
}

/** A control's default for an instrument. */
export function controlDefault(control, inst) {
  const d = control.def;
  if (d && typeof d === "object") return d[inst] ?? d.guitar;
  return d;
}

/** The default settings of a pedal type for an instrument. */
export function pedalDefaults(type, inst) {
  const out = {};
  for (const c of pedalSpec(type)?.controls || []) out[c.id] = controlDefault(c, inst);
  return out;
}

/** The built-in presets of a type for an instrument. */
export function presetsFor(type, inst) {
  return (pedalSpec(type)?.presets || []).filter((p) => p.inst === inst);
}

export function presetById(id) {
  for (const spec of Object.values(PEDALS)) {
    const p = spec.presets.find((x) => x.id === id);
    if (p) return { ...p, type: spec.type };
  }
  return null;
}

/** The pedal types an instrument can add, in its default order. */
export function typesFor(inst) {
  const order = DEFAULT_ORDER[inst] || DEFAULT_ORDER.guitar;
  return order.filter((t) => pedalSpec(t));
}

/** Clamp one value to its control, or the default when it is not usable. */
export function clampControl(control, value, inst) {
  if (control.kind === "select") {
    return control.options.some((o) => o.v === value) ? value : controlDefault(control, inst);
  }
  const n = Number(value);
  if (!Number.isFinite(n)) return controlDefault(control, inst);
  return Math.max(control.min, Math.min(control.max, n));
}
