// No footer control may be cut off, hidden under another, or scrolled out of
// sight at the desktop app's default and minimum windows.
//
// footer-fit.spec checks that the strip stops overflowing where it can. This
// checks what a person sees at the sizes the desktop app actually opens at:
// tauri.conf.json's default 1280x820 and minimum 1024x720, plus 1100 and 1440
// either side. With the library open, the tight one-line row is 910px and the
// strip has 671px at 1280, so before the "rows" level the row scrolled sideways
// with no scrollbar showing: Global key was cut mid-label and the click track
// sat behind Export Mix. That was the same in WKWebView and in Chromium.
//
// Measured with bounding boxes. A control counts as clipped when any
// ancestor with overflow other than visible (the strip is one) or the viewport
// shows less of it than its own box.
import { test, expect } from "@playwright/test";
import { openStudio, waitForClickTrack } from "./helpers.mjs";

const SIZES = [
  { width: 1280, height: 820 },
  { width: 1024, height: 720 },
  { width: 1100, height: 820 },
  { width: 1440, height: 900 },
];

const footerProblems = (page) =>
  page.evaluate(() => {
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const isShown = (el) => {
      const cs = getComputedStyle(el);
      if (cs.display === "none" || cs.visibility === "hidden") return false;
      const r = el.getBoundingClientRect();
      return r.width > 0 && r.height > 0;
    };
    const visiblePart = (el) => {
      const r = el.getBoundingClientRect();
      let { left, right, top, bottom } = r;
      for (let a = el.parentElement; a; a = a.parentElement) {
        const cs = getComputedStyle(a);
        const ar = a.getBoundingClientRect();
        if (cs.overflowX !== "visible") {
          left = Math.max(left, ar.left);
          right = Math.min(right, ar.right);
        }
        if (cs.overflowY !== "visible") {
          top = Math.max(top, ar.top);
          bottom = Math.min(bottom, ar.bottom);
        }
      }
      return {
        w: Math.max(0, Math.min(right, vw) - Math.max(left, 0)),
        h: Math.max(0, Math.min(bottom, vh) - Math.max(top, 0)),
      };
    };
    const label = (el) => el.id || `${el.className}`.split(" ")[0] || el.tagName;

    const row = document.querySelector(".footer-row-controls");
    // Popovers are opened on demand and placed over the lanes on purpose; the
    // controls on the row itself are what has to fit.
    const controls = [
      ...row.querySelectorAll(
        "button, select, input, output, .footer-group-label, .footer-elapsed, .footer-total, .footer-loop-hint",
      ),
    ].filter((el) => isShown(el) && !el.closest(".track-chip-panel, .metro-vol-panel, .footer-metro-panel.open"));

    const clipped = [];
    const truncated = [];
    for (const el of controls) {
      const r = el.getBoundingClientRect();
      const v = visiblePart(el);
      if (r.right > vw + 0.5 || v.w < r.width - 1 || v.h < r.height - 1) clipped.push(label(el));
      if (!["INPUT", "SELECT"].includes(el.tagName) && el.scrollWidth > el.clientWidth + 1) truncated.push(label(el));
    }

    const overlaps = [];
    for (let i = 0; i < controls.length; i++) {
      for (let j = i + 1; j < controls.length; j++) {
        const a = controls[i];
        const b = controls[j];
        if (a.contains(b) || b.contains(a)) continue;
        const ra = a.getBoundingClientRect();
        const rb = b.getBoundingClientRect();
        const ox = Math.min(ra.right, rb.right) - Math.max(ra.left, rb.left);
        const oy = Math.min(ra.bottom, rb.bottom) - Math.max(ra.top, rb.top);
        if (ox > 0.5 && oy > 0.5) overlaps.push(`${label(a)} x ${label(b)}`);
      }
    }

    // Export Mix is pinned outside the strip, so it is the control most able
    // to sit on top of another. Asked of the hit test as well as the boxes:
    // whatever is under each control's centre has to be that control.
    const exportBtn = document.querySelector("#t-export-btn").getBoundingClientRect();
    const underExport = controls
      .filter((el) => !el.closest("#footer-export-wrap"))
      .filter((el) => {
        const r = el.getBoundingClientRect();
        return r.left < exportBtn.right && r.right > exportBtn.left && r.top < exportBtn.bottom && r.bottom > exportBtn.top;
      })
      .map(label);
    const notOnTop = controls
      .filter((el) => el.matches("button, select, input"))
      .filter((el) => {
        const r = el.getBoundingClientRect();
        const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
        return !(hit && (el === hit || el.contains(hit)));
      })
      .map(label);

    return { clipped, overlaps, truncated, underExport, notOnTop };
  });

test.describe("footer never clips a control", () => {
  for (const size of SIZES) {
    test(`at ${size.width}x${size.height} with the library open`, async ({ page }) => {
      await page.setViewportSize(size);
      await openStudio(page, { tauri: true });
      await waitForClickTrack(page, { revealOptions: false });

      // footerFit lands on the next frame after any width change.
      await expect
        .poll(() => footerProblems(page), { timeout: 5000 })
        .toEqual({ clipped: [], overlaps: [], truncated: [], underExport: [], notOnTop: [] });
    });
  }

  test("the strip goes back to one line when the room comes back", async ({ page }) => {
    // The rows level is re-measured from the uncollapsed state like the
    // others, so a wide window has to undo it rather than keep a second line.
    await page.setViewportSize({ width: 1280, height: 820 });
    await openStudio(page, { tauri: true });
    await waitForClickTrack(page, { revealOptions: false });
    const levels = () =>
      page.evaluate(() => ({
        strip: [...document.querySelector(".footer-clusters").classList].filter((c) => c.startsWith("collapse-")),
        row: document.querySelector(".footer-row-controls").classList.contains("collapse-stack"),
      }));

    await page.setViewportSize({ width: 2200, height: 900 });
    await expect.poll(levels, { timeout: 5000 }).toEqual({ strip: [], row: false });
  });
});
