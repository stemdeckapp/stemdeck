#!/usr/bin/env node
// Upload release assets into one draft release, addressed by its numeric id
// (#1039). Used by windows-release.yml and macos-release.yml.
//
//   node scripts/release/upload-assets.mjs <file> [<file> ...]
//
// Environment (all required):
//   GH_TOKEN            token with contents: write
//   API_URL             github.api_url, e.g. https://api.github.com
//   REPO                owner/name
//   RELEASE_ID          the id the guard or the tag job validated
//   TAG                 the validated tag
//   RUN_SHA             the commit this run built
//   RELEASE_PRERELEASE  'true' or 'false', the flag the guard or tag job read
//
// Why not softprops/action-gh-release: it looks the release up by tag, so a
// second release with the same tag can receive the files, and it writes the
// draft and prerelease fields back. This script never edits the release
// itself. It only reads it, deletes an older asset with the same file name,
// and uploads.
//
// Before anything is uploaded the release must be a draft with the validated
// tag and pre-release flag, pointing at the commit this run built (the
// existing tag's commit, else target_commitish). Before each file it reads the
// release again by id, and stops without uploading that file if the release
// is no longer such a draft. "Re-run failed jobs" reuses the earlier job's
// result, so these checks are what stops a re-run after publishing.

import fs from 'node:fs';
import path from 'node:path';
import http from 'node:http';
import https from 'node:https';
import { pathToFileURL } from 'node:url';

export class Refusal extends Error {}
const stop = (why) => {
  throw new Refusal(why);
};
const isObject = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);

function request(method, url, { token, headers = {}, body, bodyFile, size } = {}) {
  return new Promise((resolve, reject) => {
    const u = new URL(url);
    const lib = u.protocol === 'http:' ? http : https;
    const req = lib.request(
      u,
      {
        method,
        headers: {
          Authorization: `Bearer ${token}`,
          Accept: 'application/vnd.github+json',
          'X-GitHub-Api-Version': '2022-11-28',
          'User-Agent': 'stemdeck-release-upload',
          ...headers,
        },
      },
      (res) => {
        const chunks = [];
        res.on('data', (c) => chunks.push(c));
        res.on('end', () => {
          const text = Buffer.concat(chunks).toString('utf8');
          let json;
          try {
            json = text ? JSON.parse(text) : undefined;
          } catch {
            json = undefined;
          }
          resolve({ status: res.statusCode, json });
        });
        res.on('error', reject);
      },
    );
    req.on('error', reject);
    if (bodyFile) {
      req.setHeader('Content-Type', 'application/octet-stream');
      req.setHeader('Content-Length', String(size));
      const stream = fs.createReadStream(bodyFile);
      stream.on('error', (e) => req.destroy(e));
      stream.pipe(req);
    } else {
      if (body !== undefined) {
        const data = Buffer.from(JSON.stringify(body));
        req.setHeader('Content-Type', 'application/json');
        req.setHeader('Content-Length', String(data.length));
        req.end(data);
      } else {
        req.end();
      }
    }
  });
}

export function readConfig(env) {
  const { GH_TOKEN, API_URL, REPO, RELEASE_ID, TAG, RUN_SHA, RELEASE_PRERELEASE } = env;
  if (
    !GH_TOKEN ||
    !API_URL ||
    !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(REPO || '') ||
    !/^[0-9]+$/.test(RELEASE_ID || '') ||
    !TAG ||
    !/^[0-9a-f]{40}$/.test(RUN_SHA || '') ||
    !/^(true|false)$/.test(RELEASE_PRERELEASE || '')
  ) {
    stop('the guard set no release id, tag or pre-release flag, or the run has no commit SHA.');
  }
  return {
    token: GH_TOKEN,
    api: API_URL.replace(/\/+$/, ''),
    repo: REPO,
    id: RELEASE_ID,
    tag: TAG,
    sha: RUN_SHA,
    prerelease: RELEASE_PRERELEASE === 'true',
  };
}

async function get(cfg, apiPath) {
  const res = await request('GET', `${cfg.api}/repos/${cfg.repo}/${apiPath}`, { token: cfg.token });
  if (res.status !== 200) stop(`${apiPath} could not be read (HTTP ${res.status}).`);
  return res.json;
}

// Read the release by id and check it is still the validated draft.
export async function readDraft(cfg) {
  const rel = await get(cfg, `releases/${cfg.id}`);
  if (
    !isObject(rel) ||
    String(rel.id) !== cfg.id ||
    typeof rel.tag_name !== 'string' ||
    typeof rel.draft !== 'boolean' ||
    typeof rel.prerelease !== 'boolean' ||
    typeof rel.target_commitish !== 'string' ||
    typeof rel.upload_url !== 'string' ||
    !Array.isArray(rel.assets)
  ) {
    stop(`release ${cfg.id} did not come back as a release object.`);
  }
  if (rel.tag_name !== cfg.tag) stop(`release ${cfg.id} now has the tag ${rel.tag_name}, not ${cfg.tag}.`);
  if (rel.draft !== true) {
    stop(`release ${cfg.tag} (release ${cfg.id}) is no longer a draft. Nothing more is uploaded to a published release.`);
  }
  if (rel.prerelease !== cfg.prerelease) {
    stop(
      `the pre-release flag of ${cfg.tag} changed to ${rel.prerelease} after the guard read ${cfg.prerelease}. Start a new run.`,
    );
  }
  return rel;
}

// The commit the release will be tagged at must be the commit this run built.
export async function checkCommit(cfg, rel) {
  const refs = await get(cfg, `git/matching-refs/tags/${cfg.tag}`);
  if (!Array.isArray(refs)) stop(`the tag lookup for ${cfg.tag} did not come back as a list.`);
  let expected;
  let source;
  if (refs.some((r) => isObject(r) && r.ref === `refs/tags/${cfg.tag}`)) {
    const commit = await get(cfg, `commits/refs/tags/${cfg.tag}`);
    if (!isObject(commit) || typeof commit.sha !== 'string') stop(`the tag ${cfg.tag} did not resolve to a commit.`);
    expected = commit.sha;
    source = `existing tag ${cfg.tag}`;
  } else {
    expected = rel.target_commitish;
    source = 'target_commitish';
  }
  console.log(`Commit this run built: ${cfg.sha}`);
  console.log(`Commit the release points at (${source}): ${expected}`);
  if (expected !== cfg.sha) {
    stop(
      `this run built ${cfg.sha} but ${cfg.tag} now points at ${expected} (${source}). Start a new run instead of re-running this one.`,
    );
  }
}

// The upload endpoint for this release id, from the release object itself.
export function uploadBase(cfg, rel) {
  const base = rel.upload_url.replace(/\{[^}]*\}$/, '');
  if (!base.endsWith(`/repos/${cfg.repo}/releases/${cfg.id}/assets`)) {
    stop(`release ${cfg.id} has an upload URL for another release.`);
  }
  return base;
}

async function uploadOne(cfg, file, attempts) {
  const name = path.basename(file);
  const size = fs.statSync(file).size;
  for (let attempt = 1; ; attempt++) {
    // Read the release again before every attempt: if it was published in
    // the meantime, this file is not uploaded.
    const rel = await readDraft(cfg);
    const old = rel.assets.find((a) => isObject(a) && a.name === name);
    if (old) {
      const del = await request('DELETE', `${cfg.api}/repos/${cfg.repo}/releases/assets/${old.id}`, {
        token: cfg.token,
      });
      if (del.status !== 204 && del.status !== 404) {
        stop(`could not replace the older ${name} on draft ${cfg.tag} (HTTP ${del.status}).`);
      }
      console.log(`Removed the older ${name} from draft ${cfg.tag}.`);
    }
    const url = `${uploadBase(cfg, rel)}?name=${encodeURIComponent(name)}`;
    let res;
    try {
      res = await request('POST', url, { token: cfg.token, bodyFile: file, size });
    } catch (e) {
      res = { status: 0, error: e };
    }
    if (res.status === 201 && isObject(res.json) && res.json.name === name && res.json.size === size) {
      console.log(`Uploaded ${name} (${size} bytes) to draft ${cfg.tag} (release ${cfg.id}), attempt ${attempt}.`);
      return;
    }
    const why = res.error ? res.error.message : `HTTP ${res.status}`;
    if (attempt >= attempts || (res.status >= 400 && res.status < 500 && res.status !== 422)) {
      stop(`could not upload ${name} to draft ${cfg.tag}: ${why}.`);
    }
    console.log(`Upload of ${name} failed (${why}); trying again (${attempt + 1}/${attempts}).`);
  }
}

export async function run(env, files, { attempts = 3 } = {}) {
  const cfg = readConfig(env);
  if (files.length === 0) stop('no files to upload.');
  for (const f of files) {
    if (!fs.existsSync(f) || !fs.statSync(f).isFile()) stop(`missing release asset: ${f}`);
  }
  const rel = await readDraft(cfg);
  console.log(
    `Release ${cfg.id}: tag=${rel.tag_name} draft=${rel.draft} prerelease=${rel.prerelease} target_commitish=${rel.target_commitish}`,
  );
  await checkCommit(cfg, rel);
  uploadBase(cfg, rel);
  console.log(`Decision: UPLOAD ${files.length} files to draft ${cfg.tag} (release ${cfg.id}).`);
  for (const f of files) await uploadOne(cfg, f, attempts);
  console.log(`Done: ${files.length} files on draft ${cfg.tag}. The release was not edited and is still a draft.`);
}

if (import.meta.url === pathToFileURL(process.argv[1] || '').href) {
  // Set the exit code rather than calling process.exit(), so an open socket
  // never aborts Node on Windows.
  run(process.env, process.argv.slice(2)).catch((err) => {
    const prefix = err instanceof Refusal ? 'Refusing to upload: ' : 'Upload failed: ';
    console.log(`::error::${prefix}${err.message}`);
    process.exitCode = 1;
  });
}
