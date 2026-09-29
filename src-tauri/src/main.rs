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
//!
//! On Windows the server is also put in a job object that is killed when
//! this process's last handle to it closes (`job` below). The pipe asks
//! the server to stop; the job makes sure that the server and everything
//! it started — Chromium, the Python worker, a Build's child process —
//! end with this process even when the server cannot ask them to.

#![cfg_attr(all(not(debug_assertions), target_os = "windows"), windows_subsystem = "windows")]

use std::io::{BufRead, BufReader};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc, Mutex};
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
///
/// The reports go out one at a time, in order, from one thread
/// (`report_visibility`). They used to get a thread each, and a minimize
/// and a quick restore could then reach the server the wrong way round,
/// leaving it believing a window on screen was hidden — Chromium closed
/// under a visible window five minutes later. A report that fails is
/// tried again, since nothing else would ever repeat it.
#[derive(Default)]
struct Minimized {
    state: AtomicBool,
    reports: Mutex<Option<mpsc::Sender<bool>>>, // to report_visibility, once the server is up
}

fn note_minimized(app: &tauri::AppHandle, minimized: bool) {
    let Some(m) = app.try_state::<Minimized>() else { return };
    let Some(reports) = m.reports.lock().ok().and_then(|r| r.clone()) else { return };
    if m.state.swap(minimized, Ordering::SeqCst) == minimized {
        return;
    }
    println!(
        "     (window {}: telling the server)",
        if minimized { "minimized" } else { "restored" }
    );
    // To the reporting thread: a slow answer must never hold up window events.
    let _ = reports.send(minimized);
}

/// How many times one report is tried, and how long to wait after the
/// first failure (doubled after each one after that).
const VISIBILITY_ATTEMPTS: u32 = 4;
const VISIBILITY_RETRY: Duration = Duration::from_millis(500);

/// Send the window's minimized state to the server, in the order it
/// changed, until the sending side goes away.
///
/// Only the latest state matters: reports that queued up while one was
/// in flight collapse into the last of them, and a change that arrives
/// while a failed report waits to be retried replaces it.
fn report_visibility(addr: String, changes: mpsc::Receiver<bool>) {
    while let Ok(mut hidden) = changes.recv() {
        while let Ok(newer) = changes.try_recv() {
            hidden = newer;
        }
        let mut attempt = 1;
        let mut wait = VISIBILITY_RETRY;
        while let Err(e) = post_visibility(&addr, hidden) {
            let state = if hidden { "minimized" } else { "restored" };
            if attempt >= VISIBILITY_ATTEMPTS {
                println!("     (could not tell the server the window was {state}: {e}; giving up)");
                break;
            }
            println!("     (could not tell the server the window was {state}: {e}; trying again)");
            match changes.recv_timeout(wait) {
                Ok(newer) => {
                    hidden = newer;
                    while let Ok(newer) = changes.try_recv() {
                        hidden = newer;
                    }
                    attempt = 1;
                    wait = VISIBILITY_RETRY;
                }
                Err(mpsc::RecvTimeoutError::Timeout) => {
                    attempt += 1;
                    wait *= 2;
                }
                Err(mpsc::RecvTimeoutError::Disconnected) => return,
            }
        }
    }
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
///
/// Longer than the server's own backstop: its `shutdown()` in
/// build/studio_server.js exits by itself after 5 s if disposing of
/// Chromium and the worker hangs. A shorter wait here (it was 2 s) killed
/// the server in the middle of a slow but working shutdown — the very
/// case in which it had not yet closed Chromium or stopped the worker.
const GRACEFUL_SHUTDOWN: Duration = Duration::from_secs(6);

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

/// The Windows job object the server runs in.
///
/// `stop_server` kills only node.exe; Windows does not stop a process's
/// children with it, so a server killed before it had disposed of its
/// engine left Chromium, the Python worker or a Build's `node resume.js`
/// running with nothing attached. And if this process is itself killed
/// (Task Manager, a crash), nothing here runs at all. A job object with
/// KILL_ON_JOB_CLOSE covers both: every process the server starts joins
/// its job, and Windows ends the whole job when the last handle to it
/// closes — this process's, which it holds until it exits, however it
/// exits. The graceful stop through the pipe still comes first; the job
/// is what is left when that did not finish.
///
/// Declared here rather than taken from a crate: three kernel32 calls
/// did not seem worth a dependency (the `windows-sys` Tauri already pulls
/// in has them, but only as someone else's dependency). The layouts are
/// those of `JOBOBJECT_EXTENDED_LIMIT_INFORMATION` in the Windows SDK,
/// and the size checks below pin them to it.
#[cfg(windows)]
mod job {
    use std::ffi::c_void;
    use std::os::windows::io::AsRawHandle;
    use std::process::Child;
    use std::sync::OnceLock;

    type Handle = *mut c_void;

    #[repr(C)]
    #[derive(Default)]
    struct BasicLimitInformation {
        per_process_user_time_limit: i64,
        per_job_user_time_limit: i64,
        limit_flags: u32,
        minimum_working_set_size: usize,
        maximum_working_set_size: usize,
        active_process_limit: u32,
        affinity: usize,
        priority_class: u32,
        scheduling_class: u32,
    }

    #[repr(C)]
    #[derive(Default)]
    struct IoCounters {
        read_operation_count: u64,
        write_operation_count: u64,
        other_operation_count: u64,
        read_transfer_count: u64,
        write_transfer_count: u64,
        other_transfer_count: u64,
    }

    #[repr(C)]
    #[derive(Default)]
    struct ExtendedLimitInformation {
        basic_limit_information: BasicLimitInformation,
        io_info: IoCounters,
        process_memory_limit: usize,
        job_memory_limit: usize,
        peak_process_memory_used: usize,
        peak_job_memory_used: usize,
    }

    #[cfg(target_pointer_width = "64")]
    const _: () = assert!(std::mem::size_of::<ExtendedLimitInformation>() == 144);
    #[cfg(target_pointer_width = "32")]
    const _: () = assert!(std::mem::size_of::<ExtendedLimitInformation>() == 112);

    const JOB_OBJECT_EXTENDED_LIMIT_INFORMATION: i32 = 9;
    const JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: u32 = 0x2000;

    #[link(name = "kernel32")]
    extern "system" {
        fn CreateJobObjectW(attributes: *const c_void, name: *const u16) -> Handle;
        fn SetInformationJobObject(job: Handle, class: i32, info: *const c_void, length: u32) -> i32;
        fn AssignProcessToJobObject(job: Handle, process: Handle) -> i32;
    }

    /// The job, created on first use and never closed: its handle closing
    /// is what ends the server, so it lives exactly as long as this
    /// process. Kept as an address because a raw handle is not `Sync`.
    /// Not inheritable (no security attributes), so the server holds no
    /// handle to its own job that would keep it open.
    static JOB: OnceLock<Result<usize, String>> = OnceLock::new();

    fn job() -> Result<Handle, String> {
        JOB.get_or_init(|| unsafe {
            let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            if job.is_null() {
                return Err(format!("CreateJobObject: {}", std::io::Error::last_os_error()));
            }
            let mut info = ExtendedLimitInformation::default();
            info.basic_limit_information.limit_flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            let set = SetInformationJobObject(
                job,
                JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                &info as *const ExtendedLimitInformation as *const c_void,
                std::mem::size_of::<ExtendedLimitInformation>() as u32,
            );
            if set == 0 {
                return Err(format!("SetInformationJobObject: {}", std::io::Error::last_os_error()));
            }
            Ok(job as usize)
        })
        .clone()
        .map(|job| job as Handle)
    }

    /// Put the child in the job. Processes it starts from then on join
    /// the job with it.
    pub fn contain(child: &Child) -> Result<(), String> {
        let job = job()?;
        if unsafe { AssignProcessToJobObject(job, child.as_raw_handle() as Handle) } == 0 {
            return Err(format!("AssignProcessToJobObject: {}", std::io::Error::last_os_error()));
        }
        Ok(())
    }
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
    let mut command = Command::new(node_command());
    command
        .arg(root.join("build").join("studio_server.js"))
        .arg("--exit-with-parent")
        .current_dir(root)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit());
    // A release build is a GUI program with no console (windows_subsystem
    // above), so Windows gives node.exe, a console program, a console
    // window of its own, on screen for as long as the app runs. Without
    // one it inherits nothing, which is what the pipes above expect.
    // Only a release build: `tauri dev` has a terminal, which node shares.
    #[cfg(windows)]
    if !cfg!(debug_assertions) {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(CREATE_NO_WINDOW);
    }
    let mut child = command.spawn().map_err(|e| {
        format!(
            "Could not start Node ({e}).\n\n\
             Resume Studio runs the build pipeline with Node. Install Node 18 \
             or newer and make sure `node` is on your PATH."
        )
    })?;

    // Before the server can start anything of its own, so all of it is
    // in the job. Not fatal: without the job, the pipe still stops the
    // server in every case but a hard kill of this process mid-shutdown.
    #[cfg(windows)]
    if let Err(e) = job::contain(&child) {
        println!("     (could not tie the server's lifetime to this window: {e})");
    }

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

/// Whether the window may go to `url`: the loading page, as Tauri serves
/// it, and the server — its exact origin, once it has reported one.
///
/// The Studio page never leaves its own origin, so anything else is a
/// link, a redirect or a script taking the window somewhere it should
/// not be: a page elsewhere would sit in the app's own window, looking
/// like the app. Refused, and logged so it is not silently lost.
///
/// Tauri serves the loading page at tauri://localhost/ on Linux and
/// macOS and at http(s)://tauri.localhost/ on Windows. about:blank is
/// allowed because some webviews pass through it on the way to the first
/// page; it has no content of its own.
fn navigation_allowed(url: &tauri::Url, server: &Mutex<Option<tauri::Url>>) -> bool {
    let tauri_origin = match url.scheme() {
        "tauri" => url.host_str() == Some("localhost"),
        "http" | "https" => url.host_str() == Some("tauri.localhost"),
        _ => false,
    };
    if tauri_origin && url.path().strip_prefix('/') == Some(LOADING_PAGE) {
        return true;
    }
    if url.as_str() == "about:blank" {
        return true;
    }
    let to_server = server
        .lock()
        .ok()
        .and_then(|s| s.as_ref().map(|s| s.origin() == url.origin()))
        .unwrap_or(false);
    if !to_server {
        println!("     (kept the window from going to {url})");
    }
    to_server
}

/// WINDOW STATE
/// ------------
/// The window opens where it was closed: same size, same place, and
/// maximized if it was. A first launch opens maximized (see `main`).
///
/// Kept in `window-state.json` in the app's config directory, written
/// when the window is closed. The size and position are those of the
/// window when it is not maximized — what un-maximizing returns to — so
/// they are noted on every move and resize made while it is neither
/// maximized nor minimized, and a maximized window at close saves the
/// last of them. Whether it was maximized is noted the same way, so a
/// window closed while minimized (from the taskbar) keeps what it was.
///
/// In physical pixels, set on the window after it is built, as
/// tauri-plugin-window-state does. Not logical pixels through the window
/// builder: a logical position means nothing without the scale of the
/// monitor it is on, and on Windows the builder converts it with the
/// scale of the first monitor it fits on — a window closed on a 150 %
/// screen to the right of a 100 % one came back on the 100 % one. A
/// saved position that is on no connected monitor any more (a laptop
/// off its external screen) is dropped and the system places the window
/// instead.
///
/// Done here rather than with tauri-plugin-window-state, to keep the
/// shell's dependencies what they are; the plugin does the same job.
const WINDOW_STATE_FILE: &str = "window-state.json";

#[derive(Clone, Copy)]
struct Placement {
    x: f64,
    y: f64,
    width: f64,
    height: f64,
}

#[derive(Default)]
struct WindowState {
    path: Option<std::path::PathBuf>,
    normal: Mutex<Option<Placement>>, // the last un-maximized, un-minimized placement
    maximized: AtomicBool,            // as last seen while not minimized
}

impl WindowState {
    /// The saved placement, if any, and whether the window was maximized.
    fn load(&self) -> (Option<Placement>, Option<bool>) {
        let saved = self
            .path
            .as_ref()
            .and_then(|p| std::fs::read_to_string(p).ok())
            .and_then(|text| serde_json::from_str::<serde_json::Value>(&text).ok());
        let Some(saved) = saved else { return (None, None) };
        let number = |key: &str| saved.get(key).and_then(|v| v.as_f64()).filter(|v| v.is_finite());
        let placement = match (number("x"), number("y"), number("width"), number("height")) {
            (Some(x), Some(y), Some(width), Some(height))
                if (1.0..=100_000.0).contains(&width) && (1.0..=100_000.0).contains(&height) =>
            {
                Some(Placement { x, y, width, height })
            }
            _ => None,
        };
        if let (Some(p), Ok(mut normal)) = (placement, self.normal.lock()) {
            *normal = Some(p);
        }
        let maximized = saved.get("maximized").and_then(|v| v.as_bool());
        self.maximized.store(maximized.unwrap_or(true), Ordering::SeqCst);
        (placement, maximized)
    }

    /// Note where the window is, if it is in its normal state.
    fn note(&self, window: &tauri::Window) {
        if window.is_minimized().unwrap_or(true) {
            return;
        }
        let Ok(maximized) = window.is_maximized() else { return };
        self.maximized.store(maximized, Ordering::SeqCst);
        if maximized {
            return;
        }
        let (Ok(position), Ok(size)) = (window.outer_position(), window.inner_size()) else {
            return;
        };
        if let Ok(mut normal) = self.normal.lock() {
            *normal = Some(Placement {
                x: f64::from(position.x),
                y: f64::from(position.y),
                width: f64::from(size.width),
                height: f64::from(size.height),
            });
        }
    }

    /// Write the state down. Failing to is not worth bothering anyone
    /// with: the next launch opens maximized, as a first one does.
    fn save(&self, window: &tauri::Window) {
        let Some(path) = &self.path else { return };
        self.note(window);
        let mut state = serde_json::json!({
            "maximized": self.maximized.load(Ordering::SeqCst),
        });
        if let Some(p) = self.normal.lock().ok().and_then(|n| *n) {
            state["x"] = p.x.into();
            state["y"] = p.y.into();
            state["width"] = p.width.into();
            state["height"] = p.height.into();
        }
        let written = path
            .parent()
            .map_or(Ok(()), std::fs::create_dir_all)
            .and_then(|_| std::fs::write(path, state.to_string()));
        if let Err(e) = written {
            println!("     (could not remember the window's size and position: {e})");
        }
    }
}

/// Whether a window placed at `p` would show its title bar on one of the
/// monitors connected now.
fn on_screen(monitors: &[tauri::Monitor], p: &Placement) -> bool {
    monitors.iter().any(|m| {
        let (left, top) = (f64::from(m.position().x), f64::from(m.position().y));
        let (width, height) = (f64::from(m.size().width), f64::from(m.size().height));
        // A point a little inside the window's top-left corner, on its
        // title bar. All physical pixels, as monitors report themselves.
        let (x, y) = (p.x + 40.0, p.y + 10.0);
        x >= left && x < left + width && y >= top && y < top + height
    })
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
            app.manage(WindowState {
                path: app.path().app_config_dir().ok().map(|d| d.join(WINDOW_STATE_FILE)),
                ..WindowState::default()
            });

            // Where the window may navigate (see navigation_allowed): the
            // server's origin is filled in once the server reports it,
            // before the window is sent there.
            let server_url: Arc<Mutex<Option<tauri::Url>>> = Arc::default();
            let allowed = server_url.clone();

            // Where it was last closed (see WindowState), else the
            // defaults below.
            let (placement, was_maximized) = app.state::<WindowState>().load();
            let monitors = app.available_monitors().unwrap_or_default();
            let position = placement.filter(|p| on_screen(&monitors, p));

            let mut builder = WebviewWindowBuilder::new(app, "main", WebviewUrl::App(LOADING_PAGE.into()))
                .title("Resume Studio")
                // Windowed fullscreen: maximized, with the title
                // bar and the taskbar still there. Not
                // .fullscreen(true), which takes over the screen
                // and hides both — wrong for an app you use
                // alongside the editor you are typing your YAML
                // in. That is the first launch; after it, the
                // window is as it was left.
                //
                // inner_size stays as the size to restore to
                // when the window is un-maximized.
                .inner_size(1440.0, 920.0)
                .min_inner_size(760.0, 560.0);
            // A remembered placement is applied once the window exists
            // (below), so it is built hidden and shown then, rather than
            // seen jumping into place.
            builder = match (position, placement) {
                (None, None) => builder.maximized(was_maximized.unwrap_or(true)),
                _ => builder.visible(false),
            };
            let window = builder
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
                // Ctrl+plus/minus/0 (and Ctrl+wheel in WebView2) zoom the
                // whole interface, as in a browser. Off by default in
                // Tauri, which left no way to enlarge the Studio's text
                // short of the system's display scale (WCAG 1.4.4).
                //
                // The page does not claim these keys for its preview: the
                // preview zooms with Ctrl+wheel over the pages (the page
                // cancels the event there, so WebView2 does not zoom),
                // its toolbar, + − 0 while it has focus, and Ctrl+8/9.
                // See "zoom" in ui/index.html.
                //
                // WebView2's other browser accelerator keys are still on
                // (AreBrowserAcceleratorKeysEnabled; wry's
                // with_browser_accelerator_keys is not exposed by Tauri's
                // WebviewWindowBuilder). Turning them off from here means
                // with_webview + ICoreWebView2Settings3, which needs the
                // webview2-com crate as a direct dependency, and would
                // also turn off the Ctrl+plus/minus zoom above (it is in
                // the list that setting disables: learn.microsoft.com/
                // microsoft-edge/webview2/reference/win32/
                // icorewebview2settings3). So the
                // page handles the one that hurts: F5 / Ctrl+R, a reload
                // that throws away every render in memory, which it
                // cancels and treats as Re-render.
                .zoom_hotkeys_enabled(true)
                .on_navigation(move |url| navigation_allowed(url, &allowed))
                // Nothing in the Studio opens a window; a page that tries
                // to gets none, for the same reason as above.
                .on_new_window(|url, _| {
                    println!("     (kept the page from opening a window on {url})");
                    tauri::webview::NewWindowResponse::Deny
                })
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

            if placement.is_some() {
                // Position first: moving onto a monitor of another scale
                // resizes the window to match, and the size set after it
                // is the one that was saved.
                if let Some(p) = position {
                    let _ = window.set_position(tauri::PhysicalPosition::new(p.x as i32, p.y as i32));
                }
                if let Some(p) = placement {
                    let _ = window.set_size(tauri::PhysicalSize::new(p.width as u32, p.height as u32));
                }
                if was_maximized.unwrap_or(true) {
                    let _ = window.maximize();
                }
                let _ = window.show();
            }

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
                        let (reports, changes) = mpsc::channel();
                        std::thread::spawn(move || report_visibility(addr, changes));
                        if let Some(m) = handle.try_state::<Minimized>() {
                            if let Ok(mut r) = m.reports.lock() {
                                *r = Some(reports);
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
                        if let Ok(mut s) = server_url.lock() {
                            *s = Some(url.clone());
                        }
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
            // While the window is still there to ask where it is.
            tauri::WindowEvent::CloseRequested { .. } => {
                if let Some(state) = window.app_handle().try_state::<WindowState>() {
                    state.save(window);
                }
            }
            tauri::WindowEvent::Moved(_) => {
                if let Some(state) = window.app_handle().try_state::<WindowState>() {
                    state.note(window);
                }
            }
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
                if let Some(state) = window.app_handle().try_state::<WindowState>() {
                    state.note(window);
                }
            }
            _ => {}
        })
        .run(tauri::generate_context!())
        .expect("error while running Resume Studio");
}
