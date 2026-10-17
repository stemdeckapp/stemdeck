// The take render's video encoder, as the UI words it: the "Encoded with"
// note after a Streamer export, and the Settings "Video encoder" table and
// its Copy text. Pure functions; `t` is i18n.t, passed in so node tests can
// run them without the DOM. The server side is app/pipeline/video_encoder.py.

const FAMILY = {
  h264_nvenc: "nvidia",
  h264_qsv: "intel",
  h264_amf: "amd",
  h264_vaapi: "vaapi",
  h264_videotoolbox: "apple",
  libx264: "cpu",
};

export const CPU_ENCODER = "libx264";

/** "nvidia" | "intel" | "amd" | "vaapi" | "apple" | "cpu", or "" for a name this page does not know. */
export function encoderFamily(name) {
  return FAMILY[name] || "";
}

/** "NVIDIA GPU", "CPU", ...; the raw name for one this page does not know. */
export function encoderName(name, t) {
  const family = encoderFamily(name);
  return family ? t(`encoder.name.${family}`) : String(name || "");
}

/**
 * The note after an export, from the render status ({ encoder, fallback }):
 * the fallback sentence when a GPU attempt failed, else "Encoded with ...",
 * or "" when the server did not say (an older server, a restart).
 */
export function encoderNote(status, t) {
  if (!status || typeof status.encoder !== "string" || !status.encoder) return "";
  if (status.fallback) return t("encoder.note.fallback");
  return t("encoder.note.used", { name: encoderName(status.encoder, t) });
}

/** The Select's label for one GPU encoder: "NVIDIA GPU (h264_nvenc)". */
export function encoderOptionLabel(name, t) {
  return t("encoder.option", { name: encoderName(name, t), encoder: name });
}

/** GPU encoders that passed at either size, in the rows' order. */
export function workingEncoders(rows) {
  return (rows || [])
    .filter((row) => row.encoder !== CPU_ENCODER && !row.disabled)
    .filter((row) => Object.values(row.results || {}).some((r) => r && r.ok))
    .map((row) => row.encoder);
}

/** The Select's options: Auto, CPU only, the working encoders, and the
 * stored choice even when it is not (or no longer) among them. */
export function encoderChoices(rows, current) {
  const list = ["auto", "cpu", ...workingEncoders(rows)];
  if (current && !list.includes(current)) list.push(current);
  return list;
}

/** One cell of the table: "Yes, 2.9x" | "No" | "Not tested". */
export function cellText(result, t) {
  if (!result) return t("settings.export.encoder.untested");
  if (result.ok) {
    const speed = Number.isFinite(result.speed) ? result.speed.toFixed(1) : "?";
    return t("settings.export.encoder.works", { speed });
  }
  if (result.reason === "budget") return t("settings.export.encoder.untested");
  return t("settings.export.encoder.fails");
}

const REASONS = new Set(["missing", "nodevice", "failed", "busy", "timeout", "error", "disabled", "budget"]);

/** Why a row does not work, or "": the first failing size's reason, with
 * ffmpeg's own line after it when there is one. */
export function reasonText(row, t) {
  if (row?.disabled) return t("encoder.reason.disabled");
  const failed = Object.values(row?.results || {}).find((r) => r && !r.ok);
  if (!failed) return "";
  const code = REASONS.has(failed.reason) ? failed.reason : "failed";
  const words = t(`encoder.reason.${code}`);
  return failed.detail ? `${words}: ${failed.detail}` : words;
}

function cellCode(result) {
  if (!result) return "-";
  if (result.ok) return `ok ${Number.isFinite(result.speed) ? result.speed.toFixed(2) : "?"}x`;
  return `no (${result.reason || "failed"})`;
}

/**
 * The table as plain text for a bug report: codes and numbers, not
 * translated words, so the maintainer reads every report the same way.
 */
export function encoderTableText(data) {
  const rows = data?.results || [];
  const lines = [
    "StemDeck video encoder test",
    `ffmpeg: ${data?.ffmpeg || "unknown"}`,
    `os: ${data?.os || "?"} (${data?.platform || "?"})`,
    `setting: ${data?.setting || "?"}, used: ${data?.effective || "?"} (from ${data?.source || "?"})`,
    "",
    ["encoder".padEnd(20), "1080p30".padEnd(18), "4K30".padEnd(18), "reason"].join(""),
  ];
  for (const row of rows) {
    const r = row.results || {};
    const failed = Object.values(r).find((x) => x && !x.ok && x.reason !== "budget");
    const why = row.disabled ? "disabled after failures" : failed ? `${failed.reason}${failed.detail ? `: ${failed.detail}` : ""}` : "";
    lines.push([String(row.encoder).padEnd(20), cellCode(r["1080"]).padEnd(18), cellCode(r["2160"]).padEnd(18), why].join("").trimEnd());
  }
  return lines.join("\n");
}
