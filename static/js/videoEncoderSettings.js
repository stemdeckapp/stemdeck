// Settings > Export > Video encoder: the choice (Auto, CPU only, the GPU
// encoders that passed a test here), the "Test encoders" run, its table and
// the Copy button for bug reports. Server: GET /api/video-encoders (no
// probing), POST /api/video-encoders/test (up to about two minutes), the
// choice through POST /api/settings { video_encoder }.

import { t } from "./i18n.js";
import { encoderChoices, encoderName, encoderOptionLabel, cellText, reasonText, encoderTableText } from "./videoEncoderNote.js";

function optionLabel(value) {
  if (value === "auto") return t("settings.export.encoder.auto");
  if (value === "cpu") return t("settings.export.encoder.cpu");
  return encoderOptionLabel(value, t);
}

function fillSelect(sel, data) {
  const current = data.setting || "auto";
  sel.replaceChildren(
    ...encoderChoices(data.results, current).map((value) => {
      const opt = document.createElement("option");
      opt.value = value;
      opt.textContent = optionLabel(value);
      return opt;
    }),
  );
  sel.value = current;
  // STEMDECK_VIDEO_ENCODER on the server wins over this control.
  sel.disabled = data.source === "env";
}

function fillTable(wrap, data) {
  const rows = data.results || [];
  wrap.hidden = rows.length === 0;
  if (!rows.length) return;
  const table = document.createElement("table");
  table.className = "library-editor-table video-encoder-table";
  const head = document.createElement("tr");
  for (const text of [t("settings.export.encoder.col.encoder"), "1080p", "4K", t("settings.export.encoder.col.reason")]) {
    const th = document.createElement("th");
    th.textContent = text;
    head.append(th);
  }
  const thead = document.createElement("thead");
  thead.append(head);
  const tbody = document.createElement("tbody");
  for (const row of rows) {
    const tr = document.createElement("tr");
    const name = document.createElement("td");
    name.textContent = `${encoderName(row.encoder, t)} (${row.encoder})`;
    tr.append(name);
    for (const size of ["1080", "2160"]) {
      const td = document.createElement("td");
      const res = row.results?.[size];
      td.textContent = cellText(res, t);
      td.classList.toggle("is-ok", Boolean(res?.ok));
      tr.append(td);
    }
    const why = document.createElement("td");
    why.textContent = reasonText(row, t);
    tr.append(why);
    tbody.append(tr);
  }
  table.append(thead, tbody);
  wrap.replaceChildren(table);
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (err) {
    console.warn("[settings] clipboard write failed:", err);
    return false;
  }
}

export async function wireVideoEncoderSetting(overlay, showError) {
  const sel = overlay.querySelector(".set-video-encoder");
  const desc = overlay.querySelector(".set-video-encoder-desc");
  const testBtn = overlay.querySelector(".video-encoder-test");
  const copyBtn = overlay.querySelector(".video-encoder-copy");
  const status = overlay.querySelector(".video-encoder-status");
  const wrap = overlay.querySelector(".video-encoder-table-wrap");
  if (!sel || !testBtn || !wrap) return;
  let data = null;
  let last = "auto";

  const show = (next) => {
    data = next;
    last = next.setting || "auto";
    fillSelect(sel, next);
    fillTable(wrap, next);
    if (copyBtn) copyBtn.hidden = !(next.results || []).length;
    if (desc) {
      desc.textContent = next.source === "env"
        ? t("settings.export.encoder.override", { value: optionLabel(next.effective) })
        : t("settings.export.encoder.desc");
    }
  };

  try {
    const r = await fetch("/api/video-encoders", { cache: "no-store" });
    if (r.ok) show(await r.json());
  } catch (err) {
    console.warn("[settings] video encoders not loaded:", err);
  }

  sel.addEventListener("change", async () => {
    try {
      const r = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ video_encoder: sel.value }),
      });
      if (r.ok) {
        last = (await r.json()).video_encoder || sel.value;
        sel.value = last;
        return;
      }
      showError?.(t("settings.export.encoder.changeFailed"));
    } catch (err) {
      console.warn("[settings] video encoder not saved:", err);
    }
    sel.value = last;
  });

  testBtn.addEventListener("click", async () => {
    testBtn.disabled = true;
    if (status) status.textContent = t("settings.export.encoder.testing");
    try {
      const r = await fetch("/api/video-encoders/test", { method: "POST" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      show(await r.json());
      if (status) status.textContent = "";
    } catch (err) {
      console.warn("[settings] encoder test failed:", err);
      if (status) status.textContent = t("settings.export.encoder.testFailed");
    } finally {
      testBtn.disabled = false;
    }
  });

  copyBtn?.addEventListener("click", async () => {
    if (!data) return;
    const ok = await copyText(encoderTableText(data));
    if (status) status.textContent = ok ? t("settings.export.encoder.copied") : t("settings.export.encoder.copyFailed");
  });
}
