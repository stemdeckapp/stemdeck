// What the checks do in the app, written once: import through the top bar,
// wait for the queue, open a track, open its boxes, and keep the evidence
// (a screenshot per check, and what the outside services answered).
//
// Everything here goes through the app's own UI, the way the manual run does.
// The API is only read, to know when a job has finished and what it found,
// so that a wait never has to be a guess.

import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { expect } from "@playwright/test";
import {
  DATA_DIR,
  FILES_DIR,
  LOGS_DIR,
  SHOTS_DIR,
  readState,
  resizeWindow,
  writeState,
} from "./harness.mjs";

// ─── What gets imported ─────────────────────────────────────────────────────
//
// Queued together once, in this order, so separation runs while earlier checks
// are being looked at. The hour-long compilation goes last so nothing waits
// behind it.

export const SONGS = {
  wicked: { url: "https://www.youtube.com/watch?v=CnIjnMDY5-c", label: "Wicked, Dancing Through Life" },
  showman: { url: "https://www.youtube.com/watch?v=wEJd2RyGm8Q", label: "This Is Me" },
  phantom: { url: "https://www.youtube.com/watch?v=ipsZhMjCc-A", label: "The Phantom of the Opera" },
  billie: { url: "https://www.youtube.com/watch?v=Zi_XLOBDo_Y", label: "Billie Jean" },
  jay: { url: "https://www.youtube.com/watch?v=DYptgVvkVLQ", label: "Jay Chou, Qing Tian" },
  yoasobi: { url: "https://www.youtube.com/watch?v=Y4nEEZwckuU", label: "YOASOBI, Gunjou" },
  iu: { url: "https://www.youtube.com/watch?v=jeqdYqsrsA0", label: "IU, Good Day" },
  polish: { url: "https://www.youtube.com/watch?v=cKKWT4zR5aY", label: "Natalia Kukulska, W biegu" },
  // The video sings 16 s after LRCLIB's copies start their first line (0.05 s).
  basketCase: { url: "https://www.youtube.com/watch?v=NUTGr5t3MoY", label: "Green Day, Basket Case" },
  localMp3: { file: "acceptance-tone.mp3", label: "local MP3 with tags" },
  untagged: { file: "recording.wav", label: "local WAV with no tags" },
  teresa: { url: "https://www.youtube.com/watch?v=xofwwFiLtuk", label: "Teresa Teng compilation (59 min)", long: true },
};

export const SKIP_LONG = process.env.STEMDECK_ACCEPTANCE_SKIP_LONG === "1";

// The only place the real AcoustID application key is read. It is typed into
// the masked field and nowhere else: never logged, never in a file, never in
// an assertion message (see A2).
export const ACOUSTID_KEY = (process.env.STEMDECK_ACCEPTANCE_ACOUSTID_KEY || "").trim();

// How long a job may take, from being queued. Separation on the GPU is a
// minute or two a song, but a job waits for every one queued before it.
const JOB_TIMEOUT_MS = Number(process.env.STEMDECK_ACCEPTANCE_JOB_TIMEOUT_MIN || 90) * 60_000;

// ─── The window ─────────────────────────────────────────────────────────────

/**
 * The window size the checks run at: wide enough for the now-playing card,
 * which the top bar hides below 1460px. Outer size; the page is 16px narrower
 * and 39px shorter.
 */
export async function wideWindow(page, width = 1700, height = 1000) {
  if (!resizeWindow(width, height)) await page.setViewportSize({ width: width - 16, height: height - 39 });
  await expect.poll(() => page.evaluate(() => innerWidth), { timeout: 5000 }).toBeGreaterThan(width - 60);
}

export async function setWindowWidth(page, width, height = 1000) {
  if (!resizeWindow(width, height)) await page.setViewportSize({ width: width - 16, height: height - 39 });
  await page.waitForTimeout(600);
}

// ─── Evidence ───────────────────────────────────────────────────────────────

/** A screenshot of the app, kept in the results folder and named on the check. */
export async function shot(page, testInfo, name, { locator = null } = {}) {
  fs.mkdirSync(SHOTS_DIR, { recursive: true });
  const id = testInfo.title.split(" ")[0];
  const file = path.join(SHOTS_DIR, `${id}-${name}.png`);
  try {
    if (locator) await locator.screenshot({ path: file, timeout: 10_000 });
    else await page.screenshot({ path: file, timeout: 15_000 });
    testInfo.annotations.push({ type: "screenshot", description: file });
  } catch (err) {
    testInfo.annotations.push({ type: "note", description: `screenshot ${name} failed: ${err.message.split("\n")[0]}` });
  }
  return file;
}

/** Say that part of this check still needs a person, and which part. */
export function partly(testInfo, why) {
  testInfo.annotations.push({ type: "partly", description: why });
}

export function note(testInfo, text) {
  testInfo.annotations.push({ type: "note", description: text });
}

/** Mark a failure as an outside service being down rather than StemDeck. */
export function serviceDown(testInfo, evidence) {
  testInfo.annotations.push({ type: "service", description: evidence });
}

// Answers from outside hosts the page asked (Wikidata, Wikipedia, LRCLIB, the
// artwork CDN), kept per page so a failed check can say what the services did.
const outside = new WeakMap();

export function watchNetwork(page) {
  if (outside.has(page)) return outside.get(page);
  const seen = [];
  outside.set(page, seen);
  const external = (url) => /^https?:\/\//.test(url) && !/^https?:\/\/(127\.0\.0\.1|localhost|tauri\.localhost|ipc\.localhost)/.test(url);
  page.on("response", (res) => {
    const url = res.url();
    if (external(url)) seen.push({ at: Date.now(), url: short(url), status: res.status() });
  });
  page.on("requestfailed", (req) => {
    const url = req.url();
    if (external(url)) seen.push({ at: Date.now(), url: short(url), status: 0, failure: req.failure()?.errorText || "failed" });
  });
  return seen;
}

const short = (url) => {
  try {
    const u = new URL(url);
    return `${u.host}${u.pathname}`.slice(0, 120);
  } catch {
    return url.slice(0, 120);
  }
};

/** The app's own logs, read from the package's data folder. */
export function readLogs() {
  const out = [];
  for (const name of ["stemdeck.log", "backend.log"]) {
    try {
      out.push(fs.readFileSync(path.join(LOGS_DIR, name), "utf8"));
    } catch {
      // Not written yet.
    }
  }
  return out.join("\n");
}

// A service that is down, rate limiting or refusing, as the backend logs it or
// as a request from the page saw it. Deliberately about the service, not the
// code: a traceback of ours is not in here.
const SERVICE_LOG = /(HTTP Error (5\d\d|429)|\b(502|503|504)\b.*(Service|Gateway|Bad)|Service Unavailable|Too Many Requests|LRCLIB busy|timed out|ConnectTimeout|ReadTimeout|Max retries exceeded|Sign in to confirm|confirm your age|rate.?limit)/i;
const YOUTUBE_REFUSAL = /(Sign in to confirm|not a bot|confirm your age|age.?restrict|Video unavailable|This video is (private|unavailable)|HTTP Error 4(03|29)|Requested format is not available|unable to extract)/i;

/**
 * Evidence of an outside service failing since `since` (ms), for the report.
 * Empty when there is none, which means a failure is StemDeck's.
 */
export function serviceEvidence(page, since, { ignoreHosts = [] } = {}) {
  const lines = [];
  for (const r of outside.get(page) || []) {
    if (r.at < since) continue;
    if (ignoreHosts.some((h) => r.url.startsWith(h))) continue;
    if (r.status >= 500 || r.status === 429 || r.status === 0) {
      lines.push(`page: ${r.url} -> ${r.status || r.failure}`);
    }
  }
  for (const line of readLogs().split(/\r?\n/)) {
    // stemdeck.log lines start with a local "YYYY-MM-DD HH:MM:SS"; compare as
    // text, and keep undated lines (tracebacks) only when they name a service.
    const stamp = line.slice(0, 19);
    const dated = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(stamp);
    if (dated && localStamp(since - 1000) > stamp) continue;
    if (!dated && !/(musicbrainz|lrclib|acoustid|wikidata|wikipedia|youtube)/i.test(line)) continue;
    // A retry still under way is not an outage.
    if (SERVICE_LOG.test(line) && !/asking again|retrying/i.test(line)) lines.push(`log: ${line.trim().slice(0, 220)}`);
  }
  return [...new Set(lines)].slice(0, 12);
}

function localStamp(ms) {
  const d = new Date(ms);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

// ─── The API, read only ─────────────────────────────────────────────────────

const baseURL = () => {
  const state = readState();
  if (!state?.baseURL) throw new Error("No backend URL recorded; the page was never reached.");
  return state.baseURL;
};

export async function api(pathname) {
  const res = await fetch(`${baseURL()}${pathname}`);
  if (!res.ok) throw new Error(`GET ${pathname} -> ${res.status}`);
  return res.json();
}

export const job = (id) => api(`/api/jobs/${encodeURIComponent(id)}`);

/** Thrown when a job fails for a reason that is YouTube's, not StemDeck's. */
export class ServiceError extends Error {}

/**
 * Wait for a job to finish. A job that fails throws, as a ServiceError when
 * YouTube refused it (an age gate, a bot check, a video taken down).
 */
export async function waitForJob(id, { timeout = JOB_TIMEOUT_MS, label = id } = {}) {
  const deadline = Date.now() + timeout;
  let last = null;
  while (Date.now() < deadline) {
    try {
      last = await job(id);
    } catch {
      last = null;
    }
    if (last?.status === "done") return last;
    if (last?.status === "error" || last?.status === "cancelled") {
      const why = `${label}: import ${last.status}: ${last.error || last.stage || ""} ${last.error_detail || ""}`.trim();
      if (YOUTUBE_REFUSAL.test(why) || SERVICE_LOG.test(why)) throw new ServiceError(why);
      throw new Error(why);
    }
    await new Promise((r) => setTimeout(r, 3000));
  }
  throw new Error(`${label}: not finished after ${Math.round(timeout / 60000)} min (last: ${last?.status} ${last?.stage})`);
}

// ─── Importing ──────────────────────────────────────────────────────────────

/** Make the library the sidebar's view, as a fresh window has it. */
export async function showLibrary(page) {
  const lib = page.locator("#appMenuBtn");
  if ((await lib.getAttribute("aria-pressed")) !== "true") await lib.click();
  await expect(lib).toHaveAttribute("aria-pressed", "true");
}

/** Paste a link in the top bar's field and press Split stems. Returns the job id. */
export async function importLink(page, url) {
  const field = page.locator("#url");
  await field.fill(url);
  const [res] = await Promise.all([
    page.waitForResponse((r) => new URL(r.url()).pathname === "/api/jobs" && r.request().method() === "POST", { timeout: 30_000 }),
    page.locator("#submit").click(),
  ]);
  const body = await res.json().catch(() => ({}));
  if (!res.ok()) throw new Error(`Split stems refused ${url}: ${res.status()} ${body.detail || ""}`);
  return body.job_id;
}

/** Hand a file to the top bar's file input, as the upload button does, and split it. */
export async function importFile(page, file) {
  await page.locator("#fileInput").setInputFiles(file);
  await expect(page.locator("#filePill")).toBeVisible();
  const [res] = await Promise.all([
    page.waitForResponse((r) => new URL(r.url()).pathname === "/api/jobs" && r.request().method() === "POST", { timeout: 60_000 }),
    page.locator("#submit").click(),
  ]);
  const body = await res.json().catch(() => ({}));
  if (!res.ok()) throw new Error(`Split stems refused ${path.basename(file)}: ${res.status()} ${body.detail || ""}`);
  return body.job_id;
}

// ─── Local files ────────────────────────────────────────────────────────────

/** The package's own FFmpeg, which first run put in data\ffmpeg. */
export function packageFfmpeg() {
  const exe = path.join(DATA_DIR, "ffmpeg", process.platform === "win32" ? "ffmpeg.exe" : "ffmpeg");
  if (!fs.existsSync(exe)) throw new Error(`The package has no FFmpeg yet at ${exe}`);
  return exe;
}

/**
 * A minute of a chord over soft noise, made by the package's FFmpeg. Music
 * enough to separate, and nothing any service could recognise. `tags` become
 * the file's own tags; none leaves a file with nothing to go on.
 */
export function makeAudio(name, { tags = null, seconds = 60 } = {}) {
  fs.mkdirSync(FILES_DIR, { recursive: true });
  const file = path.join(FILES_DIR, name);
  const args = [
    "-y", "-hide_banner", "-loglevel", "error",
    "-f", "lavfi", "-i", `sine=frequency=220:duration=${seconds}`,
    "-f", "lavfi", "-i", `sine=frequency=277:duration=${seconds}`,
    "-f", "lavfi", "-i", `sine=frequency=330:duration=${seconds}`,
    "-f", "lavfi", "-i", `anoisesrc=color=pink:amplitude=0.05:duration=${seconds}`,
    "-filter_complex", "amix=inputs=4:normalize=0,volume=0.4",
    "-ac", "2", "-ar", "44100",
    "-map_metadata", "-1",
  ];
  for (const [k, v] of Object.entries(tags || {})) args.push("-metadata", `${k}=${v}`);
  if (name.endsWith(".mp3")) args.push("-c:a", "libmp3lame", "-b:a", "192k", "-id3v2_version", "3");
  args.push(file);
  const out = spawnSync(packageFfmpeg(), args, { encoding: "utf8" });
  if (out.status !== 0) throw new Error(`FFmpeg could not make ${name}: ${out.stderr}`);
  return file;
}

// ─── The queue, once ────────────────────────────────────────────────────────

/**
 * Queue every import the checks need, once per run; later calls return the
 * job ids already recorded. The first one takes the studio (nothing is open
 * yet), the rest go to the background, exactly as the manual run does it.
 */
export async function queueImports(page) {
  const state = readState() || {};
  const jobs = state.jobs || {};
  for (const [key, song] of Object.entries(SONGS)) {
    if (jobs[key] || (song.long && SKIP_LONG)) continue;
    try {
      if (song.long) await setMaxTrackLength(page, 60);
      if (song.url) {
        jobs[key] = await importLink(page, song.url);
      } else {
        const tags = key === "localMp3"
          ? { title: "Acceptance Tone", artist: "StemDeck Acceptance", album: "Test Files", date: "2026" }
          : null;
        jobs[key] = await importFile(page, makeAudio(song.file, { tags }));
      }
    } catch (err) {
      jobs[key] = `failed: ${err.message.split("\n")[0]}`;
    }
    writeState({ ...readState(), jobs });
  }
  return jobs;
}

/**
 * The job id of an import, waiting for it to finish. A link YouTube refused
 * (a 403 on the media now and then) is split once more, as a person would
 * press Split stems again; the retry is recorded in the state file so the
 * check can say so.
 */
export async function songJob(page, key) {
  const jobs = await queueImports(page);
  const id = jobs[key];
  if (!id || id.startsWith("failed:")) throw new Error(`${SONGS[key].label} was not imported: ${id}`);
  try {
    await waitForJob(id, { label: SONGS[key].label });
    return id;
  } catch (err) {
    const retried = readState().retried || {};
    if (!(err instanceof ServiceError) || !SONGS[key].url || retried[key]) throw err;
    const again = await importLink(page, SONGS[key].url);
    const state = readState();
    writeState({
      ...state,
      jobs: { ...state.jobs, [key]: again },
      retried: { ...retried, [key]: err.message.slice(0, 200) },
    });
    await waitForJob(again, { label: `${SONGS[key].label} (second try)` });
    return again;
  }
}

// ─── The studio ─────────────────────────────────────────────────────────────

/**
 * Open a finished track from the library and wait until it is the one in the
 * studio: the page's current track is this id and the transport's total is
 * its length. Waiting only for "a duration" returned while the previous track
 * was still the one loaded.
 */
export async function openTrack(page, id) {
  await showLibrary(page);
  const duration = (await job(id).catch(() => ({}))).duration || 0;
  const row = page.locator(`.cat-item[data-id="${id}"]`).first();
  await row.waitFor({ timeout: 60_000 });
  await row.scrollIntoViewIfNeeded();
  await row.click();
  await expect(page.locator(".app")).not.toHaveClass(/no-track/, { timeout: 30_000 });
  await page.waitForFunction(
    async ([want, length]) => {
      const { getCurrentTrackInfo } = await import("/js/catalog.js");
      if (getCurrentTrackInfo()?.id !== want) return false;
      const m = (document.querySelector("#t-time")?.textContent || "").match(/\/\s*(\d+):(\d{2})(?::(\d{2}))?/);
      if (!m) return false;
      const total = m[3] ? Number(m[1]) * 3600 + Number(m[2]) * 60 + Number(m[3]) : Number(m[1]) * 60 + Number(m[2]);
      return total > 0 && (!length || Math.abs(total - length) <= 2);
    },
    [id, duration],
    { timeout: 120_000, polling: 250 },
  );
}

/**
 * Raise Max track length in Settings, General, as a person must before an
 * hour-long import: the default is 20 minutes and a longer video is refused.
 */
export async function setMaxTrackLength(page, minutes) {
  const dialog = await openSettings(page, "general");
  const field = dialog.locator(".set-max-duration");
  await field.scrollIntoViewIfNeeded();
  await field.fill(String(minutes));
  await Promise.all([
    page.waitForResponse((r) => r.url().endsWith("/api/settings") && r.request().method() === "POST", { timeout: 30_000 }),
    field.press("Tab"),
  ]);
  await closeSettings(page);
  const kept = Math.round((await api("/api/settings")).max_duration_sec / 60);
  if (kept < minutes) throw new Error(`Max track length stayed at ${kept} minutes`);
}

/**
 * Open About this song and wait until it has stopped looking things up:
 * no "Looking up ..." and no "Finding this track's artist...".
 */
export async function openAbout(page) {
  await page.locator("#np-details-btn").click();
  const dialog = page.locator("#artistDialog");
  await expect(dialog).toBeVisible();
  await expect.poll(
    () => page.evaluate(() => Boolean(document.querySelector("#artistBody .artist-status.loading"))),
    { timeout: 90_000, intervals: [500] },
  ).toBe(false);
  // Photos load lazily; give the ones on screen the moment they need, so the
  // screenshot shows them and a check can tell a photo from an empty box.
  await expect.poll(
    () => page.evaluate(() => [...document.querySelectorAll("#artistBody img")].every((img) => img.complete)),
    { timeout: 15_000 },
  ).toBe(true).catch(() => {});
  return dialog;
}

export async function closeAbout(page) {
  const dialog = page.locator("#artistDialog");
  if (await dialog.isVisible()) await page.locator("#artistClose").click();
  await expect(dialog).toBeHidden();
}

/** The artist box as text, section by section, for assertions and the report. */
export function aboutOutline(page) {
  return page.evaluate(() => {
    const body = document.getElementById("artistBody");
    const text = (sel, root = body) => root?.querySelector(sel)?.textContent?.trim() || "";
    return {
      order: [...body.children].map((c) => c.className.split(" ").filter((k) => k.startsWith("artist-")).join(".")),
      songTitle: text(".artist-song-title"),
      songFrom: text(".artist-song-from"),
      workTitle: text(".artist-work .artist-section-title"),
      workName: text(".artist-work-name"),
      workSynopsis: text(".artist-work-synopsis"),
      workCredits: text(".artist-work-credits:not(.artist-song-facts)"),
      workLinks: [...body.querySelectorAll(".artist-work .artist-link")].map((a) => a.dataset.kind),
      performerTitle: text(".artist-performer .artist-section-title"),
      name: text(".artist-name"),
      native: text(".artist-native"),
      desc: text(".artist-head .artist-desc"),
      photo: [...body.querySelectorAll(".artist-head .artist-photo")].some((img) => img.naturalWidth > 0),
      photoState: [...body.querySelectorAll(".artist-head .artist-photo")]
        .map((img) => `${img.complete ? (img.naturalWidth ? "loaded" : "broken") : "loading"} ${img.currentSrc.replace(/^https:\/\//, "").slice(0, 90)}`)
        .join("; ") || "none",
      sections: [...body.querySelectorAll(".artist-section-title")].map((h) => h.textContent.trim()),
      history: text(".artist-history"),
      albums: body.querySelectorAll(".artist-albums li").length,
      bandLinks: [...body.querySelectorAll(":scope > .artist-links .artist-link, .artist-performer .artist-link")].map((a) => a.dataset.kind),
      status: text(".artist-status"),
      searchShown: !document.getElementById("artistSearch")?.classList.contains("hidden"),
    };
  });
}

/** Switch the sidebar to Lyrics and wait for it to settle on an answer. */
export async function openLyrics(page) {
  const rail = page.locator(".rail-lyrics");
  if ((await rail.getAttribute("aria-pressed")) !== "true") await rail.click();
  await expect(page.locator("#lyricsPanel")).toBeVisible();
  await expect.poll(
    () => page.evaluate(() => {
      const status = document.getElementById("lyricsStatus");
      const busy = status?.classList.contains("loading");
      const lines = document.querySelectorAll("#lyricsBody .lyrics-line, #lyricsBody .lyrics-text").length;
      return !busy && (lines > 0 || Boolean(status?.textContent.trim()) || document.querySelectorAll("#lyricsVersions .lyrics-version").length > 0);
    }),
    { timeout: 60_000, intervals: [500] },
  ).toBe(true);
}

/** The Lyrics panel's state, as text and counts. */
export function lyricsState(page) {
  return page.evaluate(() => ({
    status: document.getElementById("lyricsStatus")?.textContent.trim() || "",
    statusKind: document.getElementById("lyricsStatus")?.className || "",
    meta: document.querySelector("#lyricsVersions .lyrics-match-meta")?.textContent.trim() || "",
    match: document.querySelector("#lyricsVersions .lyrics-match-title")?.textContent.trim() || "",
    synced: document.querySelectorAll("#lyricsBody .lyrics-line").length,
    plain: document.querySelectorAll("#lyricsBody .lyrics-text").length,
    offered: document.querySelectorAll("#lyricsVersions .lyrics-version").length,
    text: document.getElementById("lyricsBody")?.innerText || "",
  }));
}

/** Play for `ms` and report how the karaoke marks moved. */
export async function playAndWatch(page, ms) {
  const play = page.locator("#t-play");
  await play.click();
  const samples = [];
  const end = Date.now() + ms;
  while (Date.now() < end) {
    samples.push(await page.evaluate(() => ({
      time: document.querySelector("#t-time")?.textContent || "",
      current: [...document.querySelectorAll("#lyricsBody .lyrics-line")].findIndex((l) => l.classList.contains("current")),
      sung: document.querySelectorAll("#lyricsBody .lw.sung").length,
      singing: document.querySelectorAll("#lyricsBody .lw.singing").length,
    })));
    await page.waitForTimeout(400);
  }
  await play.click();
  return samples;
}

/** Seconds from a "mm:ss" or "hh:mm:ss" readout (the first time in the text). */
export function seconds(text) {
  const m = String(text).match(/(\d+):(\d{2})(?::(\d{2}))?/);
  if (!m) return NaN;
  return m[3] ? Number(m[1]) * 3600 + Number(m[2]) * 60 + Number(m[3]) : Number(m[1]) * 60 + Number(m[2]);
}

/**
 * Close whatever a previous check left open, with the app's own close
 * buttons: an error card, Settings, the About and artist boxes, the folder
 * editor, the notifications panel.
 */
export async function tidy(page) {
  const closers = [
    ["#error:not(.hidden)", "#error .error-close"],
    [".folder-editor-backdrop", ".folder-editor-cancel"],
    [".library-editor-backdrop", ".library-editor-backdrop .settings-done"],
    ["#artistDialog:not(.hidden)", "#artistClose"],
    ["#aboutDialog:not(.hidden)", "#aboutClose"],
    ["#releaseDialog:not(.hidden)", "#releaseClose"],
    [".daw-notif-wrap.open, #notifBtn[aria-expanded=\"true\"]", ".daw-notif-close"],
  ];
  for (const [open, close] of closers) {
    try {
      if (await page.locator(open).first().isVisible()) await page.locator(close).first().click({ timeout: 5000 });
    } catch {
      // Already gone.
    }
  }
  await page.keyboard.press("Escape").catch(() => {});
}

/** Open Settings on a tab. */
export async function openSettings(page, tab = "general") {
  await page.locator("#settingsBtn").click();
  const dialog = page.locator(".library-editor-backdrop");
  await expect(dialog).toBeVisible();
  await dialog.locator(`.settings-tab[data-tab="${tab}"]`).click();
  return dialog;
}

export async function closeSettings(page) {
  const dialog = page.locator(".library-editor-backdrop");
  if (await dialog.count()) await dialog.locator(".settings-done").click();
  await expect(dialog).toHaveCount(0);
}

/**
 * Record every native command the page asks the desktop shell for, still
 * passing each one through. Set STEMDECK_ACCEPTANCE_NO_BROWSER=1 to keep
 * open_url from reaching the real browser.
 */
export async function spyOnInvoke(page) {
  const passThrough = process.env.STEMDECK_ACCEPTANCE_NO_BROWSER !== "1";
  return page.evaluate((through) => {
    const tauri = window.__TAURI__;
    if (!tauri?.core) return false;
    if (!window.__acceptance) {
      // The core object is frozen, but the property holding it is not: put a
      // copy in its place whose invoke records the call and passes it on.
      const core = tauri.core;
      const original = core.invoke.bind(core);
      window.__acceptance = { calls: [], original };
      const invoke = (cmd, args, options) => {
        const call = { cmd, args, settled: null };
        window.__acceptance.calls.push(call);
        if (cmd === "open_url" && !window.__acceptance.through) {
          call.settled = "held";
          return Promise.resolve(null);
        }
        const p = original(cmd, args, options);
        p.then(() => { call.settled = "ok"; }, (e) => { call.settled = `error: ${e}`; });
        return p;
      };
      tauri.core = { ...core, invoke };
    }
    window.__acceptance.through = through;
    return window.__TAURI__.core.invoke !== window.__acceptance.original;
  }, passThrough);
}

// ─── Lyrics timing ──────────────────────────────────────────────────────────

/**
 * An LRC's lines as { time, text, words }: `text` without its word stamps,
 * `words` the times of its enhanced-LRC word stamps (<mm:ss.xx>), in order.
 * Lines with no words (a stamp alone marks a pause) come back with text "".
 */
export function lrcLines(lrc) {
  const out = [];
  for (const raw of String(lrc || "").split(/\r?\n/)) {
    const m = /^\s*\[(\d+):(\d+(?:\.\d+)?)\](.*)$/.exec(raw);
    if (!m) continue;
    const rest = m[3];
    const words = [...rest.matchAll(/<(\d+):(\d+(?:\.\d+)?)>/g)].map((w) => Number(w[1]) * 60 + Number(w[2]));
    const text = rest.replace(/<\d+:\d+(?:\.\d+)?>/g, "").trim();
    out.push({ time: Number(m[1]) * 60 + Number(m[2]), text, words });
  }
  return out;
}

/** Seconds from the Sync lines readout, "m:ss.cc" (lyricsLane.js formatClock). */
export function clockSeconds(text) {
  const m = /^(\d+):(\d{2})\.(\d{2})$/.exec(String(text || "").trim());
  return m ? Number(m[1]) * 60 + Number(m[2]) + Number(m[3]) / 100 : NaN;
}

/**
 * Where the voice starts in the vocals stem, from its envelope as the app
 * serves it (GET /api/jobs/{id}/vocal-envelope: { hop, db }): the rise of the
 * level over 120 ms, where the voice is singing, as in
 * app/pipeline/lyrics_align.py onset_strength. Written again here, not
 * called, so the check does not grade the code with itself: an onset is a
 * rise of 6 dB or more (_HIT_RISE_DB) within 20 dB of the stem's loud parts.
 *
 * Returns near(t, within): whether the voice rises within `within` s of t.
 */
export async function voiceOnsets(jobId) {
  const { hop, db } = await api(`/api/jobs/${encodeURIComponent(jobId)}/vocal-envelope`);
  const levels = (db || []).map(Number);
  const n = levels.length;
  const strength = new Float64Array(n);
  if (n > 3 && hop > 0) {
    // numpy's percentile, linear between neighbours.
    const sorted = [...levels].sort((a, b) => a - b);
    const pos = (n - 1) * 0.95;
    const lo = Math.floor(pos);
    const loud = sorted[lo] + (sorted[Math.min(n - 1, lo + 1)] - sorted[lo]) * (pos - lo);
    if (loud >= -45) {
      const floor = Math.max(loud - 20, -50);
      // np.convolve(levels, ones(3)/3, "same"): zeros past the ends.
      const smooth = levels.map((_, i) => ((levels[i - 1] ?? 0) + levels[i] + (levels[i + 1] ?? 0)) / 3);
      for (let i = 3; i < n; i++) {
        if (smooth[i] < floor) continue;
        strength[i] = Math.max(0, smooth[i] - smooth[i - 3]);
      }
    }
  }
  const frame = (t) => Math.min(Math.max(0, Math.round(t / hop)), Math.max(0, n - 1));
  const near = (t, within) => {
    for (let i = frame(t - within); i <= frame(t + within); i++) if (strength[i] >= 6) return true;
    return false;
  };
  return { hop, frames: n, near };
}
