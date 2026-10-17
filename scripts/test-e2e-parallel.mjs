#!/usr/bin/env node
// Run the Playwright browser suite as N shards at the same time, each with its
// own backend port and its own throwaway library, so they cannot share state.
//
//   npm run test:e2e:parallel -- [--shards 3] [--base-port 8601] [extra playwright args]
//
// Why this is safe: playwright.config.mjs starts one backend per run from
// STEMDECK_E2E_PORT, and tests/e2e/serve.sh keeps everything it writes under
// STEMDECK_E2E_DIR. Shard i gets port base+i-1 and its own directory under the
// OS temp folder, so there is nothing to share. Files stay whole inside a shard
// (fullyParallel is off), same as the CI split in .github/workflows/ci.yml.
//
// The environment is passed through unchanged (PATH, UV_NO_SYNC,
// UV_PROJECT_ENVIRONMENT, STEMDECK_FFMPEG_DIR), so `uv run` in serve.sh reuses
// the environment you already have and never installs into a shared one.

import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import { mkdirSync, createWriteStream, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const require = createRequire(import.meta.url);

export function parseArgs(argv) {
  const opts = { shards: 3, basePort: 8601, extra: [] };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--shards") opts.shards = Number(argv[++i]);
    else if (a.startsWith("--shards=")) opts.shards = Number(a.slice(9));
    else if (a === "--base-port") opts.basePort = Number(argv[++i]);
    else if (a.startsWith("--base-port=")) opts.basePort = Number(a.slice(12));
    else opts.extra.push(a);
  }
  if (!Number.isInteger(opts.shards) || opts.shards < 1) throw new Error("--shards must be a whole number of 1 or more");
  if (!Number.isInteger(opts.basePort) || opts.basePort < 1024 || opts.basePort + opts.shards > 65535) throw new Error("--base-port is out of range");
  return opts;
}

// Pull the counts and the flaky and failed names out of a Playwright list-reporter log.
export function parseSummary(text) {
  const out = { passed: 0, skipped: 0, failed: 0, flaky: 0, flakyNames: [], failedNames: [] };
  let section = null;
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.replace(/\x1b\[[0-9;]*m/g, "");
    let m = /^\s+(\d+) (passed|skipped|failed|flaky|did not run|interrupted)\b/.exec(line);
    if (m) {
      const key = m[2];
      if (key in out) out[key] = Number(m[1]);
      section = key === "flaky" || key === "failed" ? key : null;
      continue;
    }
    m = /^\s+\[[^\]]+\] .*/.exec(line);
    if (m && section) (section === "flaky" ? out.flakyNames : out.failedNames).push(line.trim());
  }
  return out;
}

const fmt = (ms) => `${Math.floor(ms / 60000)}m${String(Math.round((ms % 60000) / 1000)).padStart(2, "0")}s`;

async function main() {
  const opts = parseArgs(process.argv.slice(2));
  const cli = require.resolve("@playwright/test/cli");
  const root = join(tmpdir(), `stemdeck-e2e-parallel-${process.pid}`);
  mkdirSync(root, { recursive: true });
  const t0 = Date.now();
  console.log(`running ${opts.shards} shards on ports ${opts.basePort}-${opts.basePort + opts.shards - 1}, logs in ${root}`);

  const state = [];
  const runs = [];
  for (let i = 1; i <= opts.shards; i++) {
    const port = opts.basePort + i - 1;
    const dir = join(root, `shard${i}`).split(String.fromCharCode(92)).join("/");
    const st = { i, port, total: 0, done: 0, bad: 0, buf: "", code: null, start: Date.now(), end: null };
    state.push(st);
    const log = createWriteStream(join(root, `shard${i}.log`));
    // Each shard gets its own output folder: Playwright empties it at start, so a
    // shared test-results folder would let one shard delete another's screenshots.
    const args = [cli, "test", `--shard=${i}/${opts.shards}`, "--reporter=list", `--output=${join(root, `results${i}`)}`, ...opts.extra];
    const child = spawn(process.execPath, args, {
      env: { ...process.env, STEMDECK_E2E_PORT: String(port), STEMDECK_E2E_DIR: dir, FORCE_COLOR: "0" },
      stdio: ["ignore", "pipe", "pipe"],
    });
    const onData = (chunk) => {
      log.write(chunk);
      st.buf += chunk.toString();
      let nl;
      while ((nl = st.buf.indexOf("\n")) >= 0) {
        const line = st.buf.slice(0, nl);
        st.buf = st.buf.slice(nl + 1);
        const t = /^Running (\d+) tests?/.exec(line);
        if (t) st.total = Number(t[1]);
        const r = /^\s*(\S+)\s+(\d+) \[[^\]]+\] /.exec(line);
        if (r) {
          st.done++;
          if (/^(x|✘|×)$/.test(r[1])) st.bad++;
        }
      }
    };
    child.stdout.on("data", onData);
    child.stderr.on("data", onData);
    runs.push(new Promise((resolve) => child.on("close", (code) => { st.code = code; st.end = Date.now(); log.end(); resolve(); })));
  }

  const tick = setInterval(() => {
    const parts = state.map((s) => `s${s.i} ${s.code === null ? `${s.done}/${s.total || "?"}` : `done(${s.code})`}${s.bad ? ` bad ${s.bad}` : ""}`);
    console.log(`[${fmt(Date.now() - t0)}] ${parts.join(" | ")}`);
  }, 15000);
  await Promise.all(runs);
  clearInterval(tick);

  const { readFileSync } = await import("node:fs");
  const sum = { passed: 0, skipped: 0, failed: 0, flaky: 0, flakyNames: [], failedNames: [] };
  for (const s of state) {
    const p = parseSummary(readFileSync(join(root, `shard${s.i}.log`), "utf8"));
    for (const k of ["passed", "skipped", "failed", "flaky"]) sum[k] += p[k];
    sum.flakyNames.push(...p.flakyNames);
    sum.failedNames.push(...p.failedNames);
    console.log(`shard ${s.i}: exit ${s.code}, ${p.passed} passed, ${p.skipped} skipped, ${p.failed} failed, ${p.flaky} flaky, ${fmt(s.end - s.start)}`);
  }
  console.log(`TOTAL: ${sum.passed} passed, ${sum.skipped} skipped, ${sum.failed} failed, ${sum.flaky} flaky of ${sum.passed + sum.skipped + sum.failed + sum.flaky}, wall time ${fmt(Date.now() - t0)}`);
  for (const n of sum.failedNames) console.log(`  FAILED ${n}`);
  for (const n of sum.flakyNames) console.log(`  FLAKY ${n}`);
  const bad = state.some((s) => s.code !== 0);
  if (!bad) { try { rmSync(root, { recursive: true, force: true }); } catch { /* keep logs */ } }
  else console.log(`logs kept in ${root}`);
  process.exit(bad ? 1 : 0);
}

import { pathToFileURL } from "node:url";
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) main();
