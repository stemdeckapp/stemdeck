//! Finding REAPER, for "Open in REAPER" and the External editors setting.
//!
//! A path the user chose in Settings wins while it still exists. Otherwise
//! REAPER is looked for where its installers put it: on Windows the folder
//! the installer records in the registry (HKLM, then HKCU, `SOFTWARE\REAPER`)
//! and the default Program Files folders; on macOS the Applications folders;
//! on Linux PATH and the folders REAPER's install script offers.
//!
//! The pure parts (parsing `reg query`, choosing between a chosen and a
//! detected path, the launch command) are separate so they can be tested on
//! every platform.

use std::ffi::OsString;
use std::path::{Path, PathBuf};

/// Store key holding the REAPER the user chose. Absent = detect.
pub const REAPER_PATH_KEY: &str = "reaper_path";

/// Where the REAPER that will be used came from, for the Settings row.
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Source {
    /// The user chose it in Settings.
    Chosen,
    /// Found where an installer puts it.
    Detected,
    /// Not found anywhere.
    None,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct Status {
    /// The REAPER that "Open in REAPER" launches, if any.
    pub path: Option<String>,
    pub source: Source,
    /// A REAPER chosen in Settings that is no longer there (moved or
    /// uninstalled). Shown so the user knows why their choice is not used.
    pub chosen_missing: Option<String>,
}

/// Is `p` something REAPER can be launched from: the executable, or on
/// macOS the .app bundle.
pub fn usable(p: &Path) -> bool {
    if cfg!(target_os = "macos") {
        p.is_dir() && p.extension().is_some_and(|e| e == "app") || p.is_file()
    } else {
        p.is_file()
    }
}

/// Decides between a chosen path and detection. `detect` only runs when
/// there is no usable chosen path.
pub fn resolve(chosen: Option<&str>, detect: impl FnOnce() -> Option<PathBuf>) -> Status {
    let chosen = chosen.map(str::trim).filter(|s| !s.is_empty());
    if let Some(c) = chosen {
        if usable(Path::new(c)) {
            return Status {
                path: Some(c.to_string()),
                source: Source::Chosen,
                chosen_missing: None,
            };
        }
    }
    let chosen_missing = chosen.map(str::to_string);
    match detect() {
        Some(p) => Status {
            path: Some(p.to_string_lossy().into_owned()),
            source: Source::Detected,
            chosen_missing,
        },
        None => Status {
            path: None,
            source: Source::None,
            chosen_missing,
        },
    }
}

/// The folder in `reg query <key> /ve` output: the line holding the default
/// value, `    (Default)    REG_SZ    C:\Program Files\REAPER (x64)`. The
/// "(Default)" label is localised, so the line is found by its type.
pub fn parse_reg_default(output: &str) -> Option<PathBuf> {
    output.lines().find_map(|line| {
        let (_, rest) = line.split_once("REG_SZ")?;
        let dir = rest.trim();
        (!dir.is_empty()).then(|| PathBuf::from(dir))
    })
}

/// The places to look, in order, given the registry's answers and the
/// environment. Separate from the filesystem checks so it can be tested.
#[cfg_attr(not(windows), allow(dead_code))]
pub fn windows_candidates(
    registry_dirs: &[PathBuf],
    program_files: &[PathBuf],
    local_app_data: Option<&Path>,
) -> Vec<PathBuf> {
    let mut out: Vec<PathBuf> = registry_dirs.iter().map(|d| d.join("reaper.exe")).collect();
    for pf in program_files {
        out.push(pf.join("REAPER (x64)").join("reaper.exe"));
        out.push(pf.join("REAPER").join("reaper.exe"));
    }
    if let Some(local) = local_app_data {
        out.push(local.join("Programs").join("REAPER").join("reaper.exe"));
    }
    out
}

/// The system's own reg.exe, not whatever `reg` a PATH search would find first.
#[cfg(windows)]
fn reg_exe() -> PathBuf {
    let root = std::env::var_os("SystemRoot").unwrap_or_else(|| OsString::from(r"C:\Windows"));
    PathBuf::from(root).join("System32").join("reg.exe")
}

#[cfg(windows)]
fn registry_dirs() -> Vec<PathBuf> {
    ["HKLM\\SOFTWARE\\REAPER", "HKCU\\SOFTWARE\\REAPER"]
        .iter()
        .filter_map(|key| {
            let out = crate::childproc::command(reg_exe())
                .args(["query", key, "/ve"])
                .output()
                .ok()?;
            if !out.status.success() {
                return None;
            }
            parse_reg_default(&String::from_utf8_lossy(&out.stdout))
        })
        .collect()
}

/// REAPER where an installer puts it, if it is there.
pub fn detect() -> Option<PathBuf> {
    candidates().into_iter().find(|p| usable(p))
}

#[cfg(windows)]
fn candidates() -> Vec<PathBuf> {
    let program_files: Vec<PathBuf> = ["ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"]
        .iter()
        .filter_map(std::env::var_os)
        .map(PathBuf::from)
        .collect();
    let local = std::env::var_os("LOCALAPPDATA").map(PathBuf::from);
    windows_candidates(&registry_dirs(), &program_files, local.as_deref())
}

#[cfg(target_os = "macos")]
fn candidates() -> Vec<PathBuf> {
    let mut dirs = vec![PathBuf::from("/Applications")];
    if let Some(home) = std::env::var_os("HOME") {
        dirs.push(PathBuf::from(home).join("Applications"));
    }
    dirs.iter()
        .flat_map(|d| ["REAPER.app", "REAPER64.app"].map(|n| d.join(n)))
        .collect()
}

#[cfg(all(unix, not(target_os = "macos")))]
fn candidates() -> Vec<PathBuf> {
    let mut out: Vec<PathBuf> = std::env::var_os("PATH")
        .map(|p| {
            std::env::split_paths(&p)
                .map(|d| d.join("reaper"))
                .collect()
        })
        .unwrap_or_default();
    if let Some(home) = std::env::var_os("HOME") {
        out.push(PathBuf::from(home).join("opt/REAPER/reaper"));
    }
    out.push(PathBuf::from("/opt/REAPER/reaper"));
    out
}

/// The program and arguments that open `project` in the REAPER at `reaper`.
/// Arguments, never a shell string, so no part of either path is read as a
/// command line.
pub fn launch_command(reaper: &Path, project: &Path) -> (OsString, Vec<OsString>) {
    if cfg!(target_os = "macos") && reaper.extension().is_some_and(|e| e == "app") {
        (
            OsString::from("open"),
            vec![
                OsString::from("-a"),
                reaper.as_os_str().to_owned(),
                project.as_os_str().to_owned(),
            ],
        )
    } else {
        (
            reaper.as_os_str().to_owned(),
            vec![project.as_os_str().to_owned()],
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reg_output_gives_the_install_folder() {
        let out = "\r\nHKEY_LOCAL_MACHINE\\SOFTWARE\\REAPER\r\n    (Default)    REG_SZ    C:\\Program Files\\REAPER (x64)\r\n\r\n";
        assert_eq!(
            parse_reg_default(out),
            Some(PathBuf::from("C:\\Program Files\\REAPER (x64)"))
        );
        // Localised label, same type.
        let de = "    (Standard)    REG_SZ    D:\\Audio\\REAPER\r\n";
        assert_eq!(
            parse_reg_default(de),
            Some(PathBuf::from("D:\\Audio\\REAPER"))
        );
        assert_eq!(parse_reg_default("    (Default)    REG_SZ    \r\n"), None);
        assert_eq!(parse_reg_default("ERROR: not found"), None);
    }

    #[test]
    fn windows_candidates_put_the_registry_first() {
        let got = windows_candidates(
            &[PathBuf::from("D:/R")],
            &[PathBuf::from("C:/PF")],
            Some(Path::new("C:/L")),
        );
        assert_eq!(
            got,
            vec![
                PathBuf::from("D:/R").join("reaper.exe"),
                PathBuf::from("C:/PF")
                    .join("REAPER (x64)")
                    .join("reaper.exe"),
                PathBuf::from("C:/PF").join("REAPER").join("reaper.exe"),
                PathBuf::from("C:/L")
                    .join("Programs")
                    .join("REAPER")
                    .join("reaper.exe"),
            ]
        );
    }

    #[test]
    fn a_chosen_reaper_wins_while_it_exists() {
        let dir = tempfile::TempDir::new().unwrap();
        let exe = dir.path().join("reaper.exe");
        std::fs::write(&exe, b"").unwrap();
        let s = resolve(Some(exe.to_str().unwrap()), || panic!("must not detect"));
        assert_eq!(s.source, Source::Chosen);
        assert_eq!(s.path.as_deref(), exe.to_str());
        assert_eq!(s.chosen_missing, None);
    }

    #[test]
    fn a_missing_choice_falls_back_to_detection_and_says_so() {
        let found = PathBuf::from("/found/reaper");
        let s = resolve(Some("/gone/reaper"), || Some(found.clone()));
        assert_eq!(s.source, Source::Detected);
        assert_eq!(s.path, Some(found.to_string_lossy().into_owned()));
        assert_eq!(s.chosen_missing.as_deref(), Some("/gone/reaper"));

        let s = resolve(None, || None);
        assert_eq!(s.source, Source::None);
        assert_eq!(s.path, None);
        assert_eq!(s.chosen_missing, None);
        // A blank stored value is no choice at all.
        assert_eq!(resolve(Some("  "), || None).chosen_missing, None);
    }

    #[test]
    fn launch_passes_both_paths_as_single_arguments() {
        let reaper = PathBuf::from("/x/a b & c/reaper");
        let project = PathBuf::from("/p/song \"1\"; rm.rpp");
        let (program, args) = launch_command(&reaper, &project);
        assert_eq!(program, reaper.as_os_str());
        assert_eq!(args, vec![project.as_os_str().to_owned()]);
    }
}
