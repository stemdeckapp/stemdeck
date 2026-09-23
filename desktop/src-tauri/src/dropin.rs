//! Files dragged into StemDeck from the desktop, where the WebView cannot see
//! them.
//!
//! On Windows and macOS an OS file drop reaches the page as an ordinary HTML5
//! `drop` with real `File` objects, and `main.js` handles it there. WebKitGTK,
//! the Linux WebView, does not do that. A drag from a GTK file manager reports
//! its types as `text/uri-list` and `text/html`, never `Files`; at drop time
//! `dataTransfer.files` is empty and the `file://` URI is blanked out of
//! `text/uri-list`. There is no way for the page to get the file. What it does
//! get, if it does nothing, is WebKit's default action: the window navigates
//! to the file and becomes a bare media player with no way back, or a drop on
//! the URL box types the file's URI into it (#672).
//!
//! So on Linux the native drop handler is switched on for the main window (see
//! [`NATIVE_FILE_DROP`]), and the drop is taken here instead. Tauri's handler
//! returns `true` on a drop, which consumes it before WebKit's default runs,
//! and wry only claims drags that carry a file `text/uri-list`, so the
//! library's own HTML5 drags between folders, lanes and the trash are not
//! affected.
//!
//! ## Paths stay on this side
//!
//! The page is served over http by the Python backend, which Tauri treats as a
//! remote origin, so app-defined commands are callable from it without an ACL.
//! The rule that follows, and that `dragout` and `download_to_path` already
//! keep, is that no command accepts a filesystem path from JavaScript. Here a
//! drop is recorded against opaque ids and the page is told only a name and a
//! size for each. It can read back a file the user dropped, once, by id, and
//! nothing else.
//!
//! ## Why a waiting command rather than an event
//!
//! A remote origin cannot `listen` to Tauri events without a remote capability,
//! and granting one would open every core event command to the page. The page
//! already talks to this process only through commands, so it asks for the
//! next drop signal and the command resolves when there is one.

use std::collections::{HashMap, VecDeque};
use std::fs;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::sync::{Condvar, Mutex};
use std::time::Duration;

use serde::Serialize;

/// Whether the main window takes OS file drops natively.
///
/// Linux only. On Windows, WebView2 stops delivering HTML5 `dragover` and
/// `drop` to the page while the native handler is on, which broke the
/// library's own drags and is why `tauri.conf.json` turns it off (#24). On
/// macOS the HTML5 path already carries the files. Neither needs this module.
pub const NATIVE_FILE_DROP: bool = cfg!(target_os = "linux");

/// The largest file that will be read back into the page.
///
/// Mirrors the server's `_MAX_UPLOAD_BYTES` and `MAX_UPLOAD_BYTES` in
/// `static/js/main.js`; `tests/test_upload_limit_parity.py` holds all three to
/// the same number. The page refuses larger files before it asks for them, so
/// this is the backstop that keeps a stale or hostile caller from reading an
/// arbitrarily large file into memory.
pub const MAX_DROPPED_FILE_BYTES: u64 = 400 * 1024 * 1024;

/// How long `next_signal` waits before answering "nothing yet".
///
/// Long enough that an idle window costs almost nothing, short enough that a
/// waiting command never outlives the app by much when it quits.
pub const WAIT: Duration = Duration::from_secs(25);

/// Signals kept for the page to catch up on. Nothing older than a handful is
/// still useful, and a page that has stopped asking must not make this grow.
const LOG_LIMIT: usize = 16;

/// What the page is told about one dropped file.
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct DroppedFile {
    pub id: u64,
    pub name: String,
    pub size: u64,
}

/// One thing that happened to a drag over the window.
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
#[serde(tag = "kind", rename_all = "camelCase")]
pub enum DropSignal {
    Enter,
    Leave,
    Drop { files: Vec<DroppedFile> },
}

/// The answer to "anything since `after`?".
///
/// `seq` is always the cursor to ask from next, including when there is no
/// signal, so a page that has only just started never misses one that lands
/// between two of its calls.
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct NextSignal {
    pub seq: u64,
    pub signal: Option<DropSignal>,
}

#[derive(Default)]
struct Inner {
    next_id: u64,
    /// Files from the most recent drop that the page has not read yet.
    pending: HashMap<u64, PathBuf>,
    /// Sequence number of the newest signal; 0 before there has been one.
    seq: u64,
    log: VecDeque<(u64, DropSignal)>,
}

/// Where native drops wait for the page.
///
/// A log read by cursor rather than a queue that is popped. A reload leaves
/// the old page's call still waiting in here with nobody left to answer, and a
/// popped queue would hand it the next drop, which the new page would never
/// see. With a cursor, every caller sees every signal after its own position.
#[derive(Default)]
pub struct DropInbox {
    inner: Mutex<Inner>,
    ready: Condvar,
}

impl DropInbox {
    /// Every change to `Inner` is a single step, so a panic elsewhere while the
    /// lock was held cannot have left it half-updated. Carrying on with the
    /// data is correct, and it keeps drops from being refused for good because
    /// of an unrelated failure.
    fn lock(&self) -> std::sync::MutexGuard<'_, Inner> {
        self.inner
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }

    fn publish(&self, inner: &mut Inner, signal: DropSignal) {
        inner.seq += 1;
        let seq = inner.seq;
        if inner.log.len() >= LOG_LIMIT {
            inner.log.pop_front();
        }
        inner.log.push_back((seq, signal));
        self.ready.notify_all();
    }

    pub fn enter(&self) {
        let mut inner = self.lock();
        self.publish(&mut inner, DropSignal::Enter);
    }

    pub fn leave(&self) {
        let mut inner = self.lock();
        self.publish(&mut inner, DropSignal::Leave);
    }

    /// Records a drop and publishes it.
    ///
    /// Replaces whatever an earlier drop left unread: the page only ever acts
    /// on the latest drop, so older paths would sit here for the life of the
    /// app, still readable, for no reason.
    pub fn drop_paths(&self, paths: &[PathBuf]) -> Vec<DroppedFile> {
        // Looked at before taking the lock: a stat can be slow on a network
        // mount, and nothing else needs to wait for it.
        let described: Vec<(String, u64)> = paths
            .iter()
            .map(|path| {
                (
                    display_name(path),
                    // A folder, or something gone by the time it is looked
                    // at, reports 0. The page screens by name and size, so a
                    // folder is refused as "not audio" as the HTML5 path does.
                    fs::metadata(path)
                        .ok()
                        .filter(|m| m.is_file())
                        .map_or(0, |m| m.len()),
                )
            })
            .collect();

        let mut inner = self.lock();
        inner.pending.clear();
        let mut files = Vec::with_capacity(paths.len());
        for (path, (name, size)) in paths.iter().zip(described) {
            inner.next_id += 1;
            let id = inner.next_id;
            inner.pending.insert(id, path.clone());
            files.push(DroppedFile { id, name, size });
        }
        self.publish(
            &mut inner,
            DropSignal::Drop {
                files: files.clone(),
            },
        );
        files
    }

    /// The first signal after `after`, waiting up to `wait` for one.
    ///
    /// `after: None` means "from now", for a page that has no cursor yet.
    pub fn next_signal(&self, after: Option<u64>, wait: Duration) -> NextSignal {
        let inner = self.lock();
        let after = after.unwrap_or(inner.seq);
        let (inner, _) = self
            .ready
            .wait_timeout_while(inner, wait, |i| i.seq <= after)
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        match inner.log.iter().find(|(seq, _)| *seq > after) {
            Some((seq, signal)) => NextSignal {
                seq: *seq,
                signal: Some(signal.clone()),
            },
            None => NextSignal {
                seq: inner.seq.max(after),
                signal: None,
            },
        }
    }

    /// Hands over the path behind `id`, once.
    pub fn take(&self, id: u64) -> Option<PathBuf> {
        self.lock().pending.remove(&id)
    }
}

/// The name the page shows for a dropped path. Only the last component: the
/// directory it came from is none of the page's business.
fn display_name(path: &Path) -> String {
    path.file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default()
}

/// Reads a dropped file, refusing anything that is not a regular file within
/// `max` bytes.
///
/// The size is checked against the open handle rather than a separate stat,
/// and the read is capped as well, so a file that grows between the two cannot
/// get past the limit.
pub fn read_dropped(path: &Path, max: u64) -> Result<Vec<u8>, String> {
    let file = fs::File::open(path).map_err(|e| format!("could not open the file: {e}"))?;
    let meta = file
        .metadata()
        .map_err(|e| format!("could not read the file: {e}"))?;
    if !meta.is_file() {
        return Err("not a file".to_string());
    }
    if meta.len() > max {
        return Err(format!("file is larger than {max} bytes"));
    }
    let mut bytes = Vec::with_capacity(meta.len() as usize);
    file.take(max + 1)
        .read_to_end(&mut bytes)
        .map_err(|e| format!("could not read the file: {e}"))?;
    if bytes.len() as u64 > max {
        return Err(format!("file is larger than {max} bytes"));
    }
    Ok(bytes)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    fn file_with(dir: &Path, name: &str, bytes: &[u8]) -> PathBuf {
        let path = dir.join(name);
        fs::File::create(&path).unwrap().write_all(bytes).unwrap();
        path
    }

    #[test]
    fn a_drop_tells_the_page_names_and_sizes_but_not_paths() {
        let dir = tempfile::tempdir().unwrap();
        let a = file_with(dir.path(), "Take 3.flac", b"abcd");
        let inbox = DropInbox::default();

        let files = inbox.drop_paths(&[a]);

        assert_eq!(files.len(), 1);
        assert_eq!(files[0].name, "Take 3.flac");
        assert_eq!(files[0].size, 4);
        // The serialised form is what crosses to the page.
        let json = serde_json::to_string(&inbox.next_signal(Some(0), WAIT)).unwrap();
        assert!(json.contains("\"kind\":\"drop\""), "{json}");
        assert!(!json.contains(dir.path().to_str().unwrap()), "{json}");
    }

    #[test]
    fn a_folder_is_reported_with_no_size_and_cannot_be_read() {
        let dir = tempfile::tempdir().unwrap();
        let inbox = DropInbox::default();

        let files = inbox.drop_paths(&[dir.path().to_path_buf()]);

        assert_eq!(files[0].size, 0);
        let path = inbox.take(files[0].id).unwrap();
        // Refused either way, by a different step per platform: Windows will
        // not open a directory at all, and Linux opens it and then fails the
        // regular-file check.
        assert!(read_dropped(&path, MAX_DROPPED_FILE_BYTES).is_err());
    }

    #[test]
    fn a_dropped_file_can_be_read_once() {
        let dir = tempfile::tempdir().unwrap();
        let a = file_with(dir.path(), "a.wav", b"RIFF");
        let inbox = DropInbox::default();
        let id = inbox.drop_paths(&[a])[0].id;

        let path = inbox.take(id).expect("first read is allowed");
        assert_eq!(
            read_dropped(&path, MAX_DROPPED_FILE_BYTES).unwrap(),
            b"RIFF"
        );
        assert!(inbox.take(id).is_none(), "a second read is not");
    }

    #[test]
    fn an_id_that_was_never_dropped_reads_nothing() {
        let inbox = DropInbox::default();
        assert!(inbox.take(1).is_none());
        assert!(inbox.take(u64::MAX).is_none());
    }

    #[test]
    fn a_new_drop_retires_what_the_last_one_left_unread() {
        let dir = tempfile::tempdir().unwrap();
        let a = file_with(dir.path(), "a.wav", b"a");
        let b = file_with(dir.path(), "b.wav", b"b");
        let inbox = DropInbox::default();

        let first = inbox.drop_paths(&[a])[0].id;
        let second = inbox.drop_paths(&[b])[0].id;

        assert_ne!(first, second, "ids are never reused");
        assert!(inbox.take(first).is_none());
        assert!(inbox.take(second).is_some());
    }

    /// Reads everything after `from`, the way the page walks the log.
    fn drain(inbox: &DropInbox, mut from: u64) -> Vec<DropSignal> {
        let mut got = vec![];
        loop {
            let next = inbox.next_signal(Some(from), Duration::from_millis(1));
            match next.signal {
                Some(signal) => got.push(signal),
                None => return got,
            }
            from = next.seq;
        }
    }

    #[test]
    fn signals_arrive_in_order() {
        let inbox = DropInbox::default();
        inbox.enter();
        inbox.leave();
        inbox.enter();
        inbox.drop_paths(&[]);

        assert_eq!(
            drain(&inbox, 0),
            vec![
                DropSignal::Enter,
                DropSignal::Leave,
                DropSignal::Enter,
                DropSignal::Drop { files: vec![] },
            ]
        );
    }

    #[test]
    fn waiting_with_nothing_to_say_still_returns_a_cursor() {
        let inbox = DropInbox::default();
        inbox.enter();
        let next = inbox.next_signal(None, Duration::from_millis(20));
        assert_eq!(
            next,
            NextSignal {
                seq: 1,
                signal: None
            }
        );
    }

    #[test]
    fn a_waiting_caller_is_woken_by_a_drop() {
        let inbox = std::sync::Arc::new(DropInbox::default());
        let waiter = {
            let inbox = inbox.clone();
            std::thread::spawn(move || inbox.next_signal(Some(0), Duration::from_secs(10)))
        };
        std::thread::sleep(Duration::from_millis(50));
        inbox.enter();
        assert_eq!(waiter.join().unwrap().signal, Some(DropSignal::Enter));
    }

    #[test]
    fn a_stale_caller_does_not_take_the_signal_from_a_new_one() {
        // A reload leaves the old page's call waiting. The drop that follows
        // has to reach the new page as well, not only the caller that is gone.
        let inbox = DropInbox::default();
        let old_page = inbox.next_signal(None, Duration::from_millis(1)).seq;
        let new_page = inbox.next_signal(None, Duration::from_millis(1)).seq;
        inbox.drop_paths(&[]);

        assert!(inbox.next_signal(Some(old_page), WAIT).signal.is_some());
        assert!(inbox.next_signal(Some(new_page), WAIT).signal.is_some());
    }

    #[test]
    fn a_page_that_stops_asking_cannot_grow_the_log() {
        let inbox = DropInbox::default();
        for _ in 0..(LOG_LIMIT * 4) {
            inbox.enter();
        }
        assert_eq!(drain(&inbox, 0).len(), LOG_LIMIT);
    }

    #[test]
    fn a_file_over_the_limit_is_refused_without_reading_it() {
        let dir = tempfile::tempdir().unwrap();
        let a = file_with(dir.path(), "a.wav", b"12345");
        assert!(read_dropped(&a, 4).unwrap_err().contains("larger"));
        assert_eq!(read_dropped(&a, 5).unwrap(), b"12345");
    }
}
