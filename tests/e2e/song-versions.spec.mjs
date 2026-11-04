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

  // On the desktop each store_set saves the whole user-data store to disk
  // (#996): a delete that cleared its eight records one by one rewrote the
  // file eight times. Its records go in one store_set_many, which saves once.
  test("on the desktop, deleting a version clears its records in one store write", async ({ page, request }) => {
    const made = await (await request.post(`/api/jobs/${JOB_ID}/versions`, { data: { name: "Guitar study", stems: ["drums", "vocals"], select: false } })).json();
    const vid = made.version.id;
    await page.setViewportSize({ width: 1280, height: 820 });
    await openStudio(page, { tauri: true });
    await expect(rows(page)).toHaveCount(2);

    const del = rows(page).nth(1).locator(".cat-version-del");
    await rows(page).nth(1).hover();
    await del.click();
    await del.click();
    await expect(rows(page)).toHaveCount(0);

    const scoped = `${JOB_ID}~${vid}:`;
    const writes = (await page.evaluate(() => window.__e2e.calls.filter((c) => c.cmd.startsWith("store_set"))))
      .filter((c) => c.cmd === "store_set" ? c.args.key.includes(scoped) : Object.keys(c.args.entries).some((k) => k.includes(scoped)));
    expect(writes.map((c) => c.cmd)).toEqual(["store_set_many"]);
    const modes = ["practice", "daw", "karaoke", "streamer"];
    expect(Object.keys(writes[0].args.entries).sort()).toEqual(
      modes.flatMap((m) => [`stemdeck:mix:${scoped}${m}`, `stemdeck:stems:${scoped}${m}`]).sort(),
    );
    expect(Object.values(writes[0].args.entries).every((v) => v === null)).toBe(true);
  });

  test("on the desktop, a new version's mixes and voice are written in one store write", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 820 });
    await openStudio(page, { tauri: true });
    const modes = ["practice", "daw", "karaoke", "streamer"];
    // A saved mix in every mode of the version open now, for copyMix to carry.
    await page.evaluate(async ([job, modes]) => {
      const { storeSet } = await import("/js/utils.js");
      for (const m of modes) await storeSet(`stemdeck:mix:${job}:${m}`, { drums: { volume: 0.5, muted: true, soloed: false, pitch: 0, fx: [] } });
    }, [JOB_ID, modes]);
    const vid = await page.evaluate(async (job) => {
      const { createVersion } = await import("/js/versions.js");
      return (await createVersion(job, { name: "Two", lanes: ["drums", "vocals"], voice: "split", copyMix: true })).id;
    }, JOB_ID);

    const scoped = `${JOB_ID}~${vid}:`;
    const writes = (await page.evaluate(() => window.__e2e.calls.filter((c) => c.cmd.startsWith("store_set"))))
      .filter((c) => c.cmd === "store_set" ? c.args.key.includes(scoped) : Object.keys(c.args.entries).some((k) => k.includes(scoped)));
    // The first write of the new version's records holds all of them; the
    // mix the open song saves later as it is played is its own write.
    expect(writes[0].cmd).toBe("store_set_many");
    const entries = writes[0].args.entries;
    expect(Object.keys(entries).sort()).toEqual([
      ...modes.map((m) => `stemdeck:mix:${scoped}${m}`),
      `stemdeck:stems:${scoped}practice`,
      `stemdeck:stems:${scoped}daw`,
    ].sort());
    for (const m of modes) expect(entries[`stemdeck:mix:${scoped}${m}`].drums.muted).toBe(true);
    expect(entries[`stemdeck:stems:${scoped}practice`].voice).toBe("split");
    expect(entries[`stemdeck:stems:${scoped}daw`].voice).toBe("split");
  });

  test("on the desktop, Extract with Lead + Backing writes the matching version's voice in one store write", async ({ page, request }) => {
    const made = await (await request.post(`/api/jobs/${JOB_ID}/versions`, { data: { name: "Two", stems: ["vocals", "drums"], select: false } })).json();
    const vid = made.version.id;
    // The split itself is the server's; here it only has to answer done.
    let split = 0;
    await page.route("**/api/jobs/*/vocal-split", (route) => {
      split += 1;
      return route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
    });
    await page.setViewportSize({ width: 1280, height: 820 });
    await openStudio(page, { tauri: true });
    await expect(rows(page)).toHaveCount(2);

    await page.evaluate(async (job) => {
      const { extractAsVersion } = await import("/js/versions.js");
      await extractAsVersion(["vocals", "drums"], "split", () => {}, job);
    }, JOB_ID);
    expect(split).toBe(1);

    const scoped = `${JOB_ID}~${vid}:`;
    const many = (await page.evaluate(() => window.__e2e.callsFor("store_set_many")))
      .filter((c) => Object.keys(c.args.entries).some((k) => k.includes(scoped)));
    // First the freeze before the split (every version at one Vocals lane),
    // then this version's own voice, which replaces it.
    expect(many).toHaveLength(2);
    expect(Object.values(many[0].args.entries).every((r) => r.voice === "combined")).toBe(true);
    const entries = many[1].args.entries;
    expect(Object.keys(entries).sort()).toEqual([`stemdeck:stems:${scoped}daw`, `stemdeck:stems:${scoped}practice`]);
    expect(entries[`stemdeck:stems:${scoped}practice`].voice).toBe("split");
    expect(entries[`stemdeck:stems:${scoped}daw`].voice).toBe("split");
  });

  // Before a Lead + Backing split, every mode of every version with no voice
  // of its own is frozen at one Vocals lane. One store_set per record saved
  // the whole store file once each (#996); the records go in one
  // store_set_many, written before the split starts.
  test("on the desktop, freezing the other versions' voice before a split is one store write", async ({ page, request }) => {
    const made = await (await request.post(`/api/jobs/${JOB_ID}/versions`, { data: { name: "Two", stems: ["vocals", "drums"], select: false } })).json();
    const vid = made.version.id;
    let callsAtSplit = -1;
    await page.route("**/api/jobs/*/vocal-split", async (route) => {
      callsAtSplit = await page.evaluate(() => window.__e2e.calls.length);
      return route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
    });
    await page.setViewportSize({ width: 1280, height: 820 });
    await openStudio(page, { tauri: true });
    await expect(rows(page)).toHaveCount(2);

    // Version 1's Karaoke has a voice of its own: it is not frozen.
    const own = `stemdeck:stems:${JOB_ID}:karaoke`;
    const start = await page.evaluate(async (key) => {
      const { storeSet } = await import("/js/utils.js");
      await storeSet(key, { remove: "", voice: "split" });
      return window.__e2e.calls.length;
    }, own);

    await page.evaluate(async (job) => {
      const { extractAsVersion } = await import("/js/versions.js");
      await extractAsVersion(["vocals", "drums"], "split", () => {}, job);
    }, JOB_ID);
    expect(callsAtSplit).toBeGreaterThan(start);

    const stems = `stemdeck:stems:${JOB_ID}`;
    const touches = (c) => c.cmd === "store_set" ? c.args.key.startsWith(stems) : Object.keys(c.args.entries).some((k) => k.startsWith(stems));
    const all = await page.evaluate(() => window.__e2e.calls);
    const before = all.slice(start, callsAtSplit).filter((c) => c.cmd.startsWith("store_set") && touches(c));
    expect(before.map((c) => c.cmd)).toEqual(["store_set_many"]);
    const entries = before[0].args.entries;
    const scoped = `${stems}~${vid}:`;
    expect(Object.keys(entries)).toEqual([
      `${stems}:practice`, `${stems}:daw`, `${stems}:streamer`,
      `${scoped}practice`, `${scoped}daw`, `${scoped}karaoke`, `${scoped}streamer`,
    ]);
    expect(own in entries).toBe(false);
    for (const rec of Object.values(entries)) expect(rec.voice).toBe("combined");
  });

  test("on the desktop, Lead + Backing asked for in the open song writes each version's modes in one store write", async ({ page, request }) => {
    const made = await (await request.post(`/api/jobs/${JOB_ID}/versions`, { data: { name: "Two", stems: ["vocals", "drums"], select: false } })).json();
    const vid = made.version.id;
    await page.addInitScript(() => localStorage.setItem("stemdeck.mode", "practice"));
    // The split answers an error: the song is not reloaded, so the calls stay.
    let callsAtSplit = -1;
    await page.route("**/api/jobs/*/vocal-split", async (route) => {
      callsAtSplit = await page.evaluate(() => window.__e2e.calls.length);
      return route.fulfill({ status: 500, contentType: "application/json", body: "{}" });
    });
    await page.setViewportSize({ width: 1280, height: 820 });
    await openStudio(page, { tauri: true });
    await expect(rows(page)).toHaveCount(2);

    const start = await page.evaluate(() => window.__e2e.calls.length);
    expect(await page.evaluate(async () => (await import("/js/modeStems.js")).requestSplit())).toBe("failed");
    expect(callsAtSplit).toBeGreaterThan(start);

    const stems = `stemdeck:stems:${JOB_ID}`;
    const touches = (c) => c.cmd === "store_set" ? c.args.key.startsWith(stems) : Object.keys(c.args.entries).some((k) => k.startsWith(stems));
    const all = await page.evaluate(() => window.__e2e.calls);
    const before = all.slice(start, callsAtSplit).filter((c) => c.cmd.startsWith("store_set") && touches(c));
    expect(before.map((c) => c.cmd)).toEqual(["store_set_many", "store_set_many"]);
    // The open version first: the pair in Practice, one Vocals lane elsewhere.
    const open = before[0].args.entries;
    expect(Object.keys(open)).toEqual(["practice", "daw", "karaoke", "streamer"].map((m) => `${stems}:${m}`));
    expect(Object.entries(open).map(([k, r]) => [k.split(":").pop(), r.voice])).toEqual([
      ["practice", "split"], ["daw", "combined"], ["karaoke", "combined"], ["streamer", "combined"],
    ]);
    // Then the other version, every mode at one Vocals lane.
    const other = before[1].args.entries;
    expect(Object.keys(other)).toEqual(["practice", "daw", "karaoke", "streamer"].map((m) => `${stems}~${vid}:${m}`));
    for (const rec of Object.values(other)) expect(rec.voice).toBe("combined");
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
