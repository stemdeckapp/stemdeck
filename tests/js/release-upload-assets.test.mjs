// scripts/release/upload-assets.mjs (#1039): uploads into one draft release by
// its id, never edits the release, and stops before a file when the release
// is no longer the validated draft.
//
// No network. A local HTTP server plays the GitHub API, keeps the release
// state, and records every request, so each case can check what was sent.
//
// Run:  node tests/js/release-upload-assets.test.mjs

import http from 'node:http';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { run, Refusal } from '../../scripts/release/upload-assets.mjs';

let pass = 0,
  fail = 0;
const check = (name, cond, detail = '') => {
  if (cond) {
    pass++;
  } else {
    fail++;
    console.error(`FAIL  ${name}${detail ? `\n      ${detail}` : ''}`);
  }
};

const SHA = '1'.repeat(40);
const OTHER = '2'.repeat(40);
const TAG = 'v1.0.0-rc.1';

// ── the fake API ──
let state;
let calls;
const server = http.createServer((req, res) => {
  const chunks = [];
  req.on('data', (c) => chunks.push(c));
  req.on('end', () => {
    const url = new URL(req.url, 'http://x');
    const body = Buffer.concat(chunks);
    calls.push({ method: req.method, path: url.pathname, query: url.search, size: body.length });
    const send = (status, json) => {
      res.writeHead(status, { 'content-type': 'application/json' });
      res.end(json === undefined ? '' : JSON.stringify(json));
    };
    let m;
    if (req.method === 'GET' && (m = url.pathname.match(/^\/repos\/o\/r\/releases\/(\d+)$/))) {
      const rel = state.releases[m[1]];
      return rel ? send(200, rel) : send(404, { message: 'Not Found' });
    }
    if (req.method === 'GET' && url.pathname.startsWith('/repos/o/r/git/matching-refs/tags/')) {
      return send(200, state.tagSha ? [{ ref: `refs/tags/${TAG}` }] : []);
    }
    if (req.method === 'GET' && url.pathname === `/repos/o/r/commits/refs/tags/${TAG}`) {
      return send(200, { sha: state.tagSha });
    }
    if (req.method === 'DELETE' && (m = url.pathname.match(/^\/repos\/o\/r\/releases\/assets\/(\d+)$/))) {
      for (const rel of Object.values(state.releases)) rel.assets = rel.assets.filter((a) => String(a.id) !== m[1]);
      return send(204);
    }
    if (req.method === 'POST' && (m = url.pathname.match(/^\/upload\/repos\/o\/r\/releases\/(\d+)\/assets$/))) {
      if (state.failNextUpload) {
        state.failNextUpload = false;
        return send(502, { message: 'Bad Gateway' });
      }
      const rel = state.releases[m[1]];
      const name = url.searchParams.get('name');
      const asset = { id: ++state.nextAsset, name, size: body.length };
      rel.assets.push(asset);
      state.uploaded.push({ release: m[1], name, bytes: body.toString('utf8') });
      if (state.publishAfterUploads && state.uploaded.length >= state.publishAfterUploads) rel.draft = false;
      return send(201, asset);
    }
    return send(405, { message: `unexpected ${req.method} ${url.pathname}` });
  });
});
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const API = `http://127.0.0.1:${server.address().port}`;

const release = (id, over = {}) => ({
  id,
  tag_name: TAG,
  draft: true,
  prerelease: true,
  target_commitish: 'main',
  upload_url: `${API}/upload/repos/o/r/releases/${id}/assets{?name,label}`,
  assets: [],
  ...over,
});
const reset = (over = {}) => {
  state = { releases: { 42: release(42) }, tagSha: SHA, nextAsset: 100, uploaded: [], ...over };
  calls = [];
};

const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'upload-assets-'));
const fileA = path.join(dir, 'StemDeck-A.zip');
const fileB = path.join(dir, 'StemDeck-B.zip');
fs.writeFileSync(fileA, 'alpha-bytes');
fs.writeFileSync(fileB, 'bravo');

const env = (over = {}) => ({
  GH_TOKEN: 'x',
  API_URL: API,
  REPO: 'o/r',
  RELEASE_ID: '42',
  TAG,
  RUN_SHA: SHA,
  RELEASE_PRERELEASE: 'true',
  ...over,
});

const quiet = async (fn) => {
  const log = console.log;
  const lines = [];
  console.log = (...a) => lines.push(a.join(' '));
  try {
    await fn();
    return { ok: true, lines };
  } catch (e) {
    return { ok: false, error: e, lines };
  } finally {
    console.log = log;
  }
};
const uploads = () => calls.filter((c) => c.method === 'POST');
const edits = () => calls.filter((c) => c.method === 'PATCH' || (c.method === 'POST' && !c.path.startsWith('/upload/')));

// ── 1. both files land on release 42, nothing edits the release ──
reset();
let r = await quiet(() => run(env(), [fileA, fileB]));
check('draft: uploads both files', r.ok, r.error?.message);
check('draft: bytes arrive intact', state.uploaded.map((u) => u.bytes).join('|') === 'alpha-bytes|bravo');
check('draft: every upload goes to release 42', uploads().every((c) => c.path === '/upload/repos/o/r/releases/42/assets'));
check('draft: the release is never edited', edits().length === 0, JSON.stringify(edits()));
check('draft: still a draft and a pre-release', state.releases[42].draft && state.releases[42].prerelease);

// ── 2. a second release with the same tag never receives anything ──
reset({ releases: { 42: release(42), 43: release(43) } });
r = await quiet(() => run(env(), [fileA]));
check('two releases: upload by id succeeds', r.ok, r.error?.message);
check('two releases: release 43 untouched', state.releases[43].assets.length === 0);

// ── 3. an older asset with the same name is replaced ──
reset({ releases: { 42: release(42, { assets: [{ id: 7, name: 'StemDeck-A.zip', size: 3 }] }) } });
r = await quiet(() => run(env(), [fileA]));
check('replace: succeeds', r.ok, r.error?.message);
check('replace: old asset deleted first', calls.some((c) => c.method === 'DELETE' && c.path.endsWith('/assets/7')));
check('replace: one asset of that name remains', state.releases[42].assets.filter((a) => a.name === 'StemDeck-A.zip').length === 1);

// ── 4. published before the upload: nothing is uploaded ──
reset({ releases: { 42: release(42, { draft: false }) } });
r = await quiet(() => run(env(), [fileA]));
check('published: refused', !r.ok && r.error instanceof Refusal, r.error?.message);
check('published: nothing uploaded', uploads().length === 0);
check('published: message says why', /no longer a draft/.test(r.error?.message || ''));

// ── 5. published between two files: the second is not uploaded ──
reset({ publishAfterUploads: 1 });
r = await quiet(() => run(env(), [fileA, fileB]));
check('published mid-way: refused', !r.ok && /no longer a draft/.test(r.error?.message || ''), r.error?.message);
check('published mid-way: only the first file was sent', state.uploaded.length === 1 && state.uploaded[0].name === 'StemDeck-A.zip');
check('published mid-way: names the file that landed', /landed on the published release: StemDeck-A\.zip\./.test(r.error?.message || ''), r.error?.message);

// ── 5b. published during the LAST file's upload: the run fails and names both ──
reset({ publishAfterUploads: 2 });
r = await quiet(() => run(env(), [fileA, fileB]));
check('published during the last upload: run fails', !r.ok && r.error instanceof Refusal, r.error?.message);
check(
  'published during the last upload: names every file that landed',
  /landed on the published release: StemDeck-A\.zip, StemDeck-B\.zip\./.test(r.error?.message || ''),
  r.error?.message,
);
check('published during the last upload: no "still a draft" line', !r.lines.some((l) => /still a draft/.test(l)));
check('published during the last upload: both files were sent, nothing after', state.uploaded.length === 2);

// ── 6. tag, flag and commit changes are refused before any upload ──
reset({ releases: { 42: release(42, { tag_name: 'v1.0.0-rc.2' }) } });
r = await quiet(() => run(env(), [fileA]));
check('tag renamed: refused', !r.ok && /now has the tag/.test(r.error?.message || ''));
reset({ releases: { 42: release(42, { prerelease: false }) } });
r = await quiet(() => run(env(), [fileA]));
check('pre-release flag changed: refused', !r.ok && /pre-release flag/.test(r.error?.message || ''));
reset({ releases: { 42: release(42, { prerelease: false }) } });
r = await quiet(() => run(env({ RELEASE_PRERELEASE: 'false' }), [fileA]));
check('final draft, flag unchanged: uploads', r.ok, r.error?.message);
reset({ tagSha: OTHER });
r = await quiet(() => run(env(), [fileA]));
check('tag moved: refused', !r.ok && /now points at/.test(r.error?.message || ''));
check('tag moved: nothing uploaded', uploads().length === 0);
reset({ tagSha: '', releases: { 42: release(42, { target_commitish: SHA }) } });
r = await quiet(() => run(env(), [fileA]));
check('no tag yet, target is the run commit: uploads', r.ok, r.error?.message);
reset({ tagSha: '', releases: { 42: release(42, { target_commitish: OTHER }) } });
r = await quiet(() => run(env(), [fileA]));
check('no tag yet, retargeted: refused', !r.ok && /target_commitish/.test(r.error?.message || ''));

// ── 7. bad input stops before any request ──
reset();
r = await quiet(() => run(env({ RELEASE_PRERELEASE: '' }), [fileA]));
check('no flag from the guard: refused', !r.ok && calls.length === 0);
r = await quiet(() => run(env({ RELEASE_ID: '42; x' }), [fileA]));
check('non-numeric id: refused', !r.ok && calls.length === 0);
r = await quiet(() => run(env(), [path.join(dir, 'missing.zip')]));
check('missing file: refused before any request', !r.ok && /missing release asset/.test(r.error?.message || '') && calls.length === 0);
r = await quiet(() => run(env(), []));
check('no files: refused', !r.ok);
reset({ releases: { 42: release(42, { upload_url: `${API}/upload/repos/o/r/releases/43/assets{?name,label}` }) } });
r = await quiet(() => run(env(), [fileA]));
check('upload URL for another release: refused', !r.ok && /another release/.test(r.error?.message || ''));
check('upload URL for another release: nothing uploaded', uploads().length === 0);
reset({ releases: { 42: [1] } });
r = await quiet(() => run(env(), [fileA]));
check('release not an object: refused', !r.ok && /release object/.test(r.error?.message || ''));
reset({ releases: {} });
r = await quiet(() => run(env(), [fileA]));
check('release gone: refused', !r.ok && /HTTP 404/.test(r.error?.message || ''));

// ── 8. a failed upload is retried after reading the release again ──
reset({ failNextUpload: true });
r = await quiet(() => run(env(), [fileA]));
check('retry: succeeds on the second attempt', r.ok, r.error?.message);
check('retry: two upload requests', uploads().length === 2);
check(
  'retry: the release was read before each attempt and once after the last file',
  calls.filter((c) => c.method === 'GET' && c.path === '/repos/o/r/releases/42').length === 4,
);

server.close();
fs.rmSync(dir, { recursive: true, force: true });
console.log(`${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
