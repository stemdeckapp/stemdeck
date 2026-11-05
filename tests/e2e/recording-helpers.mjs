// Shared steps for the recording specs (recording, recording-tracks,
// video-take). The backend is real: takes and tracks go through
// app/api/takes.py, and every test removes what it recorded so the fixture
// job looks the same to the rest of the suite.
import { expect } from "@playwright/test";
import { JOB_ID } from "./helpers.mjs";

export const recBtn = (page) => page.locator("#t-rec");
export const trackRows = (page) => page.locator(".mixer-column .lane-header.track-lane:not(.hidden)");

/** Delete every recording track (and so every take) of the fixture job. */
export async function removeTracks(request) {
  const res = await request.get(`/api/jobs/${JOB_ID}`);
  if (!res.ok()) return;
  const job = await res.json();
  for (const tr of job.tracks || []) await request.delete(`/api/jobs/${JOB_ID}/tracks/${tr.id}`);
  for (const tk of job.takes || []) await request.delete(`/api/jobs/${JOB_ID}/takes/${tk.id}`);
}

export async function jobState(page) {
  return (await page.request.get(`/api/jobs/${JOB_ID}`)).json();
}

/** Press "+ Track" and wait for the new row. Returns its track id. */
export async function addTrack(page) {
  const before = await trackRows(page).count();
  await page.locator("#addTrackBtn").click();
  await expect(trackRows(page)).toHaveCount(before + 1, { timeout: 20000 });
  return trackRows(page).nth(before).getAttribute("data-take");
}

/** Wait until the engine is up again (a track change reloads the lanes). */
export async function waitForEngine(page) {
  await page.waitForFunction(async () => {
    const st = await import("/js/state.js");
    return !!st.audioEngine && !document.querySelector(".app")?.classList.contains("no-track");
  }, null, { timeout: 20000 });
}

/**
 * Press Record and wait until it records. With no track yet, one is added
 * first: Record arms the only track there is.
 */
export async function pressRecord(page) {
  if (!(await trackRows(page).count())) await addTrack(page);
  await waitForEngine(page);
  await recBtn(page).click();
  await expect(recBtn(page)).toHaveClass(/recording/, { timeout: 10000 });
}

export async function recordForAWhile(page, ms = 2500) {
  await pressRecord(page);
  // The transport starts once the recorder has its first frame.
  await page.waitForFunction(async () => (await import("/js/state.js")).audioEngine?.isPlaying(), null, { timeout: 10000 });
  await page.waitForTimeout(ms);
  await recBtn(page).click();
  await expect(recBtn(page)).not.toHaveClass(/recording/);
}

/** Record what the page sends to the takes API, from the real requests. */
export function watchTakes(page) {
  const log = { created: [], parts: [], camera: [], finishes: [], finishStatus: [], patches: [], deletes: [], renders: [], trackPatches: [] };
  page.on("request", (req) => {
    const url = new URL(req.url());
    const m = url.pathname.match(/\/api\/jobs\/[^/]+\/(takes|tracks)(?:\/([tk][0-9a-f]{8}))?(\/.*)?$/);
    if (!m) return;
    const [, kind, id, rest = ""] = m;
    if (kind === "tracks") {
      if (req.method() === "PATCH") log.trackPatches.push(req.postDataJSON());
      return;
    }
    if (!id && req.method() === "POST") log.created.push(req.postDataJSON());
    else if (rest === "/parts/audio") log.parts.push({ take: id, seq: Number(url.searchParams.get("seq")), bytes: req.postDataBuffer()?.length ?? 0 });
    else if (rest === "/parts/camera") log.camera.push({ take: id, seq: Number(url.searchParams.get("seq")), bytes: req.postDataBuffer()?.length ?? 0, type: req.headers()["content-type"] });
    else if (rest === "/finish" && req.method() === "POST") log.finishes.push({ take: id, body: req.postDataJSON() });
    else if (rest === "" && req.method() === "PATCH") log.patches.push(req.postDataJSON());
    else if (rest === "" && req.method() === "DELETE") log.deletes.push(id);
    else if (rest === "/render" && req.method() === "POST") log.renders.push(req.postDataJSON());
  });
  page.on("response", (res) => {
    if (/\/takes\/t[0-9a-f]{8}\/finish$/.test(new URL(res.url()).pathname)) log.finishStatus.push(res.status());
  });
  return log;
}
