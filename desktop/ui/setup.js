const { invoke } = window.__TAURI__.core;

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

function showError(error) {
  // Mark any step still spinning as errored, then show the error panel.
  for (const el of steps) {
    if (el.classList.contains("active")) {
      el.classList.replace("active", "error");
    }
  }
  setStatus("Setup could not complete.");
  detailsEl.textContent = String(error);
  detailsEl.classList.remove("hidden");
  retryBtn.classList.remove("hidden");
}

// Marks a step active, runs fn(), then marks done — or marks error and rethrows.
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

// Resolves after at least `ms` milliseconds AND one animation frame, so state
// changes are always visually distinct (not batched into the same paint).
function minDelay(ms) {
  return Promise.all([
    new Promise((r) => setTimeout(r, ms)),
    new Promise((r) => requestAnimationFrame(r)),
  ]);
}

async function runSetup() {
  detailsEl.classList.add("hidden");
  retryBtn.classList.add("hidden");
  for (const step of steps) step.classList.remove("active", "done", "error");

  try {
    // ── Step 1: runtime (serial — everything else needs the python path) ──
    setStep("runtime", "active");
    setStatus("Checking Python runtime...");
    // Run with a minimum display time so the active state is always painted
    // before we transition — probe_runtime resolves in < 1 frame on warm starts.
    const [runtime] = await Promise.all([
      invoke("probe_runtime"),
      minDelay(350),
    ]);

    // Fast path: config from a previous run confirms everything is ready.
    if (runtime.pythonReady && runtime.ffmpegReady && runtime.torchDevice) {
      for (const step of steps) {
        step.classList.remove("active", "error");
        step.classList.add("done");
      }
      setStatus("All systems ready. Starting StemDeck...");
      const backend = await invoke("start_backend");
      window.location.replace(backend.url);
      return;
    }

    if (!runtime.pythonReady) {
      setStep("runtime", "error");
      throw new Error(
        `Python runtime not found. Expected python/ or .venv/ under: ${runtime.appRoot}`
      );
    }
    setStep("runtime", "done");
    setStatus(`Python runtime found at ${runtime.pythonPath}`);
    // Ensure runtime=done is painted before workspace+gpu go active.
    await minDelay(200);

    // ── Steps 2-4: parallel phase ─────────────────────────────────────────
    //
    // Dependency graph:
    //   workspace → ffmpeg  ┐
    //   gpu                 ├─→ (all done) → model → backend
    //
    // workspace and gpu are independent — run them concurrently.
    // ffmpeg needs workspace (data/ffmpeg/ dir must exist first).

    let gpuSummary = "";

    // Chain: workspace → ffmpeg (IIFE keeps async/await consistent)
    const workspaceChain = (async () => {
      await runStep("workspace", () => invoke("ensure_workspace"));

      if (runtime.ffmpegReady) {
        setStep("ffmpeg", "done");
      } else {
        await runStep("ffmpeg", async () => {
          setStatus("Downloading FFmpeg… (this may take a minute)");
          const assets = await invoke("ensure_external_assets");
          if (!assets.ffmpegReady) {
            throw new Error(
              "FFmpeg setup did not complete. Check your internet connection and retry."
            );
          }
        });
      }
    })();

    // GPU detection + torch install (independent of workspace)
    const gpuTask = runStep("gpu", async () => {
      const gpu = await invoke("ensure_torch_device");
      gpuSummary = gpu.gpuDetected
        ? gpu.cudaVerified
          ? `${gpu.gpuName} — CUDA ${gpu.cudaVersion} enabled`
          : `${gpu.gpuName} found — falling back to CPU (CUDA unverified)`
        : "No NVIDIA GPU — stem separation will use CPU";
      return gpu;
    });

    setStatus("Detecting GPU and preparing workspace…");
    await Promise.all([workspaceChain, gpuTask]);

    // ── Step 5: model (no async work — goes straight to done) ────────────
    setStep("model", "done");
    setStatus("AI separation model will download on first use (~340 MB).");

    // ── Step 6: backend (serial — needs everything above) ─────────────────
    await runStep("backend", async () => {
      setStatus(gpuSummary ? `${gpuSummary} — starting backend…` : "Starting StemDeck backend…");
      const backend = await invoke("start_backend");
      setStatus("Opening StemDeck...");
      window.location.replace(backend.url);
    });

  } catch (error) {
    showError(error);
  }
}

retryBtn.addEventListener("click", runSetup);
runSetup();
