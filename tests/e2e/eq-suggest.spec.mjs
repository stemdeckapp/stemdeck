// Suggest EQ and master in the studio (static/js/eq/eqSuggest.js), in muted
// Chromium on the real backend.
//
// The fixture's lanes are steady tones, which a tonal-balance suggestion has
// nothing to say about, so:
// 1. The real endpoint, end to end: the Drum EQ's Suggest asks the server and
//    the card answers; the columns stay aligned with the card open.
// 2. The flow, with the endpoint answering a real suggestion captured from a
//    copy of "Shining Star" (tests/fixtures/suggest_answers.json): the ghost
//    curve is drawn, Preview plays the suggestion and the loudness-matched
//    current without saving, Apply leaves exactly the server's curve on the
//    lane (descriptor and the engine's own nodes), undo and redo, Dismiss.
// 3. The Bass EQ: Apply turns the knobs.
// 4. The Master drawer: Apply sets the master EQ and the preset; undo puts
//    both back.

import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";
import { openStudio } from "./helpers.mjs";

test.use({
  launchOptions: { args: ["--mute-audio", "--autoplay-policy=no-user-gesture-required"] },
  viewport: { width: 1600, height: 900 },
});

const answers = JSON.parse(readFileSync(new URL("../fixtures/suggest_answers.json", import.meta.url), "utf8"));
const FREQS = [40, 63, 100, 160, 250, 400, 630, 1000, 1600, 2500, 4000, 6300, 10000];

const tab = (page, lane) => page.locator(`.eq-tab[data-eq-lane="${lane}"]`);
const mixRow = (page, lane) => page.locator(`.mixer-column .eq-panel-row[data-eq-lane="${lane}"]`);
const waveRow = (page, lane) => page.locator(`.stem-waveform-row.eq-panel-lane[data-eq-lane="${lane}"]`);

function watchErrors(page) {
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("console", (m) => {
    if (m.type() !== "error") return;
    if (/^Failed to load resource/.test(m.text())) return;
    errors.push(m.text());
  });
  return errors;
}

async function stubSuggest(page, answer, route = "suggest/eq") {
  const seen = [];
  await page.route(`**/api/jobs/*/${route}?*`, (r) => {
    seen.push(r.request().url());
    r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(answer) });
  });
  return seen;
}

/** Responses (dB at FREQS): the saved EQ, the engine's own nodes, and an answer's eq. */
async function responses(page, lane, answer) {
  return page.evaluate(async ({ lane, answer, freqs }) => {
    const e = await import("/js/eqEngine.js");
    const m = await import("/js/eqMath.js");
    const rate = e.liveSampleRate();
    const want = answer
      ? m.eqResponseDb(m.normalizeEq({ id: "eq", v: 1, params: { out: answer.eq.out, bands: answer.eq.bands.map((b) => ({ on: true, ...b })) } }), freqs, rate)
      : null;
    return {
      saved: Array.from(e.getFrequencyResponse(lane, freqs)),
      node: Array.from(e.getNodeResponse(lane, freqs) || []),
      want: want ? Array.from(want) : null,
    };
  }, { lane, answer, freqs: FREQS });
}

const maxDiff = (a, b) => Math.max(...a.map((v, i) => Math.abs(v - b[i])));

/** The engine's nodes reach `want` (dB at FREQS) once their 20 ms ramps end. */
async function expectNodes(page, lane, want, tol, label) {
  await expect.poll(async () => {
    const r = await responses(page, lane, null);
    return r.node.length ? maxDiff(r.node, want) : 99;
  }, { message: label, timeout: 3000 }).toBeLessThan(tol);
}
const maxAbs = (a) => Math.max(...a.map(Math.abs));

/**
 * Pixels of the ghost colour (the --eq-ghost token, 125 211 252) on a
 * panel's curve canvas. The graph's curve canvas is transparent, so the
 * colour is exact there and the band handles' blue and teal do not pass;
 * the small curve is opaque, so there it is matched blended (`blended`).
 */
async function ghostPixels(page, selector, blended = false) {
  return page.evaluate(({ sel, blended }) => {
    const cv = document.querySelector(sel);
    if (!cv) return -1;
    const d = cv.getContext("2d").getImageData(0, 0, cv.width, cv.height).data;
    let n = 0;
    for (let i = 0; i < d.length; i += 4) {
      const [r, g, b, a] = [d[i], d[i + 1], d[i + 2], d[i + 3]];
      const hit = blended
        ? a > 100 && r < 140 && g > 150 && b > 190 && b - r > 80 && g - r > 55
        : a > 100 && r < 140 && g > 195 && b > 235;
      if (hit) n++;
    }
    return n;
  }, { sel: selector, blended });
}

async function expectAligned(page, label) {
  const s = await page.evaluate(() => {
    const read = (sel) => [...document.querySelectorAll(sel)]
      .filter((e) => !e.classList.contains("hidden") && e.getBoundingClientRect().height > 0)
      .map((e) => { const b = e.getBoundingClientRect(); return { top: b.top, h: b.height }; })
      .sort((a, b) => a.top - b.top);
    return { mix: read(".mixer-column .lane-header"), wave: read(".stem-waveform-layer > .stem-waveform-row") };
  });
  expect(s.wave.length, `${label}: rows`).toBe(s.mix.length);
  const worst = Math.max(0, ...s.mix.map((r, i) => Math.max(Math.abs(r.top - s.wave[i].top), Math.abs(r.h - s.wave[i].h))));
  expect(worst, `${label}: worst drift ${worst.toFixed(2)} px`).toBeLessThanOrEqual(1);
}

test("Suggest asks the real server and answers in the card; the columns stay aligned", async ({ page }) => {
  const errors = watchErrors(page);
  await openStudio(page);
  await tab(page, "drums").click();
  const btn = mixRow(page, "drums").locator(".eq-suggest-btn");
  await expect(btn).toHaveText("Suggest");
  await expect(btn).toHaveAttribute("aria-expanded", "false");
  const reply = page.waitForResponse((r) => r.url().includes("/suggest/eq?") && r.url().includes("lane=drums"));
  await btn.click();
  const res = await reply;
  expect(res.status()).toBe(200);
  const body = await res.json();
  expect(new URL(res.url()).searchParams.get("layout")).toBe("drum");
  expect(["ok", "quiet"]).toContain(body.status);
  const card = waveRow(page, "drums").locator(".eq-suggest-card");
  await expect(card).toBeVisible();
  await expect(btn).toHaveAttribute("aria-expanded", "true");
  await expect(card.locator(".eq-suggest-status")).not.toHaveText("");
  await expect(card.locator(".eq-suggest-status")).not.toHaveText("Measuring the loud parts of the song...");
  // The card takes the band list's place; the row keeps its height.
  await expect(waveRow(page, "drums").locator(".eq-band-list")).toBeHidden();
  await expect(card.locator(".eq-suggest-taste")).toContainText("taste");
  await expect(card.locator(".eq-suggest-target option")).toHaveCount(3);
  await expectAligned(page, "drum card open");
  // Escape dismisses and gives the focus back to the button.
  await card.locator(".eq-suggest-target").focus();
  await page.keyboard.press("Escape");
  await expect(card).toBeHidden();
  await expect(btn).toBeFocused();
  await expect(waveRow(page, "drums").locator(".eq-band-list")).toBeVisible();
  // Bass EQ: the card sits beside the small curve; still aligned.
  await tab(page, "bass").click();
  await mixRow(page, "bass").locator(".eq-suggest-btn").click();
  await expect(waveRow(page, "bass").locator(".eq-suggest-card")).toBeVisible();
  await expectAligned(page, "bass card open");
  expect(errors).toEqual([]);
});

test("a suggestion is drawn, previewed without saving, applied exactly, undone and redone", async ({ page }) => {
  const errors = watchErrors(page);
  const answer = answers.drum;
  const seen = await stubSuggest(page, answer);
  await openStudio(page);
  await tab(page, "drums").click();
  const curve = '.stem-waveform-row.eq-panel-lane[data-eq-lane="drums"] .eq-curve-canvas';
  await expect(page.locator(curve)).toBeVisible();
  expect(await ghostPixels(page, curve)).toBe(0);
  await mixRow(page, "drums").locator(".eq-suggest-btn").click();
  const card = waveRow(page, "drums").locator(".eq-suggest-card");
  await expect(card.locator(".eq-suggest-status")).toHaveText(`Bands suggested: ${answer.bands.length}`);
  await expect(card.locator(".eq-suggest-why li")).toHaveCount(answer.why.length);
  await expect(card.locator(".eq-suggest-why li").first()).toContainText("dB");
  expect(new URL(seen[0]).searchParams.get("lane")).toBe("drums");
  // The ghost: the suggestion's curve, dashed, in its own colour.
  await expect.poll(() => ghostPixels(page, curve)).toBeGreaterThan(40);

  // Preview: the engine plays the suggestion, nothing is saved.
  await card.locator('[data-listen="suggested"]').click();
  await expect(card.locator('[data-listen="suggested"]')).toHaveAttribute("aria-pressed", "true");
  let r = await responses(page, "drums", answer);
  expect(maxAbs(r.saved)).toBeLessThan(1e-6);
  const want = r.want;
  const zero = FREQS.map(() => 0);
  await expectNodes(page, "drums", want, 0.05, "preview: the suggestion plays");
  // Current, matched in loudness: the flat lane trimmed by the estimate.
  await card.locator('[data-listen="current"]').click();
  await expectNodes(page, "drums", FREQS.map(() => answer.loudness.suggested_change), 0.05, "preview: matched current");
  await card.locator('[data-listen="off"]').click();
  await expectNodes(page, "drums", zero, 0.01, "preview off: the saved EQ");

  // Apply: exactly the server's curve, saved and playing.
  await card.locator('[data-listen="suggested"]').click();
  await card.locator(".eq-suggest-apply").click();
  await expect(card).toBeHidden();
  r = await responses(page, "drums", answer);
  expect(maxDiff(r.saved, r.want)).toBeLessThan(0.01);
  await expectNodes(page, "drums", want, 0.05, "applied: the suggestion plays");
  await expect(waveRow(page, "drums").locator(".eq-band-row")).toHaveCount(answer.bands.length);
  await expect(tab(page, "drums")).toHaveClass(/active/);
  expect(await ghostPixels(page, curve)).toBe(0);

  // Undo, redo: the panel's own history.
  await mixRow(page, "drums").locator(".eq-undo").click();
  r = await responses(page, "drums", null);
  expect(maxAbs(r.saved)).toBeLessThan(1e-6);
  await mixRow(page, "drums").locator(".eq-redo").click();
  r = await responses(page, "drums", answer);
  expect(maxDiff(r.saved, r.want)).toBeLessThan(0.01);

  // Dismiss leaves the EQ as it is and stops a preview.
  await mixRow(page, "drums").locator(".eq-suggest-btn").click();
  await card.locator('[data-listen="current"]').click();
  await card.locator(".eq-suggest-dismiss").click();
  await expect(card).toBeHidden();
  r = await responses(page, "drums", answer);
  expect(maxDiff(r.saved, r.want)).toBeLessThan(0.01);
  await expectNodes(page, "drums", want, 0.05, "dismissed: the saved EQ plays again");
  expect(errors).toEqual([]);
});

test("the Bass EQ applies a suggestion by turning its knobs", async ({ page }) => {
  const answer = answers.bass;
  await stubSuggest(page, answer);
  await openStudio(page);
  await tab(page, "bass").click();
  await mixRow(page, "bass").locator(".eq-suggest-btn").click();
  const card = waveRow(page, "bass").locator(".eq-suggest-card");
  await expect(card.locator(".eq-suggest-apply")).toBeEnabled();
  await expect.poll(() => ghostPixels(page, '.stem-waveform-row.eq-panel-lane[data-eq-lane="bass"] .eq-mini-curve', true)).toBeGreaterThan(20);
  await card.locator(".eq-suggest-apply").click();
  const r = await responses(page, "bass", answer);
  expect(maxDiff(r.saved, r.want)).toBeLessThan(0.01);
  const knobs = await page.evaluate(async (ans) => {
    const pm = await import("/js/eq/eqPedalMath.js");
    const sm = await import("/js/eq/eqSuggestMath.js");
    const e = await import("/js/eqEngine.js");
    return { saved: pm.controlsFromEq("bass", e.getLaneEq("bass")), want: sm.pedalControls("bass", ans) };
  }, answer);
  expect(knobs.saved).toEqual(knobs.want);
});

test("Suggest master sets the master EQ and the preset, and undo puts both back", async ({ page }) => {
  const errors = watchErrors(page);
  const answer = answers.master;
  const seen = await stubSuggest(page, answer, "suggest/master");
  await openStudio(page);
  await page.locator("#t-master-btn").click();
  const drawer = page.locator("#master-drawer");
  await expect(drawer.locator(".eq-graph-panel")).toBeVisible();
  const before = await page.evaluate(async () => {
    const m = await import("/js/masterEngine.js");
    return { on: m.isMasterOn(), preset: m.getMaster().params.preset };
  });
  const btn = drawer.locator(".eq-suggest-btn");
  await expect(btn).toHaveAttribute("title", /master EQ and a loudness preset/);
  await btn.click();
  const card = drawer.locator(".eq-suggest-card");
  await expect(card.locator(".eq-suggest-title")).toHaveText("Suggested master");
  await expect(card.locator(".eq-suggest-why li")).toHaveCount(answer.why.length + 1);
  await expect(card.locator(".eq-suggest-why li").last()).toContainText("LUFS");
  await expect(card.locator(".eq-suggest-target option")).toHaveCount(4);
  await expect(card.locator(".eq-suggest-preset")).toHaveText(/^Loudness: /);
  const q = new URL(seen[0]).searchParams;
  expect(q.get("stems")).toBeTruthy();
  expect(q.get("gains")).toBeTruthy();
  await card.locator(".eq-suggest-apply").click();
  await expect(card).toBeHidden();
  const after = await page.evaluate(async () => {
    const m = await import("/js/masterEngine.js");
    return { on: m.isMasterOn(), params: m.getMaster().params };
  });
  expect(after.on).toBe(true);
  expect(after.params.preset).toBe(answer.master.preset);
  let r = await responses(page, "__master", answer);
  expect(maxDiff(r.saved, r.want)).toBeLessThan(0.01);
  // Undo in the drawer's EQ: the EQ and the master both go back.
  await drawer.locator(".eq-undo").click();
  r = await responses(page, "__master", null);
  expect(maxAbs(r.saved)).toBeLessThan(1e-6);
  const back = await page.evaluate(async () => {
    const m = await import("/js/masterEngine.js");
    return { on: m.isMasterOn(), preset: m.getMaster().params.preset };
  });
  expect(back.on).toBe(before.on);
  expect(back.preset).toBe(before.preset);
  expect(errors).toEqual([]);
});
