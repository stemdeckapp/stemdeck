// The voice placeholder: vocals, lead and backing vocals, and recording
// tracks on a microphone show this until the Voice EQ exists. An original
// illustration (a microphone and a soft curve) and one line of text. No EQ
// calls: whatever EQ the lane already has keeps playing and exporting.

import { t } from "../i18n.js";
import { el } from "./eqPanelCommon.js";

const ART = `
<svg class="eq-voice-art" viewBox="0 0 220 120" aria-hidden="true">
  <defs>
    <linearGradient id="eqVoiceFade" x1="0" x2="1" y1="0" y2="0">
      <stop offset="0" stop-color="currentColor" stop-opacity="0"/>
      <stop offset="0.5" stop-color="currentColor" stop-opacity="0.55"/>
      <stop offset="1" stop-color="currentColor" stop-opacity="0"/>
    </linearGradient>
  </defs>
  <path d="M8 86 C 48 86, 62 58, 92 58 S 140 92, 172 74 S 204 66, 212 66"
        fill="none" stroke="url(#eqVoiceFade)" stroke-width="2.5" stroke-linecap="round"/>
  <g class="eq-voice-mic">
    <rect x="96" y="14" width="28" height="48" rx="14" fill="var(--eq-knob-cap)" stroke="currentColor" stroke-width="2"/>
    <path d="M100 30 h20 M100 38 h20 M100 46 h20" stroke="currentColor" stroke-opacity="0.45" stroke-width="1.5" stroke-linecap="round"/>
    <path d="M88 46 v4 a22 22 0 0 0 44 0 v-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
    <path d="M110 72 v16 M98 90 h24" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
  </g>
  <g fill="none" stroke="currentColor" stroke-linecap="round" stroke-opacity="0.5" stroke-width="2">
    <path d="M76 26 a26 26 0 0 0 0 30"/>
    <path d="M66 18 a38 38 0 0 0 0 46" stroke-opacity="0.28"/>
    <path d="M144 26 a26 26 0 0 1 0 30"/>
    <path d="M154 18 a38 38 0 0 1 0 46" stroke-opacity="0.28"/>
  </g>
</svg>`;

export function mountPanel({ body, controls }) {
  const box = el("div", "eq-voice-panel");
  box.innerHTML = ART;
  const text = el("div", "eq-voice-text");
  const title = el("p", "eq-voice-title");
  const sub = el("p", "eq-voice-sub");
  text.append(title, sub);
  box.append(text);
  body.append(box);
  const side = el("p", "eq-voice-side");
  controls.append(side);

  function relabel() {
    title.textContent = t("eq.voice.soon");
    sub.textContent = t("eq.voice.sub");
    side.textContent = t("eq.voice.side");
  }
  relabel();
  return { relabel, refresh() {}, destroy() {} };
}
