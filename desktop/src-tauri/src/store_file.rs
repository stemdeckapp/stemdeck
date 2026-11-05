//! The desktop user-data store: `user-data.json` in the jobs folder (#403),
//! holding the library list, folders, per-song mixes and takes, and the
//! settings the page keeps there.
//!
//! tauri-plugin-store 2.4.3 saved it with `fs::write`, which truncates the
//! file and then writes it, so a crash or power loss during a save could leave
//! it empty or cut short and the next start would lose the library list
//! (#1023). Here a save writes `user-data.json.tmp`, puts it on the disk, moves
//! the current file to `user-data.json.bak`, and renames the new one into its
//! place. At every moment one complete copy is on the disk under one of the
//! three names, and the start-up read takes the first good one.
//!
//! No code here is compiled for one platform only: the folder sync after the
//! rename is a `cfg!(unix)` branch every platform compiles. `fs::rename`
//! replaces an existing file on Windows, macOS and Linux alike; on Windows a
//! virus scanner or the search indexer can hold the file for a moment, which
//! the retry covers.

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

/// Where the entries came from when the store was opened.
#[derive(Debug, PartialEq)]
pub enum Opened {
    /// The store file itself.
    Main,
    /// No store file and no copy: a first start, or one after a reset.
    Fresh,
    /// The store file was missing, empty or unreadable, and a copy was good.
    Recovered { from: PathBuf, problem: String },
    /// Neither the store file nor a copy could be read.
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

/// Read the store at `path`, falling back to the copies when it is missing,
/// empty or not valid JSON.
///
/// A good `.tmp` is tried before the `.bak`: a save leaves one behind only
/// when it stopped after writing the new contents in full and before renaming
/// them into place, so it is newer than the backup. A `.tmp` cut short is not
/// valid JSON (the closing brace is the last byte written) and is passed over.
pub fn read_store(path: &Path) -> (Entries, Opened) {
    let problem = match read_entries(path) {
        Ok(entries) => return (entries, Opened::Main),
        Err(problem) => problem,
    };
    let mut copies_seen = false;
    for copy in [temp_path(path), backup_path(path)] {
        match read_entries(&copy) {
            Ok(entries) => {
                let problem = problem.describe();
                return (
                    entries,
                    Opened::Recovered {
                        from: copy,
                        problem,
                    },
                );
            }
            Err(Unreadable::Missing) => {}
            Err(_) => copies_seen = true,
        }
    }
    if matches!(problem, Unreadable::Missing) && !copies_seen {
        return (Entries::new(), Opened::Fresh);
    }
    let problem = problem.describe();
    (Entries::new(), Opened::Lost { problem })
}

/// How often a file operation is tried before its error is returned. Even
/// right after this process closed a file, a virus scanner or the search
/// indexer can hold it for a moment on Windows; most clear well inside the
/// budget. A save runs on the main thread (see
/// `no_command_that_can_run_long_holds_the_main_thread`), so the budget is
/// kept to one second rather than the update's five.
const ATTEMPTS: u32 = 20;
const PAUSE: Duration = Duration::from_millis(50);

/// Run `op`, trying again on any error except a missing file, which waiting
/// does not fix.
fn retrying<T>(mut op: impl FnMut() -> io::Result<T>) -> io::Result<T> {
    let mut attempt = 1;
    loop {
        match op() {
            Err(e) if e.kind() != io::ErrorKind::NotFound && attempt < ATTEMPTS => {
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

/// Write `bytes` to `path` so that a crash at any point leaves a complete
/// store on the disk: written to `.tmp` and synced first, then the current
/// file becomes `.bak` (when `keep_previous`) and `.tmp` is renamed into its
/// place.
///
/// `keep_previous` is false when the file at `path` is not known to be good,
/// so a store opened from a copy never moves a broken file over that copy.
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
    // The renames are entries in the folder, and on Linux and macOS they reach
    // the disk only when the folder does. Windows cannot sync a folder through
    // std and NTFS journals the rename, so nothing is tried there.
    if cfg!(unix) {
        if let Ok(folder) = fs::File::open(dir) {
            let _ = folder.sync_all();
        }
    }
    Ok(())
}

/// The store as it is open in this process.
pub struct OpenStore {
    path: PathBuf,
    entries: Entries,
    /// Whether the file at `path` is a good store, so a save may keep it as
    /// the backup. False after an open that recovered from a copy or started
    /// empty, until the first save replaces that file.
    main_good: bool,
    /// Whether an entry changed since the last save that worked. Every
    /// command saves at once, so this is only true after a failed save, and
    /// the exit tries that save once more, as the plugin's exit save did.
    unsaved: bool,
}

impl OpenStore {
    /// Open the store at `path`, passing any recovery to `report`.
    pub fn open(path: &Path, report: impl Fn(&str)) -> OpenStore {
        let (entries, opened) = read_store(path);
        if let Some(warning) = opened.warning(path) {
            report(&warning);
        }
        OpenStore {
            path: path.to_path_buf(),
            entries,
            main_good: opened == Opened::Main,
            unsaved: false,
        }
    }

    pub fn get(&self, key: &str) -> Option<Value> {
        self.entries.get(key).cloned()
    }

    pub fn set(&mut self, key: String, value: Value) {
        self.entries.insert(key, value);
        self.unsaved = true;
    }

    pub fn clear(&mut self) {
        self.entries.clear();
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

    /// Remove the backup, for a reset that must not leave the old library
    /// behind to be restored later.
    pub fn remove_backup(&self) -> io::Result<()> {
        match retrying(|| fs::remove_file(backup_path(&self.path))) {
            Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(()),
            result => result,
        }
    }
}

/// The user-data store, managed as Tauri state. Opened from disk on first
/// use, and again whenever its path changes, which is when the jobs folder
/// moves to a new location (#354).
#[derive(Default)]
pub struct UserData {
    open: Mutex<Option<OpenStore>>,
}

impl UserData {
    /// Run `f` on the store at `path`, holding it for the whole call so two
    /// saves never share the `.tmp` file. A store open at another path is
    /// closed first without a save: the library has moved, and writing to the
    /// old folder would leave a stale copy there.
    pub fn with<T>(
        &self,
        path: &Path,
        report: impl Fn(&str),
        f: impl FnOnce(&mut OpenStore) -> T,
    ) -> T {
        let mut open = self.open.lock().unwrap_or_else(PoisonError::into_inner);
        if open.as_ref().is_some_and(|store| store.path != path) {
            *open = None;
        }
        let store = open.get_or_insert_with(|| OpenStore::open(path, report));
        f(store)
    }

    /// At exit: save the open store if its last save failed. None when there
    /// was nothing to save.
    pub fn save_unsaved(&self) -> Option<io::Result<()>> {
        let mut open = self.open.lock().unwrap_or_else(PoisonError::into_inner);
        let store = open.as_mut().filter(|store| store.unsaved)?;
        Some(store.save())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::cell::RefCell;
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
        let store = OpenStore::open(path, |line| reported.borrow_mut().push(line.to_string()));
        (store, reported.into_inner())
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
        assert_eq!(reported.len(), 1, "{reported:?}");
        assert!(reported[0].contains("is not valid JSON"), "{}", reported[0]);
        assert!(
            reported[0].contains("user-data.json.bak"),
            "{}",
            reported[0]
        );

        // The broken file is never moved over the good backup.
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
        assert!(reported[0].contains("is empty"), "{}", reported[0]);
    }

    /// A stop between the two renames: no store file, the new contents
    /// complete in .tmp, the previous ones in .bak.
    #[test]
    fn a_missing_store_recovers_from_a_complete_tmp_before_the_backup() {
        let dir = scratch();
        let path = dir.path().join("user-data.json");
        fs::write(backup_path(&path), r#"{"a":1}"#).unwrap();
        fs::write(temp_path(&path), r#"{"a":2}"#).unwrap();

        let (mut store, reported) = open(&path);
        assert_eq!(store.get("a"), Some(json!(2)));
        assert!(reported[0].contains("is missing"), "{}", reported[0]);
        assert!(
            reported[0].contains("user-data.json.tmp"),
            "{}",
            reported[0]
        );

        store.save().expect("save");
        assert_eq!(read_json(&path), json!({ "a": 2 }));
        assert!(!temp_path(&path).exists());
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
        assert_eq!(reported.len(), 1, "{reported:?}");
        assert!(
            reported[0].contains("starting with an empty library list"),
            "{}",
            reported[0]
        );

        store.set("a".into(), json!(1));
        store.save().expect("save");
        assert_eq!(read_json(&path), json!({ "a": 1 }));
    }

    #[test]
    fn a_json_value_that_is_not_an_object_is_not_a_store() {
        let dir = scratch();
        let path = dir.path().join("user-data.json");
        fs::write(&path, "[]").unwrap();
        let (_, reported) = open(&path);
        assert!(reported[0].contains("is not valid JSON"), "{}", reported[0]);
    }

    #[test]
    fn a_missing_store_with_no_copies_is_a_fresh_start() {
        let dir = scratch();
        let (entries, opened) = read_store(&dir.path().join("user-data.json"));
        assert_eq!(opened, Opened::Fresh);
        assert!(entries.is_empty());
    }

    #[test]
    fn remove_backup_removes_it_and_is_fine_without_one() {
        let dir = scratch();
        let path = dir.path().join("user-data.json");
        let (mut store, _) = open(&path);
        store.save().unwrap();
        store.save().unwrap();
        assert!(backup_path(&path).exists());
        store.remove_backup().expect("removed");
        assert!(!backup_path(&path).exists());
        store.remove_backup().expect("nothing to remove");
    }

    #[test]
    fn user_data_opens_again_when_the_path_changes() {
        let dir = scratch();
        let first = dir.path().join("one").join("user-data.json");
        let second = dir.path().join("two").join("user-data.json");
        fs::create_dir_all(second.parent().unwrap()).unwrap();
        fs::write(&second, r#"{"where":"two"}"#).unwrap();

        let data = UserData::default();
        data.with(
            &first,
            |_| {},
            |s| {
                s.set("where".into(), json!("one"));
                s.save()
            },
        )
        .expect("saved");
        let seen = data.with(&second, |_| {}, |s| s.get("where"));
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
        let saved = data.with(
            &path,
            |_| {},
            |s| {
                s.set("a".into(), json!(1));
                s.save()
            },
        );
        assert!(saved.is_err(), "the folder cannot be made");

        fs::remove_file(&blocker).unwrap();
        data.save_unsaved()
            .expect("unsaved")
            .expect("saved at exit");
        assert_eq!(read_json(&path), json!({ "a": 1 }));
        assert!(data.save_unsaved().is_none(), "saved once");
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
        read_store(&dir.path().join("user-data.json"))
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
