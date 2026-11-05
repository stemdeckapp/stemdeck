// Named starting points for the EQ panels. Plain data: node tests read them.
// Names are i18n keys. Graph presets are bands; pedal and keys presets are
// the panel's own control values (eqPedalMath.js turns them into bands).

const bell = (f, g, q = 1) => ({ on: true, type: "bell", f, g, q, slope: 12 });
const shelf = (type, f, g) => ({ on: true, type, f, g, q: 0.707, slope: 12 });
const cut = (type, f, slope = 24) => ({ on: true, type, f, g: 0, q: 0.707, slope });

const GRAPH = [
  { key: "eq.preset.cleanLows", bands: [cut("lowcut", 80)] },
  { key: "eq.preset.lessMud", bands: [bell(300, -3, 1.2)] },
  { key: "eq.preset.presence", bands: [bell(3000, 2.5, 0.9)] },
  { key: "eq.preset.addAir", bands: [shelf("highshelf", 10000, 3)] },
];

const DRUM = [
  { key: "eq.preset.drumsTighten", bands: [cut("lowcut", 35), bell(400, -4, 1.4), bell(5000, 2, 0.8)] },
  { key: "eq.preset.drumsPunch", bands: [bell(70, 3, 1.1), bell(350, -2, 1.2), bell(4000, 2.5, 1)] },
  { key: "eq.preset.drumsCymbals", bands: [bell(3500, -1.5, 1.5), shelf("highshelf", 8000, -4)] },
];

/** The presets of a graphical panel ("drum" or "graph"). */
export function graphPresets(kind) {
  return kind === "drum" ? DRUM : GRAPH;
}

// Pedal and keys presets: control values over the panel's starting point.
const BASS = [
  { key: "eq.preset.bassCutMud", c: { mf: 300, mg: -5, mq: 2.5 } },
  { key: "eq.preset.bassGrowl", c: { mf: 800, mg: 4, mq: 1.8, hf: 2500, hg: 3, hq: 1.5 } },
  { key: "eq.preset.bassRound", c: { lf: 80, lg: 3, lq: 1, lpf: 2500, hs: -3 } },
  { key: "eq.preset.bassSlap", c: { hpf: 30, lf: 60, lg: 4, lq: 1.5, mf: 500, mg: -5, mq: 1.5, hf: 3500, hg: 4, hq: 1.5 } },
];

const GUITAR = [
  { key: "eq.preset.guitarPresence", c: { mf: 2500, mg: 4, mq: 1 } },
  { key: "eq.preset.guitarMix", c: { hpf: 100, mf: 1200, mg: 3, mq: 1.5, hf: 7000, hg: -3, hq: 2.5 } },
  { key: "eq.preset.guitarWarm", c: { lf: 150, lg: 3, lq: 1, hs: -2 } },
  { key: "eq.preset.guitarFizz", c: { lpf: 6000, hf: 4000, hg: -3, hq: 2.5 } },
];

const KEYS = [
  { key: "eq.preset.keysBright", c: { presence: 3, air: 3 } },
  { key: "eq.preset.keysWarm", c: { warmth: 3, tone: -2 } },
  { key: "eq.preset.keysThin", c: { lowcut: 120, body: -3 } },
];

/** The presets of a pedal or keys panel ("bass", "guitar", "keys"). */
export function controlPresets(kind) {
  return kind === "bass" ? BASS : kind === "guitar" ? GUITAR : kind === "keys" ? KEYS : [];
}
