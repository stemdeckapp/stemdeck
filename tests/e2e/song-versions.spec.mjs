// Versions of a song (#804, static/js/versions.js, versionScope.js): a song
// keeps one set of stems, and each version is a named choice of them with a
// saved mix of its own. A song with more than one version shows them as rows
// under it in the library; a row opens the song in that version.
//
// The fixture has four stems (vocals, drums, bass, other) and every one is on
// disk, so nothing here separates anything: fill-stems is covered by
// tests/test_versions_api.py with a stand-in for Demucs.
import { test, expect } from "@playwright/test";
import { openStudio, JOB_ID, resetVersions } from "./helpers.mjs";

const lanes = (page) =>
  page.evaluate(async () => Object.keys((await import("/js/state.js")).trackIndex).filter((n) => !/^t[kr]_/.test(n)).sort().join(","));
const rows = (page) => page.locator(`#catalogList .cat-version[data-id="${JOB_ID}"]`);
const muted = (page, name) => page.evaluate(async (n) => Boolean((await import("/js/state.js")).mixerState[n]?.muted), name);

test.describe("versions of a song", () => {
  test.beforeEach(async ({ request }) => { await resetVersions(request); });
  test.afterEach(async ({ request }) => { await resetVersions(request); });

  test("a song with one version has no rows under it", async ({ page }) => {
    await openStudio(page);
    await expect(page.locator(`#catalogList .cat-item[data-id="${JOB_ID}"]`).first()).toBeVisible();
    await expect(rows(page)).toHaveCount(0);
  });

  test("save as new version, then switch between them from the library", async ({ page, request }) => {
    await page.addInitScript(() => {
      if (!sessionStorage.getItem("song-versions-spec")) {
        sessionStorage.setItem("song-versions-spec", "1");
        localStorage.setItem("stemdeck.mode", "practice");
      }
    });
    await page.setViewportSize({ width: 1280, height: 820 });
    await openStudio(page);
    await expect.poll(() => lanes(page)).toBe("bass,drums,other,vocals");

    // Untick Bass and Other, then save that as a new version: the first
    // version keeps all four lanes.
    await page.locator("#laneStemsBtn").click();
    await page.locator('#laneStemsPop input[data-stem="bass"]').click();
    await page.locator('#laneStemsPop input[data-stem="other"]').click();
    await page.locator("#laneStemsPop .stems-pop-vnew").click();
    await expect.poll(() => lanes(page), { timeout: 15000 }).toBe("drums,original,vocals");

    // Two rows under the song, indented, each with its name and stems.
    await expect(rows(page)).toHaveCount(2);
    await expect(rows(page).nth(0).locator(".cat-version-name")).toHaveText("Version 1");
    await expect(rows(page).nth(0).locator(".cat-version-stems")).toHaveText("All stems");
    await expect(rows(page).nth(1).locator(".cat-version-name")).toHaveText("Version 2");
    await expect(rows(page).nth(1).locator(".cat-version-stems")).toHaveText("Vocals, Drums");
    await expect(rows(page).nth(1)).toHaveClass(/active/);
    const song = await page.locator(`#catalogList .cat-item[data-id="${JOB_ID}"]`).first().boundingBox();
    const child = await rows(page).nth(1).boundingBox();
    expect(child.x).toBeGreaterThan(song.x + 10);
    expect(child.x + child.width).toBeLessThanOrEqual(song.x + song.width + 1);

    // The server keeps the list, and the stems are not copied.
    const listed = await (await request.get(`/api/jobs/${JOB_ID}/versions`)).json();
    expect(listed.versions.map((v) => v.stems.join(","))).toEqual(["vocals,drums,bass,guitar,piano,other", "vocals,drums"]);
    expect(listed.active_version).toBe(listed.versions[1].id);

    // A mix of its own: Drums muted here is not muted in Version 1.
    await page.evaluate(async () => (await import("/js/mixer.js")).toggleStemMute("drums"));
    expect(await muted(page, "drums")).toBe(true);

    await rows(page).nth(0).click();
    await expect.poll(() => lanes(page), { timeout: 15000 }).toBe("bass,drums,other,vocals");
    await expect(rows(page).nth(0)).toHaveClass(/active/);
    await expect.poll(() => muted(page, "drums")).toBe(false);

    await rows(page).nth(1).click();
    await expect.poll(() => lanes(page), { timeout: 15000 }).toBe("drums,original,vocals");
    await expect.poll(() => muted(page, "drums"), { timeout: 15000 }).toBe(true);

    // After a reload the song opens in the version it was left in.
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.locator(`.cat-item[data-id="${JOB_ID}"]`).first().click();
    await expect.poll(() => lanes(page), { timeout: 15000 }).toBe("drums,original,vocals");
  });

  test("a version with the same lanes opens without loading the song again, and keeps playing", async ({ page, request }) => {
    const all = ["vocals", "drums", "bass", "guitar", "piano", "other"];
    await request.post(`/api/jobs/${JOB_ID}/versions`, { data: { name: "Second mix", stems: all, select: false } });
    await openStudio(page);
    await expect(rows(page)).toHaveCount(2);
    // The gain each lane was last given in the engine, which is what is heard.
    await page.waitForFunction(async () => {
      const st = await import("/js/state.js");
      if (!st.audioEngine || (await st.audioEngine.ready) !== true) return false;
      const eng = st.audioEngine;
      window.__engine = eng;
      window.__gains = {};
      const setGain = eng.setGain;
      eng.setGain = (name, v) => { window.__gains[name] = v; return setGain(name, v); };
      return true;
    }, null, { timeout: 20000 });
    const gain = (name) => page.evaluate((n) => window.__gains[n], name);
    const soloed = (name) => page.evaluate(async (n) => Boolean((await import("/js/state.js")).mixerState[n]?.soloed), name);
    const playing = () => page.evaluate(async () => Boolean((await import("/js/state.js")).audioEngine?.isPlaying()));
    const time = () => page.evaluate(async () => (await import("/js/state.js")).audioEngine.getCurrentTime());
    const sameEngine = () => page.evaluate(async () => (await import("/js/state.js")).audioEngine === window.__engine);

    // Version 1's own mix: Bass muted.
    await page.evaluate(async () => (await import("/js/mixer.js")).toggleStemMute("bass"));
    await expect.poll(() => gain("bass")).toBe(0);
    const loads = [];
    page.on("request", (r) => {
      if (/^\/api\/jobs\/[^/]+(\/stems\/.*|\/peaks|\/beats)?$/.test(new URL(r.url()).pathname)) loads.push(r.url());
    });
    await page.locator("#t-play").click();
    await expect.poll(playing).toBe(true);

    // Version 2: its own mix, heard at once, and the song plays on from where it was.
    const before = await time();
    await rows(page).nth(1).click();
    await expect(rows(page).nth(1)).toHaveClass(/active/);
    await expect.poll(() => muted(page, "bass")).toBe(false);
    expect(await sameEngine()).toBe(true);
    await expect.poll(() => gain("bass")).toBeGreaterThan(0);
    expect(await playing()).toBe(true);
    expect(await time()).toBeGreaterThan(before);

    // Drums soloed in Version 2 stays in Version 2.
    await page.evaluate(async () => (await import("/js/mixer.js")).toggleStemSolo("drums"));
    await expect.poll(() => gain("vocals")).toBe(0);
    await rows(page).nth(0).click();
    await expect(rows(page).nth(0)).toHaveClass(/active/);
    await expect.poll(() => muted(page, "bass")).toBe(true);
    expect(await soloed("drums")).toBe(false);
    await expect.poll(() => gain("vocals")).toBeGreaterThan(0);
    expect(await gain("bass")).toBe(0);
    await rows(page).nth(1).click();
    await expect.poll(() => soloed("drums")).toBe(true);
    await expect.poll(() => gain("vocals")).toBe(0);

    // The same engine all along: no stem, peaks or song state was fetched again.
    expect(await sameEngine()).toBe(true);
    expect(loads).toEqual([]);
    expect(await page.evaluate(async () => (await import("/js/reloadPlace.js")).restorePending())).toBe(false);
  });

  test("rename and delete a version from the Stems popover", async ({ page, request }) => {
    await request.post(`/api/jobs/${JOB_ID}/versions`, { data: { name: "Guitar study", stems: ["drums", "vocals"], select: true } });
    await page.setViewportSize({ width: 1024, height: 768 });
    await openStudio(page);
    await expect(rows(page)).toHaveCount(2);
    await expect(rows(page).nth(1).locator(".cat-version-name")).toHaveText("Guitar study");

    await page.locator("#laneStemsBtn").click();
    const pop = page.locator("#laneStemsPop");
    await expect(pop.locator("#laneVersionSelect")).toHaveValue(/^[a-z0-9]+$/);
    const box = await pop.boundingBox();
    expect(box.x + box.width).toBeLessThanOrEqual(1024);

    await pop.locator(".stems-pop-vname").fill("Karaoke");
    await pop.locator(".stems-pop-vrename").click();
    await expect(rows(page).nth(1).locator(".cat-version-name")).toHaveText("Karaoke");

    // Delete asks twice; the stems stay and the song keeps one version.
    await pop.locator(".stems-pop-vdelete").click();
    await expect(pop.locator(".stems-pop-vdelete")).toHaveClass(/armed/);
    await pop.locator(".stems-pop-vdelete").click();
    await expect(rows(page)).toHaveCount(0);
    await expect.poll(() => lanes(page), { timeout: 15000 }).toBe("bass,drums,other,vocals");
    const listed = await (await request.get(`/api/jobs/${JOB_ID}/versions`)).json();
    expect(listed.versions.map((v) => v.id)).toEqual(["main"]);
    const stem = await request.get(`/api/jobs/${JOB_ID}/stems/drums.wav`);
    expect(stem.status()).toBe(200);
    await expect(pop.locator(".stems-pop-vdelete")).toBeDisabled();
  });

  test("delete a version from its row in the library", async ({ page, request }) => {
    await request.post(`/api/jobs/${JOB_ID}/versions`, { data: { name: "Guitar study", stems: ["drums", "vocals"], select: false } });
    await page.setViewportSize({ width: 1280, height: 820 });
    await openStudio(page);
    await expect(rows(page)).toHaveCount(2);

    // The first press only arms the button and says so; nothing is deleted.
    const del = rows(page).nth(1).locator(".cat-version-del");
    await rows(page).nth(1).hover();
    await del.click();
    await expect(del).toHaveClass(/armed/);
    await expect(del).toContainText("Press again");
    expect((await (await request.get(`/api/jobs/${JOB_ID}/versions`)).json()).versions).toHaveLength(2);
    // The press did not open the version either.
    await expect(rows(page).nth(0)).toHaveClass(/active/);

    // The second press deletes it: the rows go with the second version, the
    // song and its stems stay.
    await del.click();
    await expect(rows(page)).toHaveCount(0);
    const listed = await (await request.get(`/api/jobs/${JOB_ID}/versions`)).json();
    expect(listed.versions.map((v) => v.id)).toEqual(["main"]);
    expect((await request.get(`/api/jobs/${JOB_ID}/stems/drums.wav`)).status()).toBe(200);
    await expect.poll(() => lanes(page), { timeout: 15000 }).toBe("bass,drums,other,vocals");
  });

  test("Extract with other chips on the open song makes a version and separates nothing", async ({ page, request }) => {
    let filled = 0;
    await page.route("**/api/jobs/*/fill-stems", (route) => { filled += 1; return route.continue(); });
    await openStudio(page);
    await expect.poll(() => lanes(page)).toBe("bass,drums,other,vocals");
    // The chips: Vocals and Bass only.
    await page.evaluate(async () => {
      const { selectedStems } = await import("/js/state.js");
      selectedStems.clear();
      selectedStems.add("vocals");
      selectedStems.add("bass");
      (await import("/js/stemChoice.js")).refreshStemChoiceVisuals();
    });
    await page.evaluate(async (id) => {
      const btn = document.getElementById("submit");
      btn.dataset.resplitJob = id;
      document.getElementById("url").value = "";
    }, JOB_ID);
    await page.locator("#submit").click();
    await expect(rows(page)).toHaveCount(2);
    await expect(rows(page).nth(1).locator(".cat-version-stems")).toHaveText("Vocals, Bass");
    await expect.poll(() => lanes(page), { timeout: 15000 }).toBe("bass,original,vocals");
    expect(filled).toBe(0);
    const listed = await (await request.get(`/api/jobs/${JOB_ID}/versions`)).json();
    expect(listed.versions).toHaveLength(2);
  });

  test("Extract that separates a missing stem loads the song again, even with the lanes on screen", async ({ page, request }) => {
    await request.post(`/api/jobs/${JOB_ID}/versions`, { data: { name: "Two", stems: ["vocals", "drums"], select: true } });
    // The separation itself is the server's (tests/test_versions_api.py).
    let filled = 0;
    await page.route("**/api/jobs/*/fill-stems", (route) => {
      filled += 1;
      return route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
    });
    await openStudio(page);
    await expect.poll(() => lanes(page)).toBe("drums,original,vocals");
    await page.waitForFunction(async () => {
      const st = await import("/js/state.js");
      if (!st.audioEngine || (await st.audioEngine.ready) !== true) return false;
      window.__engine = st.audioEngine;
      return true;
    }, null, { timeout: 20000 });
    // The chips: Vocals, Drums and Guitar, which the song has no file for. Until
    // the song loads again, its lanes read the same as the ones on screen.
    await page.evaluate(async (id) => {
      const { selectedStems } = await import("/js/state.js");
      selectedStems.clear();
      for (const n of ["vocals", "drums", "guitar"]) selectedStems.add(n);
      (await import("/js/stemChoice.js")).refreshStemChoiceVisuals();
      document.getElementById("submit").dataset.resplitJob = id;
      document.getElementById("url").value = "";
    }, JOB_ID);
    await page.locator("#submit").click();
    await expect(rows(page)).toHaveCount(3);
    expect(filled).toBe(1);
    await expect.poll(() => page.evaluate(async () => {
      const st = await import("/js/state.js");
      return Boolean(st.audioEngine) && st.audioEngine !== window.__engine;
    }), { timeout: 15000 }).toBe(true);
  });

  test("the Guide explains every versions control", async ({ page, request }) => {
    await request.post(`/api/jobs/${JOB_ID}/versions`, { data: { name: "Guitar study", stems: ["drums"], select: false } });
    await openStudio(page);
    await expect(rows(page)).toHaveCount(2);
    await page.locator("#helpModeBtn").click();
    await expect(page.locator("body")).toHaveClass(/help-mode/);
    const tip = page.locator("#helpTip");
    await rows(page).nth(1).hover();
    await expect(tip).toContainText("One version of this song");
    await rows(page).nth(1).locator(".cat-version-del").hover();
    await expect(tip).toContainText("Delete this version");
    // In the Guide a click explains instead of opening the version.
    await rows(page).nth(1).click();
    await expect(rows(page).nth(0)).toHaveClass(/active/);

    await page.locator("#laneStemsBtn").click();
    const pop = page.locator("#laneStemsPop");
    for (const [sel, text] of [
      ["#laneVersionSelect", "Opens another version of this song"],
      [".stems-pop-vname", "The name of the version that is open"],
      [".stems-pop-vrename", "Gives the open version the name"],
      [".stems-pop-vnew", "as a new version of this song"],
      [".stems-pop-vdelete", "Press twice to confirm"],
    ]) {
      await pop.locator(sel).hover();
      await expect(tip, sel).toContainText(text);
    }
  });
});
