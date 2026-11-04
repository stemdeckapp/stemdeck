const { invoke } = window.__TAURI__.core;

let _runtimeUnlisten = null;

// The backend always binds all interfaces; network availability is gated live
// by the backend itself (Settings → "Make StemDeck available on your network").
function startBackend() {
  return invoke("start_backend");
}

const statusEl = document.getElementById("status");
const detailsEl = document.getElementById("details");
const retryBtn = document.getElementById("retry");
const skipModelsBtn = document.getElementById("skip-models");
const continueBtn = document.getElementById("continue");
const noticeEl = document.getElementById("notice");
const steps = [...document.querySelectorAll("[data-step]")];

function setStep(name, state) {
  const el = steps.find((item) => item.dataset.step === name);
  if (!el) return;
  el.classList.remove("active", "done", "error");
  if (state) el.classList.add(state);
}

function setStatus(message) {
  statusEl.textContent = message;
}

function showError(error, hint) {
  for (const el of steps) {
    if (el.classList.contains("active")) {
      el.classList.replace("active", "error");
    }
  }
  setStatus("Setup could not complete.");
  const msg = String(error?.message ?? error);
  detailsEl.textContent = hint ? `${msg}\n\n→ ${hint}` : msg;
  detailsEl.classList.remove("hidden");
  retryBtn.classList.remove("hidden");
}

async function runStep(name, fn) {
  setStep(name, "active");
  try {
    const result = await fn();
    setStep(name, "done");
    return result;
  } catch (err) {
    setStep(name, "error");
    throw err;
  }
}

function minDelay(ms) {
  return Promise.all([
    new Promise((r) => setTimeout(r, ms)),
    new Promise((r) => requestAnimationFrame(r)),
  ]);
}

function formatElapsed(startedAt) {
  const seconds = Math.floor((Date.now() - startedAt) / 1000);
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return minutes > 0 ? `${minutes}m ${rest}s` : `${rest}s`;
}

function startProgressStatus(messages) {
  const startedAt = Date.now();
  let messageIndex = 0;

  const update = () => {
    const elapsedSeconds = Math.floor((Date.now() - startedAt) / 1000);
    while (
      messageIndex + 1 < messages.length &&
      elapsedSeconds >= messages[messageIndex + 1].afterSeconds
    ) {
      messageIndex += 1;
    }

    setStatus(`${messages[messageIndex].text} Elapsed: ${formatElapsed(startedAt)}.`);
  };

  update();
  const timer = window.setInterval(update, 1000);
  return () => window.clearInterval(timer);
}

/**
 * Real progress for the pip passes that install CUDA or ROCm acceleration.
 *
 * Those passes move several GB, and until the backend streamed pip's output
 * there was nothing to show but a timer counting up, which reads exactly like
 * the freeze it replaced (#502). The timed messages stay as the opening line
 * and as the fallback for a machine that has everything cached and never
 * downloads anything; the first real byte count takes over from them.
 *
 * Returns a stop function that unregisters the listener and hides the bar.
 */
async function startPipProgress(stopTimedMessages) {
  const progressWrap = document.getElementById("progress-wrap");
  const progressFill = document.getElementById("progress-fill");
  let tookOver = false;

  const unlisten = await window.__TAURI__.event.listen("setup-progress", (event) => {
    const { detail, received, total } = event.payload ?? {};
    // Only now is there something truthful to show, so this is where the
    // elapsed-time messages are retired rather than up front.
    if (!tookOver) {
      tookOver = true;
      stopTimedMessages();
      progressWrap.classList.remove("hidden");
    }
    if (Number.isFinite(received) && Number.isFinite(total) && total > 0) {
      const pct = Math.min(100, Math.round((received / total) * 100));
      progressFill.style.width = `${pct}%`;
      progressFill.classList.remove("indeterminate");
      setStatus(`${detail}... ${(received / 1e6).toFixed(0)} / ${(total / 1e6).toFixed(0)} MB`);
    } else {
      // Resolving, or installing what it already fetched. Real work with no
      // measurable size, which is what indeterminate is for.
      progressFill.classList.add("indeterminate");
      setStatus(`${detail}...`);
    }
  });

  return () => {
    unlisten();
    progressWrap.classList.add("hidden");
    progressFill.classList.remove("indeterminate");
  };
}

// The names app/pipeline/warmup.py gives its models, as this screen says them.
const MODEL_LABELS = {
  demucs: "stem separation",
  beat_this: "beat detection",
  sections: "song sections",
  vocal_split: "karaoke split",
  whisper: "lyrics transcription",
};

function modelsLater(separationMissing) {
  return (
    "StemDeck works without them: each one downloads the first time a feature needs it" +
    (separationMissing ? ", so the first separation downloads the separation model before it starts." : ".")
  );
}

/**
 * What to tell someone whose models did not all arrive (#405), or null when
 * they did. Until this, a missing model only reached the console, and the
 * first sign was a feature that quietly did less.
 */
function modelWarmupNotice(status, skipped) {
  if (status?.noSpace) {
    const gb = (bytes) => (bytes / 1e9).toFixed(1);
    return (
      `Not enough free disk space for the AI models: about ${gb(status.noSpace.needed)} GB ` +
      `needed, ${gb(status.noSpace.free)} GB free, so none was downloaded. StemDeck works ` +
      "without them: each one downloads the first time a feature needs it and there is " +
      "room, so the first separation downloads the separation model before it starts."
    );
  }
  // camelCase, because ModelWarmupStatus is #[serde(rename_all =
  // "camelCase")]. Reading the Rust field names instead gives undefineds,
  // `missing` is empty every time, and this says nothing.
  const missing = [
    ["demucs", status?.demucsReady],
    ["beat_this", status?.beatThisReady],
    ["sections", status?.sectionsReady],
    ["vocal_split", status?.vocalSplitReady],
    ["whisper", status?.whisperReady],
  ]
    .filter(([, ready]) => ready === false)
    .map(([name]) => name);
  if (!missing.length) return null;
  const said = (names) => names.map((name) => MODEL_LABELS[name]).join(", ");
  const later = modelsLater(missing.includes("demucs"));
  if (skipped) return `AI model download skipped, so these were not downloaded: ${said(missing)}. ${later}`;
  const offline = missing.filter((name) => status.offline?.includes(name));
  const failed = missing.filter((name) => !offline.includes(name));
  const causes = [];
  if (offline.length) {
    causes.push(
      "StemDeck could not reach the model download servers (no internet connection, or the " +
        `connection stopped answering), so these AI models were not downloaded: ${said(offline)}.`
    );
  }
  if (failed.length) causes.push(`These AI models could not be downloaded: ${said(failed)}.`);
  return `${causes.join(" ")} ${later} logs/setup.log in the StemDeck data folder has the details.`;
}

/** Shows `text` until the Continue button is pressed. */
function showNotice(text) {
  noticeEl.textContent = text;
  noticeEl.classList.remove("hidden");
  continueBtn.classList.remove("hidden");
  continueBtn.focus();
  return new Promise((resolve) => {
    continueBtn.addEventListener(
      "click",
      () => {
        continueBtn.classList.add("hidden");
        resolve();
      },
      { once: true }
    );
  });
}

/**
 * The model step: downloads the AI models, saying which one it is on, and
 * offers Skip. Returns what to tell the user afterwards, or null.
 *
 * Best-effort, per model: a model that does not download here falls back to
 * its lazy download on first use, so nothing here fails setup.
 */
async function warmupModels() {
  let current = "Starting the AI model download...";
  let since = Date.now();
  const show = () => setStatus(`${current} Elapsed: ${formatElapsed(since)}.`);
  let skipped = false;
  show();
  const timer = window.setInterval(show, 1000);
  let unlisten = null;
  try {
    unlisten = await window.__TAURI__.event.listen("model-warmup-progress", (event) => {
      if (skipped) return;
      const { model, number, count } = event.payload ?? {};
      // Offered from the first model on: only once the warmup has said
      // something is it sure to be running, and so stoppable.
      skipModelsBtn.classList.remove("hidden");
      current = `Getting AI model ${number} of ${count}: ${MODEL_LABELS[model] ?? model}...`;
      since = Date.now();
      show();
    });

    skipModelsBtn.disabled = false;
    skipModelsBtn.onclick = async () => {
      skipModelsBtn.disabled = true;
      skipped = true;
      current = "Skipping the AI model download...";
      show();
      try {
        await invoke("skip_model_warmup");
      } catch (err) {
        skipped = false;
        skipModelsBtn.disabled = false;
        current = `The download could not be stopped (${err}), so it goes on...`;
        show();
      }
    };

    try {
      return modelWarmupNotice(await invoke("warmup_models"), skipped);
    } catch (err) {
      // The warmup could not run at all (no Python), or stopped answering.
      const what = skipped ? "AI model download skipped." : `The AI model download stopped: ${err}.`;
      return `${what} ${modelsLater(true)}`;
    }
  } finally {
    window.clearInterval(timer);
    unlisten?.();
    skipModelsBtn.classList.add("hidden");
    skipModelsBtn.onclick = null;
    setStatus("AI model step finished.");
  }
}

function isMac() {
  return /mac/i.test(navigator.userAgentData?.platform ?? navigator.platform ?? "");
}

// ROCm torch exists for Linux only, so only there does setup look for an AMD
// GPU, and only there may the screen mention one (#825).
function isLinux() {
  return /linux/i.test(navigator.userAgentData?.platform ?? navigator.platform ?? "");
}

/**
 * Puts the CPU torchvision back where a CUDA one sits beside CPU torch (#778).
 *
 * The 0.20.0 Linux packages shipped that pair, and the in-app updater never
 * replaces the Python runtime, so an updated install keeps it until this runs.
 * On a healthy install it returns at once without touching the network. It
 * runs on every launch, including the fast path, and never stops the app from
 * starting: a failed repair is said once and tried again next launch.
 */
async function repairTorchvision() {
  if (isMac()) return;
  const stopPipProgress = await startPipProgress(() => {});
  let result;
  try {
    result = await invoke("repair_torchvision");
  } catch (err) {
    console.warn("torchvision check failed to run:", err);
    return;
  } finally {
    stopPipProgress();
  }
  if (result?.outcome === "failed") {
    setStatus(
      "The karaoke split could not be repaired, so it will not work yet. " +
        "StemDeck will try again next time it starts. Check your internet connection; " +
        "logs/setup.log in the StemDeck data folder has the details."
    );
    await minDelay(6000);
  }
}

async function installRuntimePack(appRoot) {
  const status = await invoke("runtime_pack_status");
  if (!status.manifestReady) {
    throw Object.assign(
      new Error(`Python runtime not found under ${appRoot}.`),
      { hint: "Try reinstalling StemDeck. If the problem persists, check that your disk has at least 2 GB free." }
    );
  }

  const progressWrap = document.getElementById("progress-wrap");
  const progressFill = document.getElementById("progress-fill");

  // lastProgressAt is updated by the SSE handler below; only meaningful
  // during a network download, so it is reset just before download starts.
  let lastReceived = 0;
  let lastProgressAt = Date.now();
  const STALL_WARN_MS = 30_000;

  // Guard against stacked listeners on rapid retry (#146): unlisten any
  // previous registration before creating a new one.
  if (_runtimeUnlisten) { _runtimeUnlisten(); _runtimeUnlisten = null; }

  const unlisten = await window.__TAURI__.event.listen(
    "runtime-download-progress",
    (event) => {
      const { received, total } = event.payload;
      if (received !== lastReceived) {
        lastReceived = received;
        lastProgressAt = Date.now();
      }
      const mb = (received / 1e6).toFixed(0);
      if (total && total > 0) {
        const pct = Math.min(100, Math.round((received / total) * 100));
        progressFill.style.width = `${pct}%`;
        progressFill.classList.remove("indeterminate");
        setStatus(`Downloading StemDeck runtime... ${mb} / ${(total / 1e6).toFixed(0)} MB`);
      } else {
        progressFill.classList.add("indeterminate");
        setStatus(`Downloading StemDeck runtime... ${mb} MB received`);
      }
    }
  );
  _runtimeUnlisten = unlisten;

  progressWrap.classList.remove("hidden");
  progressFill.style.width = "0%";
  progressFill.classList.remove("indeterminate");

  try {
    let verified = false;
    if (status.archiveReady) {
      try {
        setStatus("Runtime archive found locally, verifying...");
        progressWrap.classList.add("hidden");
        await invoke("verify_runtime_pack");
        verified = true;
      } catch {
        // Stale or corrupt archive — fall through to re-download
      }
    }
    if (!verified) {
      progressWrap.classList.remove("hidden");
      setStatus("Downloading StemDeck runtime...");

      // Reset stall baseline when network download is actually about to start (#150).
      lastProgressAt = Date.now();

      // stallTimer starts here, not at top of function, so it doesn't fire
      // during the local verify_runtime_pack path (#150).
      // When a stall is detected it stops startProgressStatus first to avoid
      // both timers writing setStatus concurrently (#149).
      let stopSlowMsg = null;
      const stallTimer = window.setInterval(() => {
        const stallMs = Date.now() - lastProgressAt;
        if (stallMs >= STALL_WARN_MS) {
          if (stopSlowMsg) { stopSlowMsg(); stopSlowMsg = null; }
          setStatus(
            stallMs >= 60_000
              ? "Download appears unreachable — check your internet connection and click Retry."
              : "Download seems slow or stalled — check your internet connection."
          );
        }
      }, 5_000);

      // startProgressStatus is assigned after stallTimer creation; the closure
      // above captures stopSlowMsg by reference, so it sees the updated value.
      stopSlowMsg = startProgressStatus([
        { afterSeconds: 0,  text: "Downloading StemDeck runtime..." },
        { afterSeconds: 30, text: "Still downloading runtime... slow connection detected." },
        { afterSeconds: 90, text: "Still downloading... large file on a slow connection can take a few minutes." },
      ]);

      try {
        await invoke("download_runtime_pack");
      } catch (err) {
        throw Object.assign(
          new Error(String(err)),
          { hint: "Check your internet connection and click Retry. If the problem persists, try a different network." }
        );
      } finally {
        window.clearInterval(stallTimer);
        if (stopSlowMsg) { stopSlowMsg(); }
      }
      progressWrap.classList.add("hidden");
      setStatus("Verifying StemDeck runtime...");
      await invoke("verify_runtime_pack");
    }
    setStatus("Installing StemDeck runtime...");
    const installed = await invoke("extract_runtime_pack");
    if (!installed.runtimeReady) {
      throw Object.assign(
        new Error("Runtime install finished but Python/backend files were not found."),
        { hint: "Your disk may be full or the archive may be corrupt. Free up space and click Retry to re-download." }
      );
    }
  } finally {
    _runtimeUnlisten = null;
    unlisten();
    progressWrap.classList.add("hidden");
  }
}

// What to tell someone whose FFmpeg step just failed.
//
// This used to be one sentence about networks and firewalls, attached to every
// failure of the step. That is right for a download that did not arrive, and
// actively misleading for a download that arrived and then would not run: a
// reporter on an Apple Silicon Mac was told to check their firewall when the
// binary they had was built for Intel (#637).
//
// macOS reports that case as EBADARCH, "Bad CPU type in executable (os error
// 86)". It happens when the per-architecture primary source is unreachable and
// the fallback, which publishes Intel builds only, is used instead. Rosetta
// normally papers over it, but a bare exec() never triggers Rosetta's
// install-on-demand prompt, so a Mac without it just fails.
function ffmpegFailureHint(message) {
  // An Apple Silicon Mac without Rosetta, where StemDeck declined to install
  // an Intel-only build (#771). The message already says what to do, and a
  // network hint under it would be wrong.
  if (/built for Intel Macs/.test(message)) return null;
  // The macOS setup error now ends with its own next steps (#824): a network
  // hint under it would point at the wrong thing when the server was reached.
  if (/Do one of these, then click Retry/.test(message)) return null;
  // An FFmpeg older than 5.1 that the user named: the message says what to do.
  if (/needs FFmpeg \d+\.\d+ or newer/.test(message)) return null;
  const wrongArchitecture = /bad cpu type|os error 86|EBADARCH/i.test(message);
  if (wrongArchitecture) {
    return (
      "The FFmpeg that was downloaded is built for a different kind of Mac than yours, " +
      "so it cannot run. This happens when the Apple Silicon download source is " +
      "unreachable and StemDeck falls back to an Intel-only build. Two ways out: " +
      'install Rosetta with "softwareupdate --install-rosetta" in Terminal, which lets ' +
      "Intel builds run, or point StemDeck at a matching build by setting the " +
      "STEMDECK_FFMPEG_URL environment variable before launching. Deleting the ffmpeg " +
      "folder in StemDeck's data directory before retrying lets it fetch a fresh copy."
    );
  }
  return (
    "If this keeps failing, your network or firewall may be blocking the FFmpeg " +
    "download server. You can point StemDeck at a different FFmpeg build by " +
    "setting the STEMDECK_FFMPEG_URL environment variable before launching, then " +
    "retrying."
  );
}

// Retry can be on screen while setup goes on (a GPU warning does not stop it),
// and a second run beside the first would share the model step's buttons.
let setupRunning = false;

async function runSetup() {
  if (setupRunning) return;
  setupRunning = true;
  try {
    await setup();
  } finally {
    setupRunning = false;
  }
}

async function setup() {
  detailsEl.classList.add("hidden");
  retryBtn.classList.add("hidden");
  noticeEl.classList.add("hidden");
  continueBtn.classList.add("hidden");
  skipModelsBtn.classList.add("hidden");
  for (const step of steps) step.classList.remove("active", "done", "error");

  try {
    setStep("runtime", "active");
    setStatus("Checking Python runtime...");
    let [runtime] = await Promise.all([
      invoke("probe_runtime"),
      minDelay(350),
    ]);

    // Compare the installed runtime against the version this app bundle expects.
    // On a DMG upgrade the old runtime is still fully "ready", so this MUST be
    // checked before the early-return below -- otherwise setup starts the stale
    // backend + frontend and the new release (e.g. new features, version) never
    // takes effect until the runtime is manually cleared.
    const runtimeStatus = await invoke("runtime_pack_status");
    const expectedVersion = runtimeStatus.manifest?.version;
    const installedVersion = runtimeStatus.installedVersion;
    // Mismatch when this build expects a version the installed runtime isn't.
    // An unknown installedVersion (a runtime from a build that never recorded
    // one) also counts, so an upgrade still refreshes it. Self-heals: after one
    // refresh the install records the version and subsequent launches match.
    const versionMismatch = Boolean(expectedVersion) && installedVersion !== expectedVersion;

    // A persisted torch device only counts as settled when it is a positive
    // result ("cuda"/"mps") or the package itself is CPU-only. A CPU device
    // born from a failure (no GPU found, CUDA verify failed) -- or from a build
    // that predates reason tracking -- re-runs the GPU step on this launch, so
    // a single bad first run can't pin the install to CPU forever (#247).
    // Cost when nothing changed: one fast nvidia-smi probe.
    //
    // A GPU that every offered CUDA build refused for having no kernels for it
    // is settled too: trying the same builds again cannot go differently, and
    // did cost about 45 s of download and install on every launch (#733). A
    // new version re-runs setup through versionMismatch below, which is when
    // different builds could be on offer.
    const torchDeviceSettled =
      runtime.torchDevice === "cuda" ||
      runtime.torchDevice === "mps" ||
      (runtime.torchDevice === "cpu" && runtime.torchDeviceReason === "cpu-only-package") ||
      (runtime.torchDevice === "cpu" && runtime.torchDeviceReason === "cuda-unsupported-gpu") ||
      // The same for an AMD GPU no ROCm build has kernels for, known before
      // anything is downloaded. No /dev/kfd access is not settled: a user who
      // fixes their groups gets the GPU on the next launch.
      (runtime.torchDevice === "cpu" && runtime.torchDeviceReason === "rocm-unsupported-gpu");

    if (runtime.pythonReady && runtime.ffmpegReady && torchDeviceSettled && !versionMismatch) {
      for (const step of steps) {
        step.classList.remove("active", "error");
        if (step.dataset.step === "backend") {
          step.classList.remove("done");
        } else {
          step.classList.add("done");
        }
      }
      await runStep("backend", async () => {
        await repairTorchvision();
        setStatus("Runtime is ready. Starting StemDeck backend...");
        const backend = await startBackend();
        setStatus("Opening StemDeck...");
        window.location.replace(backend.url);
      });
      return;
    }

    if (!runtime.pythonReady || versionMismatch) {
      if (versionMismatch) {
        setStatus(`Updating runtime from ${installedVersion || "an older build"} to ${expectedVersion}...`);
      }
      await invoke("ensure_workspace");
      await installRuntimePack(runtime.appRoot);
      runtime = await invoke("probe_runtime");
      if (!runtime.pythonReady) {
        setStep("runtime", "error");
        throw Object.assign(
          new Error(`Python runtime setup failed under: ${runtime.dataDir}`),
          { hint: "Check that your disk has at least 2 GB free and click Retry. If it keeps failing, try reinstalling StemDeck." }
        );
      }
    }
    setStep("runtime", "done");
    setStatus(`Python runtime found at ${runtime.pythonPath}`);
    await minDelay(200);

    let gpuSummary = "";

    await runStep("workspace", () => invoke("ensure_workspace"));

    if (runtime.ffmpegReady) {
      setStep("ffmpeg", "done");
    } else {
      await runStep("ffmpeg", async () => {
        const stopProgress = startProgressStatus([
          {
            afterSeconds: 0,
            text: "Downloading FFmpeg... this can take a few minutes on first run.",
          },
          {
            afterSeconds: 60,
            text: "Still downloading FFmpeg... slow networks or antivirus scans can delay this.",
          },
        ]);

        try {
          const assets = await invoke("ensure_external_assets");
          if (!assets.ffmpegReady) {
            throw new Error(
              "FFmpeg setup did not complete. Check your internet connection and retry."
            );
          }
        } catch (err) {
          // showError (the outer catch-all) reads error.hint, so attach one
          // here rather than only in the generic path -- a network/firewall
          // block on the FFmpeg host is common enough (and retrying alone
          // won't fix it) to deserve a specific next step, not just "retry".
          const message = String(err?.message ?? err);
          const wrapped = new Error(message);
          wrapped.hint = ffmpegFailureHint(message);
          throw wrapped;
        } finally {
          stopProgress();
        }
      });
    }

    await runStep("gpu", async () => {
      const macGPU = isMac();
      const gpuKinds = isLinux() ? "NVIDIA or AMD" : "NVIDIA";
      const stopProgress = startProgressStatus(
        macGPU
          ? [
              {
                afterSeconds: 0,
                text: "Checking Apple Silicon compute support...",
              },
              {
                afterSeconds: 10,
                text: "Verifying MPS acceleration for AI models...",
              },
            ]
          : [
              {
                afterSeconds: 0,
                text: `Checking ${gpuKinds} GPU and compute support...`,
              },
              {
                afterSeconds: 20,
                text: `Installing ${gpuKinds} acceleration if needed... first run can take 5-15 minutes.`,
              },
              {
                afterSeconds: 120,
                text: `Still installing ${gpuKinds} acceleration... GPU Torch packages are large.`,
              },
              {
                afterSeconds: 300,
                text: "Still working... setup should finish or time out automatically.",
              },
            ]
      );
      // macOS never runs a pip pass here, so there is nothing to listen for.
      const stopPipProgress = macGPU ? null : await startPipProgress(stopProgress);

      try {
        const gpu = await invoke("ensure_torch_device");
        if (macGPU) {
          gpuSummary =
            gpu.torchDevice === "mps"
              ? `${gpu.gpuName} acceleration enabled`
              : "MPS acceleration unavailable - stem separation will use CPU";
        } else {
          // NVIDIA through CUDA, or AMD through ROCm on Linux.
          const api = gpu.backend === "rocm" ? "ROCm" : "CUDA";
          const unsupported = gpu.reason === "cuda-unsupported-gpu" || gpu.reason === "rocm-unsupported-gpu";
          if (gpu.gpuDetected && !gpu.cudaVerified && unsupported) {
            showError(
              `GPU detected (${gpu.gpuName}), but none of the ${api} builds StemDeck can install support it - stem separation will use CPU. StemDeck will not try again until the next update.`
            );
          } else if (gpu.gpuDetected && !gpu.cudaVerified && gpu.reason === "rocm-no-kfd-access") {
            showError(
              `GPU detected (${gpu.gpuName}), but StemDeck cannot open /dev/kfd - stem separation will use CPU.\nAdd your user to the render and video groups (sudo usermod -aG render,video $USER), log out and back in, and restart StemDeck.`
            );
          } else if (gpu.gpuDetected && !gpu.cudaVerified) {
            showError(
              `GPU detected (${gpu.gpuName}) but ${api} setup failed - stem separation will use CPU.\nCheck logs/setup.log in the StemDeck data folder for details.`
            );
          }
          gpuSummary = gpu.gpuDetected
            ? gpu.cudaVerified
              ? `${gpu.gpuName} - ${api} ${gpu.cudaVersion} enabled`
              : `${gpu.gpuName} found - falling back to CPU (${api} unverified)`
            : `No ${gpuKinds} GPU - stem separation will use CPU`;
        }
        return gpu;
      } finally {
        stopProgress();
        stopPipProgress?.();
      }
    });

    // After the GPU step, so an NVIDIA install that just got CUDA torch is
    // judged with it in place, and before warmup, which loads the karaoke
    // model through torchvision.
    await repairTorchvision();

    let modelNotice = null;
    await runStep("model", async () => {
      modelNotice = await warmupModels();
    });
    // Before the backend step, which overwrites the status line and then
    // leaves this page: anything said only in passing here was never seen.
    if (modelNotice) await showNotice(modelNotice);

    await runStep("backend", async () => {
      setStatus(gpuSummary ? `${gpuSummary} - starting backend...` : "Starting StemDeck backend...");
      const backend = await startBackend();
      setStatus("Opening StemDeck...");
      window.location.replace(backend.url);
    });
  } catch (error) {
    showError(error, error?.hint);
  }
}

retryBtn.addEventListener("click", runSetup);
runSetup();
