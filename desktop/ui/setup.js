const { invoke } = window.__TAURI__.core;

let _runtimeUnlisten = null;

const statusEl = document.getElementById("status");
const detailsEl = document.getElementById("details");
const retryBtn = document.getElementById("retry");
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

function isMac() {
  return /mac/i.test(navigator.userAgentData?.platform ?? navigator.platform ?? "");
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

async function runSetup() {
  detailsEl.classList.add("hidden");
  retryBtn.classList.add("hidden");
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

    if (runtime.pythonReady && runtime.ffmpegReady && runtime.torchDevice && !versionMismatch) {
      for (const step of steps) {
        step.classList.remove("active", "error");
        if (step.dataset.step === "backend") {
          step.classList.remove("done");
        } else {
          step.classList.add("done");
        }
      }
      await runStep("backend", async () => {
        setStatus("Runtime is ready. Starting StemDeck backend...");
        const backend = await invoke("start_backend");
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
        } finally {
          stopProgress();
        }
      });
    }

    await runStep("gpu", async () => {
      const macGPU = isMac();
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
                text: "Checking NVIDIA GPU and compute support...",
              },
              {
                afterSeconds: 20,
                text: "Installing NVIDIA acceleration if needed... first run can take 5-15 minutes.",
              },
              {
                afterSeconds: 120,
                text: "Still installing NVIDIA acceleration... CUDA Torch packages are large.",
              },
              {
                afterSeconds: 300,
                text: "Still working... setup should finish or time out automatically.",
              },
            ]
      );

      try {
        const gpu = await invoke("ensure_torch_device");
        if (macGPU) {
          gpuSummary =
            gpu.torchDevice === "mps"
              ? `${gpu.gpuName} acceleration enabled`
              : "MPS acceleration unavailable - stem separation will use CPU";
        } else {
          gpuSummary = gpu.gpuDetected
            ? gpu.cudaVerified
              ? `${gpu.gpuName} - CUDA ${gpu.cudaVersion} enabled`
              : `${gpu.gpuName} found - falling back to CPU (CUDA unverified)`
            : "No NVIDIA GPU - stem separation will use CPU";
        }
        return gpu;
      } finally {
        stopProgress();
      }
    });

    setStep("model", "done");
    setStatus("AI separation model will download on first use (~340 MB).");

    await runStep("backend", async () => {
      setStatus(gpuSummary ? `${gpuSummary} - starting backend...` : "Starting StemDeck backend...");
      const backend = await invoke("start_backend");
      setStatus("Opening StemDeck...");
      window.location.replace(backend.url);
    });
  } catch (error) {
    showError(error, error?.hint);
  }
}

retryBtn.addEventListener("click", runSetup);
runSetup();
