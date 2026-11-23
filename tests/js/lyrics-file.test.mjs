// A .lrc or .txt picked in the Lyrics tab (#1083): its bytes as text, and the
// limit the server holds that text to. What the server keeps of the text is
// tested in tests/test_lyrics_api.py.
//
// Run:  node tests/js/lyrics-file.test.mjs

import { readFileSync } from "node:fs";

import {
  LYRICS_FILE_MAX_BYTES,
  LYRICS_TEXT_MAX_CHARS,
  decodeLyricsFile,
  importErrorKey,
  lyricsFileText,
} from "../../static/js/lyricsFile.js";

let pass = 0,
  fail = 0;
const check = (name, cond, detail = "") => {
  if (cond) {
    pass++;
  } else {
    fail++;
    console.log(`FAIL  ${name}${detail ? "  -- " + detail : ""}`);
  }
};

const utf8 = (text) => new TextEncoder().encode(text);
const bytes = (...list) => Uint8Array.from(list);
const cat = (...parts) => Uint8Array.from(parts.flatMap((p) => [...p]));

// UTF-8, as most .lrc files are.
{
  const text = "[00:01.00]Ça va\n[00:02.00]こんにちは";
  check("UTF-8 is read as UTF-8", lyricsFileText(utf8(text)).text === text);
}

// GB18030, as a .lrc saved on a Chinese Windows is: 你好世界, not valid UTF-8.
{
  const gb = cat(utf8("[00:01.00]"), bytes(0xc4, 0xe3, 0xba, 0xc3, 0xca, 0xc0, 0xbd, 0xe7));
  const read = lyricsFileText(gb);
  check("GB18030 bytes are read as GB18030", read.text === "[00:01.00]你好世界", JSON.stringify(read));
}

// A byte order mark is not part of the lyrics, and says the encoding.
{
  const bom = cat(bytes(0xef, 0xbb, 0xbf), utf8("[00:01.00]one"));
  check("a UTF-8 BOM is dropped", lyricsFileText(bom).text === "[00:01.00]one");
  // Windows Notepad's "Unicode": UTF-16LE with its mark. Read as UTF-8, every
  // letter would come with a NUL.
  const le = cat(bytes(0xff, 0xfe), ..."[0".split("").map((c) => bytes(c.charCodeAt(0), 0)));
  check("UTF-16LE with its mark is read as UTF-16", lyricsFileText(le).text === "[0");
  const be = cat(bytes(0xfe, 0xff), ..."ab".split("").map((c) => bytes(0, c.charCodeAt(0))));
  check("UTF-16BE with its mark is read as UTF-16", lyricsFileText(be).text === "ab");
}

// Nothing to keep.
check("an empty file is empty", lyricsFileText(new Uint8Array(0)).error === "empty");
check("a file of blank lines is empty", lyricsFileText(utf8(" \r\n\t\n　")).error === "empty");
check("a UTF-8 BOM alone is empty", lyricsFileText(bytes(0xef, 0xbb, 0xbf)).error === "empty");

// Not text: bytes that are neither UTF-8 nor GB18030, or carry a NUL.
{
  // A PNG's signature: 0x89 then "PNG", then CR LF 0x1A LF.
  const png = bytes(0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00, 0x00, 0x00, 0x0d, 0xff, 0x80);
  check("an image is not text", lyricsFileText(png).error === "notText", JSON.stringify(lyricsFileText(png)));
  check("text with a NUL is not text", lyricsFileText(utf8("[00:01.00]a\0b")).error === "notText");
  check("decodeLyricsFile says null for it", decodeLyricsFile(bytes(0x80, 0xff, 0x00)) === null);
}

// Too long: past the server's own limit, counted as the server counts.
{
  const most = "x".repeat(LYRICS_TEXT_MAX_CHARS);
  check("the most characters are allowed", lyricsFileText(utf8(most)).text === most);
  check("one more is too long", lyricsFileText(utf8(most + "x")).error === "tooLong");
  // Three bytes each: the size check alone would let this through.
  const han = "字".repeat(LYRICS_TEXT_MAX_CHARS + 1);
  check("CJK past the limit is too long", lyricsFileText(utf8(han)).error === "tooLong");
  // An emoji is two UTF-16 units and one character on the server.
  const emoji = "🎵".repeat(LYRICS_TEXT_MAX_CHARS);
  check("characters are code points, not UTF-16 units", lyricsFileText(utf8(emoji)).text === emoji);
  check(
    "a file past the size no lyrics reach is too long before it is read",
    lyricsFileText(new Uint8Array(LYRICS_FILE_MAX_BYTES + 1)).error === "tooLong",
  );
}

// The page's limit is the server's.
{
  const py = readFileSync(new URL("../../app/pipeline/lyrics_lookup.py", import.meta.url), "utf8");
  const m = /^_TEXT_MAX_CHARS = ([\d_]+)$/m.exec(py);
  check("LYRICS_TEXT_MAX_CHARS equals _TEXT_MAX_CHARS", m && Number(m[1].replaceAll("_", "")) === LYRICS_TEXT_MAX_CHARS, m?.[1]);
}

// What each refusal of POST .../lyrics/import says.
check("409: not finished", importErrorKey(409) === "lyrics.importNotReady");
check("404: gone", importErrorKey(404) === "lyrics.importGone");
check("413: too long", importErrorKey(413) === "lyrics.importTooLong");
check("422: no lyrics, or too long", importErrorKey(422) === "lyrics.importNoLyrics");
check("500: failed", importErrorKey(500) === "lyrics.importFailed");

console.log(`${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
