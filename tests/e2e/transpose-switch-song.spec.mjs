// A song's key, and the rest of its saved state, after switching to another
// song and back (#781).
//
// Three things have to agree once a song is open again: the number on the
// transport's key counter (and on each lane's key stepper), what the audio
// graph is actually doing, and what the song's saved record holds, which is
// what the next reload will show. 0.20 put the lanes back at -2 from the saved
// record but always showed 0 on the counter, so the song still played two
// semitones down under a counter claiming the original key.
//
// What the audio is doing is read from the graph itself, not only from a value
// the engine reports about itself. A lane's transpose is a connection: lane
// audio is wired into the worklet input for its shift (input 6 + n,
// pitchBus.js), and a pitch input is wired into the worklet only while a lane
// sits on it. So the recorder below keeps, for the newest worklet, which input
// every lane is on right now and which pitch inputs are wired. Lanes at -2 are
// on input 4, and input 4 is the only pitch input wired: that is a track
// playing two semitones down, with no pitch chain running for a key nothing is
// in.

import { test, expect } from "@playwright/test";
import { JOB_ID, SIBLING_JOB_ID, openStudio, chooseMode, stubTauri } from "./helpers.mjs";

const ZERO_INPUT = 6;

const down = (page) => page.locator("#t-pitch-down");
const up = (page) => page.locator("#t-pitch-up");
const laneDown = (page, stem) => page.locator(`.lane-key[data-stem="${stem}"] .lane-key-step.down`);
// Karaoke's own Key panel: the same global key, through setGlobalPitch.
const kkDown = (page) => page.locator("#karaoke .kk-key .kk-btn.kk-sq").first();
const kkUp = (page) => page.locator("#karaoke .kk-key .kk-btn.kk-sq").nth(1);
const kkBadge = (page) => page.locator("#karaoke .kk-key .kk-badge");
// The badge signs with a true minus sign (karaokeModel.semitoneLabel).
const KK_MINUS_TWO = `${String.fromCharCode(0x2212)}2`;
const ev = (page, fn, arg) => page.evaluate(fn, arg);

/** Watch the audio graph: lanes onto pitch inputs, and pitch inputs onto the worklet. */
async function recordGraph(page) {
  await page.addInitScript(() => {
    const g = { contextsOpen: 0, wired: new Set(), lanes: new Map() };
    window.__graph = g;
    let worklet = null;
    const busInput = new Map();

    const OrigCtx = window.AudioContext;
    if (OrigCtx) {
      window.AudioContext = class extends OrigCtx {
        constructor(...args) {
          super(...args);
          g.contextsOpen += 1;
          let closed = false;
          const close = this.close.bind(this);
          this.close = () => {
            if (!closed) { closed = true; g.contextsOpen -= 1; }
            return close();
          };
        }
      };
    }
    const OrigNode = window.AudioWorkletNode;
    if (OrigNode) {
      window.AudioWorkletNode = class extends OrigNode {
        constructor(...args) {
          super(...args);
          if (args[1] === "soundtouch-processor") {
            // A new engine: what the previous one was wired to went with it.
            worklet = this;
            g.wired = new Set();
            g.lanes = new Map();
            busInput.clear();
          }
        }
      };
    }
    const connect = AudioNode.prototype.connect;
    AudioNode.prototype.connect = function (dest, output, input) {
      const r = connect.apply(this, arguments);
      if (worklet && dest === worklet) {
        busInput.set(this, input || 0);
        g.wired.add(input || 0);
      } else if (busInput.has(dest)) {
        g.lanes.set(this, busInput.get(dest));
      }
      return r;
    };
    const disconnect = AudioNode.prototype.disconnect;
    AudioNode.prototype.disconnect = function (dest, output, input) {
      const r = disconnect.apply(this, arguments);
      if (worklet && dest === worklet && busInput.has(this)) g.wired.delete(input ?? busInput.get(this));
      else if (dest === undefined) g.lanes.delete(this);
      return r;
    };
  });
}

const uniq = (xs) => [...new Set(xs)].sort((a, b) => (a > b ? 1 : a < b ? -1 : 0));

/**
 * Everything that has to agree about the open song's key, in one read: for
 * each lane the song has, the key the engine routes it at, the number on its
 * stepper and the key in the saved record; the counter; and what the graph is
 * wired to. Reduced to the distinct values over the pitched lanes, so "every
 * pitched lane at -2" reads as [-2].
 */
async function summary(page) {
  const raw = await ev(page, async () => {
    const { audioEngine, currentJobId, playbackSpeed, mixerState, trackIndex } = await import("/js/state.js");
    const { getMixMode } = await import("/js/mixer.js");
    const g = window.__graph;
    const rec = JSON.parse(localStorage.getItem(`stemdeck:mix:${currentJobId}:${getMixMode()}`) || "null");
    const lanes = Object.keys(trackIndex || {}).map((n) => ({
      name: n,
      pitchable: audioEngine?.isStemPitchable?.(n) === true,
      engine: audioEngine?.getStemPitch?.(n),
      dom: document.querySelector(`.lane-key[data-stem="${n}"] .lane-key-num`)?.textContent ?? null,
      saved: rec?.[n]?.pitch ?? null,
    }));
    return {
      counter: document.querySelector("#t-pitch-value")?.textContent,
      lanes,
      inputs: [...new Set(g.lanes.values())],
      wiredPitchInputs: [...g.wired].filter((k) => k !== 6),
      saved: rec?.__global?.pitch ?? null,
      speed: playbackSpeed,
      bassMuted: mixerState.bass?.muted,
    };
  });
  const pitched = raw.lanes.filter((l) => l.pitchable);
  return {
    counter: raw.counter,
    engine: uniq(pitched.map((l) => l.engine)),
    steppers: uniq(pitched.filter((l) => l.dom !== null).map((l) => l.dom)),
    savedLanes: uniq(pitched.map((l) => l.saved)),
    byLane: Object.fromEntries(raw.lanes.map((l) => [l.name, l.engine])),
    inputs: uniq(raw.inputs),
    wiredPitchInputs: uniq(raw.wiredPitchInputs),
    saved: raw.saved,
    speed: raw.speed,
    bassMuted: raw.bassMuted,
  };
}

/** The song is open, its engine is up and the key control is live. */
async function settled(page, id) {
  await expect.poll(() => ev(page, async () => (await import("/js/state.js")).currentJobId)).toBe(id);
  await page.waitForFunction(
    () => !/\/\s*00:00\s*$/.test(document.querySelector("#t-time")?.textContent || "00:00 / 00:00"),
    null, { timeout: 20000 },
  );
  await expect.poll(() => ev(page, async () => (await import("/js/transport.js")).getGlobalPitch().available)).toBe(true);
  // Lanes are routed as stems land; wait for the graph to stop moving.
  let prev = "";
  await expect.poll(async () => {
    const s = JSON.stringify(await ev(page, () => [[...window.__graph.lanes.values()], [...window.__graph.wired]]));
    const same = s === prev;
    prev = s;
    return same;
  }, { timeout: 20000, intervals: [250] }).toBe(true);
}

const openSong = async (page, id) => {
  await page.locator(`.cat-item[data-id="${id}"]`).first().click();
  await settled(page, id);
};

/**
 * The open song is in key `n` everywhere: the counter, every pitched lane's
 * stepper, the routing (pitched lanes on input 6 + n, drums on the unpitched
 * input, and input 6 + n the only pitch input wired), and the saved record.
 */
async function expectGlobalKey(page, n) {
  const label = n > 0 ? `+${n}` : String(n);
  await expect.poll(() => summary(page)).toMatchObject({
    counter: label,
    engine: [n],
    steppers: [label],
    inputs: uniq([ZERO_INPUT + n, ZERO_INPUT]),
    wiredPitchInputs: n === 0 ? [] : [ZERO_INPUT + n],
    // A song never moved has no record yet; one that was has both.
    ...(n === 0 ? {} : { saved: n, savedLanes: [n] }),
  });
}

// The modes whose screen has a key control. Performance (streamer) has none:
// its view covers the transport and offers no key of its own. Karaoke covers
// the transport too, and has its own Key panel.
const MODES = {
  practice: { base: "practice" },
  daw: { base: "daw" },
  karaoke: { base: "daw", view: "karaoke" },
};

async function startIn(page, mode) {
  await page.addInitScript((m) => {
    if (!sessionStorage.getItem("mode-set")) {
      sessionStorage.setItem("mode-set", "1");
      localStorage.setItem("stemdeck.mode", m);
    }
  }, MODES[mode].base);
  await recordGraph(page);
  await openStudio(page);
  await settled(page, JOB_ID);
  await enterView(page, mode);
}

async function enterView(page, mode) {
  const view = MODES[mode].view;
  if (!view) return;
  await chooseMode(page, view);
  await expect(page.locator(`#${view}`)).toBeVisible();
  await expect(page.locator(`#modeMenu .mode-item[data-mode="${view}"]`)).toHaveAttribute("aria-checked", "true");
  await settled(page, JOB_ID);
}

/** Step the global key with whichever control the mode shows. */
async function stepKey(page, mode, delta) {
  const karaoke = MODES[mode].view === "karaoke";
  const btn = delta < 0 ? (karaoke ? kkDown(page) : down(page)) : (karaoke ? kkUp(page) : up(page));
  for (let i = 0; i < Math.abs(delta); i++) await btn.click();
}

test.describe("a song's key after switching songs (#781)", () => {
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize({ width: 1800, height: 1020 });
  });

  for (const mode of Object.keys(MODES)) {
    test(`${mode}: the global key agrees on screen, in the audio and in the save, after switching back and after a reload`, async ({ page }) => {
      await startIn(page, mode);
      await stepKey(page, mode, -2);
      await expectGlobalKey(page, -2);
      if (mode === "karaoke") await expect(kkBadge(page)).toHaveText(KK_MINUS_TWO);

      // The other song, left in a different key.
      await openSong(page, SIBLING_JOB_ID);
      await expectGlobalKey(page, 0);
      await stepKey(page, mode, 1);
      await expectGlobalKey(page, 1);

      // Back to the first: the reporter's steps.
      await openSong(page, JOB_ID);
      await expectGlobalKey(page, -2);
      if (mode === "karaoke") await expect(kkBadge(page)).toHaveText(KK_MINUS_TWO);

      // And after a full reload.
      await page.reload({ waitUntil: "domcontentloaded" });
      await openSong(page, JOB_ID);
      await enterView(page, mode);
      await expectGlobalKey(page, -2);
    });
  }

  for (const mode of ["practice", "daw"]) {
    test(`${mode}: one lane's own key comes back with the song`, async ({ page }) => {
      await startIn(page, mode);
      await laneDown(page, "vocals").click();
      await laneDown(page, "vocals").click();
      const vocalsOnly = {
        counter: "0",
        byLane: { vocals: -2, bass: 0, other: 0, drums: 0 },
        inputs: [ZERO_INPUT - 2, ZERO_INPUT],
        wiredPitchInputs: [ZERO_INPUT - 2],
      };
      await expect.poll(() => summary(page)).toMatchObject(vocalsOnly);

      await openSong(page, SIBLING_JOB_ID);
      await expectGlobalKey(page, 0);

      await openSong(page, JOB_ID);
      await expect(page.locator('.lane-key[data-stem="vocals"] .lane-key-num')).toHaveText("-2");
      await expect.poll(() => summary(page)).toMatchObject(vocalsOnly);

      await page.reload({ waitUntil: "domcontentloaded" });
      await openSong(page, JOB_ID);
      await expect.poll(() => summary(page)).toMatchObject(vocalsOnly);
    });
  }

  test("a song saved by 0.20 opens at the key its lanes play in", async ({ page }) => {
    // 0.20 saved one record per song (no mode) holding each lane's absolute
    // key, and never the counter. That is the reporter's saved state after
    // the upgrade: opening it must show the key the lanes play in, not 0 over
    // a track playing at -2.
    await page.addInitScript((id) => {
      if (sessionStorage.getItem("seeded-020")) return;
      sessionStorage.setItem("seeded-020", "1");
      localStorage.setItem("stemdeck.mode", "practice");
      const lane = (pitch) => ({ volume: 1, muted: false, soloed: false, pitch });
      localStorage.setItem(`stemdeck:mix:${id}`, JSON.stringify({
        vocals: lane(-2), bass: lane(-2), other: lane(-2), guitar: lane(-2), piano: lane(-2), drums: lane(0),
      }));
    }, JOB_ID);
    await recordGraph(page);
    await openStudio(page);
    await settled(page, JOB_ID);
    await expect.poll(() => summary(page)).toMatchObject({
      counter: "-2", engine: [-2], steppers: ["-2"],
      inputs: [ZERO_INPUT - 2, ZERO_INPUT], wiredPitchInputs: [ZERO_INPUT - 2],
    });
    // Stepping from there moves to -1, every lane with it, and saves both.
    await up(page).click();
    await expectGlobalKey(page, -1);
  });

  test("reopening a song at once does not lose the key it was just given", async ({ page }) => {
    // The save is debounced. Opening the song again inside that window read
    // the record from before the change, and the late write then put the key
    // back in the store, behind a counter and an engine at 0.
    await recordGraph(page);
    await openStudio(page);
    await settled(page, JOB_ID);
    await down(page).click();
    await down(page).click();
    await page.locator(`.cat-item[data-id="${SIBLING_JOB_ID}"]`).first().click();
    await openSong(page, JOB_ID);
    await expectGlobalKey(page, -2);
  });

  test("a saved record that arrives after the engine is up still sets the key", async ({ page }) => {
    // Desktop reads the record over IPC. However that is timed against the
    // engine coming up, the engine and the counter end on the record's key.
    await stubTauri(page);
    await page.addInitScript((id) => {
      const lane = (pitch) => ({ volume: 1, muted: false, soloed: false, pitch, fx: [] });
      localStorage.setItem(`stemdeck:mix:${id}:daw`, JSON.stringify({
        vocals: lane(-2), bass: lane(-2), other: lane(-2), guitar: lane(-2), piano: lane(-2), drums: lane(0),
        __global: { pitch: -2, speed: 1 },
      }));
      const invoke = window.__TAURI__.core.invoke;
      window.__TAURI__.core.invoke = (cmd, args) => {
        if (cmd === "store_get" && String(args?.key).startsWith("stemdeck:mix:")) {
          return new Promise((r) => setTimeout(r, 2500)).then(() => invoke(cmd, args));
        }
        return invoke(cmd, args);
      };
    }, JOB_ID);
    await recordGraph(page);
    await openStudio(page);
    await settled(page, JOB_ID);
    await expectGlobalKey(page, -2);
  });

  test("speed and mutes come back with the song too", async ({ page }) => {
    await recordGraph(page);
    await openStudio(page);
    await settled(page, JOB_ID);
    await page.locator("#t-speed-075").click();
    await page.locator('.lane-header[data-stem="bass"] .lane-icon-toggle').click();
    await expect.poll(() => summary(page)).toMatchObject({ speed: 0.75, bassMuted: true });

    await openSong(page, SIBLING_JOB_ID);
    await expect.poll(() => summary(page)).toMatchObject({ speed: 1, bassMuted: false });

    await openSong(page, JOB_ID);
    await expect.poll(() => summary(page)).toMatchObject({ speed: 0.75, bassMuted: true });
    await expect(page.locator("#t-speed-075")).toHaveClass(/active/);
  });

  test("switching back and forth keeps one engine and no pitch chain for a key nothing is in", async ({ page }) => {
    await recordGraph(page);
    await openStudio(page);
    await settled(page, JOB_ID);
    await down(page).click();
    await down(page).click();
    await expectGlobalKey(page, -2);
    const before = await ev(page, () => window.__graph.contextsOpen);
    for (let i = 0; i < 3; i++) {
      await openSong(page, SIBLING_JOB_ID);
      await openSong(page, JOB_ID);
    }
    await expectGlobalKey(page, -2);
    expect(await ev(page, () => window.__graph.contextsOpen)).toBe(before);
  });
});
