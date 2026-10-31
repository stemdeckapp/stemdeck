// Export mix, All stems: the zip holds the stems the user extracted for the
// song, in the chosen format, whatever the lanes show. The lanes are
// mode-shaped (the rest is folded into Original), so building the zip from
// them dropped stems: after a Lead + Backing split the zip held one file.
// Vocals is replaced by lead + backing once the split is done (the two carry
// the same signal, and the server refuses them together). Stems the separation
// left on disk but the user did not extract are not in it.

import { test, expect } from "@playwright/test";
import { openStudio, exportUi, resetVersions, JOB_ID } from "./helpers.mjs";

// The fixture song's stems (seed.py): it has no lead/backing split.
const STEMS = ["bass", "drums", "other", "vocals"];
const JOB_URL = new RegExp(`/api/jobs/${JOB_ID}$`);

// The song as the server would describe it after a Lead + Backing split of
// `selected` (the stems chosen at import), or just with `selected` chosen.
async function describeJob(page, { selected, split }) {
  await page.route(JOB_URL, async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    const res = await route.fetch();
    const job = await res.json();
    const extra = split
      ? ["lead_vocals", "backing_vocals"].map((name) => ({
          name,
          url: `/api/jobs/${JOB_ID}/stems/vocals.wav`,
        }))
      : [];
    await route.fulfill({
      response: res,
      json: {
        ...job,
        selected_stems: selected,
        versions: [{ id: "main", name: null, stems: selected, created_at: 0 }],
        vocal_split: split ? "done" : "none",
        stems: [...job.stems, ...extra],
      },
    });
  });
}

// The stems= list of the request the export makes.
async function exportedStems(page, format = "wav") {
  const ui = exportUi(page);
  await ui.open();
  await ui.fmt(format).click();
  const download = page.waitForEvent("download");
  await ui.stems.click();
  return new URL((await download).url());
}

const lanes = (page) =>
  page.evaluate(async () =>
    Object.keys((await import("/js/state.js")).trackIndex)
      .filter((n) => !/^t[kr]_/.test(n))
      .sort()
      .join(","),
  );

// Entry names of a zip, read from its central directory.
function zipNames(buf) {
  let eocd = buf.length - 22;
  while (eocd >= 0 && buf.readUInt32LE(eocd) !== 0x06054b50) eocd--;
  const count = buf.readUInt16LE(eocd + 10);
  let at = buf.readUInt32LE(eocd + 16);
  const names = [];
  for (let i = 0; i < count; i++) {
    const n = buf.readUInt16LE(at + 28);
    names.push(buf.toString("utf8", at + 46, at + 46 + n));
    at += 46 + n + buf.readUInt16LE(at + 30) + buf.readUInt16LE(at + 32);
  }
  return names.sort();
}

async function collapseToVocals(page, want = "original,vocals") {
  await page.locator("#laneStemsBtn").click();
  for (const name of ["drums", "bass", "other"]) {
    const box = page.locator(`#laneStemsPop input[data-stem="${name}"]`);
    if (await box.isChecked()) await box.click();
  }
  await page.locator("#laneStemsPop .stems-pop-apply").click();
  await expect.poll(() => lanes(page), { timeout: 15000 }).toBe(want);
}

test.describe("All stems zip", () => {
  // Extract on the open song makes a version of it on the server (#804).
  test.afterEach(async ({ request }) => {
    await resetVersions(request);
  });
  test.beforeEach(async ({ page }) => {
    await page.addInitScript(() => {
      if (!sessionStorage.getItem("all-stems-spec")) {
        sessionStorage.setItem("all-stems-spec", "1");
        localStorage.setItem("stemdeck.mode", "practice");
      }
    });
  });

  test("(a) only Vocals extracted and split: the lead + backing pair", async ({
    page,
  }) => {
    await describeJob(page, { selected: ["vocals"], split: true });
    await openStudio(page);
    const url = await exportedStems(page);
    expect(url.searchParams.get("stems")).toBe("lead_vocals,backing_vocals");
  });

  test("(b) four stems extracted and split: those, with the pair for Vocals", async ({
    page,
  }) => {
    await describeJob(page, { selected: STEMS, split: true });
    await openStudio(page);
    const url = await exportedStems(page);
    expect(url.searchParams.get("stems")).toBe(
      "lead_vocals,backing_vocals,drums,bass,other",
    );
  });

  test("(b) the lanes down to Vocals change nothing", async ({ page }) => {
    await describeJob(page, { selected: STEMS, split: true });
    await openStudio(page);
    await collapseToVocals(page, "backing_vocals,lead_vocals,original");
    const url = await exportedStems(page);
    expect(url.searchParams.get("stems")).toBe(
      "lead_vocals,backing_vocals,drums,bass,other",
    );
  });

  for (const format of ["wav", "mp3"]) {
    test(`(c)(d) no split, lanes down to Vocals: every extracted stem as ${format}`, async ({
      page,
    }) => {
      await openStudio(page);
      await collapseToVocals(page);
      const url = await exportedStems(page, format);
      expect(url.searchParams.get("format")).toBe(format);
      expect(url.searchParams.get("stems")).toBe("vocals,drums,bass,other");
      // The zip the real backend builds for that request.
      const body = await (await page.request.get(url.href)).body();
      expect(
        zipNames(body).map((n) => n.replace(/^.*_(?=[a-z_]+\.\w+$)/, "")),
      ).toEqual(STEMS.map((s) => `${s}.${format}`));
    });
  }
});
