//! Linux: keep WebKitGTK's DMA-BUF renderer from killing StemDeck at launch (#780).
//!
//! On a Wayland session with the NVIDIA driver, WebKitGTK's DMA-BUF renderer
//! commits a buffer without the explicit-sync timeline the compositor expects,
//! and the compositor drops the connection ("Missing acquire timeline", GDK
//! "Error 71"). The process exits before the window shows anything. Setting
//! `WEBKIT_DISABLE_DMABUF_RENDERER=1` before GTK starts avoids it, and has to
//! be done by the process itself, because a desktop launcher gives the user no
//! place to set it.
//!
//! Decision order, first match wins:
//!
//! 1. `WEBKIT_DISABLE_DMABUF_RENDERER` already in the environment: left alone.
//! 2. `STEMDECK_WEBKIT_DMABUF=on|off`: `on` keeps the renderer, disables both
//!    automatic rules and forgets a saved fallback; `off` turns it off.
//!    `auto`, unset or an unknown value carry on to the rules below.
//! 3. The crash guard: a saved fallback, or a previous launch that died
//!    before its window finished loading, turns the renderer off. The second
//!    case saves the fallback, so it stays off on later launches.
//! 4. Wayland (XWayland included) with the NVIDIA driver loaded: off.
//!
//! Everything that decides is a pure function here and is unit-tested on
//! every platform. Only the thin shell at the bottom touches the process
//! environment and the files, and it is Linux-only.

use std::fs;
use std::io::{self, Write};
use std::path::Path;

/// Written when the window is about to be built, removed once it has loaded.
pub const MARKER_FILE: &str = "launch-pending";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Mode {
    Auto,
    On,
    Off,
}

/// `None` or blank means auto. An unknown value is returned as `Err` so the
/// caller can log it; it is then treated as auto.
pub fn parse_mode(raw: Option<&str>) -> Result<Mode, String> {
    let Some(raw) = raw else {
        return Ok(Mode::Auto);
    };
    match raw.trim().to_ascii_lowercase().as_str() {
        "" | "auto" => Ok(Mode::Auto),
        "on" => Ok(Mode::On),
        "off" => Ok(Mode::Off),
        _ => Err(raw.to_string()),
    }
}

/// A Wayland session. `WAYLAND_DISPLAY` stays set when GTK is pushed onto
/// XWayland with `GDK_BACKEND=x11`, so that case counts too: the reporter's
/// XWayland attempt failed in the same renderer, in a different way.
pub fn is_wayland(wayland_display: Option<&str>, session_type: Option<&str>) -> bool {
    wayland_display.is_some_and(|v| !v.trim().is_empty())
        || session_type.is_some_and(|v| v.trim().eq_ignore_ascii_case("wayland"))
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Marker {
    pub pid: u32,
    /// Unix seconds. For whoever reads the file; not used to decide.
    pub started: u64,
    /// The kernel boot id, when it could be read.
    pub boot_id: Option<String>,
}

pub fn format_marker(m: &Marker) -> String {
    let mut out = format!("pid={}\nstarted={}\n", m.pid, m.started);
    if let Some(boot) = &m.boot_id {
        out.push_str(&format!("boot={boot}\n"));
    }
    out
}

pub fn parse_marker(text: &str) -> Option<Marker> {
    let mut pid = None;
    let mut started = None;
    let mut boot_id = None;
    for line in text.lines() {
        match line.split_once('=') {
            Some(("pid", v)) => pid = v.trim().parse().ok(),
            Some(("started", v)) => started = v.trim().parse().ok(),
            Some(("boot", v)) if !v.trim().is_empty() => boot_id = Some(v.trim().to_string()),
            _ => {}
        }
    }
    Some(Marker {
        pid: pid.filter(|p| *p != 0)?,
        started: started?,
        boot_id,
    })
}

/// What the marker left by the previous launch says about it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PreviousLaunch {
    /// No marker: the last launch reached a loaded window, or quit cleanly.
    Clean,
    /// Its process is gone and the window never finished loading.
    Crashed,
    /// Its process is still alive: another instance, not a crash.
    StillRunning,
    /// Unreadable, or from before a reboot: says nothing about the renderer.
    Stale,
}

pub fn classify_previous(
    marker_text: Option<&str>,
    current_boot: Option<&str>,
    own_pid: u32,
    pid_alive: impl Fn(u32) -> bool,
) -> PreviousLaunch {
    let Some(text) = marker_text else {
        return PreviousLaunch::Clean;
    };
    let Some(marker) = parse_marker(text) else {
        return PreviousLaunch::Stale;
    };
    // A power cut or a reboot in the first seconds is not a renderer crash.
    if let (Some(then), Some(now)) = (marker.boot_id.as_deref(), current_boot) {
        if then != now {
            return PreviousLaunch::Stale;
        }
    }
    // Only possible after a reboot reused the pid.
    if marker.pid == own_pid {
        return PreviousLaunch::Stale;
    }
    // A reused pid reads as alive and the crash goes unnoticed: the safe way
    // round, since a miss costs one more crash and never a wrong fallback.
    if pid_alive(marker.pid) {
        return PreviousLaunch::StillRunning;
    }
    PreviousLaunch::Crashed
}

pub struct Inputs<'a> {
    pub webkit_var: Option<&'a str>,
    pub mode: Option<&'a str>,
    pub wayland_display: Option<&'a str>,
    pub session_type: Option<&'a str>,
    pub nvidia_driver: bool,
    pub fallback_saved: bool,
    pub previous: PreviousLaunch,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Reason {
    UserSetWebkitVar,
    ForcedOn,
    ForcedOff,
    SavedFallback,
    PreviousLaunchCrashed,
    WaylandNvidia,
    NoRuleMatched,
}

impl Reason {
    pub fn describe(self) -> &'static str {
        match self {
            Reason::UserSetWebkitVar => {
                "WEBKIT_DISABLE_DMABUF_RENDERER was already set; left as is"
            }
            Reason::ForcedOn => "STEMDECK_WEBKIT_DMABUF=on; renderer left on, automatic rules off",
            Reason::ForcedOff => "STEMDECK_WEBKIT_DMABUF=off; renderer turned off",
            Reason::SavedFallback => "renderer off: saved after an earlier launch crashed",
            Reason::PreviousLaunchCrashed => {
                "renderer off: the previous launch closed before its window loaded"
            }
            Reason::WaylandNvidia => "renderer off: Wayland session with the NVIDIA driver",
            Reason::NoRuleMatched => "renderer left on: no rule matched",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Decision {
    /// Set `WEBKIT_DISABLE_DMABUF_RENDERER=1` for this process.
    pub disable_dmabuf: bool,
    pub reason: Reason,
    /// Write the launch marker for this launch.
    pub arm_guard: bool,
    /// Persist the fallback so later launches keep it.
    pub save_fallback: bool,
    /// Forget a persisted fallback.
    pub clear_fallback: bool,
    /// An unrecognised STEMDECK_WEBKIT_DMABUF value, to log.
    pub invalid_mode: Option<String>,
}

pub fn decide(i: &Inputs) -> Decision {
    let (mode, invalid_mode) = match parse_mode(i.mode) {
        Ok(m) => (m, None),
        Err(raw) => (Mode::Auto, Some(raw)),
    };
    let base = Decision {
        disable_dmabuf: false,
        reason: Reason::NoRuleMatched,
        arm_guard: false,
        save_fallback: false,
        clear_fallback: false,
        invalid_mode,
    };
    if i.webkit_var.is_some() {
        return Decision {
            reason: Reason::UserSetWebkitVar,
            ..base
        };
    }
    match mode {
        Mode::On => {
            return Decision {
                reason: Reason::ForcedOn,
                clear_fallback: i.fallback_saved,
                ..base
            }
        }
        Mode::Off => {
            return Decision {
                disable_dmabuf: true,
                reason: Reason::ForcedOff,
                ..base
            }
        }
        Mode::Auto => {}
    }
    let armed = Decision {
        arm_guard: true,
        ..base
    };
    if i.fallback_saved {
        return Decision {
            disable_dmabuf: true,
            reason: Reason::SavedFallback,
            ..armed
        };
    }
    if i.previous == PreviousLaunch::Crashed {
        return Decision {
            disable_dmabuf: true,
            reason: Reason::PreviousLaunchCrashed,
            save_fallback: true,
            ..armed
        };
    }
    if i.nvidia_driver && is_wayland(i.wayland_display, i.session_type) {
        return Decision {
            disable_dmabuf: true,
            reason: Reason::WaylandNvidia,
            ..armed
        };
    }
    armed
}

/// Temp file and rename, so a crash mid-write never leaves half a marker.
pub fn write_atomic(dir: &Path, name: &str, contents: &str) -> io::Result<()> {
    fs::create_dir_all(dir)?;
    let tmp = dir.join(format!(".{name}.{}.tmp", std::process::id()));
    let result = (|| {
        let mut f = fs::File::create(&tmp)?;
        f.write_all(contents.as_bytes())?;
        f.sync_all()?;
        fs::rename(&tmp, dir.join(name))
    })();
    if result.is_err() {
        let _ = fs::remove_file(&tmp);
    }
    result
}

/// Removes the marker only when it is this process's own, so one instance
/// never clears the marker of another that is still starting up.
pub fn clear_marker_if_ours(dir: &Path, own_pid: u32) -> bool {
    let path = dir.join(MARKER_FILE);
    let ours = fs::read_to_string(&path)
        .ok()
        .and_then(|t| parse_marker(&t))
        .is_some_and(|m| m.pid == own_pid);
    ours && fs::remove_file(&path).is_ok()
}

#[cfg(target_os = "linux")]
pub use shell::{apply_at_startup, clear_on_exit, watch_first_load};

#[cfg(target_os = "linux")]
mod shell {
    use super::*;
    use std::env;
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::OnceLock;
    use std::time::{Duration, SystemTime, UNIX_EPOCH};

    /// How long after the first page load the marker stays. The protocol
    /// error kills the process on the first frame the webview commits, within
    /// milliseconds of the page loading, so five seconds is generous. Past it,
    /// a crash is not the renderer at startup and must not flip the fallback.
    const READY_GRACE: Duration = Duration::from_secs(5);

    /// WebKitGTK's own switch. Read and set, never overridden once present.
    const WEBKIT_VAR: &str = "WEBKIT_DISABLE_DMABUF_RENDERER";
    /// StemDeck's override: `auto` (default), `on` or `off`.
    const MODE_VAR: &str = "STEMDECK_WEBKIT_DMABUF";
    /// Present while the crash guard's fallback is in force.
    const FALLBACK_FILE: &str = "webkit-dmabuf-off";

    /// The data dir, when this launch armed the guard.
    static ARMED: OnceLock<PathBuf> = OnceLock::new();

    fn log(data_dir: Option<&Path>, msg: &str) {
        eprintln!("[stemdeck] {msg}");
        if let Some(d) = data_dir {
            crate::append_to_setup_log(d, msg);
        }
    }

    fn var(name: &str) -> Option<String> {
        env::var_os(name).map(|v| v.to_string_lossy().into_owned())
    }

    fn boot_id() -> Option<String> {
        fs::read_to_string("/proc/sys/kernel/random/boot_id")
            .ok()
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
    }

    /// Decide and apply, before GTK, WebKit or any other thread exists.
    pub fn apply_at_startup() {
        let data_dir = crate::local_data_dir().ok();
        let dir = data_dir.as_deref();
        let read = |name: &str| dir.and_then(|d| fs::read_to_string(d.join(name)).ok());
        let marker = read(MARKER_FILE);
        let own_pid = std::process::id();
        let previous = classify_previous(marker.as_deref(), boot_id().as_deref(), own_pid, |p| {
            Path::new(&format!("/proc/{p}")).exists()
        });
        let webkit_var = var(WEBKIT_VAR);
        let mode = var(MODE_VAR);
        let wayland_display = var("WAYLAND_DISPLAY");
        let session_type = var("XDG_SESSION_TYPE");
        let decision = decide(&Inputs {
            webkit_var: webkit_var.as_deref(),
            mode: mode.as_deref(),
            wayland_display: wayland_display.as_deref(),
            session_type: session_type.as_deref(),
            // Present only while the NVIDIA kernel driver is loaded; nouveau
            // never creates it. A file check: no subprocess, no GPU init.
            nvidia_driver: Path::new("/proc/driver/nvidia/version").exists(),
            fallback_saved: dir.is_some_and(|d| d.join(FALLBACK_FILE).exists()),
            previous,
        });

        if let Some(raw) = &decision.invalid_mode {
            log(
                dir,
                &format!("{MODE_VAR}={raw} is not auto, on or off; using auto"),
            );
        }
        if decision.disable_dmabuf {
            // Edition 2021: set_var is safe to call, but it is only sound
            // while no other thread reads the environment. This runs first
            // thing in main, before Tauri, GTK or WebKit start a thread.
            env::set_var(WEBKIT_VAR, "1");
        }
        if let Some(d) = dir {
            if decision.clear_fallback && fs::remove_file(d.join(FALLBACK_FILE)).is_ok() {
                log(
                    dir,
                    "saved DMA-BUF fallback cleared by STEMDECK_WEBKIT_DMABUF=on",
                );
            }
            if decision.save_fallback {
                let now = SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .map(|t| t.as_secs())
                    .unwrap_or(0);
                if let Err(e) = write_atomic(d, FALLBACK_FILE, &format!("since={now}\n")) {
                    log(dir, &format!("could not save the DMA-BUF fallback: {e}"));
                }
                log(
                    dir,
                    "StemDeck closed before its window loaded last time, so WebKit's \
                     DMA-BUF renderer is now off and stays off. To turn it back on, \
                     start StemDeck once with STEMDECK_WEBKIT_DMABUF=on.",
                );
            }
            if decision.arm_guard {
                let _ = ARMED.set(d.to_path_buf());
            } else if previous != PreviousLaunch::StillRunning {
                // A leftover from an earlier launch would otherwise be read
                // as a crash the next time the guard runs.
                let _ = fs::remove_file(d.join(MARKER_FILE));
            }
        }
        log(
            dir,
            &format!("webkit dmabuf: {}", decision.reason.describe()),
        );
    }

    /// Writes the launch marker and clears it once the first page has loaded.
    /// Called with the main window's builder, after GTK is up, so a launch
    /// that fails earlier (no display at all) is never read as a crash.
    pub fn watch_first_load<'a, R: tauri::Runtime, M: tauri::Manager<R>>(
        builder: tauri::WebviewWindowBuilder<'a, R, M>,
    ) -> tauri::WebviewWindowBuilder<'a, R, M> {
        let Some(dir) = ARMED.get().cloned() else {
            return builder;
        };
        let own_pid = std::process::id();
        let marker = Marker {
            pid: own_pid,
            started: SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map(|t| t.as_secs())
                .unwrap_or(0),
            boot_id: boot_id(),
        };
        if let Err(e) = write_atomic(&dir, MARKER_FILE, &format_marker(&marker)) {
            // Never a reason not to start: the guard is just off this time.
            log(Some(&dir), &format!("launch marker not written: {e}"));
            return builder;
        }
        let started = AtomicBool::new(false);
        builder.on_page_load(move |_window, payload| {
            if matches!(payload.event(), tauri::webview::PageLoadEvent::Finished)
                && !started.swap(true, Ordering::SeqCst)
            {
                let dir = dir.clone();
                std::thread::spawn(move || {
                    std::thread::sleep(READY_GRACE);
                    clear_marker_if_ours(&dir, own_pid);
                });
            }
        })
    }

    /// A clean quit, however early, is never a crash.
    pub fn clear_on_exit() {
        if let Some(dir) = ARMED.get() {
            clear_marker_if_ours(dir, std::process::id());
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn inputs() -> Inputs<'static> {
        Inputs {
            webkit_var: None,
            mode: None,
            wayland_display: None,
            session_type: None,
            nvidia_driver: false,
            fallback_saved: false,
            previous: PreviousLaunch::Clean,
        }
    }

    fn wayland_nvidia() -> Inputs<'static> {
        Inputs {
            wayland_display: Some("wayland-1"),
            session_type: Some("wayland"),
            nvidia_driver: true,
            ..inputs()
        }
    }

    #[test]
    fn wayland_with_nvidia_turns_the_renderer_off() {
        let d = decide(&wayland_nvidia());
        assert!(d.disable_dmabuf);
        assert_eq!(d.reason, Reason::WaylandNvidia);
        assert!(d.arm_guard);
        assert!(!d.save_fallback);
    }

    #[test]
    fn wayland_without_nvidia_leaves_it_on() {
        let d = decide(&Inputs {
            nvidia_driver: false,
            ..wayland_nvidia()
        });
        assert!(!d.disable_dmabuf);
        assert_eq!(d.reason, Reason::NoRuleMatched);
        assert!(d.arm_guard);
    }

    #[test]
    fn x11_session_with_nvidia_leaves_it_on() {
        let d = decide(&Inputs {
            session_type: Some("x11"),
            nvidia_driver: true,
            ..inputs()
        });
        assert!(!d.disable_dmabuf);
        assert!(d.arm_guard);
    }

    #[test]
    fn xwayland_counts_as_wayland() {
        // GDK_BACKEND=x11 under Hyprland: WAYLAND_DISPLAY is still set.
        let d = decide(&Inputs {
            wayland_display: Some("wayland-1"),
            session_type: None,
            nvidia_driver: true,
            ..inputs()
        });
        assert!(d.disable_dmabuf);
        assert!(is_wayland(None, Some("Wayland")));
        assert!(!is_wayland(Some(" "), Some("x11")));
    }

    #[test]
    fn a_value_the_user_set_wins_over_everything() {
        for webkit in ["1", "0", ""] {
            let d = decide(&Inputs {
                webkit_var: Some(webkit),
                mode: Some("off"),
                fallback_saved: true,
                previous: PreviousLaunch::Crashed,
                ..wayland_nvidia()
            });
            assert!(!d.disable_dmabuf);
            assert_eq!(d.reason, Reason::UserSetWebkitVar);
            assert!(!d.arm_guard && !d.save_fallback && !d.clear_fallback);
        }
    }

    #[test]
    fn mode_on_keeps_the_renderer_and_forgets_the_fallback() {
        let d = decide(&Inputs {
            mode: Some("ON"),
            fallback_saved: true,
            previous: PreviousLaunch::Crashed,
            ..wayland_nvidia()
        });
        assert!(!d.disable_dmabuf);
        assert_eq!(d.reason, Reason::ForcedOn);
        assert!(!d.arm_guard && !d.save_fallback);
        assert!(d.clear_fallback);
        let none_saved = decide(&Inputs {
            mode: Some("on"),
            ..inputs()
        });
        assert!(!none_saved.clear_fallback);
    }

    #[test]
    fn mode_off_turns_it_off_anywhere() {
        let d = decide(&Inputs {
            mode: Some(" off "),
            ..inputs()
        });
        assert!(d.disable_dmabuf);
        assert_eq!(d.reason, Reason::ForcedOff);
        assert!(!d.arm_guard && !d.save_fallback);
    }

    #[test]
    fn mode_auto_and_unset_run_the_rules() {
        for mode in [None, Some("auto"), Some("")] {
            let d = decide(&Inputs {
                mode,
                ..wayland_nvidia()
            });
            assert_eq!(d.reason, Reason::WaylandNvidia);
            assert_eq!(d.invalid_mode, None);
        }
    }

    #[test]
    fn an_invalid_mode_is_reported_and_treated_as_auto() {
        let d = decide(&Inputs {
            mode: Some("yes"),
            ..wayland_nvidia()
        });
        assert_eq!(d.invalid_mode.as_deref(), Some("yes"));
        assert_eq!(d.reason, Reason::WaylandNvidia);
        assert_eq!(parse_mode(Some("maybe")), Err("maybe".to_string()));
    }

    #[test]
    fn a_crashed_previous_launch_turns_it_off_and_saves_that() {
        let d = decide(&Inputs {
            previous: PreviousLaunch::Crashed,
            ..inputs()
        });
        assert!(d.disable_dmabuf);
        assert_eq!(d.reason, Reason::PreviousLaunchCrashed);
        assert!(d.save_fallback && d.arm_guard);
    }

    #[test]
    fn a_saved_fallback_stays_in_force() {
        let d = decide(&Inputs {
            fallback_saved: true,
            ..inputs()
        });
        assert!(d.disable_dmabuf);
        assert_eq!(d.reason, Reason::SavedFallback);
        assert!(!d.save_fallback && !d.clear_fallback);
    }

    #[test]
    fn a_live_or_stale_previous_launch_changes_nothing() {
        for previous in [PreviousLaunch::StillRunning, PreviousLaunch::Stale] {
            let d = decide(&Inputs {
                previous,
                ..inputs()
            });
            assert!(!d.disable_dmabuf);
            assert!(!d.save_fallback);
        }
    }

    #[test]
    fn every_reason_has_a_log_line() {
        for r in [
            Reason::UserSetWebkitVar,
            Reason::ForcedOn,
            Reason::ForcedOff,
            Reason::SavedFallback,
            Reason::PreviousLaunchCrashed,
            Reason::WaylandNvidia,
            Reason::NoRuleMatched,
        ] {
            assert!(r.describe().is_ascii() && !r.describe().is_empty());
        }
    }

    fn marker(pid: u32, boot: Option<&str>) -> String {
        format_marker(&Marker {
            pid,
            started: 1_700_000_000,
            boot_id: boot.map(str::to_string),
        })
    }

    #[test]
    fn marker_round_trips() {
        let m = Marker {
            pid: 42,
            started: 7,
            boot_id: Some("abc".into()),
        };
        assert_eq!(parse_marker(&format_marker(&m)), Some(m));
        assert_eq!(parse_marker("pid=0\nstarted=1\n"), None);
        assert_eq!(parse_marker("garbage"), None);
    }

    #[test]
    fn marker_with_a_dead_pid_is_a_crash() {
        let text = marker(4242, Some("boot-a"));
        let got = classify_previous(Some(&text), Some("boot-a"), 1, |_| false);
        assert_eq!(got, PreviousLaunch::Crashed);
        // No boot id on either side: still decided by the pid.
        let text = marker(4242, None);
        assert_eq!(
            classify_previous(Some(&text), None, 1, |_| false),
            PreviousLaunch::Crashed
        );
    }

    #[test]
    fn marker_with_a_live_pid_is_another_instance() {
        let text = marker(4242, Some("boot-a"));
        let got = classify_previous(Some(&text), Some("boot-a"), 1, |p| p == 4242);
        assert_eq!(got, PreviousLaunch::StillRunning);
    }

    #[test]
    fn no_marker_means_a_clean_previous_launch() {
        assert_eq!(
            classify_previous(None, Some("boot-a"), 1, |_| false),
            PreviousLaunch::Clean
        );
    }

    #[test]
    fn stale_markers_are_ignored() {
        let other_boot = marker(4242, Some("boot-a"));
        assert_eq!(
            classify_previous(Some(&other_boot), Some("boot-b"), 1, |_| false),
            PreviousLaunch::Stale
        );
        let own_pid = marker(7, None);
        assert_eq!(
            classify_previous(Some(&own_pid), None, 7, |_| false),
            PreviousLaunch::Stale
        );
        assert_eq!(
            classify_previous(Some("half a fi"), None, 1, |_| false),
            PreviousLaunch::Stale
        );
    }

    #[test]
    fn a_clean_quit_leaves_no_marker_behind() {
        let tmp = tempfile::tempdir().unwrap();
        write_atomic(tmp.path(), MARKER_FILE, &marker(99, None)).unwrap();
        // Another instance's marker is left alone.
        assert!(!clear_marker_if_ours(tmp.path(), 98));
        assert!(tmp.path().join(MARKER_FILE).exists());
        assert!(clear_marker_if_ours(tmp.path(), 99));
        let left = fs::read_to_string(tmp.path().join(MARKER_FILE)).ok();
        assert_eq!(
            classify_previous(left.as_deref(), None, 1, |_| false),
            PreviousLaunch::Clean
        );
        // No temp file is left next to it.
        assert_eq!(fs::read_dir(tmp.path()).unwrap().count(), 0);
    }

    #[test]
    fn an_unwritable_data_dir_fails_quietly() {
        let tmp = tempfile::tempdir().unwrap();
        // A regular file where the directory should be: create_dir_all fails
        // on every platform, without depending on permissions or root.
        let blocker = tmp.path().join("not-a-dir");
        fs::write(&blocker, "").unwrap();
        let dir = blocker.join("stemdeck");
        assert!(write_atomic(&dir, MARKER_FILE, &marker(5, None)).is_err());
        assert!(!clear_marker_if_ours(&dir, 5));
        assert_eq!(
            classify_previous(None, None, 5, |_| false),
            PreviousLaunch::Clean
        );
    }
}
