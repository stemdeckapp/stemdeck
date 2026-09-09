// A dropped audio file must be treated as an import, wherever it lands.
//
// Everywhere outside the URL zone used to be left to the browser, which
// navigates to the file. In a tab that is surprising; in the desktop shell
// there is no address bar and no back button, so the window is replaced by the
// webview's bare media player and the only way out is to quit the app (#584).
import { test, expect } from "@playwright/test";
import { openStudio, seedLibrary, stubTauri, stubUpdateCheck } from "./helpers.mjs";

// A real WAV, small enough to be uninteresting. The extension is what the
// filter actually reads.
const WAV = Buffer.concat([
  Buffer.from("RIFF"), Buffer.from([36, 0, 0, 0]), Buffer.from("WAVEfmt "),
  Buffer.from([16, 0, 0, 0, 1, 0, 1, 0, 68, 172, 0, 0, 136, 88, 1, 0, 2, 0, 16, 0]),
  Buffer.from("data"), Buffer.from([0, 0, 0, 0]),
]);

async function dropOn(page, selector, name = "dropped.wav", bytes = WAV) {
  const handle = await page.evaluateHandle(
    ([n, data]) => {
      const dt = new DataTransfer();
      dt.items.add(new File([new Uint8Array(data)], n, { type: "audio/wav" }));
      return dt;
    },
    [name, [...bytes]],
  );
  await page.dispatchEvent(selector, "dragover", { dataTransfer: handle });
  await page.dispatchEvent(selector, "drop", { dataTransfer: handle });
}

// Whether the drop's default action was cancelled, recorded at the end of the
// bubble chain. This, not page.url(), is what has to be asserted: a synthetic
// drop never triggers the browser's real navigation, so a URL check passes with
// the fix ripped out and proves nothing. Cancelling the default is the thing
// that stops the webview replacing the app with its media player.
async function watchDropDefault(page) {
  await page.evaluate(() => {
    window.__dropDefaultPrevented = null;
    window.addEventListener(
      "drop",
      (e) => { window.__dropDefaultPrevented = e.defaultPrevented; },
      { capture: false },
    );
  });
}

const dropWasCancelled = (page) => page.evaluate(() => window.__dropDefaultPrevented);

const pill = (page) =>
  page.evaluate(() => {
    const p = document.getElementById("filePill");
    return {
      visible: p ? !p.classList.contains("hidden") : null,
      name: document.getElementById("fileName")?.textContent || "",
      armed: (document.getElementById("fileInput")?._files || []).length,
    };
  });

test.describe("file drop", () => {
  test("a file dropped on the page body arms the import", async ({ page }) => {
    await seedLibrary(page);
    await stubTauri(page);
    await stubUpdateCheck(page);
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".url-wrap");

    expect((await pill(page)).visible).toBe(false);

    // Deliberately not the URL zone. This is the gesture that used to navigate.
    await dropOn(page, "body");

    const p = await pill(page);
    expect(p.visible, "the import pill is armed").toBe(true);
    expect(p.name).toBe("dropped.wav");
    expect(p.armed).toBe(1);
  });

  test("the drop's default is cancelled, so the webview cannot navigate", async ({ page }) => {
    await seedLibrary(page);
    await stubTauri(page);
    await stubUpdateCheck(page);
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".url-wrap");
    await watchDropDefault(page);

    await dropOn(page, "body");
    await page.waitForTimeout(200);

    expect(await dropWasCancelled(page), "an uncancelled drop is the bug").toBe(true);
    await expect(page.locator(".url-wrap")).toBeVisible();
  });

  test("dropping still works with a track open", async ({ page }) => {
    await openStudio(page, { tauri: true });
    await dropOn(page, ".daw-content", "second.wav");

    const p = await pill(page);
    expect(p.visible).toBe(true);
    expect(p.name).toBe("second.wav");
  });

  test("a non-audio file is refused rather than navigated to", async ({ page }) => {
    await seedLibrary(page);
    await stubTauri(page);
    await stubUpdateCheck(page);
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".url-wrap");
    await watchDropDefault(page);

    await dropOn(page, "body", "notes.txt", Buffer.from("hello"));
    await page.waitForTimeout(200);

    // Refusing it is not enough: an unsupported file the app declines must
    // still not be handed back to the browser to open.
    expect(await dropWasCancelled(page), "a refused file must be cancelled too").toBe(true);
    expect((await pill(page)).visible, "nothing armed").toBe(false);

    // And the refusal has to be visible, in the box the file was aimed at.
    const err = page.locator("#urlDropError");
    await expect(err).toBeVisible();
    await expect(err).toContainText(/supported/i);

    const shown = await err.evaluate((el) => {
      const rgb = getComputedStyle(el).color.match(/\d+/g).map(Number);
      return { rgb, inputHidden: getComputedStyle(document.getElementById("url")).display === "none" };
    });
    // Reddish: red dominant over both other channels rather than a hard-coded
    // hex, so retheming the danger colour does not fail this.
    expect(shown.rgb[0], `red channel in rgb(${shown.rgb})`).toBeGreaterThan(shown.rgb[1] + 40);
    expect(shown.rgb[0]).toBeGreaterThan(shown.rgb[2] + 40);
    expect(shown.inputHidden, "the box says one thing at a time").toBe(true);
  });

  test("a refusal clears itself once something valid arrives", async ({ page }) => {
    await seedLibrary(page);
    await stubTauri(page);
    await stubUpdateCheck(page);
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".url-wrap");

    await dropOn(page, "body", "notes.txt", Buffer.from("hello"));
    await expect(page.locator("#urlDropError")).toBeVisible();

    // A stale refusal sitting next to an armed file would describe the wrong
    // thing entirely.
    await dropOn(page, "body", "good.wav");
    await expect(page.locator("#urlDropError")).toBeHidden();
    expect((await pill(page)).name).toBe("good.wav");
  });

  test("typing dismisses a refusal", async ({ page }) => {
    await seedLibrary(page);
    await stubTauri(page);
    await stubUpdateCheck(page);
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".url-wrap");

    await dropOn(page, "body", "notes.txt", Buffer.from("hello"));
    await expect(page.locator("#urlDropError")).toBeVisible();

    // The input is hidden while the message shows, so the message must not be
    // able to lock the user out of the box it is sitting in.
    await page.locator("#urlDropError").click();
    await page.evaluate(() => {
      const i = document.getElementById("url");
      i.value = "a";
      i.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await expect(page.locator("#urlDropError")).toBeHidden();
    await expect(page.locator("#url")).toBeVisible();
  });

  test("a library drag is not mistaken for a file drop", async ({ page }) => {
    // The library's own drags carry no file list. If the document handler
    // stopped guarding on that, dragging a track would arm the importer.
    await openStudio(page, { tauri: true });
    const handle = await page.evaluateHandle(() => new DataTransfer());
    await page.dispatchEvent("body", "dragover", { dataTransfer: handle });
    await page.dispatchEvent("body", "drop", { dataTransfer: handle });

    expect((await pill(page)).visible).toBe(false);
  });
});
