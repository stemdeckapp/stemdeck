// Settings > Models (static/js/modelsView.js): grouping by purpose, the
// "Used for" line from the per-stem choices, the badge and its size. Pure, so
// it runs without a browser; tests/e2e/models-settings.spec.mjs checks the
// drawing against a stubbed server.
//
// Run:  node tests/js/models-view.test.mjs

import { TRANSLATIONS } from "../../static/js/i18n.js";
import {
  DOWNLOAD_ERRORS,
  GROUPS,
  canDownload,
  downloadErrorText,
  downloadRunning,
  downloadView,
  groupByPurpose,
  isDownloadState,
  hasPlan,
  isModelsAnswer,
  licenceLine,
  modelEntries,
  purposeLine,
  sizeText,
  sourceView,
  stateView,
  usedForLanes,
  usedForLine,
  usedForStems,
} from "../../static/js/modelsView.js";
import { isHttpsLink, linkText, linkedSentence } from "../../static/js/sourceLink.js";

let pass = 0,
  fail = 0;
const check = (name, cond, detail = "") => {
  if (cond) {
    pass++;
    console.log(`PASS  ${name}`);
  } else {
    fail++;
    console.log(`FAIL  ${name}${detail ? "  -- " + detail : ""}`);
  }
};
const eq = (name, actual, expected) =>
  check(name, JSON.stringify(actual) === JSON.stringify(expected), `got ${JSON.stringify(actual)}, want ${JSON.stringify(expected)}`);

const tableT = (table, fallback = TRANSLATIONS.en) => (key, vars = {}) =>
  String(table[key] ?? fallback[key] ?? key).replace(/\{(\w+)\}/g, (_, k) => (k in vars ? String(vars[k]) : `{${k}}`));
const t = tableT(TRANSLATIONS.en);

const entry = (id, group, use, extra = {}) => ({
  id, group, use, name: id, stems: [], license: "MIT", state: "not_downloaded",
  size_bytes: null, download_bytes: null, available: true, ...extra,
});
const ANSWER = {
  models: [
    entry("htdemucs_6s", "separation", "stems", { stems: ["vocals", "drums", "bass", "guitar", "piano", "other"], state: "downloaded", size_bytes: 54_996_327, download_bytes: 54_996_327 }),
    entry("whisper_turbo", "analysis", "lyrics_gpu"),
    entry("basic_pitch", "transcription", "midi", { state: "built_in", license: "Apache-2.0" }),
    entry("kim_mel_roformer", "separation", "stems", { stems: ["vocals"], download_bytes: 913_106_900 }),
    entry("uvr_karaoke_2", "separation", "lead_backing", { stems: ["lead_backing"] }),
    entry("beat_this", "analysis", "beats", { license: null }),
    // Not rows: no name, an unknown group, not an object.
    { id: "x", group: "separation" },
    entry("y", "elsewhere", "stems"),
    null,
  ],
  remove: false,
};

// ── The answer and its rows ──
check("an answer has a models array", isModelsAnswer(ANSWER));
check("an older server's 404 body is not one", !isModelsAnswer({ detail: "Not Found" }));
check("null is not one", !isModelsAnswer(null));
eq("rows: an id, a name and a known group", modelEntries(ANSWER).map((m) => m.id),
  ["htdemucs_6s", "whisper_turbo", "basic_pitch", "kim_mel_roformer", "uvr_karaoke_2", "beat_this"]);

// ── Grouping by purpose ──
const groups = groupByPurpose(ANSWER);
eq("groups in the tab's order", groups.map((g) => g.id), ["separation", "transcription", "analysis"]);
eq("each group keeps the server's order", groups[0].models.map((m) => m.id), ["htdemucs_6s", "kim_mel_roformer", "uvr_karaoke_2"]);
eq("Whisper and the beat model are together", groups[2].models.map((m) => m.id), ["whisper_turbo", "beat_this"]);
eq("a group with nothing in it is left out", groupByPurpose({ models: [entry("a", "analysis", "beats")] }).map((g) => g.id), ["analysis"]);
eq("no answer, no groups", groupByPurpose(null), []);
check("every group title is in the English table", GROUPS.every((g) => g.title in TRANSLATIONS.en));

// ── Used for ──
const SETTINGS = {
  stem_models_resolved: { vocals: "kim_mel_roformer", drums: "htdemucs_6s", bass: "htdemucs_6s", guitar: "htdemucs_6s", piano: "user_aaaaaaaaaaaa" },
  transcribe_piano: "auto",
  transcribe_bass: "off",
};
eq("the stems a model separates for the next job", usedForStems(SETTINGS, "htdemucs_6s"), ["drums", "bass", "guitar"]);
eq("a model of the person's own", usedForStems(SETTINGS, "user_aaaaaaaaaaaa"), ["piano"]);
eq("a model used for nothing", usedForStems(SETTINGS, "htdemucs_ft"), []);
eq("no plan, no stems", usedForStems({}, "htdemucs_6s"), []);
// With no engine set, each lane has its default: Basic Pitch for the vocals
// and the guitar, YourMT3+ for the rest.
eq("Basic Pitch: its lanes not turned off", usedForLanes({ ...SETTINGS, transcribe_guitar: "off" }), ["vocals"]);
eq("Basic Pitch: the vocals and the guitar by default", usedForLanes({}), ["vocals", "guitar"]);
eq("YourMT3+: the drums, the bass, the piano and the other lane by default", usedForLanes({}, "ymt3"), ["drums", "bass", "piano", "other"]);

const [six, , bp, kim, karaoke, beats] = modelEntries(ANSWER);
eq("Used for, in words", usedForLine(six, SETTINGS, t, "en"), "Used for: Drums, Bass, and Guitar.");
eq("Used for one stem", usedForLine(kim, SETTINGS, t, "en"), "Used for: Vocals.");
eq("Used for nothing", usedForLine(entry("htdemucs_ft", "separation", "stems"), SETTINGS, t, "en"), "Not used for any stem now.");
eq("before the settings come, nothing is claimed", usedForLine(six, null, t, "en"), "");
eq("the Karaoke split is always for Lead + Backing", usedForLine(karaoke, null, t, "en"), "Used for: Lead + Backing.");
eq("Basic Pitch names its lanes", usedForLine(bp, SETTINGS, t, "en"), "Used for: Vocals and Guitar.");
const ALL_OFF = Object.fromEntries(["vocals", "drums", "bass", "guitar", "piano", "other"].map((l) => [`transcribe_${l}`, "off"]));
eq("Basic Pitch with every lane off", usedForLine(bp, ALL_OFF, t, "en"), "Not used now: MIDI is off for every lane.");
eq("the beat model has no choice to follow", usedForLine(beats, SETTINGS, t, "en"), "");
const hr = entry("piano_hr", "transcription", "midi_piano", { license: "CC BY 4.0", download_bytes: 171_966_578 });
const HR_ON = { transcribe_piano: "auto", transcribe_bass: "auto", transcribe_piano_engine: "piano_hr", transcribe_bass_engine: "basic_pitch" };
eq("ByteDance Piano Transcription is used for Piano when it is its engine", usedForLine(hr, HR_ON, t, "en"), "Used for: Piano.");
eq("and Basic Pitch then for Vocals, Bass and Guitar", usedForLine(bp, HR_ON, t, "en"), "Used for: Vocals, Bass, and Guitar.");
eq("not the piano's engine: not used", usedForLine(hr, { transcribe_piano: "auto", transcribe_bass: "auto" }, t, "en"), "Not used for any stem now.");
eq("its purpose", purposeLine(hr, t, "en"), "Makes MIDI notes from the Piano lane when ByteDance Piano Transcription is its engine.");
const ym = entry("ymt3", "transcription", "midi_multi", { license: "Apache-2.0", download_bytes: 561_544_628 });
eq("YourMT3+ is used for the lanes whose engine it is", usedForLine(ym, { transcribe_piano: "auto" }, t, "en"), "Used for: Drums, Bass, Piano, and Other.");
eq("YourMT3+'s purpose", purposeLine(ym, t, "en"), "Makes MIDI notes from the Drums, Bass, Guitar, Piano and Other lanes when YourMT3+ is their engine, each keeping only its own instrument.");
eq("YourMT3+'s download size before it is there", stateView(ym, t, "en").text, "Not downloaded, 562 MB to download");
check("an engine choice carries the plan", hasPlan({ transcribe_drums_engine: "ymt3" }));
eq("its download size before it is there", stateView(hr, t, "en").text, "Not downloaded, 172 MB to download");
eq("a model of yours, by id", usedForLine({ id: "user_aaaaaaaaaaaa", use: "stems" }, SETTINGS, t, "en"), "Used for: Piano.");
check("a settings answer with the plan carries it", hasPlan(SETTINGS));
check("a Transcription save carries it too", hasPlan({ transcribe_bass: "off" }));
check("an answer about something else does not", !hasPlan({ port: 8000 }));

// ── Purpose and licence ──
eq("purpose of a separation model", purposeLine(six, t, "en"), "Separates Vocals, Drums, Bass, Guitar, Piano, and Other.");
eq("purpose of the Karaoke split", purposeLine(karaoke, t, "en"), "Splits the vocals into lead and backing, when you choose Lead + Backing.");
eq("purpose of Whisper on a GPU", purposeLine(modelEntries(ANSWER)[1], t, "en"), "Writes the lyrics from the vocals when none are found for a song, on an NVIDIA GPU.");
eq("an unknown use has no purpose line", purposeLine({ use: "other" }, t, "en"), "");
eq("licence line", licenceLine(bp, t), "Licence: Apache-2.0.");
eq("no licence, no line", licenceLine(beats, t), "");

// ── Sizes and the badge ──
eq("55 MB", sizeText(54_996_327, t, "en"), "55 MB");
eq("913 MB", sizeText(913_106_900, t, "en"), "913 MB");
eq("1,618 MB below 10 GB", sizeText(1_617_941_637, t, "en"), "1,618 MB");
eq("GB from 10 GB", sizeText(12_400_000_000, t, "en"), "12.4 GB");
eq("German writes its own separators", sizeText(1_617_941_637, tableT(TRANSLATIONS.de), "de"), "1.618 MB");
eq("no size", sizeText(null, t, "en"), null);
eq("zero is no size", sizeText(0, t, "en"), null);

eq("downloaded, with its size", stateView(six, t, "en"), { state: "downloaded", text: "Downloaded, 55 MB" });
eq("not downloaded, with what it will take", stateView(kim, t, "en"), { state: "not_downloaded", text: "Not downloaded, 913 MB to download" });
eq("not downloaded, size unknown", stateView(beats, t, "en"), { state: "not_downloaded", text: "Not downloaded" });
eq("partly downloaded", stateView(entry("ft", "separation", "stems", { state: "partial", size_bytes: 84_141_271 }), t, "en"), { state: "partial", text: "Partly downloaded, 84 MB" });
eq("built in", stateView(bp, t, "en"), { state: "built_in", text: "Built in" });
eq("unknown", stateView(entry("a", "separation", "stems", { state: "unknown" }), t, "en"), { state: "unknown", text: "Not known" });
eq("a state this page does not know reads as unknown", stateView(entry("a", "separation", "stems", { state: "later" }), t, "en").state, "unknown");
eq("not downloaded and cannot run here", stateView({ ...kim, available: false }, t, "en"), { state: "unavailable", text: "Cannot run here" });
eq("downloaded but cannot run here still says downloaded", stateView({ ...six, available: false }, t, "en").state, "downloaded");

// ── The Source line: author, then the page, linked ──
const demucs = { source: { author: "Meta AI (Rouard, Massa, Défossez)", url: "https://github.com/facebookresearch/demucs" } };
eq("a source: the sentence around the link", sourceView(demucs, t), {
  before: "Source: Meta AI (Rouard, Massa, Défossez), ",
  link: { text: "github.com/facebookresearch/demucs", url: "https://github.com/facebookresearch/demucs" },
  after: "",
});
eq("no source: not verified, with no link", sourceView({ source: null }, t), { before: "Source: not verified", link: null, after: "" });
eq("an entry from an older server: not verified", sourceView({}, t).before, "Source: not verified");
eq("an author but a link that is not https: not verified", sourceView({ source: { author: "A", url: "http://example.org/a" } }, t).link, null);
eq("a javascript address is never a link", sourceView({ source: { author: "A", url: "javascript:alert(1)" } }, t).link, null);
eq("a quote in the address is never a link", isHttpsLink('https://example.org/a"b'), false);
eq("the link text drops the scheme and the trailing slash", linkText("https://zenodo.org/records/4034264/"), "zenodo.org/records/4034264");
// Word order is the language's: French puts a space before the colon, and
// the link lands where its sentence puts it.
const fr = tableT(TRANSLATIONS.fr);
eq("French keeps its own punctuation", sourceView(demucs, fr).before, "Source : Meta AI (Rouard, Massa, Défossez), ");
const before = (k, v) => (k === "x" ? `A ${v.link} B` : "plain");
eq("text after the link is kept", linkedSentence("x", "y", {}, "https://a.example/b", before), { before: "A ", link: { text: "a.example/b", url: "https://a.example/b" }, after: " B" });
eq("a sentence that lost its {link} is shown without one", linkedSentence("x", "y", {}, "https://a.example/b", () => "no link here"), { before: "no link here", link: null, after: "" });

// ── The Download button ──
const fresh = { id: "kim_mel_roformer", state: "not_downloaded", available: true, downloadable: true, download_bytes: 913_106_900 };
check("Download beside Not downloaded", canDownload(fresh));
check("and beside Partly downloaded", canDownload({ ...fresh, state: "partial" }));
check("not once it is downloaded", !canDownload({ ...fresh, state: "downloaded" }));
check("not for a model built in", !canDownload({ ...fresh, state: "built_in" }));
check("not when the state is not known", !canDownload({ ...fresh, state: "unknown" }));
check("not where it cannot run", !canDownload({ ...fresh, available: false }));
check("not when the server has no pin for it", !canDownload({ ...fresh, downloadable: false }));
check("not from an older server", !canDownload({ ...fresh, downloadable: undefined }));
const run = { state: "running", model: "kim_mel_roformer", phase: "download", bytes: 120_400_000, total: 913_106_900, error_code: null };
eq("downloading X of Y MB", downloadView(fresh, run, t, "en"), { active: true, text: "Downloading 120 of 913 MB" });
eq("big numbers are grouped as the language groups them", downloadView({ ...fresh, id: "w" }, { ...run, model: "w", bytes: 1_200_000_000, total: 1_617_941_637 }, t, "en").text, "Downloading 1,200 of 1,618 MB");
eq("before the first byte", downloadView(fresh, { ...run, bytes: 0 }, t, "en").text, "Downloading 0 of 913 MB");
eq("no total from the server: the entry's", downloadView(fresh, { ...run, total: null }, t, "en").text, "Downloading 120 of 913 MB");
eq("no total anywhere", downloadView({ ...fresh, download_bytes: null }, { ...run, total: null }, t, "en").text, "Downloading 120 MB");
eq("YourMT3+ making its weights", downloadView(fresh, { ...run, phase: "prepare" }, t, "en"), { active: true, text: "Preparing the model" });
eq("another model's run says nothing here", downloadView({ ...fresh, id: "other" }, run, t, "en"), null);
eq("a finished run says nothing: the list has the new state", downloadView(fresh, { ...run, state: "done" }, t, "en"), null);
eq("a cancelled run says nothing", downloadView(fresh, { ...run, state: "cancelled" }, t, "en"), null);
eq("a failed run says why, in the page's words", downloadView(fresh, { ...run, state: "failed", error_code: "disk" }, t, "en"), { active: false, error: "There is not enough free disk space for this model. Free some space and try again." });
eq("a code the page does not know is the general one", downloadErrorText("C:/secret", t), "The download did not finish. Try again.");
for (const code of DOWNLOAD_ERRORS) check(`error ${code} has its sentence`, t(`settings.models.downloadError.${code}`) !== `settings.models.downloadError.${code}`);
check("idle is a download state", isDownloadState({ state: "idle" }) && !downloadRunning({ state: "idle" }));
check("running is running", downloadRunning(run));
check("an older server's answer is not one", !isDownloadState(undefined) && !isDownloadState({ state: "later" }));
const de = tableT(TRANSLATIONS.de);
eq("German groups with a dot", downloadView({ ...fresh, id: "w" }, { ...run, model: "w", bytes: 1_200_000_000, total: 1_617_941_637 }, de, "de").text, "Wird heruntergeladen: 1.200 von 1.618 MB");

// ── Every key this module reads is in all ten tables ──
const KEYS = Object.keys(TRANSLATIONS.en).filter((k) => k.startsWith("settings.models.") || k === "settings.tab.models" || k === "settings.desc.models" || k.startsWith("help.set.models"));
check("the keys are there", KEYS.length >= 30, String(KEYS.length));
for (const lang of ["en", "pl", "ja", "zh-Hans", "de", "fr", "es", "pt", "id", "ko"]) {
  const missing = KEYS.filter((k) => !(k in TRANSLATIONS[lang]));
  check(`${lang}: every Models key`, missing.length === 0, missing.join(", "));
  const placeholders = KEYS.filter((k) => {
    const want = (TRANSLATIONS.en[k].match(/\{\w+\}/g) || []).sort().join();
    const got = (String(TRANSLATIONS[lang][k] ?? "").match(/\{\w+\}/g) || []).sort().join();
    return want !== got;
  });
  check(`${lang}: the same placeholders as English`, placeholders.length === 0, placeholders.join(", "));
}
const ptpt = tableT(TRANSLATIONS["pt-PT"], TRANSLATIONS.pt);
eq("European Portuguese says transferido", stateView(six, ptpt, "pt-PT").text, "Transferido, 55 MB");
eq("and falls back to pt where the words are the same", ptpt("settings.models.state.builtIn"), "Incluído");
eq("European Portuguese transfers", downloadView(fresh, run, ptpt, "pt-PT").text, "A transferir 120 de 913 MB");
eq("and its button says Transferir", ptpt("settings.models.download"), "Transferir");

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
