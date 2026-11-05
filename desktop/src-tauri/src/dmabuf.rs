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
//!    automatic rules and forgets everything the crash guard saved; `off`
//!    turns it off. `auto`, unset or an unknown value carry on to the rules
//!    below.
//! 3. The crash guard (#993). A launch that dies before its window has loaded
//!    and stayed up for a few seconds is an early exit. The dying process
//!    cannot report why (a protocol error ends it with `_exit`, a kill or an
//!    out-of-memory kill gives it no say at all), so one early exit is no
//!    evidence against the renderer: it is counted, and the renderer is tried
//!    again. Only an early exit that had the renderer on is counted, and only
//!    [`EARLY_EXITS_TO_SAVE`] of them in a row save the fallback. A launch
//!    that loads and stays up resets the count. A saved fallback is not for
//!    good: after [`RETRY_AFTER_CLEAN_RUNS`] clean launches under it, or when
//!    the WebKitGTK library or the NVIDIA driver changes, the renderer is
//!    tried again, and one more early exit puts the fallback back.
//! 4. Wayland (XWayland included) with the NVIDIA driver loaded: off. Per
//!    launch; nothing is saved.
//!
//! Everything that decides is a pure function here and is unit-tested on
//! every platform, with the clock, the files' contents and the environment
//! passed in. Only the thin shell at the bottom touches the process
//! environment and the files, and it is Linux-only.

use std::fs;
use std::io::{self, Write};
use std::path::Path;

/// Written when the window is about to be built, removed once it has loaded.
pub const MARKER_FILE: &str = "launch-pending";

/// Early exits in a row, each with the renderer on, that save the fallback.
/// Two, so a single unrelated death (a kill, a logout, an out-of-memory kill)
/// changes nothing, while a renderer that kills every launch is off from the
/// third launch on.
pub const EARLY_EXITS_TO_SAVE: u32 = 2;

/// Clean launches under a saved fallback before the renderer is tried again.
/// Doubled after each retry that failed, up to [`MAX_RETRY_DOUBLINGS`] times,
/// so a machine where the renderer really crashes pays one crash every 10,
/// then 20, 40 and at most 80 launches, while a fallback saved by bad luck
/// goes away by itself.
pub const RETRY_AFTER_CLEAN_RUNS: u32 = 10;
pub const MAX_RETRY_DOUBLINGS: u32 = 3;

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
    /// That launch ran with the DMA-BUF renderer on. A marker without the
    /// line counts as on, as every launch before #993 that wrote one did.
    pub renderer_on: bool,
}

pub fn format_marker(m: &Marker) -> String {
    let mut out = format!("pid={}\nstarted={}\n", m.pid, m.started);
    if let Some(boot) = &m.boot_id {
        out.push_str(&format!("boot={boot}\n"));
    }
    out.push_str(if m.renderer_on {
        "renderer=on\n"
    } else {
        "renderer=off\n"
    });
    out
}

pub fn parse_marker(text: &str) -> Option<Marker> {
    let mut pid = None;
    let mut started = None;
    let mut boot_id = None;
    let mut renderer_on = true;
    for line in text.lines() {
        match line.split_once('=') {
            Some(("pid", v)) => pid = v.trim().parse().ok(),
            Some(("started", v)) => started = v.trim().parse().ok(),
            Some(("boot", v)) if !v.trim().is_empty() => boot_id = Some(v.trim().to_string()),
            Some(("renderer", v)) => renderer_on = v.trim() != "off",
            _ => {}
        }
    }
    Some(Marker {
        pid: pid.filter(|p| *p != 0)?,
        started: started?,
        boot_id,
        renderer_on,
    })
}

/// What the marker left by the previous launch says about it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PreviousLaunch {
    /// No marker: the last launch reached a loaded window, or quit cleanly.
    Clean,
    /// Its process is gone and the window never finished loading.
    Crashed {
        /// It had the DMA-BUF renderer on. An early exit with the renderer
        /// off cannot be the renderer's doing and is never counted.
        renderer_on: bool,
    },
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
    PreviousLaunch::Crashed {
        renderer_on: marker.renderer_on,
    }
}

/// The fallback the crash guard saved.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Fallback {
    /// Unix seconds when it was saved. For whoever reads the file.
    pub since: u64,
    /// Launches under it that loaded and stayed up.
    pub clean_runs: u32,
    /// The WebKitGTK library and NVIDIA driver it was saved under.
    pub fingerprint: Option<String>,
}

/// Everything the crash guard keeps between launches, in one file.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct GuardState {
    /// Early exits in a row, each with the renderer on.
    pub early_exits: u32,
    /// The renderer is being tried again after a saved fallback.
    pub retrying: bool,
    /// Retries in a row that ended in the fallback again.
    pub failed_retries: u32,
    pub fallback: Option<Fallback>,
}

pub fn format_state(s: &GuardState) -> String {
    let mut out = format!(
        "early_exits={}\nretrying={}\nfailed_retries={}\n",
        s.early_exits,
        u8::from(s.retrying),
        s.failed_retries
    );
    if let Some(f) = &s.fallback {
        out.push_str(&format!(
            "fallback_since={}\nfallback_clean_runs={}\n",
            f.since, f.clean_runs
        ));
        if let Some(fp) = &f.fingerprint {
            out.push_str(&format!("fallback_fingerprint={}\n", one_line(fp)));
        }
    }
    out
}

/// `Err` names the first line that does not parse. Unknown keys are skipped,
/// so a later version can add one without an older one calling it corrupt.
pub fn parse_state(text: &str) -> Result<GuardState, String> {
    fn num<T: std::str::FromStr>(key: &str, v: &str) -> Result<T, String> {
        v.trim()
            .parse()
            .map_err(|_| format!("{key}={} is not a number", v.trim()))
    }
    let mut s = GuardState::default();
    let mut since = None;
    let mut clean_runs = 0;
    let mut fingerprint = None;
    for line in text.lines().map(str::trim).filter(|l| !l.is_empty()) {
        let Some((key, v)) = line.split_once('=') else {
            return Err(format!("\"{line}\" is not key=value"));
        };
        match key.trim() {
            "early_exits" => s.early_exits = num(key, v)?,
            "failed_retries" => s.failed_retries = num(key, v)?,
            "retrying" => {
                s.retrying = match v.trim() {
                    "0" => false,
                    "1" => true,
                    other => return Err(format!("retrying={other} is not 0 or 1")),
                }
            }
            "fallback_since" => since = Some(num(key, v)?),
            "fallback_clean_runs" => clean_runs = num(key, v)?,
            "fallback_fingerprint" => {
                fingerprint = Some(v.trim().to_string()).filter(|f| !f.is_empty())
            }
            _ => {}
        }
    }
    s.fallback = since.map(|since| Fallback {
        since,
        clean_runs,
        fingerprint,
    });
    Ok(s)
}

/// The state file's text read for this launch: `Err` carries why it was
/// unreadable, and the guard then starts again from nothing.
pub fn load_state(text: Option<&str>) -> Result<GuardState, String> {
    text.map_or(Ok(GuardState::default()), parse_state)
}

/// Clean launches a saved fallback waits for, after this many failed retries.
pub fn retry_after(failed_retries: u32) -> u32 {
    RETRY_AFTER_CLEAN_RUNS << failed_retries.min(MAX_RETRY_DOUBLINGS)
}

fn one_line(s: &str) -> String {
    s.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// What a fallback is tied to: the WebKitGTK library file the process mapped
/// (its name carries the version, as `libwebkit2gtk-4.1.so.0.13.5`) and the
/// NVIDIA driver's version line. `None` when neither can be read. Mesa is
/// left out: it is loaded only later, when WebKit asks for a GPU.
pub fn fingerprint_from(proc_maps: Option<&str>, nvidia_version: Option<&str>) -> Option<String> {
    let webkit = proc_maps.and_then(|maps| {
        maps.lines().find_map(|line| {
            let path = line.split_whitespace().nth(5)?;
            let name = path.rsplit('/').next()?;
            name.starts_with("libwebkit2gtk").then(|| name.to_string())
        })
    });
    let nvidia = nvidia_version
        .and_then(|t| t.lines().next())
        .map(one_line)
        .filter(|l| !l.is_empty());
    let parts: Vec<String> = [("webkit", webkit), ("nvidia", nvidia)]
        .into_iter()
        .filter_map(|(k, v)| v.map(|v| format!("{k}={v}")))
        .collect();
    (!parts.is_empty()).then(|| parts.join("; "))
}

pub struct Inputs<'a> {
    pub webkit_var: Option<&'a str>,
    pub mode: Option<&'a str>,
    pub wayland_display: Option<&'a str>,
    pub session_type: Option<&'a str>,
    pub nvidia_driver: bool,
    pub state: &'a GuardState,
    pub previous: PreviousLaunch,
    pub fingerprint: Option<&'a str>,
    /// Unix seconds.
    pub now: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Reason {
    UserSetWebkitVar,
    ForcedOn,
    ForcedOff,
    SavedFallback,
    EarlyExitsInARow,
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
            Reason::SavedFallback => "renderer off: saved after earlier launches closed early",
            Reason::EarlyExitsInARow => {
                "renderer off: the last launches closed before their window loaded"
            }
            Reason::WaylandNvidia => "renderer off: Wayland session with the NVIDIA driver",
            Reason::NoRuleMatched => "renderer left on: no rule matched",
        }
    }
}

/// One thing the crash guard noticed or changed, for the log.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum GuardEvent {
    /// The previous launch closed early with the renderer on.
    EarlyExitCounted {
        count: u32,
    },
    /// The previous launch closed early with the renderer already off.
    EarlyExitNotCounted,
    FallbackSaved {
        retry_after: u32,
        /// Saved again by the early exit of a retry, not by a fresh row.
        after_retry: bool,
    },
    RetryAfterCleanRuns {
        clean_runs: u32,
    },
    RetryAfterChange {
        was: String,
        now: String,
    },
    /// `STEMDECK_WEBKIT_DMABUF=on` dropped what the guard had saved.
    Forgotten,
}

impl GuardEvent {
    pub fn describe(&self) -> String {
        match self {
            GuardEvent::EarlyExitCounted { count } => {
                let mut line = format!(
                    "the previous launch closed before its window loaded, with the DMA-BUF \
                     renderer on ({count} of {EARLY_EXITS_TO_SAVE} in a row before it is \
                     turned off)"
                );
                if *count < EARLY_EXITS_TO_SAVE {
                    line.push_str(
                        "; one early exit can be anything, so the renderer is tried again",
                    );
                }
                line
            }
            GuardEvent::EarlyExitNotCounted => "the previous launch closed before its window \
                 loaded, with the DMA-BUF renderer already off; not counted against it"
                .to_string(),
            GuardEvent::FallbackSaved {
                retry_after,
                after_retry,
            } => {
                let why = if *after_retry {
                    "WebKit's DMA-BUF renderer was tried again and StemDeck closed before its \
                     window loaded"
                        .to_string()
                } else {
                    format!(
                        "StemDeck closed before its window loaded on {EARLY_EXITS_TO_SAVE} \
                         launches in a row with WebKit's DMA-BUF renderer on"
                    )
                };
                format!(
                    "{why}, so the renderer is now off. It is tried again after {retry_after} \
                     launches that load cleanly, or when WebKitGTK or the NVIDIA driver \
                     changes. To turn it back on now, start StemDeck once with \
                     STEMDECK_WEBKIT_DMABUF=on."
                )
            }
            GuardEvent::RetryAfterCleanRuns { clean_runs } => format!(
                "saved DMA-BUF fallback dropped after {clean_runs} clean launches; the \
                 renderer is tried again, and one early exit turns it back off"
            ),
            GuardEvent::RetryAfterChange { was, now } => format!(
                "saved DMA-BUF fallback dropped: the graphics stack changed ({was} -> {now}); \
                 the renderer is tried again, and one early exit turns it back off"
            ),
            GuardEvent::Forgotten => {
                "saved DMA-BUF crash guard state cleared by STEMDECK_WEBKIT_DMABUF=on".to_string()
            }
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
    /// The guard's state after this launch's decision, to persist.
    pub state: GuardState,
    /// What the guard noticed or changed, each a log line.
    pub events: Vec<GuardEvent>,
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
        state: i.state.clone(),
        events: Vec::new(),
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
            let forget = *i.state != GuardState::default();
            return Decision {
                reason: Reason::ForcedOn,
                state: GuardState::default(),
                events: if forget {
                    vec![GuardEvent::Forgotten]
                } else {
                    Vec::new()
                },
                ..base
            };
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

    let mut s = i.state.clone();
    let mut events = Vec::new();
    match i.previous {
        PreviousLaunch::Crashed { renderer_on: true } => {
            s.early_exits = s.early_exits.saturating_add(1);
            events.push(GuardEvent::EarlyExitCounted {
                count: s.early_exits,
            });
        }
        PreviousLaunch::Crashed { renderer_on: false } => {
            events.push(GuardEvent::EarlyExitNotCounted);
        }
        _ => {}
    }

    if let Some(fb) = &s.fallback {
        let changed = match (fb.fingerprint.as_deref(), i.fingerprint) {
            (Some(was), Some(now)) if was != now => Some((was.to_string(), now.to_string())),
            _ => None,
        };
        let due = fb.clean_runs >= retry_after(s.failed_retries);
        if changed.is_none() && !due {
            return Decision {
                disable_dmabuf: true,
                reason: Reason::SavedFallback,
                arm_guard: true,
                state: s,
                events,
                ..base
            };
        }
        events.push(match changed {
            Some((was, now)) => GuardEvent::RetryAfterChange { was, now },
            None => GuardEvent::RetryAfterCleanRuns {
                clean_runs: fb.clean_runs,
            },
        });
        // One step short of the fallback: the evidence that saved it
        // still stands, so one more early exit puts it back.
        s.fallback = None;
        s.retrying = true;
        s.early_exits = EARLY_EXITS_TO_SAVE - 1;
    } else if s.early_exits >= EARLY_EXITS_TO_SAVE {
        let after_retry = s.retrying;
        if after_retry {
            s.failed_retries = s.failed_retries.saturating_add(1);
        }
        s.retrying = false;
        s.early_exits = 0;
        s.fallback = Some(Fallback {
            since: i.now,
            clean_runs: 0,
            fingerprint: i.fingerprint.map(str::to_string),
        });
        events.push(GuardEvent::FallbackSaved {
            retry_after: retry_after(s.failed_retries),
            after_retry,
        });
        return Decision {
            disable_dmabuf: true,
            reason: Reason::EarlyExitsInARow,
            arm_guard: true,
            state: s,
            events,
            ..base
        };
    }

    let armed = Decision {
        arm_guard: true,
        state: s,
        events,
        ..base
    };
    if i.nvidia_driver && is_wayland(i.wayland_display, i.session_type) {
        return Decision {
            disable_dmabuf: true,
            reason: Reason::WaylandNvidia,
            ..armed
        };
    }
    armed
}

/// The guard's state once a launch has loaded its window and stayed up.
/// With the renderer on, that clears every count against it. With it off,
/// it says nothing about the renderer, and only moves a saved fallback one
/// clean launch closer to its retry.
pub fn after_clean_run(state: &GuardState, renderer_on: bool) -> GuardState {
    let mut s = state.clone();
    if renderer_on {
        s.early_exits = 0;
        s.retrying = false;
        s.failed_retries = 0;
    } else if let Some(fb) = &mut s.fallback {
        fb.clean_runs = fb.clean_runs.saturating_add(1);
    }
    s
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

/// Writes the guard's state, or removes the file when there is nothing to
/// keep, so a machine the guard never had to act on carries no file.
pub fn store_state(dir: &Path, name: &str, state: &GuardState) -> io::Result<()> {
    if *state == GuardState::default() {
        match fs::remove_file(dir.join(name)) {
            Err(e) if e.kind() != io::ErrorKind::NotFound => Err(e),
            _ => Ok(()),
        }
    } else {
        write_atomic(dir, name, &format_state(state))
    }
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

/// The crash guard's counts and saved fallback.
pub const STATE_FILE: &str = "webkit-dmabuf-guard";
/// The fallback as test builds before #993 saved it, after one early exit.
/// Never read: one early exit is not enough, so it is dropped.
pub const LEGACY_FALLBACK_FILE: &str = "webkit-dmabuf-off";

/// The pid in the name of a temp file `write_atomic` leaves when its process
/// dies between create and rename: `.launch-pending.<pid>.tmp` or
/// `.webkit-dmabuf-guard.<pid>.tmp`. `None` for every other name.
pub fn stale_tmp_pid(file_name: &str) -> Option<u32> {
    let inner = file_name.strip_prefix('.')?.strip_suffix(".tmp")?;
    let (base, pid) = inner.rsplit_once('.')?;
    if base != MARKER_FILE && base != STATE_FILE {
        return None;
    }
    // Exactly what write_atomic writes: ASCII digits, no sign, no leading
    // zero. `parse` alone would also take "+7" and "007".
    if pid.is_empty() || pid.starts_with('0') || !pid.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    pid.parse().ok()
}

/// Removes the temp files of processes that are gone. A live pid's file is
/// left: that process may be between create and rename right now.
pub fn sweep_stale_tmp(dir: &Path, own_pid: u32, pid_alive: &dyn Fn(u32) -> bool) {
    let Ok(entries) = fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let name = entry.file_name();
        let Some(pid) = name.to_str().and_then(stale_tmp_pid) else {
            continue;
        };
        if pid != own_pid && !pid_alive(pid) {
            let _ = fs::remove_file(entry.path());
        }
    }
}

/// How reading the state file went, which decides what may be written back.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StateRead {
    /// Read and parsed, or missing (no state yet): write back on a change.
    Read,
    /// Its content is not a state file (does not parse, not UTF-8): it can
    /// never be read, so it is replaced.
    Corrupt,
    /// The file could not be read at all (permissions, an I/O error). It may
    /// hold a saved fallback that a later launch can read, so it is left as
    /// it is: this launch decides without it and writes nothing.
    Unreadable,
}

impl StateRead {
    /// The state should be written, given whether the decision changed it.
    pub fn write_back(self, changed: bool) -> bool {
        match self {
            StateRead::Read => changed,
            StateRead::Corrupt => true,
            StateRead::Unreadable => false,
        }
    }
}

/// What the result of reading the state file means for this launch.
pub fn interpret_state_read(
    result: io::Result<String>,
    log: &mut dyn FnMut(&str),
) -> (GuardState, StateRead) {
    let text = match result {
        Ok(t) => Some(t),
        Err(e) if e.kind() == io::ErrorKind::NotFound => None,
        Err(e) if e.kind() == io::ErrorKind::InvalidData => {
            log(&format!(
                "DMA-BUF crash guard state is not text ({e}); starting over"
            ));
            return (GuardState::default(), StateRead::Corrupt);
        }
        Err(e) => {
            log(&format!(
                "DMA-BUF crash guard state could not be read ({e}); deciding without \
                 it this launch and leaving the file as it is"
            ));
            return (GuardState::default(), StateRead::Unreadable);
        }
    };
    match load_state(text.as_deref()) {
        Ok(s) => (s, StateRead::Read),
        Err(why) => {
            log(&format!(
                "DMA-BUF crash guard state unreadable ({why}); starting over"
            ));
            (GuardState::default(), StateRead::Corrupt)
        }
    }
}

/// Reads the state file; see [`interpret_state_read`].
pub fn read_state_file(dir: &Path, log: &mut dyn FnMut(&str)) -> (GuardState, StateRead) {
    interpret_state_read(fs::read_to_string(dir.join(STATE_FILE)), log)
}

fn save_state_file(dir: &Path, state: &GuardState, log: &mut dyn FnMut(&str)) {
    if let Err(e) = store_state(dir, STATE_FILE, state) {
        log(&format!(
            "could not save the DMA-BUF crash guard state: {e}"
        ));
    }
}

/// What the shell reads from the process and the system for one launch.
pub struct Host<'a> {
    pub webkit_var: Option<&'a str>,
    pub mode: Option<&'a str>,
    pub wayland_display: Option<&'a str>,
    pub session_type: Option<&'a str>,
    /// `/proc/driver/nvidia/version`, present only while the NVIDIA kernel
    /// driver is loaded; nouveau never creates it.
    pub nvidia_version: Option<&'a str>,
    /// `/proc/self/maps`.
    pub proc_maps: Option<&'a str>,
    pub boot_id: Option<&'a str>,
    pub own_pid: u32,
    /// Unix seconds.
    pub now: u64,
}

/// Everything a launch does with the guard's files, before GTK starts:
/// read and consume the previous launch's marker, read the state, decide,
/// log and save. The caller applies the decision to the environment.
/// `dir` is the data dir, `None` when it could not be found.
pub fn startup(
    dir: Option<&Path>,
    host: &Host,
    pid_alive: &dyn Fn(u32) -> bool,
    log: &mut dyn FnMut(&str),
) -> Decision {
    if let Some(d) = dir {
        sweep_stale_tmp(d, host.own_pid, pid_alive);
    }
    let marker = dir.and_then(|d| fs::read_to_string(d.join(MARKER_FILE)).ok());
    let previous = classify_previous(marker.as_deref(), host.boot_id, host.own_pid, pid_alive);
    if let Some(d) = dir {
        // Consumed here, read once: a launch that never reaches the window
        // (no display, an early setup error) writes no marker of its own,
        // and the next launch must not count the same early exit again.
        // Another instance's live marker is left for it to clear.
        if marker.is_some() && previous != PreviousLaunch::StillRunning {
            let _ = fs::remove_file(d.join(MARKER_FILE));
        }
    }
    let (state, read) = match dir {
        Some(d) => read_state_file(d, log),
        None => (GuardState::default(), StateRead::Read),
    };
    let fingerprint = fingerprint_from(host.proc_maps, host.nvidia_version);
    let decision = decide(&Inputs {
        webkit_var: host.webkit_var,
        mode: host.mode,
        wayland_display: host.wayland_display,
        session_type: host.session_type,
        nvidia_driver: host.nvidia_version.is_some(),
        state: &state,
        previous,
        fingerprint: fingerprint.as_deref(),
        now: host.now,
    });
    if let Some(raw) = &decision.invalid_mode {
        log(&format!(
            "STEMDECK_WEBKIT_DMABUF={raw} is not auto, on or off; using auto"
        ));
    }
    if let Some(d) = dir {
        if fs::remove_file(d.join(LEGACY_FALLBACK_FILE)).is_ok() {
            log(
                "dropped a DMA-BUF fallback saved after a single early exit; \
                 the crash guard now needs two in a row",
            );
        }
        for event in &decision.events {
            log(&event.describe());
        }
        if read.write_back(decision.state != state) {
            save_state_file(d, &decision.state, log);
        }
    }
    log(&format!("webkit dmabuf: {}", decision.reason.describe()));
    decision
}

/// The marker for this launch, written just before the window is built.
pub fn write_launch_marker(
    dir: &Path,
    own_pid: u32,
    boot_id: Option<&str>,
    renderer_on: bool,
    now: u64,
) -> io::Result<()> {
    let marker = Marker {
        pid: own_pid,
        started: now,
        boot_id: boot_id.map(str::to_string),
        renderer_on,
    };
    write_atomic(dir, MARKER_FILE, &format_marker(&marker))
}

/// The window loaded and stayed up: clear this launch's marker, then credit
/// the launch in the guard's state.
pub fn record_clean_run(dir: &Path, own_pid: u32, renderer_on: bool, log: &mut dyn FnMut(&str)) {
    clear_marker_if_ours(dir, own_pid);
    let (state, read) = read_state_file(dir, log);
    let next = after_clean_run(&state, renderer_on);
    if !read.write_back(next != state) {
        return;
    }
    if renderer_on && state.early_exits > 0 {
        log("launch loaded cleanly with the DMA-BUF renderer on; early-exit count reset");
    }
    if let Some(fb) = &next.fallback {
        log(&format!(
            "launch loaded cleanly under the saved DMA-BUF fallback ({} of {} \
             before the renderer is tried again)",
            fb.clean_runs,
            retry_after(next.failed_retries)
        ));
    }
    save_state_file(dir, &next, log);
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
    /// a crash is not the renderer at startup and must not count.
    const READY_GRACE: Duration = Duration::from_secs(5);

    /// WebKitGTK's own switch. Read and set, never overridden once present.
    const WEBKIT_VAR: &str = "WEBKIT_DISABLE_DMABUF_RENDERER";
    /// StemDeck's override: `auto` (default), `on` or `off`.
    const MODE_VAR: &str = "STEMDECK_WEBKIT_DMABUF";

    struct Armed {
        dir: PathBuf,
        renderer_on: bool,
    }

    /// Set when this launch armed the guard.
    static ARMED: OnceLock<Armed> = OnceLock::new();

    fn log(data_dir: Option<&Path>, msg: &str) {
        eprintln!("[stemdeck] {msg}");
        if let Some(d) = data_dir {
            crate::append_to_setup_log(d, msg);
        }
    }

    fn var(name: &str) -> Option<String> {
        env::var_os(name).map(|v| v.to_string_lossy().into_owned())
    }

    fn now() -> u64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|t| t.as_secs())
            .unwrap_or(0)
    }

    fn boot_id() -> Option<String> {
        fs::read_to_string("/proc/sys/kernel/random/boot_id")
            .ok()
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
    }

    fn pid_alive(pid: u32) -> bool {
        Path::new(&format!("/proc/{pid}")).exists()
    }

    /// Decide and apply, before GTK, WebKit or any other thread exists.
    pub fn apply_at_startup() {
        let data_dir = crate::local_data_dir().ok();
        let dir = data_dir.as_deref();
        let nvidia_version = fs::read_to_string("/proc/driver/nvidia/version").ok();
        // The WebKitGTK library is linked in, so it is mapped before main.
        let maps = fs::read_to_string("/proc/self/maps").ok();
        let boot = boot_id();
        let webkit_var = var(WEBKIT_VAR);
        let mode = var(MODE_VAR);
        let wayland_display = var("WAYLAND_DISPLAY");
        let session_type = var("XDG_SESSION_TYPE");
        let host = Host {
            webkit_var: webkit_var.as_deref(),
            mode: mode.as_deref(),
            wayland_display: wayland_display.as_deref(),
            session_type: session_type.as_deref(),
            nvidia_version: nvidia_version.as_deref(),
            proc_maps: maps.as_deref(),
            boot_id: boot.as_deref(),
            own_pid: std::process::id(),
            now: now(),
        };
        let decision = startup(dir, &host, &pid_alive, &mut |m| log(dir, m));
        if decision.disable_dmabuf {
            // Edition 2021: set_var is safe to call, but it is only sound
            // while no other thread reads the environment. This runs first
            // thing in main, before Tauri, GTK or WebKit start a thread.
            env::set_var(WEBKIT_VAR, "1");
        }
        if let (Some(d), true) = (dir, decision.arm_guard) {
            let _ = ARMED.set(Armed {
                dir: d.to_path_buf(),
                renderer_on: !decision.disable_dmabuf,
            });
        }
    }

    /// Writes the launch marker and clears it once the first page has loaded.
    /// Called with the main window's builder, after GTK is up, so a launch
    /// that fails earlier (no display at all) is never read as a crash.
    pub fn watch_first_load<'a, R: tauri::Runtime, M: tauri::Manager<R>>(
        builder: tauri::WebviewWindowBuilder<'a, R, M>,
    ) -> tauri::WebviewWindowBuilder<'a, R, M> {
        let Some(armed) = ARMED.get() else {
            return builder;
        };
        let own_pid = std::process::id();
        let boot = boot_id();
        if let Err(e) = write_launch_marker(
            &armed.dir,
            own_pid,
            boot.as_deref(),
            armed.renderer_on,
            now(),
        ) {
            // Never a reason not to start: the guard is just off this time.
            log(Some(&armed.dir), &format!("launch marker not written: {e}"));
            return builder;
        }
        let started = AtomicBool::new(false);
        builder.on_page_load(move |_window, payload| {
            if matches!(payload.event(), tauri::webview::PageLoadEvent::Finished)
                && !started.swap(true, Ordering::SeqCst)
            {
                std::thread::spawn(move || {
                    std::thread::sleep(READY_GRACE);
                    record_clean_run(&armed.dir, own_pid, armed.renderer_on, &mut |m| {
                        log(Some(&armed.dir), m)
                    });
                });
            }
        })
    }

    /// An orderly quit, however early, is never an early exit. It is not a
    /// clean run either: it says nothing about whether the renderer holds.
    pub fn clear_on_exit() {
        if let Some(armed) = ARMED.get() {
            clear_marker_if_ours(&armed.dir, std::process::id());
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const NO_STATE: GuardState = GuardState {
        early_exits: 0,
        retrying: false,
        failed_retries: 0,
        fallback: None,
    };

    fn inputs() -> Inputs<'static> {
        Inputs {
            webkit_var: None,
            mode: None,
            wayland_display: None,
            session_type: None,
            nvidia_driver: false,
            state: &NO_STATE,
            previous: PreviousLaunch::Clean,
            fingerprint: Some("webkit=libwebkit2gtk-4.1.so.0.13.5"),
            now: 1_700_000_000,
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

    const CRASH_ON: PreviousLaunch = PreviousLaunch::Crashed { renderer_on: true };
    const CRASH_OFF: PreviousLaunch = PreviousLaunch::Crashed { renderer_on: false };

    fn saved(clean_runs: u32, failed_retries: u32) -> GuardState {
        GuardState {
            failed_retries,
            fallback: Some(Fallback {
                since: 1,
                clean_runs,
                fingerprint: Some("webkit=libwebkit2gtk-4.1.so.0.13.5".into()),
            }),
            ..GuardState::default()
        }
    }

    /// One launch's decision, from the state the launch before it left and
    /// how that launch ended. Chained with `after_clean_run` to play out a
    /// row of launches the way the shell runs them.
    fn launch(state: &GuardState, previous: PreviousLaunch) -> Decision {
        decide(&Inputs {
            state,
            previous,
            ..inputs()
        })
    }

    #[test]
    fn wayland_with_nvidia_turns_the_renderer_off() {
        let d = decide(&wayland_nvidia());
        assert!(d.disable_dmabuf);
        assert_eq!(d.reason, Reason::WaylandNvidia);
        assert!(d.arm_guard);
        assert_eq!(d.state, GuardState::default());
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
        let state = saved(0, 0);
        for webkit in ["1", "0", ""] {
            let d = decide(&Inputs {
                webkit_var: Some(webkit),
                mode: Some("off"),
                state: &state,
                previous: CRASH_ON,
                ..wayland_nvidia()
            });
            assert!(!d.disable_dmabuf);
            assert_eq!(d.reason, Reason::UserSetWebkitVar);
            assert!(!d.arm_guard);
            assert_eq!(d.state, state, "nothing saved or forgotten");
            assert!(d.events.is_empty());
        }
    }

    #[test]
    fn mode_on_keeps_the_renderer_and_forgets_everything_saved() {
        let state = GuardState {
            early_exits: 1,
            retrying: true,
            ..saved(3, 2)
        };
        let d = decide(&Inputs {
            mode: Some("ON"),
            state: &state,
            previous: CRASH_ON,
            ..wayland_nvidia()
        });
        assert!(!d.disable_dmabuf);
        assert_eq!(d.reason, Reason::ForcedOn);
        assert!(!d.arm_guard);
        assert_eq!(d.state, GuardState::default());
        assert_eq!(d.events, vec![GuardEvent::Forgotten]);
        let none_saved = decide(&Inputs {
            mode: Some("on"),
            ..inputs()
        });
        assert!(none_saved.events.is_empty());
    }

    #[test]
    fn mode_off_turns_it_off_anywhere_and_saves_nothing() {
        let d = decide(&Inputs {
            mode: Some(" off "),
            previous: CRASH_ON,
            ..inputs()
        });
        assert!(d.disable_dmabuf);
        assert_eq!(d.reason, Reason::ForcedOff);
        assert!(!d.arm_guard);
        assert_eq!(d.state, GuardState::default());
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
    fn one_unrelated_early_exit_does_not_save_the_fallback() {
        let d = launch(&GuardState::default(), CRASH_ON);
        assert!(!d.disable_dmabuf, "the renderer is tried again");
        assert_eq!(d.reason, Reason::NoRuleMatched);
        assert!(d.arm_guard);
        assert_eq!(d.state.early_exits, 1);
        assert_eq!(d.state.fallback, None);
        assert_eq!(d.events, vec![GuardEvent::EarlyExitCounted { count: 1 }]);
        // That launch then loads cleanly: the count is gone.
        assert_eq!(after_clean_run(&d.state, true), GuardState::default());
    }

    #[test]
    fn two_early_exits_in_a_row_save_the_fallback() {
        let first = launch(&GuardState::default(), CRASH_ON);
        let second = launch(&first.state, CRASH_ON);
        assert!(second.disable_dmabuf);
        assert_eq!(second.reason, Reason::EarlyExitsInARow);
        assert!(second.arm_guard);
        let fb = second.state.fallback.clone().expect("fallback saved");
        assert_eq!(fb.since, 1_700_000_000);
        assert_eq!(fb.clean_runs, 0);
        assert_eq!(
            fb.fingerprint.as_deref(),
            Some("webkit=libwebkit2gtk-4.1.so.0.13.5")
        );
        assert_eq!(second.state.early_exits, 0);
        assert_eq!(
            second.events,
            vec![
                GuardEvent::EarlyExitCounted { count: 2 },
                GuardEvent::FallbackSaved {
                    retry_after: RETRY_AFTER_CLEAN_RUNS,
                    after_retry: false,
                },
            ]
        );
        assert!(!second.events[0].describe().contains("tried again"));
        // And it holds on the launch after.
        let third = launch(&second.state, PreviousLaunch::Clean);
        assert!(third.disable_dmabuf);
        assert_eq!(third.reason, Reason::SavedFallback);
    }

    #[test]
    fn early_exits_with_the_renderer_off_are_not_counted() {
        let d = launch(&GuardState::default(), CRASH_OFF);
        assert_eq!(d.state.early_exits, 0);
        assert_eq!(d.events, vec![GuardEvent::EarlyExitNotCounted]);
        let one = GuardState {
            early_exits: 1,
            ..GuardState::default()
        };
        assert_eq!(launch(&one, CRASH_OFF).state.fallback, None);
    }

    #[test]
    fn a_clean_run_in_between_breaks_the_row() {
        let first = launch(&GuardState::default(), CRASH_ON);
        let cleared = after_clean_run(&first.state, true);
        let again = launch(&cleared, CRASH_ON);
        assert_eq!(again.state.fallback, None);
        assert_eq!(again.state.early_exits, 1);
    }

    #[test]
    fn a_clean_run_with_the_renderer_off_says_nothing_about_it() {
        let one = GuardState {
            early_exits: 1,
            ..GuardState::default()
        };
        assert_eq!(after_clean_run(&one, false), one);
    }

    #[test]
    fn clean_runs_under_the_fallback_bring_the_renderer_back() {
        let mut state = saved(0, 0);
        for n in 1..RETRY_AFTER_CLEAN_RUNS {
            let d = launch(&state, PreviousLaunch::Clean);
            assert_eq!(d.reason, Reason::SavedFallback, "launch {n}");
            state = after_clean_run(&d.state, !d.disable_dmabuf);
        }
        let held = launch(&state, PreviousLaunch::Clean);
        assert_eq!(held.reason, Reason::SavedFallback);
        state = after_clean_run(&held.state, false);
        assert_eq!(
            state.fallback.as_ref().map(|f| f.clean_runs),
            Some(RETRY_AFTER_CLEAN_RUNS)
        );

        let retry = launch(&state, PreviousLaunch::Clean);
        assert!(!retry.disable_dmabuf, "back on the fast path");
        assert_eq!(retry.reason, Reason::NoRuleMatched);
        assert_eq!(retry.state.fallback, None);
        assert!(retry.state.retrying);
        assert_eq!(
            retry.events,
            vec![GuardEvent::RetryAfterCleanRuns {
                clean_runs: RETRY_AFTER_CLEAN_RUNS
            }]
        );
        // The retry loads cleanly: every count against the renderer goes.
        assert_eq!(after_clean_run(&retry.state, true), GuardState::default());
    }

    #[test]
    fn a_failed_retry_puts_the_fallback_back_and_waits_longer() {
        let retry = launch(&saved(RETRY_AFTER_CLEAN_RUNS, 0), PreviousLaunch::Clean);
        let after = launch(&retry.state, CRASH_ON);
        assert!(after.disable_dmabuf);
        assert_eq!(after.reason, Reason::EarlyExitsInARow);
        assert_eq!(after.state.failed_retries, 1);
        assert!(!after.state.retrying);
        assert!(after.events.contains(&GuardEvent::FallbackSaved {
            retry_after: RETRY_AFTER_CLEAN_RUNS * 2,
            after_retry: true,
        }));
        assert_eq!(retry_after(0), 10);
        assert_eq!(retry_after(1), 20);
        assert_eq!(retry_after(3), 80);
        assert_eq!(retry_after(u32::MAX), 80, "capped");
    }

    #[test]
    fn a_changed_graphics_stack_brings_the_renderer_back() {
        let state = saved(0, 2);
        let d = decide(&Inputs {
            state: &state,
            fingerprint: Some("webkit=libwebkit2gtk-4.1.so.0.14.0"),
            ..inputs()
        });
        assert!(!d.disable_dmabuf);
        assert_eq!(d.state.fallback, None);
        assert!(matches!(
            d.events.as_slice(),
            [GuardEvent::RetryAfterChange { .. }]
        ));
        // A fingerprint that cannot be read now is not a change.
        let unknown = decide(&Inputs {
            state: &state,
            fingerprint: None,
            ..inputs()
        });
        assert_eq!(unknown.reason, Reason::SavedFallback);
    }

    #[test]
    fn the_wayland_rule_still_applies_while_retrying() {
        let state = saved(RETRY_AFTER_CLEAN_RUNS, 0);
        let d = decide(&Inputs {
            state: &state,
            ..wayland_nvidia()
        });
        assert!(d.disable_dmabuf);
        assert_eq!(d.reason, Reason::WaylandNvidia);
    }

    #[test]
    fn a_live_or_stale_previous_launch_changes_nothing() {
        for previous in [PreviousLaunch::StillRunning, PreviousLaunch::Stale] {
            let d = launch(&GuardState::default(), previous);
            assert!(!d.disable_dmabuf);
            assert_eq!(d.state, GuardState::default());
            assert!(d.events.is_empty());
        }
    }

    #[test]
    fn every_reason_and_event_has_a_log_line() {
        for r in [
            Reason::UserSetWebkitVar,
            Reason::ForcedOn,
            Reason::ForcedOff,
            Reason::SavedFallback,
            Reason::EarlyExitsInARow,
            Reason::WaylandNvidia,
            Reason::NoRuleMatched,
        ] {
            assert!(r.describe().is_ascii() && !r.describe().is_empty());
        }
        for e in [
            GuardEvent::EarlyExitCounted { count: 1 },
            GuardEvent::EarlyExitNotCounted,
            GuardEvent::EarlyExitCounted { count: 2 },
            GuardEvent::FallbackSaved {
                retry_after: 10,
                after_retry: false,
            },
            GuardEvent::FallbackSaved {
                retry_after: 20,
                after_retry: true,
            },
            GuardEvent::RetryAfterCleanRuns { clean_runs: 10 },
            GuardEvent::RetryAfterChange {
                was: "a".into(),
                now: "b".into(),
            },
            GuardEvent::Forgotten,
        ] {
            let line = e.describe();
            assert!(line.is_ascii() && !line.is_empty() && !line.contains('\n'));
            assert!(!line.contains("  "), "no stray indentation: {line}");
        }
    }

    #[test]
    fn state_round_trips() {
        for s in [
            GuardState::default(),
            GuardState {
                early_exits: 1,
                retrying: true,
                failed_retries: 2,
                fallback: None,
            },
            saved(4, 1),
            GuardState {
                fallback: Some(Fallback {
                    since: 9,
                    clean_runs: 0,
                    fingerprint: None,
                }),
                ..GuardState::default()
            },
        ] {
            assert_eq!(parse_state(&format_state(&s)), Ok(s));
        }
    }

    #[test]
    fn a_corrupt_state_file_is_reported_not_trusted() {
        for text in [
            "garbage",
            "early_exits=two\n",
            "retrying=yes\n",
            "fallback_since=-1\n",
            "fallback_clean_runs=\n",
        ] {
            assert!(load_state(Some(text)).is_err(), "{text:?}");
        }
        // No file, an empty one, and keys a later version added are fine.
        assert_eq!(load_state(None), Ok(GuardState::default()));
        assert_eq!(load_state(Some("")), Ok(GuardState::default()));
        assert_eq!(
            load_state(Some("early_exits=1\nnew_key=x\n")),
            Ok(GuardState {
                early_exits: 1,
                ..GuardState::default()
            })
        );
        // Counts without a saved fallback never invent one.
        assert_eq!(
            load_state(Some("fallback_clean_runs=3\n")).map(|s| s.fallback),
            Ok(None)
        );
    }

    #[test]
    fn a_fingerprint_with_a_newline_cannot_break_the_file() {
        let s = GuardState {
            fallback: Some(Fallback {
                since: 1,
                clean_runs: 0,
                fingerprint: Some("a\nearly_exits=9".into()),
            }),
            ..GuardState::default()
        };
        let back = parse_state(&format_state(&s)).unwrap();
        assert_eq!(back.early_exits, 0);
    }

    #[test]
    fn the_fingerprint_names_webkit_and_the_nvidia_driver() {
        let maps = "7f00-7f01 r-xp 00000000 08:01 123 /usr/lib/x86_64-linux-gnu/libc.so.6\n\
                    7f02-7f03 r-xp 00000000 08:01 456 \
                    /usr/lib/x86_64-linux-gnu/libwebkit2gtk-4.1.so.0.13.5\n\
                    7f04-7f05 rw-p 00000000 00:00 0\n";
        let nvidia = "NVRM version: NVIDIA UNIX x86_64 Kernel Module  550.54.14  Thu Feb 22\n\
                      GCC version: gcc 13\n";
        assert_eq!(
            fingerprint_from(Some(maps), Some(nvidia)).as_deref(),
            Some(
                "webkit=libwebkit2gtk-4.1.so.0.13.5; \
                 nvidia=NVRM version: NVIDIA UNIX x86_64 Kernel Module 550.54.14 Thu Feb 22"
            )
        );
        assert_eq!(
            fingerprint_from(Some(maps), None).as_deref(),
            Some("webkit=libwebkit2gtk-4.1.so.0.13.5")
        );
        assert_eq!(fingerprint_from(Some("no libraries here"), None), None);
        assert_eq!(fingerprint_from(None, None), None);
    }

    #[test]
    fn the_state_file_goes_away_when_there_is_nothing_to_keep() {
        let tmp = tempfile::tempdir().unwrap();
        let state = saved(1, 0);
        store_state(tmp.path(), "guard", &state).unwrap();
        let text = fs::read_to_string(tmp.path().join("guard")).unwrap();
        assert_eq!(parse_state(&text), Ok(state));
        store_state(tmp.path(), "guard", &GuardState::default()).unwrap();
        assert!(!tmp.path().join("guard").exists());
        // Removing a file that is already gone is not an error.
        store_state(tmp.path(), "guard", &GuardState::default()).unwrap();
        assert_eq!(fs::read_dir(tmp.path()).unwrap().count(), 0);
    }

    fn marker(pid: u32, boot: Option<&str>) -> String {
        format_marker(&Marker {
            pid,
            started: 1_700_000_000,
            boot_id: boot.map(str::to_string),
            renderer_on: true,
        })
    }

    #[test]
    fn marker_round_trips() {
        for renderer_on in [true, false] {
            let m = Marker {
                pid: 42,
                started: 7,
                boot_id: Some("abc".into()),
                renderer_on,
            };
            assert_eq!(parse_marker(&format_marker(&m)), Some(m));
        }
        assert_eq!(parse_marker("pid=0\nstarted=1\n"), None);
        assert_eq!(parse_marker("garbage"), None);
        // A marker from before #993 has no renderer line: counted as on.
        assert!(parse_marker("pid=5\nstarted=1\n").unwrap().renderer_on);
    }

    #[test]
    fn marker_with_a_dead_pid_is_a_crash() {
        let text = marker(4242, Some("boot-a"));
        let got = classify_previous(Some(&text), Some("boot-a"), 1, |_| false);
        assert_eq!(got, CRASH_ON);
        // No boot id on either side: still decided by the pid.
        let text = marker(4242, None);
        assert_eq!(classify_previous(Some(&text), None, 1, |_| false), CRASH_ON);
        let off = format_marker(&Marker {
            pid: 4242,
            started: 1,
            boot_id: None,
            renderer_on: false,
        });
        assert_eq!(classify_previous(Some(&off), None, 1, |_| false), CRASH_OFF);
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
        assert!(store_state(&dir, "guard", &saved(0, 0)).is_err());
        assert!(!clear_marker_if_ours(&dir, 5));
        assert_eq!(
            classify_previous(None, None, 5, |_| false),
            PreviousLaunch::Clean
        );
    }

    // Through the file layer: `startup`, `write_launch_marker`,
    // `record_clean_run` and `clear_marker_if_ours`, in a temp data dir, in
    // the order the shell calls them across launches.

    fn host(own_pid: u32) -> Host<'static> {
        Host {
            webkit_var: None,
            mode: None,
            wayland_display: None,
            session_type: None,
            nvidia_version: None,
            proc_maps: None,
            boot_id: Some("boot-a"),
            own_pid,
            now: 1_700_000_000,
        }
    }

    fn dead(_: u32) -> bool {
        false
    }

    /// One launch's startup, with its log lines.
    fn start(dir: &Path, own_pid: u32, alive: &dyn Fn(u32) -> bool) -> (Decision, Vec<String>) {
        let mut lines = Vec::new();
        let d = startup(Some(dir), &host(own_pid), alive, &mut |m| {
            lines.push(m.to_string())
        });
        (d, lines)
    }

    /// What the shell does once the window is about to be built.
    fn reach_window(dir: &Path, own_pid: u32, d: &Decision) {
        assert!(d.arm_guard);
        write_launch_marker(dir, own_pid, Some("boot-a"), !d.disable_dmabuf, 1).unwrap();
    }

    fn state_on_disk(dir: &Path) -> GuardState {
        load_state(fs::read_to_string(dir.join(STATE_FILE)).ok().as_deref()).unwrap()
    }

    #[test]
    fn one_early_exit_counts_once_when_the_next_launch_never_reaches_the_window() {
        let tmp = tempfile::tempdir().unwrap();
        let dir = tmp.path();
        write_launch_marker(dir, 4242, Some("boot-a"), true, 1).unwrap();

        // Launch 2 counts the early exit, then fails before its window (no
        // display, an early setup error) and writes no marker of its own.
        let (second, _) = start(dir, 100, &dead);
        assert_eq!(second.state.early_exits, 1);
        assert!(!dir.join(MARKER_FILE).exists(), "the marker is consumed");

        // Launch 3 must not read the same early exit a second time.
        let (third, _) = start(dir, 101, &dead);
        assert_eq!(third.state.early_exits, 1);
        assert_eq!(third.state.fallback, None);
        assert!(!third.disable_dmabuf);
        assert_eq!(state_on_disk(dir).early_exits, 1);
    }

    #[test]
    fn two_real_early_exits_save_the_fallback_and_clean_runs_count_under_it() {
        let tmp = tempfile::tempdir().unwrap();
        let dir = tmp.path();
        write_launch_marker(dir, 4242, Some("boot-a"), true, 1).unwrap();

        let (second, _) = start(dir, 100, &dead);
        reach_window(dir, 100, &second);
        // Launch 2 dies early too, with the renderer on.
        let (third, log) = start(dir, 101, &dead);
        assert!(third.disable_dmabuf);
        assert_eq!(third.reason, Reason::EarlyExitsInARow);
        assert!(log.iter().any(|l| l.contains("is now off")), "{log:?}");
        assert!(state_on_disk(dir).fallback.is_some());

        // Launch 3 loads and stays up under the fallback.
        reach_window(dir, 101, &third);
        let mut lines = Vec::new();
        record_clean_run(dir, 101, false, &mut |m| lines.push(m.to_string()));
        assert!(!dir.join(MARKER_FILE).exists());
        let fb = state_on_disk(dir).fallback.unwrap();
        assert_eq!(fb.clean_runs, 1);
        assert!(lines.iter().any(|l| l.contains("1 of 10")), "{lines:?}");
    }

    #[test]
    fn a_clean_run_clears_the_marker_and_the_count() {
        let tmp = tempfile::tempdir().unwrap();
        let dir = tmp.path();
        write_launch_marker(dir, 4242, Some("boot-a"), true, 1).unwrap();
        let (d, _) = start(dir, 100, &dead);
        assert_eq!(state_on_disk(dir).early_exits, 1);
        reach_window(dir, 100, &d);

        let mut lines = Vec::new();
        record_clean_run(dir, 100, true, &mut |m| lines.push(m.to_string()));
        assert!(!dir.join(MARKER_FILE).exists());
        assert!(!dir.join(STATE_FILE).exists(), "nothing left to keep");
        assert!(lines.iter().any(|l| l.contains("count reset")), "{lines:?}");

        // A clean run with nothing to change writes and logs nothing.
        let mut quiet = Vec::new();
        record_clean_run(dir, 100, true, &mut |m| quiet.push(m.to_string()));
        assert!(quiet.is_empty());
        assert_eq!(fs::read_dir(dir).unwrap().count(), 0);
    }

    #[test]
    fn a_clean_run_leaves_another_instances_marker() {
        let tmp = tempfile::tempdir().unwrap();
        let dir = tmp.path();
        write_launch_marker(dir, 200, Some("boot-a"), true, 1).unwrap();
        record_clean_run(dir, 100, true, &mut |_| {});
        assert_eq!(
            parse_marker(&fs::read_to_string(dir.join(MARKER_FILE)).unwrap()).map(|m| m.pid),
            Some(200)
        );
    }

    #[test]
    fn an_orderly_early_quit_is_not_counted_and_resets_nothing() {
        let tmp = tempfile::tempdir().unwrap();
        let dir = tmp.path();
        write_launch_marker(dir, 4242, Some("boot-a"), true, 1).unwrap();
        let (d, _) = start(dir, 100, &dead);
        reach_window(dir, 100, &d);
        // Quit before the 5 seconds were up: the marker goes, the count stays.
        assert!(clear_marker_if_ours(dir, 100));
        let (next, _) = start(dir, 101, &dead);
        assert_eq!(next.state.early_exits, 1);
        assert_eq!(next.state.fallback, None);
    }

    #[test]
    fn a_live_instances_marker_is_left_alone() {
        let tmp = tempfile::tempdir().unwrap();
        let dir = tmp.path();
        write_launch_marker(dir, 4242, Some("boot-a"), true, 1).unwrap();
        let (d, _) = start(dir, 100, &|p| p == 4242);
        assert_eq!(d.state.early_exits, 0);
        assert!(dir.join(MARKER_FILE).exists());
    }

    #[test]
    fn a_state_file_that_cannot_be_read_is_logged_and_left_alone() {
        let tmp = tempfile::tempdir().unwrap();
        let dir = tmp.path();
        // A directory where the file should be: reading it fails with an I/O
        // error, not NotFound and not bad content, on every platform and
        // without depending on permissions or root.
        fs::create_dir(dir.join(STATE_FILE)).unwrap();
        fs::write(dir.join(STATE_FILE).join("kept"), "").unwrap();
        write_launch_marker(dir, 4242, Some("boot-a"), true, 1).unwrap();

        let (d, log) = start(dir, 100, &dead);
        // The launch goes on, deciding without the state.
        assert!(d.arm_guard);
        assert_eq!(d.state.early_exits, 1);
        assert!(
            log.iter().any(|l| l.contains("could not be read")),
            "{log:?}"
        );
        // Nothing is written over it or deleted.
        assert!(!log.iter().any(|l| l.contains("could not save")), "{log:?}");
        assert!(dir.join(STATE_FILE).join("kept").exists());

        // The clean-run write stands back too.
        reach_window(dir, 100, &d);
        let mut lines = Vec::new();
        record_clean_run(dir, 100, true, &mut |m| lines.push(m.to_string()));
        assert!(
            !lines.iter().any(|l| l.contains("could not save")),
            "{lines:?}"
        );
        assert!(dir.join(STATE_FILE).join("kept").exists());
    }

    #[test]
    fn a_read_error_never_writes_back_and_bad_content_always_does() {
        let mut quiet = |_: &str| {};
        for kind in [
            io::ErrorKind::PermissionDenied,
            io::ErrorKind::Interrupted,
            io::ErrorKind::Other,
        ] {
            let (s, read) = interpret_state_read(Err(io::Error::from(kind)), &mut quiet);
            assert_eq!(read, StateRead::Unreadable, "{kind:?}");
            assert_eq!(s, GuardState::default());
            assert!(!read.write_back(true) && !read.write_back(false));
        }
        let (_, missing) =
            interpret_state_read(Err(io::Error::from(io::ErrorKind::NotFound)), &mut quiet);
        assert_eq!(missing, StateRead::Read);
        assert!(missing.write_back(true) && !missing.write_back(false));
        let (_, not_text) =
            interpret_state_read(Err(io::Error::from(io::ErrorKind::InvalidData)), &mut quiet);
        assert_eq!(not_text, StateRead::Corrupt);
        let (_, bad) = interpret_state_read(Ok("early_exits=x".into()), &mut quiet);
        assert_eq!(bad, StateRead::Corrupt);
        assert!(bad.write_back(false));
    }

    #[test]
    fn a_saved_fallback_is_not_rewritten_by_a_launch_that_changes_nothing() {
        let tmp = tempfile::tempdir().unwrap();
        let dir = tmp.path();
        store_state(dir, STATE_FILE, &saved(3, 1)).unwrap();
        let before = fs::read(dir.join(STATE_FILE)).unwrap();
        // The previous launch closed early under the fallback: not counted.
        write_launch_marker(dir, 4242, Some("boot-a"), false, 1).unwrap();
        let (d, _) = start(dir, 100, &dead);
        assert_eq!(d.reason, Reason::SavedFallback);
        assert_eq!(fs::read(dir.join(STATE_FILE)).unwrap(), before);
    }

    #[test]
    fn a_crashed_marker_is_consumed_whatever_the_override() {
        for (webkit_var, mode) in [
            (Some("1"), None),
            (Some(""), Some("on")),
            (None, Some("off")),
            (None, Some("on")),
        ] {
            let tmp = tempfile::tempdir().unwrap();
            let dir = tmp.path();
            write_launch_marker(dir, 4242, Some("boot-a"), true, 1).unwrap();
            let d = startup(
                Some(dir),
                &Host {
                    webkit_var,
                    mode,
                    ..host(100)
                },
                &dead,
                &mut |_| {},
            );
            assert!(!d.arm_guard, "{webkit_var:?} {mode:?}");
            assert!(
                !dir.join(MARKER_FILE).exists(),
                "{webkit_var:?} {mode:?}: marker left behind"
            );
        }
    }

    #[test]
    fn a_stale_marker_is_consumed() {
        let other_boot = format_marker(&Marker {
            pid: 4242,
            started: 1,
            boot_id: Some("boot-b".into()),
            renderer_on: true,
        });
        let own_pid = format_marker(&Marker {
            pid: 100,
            started: 1,
            boot_id: None,
            renderer_on: true,
        });
        for text in [other_boot.as_str(), own_pid.as_str(), "half a fi"] {
            let tmp = tempfile::tempdir().unwrap();
            let dir = tmp.path();
            fs::write(dir.join(MARKER_FILE), text).unwrap();
            let (d, _) = start(dir, 100, &dead);
            assert_eq!(d.state.early_exits, 0, "{text:?}");
            assert!(
                !dir.join(MARKER_FILE).exists(),
                "{text:?}: marker left behind"
            );
        }
    }

    #[test]
    fn a_corrupt_state_file_is_logged_and_replaced() {
        let tmp = tempfile::tempdir().unwrap();
        let dir = tmp.path();
        fs::write(dir.join(STATE_FILE), "early_exits=lots\n").unwrap();
        let (d, log) = start(dir, 100, &dead);
        assert_eq!(d.state, GuardState::default());
        assert!(log.iter().any(|l| l.contains("unreadable")), "{log:?}");
        assert!(!dir.join(STATE_FILE).exists(), "replaced by no state");
    }

    #[test]
    fn stale_temp_files_are_swept_at_startup() {
        let tmp = tempfile::tempdir().unwrap();
        let dir = tmp.path();
        for name in [
            ".launch-pending.4242.tmp",
            ".webkit-dmabuf-guard.4243.tmp",
            ".launch-pending.4244.tmp",
            ".webkit-dmabuf-guard.100.tmp",
            ".other.4242.tmp",
            "notes.txt",
        ] {
            fs::write(dir.join(name), "").unwrap();
        }
        start(dir, 100, &|p| p == 4244);
        let mut left: Vec<String> = fs::read_dir(dir)
            .unwrap()
            .map(|e| e.unwrap().file_name().into_string().unwrap())
            .collect();
        left.sort();
        assert_eq!(
            left,
            vec![
                ".launch-pending.4244.tmp",
                ".other.4242.tmp",
                ".webkit-dmabuf-guard.100.tmp",
                "notes.txt",
            ]
        );
        assert_eq!(stale_tmp_pid(".launch-pending.7.tmp"), Some(7));
        assert_eq!(stale_tmp_pid(".launch-pending.0.tmp"), None);
        assert_eq!(stale_tmp_pid(".launch-pending.x.tmp"), None);
        for odd in ["+7", "007", "-7", " 7", "7 ", "", "4294967296"] {
            assert_eq!(
                stale_tmp_pid(&format!(".launch-pending.{odd}.tmp")),
                None,
                "{odd:?}"
            );
        }
        assert_eq!(
            stale_tmp_pid(".webkit-dmabuf-guard.4294967295.tmp"),
            Some(u32::MAX)
        );
        assert_eq!(stale_tmp_pid("launch-pending.7.tmp"), None);
    }

    #[test]
    fn a_legacy_fallback_file_is_dropped_and_logged() {
        let tmp = tempfile::tempdir().unwrap();
        let dir = tmp.path();
        fs::write(dir.join(LEGACY_FALLBACK_FILE), "since=1\n").unwrap();
        let (d, log) = start(dir, 100, &dead);
        assert!(!d.disable_dmabuf);
        assert!(!dir.join(LEGACY_FALLBACK_FILE).exists());
        assert!(log.iter().any(|l| l.contains("two in a row")), "{log:?}");
    }

    #[test]
    fn no_data_dir_still_decides_and_logs() {
        let mut lines = Vec::new();
        let d = startup(None, &host(100), &dead, &mut |m| lines.push(m.to_string()));
        assert!(d.arm_guard);
        assert_eq!(lines.len(), 1);
        assert!(lines[0].starts_with("webkit dmabuf: "));
    }
}
