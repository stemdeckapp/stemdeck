//! The desktop user-data store: `user-data.json` in the jobs folder (#403),
//! holding the library list, folders, per-song mixes and takes, and the
//! settings the page keeps there.
//!
//! tauri-plugin-store 2.4.3 saved it with `fs::write`, which truncates the
//! file and then writes it, so a crash or power loss during a save could leave
//! it empty or cut short. The plugin then opened an empty store without a word
//! and the next save wrote over the damaged file (#1023).
//!
//! Here a save writes `user-data.json.tmp`, puts it on the disk, moves the
//! current file to `user-data.json.bak`, and renames the new one into its
//! place. At every moment one complete copy is on the disk under one of the
//! three names. On open, only damage (a missing, empty or invalid file) falls
//! back to a copy; a damaged file is set aside as
//! `user-data.json.corrupt-<n>` rather than written over. A file that is
//! there but cannot be read right now (a lock, a sync client) is not damage:
//! the store stays closed, every command says so, and writes stay refused
//! until the page loads it again (`UserData`).
//!
//! No code here is compiled for one platform only: the platform differences
//! are `cfg!` branches every platform compiles. `fs::rename` replaces an
//! existing file on Windows, macOS and Linux alike.

use serde_json::{Map, Value};
use std::{
    fs, io,
    io::Write,
    path::{Path, PathBuf},
    sync::{Mutex, PoisonError},
    thread,
    time::Duration,
};

/// The store's contents: one JSON value per key.
pub type Entries = Map<String, Value>;

/// The start of every error a command returns while the store cannot be
/// opened, or for a write it refused because of that. The page stops and
/// offers to load again on it (static/js/utils.js) rather than going on with
/// stand-ins for what it could not read.
pub const UNAVAILABLE: &str = "user-data-unavailable";

/// How many set-aside damaged files are kept.
const CORRUPT_KEPT: usize = 2;

/// The file a save writes before it renames it into place.
pub fn temp_path(path: &Path) -> PathBuf {
    with_suffix(path, ".tmp")
}

/// The copy of the file as it was before the last save.
pub fn backup_path(path: &Path) -> PathBuf {
    with_suffix(path, ".bak")
}

fn with_suffix(path: &Path, suffix: &str) -> PathBuf {
    let mut name = path.as_os_str().to_owned();
    name.push(suffix);
    PathBuf::from(name)
}

fn file_name(path: &Path) -> String {
    path.file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default()
}

/// The damaged files set aside beside `path`, `<name>.corrupt-<n>`, in the
/// order they were set aside (`n` counts up). Err when the folder cannot be
/// listed; a missing folder holds none.
pub fn corrupt_copies(path: &Path) -> io::Result<Vec<(u64, PathBuf)>> {
    let prefix = format!("{}.corrupt-", file_name(path));
    let Some(dir) = path.parent() else {
        return Ok(Vec::new());
    };
    let listing = match fs::read_dir(dir) {
        Ok(listing) => listing,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(e) => return Err(e),
    };
    let mut found = Vec::new();
    for entry in listing {
        let entry = entry?;
        let name = entry.file_name().to_string_lossy().into_owned();
        if let Some(n) = name.strip_prefix(&prefix).and_then(|n| n.parse().ok()) {
            found.push((n, entry.path()));
        }
    }
    found.sort();
    Ok(found)
}

/// Whether nothing of a store is at `path`: not the file, not a copy, not a
/// set-aside damaged one. Only then is it a first run, the one time the
/// pre-#403 file may be copied in. Anything that cannot be checked (a locked
/// file, a folder that cannot be listed) counts as there.
pub fn no_store_files(path: &Path) -> bool {
    let absent =
        |p: &Path| matches!(fs::symlink_metadata(p), Err(e) if e.kind() == io::ErrorKind::NotFound);
    absent(path)
        && absent(&temp_path(path))
        && absent(&backup_path(path))
        && corrupt_copies(path).is_ok_and(|found| found.is_empty())
}

/// Where the entries came from when the store was opened.
#[derive(Debug, PartialEq)]
pub enum Opened {
    /// The store file itself.
    Main,
    /// No store file and no copy: a first start, or one after a reset.
    Fresh,
    /// The store file was missing, empty or not valid JSON, and a copy was
    /// good.
    Recovered { from: PathBuf, problem: String },
    /// The store file was missing, empty or not valid JSON, and so was every
    /// copy there was.
    Lost { problem: String },
}

impl Opened {
    /// The line for the log, or None when there is nothing to say.
    pub fn warning(&self, path: &Path) -> Option<String> {
        match self {
            Opened::Main | Opened::Fresh => None,
            Opened::Recovered { from, problem } => Some(format!(
                "{} {problem}; restored the library list from {}",
                path.display(),
                from.display()
            )),
            Opened::Lost { problem } => Some(format!(
                "{} {problem}, and no copy of it could be read; \
                 starting with an empty library list",
                path.display()
            )),
        }
    }
}

/// Why a file could not give a store.
enum Unreadable {
    Missing,
    Empty,
    Invalid(String),
    /// There, but not readable now. Not damage: never a reason to fall back.
    Io(io::Error),
}

impl Unreadable {
    fn describe(&self) -> String {
        match self {
            Unreadable::Missing => "is missing".to_string(),
            Unreadable::Empty => "is empty".to_string(),
            Unreadable::Invalid(e) => format!("is not valid JSON ({e})"),
            Unreadable::Io(e) => format!("could not be read ({e})"),
        }
    }
}

fn read_entries(path: &Path) -> Result<Entries, Unreadable> {
    let bytes = retrying(|| fs::read(path)).map_err(|e| {
        if e.kind() == io::ErrorKind::NotFound {
            Unreadable::Missing
        } else {
            Unreadable::Io(e)
        }
    })?;
    if bytes.iter().all(u8::is_ascii_whitespace) {
        return Err(Unreadable::Empty);
    }
    serde_json::from_slice(&bytes).map_err(|e| Unreadable::Invalid(e.to_string()))
}

/// Read the store at `path`, falling back to the copies only when the file is
/// damaged: missing, empty or not valid JSON. Err, with the reason, when the
/// file or a copy that is needed is there but cannot be read now; opening a
/// copy then could put older contents over newer ones.
///
/// A good `.tmp` is tried before the `.bak`: a save leaves one behind only
/// when it stopped after writing the new contents in full and before renaming
/// them into place, so it is newer than the backup. A `.tmp` cut short is not
/// valid JSON (the closing brace is the last byte written) and is passed over.
pub fn read_store(path: &Path) -> Result<(Entries, Opened), String> {
    let problem = match read_entries(path) {
        Ok(entries) => return Ok((entries, Opened::Main)),
        Err(Unreadable::Io(e)) => {
            return Err(format!("{} could not be read ({e})", path.display()));
        }
        Err(problem) => problem,
    };
    let mut damaged_copy = false;
    for copy in [temp_path(path), backup_path(path)] {
        match read_entries(&copy) {
            Ok(entries) => {
                let problem = problem.describe();
                return Ok((
                    entries,
                    Opened::Recovered {
                        from: copy,
                        problem,
                    },
                ));
            }
            Err(Unreadable::Missing) => {}
            Err(Unreadable::Io(e)) => {
                return Err(format!(
                    "{} {}, and {} could not be read ({e})",
                    path.display(),
                    problem.describe(),
                    copy.display()
                ));
            }
            Err(_) => damaged_copy = true,
        }
    }
    if matches!(problem, Unreadable::Missing) && !damaged_copy {
        return Ok((Entries::new(), Opened::Fresh));
    }
    let problem = problem.describe();
    Ok((Entries::new(), Opened::Lost { problem }))
}

/// Errors that clear by themselves: on Windows a virus scanner, the search
/// indexer or a sync client holding the file (ERROR_ACCESS_DENIED for one
/// opened without delete sharing, ERROR_SHARING_VIOLATION,
/// ERROR_LOCK_VIOLATION), elsewhere EBUSY. Windows reports a read-only file
/// with the same ERROR_ACCESS_DENIED, so that one is tried again too and
/// fails after the budget. A missing file, a read-only disk or a path through
/// a file never clear, and fail at once.
fn is_transient(e: &io::Error) -> bool {
    if cfg!(windows) {
        matches!(e.raw_os_error(), Some(5 | 32 | 33))
    } else {
        e.kind() == io::ErrorKind::ResourceBusy
    }
}

/// How often one file step is tried: 200 ms of pauses per step. A save has at
/// most four retried steps (write the .tmp, move the file to .bak, rename the
/// .tmp, put the file back), so its worst case is 0.8 s; it runs on the main
/// thread (see `no_command_that_can_run_long_holds_the_main_thread`).
const ATTEMPTS: u32 = 5;
const PAUSE: Duration = Duration::from_millis(50);

/// Run `op`, trying again while its error is transient.
fn retrying<T>(mut op: impl FnMut() -> io::Result<T>) -> io::Result<T> {
    let mut attempt = 1;
    loop {
        match op() {
            Err(e) if is_transient(&e) && attempt < ATTEMPTS => {
                attempt += 1;
                thread::sleep(PAUSE);
            }
            result => return result,
        }
    }
}

fn write_synced(path: &Path, bytes: &[u8]) -> io::Result<()> {
    let mut file = retrying(|| fs::File::create(path))?;
    file.write_all(bytes)?;
    // On the disk before the rename, or a power loss could leave the new name
    // pointing at contents that never got there.
    file.sync_all()
}

fn sync_folder(dir: &Path) {
    // The renames are entries in the folder, and on Linux and macOS they reach
    // the disk only when the folder does. Windows cannot sync a folder through
    // std and NTFS journals the rename, so nothing is tried there.
    if cfg!(unix) {
        if let Ok(folder) = fs::File::open(dir) {
            let _ = folder.sync_all();
        }
    }
}

/// Write `bytes` to `path` so that a crash at any point leaves a complete
/// store on the disk: written to `.tmp` and synced first, then the current
/// file becomes `.bak` (when `keep_previous`) and `.tmp` is renamed into its
/// place.
///
/// `keep_previous` is false when the file at `path` is not a store this
/// process read or wrote, so a store opened from a copy never moves anything
/// over that copy, and a reset never keeps the library it clears.
///
/// When the final rename fails, the previous file is put back. If that fails
/// as well, the `.tmp` stays: it is the newest complete copy and the next
/// start reads it.
pub fn write_replacing(path: &Path, bytes: &[u8], keep_previous: bool) -> io::Result<()> {
    let dir = path
        .parent()
        .ok_or_else(|| io::Error::other(format!("{} has no folder", path.display())))?;
    fs::create_dir_all(dir)?;
    let temp = temp_path(path);
    if let Err(e) = write_synced(&temp, bytes) {
        let _ = fs::remove_file(&temp);
        return Err(e);
    }
    let backup = backup_path(path);
    let kept = keep_previous
        && match retrying(|| fs::rename(path, &backup)) {
            Ok(()) => true,
            Err(e) if e.kind() == io::ErrorKind::NotFound => false,
            Err(e) => {
                // The current file is untouched and still the store.
                let _ = fs::remove_file(&temp);
                return Err(e);
            }
        };
    if let Err(e) = retrying(|| fs::rename(&temp, path)) {
        if kept {
            let _ = retrying(|| fs::rename(&backup, path));
        }
        return Err(e);
    }
    sync_folder(dir);
    Ok(())
}

/// Move a damaged store file aside as `<name>.corrupt-<n>`, `n` one above the
/// highest already there, so the next save does not write over what may
/// still be repaired by hand. Only the newest `CORRUPT_KEPT` are kept, by
/// that number, so the one just set aside is never the one removed, whatever
/// the clock says. Returns the new name.
fn set_aside(path: &Path) -> io::Result<PathBuf> {
    let existing = corrupt_copies(path)?;
    let next = existing.last().map_or(1, |(n, _)| n + 1);
    let aside = with_suffix(path, &format!(".corrupt-{next}"));
    retrying(|| fs::rename(path, &aside))?;
    let surplus = (existing.len() + 1).saturating_sub(CORRUPT_KEPT);
    for (_, old) in existing.iter().take(surplus) {
        let _ = fs::remove_file(old);
    }
    Ok(aside)
}

/// The store as it is open in this process.
pub struct OpenStore {
    path: PathBuf,
    entries: Entries,
    /// Whether the file at `path` is a store this process read or wrote, so a
    /// save may keep it as the backup. False after an open that recovered from
    /// the backup or started empty, until the first save replaces that file.
    main_good: bool,
    /// Whether an entry changed since the last save that worked. Every
    /// command saves at once, so this is only true after a failed save, and
    /// the exit tries that save once more, as the plugin's exit save did.
    unsaved: bool,
}

impl OpenStore {
    /// Open the store at `path`, passing what it did about damage to
    /// `report`. Err when the store cannot be opened now; nothing on the disk
    /// has changed then, and opening again later is safe.
    ///
    /// On a first run (`no_store_files`) the pre-#403 file at `legacy` is
    /// copied in first, and only then: never into a folder that holds any
    /// store file, so never over a store that is damaged, locked or open.
    ///
    /// A damaged file is set aside first. A store recovered from a complete
    /// `.tmp` is renamed into place at once, so the next save, which writes a
    /// new `.tmp`, can never truncate the only copy of it.
    pub fn open(
        path: &Path,
        legacy: Option<&Path>,
        report: impl Fn(&str),
    ) -> Result<OpenStore, String> {
        if let Some(legacy) = legacy {
            if legacy != path && legacy.is_file() && no_store_files(path) {
                fs::copy(legacy, path)
                    .map_err(|e| format!("{} could not be copied in ({e})", legacy.display()))?;
                report(&format!(
                    "copied the library list from {} (first run)",
                    legacy.display()
                ));
            }
        }
        let (entries, opened) = read_store(path)?;
        let damaged_main = opened != Opened::Main && path.exists();
        if damaged_main {
            let aside = set_aside(path).map_err(|e| {
                format!(
                    "{} is damaged and could not be set aside ({e})",
                    path.display()
                )
            })?;
            report(&format!(
                "{} could not be opened; kept it as {}",
                path.display(),
                aside.display()
            ));
        }
        let mut main_good = opened == Opened::Main;
        if let Opened::Recovered { from, .. } = &opened {
            if *from == temp_path(path) {
                retrying(|| fs::rename(from, path))
                    .map_err(|e| format!("{} could not be put in place ({e})", from.display()))?;
                if let Some(dir) = path.parent() {
                    sync_folder(dir);
                }
                main_good = true;
            }
        }
        if let Some(warning) = opened.warning(path) {
            report(&warning);
        }
        Ok(OpenStore {
            path: path.to_path_buf(),
            entries,
            main_good,
            unsaved: false,
        })
    }

    pub fn get(&self, key: &str) -> Option<Value> {
        self.entries.get(key).cloned()
    }

    pub fn set(&mut self, key: String, value: Value) {
        self.entries.insert(key, value);
        self.unsaved = true;
    }

    /// Write every entry to the file, through `write_replacing`.
    pub fn save(&mut self) -> io::Result<()> {
        // Pretty, as tauri-plugin-store wrote it, so the file reads the same.
        let bytes = serde_json::to_vec_pretty(&self.entries).map_err(io::Error::other)?;
        write_replacing(&self.path, &bytes, self.main_good)?;
        self.main_good = true;
        self.unsaved = false;
        Ok(())
    }

    /// Clear the store for a reset (Settings, "Reset app data"). The copies go
    /// first and the empty store is written without keeping the file it
    /// replaces, so at no point after a crash can the old library sit in a
    /// copy that a later start would restore. Err only when the empty store
    /// could not be written; a copy that could not be removed is reported.
    ///
    /// Worst case on the main thread while Windows holds files: four removals
    /// (.bak, .tmp, two set-aside files) and a save of two steps (write the
    /// .tmp, rename it; nothing is kept), 200 ms each, so 1.2 s.
    pub fn reset(&mut self, report: impl Fn(&str)) -> io::Result<()> {
        let mut copies = vec![backup_path(&self.path), temp_path(&self.path)];
        match corrupt_copies(&self.path) {
            Ok(found) => copies.extend(found.into_iter().map(|(_, p)| p)),
            Err(e) => report(&format!("reset: could not list the set-aside files ({e})")),
        }
        for copy in copies {
            match retrying(|| fs::remove_file(&copy)) {
                Err(e) if e.kind() != io::ErrorKind::NotFound => report(&format!(
                    "reset: could not remove {} ({e}); it still holds the library from before the reset",
                    copy.display()
                )),
                _ => {}
            }
        }
        self.entries.clear();
        self.unsaved = true;
        self.main_good = false;
        self.save()
    }
}

/// The user-data store, managed as Tauri state. Opened from disk on first
/// use, and again whenever its path changes, which is when the jobs folder
/// moves to a new location (#354), or after an open that could not read it.
///
/// The page loads its state from the store once, after `ready` (the
/// `store_ready` command). From the first time the store cannot be reached
/// until `ready` works again, every write is refused: the page may be holding
/// stand-ins for what it could not read, and those must never be saved over
/// the real records. `ready` is what the page calls when it loads again.
#[derive(Default)]
pub struct UserData {
    state: Mutex<State>,
}

#[derive(Default)]
struct State {
    open: Option<OpenStore>,
    /// Set when the store could not be reached; cleared by `ready`.
    blocked: bool,
}

/// Where the store is: its path (Err when the jobs folder cannot be reached,
/// such as a drive that is not mounted) and the pre-#403 file a first run
/// copies in.
pub struct Place {
    pub path: Result<PathBuf, String>,
    pub legacy: Option<PathBuf>,
}

impl UserData {
    fn state(&self) -> std::sync::MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// Run `f` on the store at `place`, holding it for the whole call so two
    /// saves never share the `.tmp` file. A store open at another path is
    /// closed first without a save: the library has moved, and writing to the
    /// old folder would leave a stale copy there. Err, starting with
    /// `UNAVAILABLE`, when the store cannot be reached now, which blocks every
    /// write until `ready`. With `write`, also Err while blocked.
    fn run<T>(
        &self,
        place: Place,
        report: impl Fn(&str),
        write: bool,
        f: impl FnOnce(&mut OpenStore) -> T,
    ) -> Result<T, String> {
        let mut state = self.state();
        if write && state.blocked {
            return Err(format!(
                "{UNAVAILABLE}: not saved: the library list could not be read earlier, \
                 so it has to be loaded again first"
            ));
        }
        let path = match place.path {
            Ok(path) => path,
            Err(e) => {
                state.blocked = true;
                return Err(format!("{UNAVAILABLE}: {e}"));
            }
        };
        if state.open.as_ref().is_some_and(|store| store.path != path) {
            state.open = None;
        }
        if state.open.is_none() {
            match OpenStore::open(&path, place.legacy.as_deref(), &report) {
                Ok(store) => state.open = Some(store),
                Err(e) => {
                    state.blocked = true;
                    let error = format!("{UNAVAILABLE}: {e}");
                    report(&error);
                    return Err(error);
                }
            }
        }
        match state.open.as_mut() {
            Some(store) => Ok(f(store)),
            None => Err(format!("{UNAVAILABLE}: not open")),
        }
    }

    /// Read from the store.
    pub fn read<T>(
        &self,
        place: Place,
        report: impl Fn(&str),
        f: impl FnOnce(&OpenStore) -> T,
    ) -> Result<T, String> {
        self.run(place, report, false, |store| f(store))
    }

    /// Change the store; refused while blocked.
    pub fn write<T>(
        &self,
        place: Place,
        report: impl Fn(&str),
        f: impl FnOnce(&mut OpenStore) -> T,
    ) -> Result<T, String> {
        self.run(place, report, true, f)
    }

    /// Open the store for a page that is loading, and lift the block on
    /// writes once it is open.
    pub fn ready(&self, place: Place, report: impl Fn(&str)) -> Result<(), String> {
        self.run(place, report, false, |_| ())?;
        self.state().blocked = false;
        Ok(())
    }

    /// Clear the store (Settings, "Reset app data"). Not refused while
    /// blocked: nothing the page holds is written, and the page loads again
    /// right after.
    pub fn reset(&self, place: Place, report: impl Fn(&str) + Copy) -> Result<(), String> {
        self.run(place, report, false, |store| store.reset(report))?
            .map_err(|e| format!("could not save user-data.json: {e}"))
    }

    /// At exit: save the open store if its last save failed. None when there
    /// was nothing to save.
    pub fn save_unsaved(&self) -> Option<io::Result<()>> {
        let mut state = self.state();
        let store = state.open.as_mut().filter(|store| store.unsaved)?;
        Some(store.save())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::cell::RefCell;
    use std::collections::HashMap;
    use std::time::Instant;

    /// Test stores live under the repo's git-ignored data/, never a library.
    fn scratch() -> tempfile::TempDir {
        let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../data/store-tests");
        fs::create_dir_all(&root).expect("data/store-tests");
        tempfile::tempdir_in(root).expect("scratch folder")
    }

    fn read_json(path: &Path) -> Value {
        serde_json::from_slice(&fs::read(path).expect("read")).expect("valid JSON")
    }

    /// Opens the store, keeping what it reported.
    fn open(path: &Path) -> (OpenStore, Vec<String>) {
        let reported = RefCell::new(Vec::new());
        let store = OpenStore::open(path, None, |line| {
            reported.borrow_mut().push(line.to_string())
        })
        .expect("opened");
        (store, reported.into_inner())
    }

    /// The set-aside damaged files, oldest first.
    fn aside(path: &Path) -> Vec<PathBuf> {
        corrupt_copies(path)
            .expect("listed")
            .into_iter()
            .map(|(_, p)| p)
            .collect()
    }

    fn place(path: &Path) -> Place {
        Place {
            path: Ok(path.to_path_buf()),
            legacy: None,
        }
    }

    fn mentions(reported: &[String], text: &str) -> bool {
        reported.iter().any(|line| line.contains(text))
    }

    #[test]
    fn a_save_writes_the_store_and_keeps_the_previous_one() {
        let dir = scratch();
        let path = dir.path().join("user-data.json");
        let (mut store, reported) = open(&path);
        assert!(reported.is_empty(), "a first start is not a recovery");

        store.set("a".into(), json!(1));
        store.save().expect("first save");
        assert_eq!(read_json(&path), json!({ "a": 1 }));
        assert!(
            !backup_path(&path).exists(),
            "nothing before the first save"
        );

        store.set("b".into(), json!({ "voice": "split" }));
        store.save().expect("second save");
        assert_eq!(
            read_json(&path),
            json!({ "a": 1, "b": { "voice": "split" } })
        );
        assert_eq!(read_json(&backup_path(&path)), json!({ "a": 1 }));
        assert!(!temp_path(&path).exists(), "no .tmp left after a save");

        let (reopened, reported) = open(&path);
        assert!(reported.is_empty());
        assert_eq!(reopened.get("b"), Some(json!({ "voice": "split" })));
    }

    /// The crash #1023 is about: the store file cut short mid-write.
    #[test]
    fn a_truncated_store_with_a_good_backup_recovers_from_the_backup() {
        let dir = scratch();
        let path = dir.path().join("user-data.json");
        fs::write(backup_path(&path), r#"{"songs":["one","two"]}"#).unwrap();
        fs::write(&path, r#"{"songs":["one","tw"#).unwrap();

        let (mut store, reported) = open(&path);
        assert_eq!(store.get("songs"), Some(json!(["one", "two"])));
        assert!(mentions(&reported, "is not valid JSON"), "{reported:?}");
        assert!(mentions(&reported, "user-data.json.bak"), "{reported:?}");

        // The broken file is kept aside, not written over.
        let set_aside = aside(&path);
        assert_eq!(set_aside.len(), 1, "{set_aside:?}");
        assert_eq!(fs::read(&set_aside[0]).unwrap(), br#"{"songs":["one","tw"#);
        assert!(mentions(&reported, ".corrupt-"), "{reported:?}");

        // And never moved over the good backup.
        store.set("songs".into(), json!(["one", "two", "three"]));
        store.save().expect("save");
        assert_eq!(
            read_json(&path),
            json!({ "songs": ["one", "two", "three"] })
        );
        assert_eq!(
            read_json(&backup_path(&path)),
            json!({ "songs": ["one", "two"] })
        );
    }

    #[test]
    fn an_empty_store_recovers_from_the_backup() {
        let dir = scratch();
        let path = dir.path().join("user-data.json");
        fs::write(backup_path(&path), r#"{"a":1}"#).unwrap();
        fs::write(&path, "").unwrap();

        let (store, reported) = open(&path);
        assert_eq!(store.get("a"), Some(json!(1)));
        assert!(mentions(&reported, "is empty"), "{reported:?}");
    }

    /// A store file that is there but cannot be read now (here a folder in
    /// its place, which reads as an I/O error on every platform) is not
    /// damage: no fallback, nothing moved. Every write is refused from then on
    /// until `ready` works, even once the file can be read: the page may hold
    /// stand-ins for what it could not read.
    #[test]
    fn an_unreadable_store_blocks_writes_until_ready() {
        let dir = scratch();
        let path = dir.path().join("user-data.json");
        fs::create_dir(&path).unwrap();
        fs::write(backup_path(&path), r#"{"a":"old"}"#).unwrap();

        let reported = RefCell::new(Vec::new());
        let report = |line: &str| reported.borrow_mut().push(line.to_string());
        let data = UserData::default();
        let err = data.ready(place(&path), report).expect_err("unavailable");
        assert!(err.starts_with(UNAVAILABLE), "{err}");
        assert!(mentions(&reported.borrow(), UNAVAILABLE), "logged");
        assert!(path.is_dir(), "the file in place is untouched");
        assert!(aside(&path).is_empty(), "nothing set aside");
        assert_eq!(read_json(&backup_path(&path)), json!({ "a": "old" }));

        // Readable again, but the page has not loaded again: refused.
        fs::remove_dir(&path).unwrap();
        fs::write(&path, r#"{"a":"new"}"#).unwrap();
        let refused = data.write(place(&path), report, |s| {
            s.set("a".into(), json!("stand-in"));
            s.save()
        });
        assert!(refused.expect_err("blocked").starts_with(UNAVAILABLE));
        assert_eq!(read_json(&path), json!({ "a": "new" }), "nothing written");

        // A read works, and does not lift the block.
        let got = data
            .read(place(&path), report, |s| s.get("a"))
            .expect("read");
        assert_eq!(got, Some(json!("new")));
        assert!(data.write(place(&path), report, |_| ()).is_err());

        // The page loads again: writes work.
        data.ready(place(&path), report).expect("ready");
        data.write(place(&path), report, |s| {
            s.set("a".into(), json!("newer"));
            s.save()
        })
        .expect("open")
        .expect("saved");
        assert_eq!(read_json(&path), json!({ "a": "newer" }));
    }

    /// A jobs folder that cannot be reached (an unmounted drive) is
    /// unavailable too, and blocks writes the same way.
    #[test]
    fn an_unreachable_folder_is_unavailable_and_blocks_writes() {
        let dir = scratch();
        let path = dir.path().join("user-data.json");
        let data = UserData::default();
        let gone = Place {
            path: Err("failed to create E:\\StemDeck: the device is not ready".into()),
            legacy: None,
        };
        let err = data
            .read(gone, |_| {}, |s| s.get("a"))
            .expect_err("unavailable");
        assert!(err.starts_with(UNAVAILABLE), "{err}");
        assert!(data.write(place(&path), |_| {}, |_| ()).is_err(), "blocked");
        data.ready(place(&path), |_| {}).expect("ready");
        assert!(data.write(place(&path), |_| {}, |_| ()).is_ok());
    }

    /// The pre-#403 file is copied in on a first run only.
    #[test]
    fn the_legacy_file_is_copied_in_only_when_no_store_file_exists() {
        let dir = scratch();
        let legacy = dir.path().join("legacy.json");
        fs::write(&legacy, r#"{"from":"legacy"}"#).unwrap();
        let open_with_legacy =
            |path: &Path| OpenStore::open(path, Some(&legacy), |_| {}).expect("opened");

        // First run: copied.
        let first = dir.path().join("first").join("user-data.json");
        fs::create_dir_all(first.parent().unwrap()).unwrap();
        assert_eq!(open_with_legacy(&first).get("from"), Some(json!("legacy")));

        // A complete .tmp, a cut-short .tmp, a .bak or a set-aside file:
        // not a first run, never copied.
        for (name, contents) in [
            ("user-data.json.tmp", "{\"a\":1}"),
            ("user-data.json.tmp", "{\"a\":"),
            ("user-data.json.bak", "{\"a\":1}"),
            ("user-data.json.corrupt-1", "{"),
        ] {
            let folder = dir.path().join(format!("{name}-{}", contents.len()));
            fs::create_dir_all(&folder).unwrap();
            fs::write(folder.join(name), contents).unwrap();
            let path = folder.join("user-data.json");
            let store = open_with_legacy(&path);
            assert_eq!(store.get("from"), None, "{name} {contents}");
        }

        // All three damaged (the Lost state): never copied into the empty
        // store, which opens with the damaged file set aside.
        let lost = dir.path().join("lost").join("user-data.json");
        fs::create_dir_all(lost.parent().unwrap()).unwrap();
        fs::write(&lost, "{").unwrap();
        fs::write(temp_path(&lost), "{").unwrap();
        fs::write(backup_path(&lost), "").unwrap();
        let store = open_with_legacy(&lost);
        assert_eq!(store.get("from"), None);
        assert!(
            !lost.exists(),
            "the damaged file is set aside, not replaced"
        );
        assert!(!no_store_files(&lost));
    }

    /// The first-run check looks at whether a store file is there, never
    /// whether it reads: a .tmp that is locked (here one that cannot be read
    /// as a file at all) still stops the legacy copy. So does a folder that
    /// cannot be listed for set-aside files.
    #[test]
    fn a_store_file_that_cannot_be_read_still_blocks_the_legacy_copy() {
        let dir = scratch();
        let path = dir.path().join("user-data.json");
        fs::create_dir(temp_path(&path)).unwrap();
        assert!(read_entries(&temp_path(&path)).is_err(), "unreadable");
        assert!(!no_store_files(&path));

        let blocker = dir.path().join("not-a-folder");
        fs::write(&blocker, "x").unwrap();
        assert!(!no_store_files(&blocker.join("user-data.json")));
    }

    /// A stop between the two renames: no store file, the new contents
    /// complete in .tmp, the previous ones in .bak. The .tmp goes into place
    /// at once, so a second stop during the next save still leaves it.
    #[test]
    fn a_complete_tmp_is_put_in_place_before_the_next_save() {
        let dir = scratch();
        let path = dir.path().join("user-data.json");
        fs::write(backup_path(&path), r#"{"a":1}"#).unwrap();
        fs::write(temp_path(&path), r#"{"a":2}"#).unwrap();

        let (_store, reported) = open(&path);
        assert!(mentions(&reported, "is missing"), "{reported:?}");
        assert!(mentions(&reported, "user-data.json.tmp"), "{reported:?}");
        assert_eq!(read_json(&path), json!({ "a": 2 }), "in place at once");
        assert!(!temp_path(&path).exists());
        assert_eq!(read_json(&backup_path(&path)), json!({ "a": 1 }), "kept");

        // A second stop, while the next save writes its .tmp.
        fs::write(temp_path(&path), r#"{"a":"#).unwrap();
        let (store, reported) = open(&path);
        assert!(reported.is_empty(), "{reported:?}");
        assert_eq!(store.get("a"), Some(json!(2)));
    }

    /// A stop while the .tmp was being written: the store file is untouched
    /// and wins, and the next save leaves no .tmp behind.
    #[test]
    fn a_tmp_beside_a_good_store_is_ignored_and_cleared_by_the_next_save() {
        let dir = scratch();
        let path = dir.path().join("user-data.json");
        fs::write(&path, r#"{"a":1}"#).unwrap();
        fs::write(temp_path(&path), r#"{"a":"#).unwrap();

        let (mut store, reported) = open(&path);
        assert!(reported.is_empty(), "{reported:?}");
        assert_eq!(store.get("a"), Some(json!(1)));
        store.save().expect("save");
        assert!(!temp_path(&path).exists());
        assert_eq!(read_json(&backup_path(&path)), json!({ "a": 1 }));
    }

    #[test]
    fn a_cut_short_tmp_is_passed_over_for_the_backup() {
        let dir = scratch();
        let path = dir.path().join("user-data.json");
        fs::write(backup_path(&path), r#"{"a":1}"#).unwrap();
        fs::write(temp_path(&path), r#"{"a":"#).unwrap();

        let (store, _) = open(&path);
        assert_eq!(store.get("a"), Some(json!(1)));
    }

    #[test]
    fn a_broken_store_and_backup_start_empty_and_say_so() {
        let dir = scratch();
        let path = dir.path().join("user-data.json");
        fs::write(&path, r#"{"a":"#).unwrap();
        fs::write(backup_path(&path), "").unwrap();

        let (mut store, reported) = open(&path);
        assert_eq!(store.entries, Entries::new());
        assert!(
            mentions(&reported, "starting with an empty library list"),
            "{reported:?}"
        );
        assert_eq!(aside(&path).len(), 1, "the broken file is kept");

        store.set("a".into(), json!(1));
        store.save().expect("save");
        assert_eq!(read_json(&path), json!({ "a": 1 }));
    }

    /// Numbered one above the highest already there, and pruned by that
    /// number, so the file just set aside is always kept.
    #[test]
    fn only_the_two_newest_damaged_files_are_kept() {
        let dir = scratch();
        let path = dir.path().join("user-data.json");
        for n in [3, 100, 7] {
            fs::write(with_suffix(&path, &format!(".corrupt-{n}")), "x").unwrap();
        }
        fs::write(&path, "{").unwrap();
        let _ = open(&path);
        let kept: Vec<String> = aside(&path).iter().map(|p| file_name(p)).collect();
        assert_eq!(
            kept,
            ["user-data.json.corrupt-100", "user-data.json.corrupt-101"]
        );
        assert_eq!(fs::read(with_suffix(&path, ".corrupt-101")).unwrap(), b"{");
    }

    #[test]
    fn a_json_value_that_is_not_an_object_is_not_a_store() {
        let dir = scratch();
        let path = dir.path().join("user-data.json");
        fs::write(&path, "[]").unwrap();
        let (_, reported) = open(&path);
        assert!(mentions(&reported, "is not valid JSON"), "{reported:?}");
    }

    #[test]
    fn a_missing_store_with_no_copies_is_a_fresh_start() {
        let dir = scratch();
        let (entries, opened) = read_store(&dir.path().join("user-data.json")).unwrap();
        assert_eq!(opened, Opened::Fresh);
        assert!(entries.is_empty());
    }

    /// A reset leaves no copy of the library it cleared, at any step.
    #[test]
    fn a_reset_removes_every_copy_and_keeps_none_of_the_old_library() {
        let dir = scratch();
        let path = dir.path().join("user-data.json");
        let (mut store, _) = open(&path);
        store.set("songs".into(), json!(["one"]));
        store.save().unwrap();
        store.save().unwrap();
        fs::write(temp_path(&path), "{").unwrap();
        fs::write(with_suffix(&path, ".corrupt-100"), "{").unwrap();
        assert!(backup_path(&path).exists());

        let reported = RefCell::new(Vec::new());
        store
            .reset(|line| reported.borrow_mut().push(line.to_string()))
            .expect("reset");
        assert!(reported.borrow().is_empty(), "{:?}", reported.borrow());
        assert_eq!(read_json(&path), json!({}));
        assert!(!backup_path(&path).exists(), "the old library is not kept");
        assert!(!temp_path(&path).exists());
        assert!(aside(&path).is_empty());
    }

    #[test]
    fn transient_errors_are_the_only_ones_tried_again() {
        let raw = io::Error::from_raw_os_error;
        if cfg!(windows) {
            assert!(is_transient(&raw(32)), "sharing violation");
            assert!(is_transient(&raw(33)), "lock violation");
            assert!(is_transient(&raw(5)), "access denied");
            assert!(!is_transient(&raw(2)), "file not found");
            assert!(!is_transient(&raw(3)), "path not found");
            assert!(!is_transient(&raw(19)), "write protected");
        } else {
            // The same numbers on Linux and macOS.
            assert!(is_transient(&raw(16)), "EBUSY");
            assert!(!is_transient(&raw(2)), "ENOENT");
            assert!(!is_transient(&raw(20)), "ENOTDIR");
            assert!(!is_transient(&raw(30)), "EROFS");
            assert!(!is_transient(&raw(13)), "EACCES");
        }
        let calls = std::cell::Cell::new(0);
        let started = Instant::now();
        let result: io::Result<()> = retrying(|| {
            calls.set(calls.get() + 1);
            Err(io::Error::from(io::ErrorKind::NotFound))
        });
        assert!(result.is_err());
        assert_eq!(calls.get(), 1, "a missing file is not tried again");
        assert!(started.elapsed() < PAUSE);
    }

    #[test]
    fn user_data_opens_again_when_the_path_changes() {
        let dir = scratch();
        let first = dir.path().join("one").join("user-data.json");
        let second = dir.path().join("two").join("user-data.json");
        fs::create_dir_all(second.parent().unwrap()).unwrap();
        fs::write(&second, r#"{"where":"two"}"#).unwrap();

        let data = UserData::default();
        data.write(
            place(&first),
            |_| {},
            |s| {
                s.set("where".into(), json!("one"));
                s.save()
            },
        )
        .expect("open")
        .expect("saved");
        let seen = data
            .read(place(&second), |_| {}, |s| s.get("where"))
            .expect("open");
        assert_eq!(seen, Some(json!("two")));
        assert_eq!(read_json(&first), json!({ "where": "one" }));
    }

    /// A save that failed is tried again at exit, and only then.
    #[test]
    fn the_exit_saves_what_a_failed_save_left_unsaved() {
        let dir = scratch();
        // A file where the store's folder should be: every save fails.
        let blocker = dir.path().join("jobs");
        fs::write(&blocker, "not a folder").unwrap();
        let path = blocker.join("user-data.json");

        let data = UserData::default();
        assert!(data.save_unsaved().is_none(), "nothing open");
        let saved = data
            .write(
                place(&path),
                |_| {},
                |s| {
                    s.set("a".into(), json!(1));
                    s.save()
                },
            )
            .expect("open");
        assert!(saved.is_err(), "the folder cannot be made");

        fs::remove_file(&blocker).unwrap();
        data.save_unsaved()
            .expect("unsaved")
            .expect("saved at exit");
        assert_eq!(read_json(&path), json!({ "a": 1 }));
        assert!(data.save_unsaved().is_none(), "saved once");
    }

    /// What the fixture below holds: the keys and kinds of value the page
    /// keeps, `stemdeck.folders` in its real `{ v, folders, tracks }` shape.
    fn plugin_store_entries() -> HashMap<String, Value> {
        serde_json::from_str(include_str!(
            "../tests/fixtures/user-data-plugin-2.4.3.input.json"
        ))
        .unwrap()
    }

    // tests/fixtures/user-data-plugin-2.4.3.json holds the bytes the real
    // tauri-plugin-store 2.4.3 wrote for those entries: captured once by
    // calling its `Store::save` on a mock Tauri app (tauri's `test` feature),
    // in a scratch worktree of the shell before this change. Not regenerated
    // here, which no longer depends on the plugin.

    /// A store an existing install saved with the plugin opens as it is.
    #[test]
    fn a_store_the_plugin_saved_opens_as_it_is() {
        let fixture = include_bytes!("../tests/fixtures/user-data-plugin-2.4.3.json");
        let dir = scratch();
        let path = dir.path().join("user-data.json");
        fs::write(&path, fixture).unwrap();
        let (store, reported) = open(&path);
        assert!(reported.is_empty(), "{reported:?}");
        let expected: Entries = plugin_store_entries().into_iter().collect();
        assert_eq!(store.entries, expected);
    }

    /// What this saves, the plugin's reader (`from_slice` into a HashMap, as
    /// 0.20 runs it) reads back whole, so a downgrade keeps the library.
    #[test]
    fn what_a_save_writes_the_plugin_reads() {
        let dir = scratch();
        let path = dir.path().join("user-data.json");
        let (mut store, _) = open(&path);
        for (key, value) in plugin_store_entries() {
            store.set(key, value);
        }
        store.save().unwrap();
        let read: HashMap<String, Value> = serde_json::from_slice(&fs::read(&path).unwrap())
            .expect("the plugin's reader accepts it");
        assert_eq!(read, plugin_store_entries());
    }

    /// A store about the size of a 300-song library with every song's mixes:
    /// 9 MB, as measured for #996.
    fn nine_megabytes() -> Entries {
        let mut store = Entries::new();
        for song in 0..300 {
            let lanes: Vec<Value> = (0..98)
                .map(|lane| {
                    json!({
                        "stem": format!("stem-{lane}"),
                        "gain": 0.75, "pan": -0.25, "mute": false, "solo": false,
                        "eq": [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7],
                        "label": "a lane label of an ordinary length",
                    })
                })
                .collect();
            store.insert(
                format!("stemdeck:mix:{song:032x}"),
                json!({ "lanes": lanes }),
            );
        }
        store
    }

    /// Where the crash helper saves, and how: "atomic" through
    /// `write_replacing`, "in-place" as tauri-plugin-store 2.4.3 did.
    const CRASH_DIR: &str = "STEMDECK_STORE_CRASH_DIR";
    const CRASH_MODE: &str = "STEMDECK_STORE_CRASH_MODE";

    /// Not a test: the child process `kill_mid_save` starts and kills. Writes
    /// a 9 MB store forever, the two versions `crash_versions` made, in turn.
    /// They are serialized up front, so nearly all of the time, and so nearly
    /// every kill, falls in the write itself.
    #[test]
    #[ignore]
    fn crash_helper_saves_until_killed() {
        let (Ok(dir), Ok(mode)) = (std::env::var(CRASH_DIR), std::env::var(CRASH_MODE)) else {
            return;
        };
        let dir = Path::new(&dir);
        let path = dir.join("user-data.json");
        let versions: Vec<Vec<u8>> = (0..2)
            .map(|n| fs::read(dir.join(format!("version-{n}"))).expect("version"))
            .collect();
        for n in 0usize.. {
            let bytes = &versions[n % 2];
            if mode == "atomic" {
                write_replacing(&path, bytes, true).expect("save");
            } else {
                fs::write(&path, bytes).expect("write");
            }
            if n == 0 {
                println!("saved");
            }
        }
    }

    /// The two versions of a 9 MB store the helper writes, made once.
    fn crash_versions() -> [Vec<u8>; 2] {
        let mut entries = nine_megabytes();
        [0, 1].map(|n| {
            entries.insert("n".into(), json!(n));
            serde_json::to_vec_pretty(&entries).unwrap()
        })
    }

    /// Start the helper, let it save for a while, kill it at `delay`, and say
    /// how the store opens afterwards.
    fn kill_mid_save(mode: &str, versions: &[Vec<u8>; 2], delay: Duration) -> (Entries, Opened) {
        use std::io::{BufRead, BufReader};
        use std::process::{Command, Stdio};

        let dir = scratch();
        for (n, bytes) in versions.iter().enumerate() {
            fs::write(dir.path().join(format!("version-{n}")), bytes).unwrap();
        }
        let mut child = Command::new(std::env::current_exe().unwrap())
            .args([
                "store_file::tests::crash_helper_saves_until_killed",
                "--exact",
                "--ignored",
                "--nocapture",
            ])
            .env(CRASH_DIR, dir.path())
            .env(CRASH_MODE, mode)
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .expect("helper started");
        let stdout = child.stdout.take().unwrap();
        let saved = BufReader::new(stdout)
            .lines()
            .map_while(Result::ok)
            .any(|line| line == "saved");
        assert!(saved, "the helper never saved");
        // A kill somewhere inside a later save.
        thread::sleep(delay);
        child.kill().expect("killed");
        child.wait().expect("reaped");
        read_store(&dir.path().join("user-data.json")).expect("readable")
    }

    /// The crash #1023 is about, for real: a process killed partway through
    /// saving a 9 MB store, at 24 different moments. Every time the store
    /// opens with its entries, from the file or from a copy. (A process kill
    /// leaves the OS cache intact, so this covers a crash, not a power loss;
    /// the sync before the rename is what covers that.)
    #[test]
    fn a_process_killed_mid_save_always_leaves_a_store_to_open() {
        let versions = crash_versions();
        for step in 0..24 {
            let delay = Duration::from_millis(step * 3);
            let (entries, opened) = kill_mid_save("atomic", &versions, delay);
            assert!(
                !matches!(opened, Opened::Lost { .. } | Opened::Fresh),
                "kill {step}: {opened:?}"
            );
            assert!(entries.contains_key("n"), "kill {step}: no entries");
        }
    }

    /// The same kills against the plugin's in-place write, to show what #1023
    /// fixes. A measurement: `cargo test in_place_kills -- --ignored --nocapture`.
    #[test]
    #[ignore]
    fn in_place_kills_lose_the_store() {
        let versions = crash_versions();
        let mut lost = 0;
        for step in 0..24 {
            let delay = Duration::from_millis(step * 3);
            let (_, opened) = kill_mid_save("in-place", &versions, delay);
            if !matches!(opened, Opened::Main) {
                lost += 1;
            }
        }
        println!("in-place write: {lost} of 24 kills left a store that does not open");
    }

    /// Save time before (#1023) and after, for a 9 MB store. "Before" is what
    /// tauri-plugin-store 2.4.3 ran, `to_vec_pretty` then `fs::write`.
    /// Ignored: a measurement, not a check. Run with
    /// `cargo test --release save_time -- --ignored --nocapture`.
    #[test]
    #[ignore]
    fn save_time_before_and_after() {
        const RUNS: usize = 30;
        let dir = scratch();
        let path = dir.path().join("user-data.json");
        let store = nine_megabytes();
        let size = serde_json::to_vec_pretty(&store).unwrap().len();

        let median = |mut times: Vec<Duration>| {
            times.sort();
            times[times.len() / 2]
        };
        let mut before = Vec::new();
        for _ in 0..RUNS {
            let start = Instant::now();
            let bytes = serde_json::to_vec_pretty(&store).unwrap();
            fs::write(&path, bytes).unwrap();
            before.push(start.elapsed());
        }
        fs::remove_file(&path).unwrap();

        let mut open = OpenStore {
            path: path.clone(),
            entries: store,
            main_good: true,
            unsaved: false,
        };
        let mut after = Vec::new();
        for _ in 0..RUNS {
            let start = Instant::now();
            open.save().unwrap();
            after.push(start.elapsed());
        }
        let (b_min, a_min) = (*before.iter().min().unwrap(), *after.iter().min().unwrap());
        let (b_max, a_max) = (*before.iter().max().unwrap(), *after.iter().max().unwrap());
        let (b, a) = (median(before), median(after));
        println!(
            "store {:.1} MB, {RUNS} saves each\n\
             before (fs::write): median {b:?}, min {b_min:?}, max {b_max:?}\n\
             after (tmp, sync, rename): median {a:?}, min {a_min:?}, max {a_max:?}\n\
             ratio of medians {:.2}",
            size as f64 / 1_048_576.0,
            a.as_secs_f64() / b.as_secs_f64()
        );
    }
}
