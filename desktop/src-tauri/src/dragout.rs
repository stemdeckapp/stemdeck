//! Dragging audio out of StemDeck and into a DAW or a folder.
//!
//! Every export today is three actions: pick a format, click Export, choose a
//! folder. For someone auditioning loops against a project that is the whole
//! session, repeated. Dragging is one action, and it is the gesture a DAW user
//! already has in their hands.
//!
//! A WebView cannot produce an OS drag. HTML5 `dragstart` can carry text and,
//! in Chromium only, a `DownloadURL` that some file managers accept -- but a
//! DAW wants a real path on a real filesystem, and no browser will give it one.
//! So the gesture is cancelled in JavaScript and handed to the platform here.
//!
//! ## Why not tauri-plugin-drag
//!
//! The plugin exists and works, but it exposes `start_drag` only as a
//! `#[command]`, which means **JavaScript names the file path**. That breaks
//! the rule the rest of StemDeck's exports are built on: in `download_to_path`
//! the destination is held in Rust behind an opaque token precisely so nothing
//! running in the WebView can write an arbitrary URL to an arbitrary location.
//! The page is served over http by the Python backend, which Tauri treats as a
//! remote origin, so app-defined commands are not ACL-gated and that rule is
//! the only thing holding.
//!
//! Here the same rule holds: JavaScript passes a localhost URL and a bare
//! filename. The directory is resolved in Rust, the filename is reduced to a
//! single path component, and the resulting path never crosses the IPC
//! boundary in either direction.
//!
//! ## Why the file has to persist
//!
//! DAWs disagree about what a dropped file means. Reaper references it where
//! it lies; others copy it into the project. So a dragged file cannot live in
//! the render cache, which is pruned at 20 files / 500 MB and would eventually
//! delete audio somebody's project still points at. It goes to an exports
//! folder that nothing cleans up, beside the stems folder, and the user can
//! move it in Settings.

use std::path::{Path, PathBuf};

use drag::{DragItem, DragMode, DragResult, Image, Options};

/// The fallback drag preview: StemDeck's own icon.
///
/// Embedded rather than read from disk, because the icon's path differs between
/// a dev run, the portable zip and an installed bundle, and a drag with no
/// preview looks broken. Used for the mix and whenever a lane's own icon is
/// unavailable.
const DRAG_IMAGE: &[u8] = include_bytes!("../icons/drag.png");

/// Ceiling on a caller-supplied preview. It is a small label on a plate; a
/// megabyte of one is a mistake or an attempt at one, and the app icon is a
/// better answer than either.
const MAX_ICON_BYTES: usize = 512 * 1024;

/// Decode the preview the UI rendered for this lane, or fall back.
///
/// The picture in flight should say which track is coming, and the UI is where
/// the lane's name and colour already live, so it draws the label to a canvas
/// and sends the PNG rather than this module keeping a second set of assets
/// that would drift from the mixer.
///
/// Nothing here is trusted: a malformed, empty or oversized payload silently
/// becomes the app icon, because a drag carrying the wrong picture is still a
/// working drag and refusing one would cost the user the gesture.
fn decode_icon(icon: Option<String>) -> Vec<u8> {
    use base64::Engine;

    let Some(encoded) = icon else {
        return DRAG_IMAGE.to_vec();
    };
    match base64::engine::general_purpose::STANDARD.decode(encoded.as_bytes()) {
        Ok(bytes) if !bytes.is_empty() && bytes.len() <= MAX_ICON_BYTES => bytes,
        Ok(bytes) => {
            log_icon_rejected(bytes.len());
            DRAG_IMAGE.to_vec()
        }
        Err(_) => {
            log_icon_rejected(0);
            DRAG_IMAGE.to_vec()
        }
    }
}

fn log_icon_rejected(len: usize) {
    eprintln!("[stemdeck] ignoring a drag preview of {len} bytes; using the app icon");
}

/// Characters no Windows filename may contain, plus the separators every
/// platform uses. `:` also matters on macOS, where Finder still shows it as a
/// path separator.
const ILLEGAL: &[char] = &['<', '>', ':', '"', '|', '?', '*', '/', '\\'];

/// Device names Windows resolves before it ever looks at the directory, with
/// or without an extension. `CON.wav` is not a file.
const RESERVED: &[&str] = &[
    "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8",
    "COM9", "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
];

/// Reduce a caller-supplied name to a single, safe path component.
///
/// Rejects rather than repairs anything that looks like an attempt to escape
/// the exports folder, because a caller asking to write `../../autoexec` is
/// not a caller whose intent is worth guessing at. Cosmetic problems (illegal
/// characters, a trailing dot) are repaired, since those come from song titles
/// and rejecting them would make ordinary tracks undraggable.
pub fn sanitize_filename(name: &str) -> Result<String, String> {
    let trimmed = name.trim();
    if trimmed.is_empty() {
        return Err("empty filename".to_string());
    }
    if trimmed == "." || trimmed == ".." || trimmed.contains("..") {
        return Err("filename may not traverse directories".to_string());
    }
    if trimmed.contains('/') || trimmed.contains('\\') {
        return Err("filename may not contain a path separator".to_string());
    }

    let cleaned: String = trimmed
        .chars()
        .map(|c| {
            if ILLEGAL.contains(&c) || c.is_control() {
                '_'
            } else {
                c
            }
        })
        .collect();

    // Windows silently strips these, so a name ending in one resolves to a
    // different file than the one reported back to the user.
    let cleaned = cleaned.trim_end_matches(['.', ' ']).to_string();
    if cleaned.is_empty() {
        return Err("filename is empty once cleaned".to_string());
    }

    let stem = cleaned.split('.').next().unwrap_or("").to_ascii_uppercase();
    if RESERVED.contains(&stem.as_str()) {
        return Err(format!("{cleaned} is a reserved device name"));
    }

    // Long titles plus a region suffix can pass NTFS's 255-byte component
    // limit. Truncate the stem, never the extension, or the DAW cannot tell
    // what it was handed.
    Ok(truncate_component(&cleaned, 200))
}

/// Shorten a filename to `max` bytes while keeping its extension intact.
fn truncate_component(name: &str, max: usize) -> String {
    if name.len() <= max {
        return name.to_string();
    }
    let (stem, ext) = match name.rfind('.') {
        Some(i) if i > 0 => (&name[..i], &name[i..]),
        _ => (name, ""),
    };
    let room = max.saturating_sub(ext.len());
    let mut cut = room.min(stem.len());
    // Never split a multi-byte character.
    while cut > 0 && !stem.is_char_boundary(cut) {
        cut -= 1;
    }
    format!("{}{}", &stem[..cut], ext)
}

/// Where dragged files land.
///
/// `configured` is whatever Settings recorded, which may be stale: a folder on
/// a drive that is no longer mounted must not silently swallow exports, so an
/// absolute path that does not exist and cannot be created falls back rather
/// than failing the drag. A relative path is ignored outright, because it
/// would resolve against the process working directory, which is not
/// somewhere the user chose.
pub fn resolve_exports_dir(configured: Option<&str>, fallback: &Path) -> PathBuf {
    if let Some(raw) = configured {
        let candidate = PathBuf::from(raw.trim());
        if candidate.is_absolute() && std::fs::create_dir_all(&candidate).is_ok() {
            return candidate;
        }
    }
    fallback.to_path_buf()
}

/// The default exports folder: a sibling of the stems folder, so everything
/// StemDeck writes for the user sits in one place they already know about.
pub fn default_exports_dir(jobs_dir: &Path, data_dir: &Path) -> PathBuf {
    match jobs_dir.parent() {
        Some(parent) if parent.as_os_str() != "" => parent.join("exports"),
        _ => data_dir.join("exports"),
    }
}

/// Hand `path` to the platform's drag-and-drop, from the main thread.
///
/// Must be called on the main thread: all three backends talk to UI toolkit
/// state (OLE on Windows, AppKit on macOS, GTK on Linux) that is not
/// thread-safe. The caller is responsible for that; see `start_audio_drag`.
pub fn begin_drag(
    window: &tauri::WebviewWindow,
    path: PathBuf,
    icon: Option<String>,
) -> Result<(), String> {
    let options = Options {
        // Copy, never Move: the exports folder is the user's copy and a DAW
        // must not relocate it out from under them.
        mode: DragMode::Copy,
        skip_animatation_on_cancel_or_failure: false,
    };
    let item = DragItem::Files(vec![path]);
    let image = Image::Raw(decode_icon(icon));
    let on_drop = |result: DragResult, _: drag::CursorPosition| {
        // Nothing to undo either way. The file stays in the exports folder
        // whether it was dropped or the drag was abandoned, which is what a
        // user who drags twice expects.
        if matches!(result, DragResult::Cancel) {
            eprintln!("[stemdeck] drag cancelled");
        }
    };

    #[cfg(target_os = "linux")]
    {
        let gtk = window.gtk_window().map_err(|e| e.to_string())?;
        drag::start_drag(&gtk, item, image, on_drop, options).map_err(|e| e.to_string())
    }
    #[cfg(not(target_os = "linux"))]
    {
        drag::start_drag(&window.clone(), item, image, on_drop, options).map_err(|e| e.to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keeps_an_ordinary_name() {
        assert_eq!(
            sanitize_filename("Song - vocals.wav").unwrap(),
            "Song - vocals.wav"
        );
    }

    #[test]
    fn rejects_traversal() {
        assert!(sanitize_filename("../secret.wav").is_err());
        assert!(sanitize_filename("..").is_err());
        assert!(sanitize_filename("a/../../b.wav").is_err());
    }

    #[test]
    fn rejects_separators() {
        assert!(sanitize_filename("sub/dir.wav").is_err());
        assert!(sanitize_filename("sub\\dir.wav").is_err());
    }

    #[test]
    fn rejects_empty() {
        assert!(sanitize_filename("").is_err());
        assert!(sanitize_filename("   ").is_err());
        assert!(sanitize_filename("...").is_err());
    }

    #[test]
    fn repairs_illegal_characters() {
        assert_eq!(
            sanitize_filename("AC*DC: Back?.wav").unwrap(),
            "AC_DC_ Back_.wav"
        );
    }

    #[test]
    fn rejects_windows_device_names() {
        assert!(sanitize_filename("CON.wav").is_err());
        assert!(sanitize_filename("nul.mp3").is_err());
        assert!(sanitize_filename("LPT1").is_err());
        // Only the exact device name, not anything starting with it.
        assert!(sanitize_filename("CONCERT.wav").is_ok());
    }

    #[test]
    fn strips_trailing_dots_and_spaces() {
        assert_eq!(sanitize_filename("track.wav . ").unwrap(), "track.wav");
    }

    #[test]
    fn truncates_a_long_name_but_keeps_the_extension() {
        let long = format!("{}.wav", "a".repeat(400));
        let got = sanitize_filename(&long).unwrap();
        assert!(got.len() <= 200, "{}", got.len());
        assert!(got.ends_with(".wav"));
    }

    #[test]
    fn truncation_does_not_split_a_character() {
        let long = format!("{}.wav", "é".repeat(300));
        let got = sanitize_filename(&long).unwrap();
        assert!(got.ends_with(".wav"));
        // Round-trips, so no partial code unit survived.
        assert_eq!(got, String::from_utf8(got.clone().into_bytes()).unwrap());
    }

    #[test]
    fn no_icon_falls_back_to_the_app_icon() {
        assert_eq!(decode_icon(None), DRAG_IMAGE);
    }

    #[test]
    fn a_valid_icon_is_decoded() {
        use base64::Engine;
        // Contents are never inspected here; the platform decides whether
        // it is a usable image and tolerates one that is not.
        let png = b"pretend this is a PNG";
        let encoded = base64::engine::general_purpose::STANDARD.encode(png);
        assert_eq!(decode_icon(Some(encoded)), png);
    }

    #[test]
    fn a_broken_icon_falls_back_rather_than_failing_the_drag() {
        // Wrong picture beats no drag.
        assert_eq!(decode_icon(Some("not base64 at all!!".into())), DRAG_IMAGE);
        assert_eq!(decode_icon(Some(String::new())), DRAG_IMAGE);
    }

    #[test]
    fn an_oversized_icon_falls_back() {
        use base64::Engine;
        let huge = vec![0u8; MAX_ICON_BYTES + 1];
        let encoded = base64::engine::general_purpose::STANDARD.encode(&huge);
        assert_eq!(decode_icon(Some(encoded)), DRAG_IMAGE);
    }

    #[test]
    fn exports_dir_prefers_a_usable_configured_path() {
        let dir = tempfile::tempdir().unwrap();
        let chosen = dir.path().join("elsewhere");
        let got = resolve_exports_dir(Some(chosen.to_str().unwrap()), dir.path());
        assert_eq!(got, chosen);
        assert!(chosen.is_dir(), "resolving should create it");
    }

    #[test]
    fn exports_dir_falls_back_when_unset_or_relative() {
        let dir = tempfile::tempdir().unwrap();
        assert_eq!(resolve_exports_dir(None, dir.path()), dir.path());
        // Relative would resolve against the working directory, not a choice.
        assert_eq!(resolve_exports_dir(Some("exports"), dir.path()), dir.path());
    }

    #[test]
    fn default_exports_dir_sits_beside_the_stems_folder() {
        let jobs = PathBuf::from("/home/u/Documents/StemDeck/jobs");
        let data = PathBuf::from("/home/u/.local/share/stemdeck");
        assert_eq!(
            default_exports_dir(&jobs, &data),
            PathBuf::from("/home/u/Documents/StemDeck/exports")
        );
    }
}
