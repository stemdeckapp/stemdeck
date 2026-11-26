// Settings > Models, "Built-in models": every model StemDeck comes with,
// grouped by what it is for (Separation, Transcription, Lyrics and beat grid),
// with its licence, whether it is downloaded and the space it takes, and what
// it is used for now. Read-only: nothing here downloads or removes a model.
// "My models" below it is myModels.js, wired by separationSettings.js.
//
// The group is static HTML in catalog.js (".models-builtin-group"), hidden
// until GET /api/models answers: an older server has no route and no group.
// The words come from the pure modelsView.js.
//
// "Used for" follows the choices in the Separation and Transcription tabs:
// every settings answer catalog.js applies is announced on the overlay as
// "stemdeck:settings-applied" (the Transcription tab's saves too), and a
// saved model choice is announced on the document as STEM_MODELS_CHANGED_EVENT.
// Only the "Used for" lines repaint: the list itself is read once per opening.

import { t, getLanguage } from "./i18n.js";
import { SETTINGS_APPLIED_EVENT } from "./separationSettings.js";
import { STEM_MODELS_CHANGED_EVENT } from "./laneModelView.js";
import {
  groupByPurpose,
  hasPlan,
  isModelsAnswer,
  licenceLine,
  purposeLine,
  sourceView,
  stateView,
  usedForLine,
} from "./modelsView.js";
import { linkedLine } from "./sourceLink.js";

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

export async function wireModelsSettings(overlay) {
  const group = overlay?.querySelector(".models-builtin-group");
  const list = group?.querySelector(".models-inventory");
  if (!group || !list) return;

  let answer = null;
  let settings = null;

  // One row per model. Every word is text: a name a deployment set (a
  // Karaoke model's file name) is never read as HTML.
  const buildRow = (m) => {
    const locale = getLanguage();
    const row = el("div", "settings-row models-row");
    row.dataset.model = m.id;
    row.dataset.group = m.group;
    const text = el("div", "settings-row-text");
    text.append(
      el("div", "settings-row-title models-name", m.name),
      el("div", "settings-row-desc models-purpose", purposeLine(m, t, locale)),
      el("div", "settings-row-desc models-licence", licenceLine(m, t)),
      linkedLine("settings-row-desc models-source", sourceView(m, t)),
      el("div", "settings-row-desc models-used", usedForLine(m, settings, t, locale)),
    );
    const view = stateView(m, t, locale);
    const badge = el("span", "models-state", view.text);
    badge.dataset.state = view.state;
    row.append(text, badge);
    return row;
  };

  const render = () => {
    const groups = groupByPurpose(answer);
    list.replaceChildren(...groups.map((g) => {
      const section = el("div", "models-section");
      section.dataset.group = g.id;
      const id = `modelsGroup-${g.id}`;
      const title = el("h5", "models-group-title", t(g.title));
      title.id = id;
      const card = el("div", "settings-section");
      card.setAttribute("role", "group");
      card.setAttribute("aria-labelledby", id);
      card.append(...g.models.map(buildRow));
      section.append(title, card);
      return section;
    }));
    group.hidden = groups.length === 0;
  };

  // A new choice changes "Used for" only.
  const repaintUsed = () => {
    if (!answer) return;
    const locale = getLanguage();
    for (const row of list.querySelectorAll(".models-row")) {
      const m = answer.models.find((e) => e?.id === row.dataset.model);
      const used = row.querySelector(".models-used");
      if (m && used) used.textContent = usedForLine(m, settings, t, locale);
    }
  };

  const take = (d) => {
    if (!hasPlan(d)) return;
    settings = { ...(settings || {}), ...d };
    repaintUsed();
  };

  overlay.addEventListener(SETTINGS_APPLIED_EVENT, (e) => take(e.detail));
  const onChoice = (e) => {
    if (!overlay.isConnected) {
      document.removeEventListener(STEM_MODELS_CHANGED_EVENT, onChoice);
      return;
    }
    take(e.detail);
  };
  document.addEventListener(STEM_MODELS_CHANGED_EVENT, onChoice);

  const [modelsRes, settingsRes] = await Promise.allSettled([
    fetch("/api/models", { cache: "no-store" }).then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))),
    fetch("/api/settings", { cache: "no-store" }).then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))),
  ]);
  if (settingsRes.status === "fulfilled") take(settingsRes.value);
  else console.warn("[models] the settings could not be read:", settingsRes.reason);
  if (modelsRes.status === "fulfilled" && isModelsAnswer(modelsRes.value)) {
    answer = modelsRes.value;
    render();
  } else {
    // An older server: no route, no group, nothing to say.
    console.info("[models] the built-in list is not offered by this server:", modelsRes.reason?.message ?? "not a models answer");
    group.hidden = true;
  }
}
