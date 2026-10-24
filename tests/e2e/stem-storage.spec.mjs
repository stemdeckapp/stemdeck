// Stem storage format (#783). Settings > Library chooses how new songs' stems
// are kept (MP3 at 128 kbps unless WAV is chosen), and a song stored as MP3
// plays, mixes, clicks and downloads like one stored as WAV.
//
// The MP3 song is seed.py's e2e0f00dface: the fixture track's stems encoded
// the way the storage stage encodes them, its WAVs gone. Its vocals are the
// same samples as the fixture track's vocals.wav, which is what makes the
// alignment test below possible: Chromium's decodeAudioData of the MP3 must
// give that WAV back with the same length and on the same sample.
//
// The setting is put back after every test: the backend is shared by the
// whole suite.

import { test, expect } from "@playwright/test";
import {
  JOB_ID,
  SIBLING_JOB_ID,
  TRACK_TITLE,
  SIBLING_TITLE,
  fixtureTrack,
  seedCatalogState,
  stubExportEndpoints,
  stubUpdateCheck,
  waitForClickTrack,
  chooseMode,
} from "./helpers.mjs";

const MP3_JOB_ID = "e2e0f00dface";
const MP3_TITLE = "E2E MP3 Track";

test.afterEach(async ({ request }) => {
  await request.post("/api/settings", { data: { stem_format: "mp3", stem_mp3_kbps: 128 } });
});

/** The library store with the MP3 song in it as well. */
async function seedWithMp3(page) {
  await seedCatalogState(page, {
    folders: [
      { id: "f-unsorted", name: "Unsorted", items: [JOB_ID, SIBLING_JOB_ID, MP3_JOB_ID], color: null },
      { id: "trash", name: "Trash", items: [], color: null },
    ],
    tracks: {
      [JOB_ID]: fixtureTrack(JOB_ID, TRACK_TITLE),
      [SIBLING_JOB_ID]: fixtureTrack(SIBLING_JOB_ID, SIBLING_TITLE),
      [MP3_JOB_ID]: { ...fixtureTrack(MP3_JOB_ID, MP3_TITLE), sourceUrl: "local:e2e-mp3-fixture" },
    },
  });
}

async function openSettingsLibrary(page) {
  await seedWithMp3(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator("#settingsBtn").click();
  await expect(page.locator(".library-editor")).toBeVisible();
  await page.locator('.settings-tab[data-tab="library"]').click();
}

async function openMp3Song(page) {
  await seedWithMp3(page);
  await stubExportEndpoints(page);
  await stubUpdateCheck(page);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.locator(`.cat-item[data-id="${MP3_JOB_ID}"]`).first().click();
  await page.waitForFunction(
    () => !/\/\s*00:00\s*$/.test(document.querySelector("#t-time")?.textContent || "00:00 / 00:00"),
    null,
    { timeout: 20000 },
  );
}

const now = (page) => page.evaluate(async () => (await import("/js/transport.js")).transport().getCurrentTime());

async function playsFor(page, seconds = 0.6) {
  const before = await now(page);
  await page.locator("#t-play").click();
  await page.waitForTimeout(seconds * 1000);
  const after = await now(page);
  await page.locator("#t-play").click();
  return after - before;
}

// ── The setting ──

test("a fresh profile shows MP3 at 128 kbps, the bitrate visible", async ({ page }) => {
  await openSettingsLibrary(page);
  await expect(page.locator(".set-stem-format")).toHaveValue("mp3");
  await expect(page.locator(".stem-bitrate-row")).toBeVisible();
  await expect(page.locator(".set-stem-kbps")).toHaveValue("128");
  await expect(page.locator(".set-stem-kbps option")).toHaveText(["128 kbps", "192 kbps", "256 kbps"]);
});

test("WAV hides the bitrate, and both choices survive a reload without a restart", async ({ page, request }) => {
  await openSettingsLibrary(page);
  await page.locator(".set-stem-format").selectOption("wav");
  await expect(page.locator(".stem-bitrate-row")).toBeHidden();
  // Live on the server at once: the next song that finishes reads it.
  await expect.poll(async () => (await (await request.get("/api/settings")).json()).stem_format).toBe("wav");

  await page.reload({ waitUntil: "domcontentloaded" });
  await page.locator("#settingsBtn").click();
  await page.locator('.settings-tab[data-tab="library"]').click();
  await expect(page.locator(".set-stem-format")).toHaveValue("wav");
  await expect(page.locator(".stem-bitrate-row")).toBeHidden();

  await page.locator(".set-stem-format").selectOption("mp3");
  await expect(page.locator(".stem-bitrate-row")).toBeVisible();
  await page.locator(".set-stem-kbps").selectOption("256");
  await expect
    .poll(async () => {
      const d = await (await request.get("/api/settings")).json();
      return `${d.stem_format}:${d.stem_mp3_kbps}`;
    })
    .toBe("mp3:256");
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.locator("#settingsBtn").click();
  await page.locator('.settings-tab[data-tab="library"]').click();
  await expect(page.locator(".set-stem-kbps")).toHaveValue("256");
});

test("settings search finds the storage format and, with MP3, the bitrate", async ({ page }) => {
  await openSettingsLibrary(page);
  const search = page.locator(".settings-search-input");
  await search.fill("storage format");
  await expect(page.locator(".settings-results-list li")).toContainText(["Stem storage format"]);
  await search.fill("bitrate");
  await expect(page.locator(".settings-results-list li")).toContainText(["MP3 bitrate"]);
  await page.locator(".settings-results-list li").first().click();
  await expect(page.locator(".stem-bitrate-row")).toBeVisible();
});

// ── An MP3 song ──

test("Chromium decodes an MP3 stem to the WAV's samples: same length, lag 0", async ({ page }) => {
  await page.goto("/", { waitUntil: "domcontentloaded" });
  const result = await page.evaluate(async ([wavUrl, mp3Url]) => {
    const decode = async (url) => {
      const bytes = await (await fetch(url)).arrayBuffer();
      const ctx = new OfflineAudioContext(2, 1, 44100);
      return ctx.decodeAudioData(bytes);
    };
    const [wav, mp3] = await Promise.all([decode(wavUrl), decode(mp3Url)]);
    const a = wav.getChannelData(0);
    const b = mp3.getChannelData(0);
    // Mean squared difference at each lag over the first two seconds: the
    // stem is a steady tone, so its correlation repeats every period, but the
    // difference is only near zero where the samples really line up.
    const from = 4096;
    const to = Math.min(a.length, b.length, 2 * 44100) - 4096;
    const msd = (lag) => {
      let s = 0;
      for (let i = from; i < to; i++) {
        const d = a[i] - b[i + lag];
        s += d * d;
      }
      return s / (to - from);
    };
    let best = 0;
    let bestV = Infinity;
    for (let lag = -1500; lag <= 1500; lag++) {
      const v = msd(lag);
      if (v < bestV) { bestV = v; best = lag; }
    }
    return {
      wavLength: wav.length,
      mp3Length: mp3.length,
      rate: mp3.sampleRate,
      lag: best,
      atZero: msd(0),
      at576: msd(576),
      at1105: msd(1105),
    };
  }, [`/api/jobs/${JOB_ID}/stems/vocals.wav`, `/api/jobs/${MP3_JOB_ID}/stems/vocals.mp3`]);
  console.log("[stem-storage] chromium decode:", JSON.stringify(result));
  expect(result.rate).toBe(44100);
  expect(result.mp3Length).toBe(result.wavLength);
  expect(result.lag).toBe(0);
  // Coding noise, not a misplaced signal: orders of magnitude below a shift.
  expect(result.atZero).toBeLessThan(result.at576 / 100);
  expect(result.atZero).toBeLessThan(result.at1105 / 100);
});

test("an MP3 song opens on the full-decode engine and plays", async ({ page }) => {
  await openMp3Song(page);
  // supportsCountIn is the full-decode engine's: the chunked one cannot read MP3.
  const engine = await page.evaluate(async () => {
    const { audioEngine } = await import("/js/state.js");
    return { up: Boolean(audioEngine), fullDecode: audioEngine?.supportsCountIn === true };
  });
  expect(engine).toEqual({ up: true, fullDecode: true });
  for (const name of ["vocals", "drums", "bass", "other"]) {
    await expect(page.locator(`.lane-header[data-stem="${name}"]`).first()).toBeVisible();
  }
  expect(await playsFor(page)).toBeGreaterThan(0.2);
  // No playback error over the song.
  await expect(page.locator('[data-kind="playback"]:visible')).toHaveCount(0);
});

test("an MP3 song's lane download is named .mp3 and serves audio/mpeg", async ({ page, request }) => {
  await openMp3Song(page);
  const dl = page.locator('.lane-header[data-stem="vocals"] .lane-dl');
  await expect(dl).toHaveAttribute("download", /_vocals\.mp3$/);
  const href = await dl.getAttribute("href");
  const res = await request.get(href);
  expect(res.status()).toBe(200);
  expect(res.headers()["content-type"]).toBe("audio/mpeg");
});

test("the click track runs on an MP3 song", async ({ page }) => {
  await openMp3Song(page);
  await waitForClickTrack(page);
  await expect(page.locator("#t-metro")).toBeEnabled();
});

test("an MP3 song exports a mix, a stem and a zip", async ({ request }) => {
  const mix = await request.get(`/api/jobs/${MP3_JOB_ID}/mixdown.wav?stems=vocals,drums&gains=1,1`);
  expect(mix.status()).toBe(200);
  expect(mix.headers()["content-type"]).toContain("audio/wav");
  const region = await request.get(`/api/jobs/${MP3_JOB_ID}/stems/vocals.wav?start=1&end=2`);
  expect(region.status()).toBe(200);
  const zip = await request.get(`/api/jobs/${MP3_JOB_ID}/stems/all.zip?format=mp3`);
  expect(zip.status()).toBe(200);
  expect(zip.headers()["content-type"]).toBe("application/zip");
});

/** Start playback from `at` and wait for the clock to pass a quarter second
 * beyond it. Entering a mode rebuilds the engine, so a press that lands on the
 * one being replaced is pressed again until the transport reports playing. */
async function startsPlaying(page, at = 1) {
  await page.evaluate(async (t0) => (await import("/js/transport.js")).setPlayheadTime(t0), at);
  await expect
    .poll(
      () =>
        page.evaluate(async () => {
          const t = await import("/js/transport.js");
          if (!t.transport().isPlaying()) t.togglePlayPause();
          return t.transport().getCurrentTime();
        }),
      { timeout: 15000 },
    )
    .toBeGreaterThan(at + 0.25);
  await page.evaluate(async () => {
    const t = await import("/js/transport.js");
    if (t.transport().isPlaying()) t.togglePlayPause();
  });
}

test("Karaoke and Performance play an MP3 song", async ({ page }) => {
  await openMp3Song(page);
  await chooseMode(page, "karaoke");
  await expect(page.locator("#karaoke")).toBeVisible();
  await startsPlaying(page);

  await chooseMode(page, "streamer");
  await expect(page.locator("#streamer")).toBeVisible();
  await startsPlaying(page);
});
