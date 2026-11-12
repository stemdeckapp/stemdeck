//! Microphone and camera for StemDeck's own page, and for nothing else.
//!
//! Recording a take and recording a performance video both call
//! `getUserMedia` from the studio page, which the Python backend serves at
//! `http://127.0.0.1:<port>`. Each webview needs something different before
//! that call can succeed, and wry 0.55.1 (the version in Cargo.lock) supplies
//! only part of it:
//!
//! - Windows, WebView2: wry registers a `PermissionRequested` handler for the
//!   clipboard only (`wry-0.55.1/src/webview2/mod.rs:497-515`). Microphone and
//!   camera fall through to WebView2's own prompt, and a "Block" there is saved
//!   in the profile and never asked again (tauri-apps/tauri#5042). The handler
//!   below answers both kinds itself, without saving the answer.
//! - Linux, WebKitGTK: wry turns on WebGL and Web Audio but not media streams,
//!   and connects no `permission-request` handler
//!   (`wry-0.55.1/src/webkitgtk/mod.rs:427-449`). WebKitGTK then refuses every
//!   `getUserMedia` call. The block below turns media streams on and answers
//!   user-media requests.
//! - macOS, WKWebView: wry already grants media capture in its UI delegate
//!   (`wry-0.55.1/src/wkwebview/class/wry_web_view_ui_delegate.rs:126-136`),
//!   so the operating system is the only gate. That gate needs the usage texts
//!   in `Info.plist` and, once the app is signed with the hardened runtime, the
//!   entitlements in `Entitlements.plist`. No Rust is involved.
//!
//! Checked in the real Windows app (WebView2 154, over CDP): the backend page
//! gets both with no prompt, `http://localhost:<port>` and another port are
//! refused at once, and the profile's `media_stream_mic` and
//! `media_stream_camera` stay empty after either answer. Because nothing is
//! saved, every page load starts with nameless devices until something calls
//! `getUserMedia`; the track input picker opens the microphone once for that
//! (`needsNameUnlock` in static/js/capture/trackInputs.js).
//!
//! Only one origin is ever granted: the loopback backend this process started,
//! scheme, host and port all equal. The port is read when the request
//! arrives, not when the window opens, because the backend can restart on a
//! different port (a runtime swap) and the page follows it there. Any other
//! page that ends up in the window, including the bundled setup page, is
//! refused.

/// Whether a page at `request_uri` may use the microphone and camera, given
/// the backend this process is running at `backend_url`.
///
/// A pure function of two strings so the rule itself is tested on every host,
/// although the two handlers that call it each compile on one platform only.
#[cfg(any(windows, target_os = "linux", test))]
pub(crate) fn origin_allowed(request_uri: &str, backend_url: &str) -> bool {
    let (Ok(request), Ok(backend)) = (
        reqwest::Url::parse(request_uri),
        reqwest::Url::parse(backend_url),
    ) else {
        return false;
    };
    // The backend is always plain http on 127.0.0.1 (start_backend). Anything
    // else in that slot means the state is not what this rule was written
    // for, so grant nothing rather than guess.
    if backend.scheme() != "http" || backend.host_str() != Some("127.0.0.1") {
        return false;
    }
    request.scheme() == backend.scheme()
        && request.host_str() == backend.host_str()
        && request.port_or_known_default() == backend.port_or_known_default()
}

/// The lookup the handlers use for the backend's current URL. `None` while no
/// backend is running, which denies every request.
#[cfg(any(windows, target_os = "linux"))]
type BackendUrl = std::sync::Arc<dyn Fn() -> Option<String> + Send + Sync>;

/// Attaches the media permission handler to `window`.
///
/// Best effort: a failure is logged and the window keeps working, with
/// recording left to the webview's default behaviour.
#[cfg(any(windows, target_os = "linux"))]
pub(crate) fn install<F>(window: &tauri::WebviewWindow, backend_url: F)
where
    F: Fn() -> Option<String> + Send + Sync + 'static,
{
    let backend_url: BackendUrl = std::sync::Arc::new(backend_url);
    if let Err(e) = window.with_webview(move |webview| attach(webview, backend_url)) {
        eprintln!("[stemdeck] could not reach the webview for media permissions: {e}");
    }
}

/// macOS needs no handler: wry grants media capture and the operating system
/// asks the user, using the texts in Info.plist.
#[cfg(target_os = "macos")]
pub(crate) fn install<F>(_window: &tauri::WebviewWindow, _backend_url: F)
where
    F: Fn() -> Option<String> + Send + Sync + 'static,
{
}

#[cfg(windows)]
fn attach(webview: tauri::webview::PlatformWebview, backend_url: BackendUrl) {
    use webview2_com::{
        take_pwstr,
        Microsoft::Web::WebView2::Win32::{
            ICoreWebView2PermissionRequestedEventArgs3, COREWEBVIEW2_PERMISSION_KIND,
            COREWEBVIEW2_PERMISSION_KIND_CAMERA, COREWEBVIEW2_PERMISSION_KIND_MICROPHONE,
            COREWEBVIEW2_PERMISSION_STATE_ALLOW, COREWEBVIEW2_PERMISSION_STATE_DENY,
        },
        PermissionRequestedEventHandler,
    };
    use windows_core::{Interface, PWSTR};

    let handler = PermissionRequestedEventHandler::create(Box::new(move |_, args| {
        let Some(args) = args else { return Ok(()) };
        let mut kind = COREWEBVIEW2_PERMISSION_KIND::default();
        // SAFETY: `args` is the live event argument WebView2 handed to this
        // callback, and `kind` is a valid out pointer for its duration.
        unsafe { args.PermissionKind(&mut kind)? };
        if kind != COREWEBVIEW2_PERMISSION_KIND_MICROPHONE
            && kind != COREWEBVIEW2_PERMISSION_KIND_CAMERA
        {
            // Every other kind keeps WebView2's default, as before this
            // handler existed. wry's own clipboard handler still runs.
            return Ok(());
        }
        let mut uri = PWSTR::null();
        // SAFETY: as above; WebView2 allocates the string with CoTaskMemAlloc
        // and take_pwstr frees it.
        unsafe { args.Uri(&mut uri)? };
        let uri = take_pwstr(uri);
        let allowed = backend_url().is_some_and(|backend| origin_allowed(&uri, &backend));
        // Never save the answer in the profile. A saved state is what turned
        // one "Block" into a permanent denial (tauri#5042), and the origin's
        // port changes between launches anyway. Older runtimes without the
        // third interface simply keep their default.
        if let Ok(args3) = args.cast::<ICoreWebView2PermissionRequestedEventArgs3>() {
            // SAFETY: a successful cast of the same live argument object.
            unsafe { args3.SetSavesInProfile(false)? };
        }
        let state = if allowed {
            COREWEBVIEW2_PERMISSION_STATE_ALLOW
        } else {
            COREWEBVIEW2_PERMISSION_STATE_DENY
        };
        // SAFETY: as above.
        unsafe { args.SetState(state) }
    }));

    let mut token = 0i64;
    // SAFETY: the controller belongs to this window's webview and is used on
    // the thread with_webview runs its closure on (the main thread).
    let added = unsafe {
        webview
            .controller()
            .CoreWebView2()
            .and_then(|core| core.add_PermissionRequested(&handler, &mut token))
    };
    if let Err(e) = added {
        eprintln!("[stemdeck] could not install the media permission handler: {e}");
    }
}

#[cfg(target_os = "linux")]
fn attach(webview: tauri::webview::PlatformWebview, backend_url: BackendUrl) {
    use webkit2gtk::{
        glib::{prelude::*, translate::ToGlibPtr},
        PermissionRequestExt, SettingsExt, UserMediaPermissionRequest,
        UserMediaPermissionRequestExt, WebViewExt,
    };

    let view = webview.inner();
    // Off by default in WebKitGTK and not turned on by wry. Without it
    // navigator.mediaDevices.getUserMedia does not work at all.
    if let Some(settings) = WebViewExt::settings(&view) {
        settings.set_enable_media_stream(true);
    }

    view.connect_permission_request(move |view, request| {
        let Some(media) = request.downcast_ref::<UserMediaPermissionRequest>() else {
            // Not a microphone or camera request: leave it to WebKitGTK's
            // default, exactly as before this handler existed.
            return false;
        };
        // Screen capture arrives as the same request type. Recording never
        // asks for it, so it is never granted.
        // SAFETY: `media` is a live WebKitUserMediaPermissionRequest borrowed
        // for the duration of this signal; the call only reads it. Available
        // since WebKitGTK 2.34; this crate is built with v2_38, as wry is.
        let display = unsafe {
            webkit2gtk::ffi::webkit_user_media_permission_is_for_display_device(
                media.to_glib_none().0,
            )
        } != 0;
        let device = media.is_for_audio_device() || media.is_for_video_device();
        let allowed = !display
            && device
            && view
                .uri()
                .zip(backend_url())
                .is_some_and(|(uri, backend)| origin_allowed(&uri, &backend));
        if allowed {
            request.allow();
        } else {
            request.deny();
        }
        true
    });
}

#[cfg(test)]
mod tests {
    use super::origin_allowed;

    const BACKEND: &str = "http://127.0.0.1:8123";

    #[test]
    fn the_backend_page_is_allowed() {
        assert!(origin_allowed("http://127.0.0.1:8123/", BACKEND));
        assert!(origin_allowed(
            "http://127.0.0.1:8123/studio?job=abc#lane",
            BACKEND
        ));
    }

    #[test]
    fn a_different_port_is_refused() {
        // Another local server, or the backend from an earlier launch.
        assert!(!origin_allowed("http://127.0.0.1:8124/", BACKEND));
        assert!(!origin_allowed("http://127.0.0.1/", BACKEND));
    }

    #[test]
    fn https_and_other_hosts_are_refused() {
        // The LAN https listener serves phones, never this window.
        assert!(!origin_allowed("https://127.0.0.1:8123/", BACKEND));
        assert!(!origin_allowed("http://localhost:8123/", BACKEND));
        assert!(!origin_allowed("http://192.168.1.10:8123/", BACKEND));
        assert!(!origin_allowed("http://example.com:8123/", BACKEND));
    }

    #[test]
    fn other_loopback_spellings_are_refused() {
        // Each is another origin string to the webview, so a grant to one
        // would be a grant the page never asked for under its own name.
        assert!(!origin_allowed("http://[::1]:8123/", BACKEND));
        assert!(!origin_allowed("http://127.0.0.2:8123/", BACKEND));
        assert!(!origin_allowed("http://0.0.0.0:8123/", BACKEND));
        // Case is not a different origin: the URL parser lowers it.
        assert!(origin_allowed("HTTP://127.0.0.1:8123/", BACKEND));
    }

    #[test]
    fn userinfo_cannot_disguise_another_host() {
        assert!(!origin_allowed("http://127.0.0.1:8123@evil.test/", BACKEND));
    }

    #[test]
    fn the_bundled_setup_page_is_refused() {
        assert!(!origin_allowed(
            "http://tauri.localhost/index.html",
            BACKEND
        ));
        assert!(!origin_allowed("tauri://localhost/index.html", BACKEND));
    }

    #[test]
    fn garbage_is_refused() {
        assert!(!origin_allowed("", BACKEND));
        assert!(!origin_allowed("not a url", BACKEND));
        assert!(!origin_allowed("about:blank", BACKEND));
        assert!(!origin_allowed("http://127.0.0.1:8123/", "not a url"));
    }

    #[test]
    fn a_backend_that_is_not_loopback_http_grants_nothing() {
        assert!(!origin_allowed(
            "https://127.0.0.1:8123/",
            "https://127.0.0.1:8123"
        ));
        assert!(!origin_allowed(
            "http://example.com:8123/",
            "http://example.com:8123"
        ));
    }
}
