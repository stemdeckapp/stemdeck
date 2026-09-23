// A track with no artwork shows its file's extension, not a generic note.
//
// Uploaded files have no thumbnail, so in a library of them every row looked
// the same. The extension is the one thing about the file the row can say.
// The CSS for this shipped in #665 and the JavaScript that feeds it did not,
// so for a release it styled nothing; these pin the half that went missing.
import { test, expect } from "@playwright/test";
import { openStudio } from "./helpers.mjs";

test.describe("file extension in place of artwork", () => {
  test("the library row shows the extension of an uploaded file", async ({ page }) => {
    await page.setViewportSize({ width: 1600, height: 900 });
    await openStudio(page, { tauri: true });

    const row = page.locator(".cat-item .cat-thumb .thumb-ext").first();
    await expect(row).toHaveText("WAV");
  });

  test("the now-playing square shows it too", async ({ page }) => {
    await page.setViewportSize({ width: 1600, height: 900 });
    await openStudio(page, { tauri: true });

    await expect(page.locator("#np-art .np-art-placeholder")).toHaveAttribute("data-ext", "WAV");
  });

  test("only for a name that has an extension to show", async ({ page }) => {
    await openStudio(page, { tauri: true });
    const got = await page.evaluate(async () => {
      const { extLabel } = await import("/js/catalog.js");
      return {
        flac: extLabel({ sourceUrl: "local:Take 3.flac" }),
        dotted: extLabel({ sourceUrl: "local:my.song.final.mp3" }),
        none: extLabel({ sourceUrl: "local:README" }),
        hidden: extLabel({ sourceUrl: "local:.wav" }),
        junk: extLabel({ sourceUrl: 'local:x.<img src=x>' }),
        url: extLabel({ sourceUrl: "https://youtu.be/abc.mp4" }),
        art: extLabel({ sourceUrl: "local:a.wav", thumb: "/t.jpg" }),
      };
    });
    expect(got).toEqual({
      flac: "FLAC", dotted: "MP3", none: "", hidden: "", junk: "", url: "", art: "",
    });
  });
});
