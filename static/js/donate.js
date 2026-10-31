// The Donate dialog, opened from the Donate button in the left rail
// (index.html). Laid out like a donate page: a hero on top, the ways to give
// with their amounts on the left, and sharing StemDeck on the right.
//
// The links live in donateLinks.js. A way is live only when its link is a real
// https:// address; until then the arrow is a disabled "Coming soon" and the
// dialog holds no link for it.
//
// The amount (a preset or a custom whole-dollar amount, 2 or more) is a choice
// in the dialog. It reaches the link only through an optional template in
// donateLinks.js that contains {amount}; without one the arrow opens the plain
// link, because a plain payment page does not take an amount from its address.
//
// The live arrow is a plain a[target="_blank"]: the document-level handler in
// main.js routes it through the Tauri open_url command on desktop, and a
// browser opens it in a new tab. Nothing from Stripe or Liberapay is loaded by
// this page. The QR code is drawn by this app's own server (/api/qr), when the
// dialog first opens.

import * as donateLinks from "./donateLinks.js";
import { t, onLanguageChange } from "./i18n.js";

export const REPO_URL = "https://github.com/stemdeckapp/stemdeck";
export const AMOUNT_MIN = 2;
export const AMOUNT_MAX = 10000;
const WAY_NAMES = { stripe: "Stripe", liberapay: "Liberapay" };

/** The address a link opens, or null while it is a placeholder. Only https. */
export function donateHref(value) {
  if (typeof value !== "string" || !value.trim()) return null;
  let url;
  try {
    url = new URL(value.trim());
  } catch (err) {
    console.warn("[donate] not a link, stays Coming soon:", value, err);
    return null;
  }
  return url.protocol === "https:" ? url.href : null;
}

/**
 * Where the arrow goes for this way and amount, or null while the way is a
 * placeholder. The plain link decides whether the way is live; a template with
 * {amount} is used on top of it only when an amount is picked.
 */
export function donateUrl(way, amount, links = donateLinks.DONATE_LINKS, templates = donateLinks.DONATE_LINK_TEMPLATES) {
  const plain = donateHref(links?.[way]);
  if (!plain) return null;
  const tpl = templates?.[way];
  if (Number.isInteger(amount) && amount >= AMOUNT_MIN && typeof tpl === "string" && tpl.includes("{amount}")) {
    const filled = donateHref(tpl.split("{amount}").join(String(amount)));
    if (filled) return filled;
  }
  return plain;
}

/**
 * Read the custom amount field. Only digits are kept (whole US dollars). While
 * typing, an amount below the minimum is not an amount yet; on commit (leaving
 * the field or Enter) it is raised to the minimum, and anything above the
 * maximum is lowered to it. `hint` names why the field changed, if it did.
 */
export function readAmount(raw, commit = false) {
  const text = String(raw ?? "").trim();
  const digits = text.replace(/\D+/g, "").replace(/^0+(?=\d)/, "").slice(0, 6);
  let hint = /\D/.test(text) ? "digits" : null;
  if (!digits) return { value: "", amount: null, hint };
  const n = Number(digits);
  if (commit && n < AMOUNT_MIN) return { value: String(AMOUNT_MIN), amount: AMOUNT_MIN, hint: "min" };
  if (n > AMOUNT_MAX) {
    if (commit) return { value: String(AMOUNT_MAX), amount: AMOUNT_MAX, hint: "max" };
    return { value: digits, amount: null, hint: "max" };
  }
  return { value: digits, amount: n >= AMOUNT_MIN ? n : null, hint };
}

const HINT_KEYS = { digits: "donate.hintDigits", min: "donate.hintMin", max: "donate.hintMax" };

function focusables(root) {
  return [...root.querySelectorAll("button, [href], input, select, textarea, [tabindex]")].filter(
    (el) => el.tabIndex >= 0 && !el.disabled && !el.hidden && el.getClientRects().length > 0,
  );
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (err) {
    console.warn("[donate] clipboard write failed:", err);
    return false;
  }
}

export function wireDonateDialog() {
  const btn = document.getElementById("donateBtn");
  const dialog = document.getElementById("donateDialog");
  const close = document.getElementById("donateClose");
  if (!btn || !dialog || dialog.dataset.ready === "1") return;

  const tiles = [...dialog.querySelectorAll(".donate-way[data-way]")];
  const presets = [...dialog.querySelectorAll(".donate-preset[data-amount]")];
  const custom = document.getElementById("donateCustom");
  const go = document.getElementById("donateGo");
  const soonWrap = document.getElementById("donateSoonWrap");
  const hintEl = document.getElementById("donateHint");
  const qr = document.getElementById("donateQr");
  const copyBtn = document.getElementById("donateCopy");
  const shareBtn = document.getElementById("donateShare");

  const state = { way: tiles[0]?.dataset.way || "stripe", amount: null, fromPreset: false, hint: null };

  const render = () => {
    for (const tile of tiles) tile.setAttribute("aria-pressed", String(tile.dataset.way === state.way));
    for (const p of presets) {
      p.setAttribute("aria-pressed", String(state.fromPreset && Number(p.dataset.amount) === state.amount));
    }
    const href = donateUrl(state.way, state.amount);
    dialog.classList.toggle("is-soon", !href);
    if (go) {
      if (href) go.href = href;
      else go.removeAttribute("href");
      go.hidden = !href;
      const label = t("donate.go", { way: WAY_NAMES[state.way] || state.way });
      go.setAttribute("aria-label", label);
      go.title = label;
    }
    if (soonWrap) soonWrap.hidden = Boolean(href);
    if (hintEl) {
      hintEl.hidden = !state.hint;
      hintEl.textContent = state.hint
        ? t(HINT_KEYS[state.hint], { min: AMOUNT_MIN, max: AMOUNT_MAX.toLocaleString("en-US") })
        : "";
    }
    if (qr) qr.alt = t("donate.qrAlt");
  };

  for (const tile of tiles) {
    tile.addEventListener("click", () => {
      state.way = tile.dataset.way;
      render();
    });
  }
  for (const p of presets) {
    p.addEventListener("click", () => {
      state.amount = Number(p.dataset.amount);
      state.fromPreset = true;
      state.hint = null;
      if (custom) custom.value = "";
      render();
    });
  }
  const readCustom = (commit) => {
    const res = readAmount(custom.value, commit);
    if (custom.value !== res.value) custom.value = res.value;
    state.hint = res.hint;
    if (res.value || !state.fromPreset) {
      state.amount = res.amount;
      state.fromPreset = false;
    }
    render();
  };
  custom?.addEventListener("input", () => readCustom(false));
  custom?.addEventListener("change", () => readCustom(true));
  custom?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      readCustom(true);
    }
  });

  let copyTimer = null;
  copyBtn?.addEventListener("click", async () => {
    const ok = await copyText(REPO_URL);
    const label = copyBtn.querySelector(".donate-sq-label");
    copyBtn.classList.toggle("is-done", ok);
    if (label) label.textContent = t(ok ? "donate.copied" : "donate.copyFailed");
    clearTimeout(copyTimer);
    copyTimer = setTimeout(() => {
      copyBtn.classList.remove("is-done");
      if (label) label.textContent = t("donate.copy");
    }, 1600);
  });

  // System share where the platform has it; otherwise the button stays hidden.
  const canShare = typeof navigator.share === "function";
  shareBtn?.parentElement?.classList.toggle("is-three", !canShare);
  if (shareBtn && canShare) {
    shareBtn.hidden = false;
    shareBtn.addEventListener("click", async () => {
      try {
        await navigator.share({ title: "StemDeck", url: REPO_URL });
      } catch (err) {
        if (err?.name !== "AbortError") console.warn("[donate] share failed:", err);
      }
    });
  }

  render();
  onLanguageChange(render);
  dialog.dataset.ready = "1";

  let opener = null;
  const open = () => {
    opener = document.activeElement instanceof HTMLElement ? document.activeElement : btn;
    if (qr && !qr.getAttribute("src")) qr.src = `/api/qr?url=${encodeURIComponent(REPO_URL)}`;
    dialog.classList.remove("hidden");
    close?.focus();
  };
  const hide = () => {
    if (dialog.classList.contains("hidden")) return;
    dialog.classList.add("hidden");
    // Back to whatever opened it: the Donate button in the rail.
    const back = opener?.isConnected && opener.getClientRects().length ? opener : btn;
    back.focus();
    opener = null;
  };

  btn.addEventListener("click", open);
  close?.addEventListener("click", hide);
  dialog.addEventListener("mousedown", (e) => {
    if (e.target !== dialog) return;
    // Kept from moving focus to the page, so it can go back to the opener.
    e.preventDefault();
    hide();
  });
  dialog.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      e.stopPropagation();
      hide();
      return;
    }
    if (e.key !== "Tab") return;
    const items = focusables(dialog);
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (e.shiftKey && (document.activeElement === first || !dialog.contains(document.activeElement))) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  });
}
