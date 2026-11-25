//! Opt-in crash and error reports, sent to Sentry from the desktop shell only.
//!
//! Off by default. Nothing in this module exists at run time until the person
//! turns on Settings, Privacy, "Send crash reports": no Sentry client, no hub
//! binding, no worker thread, no queue. The panic hook installed at start-up
//! only looks for a reporter, and there is none while consent is off.
//!
//! What a report may hold is decided here, by allow-list, not by trusting the
//! SDK's defaults. `scrub_event` builds a new event from a short list of
//! fields and throws the rest away, so a field the SDK adds in a later version
//! is dropped instead of sent. Text that can carry personal data (messages,
//! exception values, function names) goes through `scrub_text`, which takes
//! out paths, file names, web addresses, email addresses, IP addresses, long
//! identifiers and this machine's user and computer names.
//!
//! Withdrawal is immediate. Each enable makes its own gate; `disable` closes
//! it, unbinds the client from the hub and closes the client. The transport
//! checks the gate when an event is queued and again just before it is sent,
//! so nothing queued before withdrawal leaves afterwards. A request already on
//! the wire at that instant cannot be recalled, and docs/privacy.md says so.
//!
//! Docker, server and source runs never reach this code: it lives in the
//! Tauri shell, and the web page only forwards to it through a Tauri command.

use std::borrow::Cow;
use std::cell::Cell;
use std::collections::hash_map::DefaultHasher;
use std::collections::{BTreeMap, HashSet, VecDeque};
use std::hash::{Hash, Hasher};
use std::panic::PanicHookInfo;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{sync_channel, Receiver, SyncSender};
use std::sync::{Arc, Condvar, Mutex, MutexGuard, OnceLock};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use regex::Regex;
use sentry::integrations::backtrace::ProcessStacktraceIntegration;
use sentry::integrations::panic::PanicIntegration;
use sentry::protocol::{
    ClientSdkInfo, Context, DeviceContext, Event, Exception, Frame, Level, Mechanism, OsContext,
    Stacktrace,
};
use sentry::types::Dsn;
use sentry::{Client, ClientOptions, Envelope, Hub, Transport};
use serde::Deserialize;

/// Where reports go, baked in when the app is compiled: the release
/// workflows set STEMDECK_SENTRY_DSN from a repository secret for the build
/// step only (build.rs reruns when it changes). A DSN only allows sending
/// reports, not reading them. A build from source has none, so crash reports
/// do not exist in it at all: no client can be made and nothing is sent.
const BUILD_DSN: Option<&str> = option_env!("STEMDECK_SENTRY_DSN");

/// A DSN from the build, if it is one this app will send to: trimmed,
/// https, and parsing as a DSN. Empty or malformed counts as none.
pub fn parse_dsn(raw: Option<&str>) -> Option<Dsn> {
    let raw = raw?.trim();
    if raw.is_empty() || !raw.starts_with("https://") {
        return None;
    }
    raw.parse::<Dsn>().ok()
}

/// This build's DSN, or None when crash reports are not available in it.
pub fn build_dsn() -> Option<Dsn> {
    parse_dsn(BUILD_DSN)
}

/// Whether this build can send crash reports at all.
pub fn available() -> bool {
    build_dsn().is_some()
}

/// The version of the consent text the person agreed to. Bump it whenever the
/// Settings text changes what is collected, together with
/// CRASH_CONSENT_VERSION in static/js/crashReports.js: a stored consent with
/// another version reads as off, so everyone is asked again.
pub const CONSENT_VERSION: u32 = 1;

/// Store key of the consent record. Written only by `set_crash_reports`;
/// store_set refuses it, so the page cannot switch reports on by itself.
pub const CONSENT_KEY: &str = "stemdeck:crash-reports-consent";

const MAX_INPUT_CHARS: usize = 8 * 1024;
const MAX_MESSAGE_CHARS: usize = 1024;
const MAX_TYPE_CHARS: usize = 128;
const MAX_SYMBOL_CHARS: usize = 256;
const MAX_STACK_CHARS: usize = 16 * 1024;
const MAX_FRAMES: usize = 64;
const MAX_EXCEPTIONS: usize = 4;
/// Reports per rolling minute, shell and web together.
const MAX_PER_MINUTE: usize = 10;
/// Distinct reports remembered for the duplicate check; past this, new ones
/// are dropped rather than the memory growing.
const MAX_SEEN: usize = 256;
const QUEUE_CAPACITY: usize = 8;
const PANIC_FLUSH: Duration = Duration::from_secs(2);
const TRUNCATED: &str = "[truncated]";

// ── Consent record ──────────────────────────────────────────────────────────

/// The consent record written to the desktop store: whether reports are on,
/// the version of the text the choice was made on, and when (Unix seconds).
pub fn consent_record(on: bool, at: u64) -> serde_json::Value {
    serde_json::json!({ "on": on, "version": CONSENT_VERSION, "at": at })
}

/// When consent was given, if the stored record is a current, valid "on".
/// A record from another text version, a malformed one, or none: None.
pub fn consent_given(value: Option<&serde_json::Value>) -> Option<u64> {
    let v = value?;
    let on = v.get("on")?.as_bool()?;
    let version = v.get("version")?.as_u64()?;
    let at = v.get("at")?.as_u64()?;
    (on && version == u64::from(CONSENT_VERSION)).then_some(at)
}

pub fn now_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

// ── What a report may say about the machine ─────────────────────────────────

/// Literal strings that identify this machine or person, replaced wherever
/// they appear in text: the home folder, the user name, the computer name.
#[derive(Clone, Default)]
pub struct Redactions {
    rules: Vec<(Regex, &'static str)>,
}

impl Redactions {
    pub fn new(home: Option<&str>, user: Option<&str>, host: Option<&str>) -> Self {
        let mut rules = Vec::new();
        // The home folder first, in both slash forms: it contains the user name.
        if let Some(home) = home.filter(|h| h.len() >= 3) {
            let forward = home.replace('\\', "/");
            let back = home.replace('/', "\\");
            for form in [home.to_string(), forward, back] {
                if let Ok(re) = Regex::new(&format!("(?i){}", regex::escape(&form))) {
                    rules.push((re, "<home>"));
                }
            }
        }
        for (value, label) in [(user, "<user>"), (host, "<host>")] {
            // Shorter names would match inside ordinary words.
            if let Some(v) = value.map(str::trim).filter(|v| v.chars().count() >= 3) {
                if let Ok(re) = Regex::new(&format!("(?i){}", regex::escape(v))) {
                    rules.push((re, label));
                }
            }
        }
        Redactions { rules }
    }

    /// This process's own home folder, user name and computer name.
    pub fn from_env() -> Self {
        let var = |names: &[&str]| {
            names
                .iter()
                .find_map(|n| std::env::var(n).ok().filter(|v| !v.is_empty()))
        };
        Redactions::new(
            var(&["USERPROFILE", "HOME"]).as_deref(),
            var(&["USERNAME", "USER", "LOGNAME"]).as_deref(),
            var(&["COMPUTERNAME", "HOSTNAME"]).as_deref(),
        )
    }

    fn apply(&self, text: &str) -> String {
        let mut out = text.to_string();
        for (re, label) in &self.rules {
            out = re.replace_all(&out, *label).into_owned();
        }
        out
    }

    fn matches(&self, text: &str) -> bool {
        self.rules.iter().any(|(re, _)| re.is_match(text))
    }
}

/// Everything a report says about where it came from. Coarse on purpose.
#[derive(Clone)]
pub struct Facts {
    pub os: &'static str,
    pub arch: &'static str,
    pub gpu: Option<&'static str>,
    pub release: String,
    pub environment: &'static str,
    pub redact: Redactions,
}

impl Facts {
    /// The facts for this build, given the persisted torch device.
    pub fn for_this_build(torch_device: Option<&str>, redact: Redactions) -> Self {
        Facts {
            os: os_name(),
            arch: arch_name(),
            gpu: gpu_class(torch_device),
            release: format!("stemdeck@{}", env!("CARGO_PKG_VERSION")),
            environment: if cfg!(debug_assertions) {
                "development"
            } else {
                "desktop"
            },
            redact,
        }
    }
}

pub fn os_name() -> &'static str {
    match std::env::consts::OS {
        "windows" => "windows",
        "macos" => "macos",
        "linux" => "linux",
        _ => "other",
    }
}

pub fn arch_name() -> &'static str {
    match std::env::consts::ARCH {
        "x86_64" => "x86_64",
        "aarch64" => "aarch64",
        _ => "other",
    }
}

/// The GPU class tag, from the device setup persisted (config.json
/// torchDevice). "cuda" and not "nvidia": torch reports an AMD card on ROCm as
/// "cuda" too, and nothing persisted tells the two apart, so "nvidia" would be
/// wrong for AMD machines. Never a model name or a driver version.
pub fn gpu_class(torch_device: Option<&str>) -> Option<&'static str> {
    match torch_device? {
        "cuda" => Some("cuda"),
        "mps" => Some("mps"),
        "cpu" => Some("cpu"),
        _ => None,
    }
}

// ── Text scrubbing ──────────────────────────────────────────────────────────

fn regex(cell: &'static OnceLock<Regex>, pattern: &str) -> &'static Regex {
    cell.get_or_init(|| Regex::new(pattern).expect("telemetry pattern compiles"))
}

fn url_re() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    regex(&R, r#"(?i)\b[a-z][a-z0-9+.\-]*://[^\s"'<>()\[\]{}`]+"#)
}

fn opaque_uri_re() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    regex(
        &R,
        r#"(?i)\b(?:blob|data|mailto|javascript|file):[^\s"'<>`]+"#,
    )
}

fn bearer_re() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    regex(&R, r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]+")
}

fn secret_re() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    regex(
        &R,
        r#"(?i)\b(access_token|refresh_token|id_token|token|api_key|apikey|key|secret|password|passwd|pwd|auth|authorization|sessionid|session|signature|sig|code|cookie)=[^\s&"',;]+"#,
    )
}

fn email_re() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    regex(&R, r"(?i)[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}")
}

fn media_re() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    regex(
        &R,
        r"(?i)\.(?:mp3|wav|wave|flac|m4a|aac|ogg|oga|opus|aif|aiff|aifc|wma|alac|ape|wv|mp4|m4v|mov|mkv|webm|avi|lrc|mid|midi|zip|pdf|jpg|jpeg|png|gif|webp|heic|rpp|als|flp|logicx|ptx|cpr|txt)\b",
    )
}

fn ipv4_re() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    regex(&R, r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
}

fn ipv6_full_re() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    regex(&R, r"(?i)\b(?:[0-9a-f]{1,4}:){7}[0-9a-f]{1,4}\b")
}

fn ipv6_short_re() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    regex(
        &R,
        r"(?i)(?:\b[0-9a-f]{1,4})?(?::[0-9a-f]{1,4})*::(?:[0-9a-f]{1,4}(?::[0-9a-f]{1,4})*)?",
    )
}

fn uuid_re() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    regex(
        &R,
        r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    )
}

fn hex_id_re() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    regex(&R, r"(?i)\b[0-9a-f]{12,}\b")
}

/// `text` with everything personal taken out, at most `max` characters.
///
/// Errs on the side of removing too much: a path is taken to run to the end
/// of the line unless a clear separator (a quote, ": ", ", ", " (") ends it,
/// because folder and song names contain spaces, and a file name with an
/// audio or media extension takes the words before it back to the previous
/// separator. A report that says less is still useful; one that names a
/// person's song is a breach.
pub fn scrub_text(text: &str, redact: &Redactions, max: usize) -> String {
    let bounded: String = text.chars().take(MAX_INPUT_CHARS).collect();
    // blob:http://... first, or the inner http://... would be taken alone.
    let mut s = opaque_uri_re().replace_all(&bounded, "<url>").into_owned();
    s = url_re()
        .replace_all(&s, |c: &regex::Captures| scrub_url(&c[0]))
        .into_owned();
    s = bearer_re()
        .replace_all(&s, "Bearer <redacted>")
        .into_owned();
    s = secret_re().replace_all(&s, "$1=<redacted>").into_owned();
    s = email_re().replace_all(&s, "<email>").into_owned();
    s = redact_paths(&s);
    s = redact_media_names(&s);
    s = redact.apply(&s);
    s = ipv4_re()
        .replace_all(&s, |c: &regex::Captures| match &c[0] {
            "127.0.0.1" | "0.0.0.0" => c[0].to_string(),
            _ => "<ip>".to_string(),
        })
        .into_owned();
    s = ipv6_full_re().replace_all(&s, "<ip>").into_owned();
    s = ipv6_short_re()
        .replace_all(&s, |c: &regex::Captures| {
            let m = &c[0];
            if m != "::1" && m.chars().any(|ch| ch.is_ascii_digit()) {
                "<ip>".to_string()
            } else {
                m.to_string()
            }
        })
        .into_owned();
    s = uuid_re().replace_all(&s, "<id>").into_owned();
    s = hex_id_re().replace_all(&s, "<id>").into_owned();
    truncate_chars(&s, max)
}

/// At most `max` characters, the cut marked.
pub fn truncate_chars(s: &str, max: usize) -> String {
    if s.chars().count() <= max {
        return s.to_string();
    }
    let keep = max.saturating_sub(TRUNCATED.len());
    let mut out: String = s.chars().take(keep).collect();
    out.push_str(TRUNCATED);
    out
}

/// A web address, reduced to the path of StemDeck's own code when it is one
/// (`/static/js/catalog.js:10:5`, for a stack), else `<url>`. Query strings
/// and fragments never survive.
pub fn scrub_url(url: &str) -> String {
    let Some((scheme, rest)) = url.split_once("://") else {
        return "<url>".to_string();
    };
    let scheme = scheme.to_ascii_lowercase();
    if !matches!(scheme.as_str(), "http" | "https" | "tauri") {
        return "<url>".to_string();
    }
    let (authority, path) = match rest.find('/') {
        Some(i) => (&rest[..i], &rest[i..]),
        None => (rest, ""),
    };
    if authority.contains('@') {
        return "<url>".to_string();
    }
    // The host without its port: [::1]:8000 -> [::1], 127.0.0.1:8000 -> 127.0.0.1.
    let host = match authority.rsplit_once(':') {
        Some((h, port))
            if !port.is_empty()
                && port.chars().all(|c| c.is_ascii_digit())
                && (!h.starts_with('[') || h.ends_with(']')) =>
        {
            h
        }
        _ => authority,
    }
    .to_ascii_lowercase();
    let local = matches!(
        host.as_str(),
        "127.0.0.1" | "localhost" | "[::1]" | "tauri.localhost" | "ipc.localhost"
    );
    let path = path.split(['?', '#']).next().unwrap_or("");
    if local && is_code_path(path) {
        path.to_string()
    } else {
        "<url>".to_string()
    }
}

/// StemDeck's own web code: `/static/...` with nothing but plain path
/// characters (and `:line:col`).
fn is_code_path(path: &str) -> bool {
    path.starts_with("/static/")
        && path
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || "._/:-".contains(c))
}

fn is_boundary(prev: Option<char>) -> bool {
    match prev {
        None => true,
        Some(c) => c.is_whitespace() || "\"'`([{=,:;<>|".contains(c),
    }
}

#[derive(PartialEq)]
enum PathStart {
    Unix,
    Other,
}

fn path_start(c: &[char], i: usize) -> Option<PathStart> {
    let at = |k: usize| c.get(k).copied();
    let prev = if i == 0 { None } else { Some(c[i - 1]) };
    let ch = c[i];
    // C:\ or C:/, not inside a word.
    if ch.is_ascii_alphabetic()
        && at(i + 1) == Some(':')
        && matches!(at(i + 2), Some('\\') | Some('/'))
        && !prev.is_some_and(|p| p.is_alphanumeric())
    {
        return Some(PathStart::Other);
    }
    if !is_boundary(prev) {
        return None;
    }
    // \\server\share and \\?\C:\ paths.
    if ch == '\\' && at(i + 1) == Some('\\') {
        return Some(PathStart::Other);
    }
    // ~/x and ~\x.
    if ch == '~' && matches!(at(i + 1), Some('/') | Some('\\')) {
        return Some(PathStart::Other);
    }
    // %USERPROFILE%\x and $HOME/x.
    if ch == '%' || ch == '$' {
        let mut k = i + 1;
        while at(k).is_some_and(|x| x.is_ascii_alphanumeric() || x == '_') && k - i < 40 {
            k += 1;
        }
        if ch == '%' && at(k) == Some('%') {
            k += 1;
        }
        if k > i + 1 && matches!(at(k), Some('/') | Some('\\')) {
            return Some(PathStart::Other);
        }
        return None;
    }
    if ch == '/' && at(i + 1).is_some_and(|x| x.is_alphanumeric() || "._~-".contains(x)) {
        return Some(PathStart::Unix);
    }
    None
}

/// Whether the bracket group opening at `open` is followed, before the next
/// space, by a path separator: then it is a folder name, not a remark.
fn group_continues_path(c: &[char], open: usize) -> bool {
    let Some(close) = c[open..].iter().position(|&x| x == ')').map(|k| open + k) else {
        return false;
    };
    c[close + 1..]
        .iter()
        .take_while(|x| !x.is_whitespace())
        .any(|&x| x == '/' || x == '\\')
}

/// Where a path that starts at `i` ends.
fn path_end(c: &[char], i: usize) -> usize {
    let mut j = i;
    while j < c.len() {
        let ch = c[j];
        if "\"'`<>|\r\n\t".contains(ch) {
            break;
        }
        let next_space = c.get(j + 1).is_none_or(|n| n.is_whitespace());
        if matches!(ch, ':' | ',' | ';') && next_space && j > i + 2 {
            break;
        }
        // " (os error 5)" ends a path; " (Live)\Take 2.wav" is still inside it.
        if ch == ' ' && c.get(j + 1) == Some(&'(') && !group_continues_path(c, j + 1) {
            break;
        }
        j += 1;
    }
    // Sentence punctuation and a closing bracket the path did not open stay
    // outside it.
    while j > i {
        let last = c[j - 1];
        let opened = |open: char| c[i..j].contains(&open);
        if last.is_whitespace()
            || last == '.'
            || (last == ')' && !opened('('))
            || (last == ']' && !opened('['))
        {
            j -= 1;
        } else {
            break;
        }
    }
    j
}

fn redact_paths(s: &str) -> String {
    let c: Vec<char> = s.chars().collect();
    let mut out = String::with_capacity(s.len());
    let mut i = 0;
    while i < c.len() {
        if let Some(kind) = path_start(&c, i) {
            let end = path_end(&c, i);
            if kind == PathStart::Unix {
                // Decided on the first word: "/api 1/2" is not a path, and
                // "/static/js/a.js:1:2" is StemDeck's own code.
                let word_end = (i..end).find(|&k| c[k].is_whitespace()).unwrap_or(end);
                let word: String = c[i..word_end].iter().collect();
                if is_code_path(&word) || !word[1..].contains(['/', '\\']) {
                    out.push_str(&word);
                    i = word_end.max(i + 1);
                    continue;
                }
            }
            out.push_str("<path>");
            i = end.max(i + 1);
            continue;
        }
        out.push(c[i]);
        i += 1;
    }
    out
}

/// "Bohemian Rhapsody.mp3" -> "<file>": the extension, and every word before
/// it back to the previous separator, since titles have spaces.
fn redact_media_names(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut copied = 0;
    for m in media_re().find_iter(s) {
        if m.start() < copied {
            continue;
        }
        let before = &s[copied..m.start()];
        let cut = before
            .char_indices()
            .rev()
            // Not brackets: "My Song (Live).wav" is one title.
            .find(|(_, ch)| "\"'`<>[{=,:;\r\n\t".contains(*ch))
            .map_or(0, |(k, ch)| k + ch.len_utf8());
        let head = &before[..cut];
        let gap = before[cut..].len() - before[cut..].trim_start().len();
        out.push_str(head);
        out.push_str(&before[cut..cut + gap]);
        out.push_str("<file>");
        copied = m.end();
    }
    out.push_str(&s[copied..]);
    out
}

/// A module or package name as the SDK sets it (`std::panicking`,
/// `stemdeck`), kept only when it is a plain code identifier: nothing that
/// could be a path, a URL, a file name or this machine's names.
fn code_name(name: &str, redact: &Redactions) -> Option<String> {
    let plain = !name.is_empty()
        && name.chars().count() <= MAX_TYPE_CHARS
        && name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || "_:$-".contains(c))
        && !redact.matches(name);
    plain.then(|| name.to_string())
}

// ── Building the event that is actually sent ────────────────────────────────

/// A frame as it may leave: the function (scrubbed), the module and package
/// when they are plain code names, the line, the column and whether it is
/// StemDeck's own code. No file name and no path of any kind (`abs_path`,
/// `filename`), for shell and web frames alike, whatever a caller put there.
/// Gone too: symbol (mangled), vars, source context lines, and every
/// address. Without debug images an address explains nothing, and it is one
/// more number particular to this process.
fn scrub_frame(f: Frame, redact: &Redactions) -> Frame {
    Frame {
        function: f
            .function
            .as_deref()
            .map(|s| scrub_text(s, redact, MAX_SYMBOL_CHARS)),
        module: f.module.as_deref().and_then(|m| code_name(m, redact)),
        package: f.package.as_deref().and_then(|p| code_name(p, redact)),
        lineno: f.lineno,
        colno: f.colno,
        in_app: f.in_app,
        ..Default::default()
    }
}

fn scrub_stacktrace(st: Stacktrace, redact: &Redactions) -> Option<Stacktrace> {
    // Sentry orders frames outermost first; the frames that matter most are
    // the last ones, so a long stack keeps its end.
    let skip = st.frames.len().saturating_sub(MAX_FRAMES);
    let frames: Vec<Frame> = st
        .frames
        .into_iter()
        .skip(skip)
        .map(|f| scrub_frame(f, redact))
        .collect();
    (!frames.is_empty()).then(|| Stacktrace {
        frames,
        ..Default::default()
    })
}

fn scrub_exception(e: Exception, redact: &Redactions) -> Exception {
    Exception {
        ty: scrub_text(&e.ty, redact, MAX_TYPE_CHARS),
        value: e
            .value
            .as_deref()
            .map(|v| scrub_text(v, redact, MAX_MESSAGE_CHARS)),
        stacktrace: e.stacktrace.and_then(|st| scrub_stacktrace(st, redact)),
        mechanism: e.mechanism.map(|m| Mechanism {
            ty: scrub_text(&m.ty, redact, MAX_TYPE_CHARS),
            handled: m.handled,
            synthetic: m.synthetic,
            ..Default::default()
        }),
        ..Default::default()
    }
}

/// The event as it may leave the machine: rebuilt from an allow-list.
///
/// Kept: event id (random per event), level, time, platform, the scrubbed
/// message and exceptions, release, environment, the SDK's name and version,
/// the tags os, gpu and component, and the os name and cpu architecture.
/// Everything else the SDK or a caller put in is dropped by construction:
/// user, request, server_name, breadcrumbs, extra, modules, threads,
/// debug_meta, fingerprint, culprit, transaction, logger, other contexts.
pub fn scrub_event(event: Event<'static>, facts: &Facts) -> Event<'static> {
    let component = match event.tags.get("component").map(String::as_str) {
        Some("web") => "web",
        _ => "shell",
    };
    let mut tags = BTreeMap::new();
    tags.insert("os".to_string(), facts.os.to_string());
    if let Some(gpu) = facts.gpu {
        tags.insert("gpu".to_string(), gpu.to_string());
    }
    tags.insert("component".to_string(), component.to_string());

    let mut contexts = BTreeMap::new();
    contexts.insert(
        "os".to_string(),
        Context::Os(Box::new(OsContext {
            name: Some(facts.os.to_string()),
            ..Default::default()
        })),
    );
    contexts.insert(
        "device".to_string(),
        Context::Device(Box::new(DeviceContext {
            arch: Some(facts.arch.to_string()),
            ..Default::default()
        })),
    );

    let redact = &facts.redact;
    let exception: Vec<Exception> = event
        .exception
        .values
        .into_iter()
        .take(MAX_EXCEPTIONS)
        .map(|e| scrub_exception(e, redact))
        .collect();
    Event {
        event_id: event.event_id,
        level: event.level,
        timestamp: event.timestamp,
        // "native" for the web reports too: Sentry fills in the sender's IP
        // address on its own for some browser platforms.
        platform: Cow::Borrowed("native"),
        message: event
            .message
            .as_deref()
            .map(|m| scrub_text(m, redact, MAX_MESSAGE_CHARS)),
        exception: exception.into(),
        release: Some(Cow::Owned(facts.release.clone())),
        environment: Some(Cow::Borrowed(facts.environment)),
        sdk: event.sdk.map(|sdk| {
            Cow::Owned(ClientSdkInfo {
                name: sdk.name.clone(),
                version: sdk.version.clone(),
                integrations: Vec::new(),
                packages: Vec::new(),
            })
        }),
        tags,
        contexts,
        ..Default::default()
    }
}

// ── Reports from the web page ───────────────────────────────────────────────

/// An error or unhandled rejection from the web UI, as the page sends it
/// (static/js/crashReports.js). Every field is scrubbed here again.
#[derive(Deserialize, Debug, Default, Clone)]
pub struct WebReport {
    /// "error" or "rejection".
    pub kind: String,
    pub name: Option<String>,
    pub message: String,
    pub stack: Option<String>,
}

/// One line of a JavaScript stack as a frame: V8's `at fn (url:1:2)` and
/// `at url:1:2`, and WebKit's `fn@url:1:2`. None for anything else, such as
/// the "TypeError: message" line V8 starts with.
fn js_frame(line: &str, redact: &Redactions) -> Option<Frame> {
    let line = line.trim();
    let (func, loc) = if let Some(rest) = line.strip_prefix("at ") {
        match rest.strip_suffix(')').and_then(|r| r.rsplit_once(" (")) {
            Some((f, l)) => (Some(f), l),
            None => (None, rest),
        }
    } else if let Some((f, l)) = line.split_once('@') {
        (Some(f).filter(|f| !f.is_empty()), l)
    } else {
        return None;
    };
    let mut parts = loc.rsplitn(3, ':');
    let col = parts.next()?.parse::<u64>().ok()?;
    let lineno = parts.next()?.parse::<u64>().ok()?;
    let target = parts.next()?;
    // The address is read only to tell StemDeck's own code from anything
    // else; neither it nor the file name goes into the frame.
    let own_code = target.contains("://") && scrub_url(target).starts_with("/static/");
    let function = func
        .map(|f| f.trim_start_matches("async ").trim())
        .filter(|f| !f.is_empty())
        .map(|f| scrub_text(f, redact, MAX_SYMBOL_CHARS));
    Some(Frame {
        function,
        in_app: Some(own_code),
        lineno: Some(lineno),
        colno: Some(col),
        ..Default::default()
    })
}

pub fn parse_js_stack(stack: &str, redact: &Redactions) -> Option<Stacktrace> {
    let bounded: String = stack.chars().take(MAX_STACK_CHARS).collect();
    let frames: Vec<Frame> = bounded
        .lines()
        .filter_map(|l| js_frame(l, redact))
        .take(MAX_FRAMES)
        .collect();
    // JavaScript lists the innermost call first, Sentry the outermost.
    Stacktrace::from_frames_reversed(frames)
}

/// The event for a web report, before `scrub_event`.
pub fn web_event(report: &WebReport, redact: &Redactions) -> Event<'static> {
    let (mechanism, level) = match report.kind.as_str() {
        "rejection" => ("onunhandledrejection", Level::Error),
        _ => ("onerror", Level::Error),
    };
    let ty = report
        .name
        .as_deref()
        .map(str::trim)
        .filter(|n| !n.is_empty())
        .unwrap_or("Error");
    let mut tags = BTreeMap::new();
    tags.insert("component".to_string(), "web".to_string());
    Event {
        level,
        exception: vec![Exception {
            ty: scrub_text(ty, redact, MAX_TYPE_CHARS),
            value: Some(scrub_text(&report.message, redact, MAX_MESSAGE_CHARS)),
            stacktrace: report
                .stack
                .as_deref()
                .and_then(|s| parse_js_stack(s, redact)),
            mechanism: Some(Mechanism {
                ty: mechanism.to_string(),
                handled: Some(false),
                ..Default::default()
            }),
            ..Default::default()
        }]
        .into(),
        tags,
        ..Default::default()
    }
}

// ── The example report in Settings, Privacy ─────────────────────────────────

/// The made-up error the example is built from. Every personal-looking part
/// is fake, and each is one the scrubber must take out: a home folder path,
/// a song file name, an email address, a web address with a query string.
pub fn example_input() -> WebReport {
    WebReport {
        kind: "error".to_string(),
        name: Some("TypeError".to_string()),
        message: r"Could not open C:\Users\example-user\Music\Example Artist - Example Song.mp3 for example@example.com (from https://example.com/song?token=example-token)".to_string(),
        stack: Some(
            "TypeError: Could not open\n    at openSong (http://127.0.0.1:8000/static/js/player.js?v=1:120:9)\n    at https://example.com/x.js?token=example-token:1:1"
                .to_string(),
        ),
    }
}

/// The example's fixed report ID and time, so it is plainly not a real one:
/// all zeros, and 2026-01-01 00:00:00 UTC.
const EXAMPLE_EVENT_ID: sentry::types::Uuid = sentry::types::Uuid::nil();
const EXAMPLE_TIME_SECS: u64 = 1_767_225_600;

/// The SDK's own name and version, as the client puts them on every event.
fn sdk_info() -> ClientSdkInfo {
    let agent = ClientOptions::new().user_agent;
    let (name, version) = agent.split_once('/').unwrap_or((&agent, ""));
    ClientSdkInfo {
        name: name.to_string(),
        version: version.to_string(),
        integrations: Vec::new(),
        packages: Vec::new(),
    }
}

/// The example event: the made-up error, through the same steps a real web
/// report takes (`web_event`, the SDK info the client adds, `scrub_event`).
/// Sends nothing and needs no client, no consent and no network.
pub fn example_event(facts: &Facts) -> Event<'static> {
    let mut event = web_event(&example_input(), &facts.redact);
    event.event_id = EXAMPLE_EVENT_ID;
    event.timestamp = UNIX_EPOCH + Duration::from_secs(EXAMPLE_TIME_SECS);
    event.sdk = Some(Cow::Owned(sdk_info()));
    scrub_event(event, facts)
}

/// The example as Settings shows it: the event item's JSON, serialised as
/// the envelope serialises it, then laid out for reading.
pub fn example_report(facts: &Facts) -> String {
    serde_json::to_value(example_event(facts))
        .and_then(|v| serde_json::to_string_pretty(&v))
        .unwrap_or_default()
}

// ── Flood guard ─────────────────────────────────────────────────────────────

/// At most MAX_PER_MINUTE reports a minute, and each distinct report once per
/// run, so an error in a render loop sends one report, not thousands.
#[derive(Default)]
pub struct Limiter {
    sent: VecDeque<Instant>,
    seen: HashSet<u64>,
}

impl Limiter {
    pub fn allow(&mut self, key: &str, now: Instant) -> bool {
        while self
            .sent
            .front()
            .is_some_and(|t| now.duration_since(*t) >= Duration::from_secs(60))
        {
            self.sent.pop_front();
        }
        let mut h = DefaultHasher::new();
        key.hash(&mut h);
        let id = h.finish();
        if self.seen.contains(&id) || self.seen.len() >= MAX_SEEN {
            return false;
        }
        if self.sent.len() >= MAX_PER_MINUTE {
            return false;
        }
        self.seen.insert(id);
        self.sent.push_back(now);
        true
    }
}

/// What makes two reports the same one: where it came from, its type, its
/// message and its innermost frame. Built from scrubbed text.
fn dedupe_key(event: &Event<'static>) -> String {
    let component = event.tags.get("component").map_or("", String::as_str);
    let mut key = format!("{component}|{}", event.message.as_deref().unwrap_or(""));
    if let Some(e) = event.exception.values.first() {
        key.push('|');
        key.push_str(&e.ty);
        key.push('|');
        key.push_str(e.value.as_deref().unwrap_or(""));
        if let Some(f) = e.stacktrace.as_ref().and_then(|s| s.frames.last()) {
            key.push_str(&format!(
                "|{}|{}|{}",
                f.function.as_deref().unwrap_or(""),
                f.lineno.unwrap_or(0),
                f.colno.unwrap_or(0)
            ));
        }
    }
    key
}

// ── Transport ───────────────────────────────────────────────────────────────

/// What one attempt to send a report came to.
pub enum PostOutcome {
    Sent,
    /// The server asked for a pause (429, or a rate limit header).
    Pause(Duration),
    Failed,
}

/// Sends one serialized envelope. Runs on the transport's own thread.
pub type Poster = Box<dyn FnMut(&[u8]) -> PostOutcome + Send>;

/// The last report as it was sent, kept in memory only.
pub type LastReport = Arc<Mutex<Option<String>>>;

#[derive(Default)]
struct Pending {
    count: Mutex<usize>,
    idle: Condvar,
}

impl Pending {
    fn add(&self) {
        *lock(&self.count) += 1;
    }
    fn done(&self) {
        let mut n = lock(&self.count);
        *n = n.saturating_sub(1);
        self.idle.notify_all();
    }
    fn wait_idle(&self, timeout: Duration, gate: &AtomicBool) -> bool {
        let deadline = Instant::now() + timeout;
        let mut n = lock(&self.count);
        while *n > 0 && gate.load(Ordering::SeqCst) {
            let left = deadline.saturating_duration_since(Instant::now());
            if left.is_zero() {
                break;
            }
            n = match self.idle.wait_timeout(n, left) {
                Ok((g, _)) => g,
                Err(p) => p.into_inner().0,
            };
        }
        *n == 0
    }
}

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|p| p.into_inner())
}

/// Sends reports on a thread of its own, one at a time, from a small
/// bounded queue. Only event items leave: a client report or anything else
/// the SDK attaches is not sent.
pub struct HttpTransport {
    tx: Mutex<Option<SyncSender<Envelope>>>,
    gate: Arc<AtomicBool>,
    pending: Arc<Pending>,
}

impl HttpTransport {
    pub fn spawn(gate: Arc<AtomicBool>, last: LastReport, poster: Poster) -> Arc<Self> {
        let (tx, rx) = sync_channel::<Envelope>(QUEUE_CAPACITY);
        let pending = Arc::new(Pending::default());
        let worker_gate = gate.clone();
        let worker_pending = pending.clone();
        let spawned = thread::Builder::new()
            .name("crash-reports".to_string())
            .spawn(move || send_loop(rx, worker_gate, last, worker_pending, poster));
        Arc::new(HttpTransport {
            // No thread, no queue: reports are dropped rather than held.
            tx: Mutex::new(spawned.ok().map(|_| tx)),
            gate,
            pending,
        })
    }
}

fn send_loop(
    rx: Receiver<Envelope>,
    gate: Arc<AtomicBool>,
    last: LastReport,
    pending: Arc<Pending>,
    mut poster: Poster,
) {
    let mut paused_until: Option<Instant> = None;
    for envelope in rx {
        let paused = paused_until.is_some_and(|t| Instant::now() < t);
        if gate.load(Ordering::SeqCst) && !paused {
            let mut body = Vec::new();
            // Checked again here: withdrawal may have come while this waited.
            if envelope.to_writer(&mut body).is_ok() && gate.load(Ordering::SeqCst) {
                *lock(&last) = Some(pretty_envelope(&body));
                if let PostOutcome::Pause(d) = poster(&body) {
                    paused_until = Some(Instant::now() + d);
                }
            }
        }
        pending.done();
    }
}

impl Transport for HttpTransport {
    fn send_envelope(&self, envelope: Envelope) {
        if !self.gate.load(Ordering::SeqCst) {
            return;
        }
        let Some(event) = envelope.event().cloned() else {
            return;
        };
        let only_event = Envelope::from(event);
        self.pending.add();
        let queued = lock(&self.tx)
            .as_ref()
            .is_some_and(|tx| tx.try_send(only_event).is_ok());
        if !queued {
            self.pending.done();
        }
    }

    fn flush(&self, timeout: Duration) -> bool {
        self.pending.wait_idle(timeout, &self.gate)
    }

    /// Withdrawal: close the gate and drop the queue. Nothing is flushed.
    fn shutdown(&self, _timeout: Duration) -> bool {
        self.gate.store(false, Ordering::SeqCst);
        lock(&self.tx).take();
        true
    }
}

/// Each line of the envelope as readable JSON, for the Last report view.
/// The same content as sent, only laid out.
pub fn pretty_envelope(body: &[u8]) -> String {
    String::from_utf8_lossy(body)
        .lines()
        .filter(|l| !l.trim().is_empty())
        .map(|l| {
            serde_json::from_str::<serde_json::Value>(l)
                .ok()
                .and_then(|v| serde_json::to_string_pretty(&v).ok())
                .unwrap_or_else(|| l.to_string())
        })
        .collect::<Vec<_>>()
        .join("\n")
}

/// The real sender: one POST of the envelope with the existing reqwest
/// (rustls) client, built on the transport's own thread so it is never made
/// or dropped inside the async runtime. No redirects, so a report is never
/// handed on to another address; https only, outside tests.
pub fn http_poster(dsn: &Dsn, https_only: bool) -> Poster {
    let url = dsn.envelope_api_url().to_string();
    // "sentry.rust/<version>", the SDK's own client name.
    let agent = ClientOptions::new().user_agent;
    let auth = dsn.to_auth(Some(&agent)).to_string();
    let mut client: Option<reqwest::blocking::Client> = None;
    Box::new(move |body: &[u8]| {
        if client.is_none() {
            client = reqwest::blocking::Client::builder()
                .connect_timeout(Duration::from_secs(10))
                .timeout(Duration::from_secs(20))
                .redirect(reqwest::redirect::Policy::none())
                .https_only(https_only)
                .build()
                .ok();
        }
        let Some(c) = client.as_ref() else {
            return PostOutcome::Failed;
        };
        let sent = c
            .post(&url)
            .header("X-Sentry-Auth", &auth)
            .header("Content-Type", "application/x-sentry-envelope")
            .body(body.to_vec())
            .send();
        match sent {
            Ok(r) => {
                let secs = |name: &str| {
                    r.headers()
                        .get(name)
                        .and_then(|v| v.to_str().ok())
                        .and_then(|v| v.split([':', ',']).next())
                        .and_then(|v| v.trim().parse::<u64>().ok())
                };
                if r.status().as_u16() == 429 {
                    PostOutcome::Pause(Duration::from_secs(secs("retry-after").unwrap_or(60)))
                } else if let Some(s) = secs("x-sentry-rate-limits") {
                    PostOutcome::Pause(Duration::from_secs(s))
                } else if r.status().is_success() {
                    PostOutcome::Sent
                } else {
                    PostOutcome::Failed
                }
            }
            Err(_) => PostOutcome::Failed,
        }
    })
}

/// The production transport for `dsn`.
pub fn http_transport(dsn: &Dsn, gate: Arc<AtomicBool>, last: LastReport) -> Arc<dyn Transport> {
    HttpTransport::spawn(gate, last, http_poster(dsn, true))
}

// ── The reporter ────────────────────────────────────────────────────────────

#[derive(Default)]
struct State {
    client: Option<Arc<Client>>,
    gate: Option<Arc<AtomicBool>>,
    facts: Option<Facts>,
    limiter: Limiter,
}

/// Owns the Sentry client while consent is on, and nothing while it is off.
pub struct Reporter {
    hub: Arc<Hub>,
    on: AtomicBool,
    state: Mutex<State>,
    last: LastReport,
}

thread_local! {
    /// Set while this thread is building or capturing a report, so a panic
    /// inside that code is not reported again from the panic hook.
    static BUSY: Cell<bool> = const { Cell::new(false) };
}

struct Busy;
impl Busy {
    fn enter() -> Option<Busy> {
        BUSY.with(|b| {
            if b.get() {
                None
            } else {
                b.set(true);
                Some(Busy)
            }
        })
    }
}
impl Drop for Busy {
    fn drop(&mut self) {
        BUSY.with(|b| b.set(false));
    }
}

impl Reporter {
    pub fn new(hub: Arc<Hub>) -> Self {
        Reporter {
            hub,
            on: AtomicBool::new(false),
            state: Mutex::new(State::default()),
            last: Arc::new(Mutex::new(None)),
        }
    }

    pub fn is_on(&self) -> bool {
        self.on.load(Ordering::SeqCst)
    }

    pub fn last_report(&self) -> Option<String> {
        lock(&self.last).clone()
    }

    /// Start reporting: a new client bound to the hub, with a transport that
    /// has a gate of its own. With no DSN (a build without crash reports)
    /// this refuses before anything is made: no client, no transport.
    pub fn enable(
        &self,
        dsn: Option<Dsn>,
        facts: Facts,
        make_transport: impl FnOnce(&Dsn, Arc<AtomicBool>, LastReport) -> Arc<dyn Transport>,
    ) -> Result<(), String> {
        let dsn = dsn.ok_or_else(|| "crash reports are not available in this build".to_string())?;
        self.disable();
        let mut state = lock(&self.state);
        let gate = Arc::new(AtomicBool::new(true));
        let transport = make_transport(&dsn, gate.clone(), self.last.clone());
        let before_facts = facts.clone();

        let mut options = ClientOptions::new();
        options.dsn = Some(dsn);
        options.release = Some(Cow::Owned(facts.release.clone()));
        options.environment = Some(Cow::Borrowed(facts.environment));
        options.send_default_pii = false;
        options.server_name = None;
        options.max_breadcrumbs = 0;
        options.attach_stacktrace = false;
        options.auto_session_tracking = false;
        // None of the SDK's own integrations: no contexts (it reads the
        // hostname), no debug images (they list library paths), and the
        // panic hook is this module's own (install_panic_hook).
        options.default_integrations = false;
        options.integrations = vec![Arc::new(ProcessStacktraceIntegration)];
        options.before_send = Some(Arc::new(move |event| {
            Some(scrub_event(event, &before_facts))
        }));
        options.before_breadcrumb = Some(Arc::new(|_| None));
        // No Sentry Logs. The logs feature is not compiled in at all, so no
        // log can be captured; this is the second lock on that door.
        options.before_send_log = Some(Arc::new(|_| None));
        options.before_send_metric = Some(Arc::new(|_| None));
        options.transport = Some(Arc::new(move |_: &ClientOptions| transport.clone()));

        let client = Arc::new(Client::with_options(options));
        self.hub.bind_client(Some(client.clone()));
        state.client = Some(client);
        state.gate = Some(gate);
        state.facts = Some(facts);
        self.on.store(true, Ordering::SeqCst);
        Ok(())
    }

    /// Stop reporting, at once: gate shut, client unbound and closed, queue
    /// dropped. Nothing that was waiting is sent.
    pub fn disable(&self) {
        self.on.store(false, Ordering::SeqCst);
        let mut state = lock(&self.state);
        if let Some(gate) = state.gate.take() {
            gate.store(false, Ordering::SeqCst);
        }
        self.hub.bind_client(None);
        state.facts = None;
        if let Some(client) = state.client.take() {
            client.close(Some(Duration::ZERO));
        }
    }

    /// Scrub, check the flood guard, and hand the event to the client.
    /// `wait` false only tries the lock, for the panic hook: the panic may
    /// have happened while this thread held it.
    fn capture(&self, event: Event<'static>, wait: bool) -> Option<Arc<Client>> {
        if !self.is_on() {
            return None;
        }
        let _busy = Busy::enter()?;
        let mut state = if wait {
            lock(&self.state)
        } else {
            match self.state.try_lock() {
                Ok(g) => g,
                Err(std::sync::TryLockError::Poisoned(p)) => p.into_inner(),
                Err(std::sync::TryLockError::WouldBlock) => return None,
            }
        };
        let facts = state.facts.clone()?;
        let client = state.client.clone()?;
        let event = scrub_event(event, &facts);
        if !state.limiter.allow(&dedupe_key(&event), Instant::now()) {
            return None;
        }
        drop(state);
        client.capture_event(event, None);
        Some(client)
    }

    pub fn capture_web(&self, report: &WebReport) -> bool {
        if !self.is_on() {
            return false;
        }
        let redact = match lock(&self.state).facts.as_ref() {
            Some(f) => f.redact.clone(),
            None => return false,
        };
        self.capture(web_event(report, &redact), true).is_some()
    }

    #[cfg(test)]
    pub fn capture_shell(&self, event: Event<'static>) -> bool {
        self.capture(event, true).is_some()
    }

    fn capture_panic(&self, info: &PanicHookInfo<'_>) {
        if !self.is_on() || BUSY.with(Cell::get) {
            return;
        }
        let mut event = PanicIntegration::new().event_from_panic_info(info);
        event
            .tags
            .insert("component".to_string(), "shell".to_string());
        if let Some(client) = self.capture(event, false) {
            // The process may be about to end: give the report a moment.
            client.flush(Some(PANIC_FLUSH));
        }
    }
}

static REPORTER: OnceLock<Reporter> = OnceLock::new();

/// The process's reporter, made on first need (the first time reports are
/// turned on). Before that, nothing of Sentry exists in this process.
pub fn reporter() -> &'static Reporter {
    REPORTER.get_or_init(|| Reporter::new(Hub::main()))
}

/// The reporter if one was ever made; None means reports were never on.
pub fn existing_reporter() -> Option<&'static Reporter> {
    REPORTER.get()
}

/// Chain a panic hook that reports the panic when, and only when, reports
/// are on. Installed once at start-up; with consent off it finds no reporter
/// and goes straight on to the previous hook.
pub fn install_panic_hook() {
    let previous = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        if let Some(r) = existing_reporter() {
            r.capture_panic(info);
        }
        previous(info);
    }));
}

#[cfg(test)]
mod tests {
    use super::*;
    use sentry::Scope;
    use std::io::{BufRead, BufReader, Read, Write};
    use std::net::TcpListener;
    use std::sync::atomic::AtomicUsize;

    fn red() -> Redactions {
        Redactions::new(
            Some(r"C:\Users\Thales"),
            Some("thales"),
            Some("DESKTOP-K7Q2"),
        )
    }

    fn scrub(s: &str) -> String {
        scrub_text(s, &red(), MAX_MESSAGE_CHARS)
    }

    fn facts() -> Facts {
        Facts {
            os: "windows",
            arch: "x86_64",
            gpu: Some("cuda"),
            release: "stemdeck@1.2.3".to_string(),
            environment: "desktop",
            redact: red(),
        }
    }

    // ── scrubber ──

    #[test]
    fn windows_paths_are_removed_with_their_spaces() {
        let out = scrub(
            r"failed to open D:\Music\My Band\Song Title.flac: Access is denied. (os error 5)",
        );
        assert_eq!(out, "failed to open <path>: Access is denied. (os error 5)");
        let out = scrub(r#"could not read "C:\\Users\\Thales\\Documents\\StemDeck\\jobs\\x.json""#);
        assert_eq!(out, r#"could not read "<path>""#);
        assert!(!scrub("E:/Stems/Live Set (2024)/a.wav").contains("Live"));
    }

    #[test]
    fn macos_and_linux_paths_are_removed() {
        let out = scrub("No such file: /Users/alice/Music/Some Artist - Track.mp3");
        assert_eq!(out, "No such file: <path>");
        let out = scrub("open /home/bob/.local/share/stemdeck/jobs/abc (No such file)");
        assert_eq!(out, "open <path> (No such file)");
        assert_eq!(scrub("mkdir ~/Music/x failed"), "mkdir <path>");
        // One segment is not a path anyone owns, and a ratio is not a path.
        assert_eq!(scrub("GET /health 1/2 done"), "GET /health 1/2 done");
    }

    #[test]
    fn unc_and_env_paths_are_removed() {
        assert_eq!(
            scrub(r"copy \\NAS-01\share\songs\a b.wav: denied"),
            "copy <path>: denied"
        );
        assert_eq!(scrub(r"open \\?\C:\very\long\path"), "open <path>");
        assert_eq!(scrub(r"in %USERPROFILE%\Music\x"), "in <path>");
        assert_eq!(scrub("in $HOME/Music/x"), "in <path>");
    }

    #[test]
    fn the_home_folder_user_and_computer_names_are_removed_anywhere() {
        let out = scrub("user thales on DESKTOP-K7Q2 said Thales");
        assert_eq!(out, "user <user> on <host> said <user>");
        assert!(!scrub(r"C:\Users\Thales").to_lowercase().contains("thales"));
    }

    #[test]
    fn emails_are_removed() {
        assert_eq!(
            scrub("mail jane.doe+x@example.co.uk now"),
            "mail <email> now"
        );
    }

    #[test]
    fn urls_lose_everything_but_stemdecks_own_code_paths() {
        assert_eq!(
            scrub("fetch https://api.example.com/v1/song?token=abc123&title=My%20Song failed"),
            "fetch <url> failed"
        );
        assert_eq!(
            scrub("at http://127.0.0.1:51234/static/js/catalog.js?v=9#x:10:5"),
            "at /static/js/catalog.js"
        );
        assert_eq!(
            scrub("at http://127.0.0.1:51234/static/js/a.js:10:5"),
            "at /static/js/a.js:10:5"
        );
        assert_eq!(
            scrub("GET http://127.0.0.1:8000/api/jobs/e2e0deadbeef/stems/vocals.wav"),
            "GET <url>"
        );
        assert_eq!(scrub("img blob:http://127.0.0.1/1234-5678"), "img <url>");
        assert_eq!(
            scrub("token=abc123&sig=zzz"),
            "token=<redacted>&sig=<redacted>"
        );
        assert_eq!(
            scrub("Authorization: Bearer eyJabc.def"),
            "Authorization: Bearer <redacted>"
        );
        assert_eq!(scrub("http://user:pw@127.0.0.1/static/js/a.js"), "<url>");
    }

    #[test]
    fn song_file_names_are_removed_with_the_title_before_them() {
        assert_eq!(scrub("Decoding failed for Bohemian Rhapsody.mp3"), "<file>");
        assert_eq!(
            scrub("error: My Song (Live).wav not found"),
            "error: <file> not found"
        );
        assert_eq!(
            scrub(r#"cannot load "secret demo.m4a""#),
            r#"cannot load "<file>""#
        );
    }

    #[test]
    fn ips_and_long_ids_are_removed() {
        assert_eq!(
            scrub("bind 192.168.1.20:8443 failed"),
            "bind <ip>:8443 failed"
        );
        assert_eq!(scrub("bind 127.0.0.1:8443"), "bind 127.0.0.1:8443");
        assert_eq!(scrub("peer fe80::1c2b:3c4d"), "peer <ip>");
        assert_eq!(scrub("job e2e0deadbeef gone"), "job <id> gone");
        assert_eq!(scrub("id 123e4567-e89b-12d3-a456-426614174000"), "id <id>");
        // Rust paths and line:col pairs are not addresses.
        assert_eq!(
            scrub("std::io::Error at main.rs:10:5"),
            "std::io::Error at main.rs:10:5"
        );
    }

    #[test]
    fn long_messages_are_cut_and_marked() {
        let long = "x".repeat(5000);
        let out = scrub_text(&long, &red(), 100);
        assert_eq!(out.chars().count(), 100);
        assert!(out.ends_with(TRUNCATED));
        // A huge input is bounded before any pattern runs.
        let huge = "a ".repeat(200_000);
        assert!(scrub(&huge).chars().count() <= MAX_MESSAGE_CHARS);
    }

    #[test]
    fn scrubbing_twice_changes_nothing() {
        for s in [
            r"open C:\a b\c.mp3: no",
            "x@y.com at /home/u/x",
            "fine message",
        ] {
            let once = scrub(s);
            assert_eq!(scrub(&once), once);
        }
    }

    /// No frame carries a file name or a path, whatever came in: not in
    /// abs_path, filename, module, package, the source lines or vars.
    #[test]
    fn a_frame_keeps_no_path_or_file_name() {
        let inputs = [
            r"D:\Music\My Band\Song Title.flac",
            r"\\NAS-01\share\songs\a b.wav",
            r"C:\Users\Thales\.cargo\registry\src\x\sentry-0.49.6\src\lib.rs",
            "/home/bob/project/src/main.rs",
            "https://evil.example/x.js?token=secret",
        ];
        for input in inputs {
            let frame = Frame {
                function: Some(format!("load {input}")),
                abs_path: Some(input.to_string()),
                filename: Some(input.to_string()),
                module: Some(input.to_string()),
                package: Some(input.to_string()),
                context_line: Some(input.to_string()),
                pre_context: vec![input.to_string()],
                post_context: vec![input.to_string()],
                vars: [("p".to_string(), serde_json::json!(input))].into(),
                instruction_addr: Some(0x7ff6_1234_u64.into()),
                symbol_addr: Some(0x7ff6_1000_u64.into()),
                image_addr: Some(0x7ff6_0000_u64.into()),
                symbol: Some(input.to_string()),
                lineno: Some(12),
                colno: Some(3),
                in_app: Some(true),
                ..Default::default()
            };
            let event = Event {
                exception: vec![Exception {
                    ty: "panic".to_string(),
                    stacktrace: Some(Stacktrace {
                        frames: vec![frame],
                        ..Default::default()
                    }),
                    ..Default::default()
                }]
                .into(),
                ..Default::default()
            };
            let out = scrub_event(event, &facts());
            let json = serde_json::to_string(&out).unwrap();
            for word in [
                "abs_path",
                "filename",
                "context_line",
                "pre_context",
                "post_context",
                "vars",
                "addr",
                "symbol",
            ] {
                assert!(!json.contains(word), "{word} in {json}");
            }
            for part in [
                "Music", "Song", "NAS-01", "share", "Thales", "registry", "bob", "evil", "token",
                "secret", ".rs", ".flac", ".wav",
            ] {
                assert!(!json.contains(part), "{part} survived: {json}");
            }
            let f = &out.exception.values[0].stacktrace.as_ref().unwrap().frames[0];
            assert!(f.module.is_none() && f.package.is_none(), "{json}");
            assert_eq!(
                (f.lineno, f.colno, f.in_app),
                (Some(12), Some(3), Some(true))
            );
            // Scrubbing what already left changes nothing.
            let again = serde_json::to_string(&scrub_event(out, &facts())).unwrap();
            assert_eq!(again, json);
        }
    }

    #[test]
    fn plain_module_and_package_names_are_kept() {
        let f = scrub_frame(
            Frame {
                function: Some("stemdeck::main".to_string()),
                module: Some("std::panicking".to_string()),
                package: Some("stemdeck".to_string()),
                ..Default::default()
            },
            &red(),
        );
        assert_eq!(f.module.as_deref(), Some("std::panicking"));
        assert_eq!(f.package.as_deref(), Some("stemdeck"));
        assert_eq!(f.function.as_deref(), Some("stemdeck::main"));
    }

    // ── the event that leaves ──

    #[test]
    fn only_allowed_tags_and_contexts_survive() {
        let mut tags = BTreeMap::new();
        tags.insert("component".to_string(), "web".to_string());
        tags.insert("user".to_string(), "bob".to_string());
        tags.insert("os".to_string(), "spoofed".to_string());
        let mut contexts = BTreeMap::new();
        contexts.insert(
            "device".to_string(),
            Context::Device(Box::new(DeviceContext {
                name: Some("Bob's PC".to_string()),
                model: Some("RTX 4090".to_string()),
                ..Default::default()
            })),
        );
        let event = Event {
            tags,
            contexts,
            server_name: Some("DESKTOP-K7Q2".into()),
            user: Some(sentry::User {
                email: Some("bob@example.com".to_string()),
                username: Some("bob".to_string()),
                ..Default::default()
            }),
            extra: [("path".to_string(), serde_json::json!("/home/bob"))].into(),
            modules: [("x".to_string(), "1".to_string())].into(),
            breadcrumbs: vec![sentry::Breadcrumb::default()].into(),
            ..Default::default()
        };
        let out = scrub_event(event, &facts());
        let keys: Vec<&str> = out.tags.keys().map(String::as_str).collect();
        assert_eq!(keys, ["component", "gpu", "os"]);
        assert_eq!(out.tags["os"], "windows");
        assert_eq!(out.tags["gpu"], "cuda");
        assert_eq!(out.tags["component"], "web");
        assert!(out.server_name.is_none());
        assert!(out.user.is_none());
        assert!(out.request.is_none());
        assert!(out.extra.is_empty());
        assert!(out.modules.is_empty());
        assert!(out.breadcrumbs.values.is_empty());
        assert_eq!(out.release.as_deref(), Some("stemdeck@1.2.3"));
        assert_eq!(out.environment.as_deref(), Some("desktop"));
        let json = serde_json::to_string(&out).unwrap();
        assert!(!json.contains("Bob"), "{json}");
        assert!(!json.contains("4090"), "{json}");
        let ctx: Vec<&str> = out.contexts.keys().map(String::as_str).collect();
        assert_eq!(ctx, ["device", "os"]);
    }

    #[test]
    fn an_unknown_component_reads_as_shell() {
        let mut tags = BTreeMap::new();
        tags.insert("component".to_string(), "anything".to_string());
        let out = scrub_event(
            Event {
                tags,
                ..Default::default()
            },
            &facts(),
        );
        assert_eq!(out.tags["component"], "shell");
    }

    #[test]
    fn gpu_class_never_carries_a_model() {
        assert_eq!(gpu_class(Some("cuda")), Some("cuda"));
        assert_eq!(gpu_class(Some("mps")), Some("mps"));
        assert_eq!(gpu_class(Some("cpu")), Some("cpu"));
        assert_eq!(gpu_class(Some("NVIDIA GeForce RTX 4090")), None);
        assert_eq!(gpu_class(None), None);
    }

    #[test]
    fn a_web_stack_becomes_frames_of_stemdecks_code_only() {
        let report = WebReport {
            kind: "error".to_string(),
            name: Some("TypeError".to_string()),
            message: "Cannot read properties of undefined (reading 'title') in /Users/bob/x".to_string(),
            stack: Some(
                "TypeError: boom\n    at paintRow (http://127.0.0.1:5123/static/js/catalog.js?v=3:120:9)\n    at http://127.0.0.1:5123/static/js/main.js:5:1\n    at fetch (https://evil.example/x.js?token=1:1:1)\nrender@http://127.0.0.1:5123/static/js/player.js:7:2"
                    .to_string(),
            ),
        };
        let event = scrub_event(web_event(&report, &red()), &facts());
        let e = &event.exception.values[0];
        assert_eq!(e.ty, "TypeError");
        assert_eq!(
            e.value.as_deref(),
            Some("Cannot read properties of undefined (reading 'title') in <path>")
        );
        let frames = &e.stacktrace.as_ref().unwrap().frames;
        let functions: Vec<Option<&str>> = frames.iter().map(|f| f.function.as_deref()).collect();
        assert_eq!(
            functions,
            [Some("render"), Some("fetch"), None, Some("paintRow")]
        );
        let own: Vec<Option<bool>> = frames.iter().map(|f| f.in_app).collect();
        assert_eq!(own, [Some(true), Some(false), Some(true), Some(true)]);
        assert!(frames
            .iter()
            .all(|f| f.abs_path.is_none() && f.filename.is_none()));
        assert_eq!(frames[3].function.as_deref(), Some("paintRow"));
        assert_eq!(frames[3].lineno, Some(120));
        assert_eq!(event.tags["component"], "web");
        let json = serde_json::to_string(&event).unwrap();
        assert!(!json.contains("token"), "{json}");
        assert!(!json.contains("5123"), "{json}");
        assert!(!json.contains("bob"), "{json}");
        assert!(
            !json.contains("catalog.js") && !json.contains("/static/"),
            "{json}"
        );
    }

    // ── flood guard ──

    #[test]
    fn the_limiter_drops_duplicates_and_caps_a_minute() {
        let mut l = Limiter::default();
        let t0 = Instant::now();
        assert!(l.allow("a", t0));
        assert!(!l.allow("a", t0), "a duplicate is sent once per run");
        for i in 0..9 {
            assert!(l.allow(&format!("k{i}"), t0));
        }
        assert!(!l.allow("eleventh", t0), "ten a minute at most");
        assert!(l.allow("later", t0 + Duration::from_secs(61)));
        assert!(
            !l.allow("a", t0 + Duration::from_secs(120)),
            "still a duplicate"
        );
    }

    // ── consent ──

    #[test]
    fn consent_counts_only_when_on_and_of_this_version() {
        assert_eq!(consent_given(Some(&consent_record(true, 42))), Some(42));
        assert_eq!(consent_given(Some(&consent_record(false, 42))), None);
        let old = serde_json::json!({ "on": true, "version": CONSENT_VERSION + 1, "at": 1 });
        assert_eq!(
            consent_given(Some(&old)),
            None,
            "another text version reads as off"
        );
        let older = serde_json::json!({ "on": true, "version": 0, "at": 1 });
        assert_eq!(consent_given(Some(&older)), None);
        assert_eq!(consent_given(Some(&serde_json::json!(true))), None);
        assert_eq!(
            consent_given(Some(&serde_json::json!({ "on": "yes" }))),
            None
        );
        assert_eq!(consent_given(None), None);
    }

    /// Counts what reaches it, and never touches the network.
    struct CountingTransport {
        gate: Arc<AtomicBool>,
        count: Arc<AtomicUsize>,
    }
    impl Transport for CountingTransport {
        fn send_envelope(&self, _envelope: Envelope) {
            if self.gate.load(Ordering::SeqCst) {
                self.count.fetch_add(1, Ordering::SeqCst);
            }
        }
    }

    /// A DSN for tests only. The transports in these tests keep or count
    /// what they get; none sends to this address.
    fn test_dsn() -> Option<Dsn> {
        Some(
            "https://0123456789abcdef0123456789abcdef@example.invalid/42"
                .parse()
                .unwrap(),
        )
    }

    #[test]
    fn a_dsn_counts_only_when_it_is_https_and_parses() {
        assert!(parse_dsn(None).is_none(), "missing");
        assert!(parse_dsn(Some("")).is_none(), "empty");
        assert!(parse_dsn(Some("   ")).is_none(), "blank");
        assert!(parse_dsn(Some("not a dsn")).is_none(), "malformed");
        assert!(
            parse_dsn(Some("https://example.invalid/")).is_none(),
            "no key or project"
        );
        assert!(
            parse_dsn(Some("http://key@example.invalid/1")).is_none(),
            "not https"
        );
        let dsn =
            parse_dsn(Some("  https://key@o1.ingest.de.example.invalid/42\n")).expect("valid");
        assert_eq!(dsn.public_key(), "key");
        assert_eq!(dsn.project_id().to_string(), "42");
    }

    #[test]
    fn without_a_dsn_nothing_is_made_and_nothing_is_sent() {
        let hub = test_hub();
        let r = Reporter::new(hub.clone());
        let made = Arc::new(AtomicUsize::new(0));
        let m = made.clone();
        let refused = r.enable(None, facts(), move |_, gate, _| {
            m.fetch_add(1, Ordering::SeqCst);
            Arc::new(CountingTransport {
                gate,
                count: Arc::new(AtomicUsize::new(0)),
            })
        });
        assert!(refused.is_err());
        assert_eq!(made.load(Ordering::SeqCst), 0, "no transport is made");
        assert!(!r.is_on());
        assert!(hub.client().is_none());
        assert!(!r.capture_web(&report("x")));
        // The example still works without one.
        assert!(example_report(&facts()).contains("event_id"));
    }

    fn test_hub() -> Arc<Hub> {
        Arc::new(Hub::new(None, Arc::new(Scope::default())))
    }

    fn report(msg: &str) -> WebReport {
        WebReport {
            kind: "error".to_string(),
            message: msg.to_string(),
            ..Default::default()
        }
    }

    #[test]
    fn with_consent_off_no_client_is_bound_and_nothing_is_sent() {
        let hub = test_hub();
        let r = Reporter::new(hub.clone());
        assert!(!r.is_on());
        assert!(hub.client().is_none());
        assert!(!r.capture_web(&report("x")));
        assert!(!r.capture_shell(Event::default()));
        assert!(r.last_report().is_none());
    }

    #[test]
    fn turning_reports_off_unbinds_the_client_and_stops_sending_at_once() {
        let hub = test_hub();
        let r = Reporter::new(hub.clone());
        let count = Arc::new(AtomicUsize::new(0));
        let c = count.clone();
        r.enable(test_dsn(), facts(), move |_, gate, _| {
            Arc::new(CountingTransport { gate, count: c })
        })
        .unwrap();
        assert!(hub.client().is_some());
        assert!(r.capture_web(&report("first")));
        assert_eq!(count.load(Ordering::SeqCst), 1);

        // A client someone kept hold of from before is shut too.
        let stale = hub.client().unwrap();
        r.disable();
        assert!(hub.client().is_none());
        assert!(!r.capture_web(&report("second")));
        stale.capture_event(Event::default(), None);
        assert_eq!(count.load(Ordering::SeqCst), 1, "nothing after withdrawal");

        // On again: a new client with a new gate.
        let c2 = count.clone();
        r.enable(test_dsn(), facts(), move |_, gate, _| {
            Arc::new(CountingTransport { gate, count: c2 })
        })
        .unwrap();
        assert!(r.capture_web(&report("third")));
        assert_eq!(count.load(Ordering::SeqCst), 2);
        r.disable();
    }

    #[test]
    fn the_http_transport_sends_nothing_queued_before_withdrawal() {
        let gate = Arc::new(AtomicBool::new(true));
        let posts = Arc::new(AtomicUsize::new(0));
        let p = posts.clone();
        let (release_tx, release_rx) = std::sync::mpsc::channel::<()>();
        let release_rx = Mutex::new(release_rx);
        let last: LastReport = Arc::new(Mutex::new(None));
        let t = HttpTransport::spawn(
            gate.clone(),
            last.clone(),
            Box::new(move |_| {
                // Hold the first send so the next ones wait in the queue.
                let _ = lock(&release_rx).recv_timeout(Duration::from_secs(5));
                p.fetch_add(1, Ordering::SeqCst);
                PostOutcome::Sent
            }),
        );
        for _ in 0..3 {
            t.send_envelope(Envelope::from(Event::default()));
        }
        thread::sleep(Duration::from_millis(50));
        // Withdraw while one is in flight and two are queued.
        t.shutdown(Duration::ZERO);
        release_tx.send(()).unwrap();
        thread::sleep(Duration::from_millis(100));
        assert_eq!(
            posts.load(Ordering::SeqCst),
            1,
            "only the one already on its way"
        );
        t.send_envelope(Envelope::from(Event::default()));
        thread::sleep(Duration::from_millis(50));
        assert_eq!(posts.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn the_http_transport_sends_only_the_event_item() {
        let gate = Arc::new(AtomicBool::new(true));
        let bodies = Arc::new(Mutex::new(Vec::<Vec<u8>>::new()));
        let b = bodies.clone();
        let last: LastReport = Arc::new(Mutex::new(None));
        let t = HttpTransport::spawn(
            gate,
            last.clone(),
            Box::new(move |body| {
                lock(&b).push(body.to_vec());
                PostOutcome::Sent
            }),
        );
        let mut envelope = Envelope::from(Event::default());
        envelope.add_item(sentry::protocol::Attachment {
            buffer: b"C:\\Users\\Thales\\Music\\song.wav".to_vec(),
            filename: "song.txt".to_string(),
            ..Default::default()
        });
        t.send_envelope(envelope);
        assert!(t.flush(Duration::from_secs(5)));
        let sent = lock(&bodies);
        assert_eq!(sent.len(), 1);
        let text = String::from_utf8_lossy(&sent[0]);
        // Header, item header, event: nothing else.
        assert_eq!(text.lines().filter(|l| !l.is_empty()).count(), 3, "{text}");
        assert!(lock(&last).as_deref().unwrap().contains("event_id"));
    }

    /// The whole path, real HTTP included, against a listener on this
    /// machine's loopback address: no report leaves the machine.
    #[test]
    fn a_report_over_loopback_is_scrubbed_end_to_end() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let (tx, rx) = std::sync::mpsc::channel::<(String, String)>();
        thread::spawn(move || {
            let (stream, _) = listener.accept().unwrap();
            let mut reader = BufReader::new(stream.try_clone().unwrap());
            let mut head = String::new();
            let mut len = 0usize;
            loop {
                let mut line = String::new();
                reader.read_line(&mut line).unwrap();
                if line == "\r\n" || line.is_empty() {
                    break;
                }
                if let Some(v) = line.to_ascii_lowercase().strip_prefix("content-length:") {
                    len = v.trim().parse().unwrap();
                }
                head.push_str(&line);
            }
            let mut body = vec![0u8; len];
            reader.read_exact(&mut body).unwrap();
            let mut stream = stream;
            stream
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                .unwrap();
            tx.send((head, String::from_utf8(body).unwrap())).unwrap();
        });

        let hub = test_hub();
        let r = Reporter::new(hub);
        let dsn = format!("http://0123456789abcdef0123456789abcdef@127.0.0.1:{port}/42");
        r.enable(Some(dsn.parse().unwrap()), facts(), |dsn, gate, last| {
            HttpTransport::spawn(gate, last, http_poster(dsn, false))
        })
        .unwrap();
        let sent = r.capture_web(&WebReport {
            kind: "rejection".to_string(),
            name: Some("Error".to_string()),
            message:
                r"load failed for C:\Users\Thales\Music\Secret Song.wav (mail thales@example.com)"
                    .to_string(),
            stack: Some("at go (http://127.0.0.1:9/static/js/job.js?x=1:3:4)".to_string()),
        });
        assert!(sent);
        let (head, body) = rx
            .recv_timeout(Duration::from_secs(20))
            .expect("report arrived");
        r.disable();

        let head_lc = head.to_ascii_lowercase();
        assert!(head_lc.starts_with("post /api/42/envelope/"), "{head}");
        assert!(
            head_lc.contains("x-sentry-auth: sentry sentry_key=0123456789abcdef0123456789abcdef")
        );
        assert!(!head_lc.contains("cookie"));
        assert!(!head_lc.contains("user-agent: "), "{head}");

        let lines: Vec<serde_json::Value> = body
            .lines()
            .filter(|l| !l.is_empty())
            .map(|l| serde_json::from_str(l).unwrap())
            .collect();
        assert_eq!(lines.len(), 3, "{body}");
        let header = lines[0].as_object().unwrap();
        assert_eq!(header.keys().collect::<Vec<_>>(), ["event_id"]);
        let event = lines[2].as_object().unwrap();
        let mut keys: Vec<&str> = event.keys().map(String::as_str).collect();
        keys.sort_unstable();
        assert_eq!(
            keys,
            [
                "contexts",
                "environment",
                "event_id",
                "exception",
                // level is left out when it is "error", the default.
                "platform",
                "release",
                "sdk",
                "tags",
                "timestamp"
            ],
            "{body}"
        );
        assert!(
            !body.contains("Thales") && !body.contains("thales"),
            "{body}"
        );
        assert!(!body.contains("Secret"), "{body}");
        assert!(!body.contains("example.com"), "{body}");
        assert!(!body.contains("x=1"), "{body}");
        assert!(!body.contains("job.js"), "{body}");
        assert!(
            !body.contains("abs_path") && !body.contains("filename"),
            "{body}"
        );
        assert!(body.contains(r#""function":"go""#), "{body}");
        let value = event["exception"]["values"][0]["value"].as_str().unwrap();
        assert_eq!(value, "load failed for <path> (mail <email>)");
        eprintln!("captured envelope:\n{body}");
    }

    // ── the example report ──

    const FAKE: [&str; 9] = [
        "example-user",
        "Users",
        "Example Artist",
        "Example Song",
        ".mp3",
        "example@example.com",
        "example.com",
        "token",
        "8000",
    ];

    #[test]
    fn the_example_has_only_the_allowed_fields_and_none_of_the_fake_data() {
        let json = example_report(&facts());
        eprintln!("example report:\n{json}");
        let value: serde_json::Value = serde_json::from_str(&json).unwrap();
        let mut keys: Vec<&str> = value
            .as_object()
            .unwrap()
            .keys()
            .map(String::as_str)
            .collect();
        keys.sort_unstable();
        assert_eq!(
            keys,
            [
                "contexts",
                "environment",
                "event_id",
                "exception",
                "platform",
                "release",
                "sdk",
                "tags",
                "timestamp"
            ]
        );
        for key in ["abs_path", "filename"] {
            assert!(!json.contains(key), "{key} in {json}");
        }
        for fake in FAKE {
            assert!(!json.contains(fake), "{fake} survived: {json}");
        }
        assert_eq!(value["event_id"], "00000000000000000000000000000000");
        assert_eq!(value["timestamp"], 1_767_225_600.0);
        let msg = value["exception"]["values"][0]["value"].as_str().unwrap();
        assert!(
            msg.contains("<path>") && msg.contains("<email>") && msg.contains("<url>"),
            "{msg}"
        );
    }

    #[test]
    fn the_example_needs_no_reporter_and_creates_none() {
        assert!(existing_reporter().is_none());
        let _ = example_report(&facts());
        assert!(
            existing_reporter().is_none(),
            "no reporter, so no client and no hub binding"
        );
        assert!(Hub::main().client().is_none());
    }

    /// Keeps whatever reaches it, and never touches the network.
    struct KeepingTransport {
        gate: Arc<AtomicBool>,
        kept: Arc<Mutex<Vec<Envelope>>>,
    }
    impl Transport for KeepingTransport {
        fn send_envelope(&self, envelope: Envelope) {
            if self.gate.load(Ordering::SeqCst) {
                lock(&self.kept).push(envelope);
            }
        }
    }

    /// The example cannot drift: the same made-up error, sent through the
    /// real client to a transport that keeps it, comes out identical apart
    /// from its ID and time.
    #[test]
    fn the_example_is_what_the_real_pipeline_sends() {
        let r = Reporter::new(test_hub());
        let kept = Arc::new(Mutex::new(Vec::new()));
        let k = kept.clone();
        r.enable(test_dsn(), facts(), move |_, gate, _| {
            Arc::new(KeepingTransport { gate, kept: k })
        })
        .unwrap();
        assert!(r.capture_web(&example_input()));
        r.disable();
        let envelope = lock(&kept).pop().expect("the report reached the transport");
        let mut real = serde_json::to_value(envelope.event().unwrap()).unwrap();
        let mut example = serde_json::to_value(example_event(&facts())).unwrap();
        for v in [&mut real, &mut example] {
            let o = v.as_object_mut().unwrap();
            o.remove("event_id");
            o.remove("timestamp");
        }
        assert_eq!(real, example);
    }

    #[test]
    fn pretty_envelope_keeps_every_line() {
        let body = b"{\"event_id\":\"x\"}\n{\"type\":\"event\"}\n{\"a\":1}\n";
        let out = pretty_envelope(body);
        assert!(out.contains("\"event_id\": \"x\""));
        assert!(out.contains("\"a\": 1"));
    }
}
