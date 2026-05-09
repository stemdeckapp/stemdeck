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

async function runSetup() {
  detailsEl.classList.add("hidden");
  retryBtn.classList.add("hidden");
  for (const step of steps) step.classList.remove("active", "done", "error");

  try {
    setStep("runtime", "active");
    setStatus("Checking Python runtime...");
    const [runtime] = await Promise.all([
      invoke("probe_runtime"),
      minDelay(350),
    ]);

    if (runtime.pythonReady && runtime.ffmpegReady && runtime.torchDevice) {
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

    if (!runtime.pythonReady) {
      setStep("runtime", "error");
      throw new Error(
        `Python runtime not found. Expected python/ or .venv/ under: ${runtime.appRoot}`
      );
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
      const stopProgress = startProgressStatus([
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
      ]);

      try {
        const gpu = await invoke("ensure_torch_device");
        gpuSummary = gpu.gpuDetected
          ? gpu.cudaVerified
            ? `${gpu.gpuName} - CUDA ${gpu.cudaVersion} enabled`
            : `${gpu.gpuName} found - falling back to CPU (CUDA unverified)`
          : "No NVIDIA GPU - stem separation will use CPU";
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
    showError(error);
  }
}

retryBtn.addEventListener("click", runSetup);
runSetup();
