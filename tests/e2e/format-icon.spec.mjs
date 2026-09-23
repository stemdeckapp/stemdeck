// An uploaded track with no artwork shows its file format, not a generic note.
//
// Uploads have no thumbnail, so in a library of them every row looked the
// same. The format is the one thing about the file the row can say. It first
// shipped reading the format out of sourceUrl, which for a real upload is
// "local:<title>" with the extension removed, so it never appeared. It passed
// here because this fixture used "local:e2e-fixture.wav", a shape no upload
// produces (#690). The fixture now has the real shape, and the format comes
// from the server's source_format.
import { test, expect } from "@playwright/test";
import { openStudio, readCatalogState, JOB_ID } from "./helpers.mjs";

test.describe("file format in place of artwork", () => {
  test("the library row shows the format of an uploaded file", async ({ page }) => {
    await page.setViewportSize({ width: 1600, height: 900 });
    await openStudio(page, { tauri: true });

    const icon = page.locator(`.cat-item[data-id="${JOB_ID}"] .cat-thumb .format-icon`);
    await expect(icon).toHaveAttribute("data-format", "wav");
    await expect(icon).toHaveText("WAV");
  });

  test("the now-playing square shows it too, and the note once no track is open", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1600, height: 900 });
    await openStudio(page, { tauri: true });

    const square = page.locator("#np-art .np-art-placeholder");
    await expect(square.locator(".format-icon")).toHaveAttribute("data-format", "wav");

    await page.evaluate(async () => {
      const mod = await import("/js/formatIcon.js");
      mod.paintNowPlayingArt("");
    });
    await expect(square.locator(".format-icon")).toHaveCount(0);
    await expect(square.locator("svg path")).toHaveAttribute("d", /^M9 18V5/);
  });

  test("a track saved before the server reported a format learns it at startup", async ({
    page,
  }) => {
    // The seeded library store has no sourceFormat, as every track saved
    // before #690 does. The server finds it from the kept source.wav.
    await openStudio(page, { tauri: true });

    await expect
      .poll(async () => (await readCatalogState(page)).tracks[JOB_ID].sourceFormat)
      .toBe("wav");
  });

  test("each format has a band colour of its own", async ({ page }) => {
    await openStudio(page, { tauri: true });
    const fills = await page.evaluate(async () => {
      const { formatIconSvg } = await import("/js/formatIcon.js");
      const host = document.createElement("div");
      document.body.append(host);
      const out = {};
      for (const f of ["wav", "flac", "mp3", "m4a", "ogg", "opus", "mp4"]) {
        host.innerHTML = formatIconSvg(f);
        out[f] = getComputedStyle(host.querySelector(".format-icon-band")).fill;
      }
      host.remove();
      return out;
    });
    expect(new Set(Object.values(fills)).size).toBe(7);
    expect(fills.mp3).toBe("rgb(91, 156, 245)");
  });

  test("only for an upload in a format the app accepts", async ({ page }) => {
    await openStudio(page, { tauri: true });
    const got = await page.evaluate(async () => {
      const { trackFormat } = await import("/js/formatIcon.js");
      return {
        flac: trackFormat({ sourceUrl: "local:Take 3", sourceFormat: "flac" }),
        upper: trackFormat({ sourceUrl: "local:Take 3", sourceFormat: "OPUS" }),
        // The title is not where the format lives, whatever it looks like.
        dotted: trackFormat({ sourceUrl: "local:my.song.final.mp3" }),
        unknown: trackFormat({ sourceUrl: "local:x", sourceFormat: "<img src=x>" }),
        url: trackFormat({ sourceUrl: "https://youtu.be/abc", sourceFormat: "mp4" }),
        art: trackFormat({ sourceUrl: "local:a", sourceFormat: "wav", thumb: "/t.jpg" }),
      };
    });
    expect(got).toEqual({ flac: "flac", upper: "opus", dotted: "", unknown: "", url: "", art: "" });
  });
});
