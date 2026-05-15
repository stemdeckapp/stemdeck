use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    env, fs,
    io::{Read, Write},
    net::{TcpListener, TcpStream},
    path::{Path, PathBuf},
    process::{Child, Command, Output, Stdio},
    sync::Mutex,
    thread,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use flate2::read::GzDecoder;
use tar::Archive;
use tauri::{Emitter, Manager};
#[cfg(windows)]
use zip::ZipArchive;

const SETUP_VERSION: u64 = 1;
const DEFAULT_WINDOWS_FFMPEG_URL: &str =
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip";
const DEFAULT_MACOS_FFMPEG_URL: &str = "https://evermeet.cx/ffmpeg/getrelease/ffmpeg/zip";
const DEFAULT_MACOS_FFPROBE_URL: &str = "https://evermeet.cx/ffmpeg/getrelease/ffprobe/zip";

#[derive(Default)]
struct BackendState {
    child: Mutex<Option<Child>>,
    url: Mutex<Option<String>>,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct RuntimeProbe {
    app_root: String,
    data_dir: String,
    python_path: Option<String>,
    python_ready: bool,
    ffmpeg_path: Option<String>,
    ffmpeg_ready: bool,
    /// Persisted from previous setup run; None means GPU step hasn't run yet.
    torch_device: Option<String>,
}

#[derive(Deserialize, Serialize, Clone)]
#[serde(rename_all = "camelCase")]
struct RuntimeManifest {
    version: String,
    arch: String,
    runtime_url: String,
    runtime_sha256: String,
    runtime_size: Option<u64>,
    archive_name: Option<String>,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct RuntimePackStatus {
    manifest_ready: bool,
    manifest_path: Option<String>,
    runtime_ready: bool,
    runtime_dir: String,
    backend_ready: bool,
    python_ready: bool,
    archive_path: Option<String>,
    archive_ready: bool,
    installed_version: Option<String>,
    manifest: Option<RuntimeManifest>,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct RuntimeArchive {
    archive_path: String,
    sha256: String,
    size: u64,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct DownloadProgress {
    received: u64,
    total: Option<u64>,
}

#[derive(Serialize)]
struct BackendStarted {
    url: String,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct AssetStatus {
    ffmpeg_ready: bool,
    ffmpeg_path: Option<String>,
    model_ready: bool,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct GpuSetup {
    gpu_detected: bool,
    gpu_name: Option<String>,
    cuda_version: Option<String>,
    torch_device: String,
    cuda_verified: bool,
}

fn main() {
    tauri::Builder::default()
        .manage(BackendState::default())
        .invoke_handler(tauri::generate_handler![
            probe_runtime,
            ensure_workspace,
            runtime_pack_status,
            download_runtime_pack,
            verify_runtime_pack,
            extract_runtime_pack,
            ensure_external_assets,
            ensure_torch_device,
            start_backend,
            open_url,
        ])
        .build(tauri::generate_context!())
        .expect("failed to build StemDeck desktop app")
        .run(|app_handle, event| match event {
            tauri::RunEvent::ExitRequested { .. } => {
                let state = app_handle.state::<BackendState>();
                stop_backend(&state);
            }
            tauri::RunEvent::WindowEvent {
                event: tauri::WindowEvent::CloseRequested { .. },
                ..
            } => {
                let state = app_handle.state::<BackendState>();
                stop_backend(&state);
                app_handle.exit(0);
            }
            _ => {}
        });
}

#[tauri::command]
fn probe_runtime() -> Result<RuntimeProbe, String> {
    let root = app_root()?;
    let data_dir = local_data_dir()?;
    let python = python_path(&root);
    if let Some(path) = python.as_deref() {
        patch_pyvenv_cfg(path);
    }
    let ffmpeg = ffmpeg_path(&data_dir);
    let torch_device = read_config_str(&data_dir, "torchDevice");
    Ok(RuntimeProbe {
        app_root: root.display().to_string(),
        data_dir: data_dir.display().to_string(),
        python_ready: python.as_ref().is_some_and(|p| python_stdlib_ok(p)),
        python_path: python.map(|p| p.display().to_string()),
        ffmpeg_ready: ffmpeg.as_ref().is_some_and(|p| p.is_file()),
        ffmpeg_path: ffmpeg.map(|p| p.display().to_string()),
        torch_device,
    })
}

/// Read a single string field from data/config.json, returning None on any error.
fn read_config_str(data_dir: &std::path::Path, key: &str) -> Option<String> {
    let text = fs::read_to_string(data_dir.join("config.json")).ok()?;
    let value: serde_json::Value = serde_json::from_str(&text).ok()?;
    value.get(key)?.as_str().map(|s| s.to_string())
}

#[tauri::command]
fn runtime_pack_status() -> Result<RuntimePackStatus, String> {
    let root = app_root()?;
    let data_dir = local_data_dir()?;
    let runtime_dir = runtime_dir(&data_dir);
    let backend_dir = runtime_dir.join("backend");
    let python = runtime_python_path(&data_dir);
    let manifest_path = runtime_manifest_path(&root);
    let manifest = manifest_path
        .as_deref()
        .and_then(|path| read_runtime_manifest(path).ok());
    let archive_path = manifest
        .as_ref()
        .map(|item| runtime_archive_path(&data_dir, item));
    let installed_version = read_runtime_install_manifest(&runtime_dir)
        .and_then(|value| value.get("version")?.as_str().map(|text| text.to_string()));

    Ok(RuntimePackStatus {
        manifest_ready: manifest.is_some(),
        manifest_path: manifest_path.map(|path| path.display().to_string()),
        runtime_ready: backend_dir.join("app").is_dir() && python.is_file(),
        runtime_dir: runtime_dir.display().to_string(),
        backend_ready: backend_dir.join("app").is_dir(),
        python_ready: python.is_file(),
        archive_ready: archive_path.as_ref().is_some_and(|path| path.is_file()),
        archive_path: archive_path.map(|path| path.display().to_string()),
        installed_version,
        manifest,
    })
}

#[tauri::command]
async fn download_runtime_pack(app_handle: tauri::AppHandle) -> Result<RuntimeArchive, String> {
    ensure_workspace()?;
    let root = app_root()?;
    let data_dir = local_data_dir()?;
    let manifest = load_runtime_manifest(&root)?;
    validate_runtime_manifest(&manifest)?;
    let archive = runtime_archive_path(&data_dir, &manifest);
    if let Some(parent) = archive.parent() {
        fs::create_dir_all(parent)
            .map_err(|e| format!("failed to create {}: {e}", parent.display()))?;
    }
    download_file_with_progress(&manifest.runtime_url, &archive, &app_handle).await?;
    verify_runtime_archive(&manifest, &archive)
}

#[tauri::command]
fn verify_runtime_pack() -> Result<RuntimeArchive, String> {
    let root = app_root()?;
    let data_dir = local_data_dir()?;
    let manifest = load_runtime_manifest(&root)?;
    validate_runtime_manifest(&manifest)?;
    let archive = runtime_archive_path(&data_dir, &manifest);
    verify_runtime_archive(&manifest, &archive)
}

#[tauri::command]
fn extract_runtime_pack() -> Result<RuntimePackStatus, String> {
    ensure_workspace()?;
    let root = app_root()?;
    let data_dir = local_data_dir()?;
    let manifest = load_runtime_manifest(&root)?;
    validate_runtime_manifest(&manifest)?;
    let archive = runtime_archive_path(&data_dir, &manifest);
    verify_runtime_archive(&manifest, &archive)?;

    let runtime = runtime_dir(&data_dir);
    let tmp = data_dir.join("runtime.tmp");
    let old = data_dir.join("runtime.old");
    if tmp.exists() {
        fs::remove_dir_all(&tmp).map_err(|e| format!("failed to remove {}: {e}", tmp.display()))?;
    }
    fs::create_dir_all(&tmp).map_err(|e| format!("failed to create {}: {e}", tmp.display()))?;

    extract_tar_archive(&archive, &tmp)?;
    let extracted = tmp.join("runtime");
    if !extracted.join("backend").join("app").is_dir() {
        return Err("runtime archive did not contain runtime/backend/app".to_string());
    }
    if !extracted
        .join("python")
        .join("bin")
        .join("python")
        .is_file()
    {
        return Err("runtime archive did not contain runtime/python/bin/python".to_string());
    }

    let install_manifest = serde_json::json!({
        "version": manifest.version,
        "arch": manifest.arch,
        "runtimeUrl": manifest.runtime_url,
        "runtimeSha256": manifest.runtime_sha256,
        "installedAt": unix_timestamp(),
    });
    fs::write(
        extracted.join("runtime-manifest.json"),
        serde_json::to_string_pretty(&install_manifest)
            .map_err(|e| format!("failed to serialize runtime install manifest: {e}"))?
            + "\n",
    )
    .map_err(|e| format!("failed to write runtime manifest: {e}"))?;

    if old.exists() {
        fs::remove_dir_all(&old).map_err(|e| format!("failed to remove {}: {e}", old.display()))?;
    }
    if runtime.exists() {
        fs::rename(&runtime, &old)
            .map_err(|e| format!("failed to move existing runtime aside: {e}"))?;
    }
    fs::rename(&extracted, &runtime).map_err(|e| format!("failed to install runtime: {e}"))?;
    let _ = fs::remove_dir_all(&tmp);
    let _ = fs::remove_dir_all(&old);

    let python = runtime.join("python").join("bin").join("python");
    patch_pyvenv_cfg(&python);
    runtime_pack_status()
}

#[tauri::command]
fn ensure_workspace() -> Result<(), String> {
    let root = app_root()?;
    let data = local_data_dir()?;
    migrate_legacy_data(&root, &data);
    fs::create_dir_all(&data).map_err(|e| format!("failed to create data dir: {e}"))?;
    for dir in ["cache", "downloads", "ffmpeg", "jobs", "logs", "models"] {
        fs::create_dir_all(data.join(dir))
            .map_err(|e| format!("failed to create data/{dir}: {e}"))?;
    }
    if is_cpu_only_package(&root, &data) {
        fs::write(data.join("cpu-only"), "")
            .map_err(|e| format!("failed to write data/cpu-only: {e}"))?;
    }
    let config = data.join("config.json");
    if !config.exists() {
        fs::write(
            &config,
            "{\n  \"setupVersion\": 1,\n  \"ffmpegReady\": false,\n  \"modelReady\": false\n}\n",
        )
        .map_err(|e| format!("failed to write {}: {e}", config.display()))?;
    }
    Ok(())
}

#[tauri::command]
fn ensure_external_assets() -> Result<AssetStatus, String> {
    ensure_workspace()?;
    let data_dir = local_data_dir()?;
    let ffmpeg = ensure_ffmpeg(&data_dir)?;
    write_setup_config(&data_dir, &ffmpeg)?;
    Ok(AssetStatus {
        ffmpeg_ready: true,
        ffmpeg_path: Some(ffmpeg.display().to_string()),
        model_ready: false,
    })
}

#[tauri::command]
fn start_backend(state: tauri::State<BackendState>) -> Result<BackendStarted, String> {
    if let Some(url) = state.url.lock().map_err(|e| e.to_string())?.clone() {
        return Ok(BackendStarted { url });
    }
    stop_backend(&state);

    let root = app_root()?;
    let backend_dir = backend_dir(&root)?;
    let data_dir = local_data_dir()?;
    let python = python_path(&root).filter(|p| p.is_file()).ok_or_else(|| {
        "Python runtime not found. Expected python/ or .venv/ under StemDeck.".to_string()
    })?;
    patch_pyvenv_cfg(&python);
    let port = free_port()?;
    let url = format!("http://127.0.0.1:{port}");
    let log_path = data_dir.join("logs").join("backend.log");
    let (stdout, stderr) = prepare_backend_stdio(&log_path).unwrap_or_else(|_| {
        // Logging should help diagnose startup; it should not prevent startup.
        (Stdio::null(), Stdio::null())
    });

    // Point Python at its bundled stdlib so it works on machines without a
    // matching system Python. Windows portable builds keep stdlib in base/Lib.
    let pythonhome = python
        .parent()
        .and_then(|bin_dir| bin_dir.parent().map(|venv| (venv, bin_dir)))
        .and_then(|(venv, bin_dir)| bundled_python_home(venv, bin_dir).map(|(home, _)| home));
    let mut cmd = Command::new(python);
    cmd.args([
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        &port.to_string(),
    ]);
    if let Some(ref pythonhome) = pythonhome {
        cmd.env("PYTHONHOME", pythonhome);
    }

    cmd.current_dir(&backend_dir)
        .env("STEMDECK_DATA_DIR", &data_dir)

        .env("STEMDECK_DESKTOP", "1")
        .env("STEMDECK_PARENT_PID", std::process::id().to_string())
        .env("PYTHONUNBUFFERED", "1")
        .env("XDG_CACHE_HOME", data_dir.join("cache"))
        .env("TORCH_HOME", data_dir.join("models").join("torch"))
        .stdout(stdout)
        .stderr(stderr);

    if let Some(ffmpeg_dir) = ffmpeg_dir_if_present(&data_dir) {
        let existing = env::var_os("PATH").unwrap_or_default();
        let mut paths = vec![ffmpeg_dir];
        paths.extend(env::split_paths(&existing));
        let joined = env::join_paths(paths).map_err(|e| e.to_string())?;
        cmd.env("PATH", joined);
    }

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x08000000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }

    let mut child = cmd
        .spawn()
        .map_err(|e| format!("failed to start backend: {e}"))?;

    if let Err(err) = wait_for_health(port, Duration::from_secs(90), &log_path) {
        let _ = child.kill();
        let _ = child.wait();
        return Err(err);
    }

    *state.child.lock().map_err(|e| e.to_string())? = Some(child);
    *state.url.lock().map_err(|e| e.to_string())? = Some(url.clone());
    Ok(BackendStarted { url })
}

#[tauri::command]
fn ensure_torch_device() -> Result<GpuSetup, String> {
    let root = app_root()?;
    let data_dir = local_data_dir()?;

    // CPU-only portable build: skip GPU detection and pip entirely.
    if is_cpu_only_package(&root, &data_dir) {
        persist_torch_device(&data_dir, "cpu");
        return Ok(GpuSetup {
            gpu_detected: false,
            gpu_name: None,
            cuda_version: None,
            torch_device: "cpu".to_string(),
            cuda_verified: false,
        });
    }

    let python = python_path(&root)
        .filter(|p| p.is_file())
        .ok_or_else(|| "Python not found".to_string())?;
    patch_pyvenv_cfg(&python);

    #[cfg(target_os = "macos")]
    {
        let mps_available = verify_mps_torch(&python);
        let device = if mps_available { "mps" } else { "cpu" };
        persist_torch_device(&data_dir, device);
        Ok(GpuSetup {
            gpu_detected: mps_available,
            gpu_name: if mps_available {
                Some("Apple Silicon (MPS)".to_string())
            } else {
                None
            },
            cuda_version: None,
            torch_device: device.to_string(),
            cuda_verified: false,
        })
    }

    #[cfg(not(target_os = "macos"))]
    {
        let setup = match detect_nvidia_gpu() {
            Some((gpu_name, cuda_version)) => {
                let index_url = cuda_index_url(&cuda_version);
                install_cuda_torch(&python, &index_url)?;
                let cuda_verified = verify_cuda_torch(&python);
                GpuSetup {
                    gpu_detected: true,
                    gpu_name: Some(gpu_name),
                    cuda_version: Some(cuda_version),
                    torch_device: if cuda_verified { "cuda" } else { "cpu" }.to_string(),
                    cuda_verified,
                }
            }
            None => GpuSetup {
                gpu_detected: false,
                gpu_name: None,
                cuda_version: None,
                torch_device: "cpu".to_string(),
                cuda_verified: false,
            },
        };
        // Persist so subsequent launches skip this step entirely.
        persist_torch_device(&data_dir, &setup.torch_device);
        Ok(setup)
    }
}

fn is_cpu_only_package(root: &Path, data_dir: &Path) -> bool {
    root.join("cpu-only").is_file() || data_dir.join("cpu-only").is_file()
}

fn persist_torch_device(data_dir: &std::path::Path, device: &str) {
    let _ = update_setup_config(
        data_dir,
        [("torchDevice", serde_json::Value::String(device.to_string()))],
    );
}

#[cfg(target_os = "macos")]
fn verify_mps_torch(python: &Path) -> bool {
    Command::new(python)
        .args([
            "-c",
            "import torch; exit(0 if getattr(torch.backends, 'mps', None) and torch.backends.mps.is_available() else 1)",
        ])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

#[cfg(not(target_os = "macos"))]
fn nvidia_smi_exe() -> &'static str {
    // nvidia-smi.exe lives in System32 on Windows but Tauri child processes
    // inherit a stripped PATH that may not include it.
    #[cfg(windows)]
    {
        const SYSTEM32: &str = r"C:\Windows\System32\nvidia-smi.exe";
        if std::path::Path::new(SYSTEM32).is_file() {
            return SYSTEM32;
        }
    }
    "nvidia-smi"
}

#[cfg(not(target_os = "macos"))]
fn detect_nvidia_gpu() -> Option<(String, String)> {
    let smi = nvidia_smi_exe();
    let mut cmd = Command::new(smi);
    cmd.args(["--query-gpu=name", "--format=csv,noheader"])
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    hide_console_window(&mut cmd);
    let name_out = command_output_with_timeout(cmd, Duration::from_secs(10), "nvidia-smi").ok()?;
    if !name_out.status.success() {
        return None;
    }
    let gpu_name = String::from_utf8_lossy(&name_out.stdout).trim().to_string();
    if gpu_name.is_empty() {
        return None;
    }

    // Read CUDA version from the standard nvidia-smi header.
    let mut smi_cmd = Command::new(smi);
    smi_cmd.stdout(Stdio::piped()).stderr(Stdio::null());
    hide_console_window(&mut smi_cmd);
    let smi_out =
        command_output_with_timeout(smi_cmd, Duration::from_secs(10), "nvidia-smi").ok()?;
    let smi_text = String::from_utf8_lossy(&smi_out.stdout);
    let cuda_version = parse_cuda_version(&smi_text).unwrap_or_else(|| "12.4".to_string());

    Some((gpu_name, cuda_version))
}

#[cfg(not(target_os = "macos"))]
fn parse_cuda_version(smi_output: &str) -> Option<String> {
    for line in smi_output.lines() {
        if let Some(pos) = line.find("CUDA Version:") {
            let rest = &line[pos + "CUDA Version:".len()..];
            let v = rest
                .trim()
                .split_whitespace()
                .next()?
                .trim_matches('|')
                .trim();
            if !v.is_empty() && v != "N/A" {
                return Some(v.to_string());
            }
        }
    }
    None
}

#[cfg(not(target_os = "macos"))]
fn cuda_tag(cuda_version: &str) -> &'static str {
    let parts: Vec<u32> = cuda_version
        .splitn(2, '.')
        .filter_map(|p| p.parse().ok())
        .collect();
    match parts.as_slice() {
        [12, minor] if *minor >= 4 => "cu124",
        [12, _] => "cu121",
        [11, _] => "cu118",
        _ => "cu124",
    }
}

#[cfg(not(target_os = "macos"))]
fn cuda_index_url(cuda_version: &str) -> String {
    format!(
        "https://download.pytorch.org/whl/{}",
        cuda_tag(cuda_version)
    )
}

#[cfg(not(target_os = "macos"))]
fn cuda_tag_from_url(index_url: &str) -> &str {
    index_url.rsplit('/').next().unwrap_or("cu124")
}

/// Update pyvenv.cfg to the bundled Python runtime. Windows venv launchers read
/// this file before Python starts, so stale build-machine paths can prevent the
/// backend from emitting any log output at all.
fn patch_pyvenv_cfg(python: &Path) {
    let Some(bin_dir) = python.parent() else {
        return;
    };
    let Some(venv_root) = bin_dir.parent() else {
        return;
    };
    let Some((home_dir, bundled_python)) = bundled_python_home(venv_root, bin_dir) else {
        return;
    };
    let cfg_path = venv_root.join("pyvenv.cfg");
    let Ok(content) = fs::read_to_string(&cfg_path) else {
        return;
    };
    let home_str = home_dir.display().to_string();
    let python_str = bundled_python.display().to_string();
    let patched: String = content
        .lines()
        .map(|line| {
            let trimmed = line.trim_start();
            if trimmed.starts_with("home") && trimmed[4..].trim_start().starts_with('=') {
                format!("home = {home_str}")
            } else if trimmed.starts_with("executable")
                && trimmed["executable".len()..].trim_start().starts_with('=')
            {
                format!("executable = {python_str}")
            } else {
                line.to_string()
            }
        })
        .collect::<Vec<_>>()
        .join("\n");
    let patched = if content.ends_with('\n') {
        patched + "\n"
    } else {
        patched
    };
    let _ = fs::write(&cfg_path, patched);
}

fn prepare_backend_stdio(log_path: &Path) -> Result<(Stdio, Stdio), String> {
    if let Some(parent) = log_path.parent() {
        fs::create_dir_all(parent)
            .map_err(|e| format!("failed to create backend log directory: {e}"))?;
    }
    fs::File::create(log_path)
        .map_err(|e| format!("failed to create backend log {}: {e}", log_path.display()))?;
    let stdout = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(log_path)
        .map_err(|e| {
            format!(
                "failed to open backend stdout log {}: {e}",
                log_path.display()
            )
        })?;
    let stderr = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(log_path)
        .map_err(|e| {
            format!(
                "failed to open backend stderr log {}: {e}",
                log_path.display()
            )
        })?;
    Ok((Stdio::from(stdout), Stdio::from(stderr)))
}

fn bundled_python_home(venv_root: &Path, bin_dir: &Path) -> Option<(PathBuf, PathBuf)> {
    let executable = if cfg!(windows) {
        "python.exe"
    } else {
        "python"
    };

    if cfg!(windows) {
        let base_home = venv_root.join("base");
        let base_python = base_home.join(executable);
        if base_python.is_file() && base_home.join("Lib").join("os.py").is_file() {
            return Some((base_home, base_python));
        }

        let legacy_root_python = venv_root.join(executable);
        if legacy_root_python.is_file() && venv_root.join("Lib").join("os.py").is_file() {
            let launcher = bin_dir.join(executable);
            if launcher.is_file() {
                return Some((bin_dir.to_path_buf(), launcher));
            }
        }
    } else if python_stdlib_present(venv_root) {
        let launcher = bin_dir.join(executable);
        if launcher.is_file() {
            return Some((bin_dir.to_path_buf(), launcher));
        }
    }

    None
}

fn python_stdlib_ok(python: &Path) -> bool {
    if !python.is_file() {
        return false;
    }
    let venv_root = python.parent().and_then(|b| b.parent());
    // On Windows the portable venv layout puts the stdlib in base/Lib/, not Lib/.
    // Using the venv root as PYTHONHOME causes `import encodings` to fail because
    // Python looks for Lib/os.py there and finds only site-packages.
    let pythonhome = venv_root.map(|venv| {
        #[cfg(windows)]
        {
            let base = venv.join("base");
            if base.join("Lib").join("os.py").is_file() {
                return base;
            }
        }
        venv.to_path_buf()
    });
    let mut cmd = Command::new(python);
    cmd.args(["-c", "import encodings"]);
    if let Some(home) = pythonhome {
        cmd.env("PYTHONHOME", home);
    }
    cmd.stdout(Stdio::null()).stderr(Stdio::null());
    cmd.status().map(|s| s.success()).unwrap_or(false)
}

fn python_stdlib_present(venv_root: &Path) -> bool {
    if venv_root.join("Lib").join("os.py").is_file() {
        return true;
    }
    let lib = venv_root.join("lib");
    let Ok(entries) = fs::read_dir(lib) else {
        return false;
    };
    entries
        .filter_map(Result::ok)
        .any(|entry| entry.path().join("os.py").is_file())
}

#[cfg(not(target_os = "macos"))]
fn install_cuda_torch(python: &Path, index_url: &str) -> Result<(), String> {
    // Skip only when CUDA torch is already active — torch.version.cuda is
    // None for CPU-only wheels, so this correctly re-installs when needed.
    if verify_cuda_torch(python) {
        return Ok(());
    }

    // Fix the build machine's Python path baked into pyvenv.cfg before pip
    // runs — pip validates the `home` entry and fails if it doesn't exist.
    patch_pyvenv_cfg(python);

    // Use the explicit local-version suffix (e.g. torch==2.6.0+cu124) so pip
    // treats the CUDA wheel as a distinct version from the CPU-only 2.6.0
    // wheel and doesn't skip the install as "already satisfied".
    let tag = cuda_tag_from_url(index_url);
    let torch_spec = format!("torch==2.6.0+{tag}");
    let torchaudio_spec = format!("torchaudio==2.6.0+{tag}");
    // --ignore-installed: overwrites even a corrupted/partial install that
    // has no RECORD file. --no-deps: only replace torch/torchaudio wheels.
    let mut command = Command::new(python);
    command
        .args([
            "-m",
            "pip",
            "install",
            &torch_spec,
            &torchaudio_spec,
            "--index-url",
            index_url,
            "--ignore-installed",
            "--no-deps",
            "--quiet",
        ])
        .stdout(Stdio::null())
        .stderr(Stdio::piped());
    let output =
        command_output_with_timeout(command, Duration::from_secs(20 * 60), "CUDA torch install")?;

    if output.status.success() {
        Ok(())
    } else {
        let stderr = String::from_utf8_lossy(&output.stderr);
        Err(format!("CUDA torch install failed: {}", stderr.trim()))
    }
}

#[cfg(not(target_os = "macos"))]
fn verify_cuda_torch(python: &Path) -> bool {
    Command::new(python)
        .args([
            "-c",
            "import torch; exit(0 if torch.cuda.is_available() else 1)",
        ])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

#[tauri::command]
fn open_url(url: String) -> Result<(), String> {
    #[cfg(windows)]
    {
        let mut cmd = Command::new("cmd");
        cmd.args(["/c", "start", "", &url]);
        hide_console_window(&mut cmd);
        cmd.spawn()
            .map_err(|e| format!("failed to open URL: {e}"))?;
    }
    #[cfg(target_os = "macos")]
    {
        Command::new("open")
            .arg(&url)
            .spawn()
            .map_err(|e| format!("failed to open URL: {e}"))?;
    }
    #[cfg(all(unix, not(target_os = "macos")))]
    {
        Command::new("xdg-open")
            .arg(&url)
            .spawn()
            .map_err(|e| format!("failed to open URL: {e}"))?;
    }
    Ok(())
}

fn stop_backend(state: &BackendState) {
    if let Ok(mut guard) = state.child.lock() {
        if let Some(child) = guard.as_mut() {
            let _ = child.kill();
            let _ = child.wait();
        }
        *guard = None;
    }
}

/// Returns the persistent user data directory for StemDeck.
/// On Windows: %LocalAppData%\StemDeck
/// On macOS: ~/Library/Application Support/StemDeck
/// On Linux: $XDG_DATA_HOME/stemdeck  or  ~/.local/share/stemdeck
/// Can be overridden by STEMDECK_DATA_DIR for development.
fn local_data_dir() -> Result<PathBuf, String> {
    if let Ok(path) = env::var("STEMDECK_DATA_DIR") {
        return Ok(PathBuf::from(path));
    }
    #[cfg(windows)]
    {
        let base = env::var("LOCALAPPDATA")
            .map_err(|_| "LOCALAPPDATA environment variable not set".to_string())?;
        Ok(PathBuf::from(base).join("StemDeck"))
    }
    #[cfg(target_os = "macos")]
    {
        let home = env::var("HOME").map_err(|_| "HOME environment variable not set".to_string())?;
        Ok(PathBuf::from(home)
            .join("Library")
            .join("Application Support")
            .join("StemDeck"))
    }
    #[cfg(all(unix, not(target_os = "macos")))]
    {
        if let Ok(xdg) = env::var("XDG_DATA_HOME") {
            return Ok(PathBuf::from(xdg).join("stemdeck"));
        }
        let home = env::var("HOME").map_err(|_| "HOME environment variable not set".to_string())?;
        Ok(PathBuf::from(home)
            .join(".local")
            .join("share")
            .join("stemdeck"))
    }
}

/// One-time migration: move legacy data/models/jobs/ffmpeg from the install
/// directory into the new per-user data directory on the user's first launch
/// after upgrading to a version that uses local_data_dir().
fn migrate_legacy_data(root: &Path, data_dir: &Path) {
    let old = root.join("data");
    // Only migrate if the old location exists and the new one doesn't yet.
    if !old.is_dir() || data_dir.exists() {
        return;
    }
    for name in ["models", "jobs", "ffmpeg", "logs", "cache"] {
        let src = old.join(name);
        if src.is_dir() {
            // rename is a cheap move on the same volume; ignore errors silently
            // so a cross-volume failure doesn't block startup.
            let _ = fs::rename(&src, data_dir.join(name));
        }
    }
    for name in ["config.json", "cpu-only"] {
        let src = old.join(name);
        if src.exists() {
            let _ = fs::copy(&src, data_dir.join(name));
        }
    }
}

fn runtime_dir(data_dir: &Path) -> PathBuf {
    data_dir.join("runtime")
}

fn runtime_python_path(data_dir: &Path) -> PathBuf {
    runtime_dir(data_dir)
        .join("python")
        .join("bin")
        .join("python")
}

fn runtime_manifest_path(root: &Path) -> Option<PathBuf> {
    [
        root.join("runtime-manifest.json"),
        root.join("desktop")
            .join("ui")
            .join("runtime-manifest.json"),
    ]
    .into_iter()
    .find(|path| path.is_file())
}

fn load_runtime_manifest(root: &Path) -> Result<RuntimeManifest, String> {
    let path = runtime_manifest_path(root)
        .ok_or_else(|| format!("runtime-manifest.json not found under {}", root.display()))?;
    read_runtime_manifest(&path)
}

fn read_runtime_manifest(path: &Path) -> Result<RuntimeManifest, String> {
    let text =
        fs::read_to_string(path).map_err(|e| format!("failed to read {}: {e}", path.display()))?;
    serde_json::from_str(&text).map_err(|e| format!("failed to parse {}: {e}", path.display()))
}

fn read_runtime_install_manifest(runtime_dir: &Path) -> Option<serde_json::Value> {
    let text = fs::read_to_string(runtime_dir.join("runtime-manifest.json")).ok()?;
    serde_json::from_str(&text).ok()
}

fn validate_runtime_manifest(manifest: &RuntimeManifest) -> Result<(), String> {
    if manifest.runtime_url.trim().is_empty() {
        return Err("runtime manifest has an empty runtimeUrl".to_string());
    }
    let sha = manifest.runtime_sha256.trim();
    if sha.len() != 64 || !sha.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err("runtime manifest must include a 64-character runtimeSha256".to_string());
    }
    Ok(())
}

fn runtime_archive_path(data_dir: &Path, manifest: &RuntimeManifest) -> PathBuf {
    let name = manifest
        .archive_name
        .clone()
        .or_else(|| manifest.runtime_url.rsplit('/').next().map(str::to_string))
        .filter(|value| !value.trim().is_empty())
        .unwrap_or_else(|| format!("StemDeck-runtime-macOS-{}.tar.zst", manifest.arch));
    data_dir.join("downloads").join(name)
}

async fn download_file_with_progress(
    url: &str,
    target: &Path,
    app_handle: &tauri::AppHandle,
) -> Result<(), String> {
    let tmp = target.with_extension("download");
    if tmp.exists() {
        fs::remove_file(&tmp)
            .map_err(|e| format!("failed to remove {}: {e}", tmp.display()))?;
    }

    if let Some(path) = url.strip_prefix("file://") {
        fs::copy(Path::new(path), &tmp)
            .map_err(|e| format!("failed to copy runtime pack from {url}: {e}"))?;
        return fs::rename(&tmp, target)
            .map_err(|e| format!("failed to move runtime pack to {}: {e}", target.display()));
    }
    if Path::new(url).is_file() {
        fs::copy(Path::new(url), &tmp)
            .map_err(|e| format!("failed to copy runtime pack from {url}: {e}"))?;
        return fs::rename(&tmp, target)
            .map_err(|e| format!("failed to move runtime pack to {}: {e}", target.display()));
    }

    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(30 * 60))
        .build()
        .map_err(|e| format!("failed to build HTTP client: {e}"))?;
    let mut response = client
        .get(url)
        .send()
        .await
        .map_err(|e| format!("failed to start download from {url}: {e}"))?;
    if !response.status().is_success() {
        return Err(format!(
            "failed to download runtime pack from {url}: HTTP {}",
            response.status()
        ));
    }

    let total = response.content_length();
    let mut file =
        fs::File::create(&tmp).map_err(|e| format!("failed to create {}: {e}", tmp.display()))?;
    let mut received: u64 = 0;
    let mut last_emit = Instant::now();

    while let Some(chunk) = response
        .chunk()
        .await
        .map_err(|e| format!("download error: {e}"))?
    {
        file.write_all(&chunk)
            .map_err(|e| format!("failed to write to {}: {e}", tmp.display()))?;
        received += chunk.len() as u64;
        if last_emit.elapsed() >= Duration::from_millis(150) {
            let _ = app_handle.emit("runtime-download-progress", DownloadProgress { received, total });
            last_emit = Instant::now();
        }
    }
    let _ = app_handle.emit(
        "runtime-download-progress",
        DownloadProgress { received, total: Some(received) },
    );

    fs::rename(&tmp, target)
        .map_err(|e| format!("failed to move runtime pack to {}: {e}", target.display()))
}

fn download_file(url: &str, target: &Path, timeout: Duration) -> Result<(), String> {
    let tmp = target.with_extension("download");
    if tmp.exists() {
        fs::remove_file(&tmp).map_err(|e| format!("failed to remove {}: {e}", tmp.display()))?;
    }

    if let Some(path) = url.strip_prefix("file://") {
        fs::copy(Path::new(path), &tmp)
            .map_err(|e| format!("failed to copy runtime pack from {url}: {e}"))?;
    } else if Path::new(url).is_file() {
        fs::copy(Path::new(url), &tmp)
            .map_err(|e| format!("failed to copy runtime pack from {url}: {e}"))?;
    } else {
        let mut command = Command::new("curl");
        command
            .args([
                "--fail",
                "--location",
                "--show-error",
                "--output",
                &tmp.display().to_string(),
                "--",
                url,
            ])
            .stdout(Stdio::null())
            .stderr(Stdio::piped());
        let output = command_output_with_timeout(command, timeout, "runtime pack download")?;
        if !output.status.success() || !tmp.is_file() {
            let stderr = String::from_utf8_lossy(&output.stderr);
            return Err(format!(
                "failed to download runtime pack from {url}: {}",
                stderr.trim()
            ));
        }
    }

    fs::rename(&tmp, target)
        .map_err(|e| format!("failed to move runtime pack to {}: {e}", target.display()))
}

fn verify_runtime_archive(
    manifest: &RuntimeManifest,
    archive: &Path,
) -> Result<RuntimeArchive, String> {
    if !archive.is_file() {
        return Err(format!(
            "runtime archive not found at {}",
            archive.display()
        ));
    }
    let size = archive
        .metadata()
        .map_err(|e| format!("failed to stat {}: {e}", archive.display()))?
        .len();
    let sha256 = sha256_file(archive)?;
    if !sha256.eq_ignore_ascii_case(manifest.runtime_sha256.trim()) {
        let _ = fs::remove_file(archive);
        return Err(format!(
            "runtime archive checksum mismatch: expected {}, got {}",
            manifest.runtime_sha256, sha256
        ));
    }
    Ok(RuntimeArchive {
        archive_path: archive.display().to_string(),
        sha256,
        size,
    })
}

fn sha256_file(path: &Path) -> Result<String, String> {
    let mut file =
        fs::File::open(path).map_err(|e| format!("failed to open {}: {e}", path.display()))?;
    let mut hasher = Sha256::new();
    let mut buffer = [0_u8; 1024 * 64];
    loop {
        let read = file
            .read(&mut buffer)
            .map_err(|e| format!("failed to read {}: {e}", path.display()))?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
    }
    Ok(format!("{:x}", hasher.finalize()))
}

fn extract_tar_archive(archive: &Path, destination: &Path) -> Result<(), String> {
    let file = fs::File::open(archive)
        .map_err(|e| format!("failed to open archive {}: {e}", archive.display()))?;
    let is_zst = archive
        .extension()
        .map_or(false, |ext| ext.eq_ignore_ascii_case("zst"));
    if is_zst {
        let decoder = zstd::Decoder::new(file)
            .map_err(|e| format!("failed to init zstd decoder: {e}"))?;
        Archive::new(decoder)
            .unpack(destination)
            .map_err(|e| format!("failed to extract runtime pack: {e}"))
    } else {
        let decoder = GzDecoder::new(file);
        Archive::new(decoder)
            .unpack(destination)
            .map_err(|e| format!("failed to extract runtime pack: {e}"))
    }
}

fn unix_timestamp() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or_default()
}

fn app_root() -> Result<PathBuf, String> {
    if let Ok(root) = env::var("STEMDECK_ROOT") {
        return Ok(PathBuf::from(root));
    }
    if let Ok(cwd) = env::current_dir() {
        if let Some(root) = find_repo_root(&cwd) {
            return Ok(root);
        }
    }
    let exe = env::current_exe().map_err(|e| format!("failed to resolve current exe: {e}"))?;
    let exe_dir = exe
        .parent()
        .ok_or_else(|| "current exe has no parent directory".to_string())?;
    if let Some(root) = find_repo_root(exe_dir) {
        return Ok(root);
    }
    #[cfg(target_os = "macos")]
    {
        if let Some(contents) = exe_dir.parent() {
            let resources = contents.join("Resources");
            if resources.is_dir() {
                return Ok(resources);
            }
        }
    }
    Ok(exe_dir.to_path_buf())
}

fn find_repo_root(start: &Path) -> Option<PathBuf> {
    for candidate in start.ancestors() {
        if candidate.join("pyproject.toml").is_file() && candidate.join("app").is_dir() {
            return Some(candidate.to_path_buf());
        }
        if candidate.join("backend").join("app").is_dir() && candidate.join("python").is_dir() {
            return Some(candidate.to_path_buf());
        }
    }
    None
}

fn backend_dir(root: &Path) -> Result<PathBuf, String> {
    if let Ok(data_dir) = local_data_dir() {
        let backend = runtime_dir(&data_dir).join("backend");
        if backend.join("app").is_dir() {
            return Ok(backend);
        }
    }

    let portable = root.join("backend");
    if portable.join("app").is_dir() {
        return Ok(portable);
    }
    if root.join("app").is_dir() {
        return Ok(root.to_path_buf());
    }
    Err(format!(
        "backend app directory not found under {}",
        root.display()
    ))
}

fn python_path(root: &Path) -> Option<PathBuf> {
    if let Ok(path) = env::var("STEMDECK_PYTHON") {
        return Some(PathBuf::from(path));
    }
    if let Ok(data_dir) = local_data_dir() {
        let python = runtime_python_path(&data_dir);
        if python.is_file() {
            return Some(python);
        }
    }
    let candidates = if cfg!(windows) {
        vec![
            root.join("python").join("Scripts").join("python.exe"),
            root.join(".venv").join("Scripts").join("python.exe"),
        ]
    } else {
        vec![
            root.join("python").join("bin").join("python"),
            root.join(".venv").join("bin").join("python"),
            PathBuf::from("python3"),
        ]
    };
    candidates
        .into_iter()
        .find(|p| p.is_file())
        .or_else(|| Some(PathBuf::from("python3")))
}

fn ffmpeg_path(data_dir: &Path) -> Option<PathBuf> {
    if let Ok(path) = env::var("STEMDECK_FFMPEG") {
        return Some(PathBuf::from(path));
    }
    let file = if cfg!(windows) {
        "ffmpeg.exe"
    } else {
        "ffmpeg"
    };
    Some(data_dir.join("ffmpeg").join(file))
}

fn ffprobe_path(data_dir: &Path) -> PathBuf {
    let file = if cfg!(windows) {
        "ffprobe.exe"
    } else {
        "ffprobe"
    };
    data_dir.join("ffmpeg").join(file)
}

fn ffmpeg_dir_if_present(data_dir: &Path) -> Option<PathBuf> {
    let path = ffmpeg_path(data_dir)?;
    if path.is_file() {
        path.parent().map(Path::to_path_buf)
    } else {
        None
    }
}

fn free_port() -> Result<u16, String> {
    let listener =
        TcpListener::bind("127.0.0.1:0").map_err(|e| format!("port bind failed: {e}"))?;
    let port = listener.local_addr().map_err(|e| e.to_string())?.port();
    drop(listener);
    Ok(port)
}

fn wait_for_health(port: u16, timeout: Duration, log_path: &Path) -> Result<(), String> {
    let deadline = Instant::now() + timeout;
    loop {
        if Instant::now() >= deadline {
            let tail = file_tail(log_path, 30);
            let hint = if tail.trim().is_empty() {
                format!(
                    "No backend log output was captured at {}.",
                    log_path.display()
                )
            } else {
                format!(
                    "Last backend log lines from {}:\n{}",
                    log_path.display(),
                    tail
                )
            };
            return Err(format!(
                "backend did not become healthy within {} seconds.\n\n{}",
                timeout.as_secs(),
                hint
            ));
        }
        if health_once(port).is_ok() {
            return Ok(());
        }
        thread::sleep(Duration::from_millis(250));
    }
}

fn file_tail(path: &Path, max_lines: usize) -> String {
    fs::read_to_string(path)
        .map(|text| {
            let lines: Vec<&str> = text.lines().rev().take(max_lines).collect();
            lines.into_iter().rev().collect::<Vec<_>>().join("\n")
        })
        .unwrap_or_default()
}

fn health_once(port: u16) -> Result<(), String> {
    let mut stream = TcpStream::connect(("127.0.0.1", port)).map_err(|e| e.to_string())?;
    stream
        .set_read_timeout(Some(Duration::from_secs(2)))
        .map_err(|e| e.to_string())?;
    stream
        .write_all(b"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
        .map_err(|e| e.to_string())?;
    let mut response = String::new();
    stream
        .read_to_string(&mut response)
        .map_err(|e| e.to_string())?;
    if response.starts_with("HTTP/1.1 200") || response.starts_with("HTTP/1.0 200") {
        Ok(())
    } else {
        Err("health endpoint did not return 200".to_string())
    }
}

fn ensure_ffmpeg(data_dir: &Path) -> Result<PathBuf, String> {
    let portable =
        ffmpeg_path(data_dir).ok_or_else(|| "failed to resolve FFmpeg path".to_string())?;
    if portable.is_file() {
        verify_ffmpeg(&portable)?;
        return Ok(portable);
    }

    #[cfg(windows)]
    {
        download_windows_ffmpeg(data_dir)?;
        let portable =
            ffmpeg_path(data_dir).ok_or_else(|| "failed to resolve FFmpeg path".to_string())?;
        verify_ffmpeg(&portable)?;
        return Ok(portable);
    }

    #[cfg(target_os = "macos")]
    {
        download_macos_ffmpeg(data_dir)?;
        let portable =
            ffmpeg_path(data_dir).ok_or_else(|| "failed to resolve FFmpeg path".to_string())?;
        verify_ffmpeg(&portable)?;
        Ok(portable)
    }

    #[cfg(all(unix, not(target_os = "macos")))]
    {
        verify_ffmpeg(Path::new("ffmpeg"))?;
        Ok(PathBuf::from("ffmpeg"))
    }
}

#[cfg(target_os = "macos")]
fn download_macos_ffmpeg(data_dir: &Path) -> Result<(), String> {
    let ffmpeg_url = env::var("STEMDECK_FFMPEG_URL")
        .ok()
        .filter(|value| !value.trim().is_empty())
        .unwrap_or_else(|| DEFAULT_MACOS_FFMPEG_URL.to_string());
    let ffprobe_url = env::var("STEMDECK_FFPROBE_URL")
        .ok()
        .filter(|value| !value.trim().is_empty())
        .unwrap_or_else(|| DEFAULT_MACOS_FFPROBE_URL.to_string());
    let downloads = data_dir.join("downloads");
    let ffmpeg_zip = downloads.join("ffmpeg-macos.zip");
    let ffprobe_zip = downloads.join("ffprobe-macos.zip");
    fs::create_dir_all(&downloads)
        .map_err(|e| format!("failed to create {}: {e}", downloads.display()))?;

    download_file(&ffmpeg_url, &ffmpeg_zip, Duration::from_secs(5 * 60))?;
    download_file(&ffprobe_url, &ffprobe_zip, Duration::from_secs(5 * 60))?;

    let ffmpeg_dir = data_dir.join("ffmpeg");
    fs::create_dir_all(&ffmpeg_dir)
        .map_err(|e| format!("failed to create {}: {e}", ffmpeg_dir.display()))?;
    extract_single_binary_from_zip(&ffmpeg_zip, &ffmpeg_dir.join("ffmpeg"), "ffmpeg")?;
    extract_single_binary_from_zip(&ffprobe_zip, &ffmpeg_dir.join("ffprobe"), "ffprobe")?;

    make_executable(&ffmpeg_dir.join("ffmpeg"))?;
    make_executable(&ffmpeg_dir.join("ffprobe"))?;
    Ok(())
}

#[cfg(target_os = "macos")]
fn extract_single_binary_from_zip(
    archive_path: &Path,
    target: &Path,
    binary_name: &str,
) -> Result<(), String> {
    let file = fs::File::open(archive_path)
        .map_err(|e| format!("failed to open {}: {e}", archive_path.display()))?;
    let mut archive = zip::ZipArchive::new(file)
        .map_err(|e| format!("failed to read zip {}: {e}", archive_path.display()))?;

    for i in 0..archive.len() {
        let mut entry = archive
            .by_index(i)
            .map_err(|e| format!("failed to read zip entry {i}: {e}"))?;
        if !entry.is_file() {
            continue;
        }
        let Some(name) = entry.enclosed_name() else {
            continue;
        };
        let Some(file_name) = name.file_name().and_then(|value| value.to_str()) else {
            continue;
        };
        if file_name != binary_name {
            continue;
        }
        let mut output = fs::File::create(target)
            .map_err(|e| format!("failed to create {}: {e}", target.display()))?;
        std::io::copy(&mut entry, &mut output)
            .map_err(|e| format!("failed to extract {}: {e}", target.display()))?;
        return Ok(());
    }

    Err(format!(
        "downloaded archive {} did not contain {binary_name}",
        archive_path.display()
    ))
}

#[cfg(target_os = "macos")]
fn make_executable(path: &Path) -> Result<(), String> {
    let output = Command::new("chmod")
        .args(["+x", &path.display().to_string()])
        .stderr(Stdio::piped())
        .output()
        .map_err(|e| format!("failed to chmod {}: {e}", path.display()))?;
    if output.status.success() {
        Ok(())
    } else {
        Err(format!(
            "failed to chmod {}: {}",
            path.display(),
            String::from_utf8_lossy(&output.stderr).trim()
        ))
    }
}

#[cfg(windows)]
fn download_windows_ffmpeg(data_dir: &Path) -> Result<(), String> {
    let url = env::var("STEMDECK_FFMPEG_URL")
        .ok()
        .filter(|value| !value.trim().is_empty())
        .unwrap_or_else(|| DEFAULT_WINDOWS_FFMPEG_URL.to_string());
    let downloads = data_dir.join("downloads");
    let archive_path = downloads.join("ffmpeg-windows.zip");
    fs::create_dir_all(&downloads)
        .map_err(|e| format!("failed to create {}: {e}", downloads.display()))?;

    download_file_with_powershell(&url, &archive_path)?;

    extract_ffmpeg_binaries(&archive_path, data_dir)
}

#[cfg(windows)]
fn download_file_with_powershell(url: &str, target: &Path) -> Result<(), String> {
    let target_str = target.display().to_string();
    // Embed url and path directly — PowerShell 5.1 -Command consumes the
    // entire remaining argv, so $args[] is always empty when passed this way.
    let script = format!(
        "$ProgressPreference = 'SilentlyContinue'; \
         Invoke-WebRequest -Uri '{url}' -OutFile '{target_str}'"
    );
    let mut command = Command::new("powershell.exe");
    command
        .args([
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            &script,
        ])
        .stdout(Stdio::null())
        .stderr(Stdio::piped());
    hide_console_window(&mut command);
    let output =
        command_output_with_timeout(command, Duration::from_secs(5 * 60), "FFmpeg download")?;
    if output.status.success() && target.is_file() {
        Ok(())
    } else {
        let stderr = String::from_utf8_lossy(&output.stderr);
        Err(format!(
            "failed to download FFmpeg from {url}: {}",
            stderr.trim()
        ))
    }
}

#[cfg(windows)]
fn extract_ffmpeg_binaries(archive_path: &Path, data_dir: &Path) -> Result<(), String> {
    let file = fs::File::open(archive_path)
        .map_err(|e| format!("failed to open {}: {e}", archive_path.display()))?;
    let mut archive = ZipArchive::new(file)
        .map_err(|e| format!("failed to read FFmpeg zip {}: {e}", archive_path.display()))?;
    let ffmpeg_dir = data_dir.join("ffmpeg");
    fs::create_dir_all(&ffmpeg_dir)
        .map_err(|e| format!("failed to create {}: {e}", ffmpeg_dir.display()))?;

    let mut copied_ffmpeg = false;
    let mut copied_ffprobe = false;
    for i in 0..archive.len() {
        let mut entry = archive
            .by_index(i)
            .map_err(|e| format!("failed to read FFmpeg zip entry {i}: {e}"))?;
        if !entry.is_file() {
            continue;
        }
        let Some(name) = entry.enclosed_name() else {
            continue;
        };
        let Some(file_name) = name.file_name().and_then(|value| value.to_str()) else {
            continue;
        };
        let target_name = match file_name.to_ascii_lowercase().as_str() {
            "ffmpeg.exe" => {
                copied_ffmpeg = true;
                "ffmpeg.exe"
            }
            "ffprobe.exe" => {
                copied_ffprobe = true;
                "ffprobe.exe"
            }
            _ => continue,
        };
        let target = ffmpeg_dir.join(target_name);
        let mut output = fs::File::create(&target)
            .map_err(|e| format!("failed to create {}: {e}", target.display()))?;
        std::io::copy(&mut entry, &mut output)
            .map_err(|e| format!("failed to extract {}: {e}", target.display()))?;
    }

    if !copied_ffmpeg {
        return Err("downloaded FFmpeg archive did not contain ffmpeg.exe".to_string());
    }
    if !copied_ffprobe {
        return Err("downloaded FFmpeg archive did not contain ffprobe.exe".to_string());
    }
    Ok(())
}

fn verify_ffmpeg(path: &Path) -> Result<(), String> {
    let mut command = Command::new(path);
    command
        .arg("-version")
        .stdout(Stdio::null())
        .stderr(Stdio::piped());
    hide_console_window(&mut command);
    let output = command_output_with_timeout(command, Duration::from_secs(15), "FFmpeg check")
        .map_err(|e| format!("failed to run FFmpeg at {}: {e}", path.display()))?;
    if output.status.success() {
        Ok(())
    } else {
        let stderr = String::from_utf8_lossy(&output.stderr);
        Err(format!(
            "FFmpeg at {} failed verification: {}",
            path.display(),
            stderr.trim()
        ))
    }
}

fn write_setup_config(data_dir: &Path, ffmpeg: &Path) -> Result<(), String> {
    let ffprobe = ffprobe_path(data_dir);
    let updated_at = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or_default();
    update_setup_config(
        data_dir,
        [
            ("setupVersion", serde_json::json!(SETUP_VERSION)),
            ("ffmpegReady", serde_json::json!(true)),
            (
                "ffmpegPath",
                serde_json::json!(ffmpeg.display().to_string()),
            ),
            ("ffprobeReady", serde_json::json!(ffprobe.is_file())),
            (
                "ffprobePath",
                serde_json::json!(ffprobe.display().to_string()),
            ),
            (
                "ffmpegSource",
                serde_json::json!(env::var("STEMDECK_FFMPEG_URL").unwrap_or_else(|_| {
                    if cfg!(windows) {
                        DEFAULT_WINDOWS_FFMPEG_URL.to_string()
                    } else if cfg!(target_os = "macos") {
                        DEFAULT_MACOS_FFMPEG_URL.to_string()
                    } else {
                        "system PATH".to_string()
                    }
                })),
            ),
            ("updatedAt", serde_json::json!(updated_at)),
        ],
    )
}

fn update_setup_config<const N: usize>(
    data_dir: &Path,
    entries: [(&str, serde_json::Value); N],
) -> Result<(), String> {
    let config_path = data_dir.join("config.json");
    let mut config = fs::read_to_string(&config_path)
        .ok()
        .and_then(|text| serde_json::from_str::<serde_json::Value>(&text).ok())
        .filter(|value| value.is_object())
        .unwrap_or_else(|| serde_json::json!({}));

    let Some(object) = config.as_object_mut() else {
        return Err("setup config is not a JSON object".to_string());
    };
    for (key, value) in entries {
        object.insert(key.to_string(), value);
    }
    object
        .entry("modelReady".to_string())
        .or_insert(serde_json::Value::Bool(false));

    let body = serde_json::to_string_pretty(&config)
        .map_err(|e| format!("failed to serialize setup config: {e}"))?;
    fs::write(&config_path, body + "\n")
        .map_err(|e| format!("failed to write {}: {e}", config_path.display()))
}

fn command_output_with_timeout(
    mut command: Command,
    timeout: Duration,
    label: &str,
) -> Result<Output, String> {
    let mut child = command
        .spawn()
        .map_err(|e| format!("failed to start {label}: {e}"))?;
    let deadline = Instant::now() + timeout;

    loop {
        if let Some(status) = child
            .try_wait()
            .map_err(|e| format!("failed to wait for {label}: {e}"))?
        {
            let mut stdout = Vec::new();
            if let Some(mut pipe) = child.stdout.take() {
                let _ = pipe.read_to_end(&mut stdout);
            }

            let mut stderr = Vec::new();
            if let Some(mut pipe) = child.stderr.take() {
                let _ = pipe.read_to_end(&mut stderr);
            }

            return Ok(Output {
                status,
                stdout,
                stderr,
            });
        }

        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            return Err(format!(
                "{label} timed out after {} seconds",
                timeout.as_secs()
            ));
        }

        thread::sleep(Duration::from_millis(100));
    }
}

#[cfg(windows)]
fn hide_console_window(command: &mut Command) {
    use std::os::windows::process::CommandExt;
    const CREATE_NO_WINDOW: u32 = 0x08000000;
    command.creation_flags(CREATE_NO_WINDOW);
}

#[cfg(not(windows))]
fn hide_console_window(_command: &mut Command) {}
