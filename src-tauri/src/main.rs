//! Resume Studio — the desktop shell.
//!
//! This file has one job: start `build/studio_server.js`, wait for it to
//! say which URL it is listening on, and show that URL in a window.
//!
//! WINDOW FIRST
//! ------------
//! The window is created at once, on `ui/loading.html` (bundled, served
//! by Tauri itself), and the server is started on a thread. When the
//! server reports its URL the window navigates there. Before, the window
//! was created only once the server was ready, so for that long a launch
//! showed nothing at all, and the webview's own start-up could not
//! overlap the server's. If the server cannot start, the reason is shown
//! in the window rather than only printed to a console nobody may have.
//!
//! WHY IT IS THIS SMALL
//! --------------------
//! The usual Tauri design would put the whole application here: spawn
//! the build engine, frame its stdio, expose a `#[tauri::command]` per
//! operation, marshal PNG bytes across the bridge. That works, and it
//! means nobody can run, test or change the app without a Rust
//! toolchain and the platform's webview development packages.
//!
//! Keeping the application in Node and this file as a launcher means
//! the entire thing can be developed and tested with `npm run ui` in an
//! ordinary browser, and the desktop build is a wrapper rather than a
//! second implementation. What is here is what genuinely needs to be
//! native: a window, and a child process whose lifetime is tied to it.
//!
//! LIFETIME
//! --------
//! The server owns a Chromium instance and a Python worker. If this
//! process dies without stopping it, those leak and the next launch
//! finds a stale port. The server is started with `--exit-with-parent`:
//! its stdin is a pipe held open by this process, and when that pipe
//! closes the server runs its own graceful shutdown (closing Chromium,
//! stopping the worker and any running build). On window close and on
//! drop this process closes the pipe on purpose, gives the server a
//! moment to finish, and only then kills it. If this process is killed
//! in a way that skips both handlers, the pipe closes anyway and the
//! server stops itself.

#![cfg_attr(all(not(debug_assertions), target_os = "windows"), windows_subsystem = "windows")]

use std::io::{BufRead, BufReader};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::webview::PageLoadEvent;
use tauri::{Manager, WebviewUrl, WebviewWindow, WebviewWindowBuilder};

/// Frame the server prints once it is listening. Must match
/// READY_PREFIX in build/studio_server.js.
const READY_PREFIX: &str = "\u{1e}STUDIO_READY ";

/// The page the window shows while the server starts (in `ui/`, which is
/// `build.frontendDist` in tauri.conf.json).
const LOADING_PAGE: &str = "loading.html";

/// Kept in Tauri's managed state so the child is stopped when the app
/// exits, however it exits.
///
/// The server starts on its own thread while the window is already up,
/// so the window can be closed before there is a child to stop. `closed`
/// records that; the thread checks it under the same lock it stores the
/// child with, and stops the child itself if nobody is left to.
struct Server {
    child: Mutex<Option<Child>>,
    closed: AtomicBool,
}

impl Server {
    /// Stop the child, if there is one, and refuse any that arrives later.
    fn shut(&self) {
        self.closed.store(true, Ordering::SeqCst);
        if let Ok(mut guard) = self.child.lock() {
            if let Some(child) = guard.take() {
                stop_server(child);
            }
        }
    }

    /// Keep the child the thread just started — or stop it at once if the
    /// window has already gone.
    fn adopt(&self, child: Child) -> bool {
        match self.child.lock() {
            Ok(mut guard) if !self.closed.load(Ordering::SeqCst) => {
                *guard = Some(child);
                true
            }
            _ => {
                stop_server(child);
                false
            }
        }
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        self.shut();
    }
}

/// A start-up error, and whether the loading page is there to show it.
///
/// The server can fail before the webview has even loaded the loading
/// page (Node missing fails in milliseconds), and script evaluated then
/// is lost. So an error that arrives early waits here, and the page-load
/// handler shows it once the page has finished loading. Both sides take
/// the lock, so the message is shown exactly once whichever comes first.
#[derive(Default)]
struct StartupError {
    state: Mutex<(bool, Option<String>)>, // (loading page loaded, pending message)
}

impl StartupError {
    fn page_loaded(&self, window: &WebviewWindow) {
        if let Ok(mut state) = self.state.lock() {
            state.0 = true;
            if let Some(message) = state.1.take() {
                show_startup_error(window, &message);
            }
        }
    }

    fn report(&self, window: &WebviewWindow, message: String) {
        if let Ok(mut state) = self.state.lock() {
            if state.0 {
                show_startup_error(window, &message);
            } else {
                state.1 = Some(message);
            }
        }
    }
}

/// Whether the window was minimized when last looked at, and where the
/// server listens.
///
/// WebView2 does not tell the page it is hidden when the Tauri window is
/// minimized: `document.visibilitychange` never fired there (checked on
/// Windows with a listener in the Studio's console). So the shell reports
/// it: `note_minimized` sends the page's own report, `POST /api/visibility`,
/// straight to the server over loopback. The page's listener stays for
/// `npm run ui` in an ordinary browser, where it does fire.
///
/// Two things look: the `Resized` event (minimizing and restoring both
/// resize the window) and a check once a second, so the report does not
/// depend on how a platform delivers window events. Only a change is
/// reported.
#[derive(Default)]
struct Minimized {
    state: AtomicBool,
    addr: Mutex<Option<String>>, // "127.0.0.1:PORT", once the server is up
}

fn note_minimized(app: &tauri::AppHandle, minimized: bool) {
    let Some(m) = app.try_state::<Minimized>() else { return };
    let Some(addr) = m.addr.lock().ok().and_then(|a| a.clone()) else { return };
    if m.state.swap(minimized, Ordering::SeqCst) == minimized {
        return;
    }
    println!(
        "     (window {}: telling the server)",
        if minimized { "minimized" } else { "restored" }
    );
    // Off this thread: a slow answer must never hold up window events.
    std::thread::spawn(move || {
        if let Err(e) = post_visibility(&addr, minimized) {
            println!("     (could not tell the server the window was {}: {e})",
                     if minimized { "minimized" } else { "restored" });
        }
    });
}

/// `POST /api/visibility {"hidden": …}` to the server, with plain std I/O —
/// one short request on loopback does not need an HTTP client dependency.
/// The Host header is the server's own address, which its request checks
/// require; no Origin is sent, which they allow.
fn post_visibility(addr: &str, hidden: bool) -> Result<(), String> {
    use std::io::{Read, Write};
    use std::net::{TcpStream, ToSocketAddrs};
    let target = addr
        .to_socket_addrs()
        .map_err(|e| e.to_string())?
        .next()
        .ok_or_else(|| format!("no address for {addr}"))?;
    let mut stream = TcpStream::connect_timeout(&target, Duration::from_secs(2))
        .map_err(|e| e.to_string())?;
    let _ = stream.set_read_timeout(Some(Duration::from_secs(5)));
    let body = format!("{{\"hidden\":{hidden}}}");
    let request = format!(
        "POST /api/visibility HTTP/1.1\r\nHost: {addr}\r\nContent-Type: application/json\r\n\
         Content-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    stream.write_all(request.as_bytes()).map_err(|e| e.to_string())?;
    let mut reply = String::new();
    let _ = stream.read_to_string(&mut reply);
    let status = reply.lines().next().unwrap_or("").to_string();
    if status.contains(" 200 ") {
        Ok(())
    } else {
        Err(format!("the server answered {status:?}"))
    }
}

/// Put the reason on the loading page (its `showStartupError`).
fn show_startup_error(window: &WebviewWindow, message: &str) {
    let text = serde_json::to_string(message).unwrap_or_else(|_| "\"\"".to_string());
    let _ = window.eval(&format!("window.showStartupError && window.showStartupError({text})"));
}

/// How long the server gets to shut down cleanly before it is killed.
const GRACEFUL_SHUTDOWN: Duration = Duration::from_secs(2);

/// Stop the server, gracefully if it will go.
///
/// `child.kill()` alone skips the server's shutdown routine, and that
/// routine is what closes Chromium and stops the Python worker — a hard
/// kill of the Node process leaves both running. So close its stdin
/// first: the server was started with `--exit-with-parent` and treats
/// the end of stdin as the signal to shut down. Wait for it, and kill
/// it only if it has not exited in time.
fn stop_server(mut child: Child) {
    // Dropping the handle closes our end of the pipe.
    drop(child.stdin.take());

    let deadline = Instant::now() + GRACEFUL_SHUTDOWN;
    loop {
        match child.try_wait() {
            Ok(Some(_)) => return,
            Ok(None) if Instant::now() < deadline => {
                std::thread::sleep(Duration::from_millis(50));
            }
            _ => break,
        }
    }
    let _ = child.kill();
    let _ = child.wait();
}

/// The project root: the directory holding package.json, build/ and ui/.
///
/// A debug build (`npm run studio`, i.e. `tauri dev`) always runs from
/// the source tree, fixed at compile time. `tauri dev` also copies
/// `bundle.resources` next to the binary, into src-tauri/target/debug/,
/// and that copy has package.json and build/ but no node_modules — so
/// the resource directory must never win there, or the server starts
/// from the copy and cannot load playwright or sass-embedded.
///
/// In a bundled app the project ships as a resource directory, which
/// the caller passes in. tauri.conf.json maps each resource to its own
/// relative path, so the files land directly in it; `_up_` is where
/// Tauri 2 puts resources listed as `../x` in the list form of
/// `bundle.resources`, and it is accepted too.
fn project_root(resource_dir: Option<std::path::PathBuf>) -> std::path::PathBuf {
    if cfg!(debug_assertions) {
        // CARGO_MANIFEST_DIR is src-tauri/; its parent is the project.
        if let Some(src) = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).parent() {
            if src.join("package.json").exists() {
                return src.to_path_buf();
            }
        }
    }
    if let Some(dir) = resource_dir {
        if dir.join("package.json").exists() {
            return dir;
        }
        let up = dir.join("_up_");
        if up.join("package.json").exists() {
            return up;
        }
    }
    let mut dir = std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|p| p.to_path_buf()))
        .unwrap_or_else(|| std::path::PathBuf::from("."));
    for _ in 0..6 {
        if dir.join("package.json").exists() {
            return dir;
        }
        match dir.parent() {
            Some(parent) => dir = parent.to_path_buf(),
            None => break,
        }
    }
    std::env::current_dir().unwrap_or_else(|_| std::path::PathBuf::from("."))
}

/// Start the server and block until it reports its URL.
///
/// Returns the URL and the child handle. Any error here is fatal and
/// worth showing the user verbatim — "node is not installed" and "the
/// engine could not start Chromium" are different problems with
/// different fixes, and collapsing them into "failed to launch" would
/// send someone down the wrong path.
fn start_server(root: &std::path::Path) -> Result<(String, Child), String> {
    let mut child = Command::new(node_command())
        .arg(root.join("build").join("studio_server.js"))
        .arg("--exit-with-parent")
        .current_dir(root)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .spawn()
        .map_err(|e| {
            format!(
                "Could not start Node ({e}).\n\n\
                 Resume Studio runs the build pipeline with Node. Install Node 18 \
                 or newer and make sure `node` is on your PATH."
            )
        })?;

    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| "The server started but produced no output.".to_string())?;

    // Read until the ready frame, then keep draining on a thread.
    //
    // The draining is not optional. The server logs every build to its
    // stdout, which is this pipe. If nothing reads it, the pipe buffer
    // fills and the server blocks; and if the read end is dropped
    // entirely — which is what happens if this reader simply goes out
    // of scope here — the server's next write fails with EPIPE and it
    // dies mid-render. An earlier version of this function did exactly
    // that, and the app crashed on its first build.
    let mut reader = BufReader::new(stdout);
    let mut line = String::new();

    loop {
        line.clear();
        let read = reader
            .read_line(&mut line)
            .map_err(|e| format!("Lost contact with the server: {e}"))?;
        if read == 0 {
            let _ = child.kill();
            return Err("The server exited before it was ready. \
                        Run `npm run ui` in a terminal to see why."
                .to_string());
        }

        let trimmed = line.trim_end();
        if let Some(rest) = trimmed.strip_prefix(READY_PREFIX) {
            let parsed: serde_json::Value = serde_json::from_str(rest)
                .map_err(|e| format!("The server's ready message was malformed: {e}"))?;
            let url = parsed
                .get("url")
                .and_then(|v| v.as_str())
                .ok_or_else(|| "The server's ready message had no URL.".to_string())?
                .to_string();

            // Hand the pipe to a thread that reads it for the rest of
            // the process's life, echoing to this stdout so `npm run
            // studio` shows the same log a terminal run would.
            std::thread::spawn(move || {
                let mut rest_of_log = String::new();
                loop {
                    rest_of_log.clear();
                    match reader.read_line(&mut rest_of_log) {
                        Ok(0) | Err(_) => break,
                        Ok(_) => print!("{rest_of_log}"),
                    }
                }
            });

            return Ok((url, child));
        }
        print!("{line}");
    }
}

fn node_command() -> String {
    std::env::var("STUDIO_NODE").unwrap_or_else(|_| "node".to_string())
}

fn main() {
    tauri::Builder::default()
        .setup(|app| {
            let resource_dir = app.path().resource_dir().ok();
            let root = project_root(resource_dir);

            app.manage(Server {
                child: Mutex::new(None),
                closed: AtomicBool::new(false),
            });
            app.manage(StartupError::default());
            app.manage(Minimized::default());

            let window = WebviewWindowBuilder::new(app, "main", WebviewUrl::App(LOADING_PAGE.into()))
                .title("Resume Studio")
                // Windowed fullscreen: maximized, with the title
                // bar and the taskbar still there. Not
                // .fullscreen(true), which takes over the screen
                // and hides both — wrong for an app you use
                // alongside the editor you are typing your YAML
                // in.
                //
                // inner_size stays as the size to restore to
                // when the window is un-maximized.
                .maximized(true)
                .inner_size(1440.0, 920.0)
                .min_inner_size(760.0, 560.0)
                // Required for the page to see file drops at all.
                //
                // By default the webview handles drops natively
                // and the HTML dragover/drop events never fire,
                // so "drop a .yml on the window" works in a
                // browser and silently does nothing here. Tauri
                // documents this method as needed to use the
                // HTML5 drag and drop APIs on Windows.
                //
                // The app's Load… button does not depend on
                // this; dropping is the convenience.
                .disable_drag_drop_handler()
                // Only the loading page's own load counts: the server's
                // page loads later and has no showStartupError.
                .on_page_load(|window, payload| {
                    if payload.event() == PageLoadEvent::Finished
                        && payload.url().path().ends_with(LOADING_PAGE)
                    {
                        if let Some(errors) = window.app_handle().try_state::<StartupError>() {
                            errors.page_loaded(&window);
                        }
                    }
                })
                .build()?;

            // The server starts here, beside the window, not before it.
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                let started = start_server(&root).and_then(|(url, child)| {
                    let parsed = url.parse::<tauri::Url>().map_err(|e| format!("bad server URL {url}: {e}"));
                    let server = handle.state::<Server>();
                    if !server.adopt(child) {
                        // The window was closed while the server started.
                        return Ok(None);
                    }
                    parsed.map(Some)
                });
                match started {
                    Ok(Some(url)) => {
                        // Where minimize reports go, then the once-a-second
                        // check (see `Minimized`), for as long as the server is.
                        let addr = format!("{}:{}", url.host_str().unwrap_or("127.0.0.1"),
                                           url.port_or_known_default().unwrap_or(80));
                        if let Some(m) = handle.try_state::<Minimized>() {
                            if let Ok(mut a) = m.addr.lock() {
                                *a = Some(addr);
                            }
                        }
                        let (poll_handle, poll_window) = (handle.clone(), window.clone());
                        std::thread::spawn(move || loop {
                            std::thread::sleep(Duration::from_secs(1));
                            let closed = poll_handle
                                .try_state::<Server>()
                                .map_or(true, |s| s.closed.load(Ordering::SeqCst));
                            if closed {
                                break;
                            }
                            if let Ok(minimized) = poll_window.is_minimized() {
                                note_minimized(&poll_handle, minimized);
                            }
                        });
                        if let Err(e) = window.navigate(url) {
                            let message = format!("Could not open the Studio page: {e}");
                            eprintln!("Resume Studio could not start.\n\n{message}");
                            handle.state::<StartupError>().report(&window, message);
                        }
                    }
                    Ok(None) => {}
                    Err(message) => {
                        // Printed too: anyone debugging a launch from a
                        // terminal (`npm run studio`) sees it there, and
                        // `npm run ui` reproduces the failure with full
                        // output.
                        eprintln!("Resume Studio could not start.\n\n{message}");
                        handle.state::<StartupError>().report(&window, message);
                    }
                }
            });
            Ok(())
        })
        .on_window_event(|window, event| match event {
            tauri::WindowEvent::Destroyed => {
                if let Some(server) = window.app_handle().try_state::<Server>() {
                    server.shut();
                }
            }
            // Minimizing and restoring both resize the window; see Minimized.
            tauri::WindowEvent::Resized(_) => {
                if let Ok(minimized) = window.is_minimized() {
                    note_minimized(window.app_handle(), minimized);
                }
            }
            _ => {}
        })
        .run(tauri::generate_context!())
        .expect("error while running Resume Studio");
}
