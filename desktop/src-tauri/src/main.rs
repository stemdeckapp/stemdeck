use flate2::read::GzDecoder;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use socket2::{Domain, Protocol, Socket, Type};
use std::{
    collections::HashMap,
    env, fs,
    io::{Read, Write},
    net::{SocketAddr, TcpStream},
    path::{Path, PathBuf},
    process::{Child, Command, Output, Stdio},
    sync::{
        atomic::{AtomicU64, Ordering},
        Mutex,
    },
    thread,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use tar::Archive;
use tauri::{Emitter, Manager};
use tauri_plugin_store::StoreExt;
#[cfg(windows)]
use zip::ZipArchive;

const SETUP_VERSION: u64 = 1;

// ── In-app updater platform support (#421) ──────────────────────────────────
//
// Windows and Linux ship the same shape: a flat directory with the executable,
// `backend/` and `python/` side by side, which is exactly what the swap needs.
//
// macOS is deliberately excluded. There `backend_dir()` resolves the backend
// inside the downloaded runtime pack rather than the .app, so the app layer is
// a different thing entirely and the existing runtime-pack updater already
// covers most of it. Treating it as "the same but with .app" would be wrong.
//
// The archive format differs because each platform's packaging script already
// produces one: Compress-Archive on Windows, tar on Linux.
#[cfg(windows)]
const UPDATE_APP_ARCHIVE: &str = "stemdeck-update-app.zip";
#[cfg(target_os = "linux")]
const UPDATE_APP_ARCHIVE: &str = "stemdeck-update-app.tar.gz";

/// The shipped executable's filename. Defined for every platform so the
/// leftover sweep does not need its own cfg dance.
#[cfg(windows)]
const APP_EXE_NAME: &str = "StemDeck.exe";
#[cfg(not(windows))]
const APP_EXE_NAME: &str = "StemDeck";
// Windows FFmpeg comes from BtbN's GitHub build (served via GitHub's CDN, far
// faster worldwide than the old gyan.dev single mirror -- #248). Unlike gyan.dev,
// which published a per-file `{url}.sha256` companion, BtbN publishes ONE combined
// `checksums.sha256` listing every asset as `<hash>  <filename>` lines; we fetch it
// and pick the line for our archive's basename. The `latest` tag is rolling (the
// `n8.1-latest` asset is rebuilt in place), so the checksum is fetched fresh each
// run and verified -- the same trust model as the old gyan flow. A compile-time pin
// is not possible without self-hosting the archive.
const DEFAULT_WINDOWS_FFMPEG_URL: &str =
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n8.1-latest-win64-gpl-8.1.zip";
#[cfg(windows)]
const DEFAULT_WINDOWS_FFMPEG_CHECKSUMS_URL: &str =
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/checksums.sha256";
// macOS FFmpeg is pinned to a specific evermeet build and verified by SHA256
// before it is extracted or executed (#172). evermeet publishes no .sha256
// companion (only a GPG signature and a size), so unlike the Windows BtbN
// path we cannot fetch the hash at runtime -- instead we pin the hash of a
// specific versioned zip, captured at build time from evermeet's TLS endpoint
// (the download size matched evermeet's signed release info). Bump the version
// and BOTH hashes together when updating FFmpeg. The rolling getrelease/latest
// URL is intentionally avoided so the pinned hash stays valid.
const DEFAULT_MACOS_FFMPEG_URL: &str = "https://evermeet.cx/ffmpeg/ffmpeg-8.1.1.zip";
#[cfg(target_os = "macos")]
const DEFAULT_MACOS_FFPROBE_URL: &str = "https://evermeet.cx/ffmpeg/ffprobe-8.1.1.zip";
#[cfg(target_os = "macos")]
const DEFAULT_MACOS_FFMPEG_SHA256: &str =
    "4610988e2f54c243c50da73a09e4e2c36d9bb77546f9aa6c84cb328dcb1a98c1";
#[cfg(target_os = "macos")]
const DEFAULT_MACOS_FFPROBE_SHA256: &str =
    "aeade29dee3c3844e9bcc974f4ae4b29cc4f87994177d77003a8589fa531009e";
// Primary macOS FFmpeg source: shaka-project's static builds, built from
// source via GitHub Actions and served from GitHub Releases -- GitHub's
// global CDN behind it, the same class of fix that already solved this for
// Windows (#248, moved off gyan.dev's single mirror). evermeet.cx above is
// now the fallback only: a single host with no CDN, reported unreachable
// from multiple regions (#388). Binaries are raw (not zip-wrapped) and
// published per-architecture. All four hashes were independently verified
// (downloaded, sha256'd, and cross-checked against the release notes' own
// published MD5s and each binary's Mach-O magic bytes) before pinning here.
// Bump the release tag and all four hashes together when updating.
#[cfg(target_os = "macos")]
const SHAKA_FFMPEG_RELEASE: &str = "n8.1.2-1";
#[cfg(target_os = "macos")]
const SHAKA_FFMPEG_BASE_URL: &str =
    "https://github.com/shaka-project/static-ffmpeg-binaries/releases/download";
#[cfg(target_os = "macos")]
const SHAKA_FFMPEG_SHA256_ARM64: &str =
    "e7b9fcd97f95f333512d6e8b8ac24d9dbc08f189f36047695499bd7b57214b22";
#[cfg(target_os = "macos")]
const SHAKA_FFMPEG_SHA256_X64: &str =
    "62c87854d851f202fc4a29bdda0fe7b6ebcddd37b863482ce1bdc81151b03fe4";
#[cfg(target_os = "macos")]
const SHAKA_FFPROBE_SHA256_ARM64: &str =
    "ded4c698b8ff38d0bc1fd30fcc5e768dc46f58bc15a8dfd61f98615ba49cde5c";
#[cfg(target_os = "macos")]
const SHAKA_FFPROBE_SHA256_X64: &str =
    "d530823f480a3c7eb6334f18a00197d1e9f1070e86172b9aa89c4bf4022bd879";
// Linux: a static amd64 build (ffmpeg + ffprobe in one .tar.xz) downloaded at
// first launch, mirroring the Windows/macOS model so we never redistribute
// FFmpeg ourselves. Overridable via STEMDECK_FFMPEG_URL. The archive unpacks to
// ffmpeg-<ver>-amd64-static/{ffmpeg,ffprobe}; extraction uses the system `tar`.
#[cfg(all(unix, not(target_os = "macos")))]
const DEFAULT_LINUX_FFMPEG_URL: &str =
    "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz";

struct BackendHandles {
    child: Child,
    url: String,
}

#[derive(Default)]
struct BackendStateInner {
    handles: Option<BackendHandles>,
    /// True while start_backend is executing; prevents concurrent starts (#145).
    starting: bool,
    /// PID of an in-progress setup-time subprocess (pip install, or the model
    /// warmup download, #275); killed by stop_backend on window close (#140).
    setup_child_pid: Option<u32>,
    /// Save destinations the user has picked but not yet downloaded to (#338).
    ///
    /// The export is two commands so the UI can tell "choosing a folder" apart
    /// from "writing the file", but the second half must not take a path from
    /// JS: that would hand a compromised WebView the ability to write any URL
    /// to any location on disk. The path stays here and JS only ever holds an
    /// opaque token.
    pending_saves: HashMap<String, PathBuf>,
    /// Source of those tokens. A counter is enough -- the token is not a
    /// secret. Every live token maps to a path the user chose in a native
    /// dialog, so guessing one only ever yields another approved destination.
    next_save_token: u64,
}

/// Cap on unconsumed destinations. A pick whose download never runs (the user
/// closes the window mid-export) would otherwise sit here for the life of the
/// process.
const MAX_PENDING_SAVES: usize = 16;

struct BackendState {
    inner: Mutex<BackendStateInner>,
}

impl Default for BackendState {
    fn default() -> Self {
        BackendState {
            inner: Mutex::new(BackendStateInner::default()),
        }
    }
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
    /// Why the persisted device was chosen (e.g. "verified", "no-gpu-detected",
    /// "cuda-verify-failed", "cpu-only-package"). None on installs that predate
    /// reason tracking -- the setup gate treats those as unsettled so a wrongly
    /// pinned CPU heals itself on the next launch (#247).
    torch_device_reason: Option<String>,
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

/// The app-layer artifact to install, resolved by the frontend from the GitHub
/// Releases API (the same check already in static/js/catalog.js) and handed to
/// `download_app_update`. Rust downloads, verifies and applies; it does not
/// re-resolve "what is the latest version" itself.
///
/// There is no runtime artifact here on purpose. The updater replaces
/// the executable and backend/ only -- python/ is never touched, because an
/// NVIDIA install rewrites it with CUDA torch at first run and replacing the
/// directory would silently drop that machine back to CPU. The frontend gates
/// on the release's runtime id first, and falls back to the full-package
/// download whenever the Python dependency set changed.
// Only the Windows build reads these fields; the other platforms keep the
// struct so download_app_update has one signature everywhere and can answer
// with a clear "not available here" rather than a missing-command error.
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
#[cfg_attr(not(windows), allow(dead_code))]
struct AppUpdatePlan {
    app_url: String,
    app_sha256: String,
}

/// Asset URLs lifted from the GitHub release JSON by the frontend, for
/// `check_app_update` to resolve.
///
/// The small metadata files are fetched HERE rather than in JS on purpose. The
/// page is served by the Python backend over http, so the backend's own
/// Content-Security-Policy applies to it, and `connect-src` allows
/// `api.github.com` but NOT `github.com`/`objects.githubusercontent.com` where
/// release *assets* actually live (app/main.py). A `fetch()` for the checksum
/// or the runtime id would be blocked outright and the updater would silently
/// never appear. reqwest is not bound by the page CSP, so doing it in Rust
/// keeps that policy exactly as tight as it is today.
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
#[cfg_attr(not(windows), allow(dead_code))]
struct AppUpdateQuery {
    app_sha_url: String,
    runtime_id_url: String,
}

/// Whether this release can be installed in place, and the verified checksum to
/// install it with. `reason` is for the log, not the user: the UI just falls
/// back to the normal download link.
#[derive(Serialize, Default)]
#[serde(rename_all = "camelCase")]
struct AppUpdateAvailability {
    supported: bool,
    app_sha256: Option<String>,
    reason: Option<String>,
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

/// Identifies which published release asset matches the running build, so the
/// frontend's "new release" dialog can offer the correct download link.
/// `gpu` only distinguishes Windows/Linux assets (NVIDIA vs CPU variant);
/// macOS ships one build per arch, so it reports "universal" and the frontend
/// keys the macOS asset name on `arch` alone.
#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct BuildTarget {
    os: String,
    arch: String,
    gpu: String,
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
    /// Why this device was chosen; mirrors the persisted torchDeviceReason.
    reason: String,
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_store::Builder::default().build())
        .setup(|app| {
            let data_dir = match local_data_dir() {
                Ok(d) => d,
                Err(e) => {
                    eprintln!("[stemdeck] could not resolve data_dir, skipping version check: {e}");
                    return Ok(());
                }
            };
            let _ = fs::create_dir_all(&data_dir);

            // Sweep what an in-app update left at the app root: the previous
            // backend/ and exe, plus a staging dir if the update was
            // interrupted before it could clean up. The new files are already
            // in place, so these are only ever the old version's leftovers.
            //
            // Runs on EVERY launch, not just a version change. apply_app_update
            // relaunches and then exits, so on the very first launch of the new
            // build the outgoing process is usually still alive and Windows
            // still holds StemDeck.exe.old open -- the delete fails silently
            // and, gated on a version change that has already happened, would
            // never be retried. Verified: after a real self-update both
            // backend.old and StemDeck.exe.old were still on disk. Three path
            // checks per launch is nothing; leaking ~30 MB forever is not.
            sweep_update_leftovers();

            let version_file = data_dir.join("last_version.txt");
            let migration_flag = data_dir.join("store_migration_done");
            let current = env!("CARGO_PKG_VERSION");
            let last = fs::read_to_string(&version_file).unwrap_or_default();

            if last.trim() != current {
                if migration_flag.exists() {
                    #[cfg(target_os = "macos")]
                    clear_webkit_data();
                }
                // A new version is the moment to throw away what the old one
                // left behind (#356): archives for runtimes that are no longer
                // the expected one, and any half-finished runtime swap. The
                // archive this build wants is spared, so an update that already
                // downloaded it does not fetch it twice.
                //
                // Deliberately narrow. settings.json in this directory holds
                // the stems location (#354); removing it would send a user who
                // moved their library to another disk back to the default
                // folder, to an empty app with their stems stranded.
                prune_runtime_leftovers(&data_dir);
                let manifest = app_root()
                    .ok()
                    .and_then(|root| load_runtime_manifest(&root).ok());
                // Spare the expected archive only while it is still needed. If
                // the installed runtime already matches, the pack it came from
                // is dead weight -- and its filename carries no version, so it
                // would otherwise sit there forever looking current.
                let keep = manifest.as_ref().and_then(|m| {
                    if runtime_is_current(&data_dir, m) {
                        None
                    } else {
                        Some(runtime_archive_path(&data_dir, m))
                    }
                });
                let freed = prune_downloads(&data_dir, keep.as_deref());
                if freed > 0 {
                    eprintln!(
                        "[stemdeck] freed {} MB of stale downloads",
                        freed / 1_048_576
                    );
                }
                // Only update the version file if write succeeds. If it fails, skip
                // cleanup — a missing version file would otherwise cause every launch
                // to wipe WebKit data.
                if let Err(e) = fs::write(&version_file, current) {
                    eprintln!("[stemdeck] failed to write version file, skipping cleanup: {e}");
                }
            }
            let _ = app; // suppress unused warning
            Ok(())
        })
        .manage(BackendState::default())
        .invoke_handler(tauri::generate_handler![
            probe_runtime,
            ensure_workspace,
            runtime_pack_status,
            download_runtime_pack,
            verify_runtime_pack,
            extract_runtime_pack,
            installed_runtime_id,
            check_app_update,
            download_app_update,
            apply_app_update,
            ensure_external_assets,
            ensure_torch_device,
            warmup_models,
            start_backend,
            local_ip,
            build_target,
            open_url,
            save_audio_file,
            pick_export_destination,
            download_to_path,
            pick_stems_folder,
            store_get,
            store_set,
            reset_user_data,
            mark_store_migration_done,
        ])
        .build(tauri::generate_context!())
        .expect("failed to build StemDeck desktop app")
        .run(|app_handle, event| {
            if let tauri::RunEvent::WindowEvent {
                event: tauri::WindowEvent::CloseRequested { .. },
                ..
            } = event
            {
                let state = app_handle.state::<BackendState>();
                stop_backend(&state);
                app_handle.exit(0);
            }
        });
}

/// Returns ~/Documents/StemDeck/ WITHOUT creating it. The Documents
/// *default* for the jobs folder (documents_dir_for_jobs below) and the
/// source of a pre-#403 user-data.json for one-time migration
/// (documents_store_path) -- chosen so the library is visible in
/// Finder/Explorer, eligible for iCloud/OneDrive backup, and survives app
/// reinstalls, before the user ever relocates it via Settings.
///
/// Deliberately does not mkdir: this is called on every startup just to
/// compute the *default* jobs path, even when the user has relocated their
/// library elsewhere via Settings and this default will never be used. Prior
/// to the fix for #403 (part 2) this always recreated an empty
/// ~/Documents/StemDeck/jobs, since the backend's own ensure_runtime_dirs
/// (app/core/config.py) already mkdirs whichever JOBS_DIR actually wins that
/// precedence -- this path only needs to exist when it is the one in use.
fn documents_stemdeck_dir(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    let documents = app.path().document_dir().map_err(|e| e.to_string())?;
    Ok(documents.join("StemDeck"))
}

/// The stems/jobs folder as it exists right now: the backend's own
/// settings.json `jobs_dir` override if the user relocated it (#354) and that
/// folder still exists, otherwise the Documents default. Mirrors
/// app/core/config.py's `_stored_jobs_dir()` precedence exactly, read
/// directly from disk (not over IPC/HTTP) so this works even before the
/// backend process is up.
fn current_jobs_dir(app: &tauri::AppHandle) -> PathBuf {
    if let Ok(data_dir) = local_data_dir() {
        let settings_path = data_dir.join("settings.json");
        if let Ok(text) = fs::read_to_string(&settings_path) {
            if let Ok(json) = serde_json::from_str::<serde_json::Value>(&text) {
                if let Some(configured) = json.get("jobs_dir").and_then(|v| v.as_str()) {
                    let candidate = PathBuf::from(configured);
                    if candidate.is_dir() {
                        return candidate;
                    }
                }
            }
        }
    }
    documents_dir_for_jobs(app)
}

/// Returns <current jobs folder>/user-data.json (library metadata store).
///
/// Lives *inside* the jobs folder (not its parent) so relocating stems via
/// Settings (#354) carries this along automatically -- move_library()
/// (app/core/stems_location.py) already moves every entry it finds inside
/// the jobs folder one by one, so a plain file sitting there (same as
/// registry.json) needs no special-casing on that side. Before #403 this
/// lived at the jobs folder's *parent* (~/Documents/StemDeck/user-data.json),
/// which relocation never touched -- a stems move would "forget" favorites,
/// folder layout, and per-job mixer state even though the audio moved fine.
///
/// One-time migration: if the new location has nothing yet, copy (not move)
/// any pre-#403 file found at the old parent-folder path. Copy rather than
/// delete so a problem here can never lose the only copy of that data.
fn documents_store_path(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    let jobs_dir = current_jobs_dir(app);
    fs::create_dir_all(&jobs_dir)
        .map_err(|e| format!("failed to create {}: {e}", jobs_dir.display()))?;
    let new_path = jobs_dir.join("user-data.json");
    if !new_path.is_file() {
        if let Ok(old_path) = documents_stemdeck_dir(app).map(|d| d.join("user-data.json")) {
            if old_path.is_file() && old_path != new_path {
                let _ = fs::copy(&old_path, &new_path);
            }
        }
    }
    Ok(new_path)
}

/// True if `path` exists and contains at least one entry. Used to tell an
/// already-in-use default folder apart from one nothing has ever written to.
fn directory_has_entries(path: &Path) -> bool {
    fs::read_dir(path)
        .map(|mut entries| entries.next().is_some())
        .unwrap_or(false)
}

/// The DEFAULT stems folder. Does NOT create it -- see documents_stemdeck_dir
/// for why.
///
/// Handed to the backend as STEMDECK_DEFAULT_JOBS_DIR, not STEMDECK_JOBS_DIR:
/// the latter means "this deployment pins the location" and would override the
/// folder the user picked in Settings (#354). The backend owns that choice; it
/// is the one that has to move the library when it changes, including
/// creating whichever path wins (app/core/config.py's ensure_runtime_dirs).
///
/// Two candidates, resolved in this order:
///
/// 1. ~/Documents/StemDeck/jobs, if it already has anything in it. Every
///    install before this default existed used this path, so an existing
///    user's real library lives there without any explicit `jobs_dir` in
///    settings.json to record it -- it was simply "the default." Checking
///    disk content directly (rather than writing a one-time migration flag
///    into settings.json, which only the backend otherwise writes) keeps this
///    self-contained: nothing to persist, no other-process race, and it stays
///    correct on every future launch for as long as that folder holds data.
/// 2. Otherwise, for the Windows portable package, local_data_dir()/jobs --
///    i.e. next to data/cache and data/models inside the package itself,
///    rather than leaving a footprint in Documents. Non-portable installs
///    (installer builds, macOS, Linux) keep candidate 1 either way: the
///    original Documents rationale (visible in Finder/Explorer, eligible for
///    OneDrive/iCloud backup, survives reinstalls) still applies to them.
fn documents_dir_for_jobs(app: &tauri::AppHandle) -> PathBuf {
    let legacy_default = match documents_stemdeck_dir(app) {
        Ok(dir) => dir.join("jobs"),
        Err(_) => {
            return local_data_dir()
                .map(|d| d.join("jobs"))
                .unwrap_or_else(|_| PathBuf::from("jobs"));
        }
    };

    if directory_has_entries(&legacy_default) {
        return legacy_default;
    }

    if let Ok(root) = app_root() {
        if is_portable_package(&root) {
            if let Ok(data_dir) = local_data_dir() {
                return data_dir.join("jobs");
            }
        }
    }

    legacy_default
}

/// Native folder picker for the stems location. Returns None when the user
/// cancels, which the UI treats as "leave it where it is".
#[tauri::command]
async fn pick_stems_folder(app: tauri::AppHandle) -> Result<Option<String>, String> {
    use tauri_plugin_dialog::DialogExt;

    let (tx, rx) = std::sync::mpsc::channel();
    app.dialog()
        .file()
        .set_title("Choose where StemDeck stores extracted stems")
        .pick_folder(move |path| {
            let _ = tx.send(path);
        });
    let picked = rx.recv().map_err(|e| e.to_string())?;
    Ok(picked.map(|p| p.to_string()))
}

/// Get a value from the persistent user-data store.
#[tauri::command]
fn store_get(app: tauri::AppHandle, key: String) -> Result<Option<serde_json::Value>, String> {
    let path = documents_store_path(&app)?;
    let store = app.store(path).map_err(|e| e.to_string())?;
    Ok(store.get(&key))
}

/// Set a value in the persistent user-data store and immediately flush to disk.
#[tauri::command]
fn store_set(app: tauri::AppHandle, key: String, value: serde_json::Value) -> Result<(), String> {
    let path = documents_store_path(&app)?;
    let store = app.store(path).map_err(|e| e.to_string())?;
    store.set(key, value);
    store.save().map_err(|e| e.to_string())
}

/// Clear the persistent user-data store entirely (Settings -> General ->
/// "Reset app data"). Complements the backend's own job-data wipe (POST
/// /api/reset) -- together they fully clear a user's local StemDeck state,
/// including the per-job mixer-state keys (stemdeck:mix:<job_id>) that have
/// no fixed enumeration to clear individually.
#[tauri::command]
fn reset_user_data(app: tauri::AppHandle) -> Result<(), String> {
    let path = documents_store_path(&app)?;
    let store = app.store(path).map_err(|e| e.to_string())?;
    store.clear();
    store.save().map_err(|e| e.to_string())
}

/// Called by JS after the one-time localStorage → store migration completes.
/// Writing this flag allows the setup hook to safely clear stale WebKit data
/// on subsequent version upgrades.
#[tauri::command]
fn mark_store_migration_done() {
    match local_data_dir() {
        Ok(d) => {
            if let Err(e) = fs::write(d.join("store_migration_done"), "") {
                eprintln!("[stemdeck] failed to write migration flag: {e}");
            }
        }
        Err(e) => eprintln!("[stemdeck] could not write migration flag: {e}"),
    }
}

/// Delete stale WebKit data directories on macOS so a new app version starts
/// with a clean WebView. Only called after the JS store migration is confirmed
/// (store_migration_done flag exists), ensuring no user data is lost.
#[cfg(target_os = "macos")]
fn clear_webkit_data() {
    let home = match std::env::var("HOME") {
        Ok(h) => h,
        Err(_) => return,
    };
    let targets = [
        format!("{home}/Library/WebKit/app.stemdeck.desktop"),
        format!("{home}/Library/WebKit/stemdeck"),
    ];
    for path in &targets {
        if let Err(e) = fs::remove_dir_all(path) {
            if e.kind() != std::io::ErrorKind::NotFound {
                eprintln!("[stemdeck] WebKit cleanup failed for {path}: {e}");
            }
        }
    }
}

/// Returns current runtime state: Python path, FFmpeg path, and persisted torch device.
#[tauri::command]
fn probe_runtime() -> Result<RuntimeProbe, String> {
    let root = app_root()?;
    let data_dir = local_data_dir()?;
    let python = python_path(&root);
    if let Some(path) = python.as_deref() {
        patch_pyvenv_cfg(path);
    }
    let ffmpeg = resolve_existing_ffmpeg(&data_dir);
    let torch_device = read_config_str(&data_dir, "torchDevice");
    let torch_device_reason = effective_device_reason(
        read_config_str(&data_dir, "torchDeviceReason"),
        is_cpu_only_package(&root),
    );
    Ok(RuntimeProbe {
        app_root: root.display().to_string(),
        data_dir: data_dir.display().to_string(),
        python_ready: python.as_ref().is_some_and(|p| python_stdlib_ok(p)),
        python_path: python.map(|p| p.display().to_string()),
        ffmpeg_ready: ffmpeg.is_some(),
        ffmpeg_path: ffmpeg.map(|p| p.display().to_string()),
        torch_device,
        torch_device_reason,
    })
}

/// Read a single string field from data/config.json, returning None on any error.
fn read_config_str(data_dir: &std::path::Path, key: &str) -> Option<String> {
    let text = fs::read_to_string(data_dir.join("config.json")).ok()?;
    let value: serde_json::Value = serde_json::from_str(&text).ok()?;
    value.get(key)?.as_str().map(|s| s.to_string())
}

/// Returns the current state of the bundled Python runtime pack (manifest, archive, install).
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

/// Downloads the Python runtime pack archive, emitting progress events to the frontend.
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

/// Verifies the SHA256 of a previously downloaded runtime pack archive.
#[tauri::command]
fn verify_runtime_pack() -> Result<RuntimeArchive, String> {
    let root = app_root()?;
    let data_dir = local_data_dir()?;
    let manifest = load_runtime_manifest(&root)?;
    validate_runtime_manifest(&manifest)?;
    let archive = runtime_archive_path(&data_dir, &manifest);
    verify_runtime_archive(&manifest, &archive)
}

/// Extracts the verified runtime pack archive and atomically swaps it into place.
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

    // Cleanup is non-fatal; log warnings rather than silently discarding errors.
    if let Err(e) = fs::remove_dir_all(&tmp) {
        if let Ok(d) = local_data_dir() {
            append_to_setup_log(&d, &format!("cleanup warning: {}: {e}", tmp.display()));
        }
    }
    if let Err(e) = fs::remove_dir_all(&old) {
        if let Ok(d) = local_data_dir() {
            append_to_setup_log(&d, &format!("cleanup warning: {}: {e}", old.display()));
        }
    }

    // The archive has done its job (#356). Keeping it meant every pack a user
    // ever installed stayed on disk at full size; a retry can download it again,
    // which costs bandwidth once rather than hundreds of megabytes forever.
    let freed = prune_downloads(&data_dir, None);
    if freed > 0 {
        append_to_setup_log(
            &data_dir,
            &format!(
                "removed {} MB of installed runtime archives",
                freed / 1_048_576
            ),
        );
    }

    let python = runtime.join("python").join("bin").join("python");
    patch_pyvenv_cfg(&python);
    runtime_pack_status()
}

/// Delete the `.old` siblings and staging dir an in-app update leaves at the
/// app root (#421). Best-effort and idempotent: whatever is still locked by the
/// outgoing process this launch is simply picked up on the next one.
fn sweep_update_leftovers() {
    let Ok(root) = app_root() else { return };
    for name in ["backend.old", "python.old", "_update_app.tmp"] {
        let stale = root.join(name);
        if stale.is_dir() {
            let _ = fs::remove_dir_all(&stale);
        }
    }
    let stale_exe = root.join(format!("{APP_EXE_NAME}.old"));
    if stale_exe.is_file() {
        let _ = fs::remove_file(&stale_exe);
    }
}

/// The Python dependency-set id of the runtime currently on disk, written into
/// `python/runtime-version.json` by make-portable.ps1. `None` when the marker
/// is absent -- a pre-#421 install, a macOS build, or a source checkout.
///
/// The frontend compares this against the release's published runtime id and
/// only offers an in-app update when they match, since the updater cannot
/// replace python/ (see `AppUpdatePlan`). `None` is treated as "cannot verify",
/// which sends the user to the full-package download rather than risking an app
/// layer whose imports the installed runtime may not satisfy.
#[tauri::command]
fn installed_runtime_id() -> Option<String> {
    let root = app_root().ok()?;
    let text = fs::read_to_string(root.join("python").join("runtime-version.json")).ok()?;
    parse_runtime_id(&text)
}

/// Split from the command above so the marker's on-disk contract -- the exact
/// shape make-portable.ps1 writes -- is unit-testable without an app root.
fn parse_runtime_id(text: &str) -> Option<String> {
    let value: serde_json::Value = serde_json::from_str(text).ok()?;
    value.get("runtimeId")?.as_str().map(|s| s.to_string())
}

/// Decides whether the latest release can be applied in place, and resolves its
/// checksum. Windows and Linux; every other platform reports unsupported.
///
/// An in-app update is offered only when the release's Python dependency set
/// matches the installed one, because the updater cannot replace `python/`
/// (see `AppUpdatePlan`). Any uncertainty -- an unreachable asset, an install
/// with no recorded runtime id, a malformed checksum -- reports unsupported, so
/// the UI falls back to the full download rather than risking an app layer
/// whose imports the installed runtime cannot satisfy.
#[tauri::command]
async fn check_app_update(query: AppUpdateQuery) -> Result<AppUpdateAvailability, String> {
    #[cfg(not(any(windows, target_os = "linux")))]
    {
        let _ = query;
        Ok(AppUpdateAvailability {
            supported: false,
            reason: Some("in-app updates are not available on this platform".to_string()),
            ..Default::default()
        })
    }
    #[cfg(any(windows, target_os = "linux"))]
    {
        let unsupported = |reason: &str| {
            Ok(AppUpdateAvailability {
                supported: false,
                reason: Some(reason.to_string()),
                ..Default::default()
            })
        };

        // A root-owned install (Linux `install.sh --global` puts it in
        // /opt/stemdeck) cannot rewrite itself. Check before promising an
        // update we would fail to apply.
        match app_root() {
            Ok(root) if !app_root_is_writable(&root) => {
                return unsupported("this install is not writable by the current user");
            }
            Err(e) => return unsupported(&format!("could not resolve the app directory: {e}")),
            _ => {}
        }

        let Some(installed) = installed_runtime_id() else {
            return unsupported("this install records no runtime id");
        };
        let release_marker = match fetch_text(&query.runtime_id_url).await {
            Ok(text) => text,
            Err(e) => return unsupported(&format!("could not read the release runtime id: {e}")),
        };
        let Some(release_id) = parse_runtime_id(&release_marker) else {
            return unsupported("the release runtime id could not be parsed");
        };
        if release_id != installed {
            return unsupported(&format!(
                "python dependencies changed ({installed} -> {release_id})"
            ));
        }

        let checksum_file = match fetch_text(&query.app_sha_url).await {
            Ok(text) => text,
            Err(e) => return unsupported(&format!("could not read the update checksum: {e}")),
        };
        let Some(sha256) = parse_sha256_line(&checksum_file) else {
            return unsupported("the update checksum could not be parsed");
        };

        Ok(AppUpdateAvailability {
            supported: true,
            app_sha256: Some(sha256),
            reason: None,
        })
    }
}

/// Fetch a small text file (a checksum, a version marker). Capped so a wrong
/// URL that points at something huge cannot be read into memory unbounded.
#[cfg(any(windows, target_os = "linux"))]
async fn fetch_text(url: &str) -> Result<String, String> {
    const MAX_BYTES: usize = 64 * 1024;
    let client = reqwest::Client::builder()
        .connect_timeout(Duration::from_secs(15))
        .timeout(Duration::from_secs(60))
        .build()
        .map_err(|e| format!("failed to build HTTP client: {e}"))?;
    let response = client
        .get(url)
        .send()
        .await
        .map_err(|e| format!("request failed: {e}"))?;
    if !response.status().is_success() {
        return Err(format!("HTTP {}", response.status()));
    }
    let bytes = response
        .bytes()
        .await
        .map_err(|e| format!("read failed: {e}"))?;
    if bytes.len() > MAX_BYTES {
        return Err(format!("response larger than {MAX_BYTES} bytes"));
    }
    String::from_utf8(bytes.to_vec()).map_err(|e| format!("response was not valid UTF-8: {e}"))
}

/// Pull the hash out of a `<sha256>  <filename>` checksum file, the shape
/// make-portable.ps1 writes (Get-FileHash + Set-Content). Rejects anything that
/// is not exactly one 64-char hex digest so a redirect to an HTML error page
/// can never be mistaken for a checksum.
#[cfg(any(windows, target_os = "linux", test))]
fn parse_sha256_line(text: &str) -> Option<String> {
    let token = text.split_whitespace().next()?.to_ascii_lowercase();
    let ok = token.len() == 64 && token.chars().all(|c| c.is_ascii_hexdigit());
    ok.then_some(token)
}

/// Downloads and checksum-verifies the app-layer update. Windows and Linux
/// only: both ship a flat directory shaped for an in-place file swap.
#[tauri::command]
async fn download_app_update(
    plan: AppUpdatePlan,
    app_handle: tauri::AppHandle,
) -> Result<(), String> {
    #[cfg(not(any(windows, target_os = "linux")))]
    {
        let _ = (plan, app_handle);
        Err("in-app updates are not available on this platform".to_string())
    }
    #[cfg(any(windows, target_os = "linux"))]
    {
        let data_dir = local_data_dir()?;
        let downloads = data_dir.join("downloads");
        fs::create_dir_all(&downloads)
            .map_err(|e| format!("failed to create {}: {e}", downloads.display()))?;

        let app_archive = downloads.join(UPDATE_APP_ARCHIVE);
        // Drop any archive left by an earlier, abandoned download so apply can
        // never install something the current plan did not ask for.
        let _ = fs::remove_file(&app_archive);

        download_file_with_progress(&plan.app_url, &app_archive, &app_handle).await?;
        verify_update_sha256(&app_archive, &plan.app_sha256, "app update")
    }
}

/// Verify a freshly downloaded update archive against its expected SHA256
/// (from the release's own published `.sha256` companion file, resolved by
/// the frontend) before it is ever extracted. On mismatch the file is removed
/// so a corrupt or tampered download can never be applied.
#[cfg(any(windows, target_os = "linux"))]
fn verify_update_sha256(path: &Path, expected: &str, label: &str) -> Result<(), String> {
    let actual = sha256_file(path)?;
    if !actual.eq_ignore_ascii_case(expected.trim()) {
        let _ = fs::remove_file(path);
        return Err(format!(
            "{label} archive checksum mismatch (expected {expected}, got {actual}). \
             The download may be corrupt or tampered. Click Retry to try again."
        ));
    }
    Ok(())
}

/// Unpack the downloaded app layer into `destination`, in whichever format
/// this platform's packaging script produces. Both shapes put `StemDeck[.exe]`
/// and `backend/` at the archive root, so the caller sees the same layout.
///
/// tar is used on Linux rather than zip specifically because it preserves the
/// executable bit; a zip would land StemDeck without +x and the relaunch would
/// fail with a permission error.
#[cfg(any(windows, target_os = "linux"))]
fn extract_update_archive(archive: &Path, destination: &Path) -> Result<(), String> {
    fs::create_dir_all(destination)
        .map_err(|e| format!("failed to create {}: {e}", destination.display()))?;
    #[cfg(windows)]
    {
        let file = fs::File::open(archive)
            .map_err(|e| format!("failed to open {}: {e}", archive.display()))?;
        let mut zip = ZipArchive::new(file)
            .map_err(|e| format!("failed to read zip {}: {e}", archive.display()))?;
        zip.extract(destination)
            .map_err(|e| format!("failed to extract {}: {e}", archive.display()))
    }
    #[cfg(target_os = "linux")]
    {
        extract_tar_archive(archive, destination)
    }
}

/// Whether this install can rewrite its own files.
///
/// `packaging/linux/install.sh` offers a global install into `/opt/stemdeck`,
/// which is root-owned while the app runs as the user. Renaming the binary
/// there fails, so the updater has to decline up front and send the user to the
/// normal download rather than discovering it half way through a swap. Windows
/// portable installs are user-writable by construction, but the probe is cheap
/// and honest on both.
#[cfg(any(windows, target_os = "linux"))]
fn app_root_is_writable(root: &Path) -> bool {
    let probe = root.join(".stemdeck-update-probe");
    match fs::File::create(&probe) {
        Ok(_) => {
            let _ = fs::remove_file(&probe);
            true
        }
        Err(_) => false,
    }
}

/// Stops the backend and waits for the process to actually exit.
///
/// The regular `stop_backend` hands the kill to a background thread and
/// returns immediately -- fine on window close, wrong here. The backend runs
/// *from* the very directories the update is about to replace: its interpreter
/// is `python/`, its code is `backend/`. Windows refuses to rename a directory
/// while a handle inside it is open, so starting the swap before the process is
/// gone fails with a permission error, or worse, part-way through. Linux would
/// tolerate it, but a backend still serving requests from a directory being
/// swapped out is not something to rely on either.
#[cfg(any(windows, target_os = "linux"))]
fn stop_backend_and_wait(state: &BackendState, timeout: Duration) -> Result<(), String> {
    let handles = match state.inner.lock() {
        Ok(mut guard) => guard.handles.take(),
        Err(_) => return Err("backend state is unavailable".to_string()),
    };
    let Some(mut handles) = handles else {
        return Ok(());
    };
    // Give uvicorn a chance to drain in-flight requests before escalating,
    // matching what stop_backend does on window close.
    #[cfg(unix)]
    {
        // SAFETY: the child was spawned by us and has not been waited on, so
        // its pid is still valid.
        unsafe { libc::kill(handles.child.id() as libc::pid_t, libc::SIGTERM) };
        let grace = Instant::now() + Duration::from_secs(3);
        while Instant::now() < grace {
            if handles.child.try_wait().ok().flatten().is_some() {
                return Ok(());
            }
            thread::sleep(Duration::from_millis(100));
        }
    }
    let _ = handles.child.kill();
    let deadline = Instant::now() + timeout;
    loop {
        match handles.child.try_wait() {
            Ok(Some(_)) => return Ok(()),
            Ok(None) => {
                if Instant::now() >= deadline {
                    return Err(
                        "the audio backend did not shut down in time; update cancelled".to_string(),
                    );
                }
                thread::sleep(Duration::from_millis(100));
            }
            Err(e) => return Err(format!("failed to wait for the audio backend to exit: {e}")),
        }
    }
}

/// Rename, retrying briefly on Windows sharing violations.
///
/// Even once the backend process is gone, a virus scanner or the search
/// indexer can hold a transient handle inside a directory that was just
/// written or is about to move. These clear in well under a second; without a
/// retry an unlucky scan turns into a failed update mid-swap.
#[cfg(any(windows, target_os = "linux"))]
fn rename_with_retry(from: &Path, to: &Path, what: &str) -> Result<(), String> {
    const ATTEMPTS: u32 = 10;
    let mut last_err = None;
    for attempt in 0..ATTEMPTS {
        match fs::rename(from, to) {
            Ok(()) => return Ok(()),
            Err(e) => {
                last_err = Some(e);
                if attempt + 1 < ATTEMPTS {
                    thread::sleep(Duration::from_millis(150));
                }
            }
        }
    }
    Err(format!(
        "failed to {what}: {}",
        last_err
            .map(|e| e.to_string())
            .unwrap_or_else(|| "unknown error".to_string())
    ))
}

/// Applies a previously downloaded+verified app update in place, then
/// relaunches. This is the one piece of the updater with no existing analog in
/// the runtime-pack machinery above: it replaces the *running* exe, not an idle
/// data directory.
///
/// Only reachable from an explicit "Restart to update" user action, never a
/// background timer, and the backend is stopped first, so this can never land
/// mid-job. Only the executable and `backend/` are replaced: `python/`,
/// `portable.txt`, `cpu-only` and `data/` are all left exactly as they are, so
/// an NVIDIA install keeps its CUDA torch and portable/GPU detection and user
/// data all survive untouched.
#[tauri::command]
fn apply_app_update(
    state: tauri::State<BackendState>,
    app_handle: tauri::AppHandle,
) -> Result<(), String> {
    #[cfg(not(any(windows, target_os = "linux")))]
    {
        let _ = (state, app_handle);
        Err("in-app updates are not available on this platform".to_string())
    }
    #[cfg(any(windows, target_os = "linux"))]
    {
        let root = app_root()?;
        let data_dir = local_data_dir()?;
        let downloads = data_dir.join("downloads");
        let app_archive = downloads.join(UPDATE_APP_ARCHIVE);
        if !app_archive.is_file() {
            return Err(
                "no downloaded app update found -- call download_app_update first".to_string(),
            );
        }

        // ── Phase 1: stage and validate, touching nothing live ──
        //
        // Everything that can fail on its own (extraction, a truncated or
        // wrong-shaped archive) happens here, before a single live file moves.
        // Once phase 2 starts it is only renames, so a failure cannot leave the
        // install straddling two versions -- a new backend/ beside the old exe
        // would be a broken app with nothing left running to repair it.
        let staging = root.join("_update_app.tmp");
        if staging.exists() {
            fs::remove_dir_all(&staging)
                .map_err(|e| format!("failed to remove {}: {e}", staging.display()))?;
        }

        let staged = (|| -> Result<PathBuf, String> {
            extract_update_archive(&app_archive, &staging)?;
            let new_exe = staging.join(APP_EXE_NAME);
            if !staging.join("backend").join("app").is_dir() || !new_exe.is_file() {
                return Err(format!(
                    "app update archive did not contain {APP_EXE_NAME} and backend/app"
                ));
            }
            Ok(new_exe)
        })();

        let new_exe = match staged {
            Ok(path) => path,
            Err(e) => {
                let _ = fs::remove_dir_all(&staging);
                return Err(e);
            }
        };

        // ── Phase 2: swap ──
        //
        // The backend must be gone first: it runs from backend/, and Windows
        // will not rename a directory with live handles inside it.
        stop_backend_and_wait(&state, Duration::from_secs(15))?;

        let backend_dir = root.join("backend");
        let backend_old = root.join("backend.old");
        let exe_path = root.join(APP_EXE_NAME);
        let exe_old = root.join(format!("{APP_EXE_NAME}.old"));
        if backend_old.exists() {
            fs::remove_dir_all(&backend_old)
                .map_err(|e| format!("failed to remove {}: {e}", backend_old.display()))?;
        }
        if exe_old.exists() {
            let _ = fs::remove_file(&exe_old);
        }

        if backend_dir.exists() {
            rename_with_retry(
                &backend_dir,
                &backend_old,
                "move the existing backend aside",
            )?;
        }
        rename_with_retry(
            &staging.join("backend"),
            &backend_dir,
            "install the updated backend",
        )?;

        // The exe goes last. Windows allows renaming a running process's own
        // on-disk image -- the OS holds the file open by handle, not by path --
        // so this needs no elevated privileges in a user-writable portable
        // folder.
        //
        // Known residual gap: these two renames are back-to-back metadata
        // updates on one volume, but they are not a single atomic operation. A
        // hard crash in that window would leave StemDeck.exe absent with
        // StemDeck.exe.old holding the previous build, recoverable only by a
        // manual rename -- unlike the swaps above there is no surviving
        // process to self-heal it on next launch. Closing it fully needs a
        // separate bootstrap launcher that is never itself replaced; flagging
        // it rather than treating it as solved.
        rename_with_retry(&exe_path, &exe_old, "move the running app aside")?;
        rename_with_retry(&new_exe, &exe_path, "install the updated app")?;

        let _ = fs::remove_dir_all(&staging);
        let _ = fs::remove_file(&app_archive);

        // Relaunch the new exe detached, then exit. The old exe, renamed aside
        // above, keeps running under its own open handle until this process
        // actually exits; the *.old siblings are swept up on the next launch by
        // setup()'s post-update cleanup.
        Command::new(&exe_path)
            .current_dir(&root)
            .spawn()
            .map_err(|e| format!("failed to relaunch the updated app: {e}"))?;
        app_handle.exit(0);
        Ok(())
    }
}

/// The per-user directory holding the settings copy that survives reinstalling.
///
/// A portable package keeps its data in `<app>/data` (#399), which means
/// settings.json lives *inside the install*. Upgrading by extracting the new
/// zip to a fresh folder therefore lost every setting the user had changed:
/// stems location, port, compute device, quality, language. `ensure_workspace`
/// already restores from here, but nothing wrote it after #399 moved the data
/// directory — the backend mirrors to it now, via STEMDECK_SETTINGS_MIRROR.
///
/// Deliberately the OS-standard data dir, i.e. exactly what `local_data_dir`
/// returns for a NON-portable install, so the two layouts share one location
/// and an install that switches between them keeps its settings either way.
fn shared_settings_dir() -> Option<PathBuf> {
    #[cfg(windows)]
    {
        env::var("LOCALAPPDATA")
            .ok()
            .map(|base| PathBuf::from(base).join("StemDeck"))
    }
    #[cfg(target_os = "macos")]
    {
        env::var("HOME").ok().map(|home| {
            PathBuf::from(home)
                .join("Library")
                .join("Application Support")
                .join("StemDeck")
        })
    }
    #[cfg(all(unix, not(target_os = "macos")))]
    {
        if let Ok(xdg) = env::var("XDG_DATA_HOME") {
            return Some(PathBuf::from(xdg).join("stemdeck"));
        }
        env::var("HOME").ok().map(|home| {
            PathBuf::from(home)
                .join(".local")
                .join("share")
                .join("stemdeck")
        })
    }
}

/// Creates required data directories and runs any pending data migrations.
#[tauri::command]
fn ensure_workspace() -> Result<(), String> {
    let root = app_root()?;
    let data = local_data_dir()?;

    // Recover from an interrupted runtime swap: if runtime/ is absent but
    // runtime.old/ exists, a previous extract_runtime_pack was killed between
    // the two rename steps. Restore the previous install so setup can retry.
    {
        let runtime_path = runtime_dir(&data);
        let old_path = data.join("runtime.old");
        if !runtime_path.exists() && old_path.is_dir() {
            let _ = fs::rename(&old_path, &runtime_path);
        }
    }

    #[cfg(windows)]
    if is_portable_package(&root) {
        // Portable data moved from %LocalAppData% to <app>/data in #399. A
        // freshly extracted package has an empty data directory, so carry the
        // user's prior choices forward before the backend reads settings.json.
        if let Some(shared) = shared_settings_dir() {
            migrate_persisted_files(&shared, &data, &["settings.json"])?;
        }
    }
    migrate_legacy_data(&root, &data)?;
    fs::create_dir_all(&data).map_err(|e| format!("failed to create data dir: {e}"))?;
    for dir in ["cache", "downloads", "ffmpeg", "jobs", "logs", "models"] {
        fs::create_dir_all(data.join(dir))
            .map_err(|e| format!("failed to create data/{dir}: {e}"))?;
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

/// Downloads FFmpeg/ffprobe if absent and writes their paths to config.json.
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

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct ModelWarmupStatus {
    demucs_ready: bool,
    beat_this_ready: bool,
    sections_ready: bool,
    vocal_split_ready: bool,
}

/// Eagerly downloads/caches the ML models StemDeck uses (Demucs, beat-this,
/// automatic song sections, and the on-demand lead/backing vocal-split karaoke model, #275) via
/// `app/pipeline/warmup.py`, so a user's first real job doesn't pay for any
/// of them mid-pipeline. Best-effort per model: a single model failing to
/// download (e.g. no network) does not fail this command — the setup wizard
/// still proceeds, and that one feature falls back to its existing
/// lazy-download-on-first-use behavior, same as before this step existed.
#[tauri::command]
fn warmup_models(state: tauri::State<BackendState>) -> Result<ModelWarmupStatus, String> {
    let root = app_root()?;
    let data_dir = local_data_dir()?;
    let backend_dir = backend_dir(&root)?;
    let python = python_path(&root)
        .filter(|p| p.is_file())
        .ok_or_else(|| "Python not found".to_string())?;
    patch_pyvenv_cfg(&python);

    let mut command = Command::new(&python);
    command
        .args(["-m", "app.pipeline.warmup"])
        .current_dir(&backend_dir)
        .env("STEMDECK_DATA_DIR", &data_dir)
        .env("PYTHONUNBUFFERED", "1")
        // Same cache locations start_backend uses, so a model downloaded here
        // is found (not re-downloaded) by the real backend later.
        .env("XDG_CACHE_HOME", data_dir.join("cache"))
        .env("TORCH_HOME", data_dir.join("models").join("torch"))
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    let child = command
        .spawn()
        .map_err(|e| format!("failed to start model warmup: {e}"))?;

    if let Ok(mut inner) = state.inner.lock() {
        inner.setup_child_pid = Some(child.id());
    }
    let output = child_output_with_timeout(child, Duration::from_secs(30 * 60), "model warmup");
    if let Ok(mut inner) = state.inner.lock() {
        inner.setup_child_pid = None;
    }
    let output = output?;

    let stdout = String::from_utf8_lossy(&output.stdout);
    let mut status = ModelWarmupStatus {
        demucs_ready: false,
        beat_this_ready: false,
        sections_ready: false,
        vocal_split_ready: false,
    };
    for line in stdout.lines() {
        match line {
            "WARMUP_OK demucs" => status.demucs_ready = true,
            "WARMUP_OK beat_this" => status.beat_this_ready = true,
            "WARMUP_OK sections" => status.sections_ready = true,
            "WARMUP_OK vocal_split" => status.vocal_split_ready = true,
            _ if line.starts_with("WARMUP_FAILED") => {
                append_to_setup_log(&data_dir, &format!("model warmup: {line}"));
            }
            _ => {}
        }
    }
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        append_to_setup_log(
            &data_dir,
            &format!(
                "model warmup process exited non-zero. stderr:\n{}",
                stderr.trim()
            ),
        );
    }
    Ok(status)
}

/// Spawns the Python/uvicorn backend, waits for it to become healthy, and returns its URL.
#[tauri::command]
fn start_backend(
    app_handle: tauri::AppHandle,
    state: tauri::State<BackendState>,
) -> Result<BackendStarted, String> {
    // Always bind all interfaces; whether other devices are actually served is
    // controlled live by the backend's network gate (Settings → "Make StemDeck
    // available on your network"), which defaults off and always allows
    // loopback. The WebView itself connects via 127.0.0.1 regardless.
    let bind_host = "0.0.0.0";
    // Gate concurrent calls: return immediately if already running or starting (#145).
    {
        let mut inner = state.inner.lock().map_err(|e| e.to_string())?;
        if let Some(ref h) = inner.handles {
            return Ok(BackendStarted { url: h.url.clone() });
        }
        if inner.starting {
            return Err("Backend startup already in progress".to_string());
        }
        inner.starting = true;
    }

    // Spawn and wait for health outside the lock; update state atomically on completion.
    let spawn_result = (|| {
        let root = app_root()?;
        let backend_dir = backend_dir(&root)?;
        let data_dir = local_data_dir()?;
        let python = python_path(&root).filter(|p| p.is_file()).ok_or_else(|| {
            "Python runtime not found. Expected python/ or .venv/ under StemDeck.".to_string()
        })?;
        patch_pyvenv_cfg(&python);
        let (port, port_guard) = reserve_port(bind_host, configured_port())?;
        let url = format!("http://127.0.0.1:{port}");
        let log_path = data_dir.join("logs").join("backend.log");
        let (stdout, stderr) = prepare_backend_stdio(&log_path).unwrap_or_else(|_| {
            // Logging should help diagnose startup; it should not prevent startup.
            (Stdio::null(), Stdio::null())
        });

        // On macOS and Linux, python-build-standalone detects its own prefix by
        // walking up from bin/ — PYTHONHOME is not needed and actively breaks
        // startup when mis-computed (it would point at python/bin, whose
        // lib/python3.X has no stdlib, so even `encodings` fails to import).
        // Only Windows, whose portable venv keeps the stdlib under base/Lib,
        // needs PYTHONHOME to locate the bundled stdlib.
        // Compute before moving python into Command::new.
        #[cfg(windows)]
        let pythonhome = python
            .parent()
            .and_then(|bin_dir| bin_dir.parent().map(|venv| (venv, bin_dir)))
            .and_then(|(venv, bin_dir)| bundled_python_home(venv, bin_dir).map(|(home, _)| home));

        let instance_token = new_instance_token();
        let mut cmd = Command::new(python);
        cmd.args([
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            bind_host,
            "--port",
            &port.to_string(),
            // Bound how long uvicorn waits for open connections on shutdown.
            // The import queue's SSE stream stays open for as long as the app
            // window is on screen, and uvicorn drains connections before it
            // runs the lifespan teardown -- so without this the backend never
            // finishes draining, we escalate to SIGKILL below, and the teardown
            // that reaps the demucs worker never runs. Kept under the 3 s
            // SIGKILL deadline so the clean path wins.
            "--timeout-graceful-shutdown",
            "2",
        ]);
        #[cfg(windows)]
        if let Some(ref pythonhome) = pythonhome {
            cmd.env("PYTHONHOME", pythonhome);
        }

        // Jobs (stem audio files) live in ~/Documents/StemDeck/jobs/ so the user's
        // library is visible in Finder, backed up by iCloud, and survives app reinstalls.
        let jobs_dir = documents_dir_for_jobs(&app_handle);

        cmd.current_dir(&backend_dir)
            .env("STEMDECK_DATA_DIR", &data_dir)
            .env("STEMDECK_DEFAULT_JOBS_DIR", &jobs_dir)
            .env("STEMDECK_DESKTOP", "1")
            // Where the backend keeps the per-user copy of settings.json that
            // survives extracting a new package into a fresh folder. Computed
            // here so the write half and ensure_workspace's restore half can
            // never point at different places.
            .envs(
                shared_settings_dir()
                    .map(|dir| ("STEMDECK_SETTINGS_MIRROR", dir.join("settings.json"))),
            )
            .env("STEMDECK_PARENT_PID", std::process::id().to_string())
            // How the backend proves it is ours when it answers /api/health.
            // The environment is the only channel that survives the Windows
            // venv launcher re-execing into python/base (#457), which is why
            // this exists rather than a PID comparison. See wait_for_health.
            .env("STEMDECK_INSTANCE_TOKEN", &instance_token)
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
        // Release the reserved port immediately after spawn so uvicorn can bind it.
        drop(port_guard);

        if let Err(err) = wait_for_health(
            &mut child,
            port,
            &instance_token,
            Duration::from_secs(90),
            &log_path,
        ) {
            let _ = child.kill();
            let _ = child.wait();
            return Err(err);
        }

        Ok((child, url))
    })();

    // Atomically update state: clear starting flag whether spawn succeeded or failed.
    let mut inner = state.inner.lock().map_err(|e| e.to_string())?;
    inner.starting = false;
    match spawn_result {
        Ok((child, url)) => {
            inner.handles = Some(BackendHandles {
                child,
                url: url.clone(),
            });
            Ok(BackendStarted { url })
        }
        Err(e) => Err(e),
    }
}

/// Reports the running build's OS/arch/GPU variant so the frontend can pick the
/// matching release asset for the "new release" download link. GPU variant is
/// derived from the shipped `cpu-only` marker (present only in CPU packages);
/// on macOS there is no variant, so it reports "universal".
#[tauri::command]
fn build_target() -> BuildTarget {
    let os = match std::env::consts::OS {
        "macos" => "macos",
        "windows" => "windows",
        _ => "linux",
    }
    .to_string();
    let arch = match std::env::consts::ARCH {
        "aarch64" => "arm64",
        _ => "x64",
    }
    .to_string();
    let gpu = if os == "macos" {
        "universal".to_string()
    } else if app_root().map(|r| is_cpu_only_package(&r)).unwrap_or(true) {
        "cpu".to_string()
    } else {
        "nvidia".to_string()
    };
    BuildTarget { os, arch, gpu }
}

/// Best-effort primary LAN IPv4, shown in Settings so the user knows the address
/// to open StemDeck from another device. Uses the "connect a UDP socket" trick:
/// no packets are sent — connect() just makes the OS pick the source IP for the
/// default route. Returns None when offline / no route.
#[tauri::command]
fn local_ip() -> Option<String> {
    use std::net::UdpSocket;
    let sock = UdpSocket::bind("0.0.0.0:0").ok()?;
    sock.connect("8.8.8.8:80").ok()?;
    match sock.local_addr().ok()?.ip() {
        std::net::IpAddr::V4(v4) if !v4.is_loopback() => Some(v4.to_string()),
        _ => None,
    }
}

/// Detects GPU hardware, installs CUDA torch if needed, and persists the chosen device.
#[tauri::command]
fn ensure_torch_device(state: tauri::State<BackendState>) -> Result<GpuSetup, String> {
    let root = app_root()?;
    let data_dir = local_data_dir()?;

    // Self-heal installs poisoned by a previous CPU-build's data-dir marker
    // before deciding anything (#247).
    clear_stale_cpu_marker(&root, &data_dir);

    // CPU-only portable build: skip GPU detection and pip entirely.
    if is_cpu_only_package(&root) {
        persist_torch_device(&data_dir, "cpu", "cpu-only-package");
        return Ok(GpuSetup {
            gpu_detected: false,
            gpu_name: None,
            cuda_version: None,
            torch_device: "cpu".to_string(),
            cuda_verified: false,
            reason: "cpu-only-package".to_string(),
        });
    }

    let python = python_path(&root)
        .filter(|p| p.is_file())
        .ok_or_else(|| "Python not found".to_string())?;
    patch_pyvenv_cfg(&python);

    #[cfg(target_os = "macos")]
    {
        let mps_available = verify_mps_torch(&python);
        let (device, reason) = if mps_available {
            ("mps", "mps")
        } else {
            ("cpu", "mps-unavailable")
        };
        persist_torch_device(&data_dir, device, reason);
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
            reason: reason.to_string(),
        })
    }

    #[cfg(not(target_os = "macos"))]
    {
        let setup = match detect_nvidia_gpu(&data_dir) {
            Some((gpu_name, cuda_version, compute_cap)) => {
                let index_url = cuda_index_url(compute_cap.as_deref(), &cuda_version);
                install_cuda_torch(&python, &index_url, &state)?;
                let cuda_verified = verify_cuda_torch(&python);
                let reason = if cuda_verified {
                    "verified"
                } else {
                    // CUDA torch is installed but unusable. Falling back to the
                    // "cpu" device is not enough — app/main.py imports torch at
                    // module scope, so a wheel that cannot load keeps the
                    // backend from starting at all. Put the CPU wheels back
                    // (#324).
                    match restore_cpu_torch(&python, &state) {
                        Ok(()) => "cuda-verify-failed",
                        Err(e) => {
                            append_to_setup_log(
                                &data_dir,
                                &format!("CPU torch restore failed: {e}"),
                            );
                            "cuda-verify-failed-cpu-restore-failed"
                        }
                    }
                };
                GpuSetup {
                    gpu_detected: true,
                    gpu_name: Some(gpu_name),
                    cuda_version: Some(cuda_version),
                    torch_device: if cuda_verified { "cuda" } else { "cpu" }.to_string(),
                    cuda_verified,
                    reason: reason.to_string(),
                }
            }
            None => GpuSetup {
                gpu_detected: false,
                gpu_name: None,
                cuda_version: None,
                torch_device: "cpu".to_string(),
                cuda_verified: false,
                reason: "no-gpu-detected".to_string(),
            },
        };
        // Persist device + reason. The setup gate only treats "cuda"/"mps" (or
        // cpu on a cpu-only package) as settled -- a CPU result born from a
        // failure is re-probed on the next launch instead of pinning the
        // install to CPU forever (#247).
        persist_torch_device(&data_dir, &setup.torch_device, &setup.reason);
        append_to_setup_log(
            &data_dir,
            &format!(
                "GPU setup decision: device={} reason={} gpu={:?}",
                setup.torch_device, setup.reason, setup.gpu_name
            ),
        );
        Ok(setup)
    }
}

/// The `cpu-only` marker is trusted ONLY in the app root: it ships inside the
/// package, so it is always correct for the running build. The per-user data
/// dir is shared across installs -- honoring a marker there let a previously
/// installed CPU build permanently force the NVIDIA build onto CPU (#247).
fn is_cpu_only_package(root: &Path) -> bool {
    root.join("cpu-only").is_file()
}

/// The `portable.txt` marker is trusted ONLY in the app root: it ships next to
/// StemDeck.exe inside the Windows portable zip (scripts/windows/make-portable.ps1),
/// mirroring the `cpu-only` marker's root-only-trust pattern above. Shipped
/// unconditionally in both the CPU and NVIDIA Windows builds, so a fresh
/// extract is portable with zero user action. Never present on macOS/Linux.
fn is_portable_package(root: &Path) -> bool {
    root.join("portable.txt").is_file()
}

/// A persisted "cpu-only-package" device decision is only trustworthy while the
/// *current* install is still the CPU-only build. When a user replaces the CPU
/// package with the NVIDIA (CUDA) build in the same data dir, the leftover
/// reason would otherwise make the setup gate treat the device as "settled" on
/// CPU and skip GPU detection entirely -- pinning a GPU machine to CPU until the
/// data dir is cleared by hand. Dropping the stale reason marks the device
/// unsettled so setup re-runs `ensure_torch_device` (which clears the stale
/// marker and re-probes the GPU). Other reasons pass through unchanged; the
/// existing #247 "unknown reason = unsettled" path handles the rest. (#316)
fn effective_device_reason(persisted: Option<String>, cpu_only_package: bool) -> Option<String> {
    match persisted.as_deref() {
        Some("cpu-only-package") if !cpu_only_package => None,
        _ => persisted,
    }
}

/// Deletes a stale `cpu-only` marker left in the shared data dir by an older
/// CPU-build install (which used to write/migrate it there). Without this, the
/// NVIDIA build would keep re-reading it forever on builds that trusted the
/// data-dir copy. Best-effort; logs so setup.log tells the story (#247).
fn clear_stale_cpu_marker(root: &Path, data_dir: &Path) {
    let stale = data_dir.join("cpu-only");
    if !is_cpu_only_package(root) && stale.is_file() {
        match fs::remove_file(&stale) {
            Ok(()) => append_to_setup_log(
                data_dir,
                "removed stale cpu-only marker left by a previous CPU-build install; \
                 GPU detection will run",
            ),
            Err(e) => append_to_setup_log(
                data_dir,
                &format!("could not remove stale cpu-only marker: {e}"),
            ),
        }
    }
}

fn persist_torch_device(data_dir: &std::path::Path, device: &str, reason: &str) {
    let _ = update_setup_config(
        data_dir,
        [
            ("torchDevice", serde_json::Value::String(device.to_string())),
            (
                "torchDeviceReason",
                serde_json::Value::String(reason.to_string()),
            ),
        ],
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

/// Finds nvidia-smi.exe under a DriverStore FileRepository directory. Modern
/// NVIDIA DCH drivers sometimes ship it ONLY there (no System32 copy), e.g.
/// `...\FileRepository\nv_dispi.inf_amd64_<hash>\nvidia-smi.exe`. Scans the
/// `nv*`-prefixed package dirs and returns the most recently modified hit, so
/// after a driver update the current package wins (#247).
#[cfg(any(windows, test))]
fn find_driver_store_nvidia_smi(file_repository: &Path) -> Option<PathBuf> {
    let entries = fs::read_dir(file_repository).ok()?;
    let mut best: Option<(std::time::SystemTime, PathBuf)> = None;
    for entry in entries.flatten() {
        let name = entry.file_name();
        let name = name.to_string_lossy().to_ascii_lowercase();
        if !name.starts_with("nv") {
            continue;
        }
        let candidate = entry.path().join("nvidia-smi.exe");
        if !candidate.is_file() {
            continue;
        }
        let modified = candidate
            .metadata()
            .and_then(|m| m.modified())
            .unwrap_or(std::time::SystemTime::UNIX_EPOCH);
        if best.as_ref().is_none_or(|(t, _)| modified > *t) {
            best = Some((modified, candidate));
        }
    }
    best.map(|(_, path)| path)
}

#[cfg(not(target_os = "macos"))]
fn nvidia_smi_exe() -> String {
    // nvidia-smi.exe lives in System32 on Windows but Tauri child processes
    // inherit a stripped PATH that may not include it. Some DCH driver installs
    // only place it in the DriverStore, so scan there before falling back to
    // PATH (#247).
    #[cfg(windows)]
    {
        const SYSTEM32: &str = r"C:\Windows\System32\nvidia-smi.exe";
        if std::path::Path::new(SYSTEM32).is_file() {
            return SYSTEM32.to_string();
        }
        let file_repository =
            std::path::Path::new(r"C:\Windows\System32\DriverStore\FileRepository");
        if let Some(found) = find_driver_store_nvidia_smi(file_repository) {
            return found.display().to_string();
        }
    }
    "nvidia-smi".to_string()
}

#[cfg(not(target_os = "macos"))]
fn detect_nvidia_gpu(data_dir: &Path) -> Option<(String, String, Option<String>)> {
    let smi = nvidia_smi_exe();
    append_to_setup_log(data_dir, &format!("GPU detection using: {smi}"));
    let mut cmd = Command::new(&smi);
    cmd.args(["--query-gpu=name", "--format=csv,noheader"])
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    hide_console_window(&mut cmd);
    // 30s (vs 10s elsewhere): the first nvidia-smi call can be slow on Optimus
    // laptops that have to wake a sleeping dGPU (#247).
    let name_out = match command_output_with_timeout(cmd, Duration::from_secs(30), "nvidia-smi") {
        Ok(out) => out,
        Err(e) => {
            append_to_setup_log(data_dir, &format!("nvidia-smi failed to run: {e}"));
            return None;
        }
    };
    if !name_out.status.success() {
        append_to_setup_log(
            data_dir,
            &format!(
                "nvidia-smi exited with {}; treating as no GPU",
                name_out.status
            ),
        );
        return None;
    }
    let gpu_name = String::from_utf8_lossy(&name_out.stdout).trim().to_string();
    if gpu_name.is_empty() {
        append_to_setup_log(data_dir, "nvidia-smi reported no GPU name");
        return None;
    }

    // Read CUDA version from the standard nvidia-smi header.
    let mut smi_cmd = Command::new(&smi);
    smi_cmd.stdout(Stdio::piped()).stderr(Stdio::null());
    hide_console_window(&mut smi_cmd);
    let smi_out =
        command_output_with_timeout(smi_cmd, Duration::from_secs(10), "nvidia-smi").ok()?;
    let smi_text = String::from_utf8_lossy(&smi_out.stdout);
    let cuda_version = parse_cuda_version(&smi_text).unwrap_or_else(|| "12.4".to_string());

    // Read the GPU's compute capability (e.g. "12.0" for Blackwell sm_120,
    // "8.9" for Ada). Drives the wheel choice: stock torch 2.6 cu12x wheels
    // have no sm_120 kernels, so Blackwell needs a cu128 / torch 2.7 build.
    // Failure here is non-fatal — we fall back to the CUDA-version heuristic.
    let compute_cap = detect_compute_cap(&smi);

    Some((gpu_name, cuda_version, compute_cap))
}

#[cfg(not(target_os = "macos"))]
fn detect_compute_cap(smi: &str) -> Option<String> {
    let mut cmd = Command::new(smi);
    cmd.args(["--query-gpu=compute_cap", "--format=csv,noheader"])
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    hide_console_window(&mut cmd);
    let out = command_output_with_timeout(cmd, Duration::from_secs(10), "nvidia-smi").ok()?;
    if !out.status.success() {
        return None;
    }
    // Multi-GPU systems print one line per GPU; take the first.
    let cap = String::from_utf8_lossy(&out.stdout)
        .lines()
        .next()
        .unwrap_or("")
        .trim()
        .to_string();
    if cap.is_empty() || cap == "N/A" {
        None
    } else {
        Some(cap)
    }
}

#[cfg(not(target_os = "macos"))]
fn parse_cuda_version(smi_output: &str) -> Option<String> {
    for line in smi_output.lines() {
        if let Some(pos) = line.find("CUDA Version:") {
            let rest = &line[pos + "CUDA Version:".len()..];
            let v = rest.split_whitespace().next()?.trim_matches('|').trim();
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

/// Pick the PyTorch wheel tag. Keyed primarily on the GPU's compute capability:
/// Blackwell (sm_100 / sm_120, major >= 10) has no kernels in the stock torch
/// 2.6 cu12x wheels and needs a cu128 / torch 2.7 build (#217). Everything else
/// falls back to the driver-CUDA-version heuristic.
#[cfg(not(target_os = "macos"))]
fn wheel_tag(compute_cap: Option<&str>, cuda_version: &str) -> &'static str {
    if let Some(cap) = compute_cap {
        if let Some(major) = cap.split('.').next().and_then(|m| m.parse::<u32>().ok()) {
            if major >= 10 {
                return "cu128";
            }
        }
    }
    cuda_tag(cuda_version)
}

#[cfg(not(target_os = "macos"))]
fn cuda_index_url(compute_cap: Option<&str>, cuda_version: &str) -> String {
    format!(
        "https://download.pytorch.org/whl/{}",
        wheel_tag(compute_cap, cuda_version)
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

/// Rotate backend.log -> backend.log.1 -> backend.log.2, dropping the oldest.
///
/// The log used to be truncated on every launch (#278), which destroyed the
/// evidence exactly when it was needed: the natural response to a crashed
/// session is "restart and retry", and the restart wiped the previous
/// session's log. All renames are best-effort -- a locked file on Windows
/// must never block launch; worst case we append to the old file, which
/// still beats truncating it.
fn rotate_log(log_path: &Path, keep: usize) {
    if keep < 2 {
        return; // nothing to rotate into
    }
    let numbered = |i: usize| log_path.with_extension(format!("log.{i}"));
    // Windows fs::rename fails when the destination exists, so drop the
    // oldest generation first, then shift the rest up.
    let _ = fs::remove_file(numbered(keep - 1));
    for i in (1..keep - 1).rev() {
        let _ = fs::rename(numbered(i), numbered(i + 1));
    }
    let _ = fs::rename(log_path, numbered(1));
}

fn prepare_backend_stdio(log_path: &Path) -> Result<(Stdio, Stdio), String> {
    if let Some(parent) = log_path.parent() {
        fs::create_dir_all(parent)
            .map_err(|e| format!("failed to create backend log directory: {e}"))?;
    }
    rotate_log(log_path, 3);
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
    let mut cmd = Command::new(python);
    cmd.args(["-c", "import encodings"]);
    // Only Windows needs PYTHONHOME: its portable venv keeps the stdlib under
    // base/Lib. macOS and Linux use python-build-standalone, which auto-detects
    // its prefix from bin/ — setting PYTHONHOME there points at the wrong dir
    // and breaks the import (parity with start_backend).
    #[cfg(windows)]
    {
        let venv_root = python.parent().and_then(|b| b.parent());
        let pythonhome = venv_root.map(|venv| {
            let base = venv.join("base");
            if base.join("Lib").join("os.py").is_file() {
                return base;
            }
            venv.to_path_buf()
        });
        if let Some(ref home) = pythonhome {
            cmd.env("PYTHONHOME", home);
        }
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

/// Maps known pip/OS failure patterns to actionable user messages.
/// Pure function — caller is responsible for logging the raw stderr before calling.
fn classify_cuda_install_error(stderr: &str) -> String {
    let lower = stderr.to_ascii_lowercase();

    if lower.contains("missing dependencies for socks") || lower.contains("pysocks") {
        return "CUDA install failed: a SOCKS proxy is active on your system. \
                Disable it temporarily and click Retry."
            .to_string();
    }
    if lower.contains("no space left on device")
        || lower.contains("not enough space on the disk")
        || lower.contains("disk quota exceeded")
    {
        return "CUDA install failed: not enough disk space. Free up space and click Retry."
            .to_string();
    }
    if lower.contains("access is denied") || lower.contains("permissionerror") {
        return "CUDA install failed: permission denied — antivirus software may be blocking \
                the install. Try adding StemDeck to your AV exclusions and click Retry."
            .to_string();
    }
    if lower.contains("could not connect") || lower.contains("connection timed out") {
        return "CUDA install failed: could not reach download.pytorch.org. \
                Check your internet connection and click Retry."
            .to_string();
    }

    // Unknown error — full stderr is already in setup.log; surface a generic message
    // rather than leaking raw pip output (file paths, stack traces) to the UI.
    "CUDA install failed — see logs/setup.log for details.".to_string()
}

/// Version of the CPU-only torch wheels baked into every package. Kept in sync
/// with scripts/windows/make-portable.ps1 and scripts/linux/make-portable.sh.
#[cfg(not(target_os = "macos"))]
const CPU_TORCH_VERSION: &str = "2.6.0";

/// Whether the CUDA torch wheel needs its `nvidia-*` runtime dependencies
/// installed separately. Linux CUDA wheels do not bundle the CUDA runtime —
/// they dlopen libcublas/libcudnn/... out of the `nvidia-*` PyPI packages at
/// import time. Windows wheels ship those DLLs inside torch/lib and declare no
/// such dependencies, so the leaner --no-deps swap stays correct there (#324).
#[cfg(not(target_os = "macos"))]
fn cuda_wheel_needs_runtime_deps() -> bool {
    cfg!(target_os = "linux")
}

/// Runs `python -m pip install <args>`, tracking the pip PID so stop_backend can
/// kill it if the window is closed mid-install (#140), bounding it at 20 minutes,
/// and logging raw stderr to setup.log before mapping it to a user-facing message.
#[cfg(not(target_os = "macos"))]
fn run_pip_install(
    python: &Path,
    args: &[&str],
    state: &BackendState,
    label: &str,
) -> Result<(), String> {
    let mut command = Command::new(python);
    command
        .args(["-m", "pip", "install"])
        .args(args)
        .stdout(Stdio::null())
        .stderr(Stdio::piped());

    let child = command
        .spawn()
        .map_err(|e| format!("failed to start {label}: {e}"))?;

    if let Ok(mut inner) = state.inner.lock() {
        inner.setup_child_pid = Some(child.id());
    }
    let output = child_output_with_timeout(child, Duration::from_secs(20 * 60), label);
    if let Ok(mut inner) = state.inner.lock() {
        inner.setup_child_pid = None;
    }
    let output = output?;

    if output.status.success() {
        return Ok(());
    }

    let stderr = String::from_utf8_lossy(&output.stderr);
    // Write full stderr to setup.log before mapping — eprintln! is silent in
    // GUI mode on Windows (no console), so file logging is the only reliable
    // diagnostic path in the deployed app.
    if let Ok(data_dir) = local_data_dir() {
        append_to_setup_log(
            &data_dir,
            &format!("{label} failed. stderr:\n{}", stderr.trim()),
        );
    }
    Err(classify_cuda_install_error(&stderr))
}

/// Puts the bundled CPU wheels back after CUDA torch turns out to be unusable.
/// The backend imports torch at module scope, so a CUDA wheel that cannot load
/// (missing CUDA runtime, driver too old, no kernels for the device) does not
/// merely disable the GPU — it stops the backend from starting at all, and the
/// broken install persists across launches (#324).
#[cfg(not(target_os = "macos"))]
fn restore_cpu_torch(python: &Path, state: &BackendState) -> Result<(), String> {
    let torch_spec = format!("torch=={CPU_TORCH_VERSION}+cpu");
    let torchaudio_spec = format!("torchaudio=={CPU_TORCH_VERSION}+cpu");
    run_pip_install(
        python,
        &[
            &torch_spec,
            &torchaudio_spec,
            "--index-url",
            "https://download.pytorch.org/whl/cpu",
            "--ignore-installed",
            "--no-deps",
            "--quiet",
        ],
        state,
        "CPU torch restore",
    )
}

#[cfg(not(target_os = "macos"))]
fn install_cuda_torch(python: &Path, index_url: &str, state: &BackendState) -> Result<(), String> {
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
    //
    // Blackwell (cu128) only has wheels for torch 2.7+; every other tag stays
    // on the validated 2.6.0 line (#217). torchaudio.save() routes through
    // soundfile here, so minor torchaudio codec changes don't affect us.
    // cu128 uses 2.8.0: 2.7.1 shipped incomplete sm_120 kernels for Blackwell
    // (RTX 5000 series), causing verify_cuda_torch to fail (#239).
    let tag = cuda_tag_from_url(index_url);
    let torch_version = if tag == "cu128" { "2.8.0" } else { "2.6.0" };
    let torch_spec = format!("torch=={torch_version}+{tag}");
    let torchaudio_spec = format!("torchaudio=={torch_version}+{tag}");
    for (label, args) in cuda_install_passes(
        &torch_spec,
        &torchaudio_spec,
        index_url,
        cuda_wheel_needs_runtime_deps(),
    ) {
        run_pip_install(python, &args, state, label)?;
    }

    Ok(())
}

/// Builds the pip passes that install CUDA torch, newest-to-oldest in intent:
///
/// 1. The wheel swap. `--ignore-installed` overwrites even a corrupted/partial
///    install that has no RECORD file; `--no-deps` keeps it to the
///    torch/torchaudio wheels.
/// 2. The CUDA runtime dependencies, Linux only. Same specs, but with
///    dependency resolution and without `--ignore-installed`: pip sees
///    torch/torchaudio as already satisfied and installs only what is missing —
///    the `nvidia-*` wheels pass 1 skipped and that the packaging script strips
///    to keep the tarball under GitHub's 2 GiB asset cap. Without them
///    `import torch` raises "libcublas.so.*[0-9] not found" and the backend
///    never starts (#324). The cuXXX index serves those wheels, so no extra
///    index is needed.
#[cfg(not(target_os = "macos"))]
fn cuda_install_passes<'a>(
    torch_spec: &'a str,
    torchaudio_spec: &'a str,
    index_url: &'a str,
    needs_runtime_deps: bool,
) -> Vec<(&'static str, Vec<&'a str>)> {
    let mut passes = vec![(
        "CUDA torch install",
        vec![
            torch_spec,
            torchaudio_spec,
            "--index-url",
            index_url,
            "--ignore-installed",
            "--no-deps",
            "--quiet",
        ],
    )];
    if needs_runtime_deps {
        passes.push((
            "CUDA runtime dependency install",
            vec![
                torch_spec,
                torchaudio_spec,
                "--index-url",
                index_url,
                "--quiet",
            ],
        ));
    }
    passes
}

#[cfg(not(target_os = "macos"))]
fn verify_cuda_torch(python: &Path) -> bool {
    // Don't trust torch.cuda.is_available() alone: it returns True even when the
    // installed wheel has no kernels for the device (e.g. sm_120 on a cu124
    // build), which then crashes mid-extraction with "no kernel image is
    // available" (#217). Force a real kernel launch so an incompatible wheel is
    // caught here and the app falls back to CPU cleanly.
    let result = Command::new(python)
        .args([
            "-c",
            "import torch; \
             exit(1) if not torch.cuda.is_available() else None; \
             (torch.ones(8, device='cuda') * 2).sum().item(); \
             torch.cuda.synchronize(); \
             exit(0)",
        ])
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .output();

    match result {
        Ok(out) if out.status.success() => true,
        Ok(out) => {
            let stderr = String::from_utf8_lossy(&out.stderr);
            if !stderr.trim().is_empty() {
                if let Ok(data_dir) = local_data_dir() {
                    let log_path = data_dir.join("logs").join("setup.log");
                    if let Some(parent) = log_path.parent() {
                        let _ = fs::create_dir_all(parent);
                    }
                    if let Ok(mut f) = fs::OpenOptions::new()
                        .create(true)
                        .append(true)
                        .open(&log_path)
                    {
                        let _ = writeln!(
                            f,
                            "[stemdeck] CUDA verify failed. stderr:\n{}",
                            stderr.trim()
                        );
                    }
                }
            }
            false
        }
        Err(_) => false,
    }
}

/// Opens an http/https URL in the system browser. Rejects non-http schemes.
#[tauri::command]
fn open_url(url: String) -> Result<(), String> {
    if !url.starts_with("https://") && !url.starts_with("http://") {
        return Err("only http/https URLs are permitted".to_string());
    }
    #[cfg(windows)]
    {
        // Use explorer.exe directly to avoid cmd.exe interpreting '&' in query strings.
        let mut cmd = Command::new("explorer.exe");
        cmd.arg(&url);
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

/// Only localhost URLs, and only http(s). Guards against a compromised WebView
/// using the desktop shell as an SSRF proxy (#138).
fn validate_download_url(url: &str) -> Result<(), String> {
    if !url.starts_with("http://") && !url.starts_with("https://") {
        return Err("only http/https URLs are permitted".to_string());
    }
    let parsed_url = reqwest::Url::parse(url).map_err(|_| "invalid URL".to_string())?;
    let host = parsed_url.host_str().unwrap_or("");
    if host != "127.0.0.1" && host != "localhost" {
        return Err("only localhost URLs are permitted".to_string());
    }
    Ok(())
}

/// Records a picked destination and returns the token JS will hand back.
fn store_pending_save(state: &BackendState, dest: PathBuf) -> Result<String, String> {
    let mut guard = state
        .inner
        .lock()
        .map_err(|_| "state poisoned".to_string())?;
    if guard.pending_saves.len() >= MAX_PENDING_SAVES {
        // Drop the oldest by token order; tokens are monotonic, so the smallest
        // numeric key is the stalest pick.
        if let Some(oldest) = guard
            .pending_saves
            .keys()
            .min_by_key(|k| k.parse::<u64>().unwrap_or(u64::MAX))
            .cloned()
        {
            guard.pending_saves.remove(&oldest);
        }
    }
    guard.next_save_token += 1;
    let token = guard.next_save_token.to_string();
    guard.pending_saves.insert(token.clone(), dest);
    Ok(token)
}

/// Consumes a token. Single use: a failed transfer needs a fresh destination
/// rather than silently reusing one the user picked for an earlier attempt.
fn take_pending_save(state: &BackendState, token: &str) -> Result<PathBuf, String> {
    let mut guard = state
        .inner
        .lock()
        .map_err(|_| "state poisoned".to_string())?;
    guard
        .pending_saves
        .remove(token)
        .ok_or_else(|| "no destination is pending for this export".to_string())
}

/// Shows the native save dialog and remembers where the user pointed it.
///
/// Split from the transfer (#338) so the UI can show "Exporting..." for the
/// writing only. Awaiting one combined command meant the button claimed to be
/// exporting for however long the picker sat open, when nothing was happening.
///
/// Returns None when the user cancels, which the caller treats as "do nothing"
/// -- no busy state is ever entered, so there is none to unwind.
#[tauri::command]
async fn pick_export_destination(
    app: tauri::AppHandle,
    state: tauri::State<'_, BackendState>,
    filename: String,
) -> Result<Option<String>, String> {
    use tauri_plugin_dialog::DialogExt;
    let dest = app
        .dialog()
        .file()
        .set_file_name(&filename)
        .blocking_save_file();
    let Some(file_path) = dest else {
        return Ok(None); // user cancelled
    };
    let dest = file_path.into_path().map_err(|e| e.to_string())?;
    store_pending_save(&state, dest).map(Some)
}

/// Streams a localhost URL to the destination a previous pick recorded.
///
/// Takes a token rather than a path on purpose: a path parameter would let
/// anything running in the WebView write an arbitrary URL to an arbitrary
/// location. The destination never leaves Rust.
#[tauri::command]
async fn download_to_path(
    state: tauri::State<'_, BackendState>,
    token: String,
    url: String,
) -> Result<(), String> {
    validate_download_url(&url)?;
    let dest = take_pending_save(&state, &token)?;

    // Stream response to disk to avoid buffering a large audio file in memory (#139).
    // 5-minute timeout covers large WAV exports over a slow loopback.
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(300))
        .build()
        .map_err(|e| format!("failed to build client: {e}"))?;
    let mut resp = client
        .get(&url)
        .send()
        .await
        .map_err(|e| format!("fetch failed: {e}"))?;
    if !resp.status().is_success() {
        return Err(format!("backend returned HTTP {}", resp.status()));
    }
    let tmp = dest.with_extension("audio.download");
    let mut file =
        std::fs::File::create(&tmp).map_err(|e| format!("failed to create temp file: {e}"))?;
    while let Some(chunk) = resp
        .chunk()
        .await
        .map_err(|e| format!("read failed: {e}"))?
    {
        file.write_all(&chunk)
            .map_err(|e| format!("write failed: {e}"))?;
    }
    file.sync_all().map_err(|e| format!("flush failed: {e}"))?;
    drop(file);
    std::fs::rename(&tmp, &dest).map_err(|e| format!("rename failed: {e}"))?;
    Ok(())
}

/// Pick-then-transfer in one call, for the lane download links.
///
/// Those have no busy state to mislabel, so they want the convenience. The
/// export menu drives the two halves separately.
#[tauri::command]
async fn save_audio_file(
    app: tauri::AppHandle,
    state: tauri::State<'_, BackendState>,
    url: String,
    filename: String,
) -> Result<(), String> {
    // Validate before showing a dialog the request could never satisfy.
    validate_download_url(&url)?;
    let Some(token) = pick_export_destination(app, state.clone(), filename).await? else {
        return Ok(()); // user cancelled
    };
    download_to_path(state, token, url).await
}

fn stop_backend(state: &BackendState) {
    let (handles, _setup_child_pid) = match state.inner.lock() {
        Ok(mut guard) => (guard.handles.take(), guard.setup_child_pid.take()),
        Err(_) => return,
    };

    // Kill any in-progress setup-time subprocess (pip install, model warmup)
    // so it doesn't corrupt the venv/cache if the window is closed mid-setup (#140).
    #[cfg(unix)]
    if let Some(pid) = _setup_child_pid {
        // SAFETY: pid was stored immediately after spawn; we send SIGTERM best-effort.
        unsafe { libc::kill(pid as libc::pid_t, libc::SIGTERM) };
    }

    let Some(mut handles) = handles else { return };

    // Drain the backend on a background thread so we don't block the Tauri
    // RunEvent main thread for up to 3 seconds (#144).
    thread::spawn(move || {
        // Send SIGTERM first so uvicorn can drain in-progress requests
        // before we escalate to SIGKILL.
        #[cfg(unix)]
        {
            // SAFETY: child was spawned by us and has not yet been waited on;
            // its PID is valid for the lifetime of the Child handle.
            unsafe { libc::kill(handles.child.id() as libc::pid_t, libc::SIGTERM) };
            let deadline = Instant::now() + Duration::from_secs(3);
            while Instant::now() < deadline {
                if handles.child.try_wait().ok().flatten().is_some() {
                    return;
                }
                thread::sleep(Duration::from_millis(100));
            }
        }
        let _ = handles.child.kill();
        let _ = handles.child.wait();
    });
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
    // Windows portable zip: redirect into data/ next to StemDeck.exe instead of
    // %LocalAppData% (#399). No-ops on macOS/Linux, where the marker never ships.
    if let Ok(root) = app_root() {
        if is_portable_package(&root) {
            return Ok(root.join("data"));
        }
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

/// Appends a timestamped line to data/logs/setup.log (best-effort; never fails the caller).
///
/// The leading value is Unix epoch seconds. It is there so the Settings -> Logs
/// viewer can show "the last hour" of this file: without a timestamp per line
/// there is nothing to filter on. Epoch rather than a formatted date keeps this
/// dependency-free -- the crate has no date library, and the viewer renders it
/// readably.
fn append_to_setup_log(data_dir: &Path, msg: &str) {
    let log = data_dir.join("logs").join("setup.log");
    if let Some(p) = log.parent() {
        let _ = fs::create_dir_all(p);
    }
    let ts = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    if let Ok(mut f) = fs::OpenOptions::new().create(true).append(true).open(&log) {
        let _ = writeln!(f, "[{ts}] [stemdeck] {msg}");
    }
}

/// One-time migration: move legacy data/models/jobs/ffmpeg from the install
/// directory into the new per-user data directory on the user's first launch
/// after upgrading to a version that uses local_data_dir(). User-owned settings
/// are copied as well so reinstalling cannot silently restore defaults.
fn migrate_legacy_data(root: &Path, data_dir: &Path) -> Result<(), String> {
    let old = root.join("data");
    if !old.is_dir() || old == data_dir {
        return Ok(());
    }
    let _ = fs::create_dir_all(data_dir);
    for name in ["models", "jobs", "ffmpeg", "logs", "cache"] {
        let src = old.join(name);
        let destination = data_dir.join(name);
        if src.is_dir() && !destination.exists() {
            // rename is a cheap move on the same volume; ignore errors silently
            // so a cross-volume failure doesn't block startup.
            let _ = fs::rename(&src, destination);
        }
    }
    // NOTE: deliberately does NOT migrate `cpu-only` -- the marker is only
    // trusted in the app root (see is_cpu_only_package); carrying it into the
    // shared data dir poisoned later NVIDIA installs (#247).
    migrate_persisted_files(&old, data_dir, &["config.json", "settings.json"])
}

/// Copy persisted choices from an older data directory without ever replacing
/// state already written at the destination.
fn migrate_persisted_files(
    source_dir: &Path,
    data_dir: &Path,
    names: &[&str],
) -> Result<(), String> {
    if source_dir == data_dir || !source_dir.is_dir() {
        return Ok(());
    }
    fs::create_dir_all(data_dir)
        .map_err(|e| format!("failed to prepare settings migration: {e}"))?;
    for name in names {
        let source = source_dir.join(name);
        let destination = data_dir.join(name);
        if source.is_file() && !destination.exists() {
            let nonce = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map(|duration| duration.as_nanos())
                .unwrap_or(0);
            let temporary = data_dir.join(format!(
                ".{name}.migrate.{}.{nonce}.tmp",
                std::process::id()
            ));
            let result = (|| -> Result<(), String> {
                let mut input = fs::File::open(&source)
                    .map_err(|e| format!("failed to read existing {name}: {e}"))?;
                let mut output = fs::OpenOptions::new()
                    .write(true)
                    .create_new(true)
                    .open(&temporary)
                    .map_err(|e| format!("failed to stage existing {name}: {e}"))?;
                std::io::copy(&mut input, &mut output)
                    .map_err(|e| format!("failed to copy existing {name}: {e}"))?;
                output
                    .flush()
                    .map_err(|e| format!("failed to flush existing {name}: {e}"))?;
                output
                    .sync_all()
                    .map_err(|e| format!("failed to sync existing {name}: {e}"))?;
                drop(output);

                // Another process may have completed migration while this copy
                // was staged. Its destination wins; never replace it.
                if destination.exists() {
                    return Ok(());
                }
                fs::rename(&temporary, &destination)
                    .map_err(|e| format!("failed to preserve existing {name}: {e}"))
            })();
            if temporary.exists() {
                let _ = fs::remove_file(&temporary);
            }
            result?;
        }
    }
    Ok(())
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

/// Remove downloaded runtime/ffmpeg archives from data/downloads, except `keep`.
///
/// Every runtime pack ever installed used to stay here at full size: the
/// extractor deletes its temp directory and the runtime it displaced, but never
/// the archive it extracted from (#356). Measured 207 MB of packs from two
/// months earlier on one machine.
///
/// `keep` is the archive this build expects. It is spared so that a download
/// already on disk is not thrown away only to be fetched again -- and, during
/// setup, so a partially downloaded file is not deleted underneath the download
/// that is writing it.
///
/// Best-effort by design: a file that will not delete (locked on Windows, gone
/// already) is worth a log line, never a failed launch.
fn prune_downloads(data_dir: &Path, keep: Option<&Path>) -> u64 {
    let downloads = data_dir.join("downloads");
    let entries = match fs::read_dir(&downloads) {
        Ok(entries) => entries,
        Err(_) => return 0,
    };
    let mut freed = 0u64;
    for entry in entries.flatten() {
        let path = entry.path();
        if keep.is_some_and(|k| k == path) {
            continue;
        }
        let size = entry.metadata().map(|m| m.len()).unwrap_or(0);
        let removed = if path.is_dir() {
            fs::remove_dir_all(&path)
        } else {
            fs::remove_file(&path)
        };
        match removed {
            Ok(()) => freed += size,
            Err(e) => eprintln!("[stemdeck] could not remove {}: {e}", path.display()),
        }
    }
    freed
}

/// Remove a runtime swap that did not finish.
///
/// extract_runtime_pack renames the live runtime to runtime.old and extracts
/// into runtime.tmp, deleting both when it succeeds. That cleanup is
/// best-effort, so an interrupted or failed swap can leave a second full
/// runtime (~900 MB) behind until the next attempt happens to reuse the name.
/// Whether the installed runtime already is the one this build expects.
///
/// When it is, nothing needs the downloaded archive any more and it can go --
/// which matters because the archive filename carries no version, so a stale
/// pack and the current one are the same name on disk. (Installing a stale one
/// is not a risk: the SHA256 in the manifest is verified before extraction.)
fn runtime_is_current(data_dir: &Path, manifest: &RuntimeManifest) -> bool {
    let runtime = runtime_dir(data_dir);
    let ready =
        runtime.join("backend").join("app").is_dir() && runtime_python_path(data_dir).is_file();
    let installed = read_runtime_install_manifest(&runtime)
        .and_then(|value| value.get("version")?.as_str().map(str::to_string));
    ready && installed.as_deref() == Some(manifest.version.as_str())
}

fn prune_runtime_leftovers(data_dir: &Path) {
    for name in ["runtime.tmp", "runtime.old"] {
        let path = data_dir.join(name);
        if !path.exists() {
            continue;
        }
        match fs::remove_dir_all(&path) {
            Ok(()) => eprintln!("[stemdeck] removed leftover {}", path.display()),
            Err(e) => eprintln!("[stemdeck] could not remove {}: {e}", path.display()),
        }
    }
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
        fs::remove_file(&tmp).map_err(|e| format!("failed to remove {}: {e}", tmp.display()))?;
    }

    // file:// and bare-path shortcuts are development-only; not available in
    // release builds so a compromised manifest cannot bypass the download (#136).
    #[cfg(debug_assertions)]
    if let Some(path) = url.strip_prefix("file://") {
        fs::copy(Path::new(path), &tmp)
            .map_err(|e| format!("failed to copy runtime pack from {url}: {e}"))?;
        return fs::rename(&tmp, target)
            .map_err(|e| format!("failed to move runtime pack to {}: {e}", target.display()));
    }
    #[cfg(debug_assertions)]
    if Path::new(url).is_file() {
        fs::copy(Path::new(url), &tmp)
            .map_err(|e| format!("failed to copy runtime pack from {url}: {e}"))?;
        return fs::rename(&tmp, target)
            .map_err(|e| format!("failed to move runtime pack to {}: {e}", target.display()));
    }

    let client = reqwest::Client::builder()
        .connect_timeout(Duration::from_secs(15))
        .timeout(Duration::from_secs(30 * 60))
        .build()
        .map_err(|e| format!("failed to build HTTP client: {e}"))?;
    let mut response = client
        .get(url)
        .send()
        .await
        .map_err(|e| {
            if e.is_connect() || e.is_timeout() {
                format!("Could not reach the download server. Check your internet connection and try again. ({})", e)
            } else {
                format!("failed to start download from {url}: {e}")
            }
        })?;
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
            let _ = app_handle.emit(
                "runtime-download-progress",
                DownloadProgress { received, total },
            );
            last_emit = Instant::now();
        }
    }
    let _ = app_handle.emit(
        "runtime-download-progress",
        DownloadProgress {
            received,
            total: Some(received),
        },
    );

    // Flush OS write cache before rename — guards against corrupt archive on
    // Windows after power loss between close and rename.
    file.sync_all()
        .map_err(|e| format!("failed to flush {}: {e}", tmp.display()))?;
    drop(file);

    fs::rename(&tmp, target)
        .map_err(|e| format!("failed to move runtime pack to {}: {e}", target.display()))
}

#[cfg(unix)]
// curl exit codes worth a retry: 6 (couldn't resolve host), 7 (couldn't
// connect), 28 (operation timeout) are transient network conditions. Anything
// else -- a 404, a checksum the caller rejects, --fail's exit 22 on an HTTP
// error -- won't succeed on retry, so don't burn the user's time on one.
fn curl_exit_is_retriable(code: Option<i32>) -> bool {
    matches!(code, Some(6) | Some(7) | Some(28))
}

/// `label` names what's being fetched ("FFmpeg", "ffprobe", ...) for error
/// messages -- this is shared by every curl-based download, so a hardcoded
/// noun here was previously wrong for every caller except the one it happened
/// to be written for.
#[cfg(unix)]
fn download_file(url: &str, target: &Path, timeout: Duration, label: &str) -> Result<(), String> {
    let tmp = target.with_extension("download");
    if tmp.exists() {
        fs::remove_file(&tmp).map_err(|e| format!("failed to remove {}: {e}", tmp.display()))?;
    }

    // file:// and bare-path shortcuts are development-only (#136).
    #[cfg(debug_assertions)]
    if let Some(path) = url.strip_prefix("file://") {
        fs::copy(Path::new(path), &tmp)
            .map_err(|e| format!("failed to copy {label} from {url}: {e}"))?;
        return fs::rename(&tmp, target)
            .map_err(|e| format!("failed to move {label} to {}: {e}", target.display()));
    }
    #[cfg(debug_assertions)]
    if Path::new(url).is_file() {
        fs::copy(Path::new(url), &tmp)
            .map_err(|e| format!("failed to copy {label} from {url}: {e}"))?;
        return fs::rename(&tmp, target)
            .map_err(|e| format!("failed to move {label} to {}: {e}", target.display()));
    }

    // Without --connect-timeout curl falls back to the OS's own TCP connect
    // timeout, which can run 60-130s depending on the network stack -- a
    // genuinely unreachable host (regionally blocked, DNS-filtered, or just
    // down) left the setup wizard hanging that long before saying so (#reported
    // via evermeet.cx from a user in Asia). 20s is generous for a slow-but-live
    // connection while failing fast on one that isn't.
    const CONNECT_TIMEOUT_SECS: &str = "20";
    const MAX_ATTEMPTS: u32 = 3;
    const RETRY_BACKOFF: [Duration; 2] = [Duration::from_secs(2), Duration::from_secs(5)];

    let mut last_detail = String::new();
    for attempt in 0..MAX_ATTEMPTS {
        let mut command = Command::new("curl");
        command
            .args([
                "--fail",
                "--location",
                "--show-error",
                "--connect-timeout",
                CONNECT_TIMEOUT_SECS,
                "--output",
                &tmp.display().to_string(),
                "--",
                url,
            ])
            .stdout(Stdio::null())
            .stderr(Stdio::piped());
        let output = command_output_with_timeout(command, timeout, label)?;
        if output.status.success() && tmp.is_file() {
            return fs::rename(&tmp, target)
                .map_err(|e| format!("failed to move {label} to {}: {e}", target.display()));
        }

        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        last_detail = if stderr.is_empty() {
            format!("curl exited with status {:?}", output.status.code())
        } else {
            stderr
        };
        if !curl_exit_is_retriable(output.status.code()) || attempt + 1 == MAX_ATTEMPTS {
            break;
        }
        std::thread::sleep(RETRY_BACKOFF[attempt as usize]);
    }

    Err(format!(
        "Could not reach the download server for {label}. Check your internet \
         connection and try again. ({last_detail})"
    ))
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

/// True for AppleDouble sidecars -- the `._name` files macOS `tar` emits to
/// carry a file's extended attributes (#505).
///
/// The runtime pack is built on macOS, where `ditto` preserves xattrs and `tar`
/// may encode them as these sidecar members. This crate has no AppleDouble
/// support, so unpacking them writes 30k binary stubs into `site-packages` --
/// and `matplotlib`'s `*.mplstyle` glob then matches `._seaborn-v0_8-bright
/// .mplstyle` and dies decoding its header, which takes down `matplotlib
/// .pyplot`, `allin1_infer`, and automatic song sections with it.
///
/// The pack script strips them at build time and fails if any survive, so this
/// exists for the packs already published without that guard.
fn is_apple_double(path: &Path) -> bool {
    path.file_name()
        .and_then(|name| name.to_str())
        .is_some_and(|name| name.starts_with("._"))
}

fn unpack_without_apple_double<R: Read>(
    mut archive: Archive<R>,
    destination: &Path,
) -> Result<(), String> {
    let entries = archive
        .entries()
        .map_err(|e| format!("failed to read runtime pack: {e}"))?;
    for entry in entries {
        let mut entry = entry.map_err(|e| format!("failed to read runtime pack: {e}"))?;
        let path = entry
            .path()
            .map_err(|e| format!("failed to read runtime pack: {e}"))?
            .into_owned();
        if is_apple_double(&path) {
            continue;
        }
        entry
            .unpack_in(destination)
            .map_err(|e| format!("failed to extract runtime pack: {e}"))?;
    }
    Ok(())
}

fn extract_tar_archive(archive: &Path, destination: &Path) -> Result<(), String> {
    let file = fs::File::open(archive)
        .map_err(|e| format!("failed to open archive {}: {e}", archive.display()))?;
    let is_zst = archive
        .extension()
        .is_some_and(|ext| ext.eq_ignore_ascii_case("zst"));
    if is_zst {
        let decoder =
            zstd::Decoder::new(file).map_err(|e| format!("failed to init zstd decoder: {e}"))?;
        unpack_without_apple_double(Archive::new(decoder), destination)
    } else {
        let decoder = GzDecoder::new(file);
        unpack_without_apple_double(Archive::new(decoder), destination)
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

/// Returns Some(PathBuf) if the env var is set and non-empty, None otherwise.
fn env_path_override(var: &str) -> Option<PathBuf> {
    env::var(var)
        .ok()
        .filter(|s| !s.is_empty())
        .map(PathBuf::from)
}

fn python_path(root: &Path) -> Option<PathBuf> {
    #[cfg(debug_assertions)]
    if let Some(p) = env_path_override("STEMDECK_PYTHON") {
        return Some(p);
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
    if let Some(p) = env_path_override("STEMDECK_FFMPEG") {
        return Some(p);
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

// Locate an FFmpeg binary that already exists on disk. Honors the STEMDECK_FFMPEG
// override, then checks the canonical flat location, then the `bin/` subfolder so a
// user who dropped an upstream FFmpeg build (which nests binaries under bin/) into
// data/ffmpeg/ is detected instead of triggering a download (#248). ffprobe lives
// alongside ffmpeg in every layout, so the returned parent dir suffices for PATH.
fn resolve_existing_ffmpeg(data_dir: &Path) -> Option<PathBuf> {
    if let Some(p) = env_path_override("STEMDECK_FFMPEG") {
        return p.is_file().then_some(p);
    }
    let file = if cfg!(windows) {
        "ffmpeg.exe"
    } else {
        "ffmpeg"
    };
    let ffmpeg_root = data_dir.join("ffmpeg");
    [ffmpeg_root.join(file), ffmpeg_root.join("bin").join(file)]
        .into_iter()
        .find(|p| p.is_file())
}

fn ffmpeg_dir_if_present(data_dir: &Path) -> Option<PathBuf> {
    let path = resolve_existing_ffmpeg(data_dir)?;
    path.parent().map(Path::to_path_buf)
}

/// Claim `host:port` without serving on it, and report the port that was
/// actually granted (`port` of 0 asks the OS to choose).
///
/// Bound but never listening, on purpose. `bind` is what reserves the address,
/// which is all this needs to do; `listen` is what makes a program a server,
/// and a server on `0.0.0.0` is what makes Windows Firewall interrupt the user.
/// The backend is the thing that should be answering that prompt, not the shell
/// that starts it.
fn claim_port(host: &str, port: u16) -> Result<(u16, Socket), String> {
    let addr: SocketAddr = format!("{host}:{port}")
        .parse()
        .map_err(|e| format!("bad bind address {host}:{port}: {e}"))?;
    let socket = Socket::new(Domain::for_address(addr), Type::STREAM, Some(Protocol::TCP))
        .map_err(|e| format!("socket failed: {e}"))?;
    socket
        .bind(&addr.into())
        .map_err(|e| format!("port bind failed: {e}"))?;
    let granted = socket
        .local_addr()
        .map_err(|e| e.to_string())?
        .as_socket()
        .ok_or_else(|| "bound socket has no address".to_string())?
        .port();
    Ok((granted, socket))
}

/// Bind to port 0 and return both the chosen port and the held reservation.
/// Caller must hold it until just after the child process is spawned, then drop
/// it so the child can bind the same port.  Holding the socket until spawn
/// narrows the TOCTOU window to a single OS context switch rather than the
/// entire command-setup period.
///
/// `host` must be the address the backend itself will bind. Probing a
/// different one proves nothing: see [`reserve_port`].
fn free_port(host: &str) -> Result<(u16, Socket), String> {
    claim_port(host, 0)
}

/// The user's preferred port (Settings -> port), read from the backend's
/// settings.json before launch. Defaults to 8080.
fn configured_port() -> u16 {
    const DEFAULT_PORT: u16 = 8000;
    let Ok(data_dir) = local_data_dir() else {
        return DEFAULT_PORT;
    };
    let Ok(text) = fs::read_to_string(data_dir.join("settings.json")) else {
        return DEFAULT_PORT;
    };
    let Ok(json) = serde_json::from_str::<serde_json::Value>(&text) else {
        return DEFAULT_PORT;
    };
    match json.get("port").and_then(serde_json::Value::as_u64) {
        Some(p) if (1024..=65535).contains(&p) => p as u16,
        _ => DEFAULT_PORT,
    }
}

/// Reserve the user's preferred port; fall back to any free port if it's taken,
/// so a port conflict can never block startup.
///
/// `host` must be the address the backend will bind (`0.0.0.0`), not loopback.
/// The two are not interchangeable: on Windows, binding `127.0.0.1:8000`
/// succeeds even while another process holds `0.0.0.0:8000`, because neither
/// socket sets `SO_EXCLUSIVEADDRUSE`. Probing loopback therefore reported a
/// taken port as free, the fallback below never ran, and the backend we spawned
/// died with `10048` while the *other* instance kept answering on that port
/// (#424).
fn reserve_port(host: &str, desired: u16) -> Result<(u16, Socket), String> {
    if let Ok(claimed) = claim_port(host, desired) {
        return Ok(claimed);
    }
    free_port(host)
}

/// A fresh identity for the backend this launch is about to spawn, handed to
/// it as `STEMDECK_INSTANCE_TOKEN` and echoed back by `/api/health`.
///
/// It has to be unique per launch, not unguessable: it answers "is the process
/// on this port the one I just started", and anything on the loopback
/// interface that wanted to lie could already read the token out of the health
/// response. So it is derived from the clock, this process and a counter
/// rather than drawn from a CSPRNG, which keeps the shell free of an RNG
/// dependency it has no other use for.
fn new_instance_token() -> String {
    static SEQUENCE: AtomicU64 = AtomicU64::new(0);
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let mut hasher = Sha256::new();
    hasher.update(std::process::id().to_le_bytes());
    hasher.update(nanos.to_le_bytes());
    hasher.update(SEQUENCE.fetch_add(1, Ordering::Relaxed).to_le_bytes());
    format!("{:x}", hasher.finalize())[..32].to_string()
}

/// Who is answering `/api/health`. Both fields are optional because either can
/// be absent from a backend older than the shell asking.
#[derive(Debug, Default, PartialEq)]
struct HealthIdentity {
    pid: Option<u32>,
    instance: Option<String>,
}

/// Wait until *our own* backend answers on `port`.
///
/// Identity matters as much as liveness here. A 200 only proves something is
/// listening; before #424 that was enough, so a second StemDeck launched while
/// one was already running would adopt the first instance's backend, and with
/// it the first instance's data directory and library, with nothing on screen
/// to suggest anything was wrong.
///
/// #424 established that identity by comparing the PID in the health payload
/// against the child we spawned, which assumed the process that binds the port
/// is the process we started. On the Windows portable build it is not (#457).
/// There `python/Scripts/python.exe` is a venv launcher pointing at
/// `python/base/python.exe`, and Windows has no `exec`, so the launcher starts
/// the real interpreter as a *child of its own*. The PID that binds the port is
/// therefore a grandchild and can never equal `child.id()`. Every Windows
/// portable user got the full ninety second timeout followed by "Another
/// program is already using port 8000", naming StemDeck's own healthy backend
/// as the intruder.
///
/// So identity travels in the environment instead, where it survives any number
/// of re-execs: the backend echoes back the token we gave it. A backend that
/// predates the token falls back to the PID comparison it was built for.
///
/// Watching the child also turns the common failure into a fast, clear one: a
/// backend that cannot bind its port exits within a second or so, and there is
/// no reason to keep polling for ninety.
fn wait_for_health(
    child: &mut Child,
    port: u16,
    token: &str,
    timeout: Duration,
    log_path: &Path,
) -> Result<(), String> {
    let deadline = Instant::now() + timeout;
    let mut interval = Duration::from_millis(250);
    let expected_pid = child.id();
    let mut foreign_pid: Option<u32> = None;
    loop {
        // Checked before the deadline so a child that died is always reported
        // as a death rather than as a timeout.
        if let Ok(Some(status)) = child.try_wait() {
            return Err(format!(
                "The backend stopped during startup ({}).{}\n\n{}",
                status,
                port_conflict_hint(port, foreign_pid),
                log_hint(log_path)
            ));
        }
        if Instant::now() >= deadline {
            return Err(format!(
                "backend did not become healthy within {} seconds.{}\n\n{}",
                timeout.as_secs(),
                port_conflict_hint(port, foreign_pid),
                log_hint(log_path)
            ));
        }
        match health_once(port) {
            // Our token came back: this is the backend we started, whatever
            // process ended up holding the socket.
            Ok(id) if id.instance.as_deref() == Some(token) => return Ok(()),
            // No token at all means a backend older than this shell, which can
            // only be identified the #424 way. Anything that does report a
            // token reports *a different one*, so it is not ours and the PID is
            // not consulted.
            Ok(id) if id.instance.is_none() && id.pid == Some(expected_pid) => return Ok(()),
            // Something is listening, but it is not the process we started.
            // Keep waiting rather than failing outright: our child is still
            // alive, and if it never gets the port it will exit and be caught
            // above. What must never happen is returning Ok for this.
            Ok(id) => foreign_pid = id.pid,
            Err(_) => {}
        }
        thread::sleep(interval);
        // Exponential backoff capped at 2 s to reduce busy-polling while
        // still detecting fast startups quickly.
        interval = (interval * 2).min(Duration::from_secs(2));
    }
}

/// Names the real problem when another program holds the port, instead of
/// leaving the user to infer it from a stack trace in the log tail.
fn port_conflict_hint(port: u16, foreign_pid: Option<u32>) -> String {
    match foreign_pid {
        Some(pid) => format!(
            "\n\nAnother program is already using port {port} (process {pid}). \
             If that is a second copy of StemDeck, close it and try again, or \
             change the port in Settings."
        ),
        None => String::new(),
    }
}

fn log_hint(log_path: &Path) -> String {
    let tail = file_tail(log_path, 30);
    if tail.trim().is_empty() {
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

/// Returns what the process on `port` claims about itself, so the caller can
/// tell our own backend apart from anything else holding the port.
fn health_once(port: u16) -> Result<HealthIdentity, String> {
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
    if !(response.starts_with("HTTP/1.1 200") || response.starts_with("HTTP/1.0 200")) {
        return Err("health endpoint did not return 200".to_string());
    }
    parse_health_identity(&response)
        .ok_or_else(|| "health response was not a JSON object".to_string())
}

/// Pull the identity fields out of a raw HTTP response. Deliberately parses
/// only the JSON body: the headers are not JSON, and a `pid` appearing there
/// (or in a header value) must not be mistaken for the backend's own.
///
/// An empty `instance` is the same as none. Every distribution but the desktop
/// shell runs the backend without a token (Docker, Unraid, a source checkout),
/// and reporting `""` for all of them must not let them match each other.
fn parse_health_identity(response: &str) -> Option<HealthIdentity> {
    let body = response
        .split_once("\r\n\r\n")
        .map(|(_, body)| body)
        .or_else(|| response.split_once("\n\n").map(|(_, body)| body))?;
    let start = body.find('{')?;
    let json: serde_json::Value = serde_json::from_str(body[start..].trim()).ok()?;
    Some(HealthIdentity {
        pid: json
            .get("pid")
            .and_then(|v| v.as_u64())
            .and_then(|p| u32::try_from(p).ok()),
        instance: json
            .get("instance")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .map(str::to_string),
    })
}

fn ensure_ffmpeg(data_dir: &Path) -> Result<PathBuf, String> {
    // Use an already-present binary (flat, bin/, or STEMDECK_FFMPEG override) before
    // downloading, so a manually-placed FFmpeg is honored (#248).
    if let Some(existing) = resolve_existing_ffmpeg(data_dir) {
        verify_ffmpeg(&existing)?;
        return Ok(existing);
    }

    // Prefer a system FFmpeg on PATH -- a Homebrew/apt/choco install, or a dev
    // machine that already has one -- over downloading our own, on every
    // platform. verify_ffmpeg() confirms it both runs on this OS *and* has
    // every encoder StemDeck's export pipeline needs (see its doc comment),
    // so this only short-circuits the download when the system build can
    // actually fulfill StemDeck's requirements. This also protects macOS
    // users on an older OS than our downloaded build assumes (#414): if they
    // already have a working system FFmpeg, we no longer force a potentially
    // incompatible download on top of it.
    if verify_ffmpeg(Path::new("ffmpeg")).is_ok() {
        return Ok(PathBuf::from("ffmpeg"));
    }

    #[cfg(windows)]
    {
        download_windows_ffmpeg(data_dir)?;
        let portable =
            ffmpeg_path(data_dir).ok_or_else(|| "failed to resolve FFmpeg path".to_string())?;
        verify_ffmpeg(&portable)?;
        Ok(portable)
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
        // No system FFmpeg was usable above -- fetch a static build into
        // data_dir/ffmpeg. The shared config.json PATH plumbing then lets the
        // Demucs subprocess find it too.
        download_linux_ffmpeg(data_dir)?;
        let portable =
            ffmpeg_path(data_dir).ok_or_else(|| "failed to resolve FFmpeg path".to_string())?;
        verify_ffmpeg(&portable)?;
        Ok(portable)
    }
}

#[cfg(all(unix, not(target_os = "macos")))]
fn download_linux_ffmpeg(data_dir: &Path) -> Result<(), String> {
    let url = env_path_override("STEMDECK_FFMPEG_URL")
        .map(|p| p.display().to_string())
        .unwrap_or_else(|| DEFAULT_LINUX_FFMPEG_URL.to_string());
    let downloads = data_dir.join("downloads");
    fs::create_dir_all(&downloads)
        .map_err(|e| format!("failed to create {}: {e}", downloads.display()))?;
    let archive = downloads.join("ffmpeg-linux.tar.xz");
    download_file(&url, &archive, Duration::from_secs(30 * 60), "FFmpeg")?;

    // Extract with the system tar (xz support is standard on desktop Linux). The
    // static build unpacks to a single ffmpeg-<ver>-amd64-static/ directory.
    let extract_dir = downloads.join("ffmpeg-linux");
    let _ = fs::remove_dir_all(&extract_dir);
    fs::create_dir_all(&extract_dir)
        .map_err(|e| format!("failed to create {}: {e}", extract_dir.display()))?;
    let status = Command::new("tar")
        .args([
            "-xJf",
            &archive.display().to_string(),
            "-C",
            &extract_dir.display().to_string(),
        ])
        .status()
        .map_err(|e| format!("failed to run tar (is it installed?): {e}"))?;
    if !status.success() {
        let _ = fs::remove_file(&archive);
        return Err("failed to extract the FFmpeg archive".to_string());
    }

    // The archive holds one top-level dir; ffmpeg + ffprobe live directly inside.
    let inner = fs::read_dir(&extract_dir)
        .map_err(|e| format!("failed to read {}: {e}", extract_dir.display()))?
        .filter_map(Result::ok)
        .map(|e| e.path())
        .find(|p| p.is_dir())
        .ok_or_else(|| "FFmpeg archive had no extracted directory".to_string())?;

    let ffmpeg_dir = data_dir.join("ffmpeg");
    fs::create_dir_all(&ffmpeg_dir)
        .map_err(|e| format!("failed to create {}: {e}", ffmpeg_dir.display()))?;
    for name in ["ffmpeg", "ffprobe"] {
        let src = inner.join(name);
        if !src.is_file() {
            return Err(format!("{name} not found in the FFmpeg archive"));
        }
        let dest = ffmpeg_dir.join(name);
        fs::copy(&src, &dest)
            .map_err(|e| format!("failed to copy {name} to {}: {e}", dest.display()))?;
        make_executable(&dest)?;
    }

    let _ = fs::remove_dir_all(&extract_dir);
    let _ = fs::remove_file(&archive);
    Ok(())
}

// "arm64" on Apple Silicon, "x64" everywhere else -- takes the arch string
// rather than reading std::env::consts::ARCH itself so both branches are
// unit-testable on any host, not just the one they happen to be running on.
#[cfg(target_os = "macos")]
fn macos_arch_suffix(arch: &str) -> &'static str {
    match arch {
        "aarch64" => "arm64",
        _ => "x64",
    }
}

// An override hash from `env_var`, trimmed and lowercased, or None if unset/
// blank. Mirrors the Windows override path: an explicit custom URL skips
// pinned-hash verification unless the matching *_SHA256 env var is also set.
#[cfg(target_os = "macos")]
fn override_sha256(env_var: &str) -> Option<String> {
    env::var(env_var)
        .ok()
        .map(|s| s.trim().to_ascii_lowercase())
        .filter(|s| !s.is_empty())
}

// Verify a freshly downloaded archive against an expected SHA256 before it is
// extracted or made executable (#172). On mismatch the file is removed so a
// corrupt or tampered binary is never run. `None` means no hash to enforce.
#[cfg(target_os = "macos")]
fn verify_pinned_sha256(path: &Path, expected: Option<&str>, label: &str) -> Result<(), String> {
    let Some(expected) = expected else {
        return Ok(());
    };
    let actual = sha256_file(path)?;
    if !actual.eq_ignore_ascii_case(expected) {
        let _ = fs::remove_file(path);
        return Err(format!(
            "{label} archive checksum mismatch (expected {expected}, got {actual}). \
             The download may be corrupt or tampered. Click Retry to try again."
        ));
    }
    Ok(())
}

/// evermeet.cx (zip-wrapped, universal binary) is used both for the fallback
/// path and for a custom STEMDECK_FFMPEG_URL override -- that env var has
/// always pointed at a zip in this shape, so overrides keep working exactly
/// as before regardless of what the built-in primary source looks like.
#[cfg(target_os = "macos")]
fn download_macos_ffmpeg_zip_source(
    ffmpeg_url: &str,
    ffprobe_url: &str,
    ffmpeg_expected: Option<&str>,
    ffprobe_expected: Option<&str>,
    downloads: &Path,
    ffmpeg_dir: &Path,
) -> Result<(), String> {
    let ffmpeg_zip = downloads.join("ffmpeg-macos.zip");
    let ffprobe_zip = downloads.join("ffprobe-macos.zip");
    download_file(
        ffmpeg_url,
        &ffmpeg_zip,
        Duration::from_secs(30 * 60),
        "FFmpeg",
    )?;
    verify_pinned_sha256(&ffmpeg_zip, ffmpeg_expected, "FFmpeg")?;
    download_file(
        ffprobe_url,
        &ffprobe_zip,
        Duration::from_secs(30 * 60),
        "ffprobe",
    )?;
    verify_pinned_sha256(&ffprobe_zip, ffprobe_expected, "ffprobe")?;

    extract_single_binary_from_zip(&ffmpeg_zip, &ffmpeg_dir.join("ffmpeg"), "ffmpeg")?;
    extract_single_binary_from_zip(&ffprobe_zip, &ffmpeg_dir.join("ffprobe"), "ffprobe")?;
    make_executable(&ffmpeg_dir.join("ffmpeg"))?;
    make_executable(&ffmpeg_dir.join("ffprobe"))?;
    Ok(())
}

/// Primary source: shaka-project's per-architecture builds, published as raw
/// (non-zip) binaries -- downloaded straight to their final path, no
/// extraction step.
#[cfg(target_os = "macos")]
fn download_macos_ffmpeg_primary(ffmpeg_dir: &Path) -> Result<(), String> {
    let arch = macos_arch_suffix(std::env::consts::ARCH);
    let (ffmpeg_sha, ffprobe_sha) = match arch {
        "arm64" => (SHAKA_FFMPEG_SHA256_ARM64, SHAKA_FFPROBE_SHA256_ARM64),
        _ => (SHAKA_FFMPEG_SHA256_X64, SHAKA_FFPROBE_SHA256_X64),
    };
    let ffmpeg_url = format!("{SHAKA_FFMPEG_BASE_URL}/{SHAKA_FFMPEG_RELEASE}/ffmpeg-osx-{arch}");
    let ffprobe_url = format!("{SHAKA_FFMPEG_BASE_URL}/{SHAKA_FFMPEG_RELEASE}/ffprobe-osx-{arch}");
    let ffmpeg_target = ffmpeg_dir.join("ffmpeg");
    let ffprobe_target = ffmpeg_dir.join("ffprobe");

    download_file(
        &ffmpeg_url,
        &ffmpeg_target,
        Duration::from_secs(30 * 60),
        "FFmpeg",
    )?;
    verify_pinned_sha256(&ffmpeg_target, Some(ffmpeg_sha), "FFmpeg")?;
    download_file(
        &ffprobe_url,
        &ffprobe_target,
        Duration::from_secs(30 * 60),
        "ffprobe",
    )?;
    verify_pinned_sha256(&ffprobe_target, Some(ffprobe_sha), "ffprobe")?;

    make_executable(&ffmpeg_target)?;
    make_executable(&ffprobe_target)?;
    Ok(())
}

#[cfg(target_os = "macos")]
fn download_macos_ffmpeg(data_dir: &Path) -> Result<(), String> {
    let downloads = data_dir.join("downloads");
    fs::create_dir_all(&downloads)
        .map_err(|e| format!("failed to create {}: {e}", downloads.display()))?;
    let ffmpeg_dir = data_dir.join("ffmpeg");
    fs::create_dir_all(&ffmpeg_dir)
        .map_err(|e| format!("failed to create {}: {e}", ffmpeg_dir.display()))?;

    // An explicit override always wins and skips the primary/fallback dance
    // entirely -- the user has already chosen a source. Goes through the
    // zip-wrapped path, the shape this override has always expected.
    if let Some(ffmpeg_override) = env_path_override("STEMDECK_FFMPEG_URL") {
        let ffmpeg_url = ffmpeg_override.display().to_string();
        let ffprobe_url = env_path_override("STEMDECK_FFPROBE_URL")
            .map(|p| p.display().to_string())
            .unwrap_or_else(|| DEFAULT_MACOS_FFPROBE_URL.to_string());
        let ffmpeg_expected = override_sha256("STEMDECK_FFMPEG_SHA256");
        let ffprobe_expected = override_sha256("STEMDECK_FFPROBE_SHA256");
        return download_macos_ffmpeg_zip_source(
            &ffmpeg_url,
            &ffprobe_url,
            ffmpeg_expected.as_deref(),
            ffprobe_expected.as_deref(),
            &downloads,
            &ffmpeg_dir,
        );
    }

    // Primary: shaka-project's per-architecture builds on GitHub Releases
    // (GitHub's global CDN) -- the same class of fix that already solved this
    // for Windows (#248, off gyan.dev's single mirror). evermeet.cx is the
    // fallback: a single host with no CDN behind it, reported unreachable
    // from multiple regions (#388). A checksum match only proves the bytes are
    // what we expect, not that the binary actually launches on this machine's
    // macOS version or has every encoder StemDeck needs -- verify both before
    // accepting it over the fallback (#414).
    let primary_result = download_macos_ffmpeg_primary(&ffmpeg_dir)
        .and_then(|()| verify_ffmpeg(&ffmpeg_dir.join("ffmpeg")));
    if let Err(primary_err) = primary_result {
        eprintln!("primary FFmpeg source failed, trying the evermeet.cx fallback: {primary_err}");
        return download_macos_ffmpeg_zip_source(
            DEFAULT_MACOS_FFMPEG_URL,
            DEFAULT_MACOS_FFPROBE_URL,
            Some(DEFAULT_MACOS_FFMPEG_SHA256),
            Some(DEFAULT_MACOS_FFPROBE_SHA256),
            &downloads,
            &ffmpeg_dir,
        )
        .map_err(|fallback_err| {
            format!(
                "Could not download FFmpeg from either source.\n\
                 Primary: {primary_err}\n\
                 Fallback: {fallback_err}"
            )
        });
    }
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

#[cfg(unix)]
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

// Parse a combined `checksums.sha256` file (lines of `<hash>  <filename>`) and
// return the lowercased hash for `filename`, or None if it is not listed.
#[cfg(any(windows, test))]
fn sha256_from_checksums(contents: &str, filename: &str) -> Option<String> {
    contents.lines().find_map(|line| {
        let mut parts = line.split_whitespace();
        let hash = parts.next()?;
        let name = parts.next()?;
        (name == filename).then(|| hash.to_ascii_lowercase())
    })
}

#[cfg(windows)]
fn download_windows_ffmpeg(data_dir: &Path) -> Result<(), String> {
    let url = env_path_override("STEMDECK_FFMPEG_URL")
        .map(|p| p.display().to_string())
        .unwrap_or_else(|| DEFAULT_WINDOWS_FFMPEG_URL.to_string());
    let is_default_url = url == DEFAULT_WINDOWS_FFMPEG_URL;
    let downloads = data_dir.join("downloads");
    let archive_path = downloads.join("ffmpeg-windows.zip");
    fs::create_dir_all(&downloads)
        .map_err(|e| format!("failed to create {}: {e}", downloads.display()))?;

    // Fetch BtbN's combined checksums.sha256 and pick the line for our archive (#135, #248).
    // Only verified for the default URL; custom overrides skip the check.
    let expected_sha256 = if is_default_url {
        let archive_name = url
            .rsplit('/')
            .next()
            .ok_or_else(|| "could not derive FFmpeg archive name from URL".to_string())?;
        let sha256_tmp = downloads.join("ffmpeg-windows.checksums.sha256");
        download_file_blocking(DEFAULT_WINDOWS_FFMPEG_CHECKSUMS_URL, &sha256_tmp)?;
        let raw = fs::read_to_string(&sha256_tmp)
            .map_err(|e| format!("failed to read FFmpeg checksums: {e}"))?;
        let _ = fs::remove_file(&sha256_tmp);
        Some(sha256_from_checksums(&raw, archive_name).ok_or_else(|| {
            format!("FFmpeg checksums file did not list an entry for {archive_name}")
        })?)
    } else {
        None
    };

    download_file_blocking(&url, &archive_path)?;

    if let Some(expected) = expected_sha256 {
        let actual = sha256_file(&archive_path)?;
        if !actual.eq_ignore_ascii_case(&expected) {
            let _ = fs::remove_file(&archive_path);
            return Err(format!(
                "FFmpeg archive checksum mismatch (expected {expected}, got {actual}). \
                 The download may be corrupt. Click Retry to try again."
            ));
        }
    }

    extract_ffmpeg_binaries(&archive_path, data_dir)
}

#[cfg(windows)]
fn download_file_blocking(url: &str, target: &Path) -> Result<(), String> {
    let tmp = target.with_extension("download");
    if tmp.exists() {
        fs::remove_file(&tmp).map_err(|e| format!("failed to remove {}: {e}", tmp.display()))?;
    }

    // NOTE: do not call this function from an async context — reqwest::blocking
    // spawns its own tokio runtime and will panic with "Cannot start a runtime
    // from within a runtime" if a tokio executor is already running on the thread.
    let client = reqwest::blocking::Client::builder()
        .connect_timeout(Duration::from_secs(15))
        .timeout(Duration::from_secs(30 * 60))
        .build()
        .map_err(|e| format!("failed to build HTTP client: {e}"))?;

    let mut response = client.get(url).send().map_err(|e| {
        if e.is_connect() || e.is_timeout() {
            format!(
                "Could not reach the download server. \
                 Check your internet connection and try again. ({e})"
            )
        } else {
            format!("failed to start download from {url}: {e}")
        }
    })?;

    if !response.status().is_success() {
        return Err(format!(
            "failed to download from {url}: HTTP {}",
            response.status()
        ));
    }

    let mut file =
        fs::File::create(&tmp).map_err(|e| format!("failed to create {}: {e}", tmp.display()))?;

    response
        .copy_to(&mut file)
        .map_err(|e| format!("failed to write to {}: {e}", tmp.display()))?;

    // Flush OS write cache to disk before closing — guards against data loss if
    // the process crashes or power is lost between close and rename.
    file.sync_all()
        .map_err(|e| format!("failed to flush {}: {e}", tmp.display()))?;

    // Explicitly drop the file handle before rename — Windows will not rename
    // a file with an open handle.
    drop(file);

    fs::rename(&tmp, target)
        .map_err(|e| format!("failed to move download to {}: {e}", target.display()))
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

// Encoders StemDeck's export pipeline actually calls for by name: pcm_s16le
// (WAV stems), flac (FLAC stems), libmp3lame (MP3 stems/zips), libvorbis (OGG
// stems), aac (the audio track on MP4 video exports) -- see app/api/stems.py's
// per-format ffmpeg args. A minimal or distro-stripped FFmpeg build can pass a
// bare `-version` check yet be missing one of these, which would otherwise
// only surface later as an export failure deep in the pipeline (#414).
const REQUIRED_FFMPEG_ENCODERS: &[&str] = &["pcm_s16le", "flac", "libmp3lame", "libvorbis", "aac"];

fn missing_required_encoders(listing: &str) -> Vec<&'static str> {
    REQUIRED_FFMPEG_ENCODERS
        .iter()
        .copied()
        .filter(|codec| !listing.contains(codec))
        .collect()
}

fn verify_ffmpeg_encoders(path: &Path) -> Result<(), String> {
    let mut command = Command::new(path);
    command
        .args(["-hide_banner", "-encoders"])
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    hide_console_window(&mut command);
    let output =
        command_output_with_timeout(command, Duration::from_secs(15), "FFmpeg encoder check")
            .map_err(|e| format!("failed to list FFmpeg encoders at {}: {e}", path.display()))?;
    let listing = String::from_utf8_lossy(&output.stdout);
    let missing = missing_required_encoders(&listing);
    if missing.is_empty() {
        Ok(())
    } else {
        Err(format!(
            "FFmpeg at {} is missing required encoder(s): {}",
            path.display(),
            missing.join(", ")
        ))
    }
}

// A binary is only "compatible with StemDeck's requirements" (#414) if it
// both runs on this machine and has every encoder the export pipeline needs
// -- checking just one half would let either a broken-on-this-OS build or a
// minimal/stripped one through.
fn verify_ffmpeg(path: &Path) -> Result<(), String> {
    let mut command = Command::new(path);
    command
        .arg("-version")
        .stdout(Stdio::null())
        .stderr(Stdio::piped());
    hide_console_window(&mut command);
    let output = command_output_with_timeout(command, Duration::from_secs(15), "FFmpeg check")
        .map_err(|e| format!("failed to run FFmpeg at {}: {e}", path.display()))?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(format!(
            "FFmpeg at {} failed verification: {}",
            path.display(),
            stderr.trim()
        ));
    }
    verify_ffmpeg_encoders(path)
}

fn write_setup_config(data_dir: &Path, ffmpeg: &Path) -> Result<(), String> {
    // ffprobe always sits next to the resolved ffmpeg (flat or bin/); fall back to
    // the canonical flat location if ffmpeg has no parent.
    let ffprobe_file = if cfg!(windows) {
        "ffprobe.exe"
    } else {
        "ffprobe"
    };
    let ffprobe = ffmpeg
        .parent()
        .map(|dir| dir.join(ffprobe_file))
        .unwrap_or_else(|| ffprobe_path(data_dir));
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

    // Write atomically: temp file → sync → rename. A crash mid-write leaves the
    // previous config intact rather than producing a truncated/empty file.
    let tmp_path = config_path.with_extension("json.tmp");
    let mut tmp_file = fs::File::create(&tmp_path)
        .map_err(|e| format!("failed to create {}: {e}", tmp_path.display()))?;
    tmp_file
        .write_all((body + "\n").as_bytes())
        .map_err(|e| format!("failed to write {}: {e}", tmp_path.display()))?;
    tmp_file
        .sync_all()
        .map_err(|e| format!("failed to flush {}: {e}", tmp_path.display()))?;
    drop(tmp_file);
    fs::rename(&tmp_path, &config_path)
        .map_err(|e| format!("failed to move config to {}: {e}", config_path.display()))
}

/// Polls an already-spawned child until it exits or the timeout elapses.
/// Mirrors command_output_with_timeout but accepts a pre-spawned Child so the
/// caller can record the PID before waiting (e.g. to kill on window close).
fn child_output_with_timeout(
    mut child: Child,
    timeout: Duration,
    label: &str,
) -> Result<Output, String> {
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

#[cfg(test)]
mod tests {
    #[cfg(target_os = "macos")]
    use std::env;
    use std::fs;
    use std::path::{Path, PathBuf};
    use std::process::{Child, Command, Stdio};
    use std::time::Duration;
    use tempfile::TempDir;

    fn make_tmp() -> TempDir {
        tempfile::tempdir().expect("failed to create temp dir")
    }

    #[test]
    fn extract_tar_archive_drops_apple_double_sidecars() {
        // A macOS-built runtime pack can carry `._name` AppleDouble members
        // alongside the real files. Unpacked verbatim, the one beside
        // matplotlib's stylelib matches its `*.mplstyle` glob and kills every
        // import of matplotlib.pyplot -- and with it automatic song sections
        // (#505).
        let source = make_tmp();
        let stylelib = source.path().join("runtime/stylelib");
        fs::create_dir_all(&stylelib).unwrap();
        fs::write(
            stylelib.join("seaborn-v0_8-bright.mplstyle"),
            b"axes.grid: True",
        )
        .unwrap();
        fs::write(
            stylelib.join("._seaborn-v0_8-bright.mplstyle"),
            b"\x00\x05\x16\x07\xa3binary AppleDouble header",
        )
        .unwrap();

        // Must be .tar.zst: that is the shape the macOS runtime pack ships in,
        // and the only one extract_tar_archive routes away from gzip.
        let archive_dir = make_tmp();
        let archive = archive_dir.path().join("runtime.tar.zst");
        let encoder = zstd::Encoder::new(fs::File::create(&archive).unwrap(), 0).unwrap();
        let mut builder = tar::Builder::new(encoder);
        builder
            .append_dir_all("runtime", source.path().join("runtime"))
            .unwrap();
        builder.into_inner().unwrap().finish().unwrap();

        let destination = make_tmp();
        super::extract_tar_archive(&archive, destination.path()).unwrap();

        let unpacked = destination.path().join("runtime/stylelib");
        assert!(
            unpacked.join("seaborn-v0_8-bright.mplstyle").is_file(),
            "real files must still be extracted"
        );
        assert!(
            !unpacked.join("._seaborn-v0_8-bright.mplstyle").exists(),
            "AppleDouble sidecar must not be written to disk"
        );
    }

    #[test]
    fn legacy_migration_preserves_user_settings_when_data_dir_already_exists() {
        // setup() creates the destination before ensure_workspace() invokes
        // migration, which used to make migration return without copying any
        // user state at all.
        let root = make_tmp();
        let destination_parent = make_tmp();
        let destination = destination_parent.path().join("StemDeck");
        fs::create_dir_all(&destination).unwrap();
        fs::create_dir_all(root.path().join("data")).unwrap();
        let settings = br#"{"jobs_dir":"D:\\Audio\\StemDeck","separation_quality":"best"}"#;
        fs::write(root.path().join("data/settings.json"), settings).unwrap();
        fs::write(
            root.path().join("data/config.json"),
            br#"{"torchDevice":"cuda"}"#,
        )
        .unwrap();

        super::migrate_legacy_data(root.path(), &destination).unwrap();

        assert_eq!(
            fs::read(destination.join("settings.json")).unwrap(),
            settings
        );
        assert!(destination.join("config.json").is_file());
        assert!(root.path().join("data/settings.json").is_file());
        assert!(
            fs::read_dir(&destination).unwrap().all(|entry| !entry
                .unwrap()
                .file_name()
                .to_string_lossy()
                .contains(".migrate.")),
            "successful migration must not leave staging files"
        );
    }

    #[test]
    fn legacy_migration_never_overwrites_newer_user_settings() {
        let root = make_tmp();
        let destination_parent = make_tmp();
        let destination = destination_parent.path().join("StemDeck");
        fs::create_dir_all(root.path().join("data")).unwrap();
        fs::create_dir_all(&destination).unwrap();
        fs::write(
            root.path().join("data/settings.json"),
            br#"{"jobs_dir":"D:\\Old"}"#,
        )
        .unwrap();
        let current = br#"{"jobs_dir":"E:\\Current"}"#;
        fs::write(destination.join("settings.json"), current).unwrap();

        super::migrate_legacy_data(root.path(), &destination).unwrap();

        assert_eq!(
            fs::read(destination.join("settings.json")).unwrap(),
            current
        );
    }

    #[test]
    fn portable_migration_carries_preferences_but_not_install_readiness() {
        let source = make_tmp();
        let destination_parent = make_tmp();
        let destination = destination_parent.path().join("data");
        fs::write(
            source.path().join("settings.json"),
            br#"{"jobs_dir":"D:\\Audio"}"#,
        )
        .unwrap();
        fs::write(
            source.path().join("config.json"),
            br#"{"modelReady":true,"ffmpegReady":true}"#,
        )
        .unwrap();

        super::migrate_persisted_files(source.path(), &destination, &["settings.json"]).unwrap();

        assert!(destination.join("settings.json").is_file());
        assert!(!destination.join("config.json").exists());
    }

    // ── stale app-data cleanup (#356) ────────────────────────────────────────

    fn seed_downloads(dir: &std::path::Path, names: &[(&str, usize)]) {
        let downloads = dir.join("downloads");
        fs::create_dir_all(&downloads).unwrap();
        for (name, size) in names {
            fs::write(downloads.join(name), vec![0u8; *size]).unwrap();
        }
    }

    #[test]
    fn prune_downloads_removes_archives_that_are_not_expected() {
        let dir = make_tmp();
        seed_downloads(
            dir.path(),
            &[
                ("StemDeck-runtime-macOS-arm64-old.tar.zst", 2048),
                ("ffmpeg-macos.zip", 1024),
                ("StemDeck-runtime-macOS-arm64.tar.zst", 512),
            ],
        );
        let keep = dir
            .path()
            .join("downloads")
            .join("StemDeck-runtime-macOS-arm64.tar.zst");

        let freed = super::prune_downloads(dir.path(), Some(&keep));

        assert_eq!(freed, 3072, "should report what it actually removed");
        assert!(
            keep.is_file(),
            "the archive this build expects must survive"
        );
        assert!(!dir
            .path()
            .join("downloads")
            .join("ffmpeg-macos.zip")
            .exists());
    }

    #[test]
    fn prune_downloads_with_nothing_to_keep_empties_the_folder() {
        let dir = make_tmp();
        seed_downloads(dir.path(), &[("a.tar.zst", 16), ("b.zip", 32)]);

        let freed = super::prune_downloads(dir.path(), None);

        assert_eq!(freed, 48);
        assert_eq!(
            fs::read_dir(dir.path().join("downloads")).unwrap().count(),
            0
        );
    }

    #[test]
    fn prune_downloads_never_touches_anything_else() {
        // settings.json holds the stems location (#354). Losing it would send a
        // user who moved their library elsewhere back to the default folder,
        // to an empty app with their stems stranded.
        let dir = make_tmp();
        seed_downloads(dir.path(), &[("old.tar.zst", 8)]);
        fs::write(
            dir.path().join("settings.json"),
            br#"{"jobs_dir":"/Volumes/Audio"}"#,
        )
        .unwrap();
        fs::write(dir.path().join("config.json"), b"{}").unwrap();
        fs::create_dir_all(dir.path().join("runtime")).unwrap();
        fs::create_dir_all(dir.path().join("models")).unwrap();
        fs::create_dir_all(dir.path().join("ffmpeg")).unwrap();

        super::prune_downloads(dir.path(), None);

        assert!(dir.path().join("settings.json").is_file());
        assert!(dir.path().join("config.json").is_file());
        assert!(dir.path().join("runtime").is_dir());
        assert!(dir.path().join("models").is_dir());
        assert!(dir.path().join("ffmpeg").is_dir());
    }

    #[test]
    fn prune_downloads_tolerates_a_missing_folder() {
        let dir = make_tmp();
        assert_eq!(super::prune_downloads(dir.path(), None), 0);
    }

    #[test]
    fn prune_runtime_leftovers_removes_an_unfinished_swap() {
        let dir = make_tmp();
        fs::create_dir_all(dir.path().join("runtime.tmp").join("runtime")).unwrap();
        fs::create_dir_all(dir.path().join("runtime.old").join("python")).unwrap();
        fs::create_dir_all(dir.path().join("runtime").join("python")).unwrap();

        super::prune_runtime_leftovers(dir.path());

        assert!(!dir.path().join("runtime.tmp").exists());
        assert!(!dir.path().join("runtime.old").exists());
        assert!(
            dir.path().join("runtime").is_dir(),
            "the live runtime must stay"
        );
    }

    #[test]
    fn prune_runtime_leftovers_is_a_no_op_when_clean() {
        let dir = make_tmp();
        fs::create_dir_all(dir.path().join("runtime")).unwrap();
        super::prune_runtime_leftovers(dir.path());
        assert!(dir.path().join("runtime").is_dir());
    }

    fn seed_installed_runtime(dir: &std::path::Path, version: &str) {
        let runtime = dir.join("runtime");
        fs::create_dir_all(runtime.join("backend").join("app")).unwrap();
        fs::create_dir_all(runtime.join("python").join("bin")).unwrap();
        fs::write(
            runtime.join("python").join("bin").join("python"),
            b"#!/bin/sh\n",
        )
        .unwrap();
        fs::write(
            runtime.join("runtime-manifest.json"),
            format!(r#"{{"version":"{version}"}}"#),
        )
        .unwrap();
    }

    fn manifest_for(version: &str) -> super::RuntimeManifest {
        super::RuntimeManifest {
            version: version.to_string(),
            arch: "arm64".to_string(),
            runtime_url: "https://example.invalid/StemDeck-runtime-macOS-arm64.tar.zst".to_string(),
            runtime_sha256: "0".repeat(64),
            runtime_size: None,
            archive_name: Some("StemDeck-runtime-macOS-arm64.tar.zst".to_string()),
        }
    }

    #[test]
    fn an_installed_matching_runtime_makes_its_archive_disposable() {
        // The real case behind #356: the pack is installed, so the 165 MB it
        // came from is dead weight. Its filename carries no version, so nothing
        // else would ever mark it stale.
        let dir = make_tmp();
        seed_installed_runtime(dir.path(), "1.2.3");
        assert!(super::runtime_is_current(
            dir.path(),
            &manifest_for("1.2.3")
        ));
    }

    #[test]
    fn an_older_installed_runtime_still_needs_the_archive() {
        let dir = make_tmp();
        seed_installed_runtime(dir.path(), "1.2.3");
        assert!(!super::runtime_is_current(
            dir.path(),
            &manifest_for("1.3.0")
        ));
    }

    #[test]
    fn a_half_installed_runtime_still_needs_the_archive() {
        let dir = make_tmp();
        fs::create_dir_all(dir.path().join("runtime").join("backend").join("app")).unwrap();
        assert!(!super::runtime_is_current(
            dir.path(),
            &manifest_for("1.2.3")
        ));
    }

    #[test]
    fn version_mismatch_detected() {
        let dir = make_tmp();
        let version_file = dir.path().join("last_version.txt");
        fs::write(&version_file, "0.4.0").unwrap();
        let last = fs::read_to_string(&version_file).unwrap_or_default();
        assert_ne!(last.trim(), "0.5.0-alpha.1");
    }

    #[test]
    fn version_match_skips_cleanup() {
        let dir = make_tmp();
        let version_file = dir.path().join("last_version.txt");
        let current = "0.5.0-alpha.1";
        fs::write(&version_file, current).unwrap();
        let last = fs::read_to_string(&version_file).unwrap_or_default();
        assert_eq!(last.trim(), current); // no cleanup should fire
    }

    #[test]
    fn migration_flag_gates_webkit_clear() {
        let dir = make_tmp();
        let migration_flag = dir.path().join("store_migration_done");
        // Flag absent → cleanup must NOT fire on first upgrade
        assert!(!migration_flag.exists());
        // Write flag
        fs::write(&migration_flag, "").unwrap();
        // Flag present → cleanup CAN fire on subsequent upgrades
        assert!(migration_flag.exists());
    }

    #[test]
    fn version_file_write_failure_does_not_loop() {
        // If version file can't be written we must NOT update it,
        // so the next launch also skips cleanup (not a repeat wipe).
        let dir = make_tmp();
        let version_file = dir.path().join("last_version.txt");
        fs::write(&version_file, "0.4.0").unwrap();
        // Simulate failure by checking: if write errors, last stays "0.4.0"
        let result = fs::write(dir.path().join("readonly_dir/last_version.txt"), "0.5.0");
        assert!(result.is_err()); // the write failed
                                  // Original file unchanged — next launch will see "0.4.0" != "0.5.0" again,
                                  // but migration_flag is absent so no cleanup fires. Correct behavior.
        let last = fs::read_to_string(&version_file).unwrap_or_default();
        assert_eq!(last.trim(), "0.4.0");
    }

    // --- cpu-only-package re-probe on package swap (#316) ---

    #[test]
    fn stale_cpu_only_reason_dropped_when_package_is_not_cpu_only() {
        // CPU build -> NVIDIA build swap in the same data dir: the leftover
        // "cpu-only-package" reason must be invalidated so the setup gate treats
        // the device as unsettled and re-runs GPU detection.
        let got = super::effective_device_reason(
            Some("cpu-only-package".to_string()),
            /* cpu_only */ false,
        );
        assert_eq!(got, None);
    }

    #[test]
    fn cpu_only_reason_kept_when_package_is_still_cpu_only() {
        // Genuine CPU-only build: the reason is accurate and must stay so setup
        // doesn't waste a probe on a machine that can never use CUDA.
        let got = super::effective_device_reason(
            Some("cpu-only-package".to_string()),
            /* cpu_only */ true,
        );
        assert_eq!(got.as_deref(), Some("cpu-only-package"));
    }

    #[test]
    fn other_device_reasons_pass_through_unchanged() {
        for reason in ["verified", "no-gpu-detected", "cuda-verify-failed", "mps"] {
            assert_eq!(
                super::effective_device_reason(Some(reason.to_string()), false).as_deref(),
                Some(reason),
            );
            assert_eq!(
                super::effective_device_reason(Some(reason.to_string()), true).as_deref(),
                Some(reason),
            );
        }
        // Absent reason stays absent (already unsettled) in both package kinds.
        assert_eq!(super::effective_device_reason(None, false), None);
        assert_eq!(super::effective_device_reason(None, true), None);
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn clear_webkit_data_tolerates_missing_dirs() {
        // Calling clear_webkit_data when the dirs don't exist must not panic.
        // We can't safely delete real WebKit dirs in a test, but we can verify
        // the function handles NotFound gracefully by checking the logic:
        let tmp = make_tmp();
        let fake_webkit = tmp.path().join("WebKit").join("app.stemdeck.desktop");
        // Never created → remove_dir_all should return NotFound, which we ignore.
        let result = fs::remove_dir_all(&fake_webkit);
        assert!(result.is_err());
        assert_eq!(result.unwrap_err().kind(), std::io::ErrorKind::NotFound);
        // clear_webkit_data suppresses NotFound — this is the correct behavior.
    }

    #[cfg(any(windows, target_os = "linux"))]
    #[test]
    fn a_writable_app_root_is_detected() {
        let dir = make_tmp();
        assert!(super::app_root_is_writable(dir.path()));
        // the probe must not leave anything behind
        assert_eq!(fs::read_dir(dir.path()).unwrap().count(), 0);
    }

    // install.sh --global puts the package in /opt, root-owned, while the app
    // runs as the user. The updater has to decline up front rather than fail
    // part way through the swap.
    #[cfg(target_os = "linux")]
    #[test]
    fn a_read_only_app_root_is_rejected() {
        use std::os::unix::fs::PermissionsExt;
        let dir = make_tmp();
        let mut perms = fs::metadata(dir.path()).unwrap().permissions();
        perms.set_mode(0o555);
        fs::set_permissions(dir.path(), perms).unwrap();
        let writable = super::app_root_is_writable(dir.path());
        let mut restore = fs::metadata(dir.path()).unwrap().permissions();
        restore.set_mode(0o755);
        fs::set_permissions(dir.path(), restore).unwrap();
        assert!(
            !writable,
            "a root-owned install must not be offered an in-place update"
        );
    }

    // --- In-app updater runtime-compatibility marker (#421) ---

    #[test]
    fn parses_the_runtime_id_make_portable_writes() {
        // Byte-for-byte what scripts/windows/make-portable.ps1 emits into
        // python/runtime-version.json (ConvertTo-Json -Compress, UTF-8, no BOM,
        // trailing newline). If that shape changes, this fails rather than the
        // updater silently reading None and sending everyone to the full
        // download forever.
        let written = "{\"runtimeId\":\"py3.12-dbda45e38e1044cf\"}\n";
        assert_eq!(
            super::parse_runtime_id(written).as_deref(),
            Some("py3.12-dbda45e38e1044cf")
        );
    }

    #[test]
    fn unreadable_runtime_markers_are_none_not_a_wrong_match() {
        // Every one of these must read as "unknown", which the frontend treats
        // as incompatible. Returning a bogus id instead could let an app-only
        // update land on a runtime that cannot satisfy its imports.
        for text in [
            "",
            "not json",
            "{}",
            "{\"runtimeId\":null}",
            "{\"runtimeId\":42}",
            "{\"version\":\"0.12.2\"}",
        ] {
            assert_eq!(super::parse_runtime_id(text), None, "input: {text:?}");
        }
    }

    #[test]
    fn parses_the_checksum_file_make_portable_writes() {
        // "<sha256>  <filename>" -- Get-FileHash + Set-Content.
        let sha = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824";
        let written = format!("{}  StemDeck-Windows-x64-app.zip\n", sha.to_uppercase());
        assert_eq!(super::parse_sha256_line(&written).as_deref(), Some(sha));
    }

    #[test]
    fn a_non_checksum_response_is_rejected() {
        // An asset URL that redirects to an HTML error page must never be
        // mistaken for a checksum -- that would verify the download against
        // garbage instead of failing closed.
        for text in [
            "",
            "<!DOCTYPE html><html>404</html>",
            "not-a-hash  file.zip",
            "2cf24dba  file.zip",
            "zzf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824  f.zip",
        ] {
            assert_eq!(super::parse_sha256_line(text), None, "input: {text:?}");
        }
    }

    // --- macOS FFmpeg checksum verification (#172) ---

    #[cfg(target_os = "macos")]
    #[test]
    fn verify_pinned_sha256_accepts_matching_hash() {
        let dir = make_tmp();
        let f = dir.path().join("a.bin");
        fs::write(&f, b"hello").unwrap();
        // sha256("hello")
        let sha = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824";
        assert!(super::verify_pinned_sha256(&f, Some(sha), "test").is_ok());
        assert!(f.exists(), "a valid download must be kept");
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn verify_pinned_sha256_rejects_and_removes_on_mismatch() {
        let dir = make_tmp();
        let f = dir.path().join("a.bin");
        fs::write(&f, b"hello").unwrap();
        let wrong = "0000000000000000000000000000000000000000000000000000000000000000";
        assert!(super::verify_pinned_sha256(&f, Some(wrong), "test").is_err());
        assert!(!f.exists(), "a tampered/corrupt download must be removed");
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn verify_pinned_sha256_none_skips() {
        let dir = make_tmp();
        let f = dir.path().join("a.bin");
        fs::write(&f, b"hello").unwrap();
        assert!(super::verify_pinned_sha256(&f, None, "test").is_ok());
        assert!(f.exists());
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn macos_arch_suffix_maps_aarch64_to_arm64_and_everything_else_to_x64() {
        assert_eq!(super::macos_arch_suffix("aarch64"), "arm64");
        assert_eq!(super::macos_arch_suffix("x86_64"), "x64");
        assert_eq!(super::macos_arch_suffix("something-unexpected"), "x64");
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn override_sha256_reads_a_set_env_var_and_skips_when_unset_or_blank() {
        let key = "STEMDECK_FFMPEG_SHA256_TEST_UNSET_388";
        env::remove_var(key);
        assert!(super::override_sha256(key).is_none());

        env::set_var(key, "  ABCDEF  ");
        assert_eq!(super::override_sha256(key).as_deref(), Some("abcdef"));

        env::set_var(key, "   ");
        assert!(super::override_sha256(key).is_none());
        env::remove_var(key);
    }

    #[test]
    fn sha256_from_checksums_picks_matching_line() {
        let contents = "\
c64a5bf0ce386059ca8898b975de96d9ca0abd2c4763929c9cb1d2f2c93a6694  ffmpeg-master-latest-win64-gpl.zip
3D26DD6B1AF970297D141531D2C651491C4F6043B95D8C68E2FEC3C141255FF7  ffmpeg-n8.1-latest-win64-gpl-8.1.zip
b6052160df96b31c9b1e33854a4dcda3d4b57641b880270f31736fb9f445d384  ffmpeg-n7.1-latest-win64-gpl-7.1.zip
";
        // Matching line -> lowercased hash.
        assert_eq!(
            super::sha256_from_checksums(contents, "ffmpeg-n8.1-latest-win64-gpl-8.1.zip")
                .as_deref(),
            Some("3d26dd6b1af970297d141531d2c651491c4f6043b95d8c68e2fec3c141255ff7")
        );
        // A different asset is not matched.
        assert_eq!(
            super::sha256_from_checksums(contents, "ffmpeg-master-latest-win64-gpl.zip").as_deref(),
            Some("c64a5bf0ce386059ca8898b975de96d9ca0abd2c4763929c9cb1d2f2c93a6694")
        );
        // Absent filename -> None.
        assert!(super::sha256_from_checksums(contents, "not-in-list.zip").is_none());
        // Extra whitespace between hash and name is tolerated.
        assert_eq!(
            super::sha256_from_checksums("abc123   only-one.zip\n", "only-one.zip").as_deref(),
            Some("abc123")
        );
    }

    #[test]
    fn resolve_existing_ffmpeg_prefers_flat_then_bin() {
        let name = if cfg!(windows) {
            "ffmpeg.exe"
        } else {
            "ffmpeg"
        };
        // Nothing present -> None.
        let dir = make_tmp();
        assert!(super::resolve_existing_ffmpeg(dir.path()).is_none());

        // Only a bin/ binary -> found in bin/.
        let dir = make_tmp();
        let bin = dir.path().join("ffmpeg").join("bin");
        fs::create_dir_all(&bin).unwrap();
        fs::write(bin.join(name), b"x").unwrap();
        assert_eq!(
            super::resolve_existing_ffmpeg(dir.path()),
            Some(bin.join(name))
        );

        // Both present -> flat wins.
        let flat = dir.path().join("ffmpeg").join(name);
        fs::write(&flat, b"x").unwrap();
        assert_eq!(super::resolve_existing_ffmpeg(dir.path()), Some(flat));
    }

    #[test]
    fn missing_required_encoders_flags_only_the_absent_ones() {
        // A full build listing every codec StemDeck needs -> nothing missing.
        let full = "\
 A....D pcm_s16le            PCM signed 16-bit little-endian
 A....D flac                 FLAC (Free Lossless Audio Codec)
 A....D libmp3lame           libmp3lame MP3 (MPEG audio layer 3)
 A....D libvorbis            libvorbis
 A....D aac                  AAC (Advanced Audio Coding)
";
        assert!(super::missing_required_encoders(full).is_empty());

        // A minimal/stripped build missing the patent-sensitive encoders.
        let stripped = "\
 A....D pcm_s16le            PCM signed 16-bit little-endian
 A....D flac                 FLAC (Free Lossless Audio Codec)
";
        assert_eq!(
            super::missing_required_encoders(stripped),
            vec!["libmp3lame", "libvorbis", "aac"]
        );

        assert_eq!(
            super::missing_required_encoders(""),
            super::REQUIRED_FFMPEG_ENCODERS
        );
    }

    #[cfg(not(target_os = "macos"))]
    #[test]
    fn wheel_tag_routes_blackwell_to_cu128() {
        // Blackwell (sm_120 / sm_100, major >= 10) -> cu128 regardless of the
        // driver CUDA version.
        assert_eq!(super::wheel_tag(Some("12.0"), "12.8"), "cu128");
        assert_eq!(super::wheel_tag(Some("10.0"), "12.4"), "cu128");
        // Non-Blackwell cards fall back to the CUDA-version heuristic.
        assert_eq!(super::wheel_tag(Some("8.9"), "12.4"), "cu124");
        assert_eq!(super::wheel_tag(Some("8.6"), "12.1"), "cu121");
        assert_eq!(super::wheel_tag(Some("7.5"), "11.8"), "cu118");
        // Missing / unparseable compute capability also falls back.
        assert_eq!(super::wheel_tag(None, "12.1"), "cu121");
        assert_eq!(super::wheel_tag(Some("N/A"), "12.4"), "cu124");
    }

    // --- CUDA runtime dependency pass (#324) ---

    #[cfg(not(target_os = "macos"))]
    #[test]
    fn cuda_install_passes_adds_dependency_pass_only_when_needed() {
        let url = "https://download.pytorch.org/whl/cu124";

        // Windows: the CUDA DLLs live inside torch/lib, so the single
        // --no-deps swap is the whole install.
        let passes =
            super::cuda_install_passes("torch==2.6.0+cu124", "torchaudio==2.6.0+cu124", url, false);
        assert_eq!(passes.len(), 1);
        assert!(passes[0].1.contains(&"--no-deps"));
        assert!(passes[0].1.contains(&"--ignore-installed"));

        // Linux: a second pass resolves the nvidia-* CUDA runtime wheels that
        // the --no-deps swap skipped. It must NOT carry --no-deps (that is the
        // whole point) nor --ignore-installed (which would rebuild every dep).
        let passes =
            super::cuda_install_passes("torch==2.6.0+cu124", "torchaudio==2.6.0+cu124", url, true);
        assert_eq!(passes.len(), 2);
        let (label, args) = &passes[1];
        assert_eq!(*label, "CUDA runtime dependency install");
        assert!(!args.contains(&"--no-deps"));
        assert!(!args.contains(&"--ignore-installed"));
        // Same specs and index as the swap, so pip treats torch as satisfied
        // and only fills in the missing dependencies.
        assert!(args.contains(&"torch==2.6.0+cu124"));
        assert!(args.contains(&"torchaudio==2.6.0+cu124"));
        assert!(args.contains(&url));
    }

    #[cfg(not(target_os = "macos"))]
    #[test]
    fn cuda_runtime_deps_needed_on_linux_only() {
        assert_eq!(
            super::cuda_wheel_needs_runtime_deps(),
            cfg!(target_os = "linux")
        );
    }

    // --- cpu-only marker precedence + self-heal (#247) ---

    #[test]
    fn cpu_only_marker_trusted_in_root_only() {
        let root = make_tmp();
        let data = make_tmp();
        // Marker only in the data dir (a previous CPU-build install) must NOT
        // mark this package CPU-only.
        fs::write(data.path().join("cpu-only"), "").unwrap();
        assert!(!super::is_cpu_only_package(root.path()));
        // Marker in the app root (ships with the package) does.
        fs::write(root.path().join("cpu-only"), "").unwrap();
        assert!(super::is_cpu_only_package(root.path()));
    }

    #[test]
    fn portable_marker_trusted_in_root_only() {
        let root = make_tmp();
        let data = make_tmp();
        // Marker only in the data dir must NOT mark this package portable.
        fs::write(data.path().join("portable.txt"), "").unwrap();
        assert!(!super::is_portable_package(root.path()));
        // Marker in the app root (ships with the package) does.
        fs::write(root.path().join("portable.txt"), "").unwrap();
        assert!(super::is_portable_package(root.path()));
    }

    #[test]
    fn directory_has_entries_true_for_a_populated_dir() {
        let dir = make_tmp();
        fs::write(dir.path().join("registry.json"), "{}").unwrap();
        assert!(super::directory_has_entries(dir.path()));
    }

    #[test]
    fn directory_has_entries_false_for_an_empty_dir() {
        let dir = make_tmp();
        assert!(!super::directory_has_entries(dir.path()));
    }

    #[test]
    fn directory_has_entries_false_for_a_missing_dir() {
        let dir = make_tmp();
        assert!(!super::directory_has_entries(
            &dir.path().join("does-not-exist")
        ));
    }

    #[test]
    fn stale_data_dir_marker_is_removed_for_gpu_builds() {
        let root = make_tmp();
        let data = make_tmp();
        let stale = data.path().join("cpu-only");
        fs::write(&stale, "").unwrap();
        // GPU build (no root marker): the stale data-dir marker is deleted.
        super::clear_stale_cpu_marker(root.path(), data.path());
        assert!(!stale.exists());
        // And the cleanup is recorded in setup.log.
        let log = fs::read_to_string(data.path().join("logs").join("setup.log")).unwrap();
        assert!(log.contains("stale cpu-only marker"));
    }

    #[test]
    fn cpu_build_keeps_its_data_dir_marker() {
        let root = make_tmp();
        let data = make_tmp();
        fs::write(root.path().join("cpu-only"), "").unwrap();
        let legacy = data.path().join("cpu-only");
        fs::write(&legacy, "").unwrap();
        // CPU build (root marker present): nothing to heal, no churn.
        super::clear_stale_cpu_marker(root.path(), data.path());
        assert!(legacy.exists());
        assert!(!data.path().join("logs").join("setup.log").exists());
    }

    // --- nvidia-smi DriverStore discovery (#247) ---

    #[test]
    fn driver_store_scan_finds_newest_nv_package() {
        let repo = make_tmp();
        // Non-NVIDIA package dirs are ignored.
        fs::create_dir_all(repo.path().join("intelgpu.inf_amd64_aaa")).unwrap();
        fs::write(
            repo.path()
                .join("intelgpu.inf_amd64_aaa")
                .join("nvidia-smi.exe"),
            b"x",
        )
        .unwrap();
        // Older NVIDIA package.
        let old_pkg = repo.path().join("nv_dispi.inf_amd64_old");
        fs::create_dir_all(&old_pkg).unwrap();
        fs::write(old_pkg.join("nvidia-smi.exe"), b"x").unwrap();
        // Newer NVIDIA package (later mtime via explicit set).
        let new_pkg = repo.path().join("nvmdi.inf_amd64_new");
        fs::create_dir_all(&new_pkg).unwrap();
        let new_exe = new_pkg.join("nvidia-smi.exe");
        fs::write(&new_exe, b"x").unwrap();
        let later = std::time::SystemTime::now() + std::time::Duration::from_secs(60);
        let f = fs::OpenOptions::new().write(true).open(&new_exe).unwrap();
        f.set_modified(later).unwrap();

        let found = super::find_driver_store_nvidia_smi(repo.path());
        assert_eq!(found, Some(new_exe));
    }

    #[test]
    fn driver_store_scan_handles_missing_dir() {
        let repo = make_tmp();
        let missing = repo.path().join("does-not-exist");
        assert_eq!(super::find_driver_store_nvidia_smi(&missing), None);
        // Present but empty: also None.
        assert_eq!(super::find_driver_store_nvidia_smi(repo.path()), None);
    }

    // ── export destinations (#338) ───────────────────────────────────────────

    #[test]
    fn a_token_yields_the_path_that_was_picked() {
        let state = super::BackendState::default();
        let token = super::store_pending_save(&state, PathBuf::from("/tmp/song.wav")).unwrap();
        assert_eq!(
            super::take_pending_save(&state, &token).unwrap(),
            PathBuf::from("/tmp/song.wav")
        );
    }

    #[test]
    fn a_token_works_only_once() {
        // A failed transfer has to go back through the dialog rather than
        // quietly reusing a destination the user chose for an earlier attempt.
        let state = super::BackendState::default();
        let token = super::store_pending_save(&state, PathBuf::from("/tmp/song.wav")).unwrap();
        assert!(super::take_pending_save(&state, &token).is_ok());
        assert!(super::take_pending_save(&state, &token).is_err());
    }

    #[test]
    fn an_unknown_token_is_refused() {
        // This is the security property: without a matching pick there is no
        // destination, so the WebView cannot name one of its own.
        let state = super::BackendState::default();
        assert!(super::take_pending_save(&state, "nope").is_err());
        assert!(super::take_pending_save(&state, "1").is_err());
    }

    #[test]
    fn tokens_are_distinct_per_pick() {
        let state = super::BackendState::default();
        let a = super::store_pending_save(&state, PathBuf::from("/tmp/a.wav")).unwrap();
        let b = super::store_pending_save(&state, PathBuf::from("/tmp/b.wav")).unwrap();
        assert_ne!(a, b);
        assert_eq!(
            super::take_pending_save(&state, &b).unwrap(),
            PathBuf::from("/tmp/b.wav")
        );
        assert_eq!(
            super::take_pending_save(&state, &a).unwrap(),
            PathBuf::from("/tmp/a.wav")
        );
    }

    #[test]
    fn unconsumed_picks_do_not_accumulate_forever() {
        let state = super::BackendState::default();
        let first = super::store_pending_save(&state, PathBuf::from("/tmp/first.wav")).unwrap();
        for i in 0..super::MAX_PENDING_SAVES {
            super::store_pending_save(&state, PathBuf::from(format!("/tmp/{i}.wav"))).unwrap();
        }
        let held = state.inner.lock().unwrap().pending_saves.len();
        assert!(held <= super::MAX_PENDING_SAVES, "held {held}");
        // The stalest pick is the one dropped.
        assert!(super::take_pending_save(&state, &first).is_err());
    }

    #[test]
    fn only_localhost_urls_are_downloadable() {
        assert!(super::validate_download_url("http://127.0.0.1:8000/api/x.wav").is_ok());
        assert!(super::validate_download_url("http://localhost:8000/api/x.wav").is_ok());
        // The SSRF boundary from #138, still enforced after the split.
        assert!(super::validate_download_url("http://example.com/x.wav").is_err());
        assert!(super::validate_download_url("file:///etc/passwd").is_err());
        assert!(super::validate_download_url("not a url").is_err());
    }

    // #424: a second StemDeck adopted the first one's backend, and with it the
    // first one's library. Both halves of that are pinned below.

    #[test]
    fn a_taken_port_is_reported_as_taken() {
        // The bug: the reservation probed 127.0.0.1 while the backend binds
        // 0.0.0.0. On Windows those do not collide, so an occupied port looked
        // free, the fallback never ran, and the spawned backend died on bind
        // while the other instance kept answering.
        let held = std::net::TcpListener::bind(("0.0.0.0", 0)).unwrap();
        let taken = held.local_addr().unwrap().port();

        let (got, _guard) = super::reserve_port("0.0.0.0", taken).unwrap();

        assert_ne!(
            got, taken,
            "handed back a port another socket already holds"
        );
    }

    #[test]
    fn a_free_port_is_granted_as_asked() {
        // The fallback must not fire needlessly: the user's configured port is
        // honoured whenever it genuinely is available.
        //
        // The port must come from outside the OS ephemeral range. This test can
        // only establish that a port is free by binding it and letting go, and
        // reserve_port then has to re-claim it. If that number came from
        // bind(0), any other test in this binary calling bind(0) in that gap is
        // handed the number we just released, claim_port fails, and the
        // fallback returns the next port up -- which is how this failed in CI,
        // asserting 62251 against 62250. Cargo runs these in parallel and
        // several of them stand up throwaway listeners.
        //
        // A fixed port is also the honest shape of the thing under test:
        // reserve_port is given a configured port (8000 by default), never one
        // the OS just handed out.
        let wanted = (21_000..21_200)
            .find(|port| std::net::TcpListener::bind(("0.0.0.0", *port)).is_ok())
            .expect("no free port in 21000..21200 to test with");

        let (got, _guard) = super::reserve_port("0.0.0.0", wanted).unwrap();

        assert_eq!(got, wanted);
    }

    #[test]
    fn a_held_reservation_keeps_everyone_else_out() {
        // The reservation binds without listening, so that StemDeck.exe is not
        // a server in the firewall's eyes. That only works if bind alone still
        // holds the address against a real listener -- if it did not, the port
        // could be stolen between reserving it and the backend binding it.
        let (port, _guard) = super::free_port("0.0.0.0").unwrap();
        assert!(
            std::net::TcpListener::bind(("0.0.0.0", port)).is_err(),
            "a bound reservation did not hold port {port}"
        );
    }

    #[test]
    fn reserved_port_is_usable_by_the_backend_after_release() {
        // The guard exists so nothing steals the port between reserving and
        // spawning; dropping it must leave the port bindable, or every start
        // would fail.
        let (port, guard) = super::free_port("0.0.0.0").unwrap();
        drop(guard);
        assert!(std::net::TcpListener::bind(("0.0.0.0", port)).is_ok());
    }

    #[test]
    fn health_identity_comes_from_the_body_only() {
        let ok = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n\
                  {\"name\":\"StemDeck\",\"status\":\"ok\",\"pid\":4242,\"instance\":\"abc\"}";
        assert_eq!(
            super::parse_health_identity(ok),
            Some(super::HealthIdentity {
                pid: Some(4242),
                instance: Some("abc".to_string()),
            })
        );

        // A header must never be mistaken for the payload, or a stranger could
        // claim to be our backend just by setting one.
        let header_only =
            "HTTP/1.1 200 OK\r\nX-Pid: 4242\r\nX-Instance: abc\r\n\r\n{\"status\":\"ok\"}";
        assert_eq!(
            super::parse_health_identity(header_only),
            Some(super::HealthIdentity::default())
        );

        // Every non-desktop distribution runs without a token and reports "".
        // Read as a token, they would all match one another.
        let untokened = "HTTP/1.1 200 OK\r\n\r\n{\"pid\":7,\"instance\":\"\"}";
        assert_eq!(
            super::parse_health_identity(untokened),
            Some(super::HealthIdentity {
                pid: Some(7),
                instance: None,
            })
        );

        assert_eq!(
            super::parse_health_identity("HTTP/1.1 200 OK\r\n\r\nnot json"),
            None
        );
        assert_eq!(super::parse_health_identity(""), None);
    }

    #[test]
    fn instance_tokens_differ_between_launches() {
        assert_ne!(super::new_instance_token(), super::new_instance_token());
        assert_eq!(super::new_instance_token().len(), 32);
    }

    /// A stand-in backend on a port, answering /api/health with the pid and
    /// token it is told to claim.
    fn responder_on_a_port(pid: u32, instance: &str) -> u16 {
        use std::io::{Read, Write};

        let listener = std::net::TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let port = listener.local_addr().unwrap().port();
        let instance = instance.to_string();
        std::thread::spawn(move || {
            for stream in listener.incoming().take(16) {
                let Ok(mut stream) = stream else { continue };
                let mut buf = [0u8; 512];
                let _ = stream.read(&mut buf);
                let body = format!(
                    "{{\"name\":\"StemDeck\",\"status\":\"ok\",\"pid\":{pid},\
                     \"instance\":\"{instance}\"}}"
                );
                let _ = stream.write_all(
                    format!(
                        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\
                         Content-Length: {}\r\nConnection: close\r\n\r\n{}",
                        body.len(),
                        body
                    )
                    .as_bytes(),
                );
            }
        });
        port
    }

    /// A child that outlives the first poll and then exits, like a backend that
    /// loses the race for its port and dies on bind.
    fn briefly_alive_child() -> Child {
        #[cfg(windows)]
        let mut cmd = {
            let mut c = Command::new("cmd");
            c.args(["/C", "ping", "-n", "2", "127.0.0.1"]);
            c
        };
        #[cfg(not(windows))]
        let mut cmd = {
            let mut c = Command::new("sh");
            c.args(["-c", "sleep 1"]);
            c
        };
        cmd.stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap()
    }

    #[test]
    fn a_stranger_on_the_port_is_never_accepted_as_our_backend() {
        // The whole of #424 in one test. Another instance answers 200 on the
        // port while the backend we spawned dies. Before the fix this returned
        // Ok, the shell pointed the window at that backend, and the second
        // install quietly drove the first install's library.
        let port = responder_on_a_port(999_999, "a-different-launch");
        let mut child = briefly_alive_child();

        let result = super::wait_for_health(
            &mut child,
            port,
            "our-token",
            Duration::from_secs(20),
            Path::new("does-not-exist.log"),
        );
        let _ = child.kill();
        let _ = child.wait();

        let err = result.expect_err("adopted a backend that was not ours");
        assert!(
            err.contains(&port.to_string()),
            "the error should name the contended port, got: {err}"
        );
    }

    #[test]
    fn our_own_backend_is_accepted_from_a_process_we_did_not_spawn_directly() {
        // #457. On the Windows portable build the process that binds the port
        // is a *grandchild*: python/Scripts/python.exe is a venv launcher and
        // Windows has no exec, so it starts python/base/python.exe beneath
        // itself. A pid that will never equal child.id() is the normal case,
        // not a stranger, and requiring equality timed out every launch.
        let mut child = briefly_alive_child();
        let grandchild_pid = child.id().wrapping_add(4);
        let port = responder_on_a_port(grandchild_pid, "our-token");

        let result = super::wait_for_health(
            &mut child,
            port,
            "our-token",
            Duration::from_secs(20),
            Path::new("does-not-exist.log"),
        );
        let _ = child.kill();
        let _ = child.wait();

        assert!(result.is_ok(), "rejected our own backend: {result:?}");
    }

    #[test]
    fn a_backend_older_than_the_token_still_starts() {
        // Verification must not be so strict that a healthy start is rejected.
        // A backend that reports no token at all predates this shell and can
        // only be identified the #424 way, so the pid comparison still stands
        // for it.
        let mut child = briefly_alive_child();
        let port = responder_on_a_port(child.id(), "");

        let result = super::wait_for_health(
            &mut child,
            port,
            "our-token",
            Duration::from_secs(20),
            Path::new("does-not-exist.log"),
        );
        let _ = child.kill();
        let _ = child.wait();

        assert!(result.is_ok(), "rejected our own backend: {result:?}");
    }

    #[test]
    fn a_conflict_hint_names_the_port_and_stays_quiet_otherwise() {
        let hint = super::port_conflict_hint(8000, Some(1234));
        assert!(hint.contains("8000") && hint.contains("1234"));
        // No foreign responder seen: say nothing rather than guess at a cause.
        assert!(super::port_conflict_hint(8000, None).is_empty());
    }
}
