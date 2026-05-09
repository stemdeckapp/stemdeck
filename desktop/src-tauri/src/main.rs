use serde::Serialize;
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
use tauri::Manager;
#[cfg(windows)]
use {std::fs::File, zip::ZipArchive};

const SETUP_VERSION: u64 = 1;
const DEFAULT_WINDOWS_FFMPEG_URL: &str =
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip";

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
            ensure_external_assets,
            ensure_torch_device,
            start_backend,
            open_url,
        ])
        .build(tauri::generate_context!())
        .expect("failed to build StemDeck desktop app")
        .run(|app_handle, event| {
            if let tauri::RunEvent::ExitRequested { .. } = event {
                let state = app_handle.state::<BackendState>();
                stop_backend(&state);
            }
        });
}

#[tauri::command]
fn probe_runtime() -> Result<RuntimeProbe, String> {
    let root = app_root()?;
    let data_dir = root.join("data");
    let python = python_path(&root);
    if let Some(path) = python.as_deref() {
        patch_pyvenv_cfg(path);
    }
    let ffmpeg = ffmpeg_path(&data_dir);
    let torch_device = read_config_str(&data_dir, "torchDevice");
    Ok(RuntimeProbe {
        app_root: root.display().to_string(),
        data_dir: data_dir.display().to_string(),
        python_ready: python.as_ref().is_some_and(|p| p.is_file()),
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
fn ensure_workspace() -> Result<(), String> {
    let root = app_root()?;
    let data = root.join("data");
    for dir in ["cache", "downloads", "ffmpeg", "jobs", "logs", "models"] {
        fs::create_dir_all(data.join(dir))
            .map_err(|e| format!("failed to create data/{dir}: {e}"))?;
    }
    if is_cpu_only_package(&root) {
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
    let root = app_root()?;
    let data_dir = root.join("data");
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
    let data_dir = root.join("data");
    let python = python_path(&root).filter(|p| p.is_file()).ok_or_else(|| {
        "Python runtime not found. Expected python/ or .venv/ under StemDeck.".to_string()
    })?;
    patch_pyvenv_cfg(&python);
    let port = free_port()?;
    let url = format!("http://127.0.0.1:{port}");
    let log_path = data_dir.join("logs").join("backend.log");
    if let Some(parent) = log_path.parent() {
        fs::create_dir_all(parent)
            .map_err(|e| format!("failed to create backend log directory: {e}"))?;
    }
    let log_file = File::create(&log_path)
        .map_err(|e| format!("failed to create backend log {}: {e}", log_path.display()))?;
    let log_file_for_stderr = log_file
        .try_clone()
        .map_err(|e| format!("failed to prepare backend log {}: {e}", log_path.display()))?;

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
    cmd.current_dir(&backend_dir)
        .env("STEMDECK_DATA_DIR", &data_dir)
        .env("STEMDECK_DESKTOP", "1")
        .env("PYTHONUNBUFFERED", "1")
        .env("XDG_CACHE_HOME", data_dir.join("cache"))
        .env("TORCH_HOME", data_dir.join("models").join("torch"))
        .stdout(Stdio::from(log_file))
        .stderr(Stdio::from(log_file_for_stderr));

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
    let data_dir = root.join("data");

    // CPU-only portable build: skip GPU detection and pip entirely.
    if is_cpu_only_package(&root) {
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

fn is_cpu_only_package(root: &Path) -> bool {
    root.join("cpu-only").is_file() || root.join("data").join("cpu-only").is_file()
}

fn persist_torch_device(data_dir: &std::path::Path, device: &str) {
    let _ = update_setup_config(
        data_dir,
        [("torchDevice", serde_json::Value::String(device.to_string()))],
    );
}

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

fn cuda_index_url(cuda_version: &str) -> String {
    format!(
        "https://download.pytorch.org/whl/{}",
        cuda_tag(cuda_version)
    )
}

fn cuda_tag_from_url(index_url: &str) -> &str {
    index_url.rsplit('/').next().unwrap_or("cu124")
}

/// Update pyvenv.cfg to the bundled Python runtime. Windows venv launchers read
/// this file before Python starts, so stale build-machine paths can prevent the
/// backend from emitting any log output at all.
fn patch_pyvenv_cfg(python: &Path) {
    let Some(scripts_dir) = python.parent() else {
        return;
    };
    let Some(venv_root) = scripts_dir.parent() else {
        return;
    };
    let bundled_python = venv_root.join(if cfg!(windows) {
        "python.exe"
    } else {
        "python"
    });
    if !bundled_python.is_file() || !venv_root.join("Lib").join("os.py").is_file() {
        return;
    }
    let cfg_path = venv_root.join("pyvenv.cfg");
    let Ok(content) = fs::read_to_string(&cfg_path) else {
        return;
    };
    let root_str = venv_root.display().to_string();
    let python_str = bundled_python.display().to_string();
    let patched: String = content
        .lines()
        .map(|line| {
            let trimmed = line.trim_start();
            if trimmed.starts_with("home") && trimmed[4..].trim_start().starts_with('=') {
                format!("home = {root_str}")
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
    #[cfg(not(windows))]
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
    let candidates = if cfg!(windows) {
        vec![
            root.join("python").join("Scripts").join("python.exe"),
            root.join("python").join("python.exe"),
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

    #[cfg(not(windows))]
    {
        verify_ffmpeg(Path::new("ffmpeg"))?;
        Ok(PathBuf::from("ffmpeg"))
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
    let file = File::open(archive_path)
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
        let mut output = File::create(&target)
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
                serde_json::json!(env::var("STEMDECK_FFMPEG_URL")
                    .unwrap_or_else(|_| DEFAULT_WINDOWS_FFMPEG_URL.to_string())),
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
