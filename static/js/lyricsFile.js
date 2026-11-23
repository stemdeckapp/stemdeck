// A lyrics file the user picked in the Lyrics tab (lyrics.js, Import .lrc):
// its bytes as text, and the limit the server holds that text to. No DOM
// here, so tests/js/lyrics-file.test.mjs runs it in node.

/** The most characters (code points) of lyrics the server keeps: _TEXT_MAX_CHARS
 * in app/pipeline/lyrics_lookup.py, which POST .../lyrics/import refuses
 * past. Keep the two equal: checked here first, the user is told why. */
export const LYRICS_TEXT_MAX_CHARS = 100_000;

/** The size past which a file cannot be lyrics, so it is not read at all:
 * every encoding read here takes at most 4 bytes a character, plus a byte
 * order mark. Below it, the characters are counted once decoded. */
export const LYRICS_FILE_MAX_BYTES = 4 * LYRICS_TEXT_MAX_CHARS + 4;

/**
 * The text in a lyrics file's bytes, or null when they are not text.
 *
 * A byte order mark says the encoding: UTF-8, or UTF-16 as Windows Notepad
 * saves "Unicode" (read as UTF-8, its ASCII would come out with a NUL after
 * every letter). Without one, UTF-8 when the bytes are valid UTF-8, else
 * GB18030, which a .lrc saved on a Chinese Windows usually is. Bytes that are
 * neither, or text with a NUL in it, are not a lyrics file.
 */
export function decodeLyricsFile(bytes) {
  const decode = (label, view = bytes) => {
    try {
      // fatal: a byte that is not this encoding throws rather than becoming
      // U+FFFD, which would be kept as part of the lyrics.
      return new TextDecoder(label, { fatal: true }).decode(view);
    } catch {
      return null;
    }
  };
  let text;
  if (bytes[0] === 0xff && bytes[1] === 0xfe) text = decode("utf-16le");
  else if (bytes[0] === 0xfe && bytes[1] === 0xff) text = decode("utf-16be");
  // The UTF-8 decoder drops its own byte order mark.
  else text = decode("utf-8") ?? decode("gb18030");
  if (text == null || text.includes("\0")) return null;
  return text;
}

/** Code points in `text`, as Python's len() counts them: an emoji or a rare
 * Han character is two UTF-16 units and one character. */
function charCount(text) {
  let count = 0;
  for (const _ of text) count++;
  return count;
}

/**
 * What a picked file gives to import: { text } to post, or { error } to say
 * why not: "tooLong" (past LYRICS_TEXT_MAX_CHARS), "notText" or "empty".
 * `bytes` is the whole file, a Uint8Array.
 */
export function lyricsFileText(bytes) {
  if (bytes.length > LYRICS_FILE_MAX_BYTES) return { error: "tooLong" };
  const text = decodeLyricsFile(bytes);
  if (text == null) return { error: "notText" };
  if (!text.trim()) return { error: "empty" };
  // Counted only when it could be over: text.length is never fewer.
  if (text.length > LYRICS_TEXT_MAX_CHARS && charCount(text) > LYRICS_TEXT_MAX_CHARS) {
    return { error: "tooLong" };
  }
  return { text };
}

/** The key of the sentence that says why an import was refused, by the
 * server's status: 409 the track is not finished, 404 it is gone, 413 and
 * 422 the text is too long or has no lyrics, anything else a failure to
 * try again. */
export function importErrorKey(status) {
  if (status === 409) return "lyrics.importNotReady";
  if (status === 404) return "lyrics.importGone";
  if (status === 413) return "lyrics.importTooLong";
  if (status === 422) return "lyrics.importNoLyrics";
  return "lyrics.importFailed";
}
