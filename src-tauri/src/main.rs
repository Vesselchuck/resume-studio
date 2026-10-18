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
//! So is the reason it stops after the window has gone to it: the window
//! is brought back to the loading page to show it (`server_stopped`).
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
//! stopping the worker and any running build). When the window is
//! destroyed (its `Destroyed` event, in `main`) this process closes the
//! pipe on purpose, gives the server a moment to finish, and only then
//! kills it. That handler is the only one: tauri's `run()` ends this
//! process with `std::process::exit`, which runs no destructors, so
//! nothing can be left to a `Drop`. If this process ends without the
//! handler running (killed, crashed), the pipe closes anyway, and where
//! there is no job (below) the server stops itself.
//!
//! On Windows the server is also put in a job object that is killed when
//! this process's last handle to it closes (`job` below). Through the
//! handler, the pipe asks the server to stop and the job is only what is
//! left if it did not. Without the handler there is no such order: the
//! pipe and the job's handle close together, as this process ends, and
//! the job ends the server and everything it started — Chromium, the
//! Python worker, a Build's child process — at once, with no graceful
//! shutdown. Ctrl+C in the `tauri dev` terminal is such an end.

#![cfg_attr(all(not(debug_assertions), target_os = "windows"), windows_subsystem = "windows")]

use std::collections::VecDeque;
use std::io::{BufRead, BufReader, Write};
use std::process::{Child, ChildStderr, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

use tauri::webview::PageLoadEvent;
use tauri::{Manager, WebviewUrl, WebviewWindow, WebviewWindowBuilder};

/// `println!` and `eprintln!` without their panic. They panic when the
/// write fails, and it fails when stdout or stderr is a pipe nobody reads
/// any more (`npm run studio | Select-String …`, and the reader quit) —
/// in a window callback or the thread draining the server's stdout,
/// that took the shell down. A line nobody can read is not worth that.
macro_rules! log_out {
    ($($arg:tt)*) => {{
        use std::io::Write as _;
        let _ = writeln!(std::io::stdout(), $($arg)*);
    }};
}

macro_rules! log_err {
    ($($arg:tt)*) => {{
        use std::io::Write as _;
        let _ = writeln!(std::io::stderr(), $($arg)*);
    }};
}

/// Frame the server prints once it is listening: the prefix, then
/// `{"url": …, "port": …, "token": …}`. Must match READY_PREFIX in
/// build/studio_server.js (tests/test_shell_frames.js).
const READY_PREFIX: &str = "\u{1e}STUDIO_READY ";

/// The header this shell's own requests carry the server's launch token
/// in. Must match TOKEN_HEADER in build/studio_server.js
/// (tests/test_shell_frames.js).
const TOKEN_HEADER: &str = "X-Studio-Token";

/// Frame the server prints when its engine fails after the ready frame,
/// just before it exits: the prefix, then `{"error": "<message>"}`. Must
/// match STARTUP_FAILED_PREFIX in build/studio_server.js
/// (tests/test_shell_frames.js).
const STARTUP_FAILED_PREFIX: &str = "\u{1e}STUDIO_FAILED ";

/// The page the window shows while the server starts (in `ui/`, which is
/// `build.frontendDist` in tauri.conf.json).
const LOADING_PAGE: &str = "loading.html";

/// The loading page's heading over the reason: the server could not
/// start (before the ready frame, or its engine after it), or it stopped
/// once the Studio was open (a crash, `/api/shutdown`).
const NOT_STARTED: &str = "Resume Studio could not start";
const STOPPED: &str = "The Studio stopped";

/// Kept in Tauri's managed state for the window's `Destroyed` handler,
/// which is what stops the child (see LIFETIME above). There is no
/// `Drop`: the process ends in `std::process::exit`, which drops nothing.
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

/// A start-up error, and whether the loading page is there to show it.
///
/// The server can fail before the webview has even loaded the loading
/// page (Node missing fails in milliseconds), and script evaluated then
/// is lost. So the error is kept here, and the page-load handler shows
/// it every time the loading page finishes loading: the first time, and
/// again after a reload. (It used to be handed over once, so F5 on the
/// error brought back the spinner for good.) Both sides take the lock,
/// so an error that arrives while the page loads is shown once it has.
#[derive(Default)]
struct StartupError {
    state: Mutex<(bool, Option<(&'static str, String)>)>, // (loading page loaded and on screen, the heading and message)
}

impl StartupError {
    /// A page started loading: until the loading page has finished,
    /// there is nothing to show an error on.
    fn page_started(&self) {
        if let Ok(mut state) = self.state.lock() {
            state.0 = false;
        }
    }

    fn page_loaded(&self, window: &WebviewWindow) {
        if let Ok(mut state) = self.state.lock() {
            state.0 = true;
            if let Some((heading, message)) = &state.1 {
                show_startup_error(window, heading, message);
            }
        }
    }

    fn report(&self, window: &WebviewWindow, heading: &'static str, message: String) {
        if let Ok(mut state) = self.state.lock() {
            if state.0 {
                show_startup_error(window, heading, &message);
            }
            state.1 = Some((heading, message));
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
///
/// The two lookers run on different threads, so the change and its
/// report are made under one lock (`note_minimized`). Made apart, a
/// minimize could be noted on one thread and a restore noted and sent on
/// the other before the minimize was sent, and the last word the server
/// had was "hidden" for a window on screen.
#[derive(Default)]
struct Minimized {
    state: AtomicBool,
    reports: Mutex<Option<mpsc::Sender<bool>>>, // to report_visibility, while the server is up
}

fn note_minimized(app: &tauri::AppHandle, minimized: bool) {
    let Some(m) = app.try_state::<Minimized>() else { return };
    // Held until the report is sent; see Minimized.
    let Ok(guard) = m.reports.lock() else { return };
    let Some(reports) = guard.as_ref() else { return };
    if m.state.swap(minimized, Ordering::SeqCst) == minimized {
        return;
    }
    log_out!(
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
fn report_visibility(addr: String, token: String, changes: mpsc::Receiver<bool>) {
    while let Ok(mut hidden) = changes.recv() {
        while let Ok(newer) = changes.try_recv() {
            hidden = newer;
        }
        let mut attempt = 1;
        let mut wait = VISIBILITY_RETRY;
        while let Err(e) = post_visibility(&addr, &token, hidden) {
            let state = if hidden { "minimized" } else { "restored" };
            if attempt >= VISIBILITY_ATTEMPTS {
                log_out!("     (could not tell the server the window was {state}: {e}; giving up)");
                break;
            }
            log_out!("     (could not tell the server the window was {state}: {e}; trying again)");
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
/// require; no Origin is sent, which they allow. The launch token from
/// the ready frame goes in TOKEN_HEADER: without it the server refuses
/// every request, this one included.
fn post_visibility(addr: &str, token: &str, hidden: bool) -> Result<(), String> {
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
         {TOKEN_HEADER}: {token}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
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

/// Put the reason on the loading page, under `heading` (its
/// `showStartupError`).
fn show_startup_error(window: &WebviewWindow, heading: &str, message: &str) {
    let _ = window.eval(&startup_error_script(heading, message));
}

/// The script `show_startup_error` evaluates: both strings as JSON, which
/// is valid JavaScript for any text.
fn startup_error_script(heading: &str, message: &str) -> String {
    let json = |s: &str| serde_json::to_string(s).unwrap_or_else(|_| "\"\"".to_string());
    format!(
        "window.showStartupError && window.showStartupError({}, {})",
        json(message),
        json(heading)
    )
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
/// exits. When the window is closed, the graceful stop through the pipe
/// still comes first and the job is what is left when that did not
/// finish. When this process ends any other way (killed, crashed, Ctrl+C
/// in its console), the job is all there is: the pipe closes in the same
/// moment, and the server gets no time to stop anything itself.
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
/// A dev build (`npm run studio`, i.e. `tauri dev`, with or without
/// `--release`: `tauri::is_dev()`, the test Tauri itself uses to serve
/// the devUrl) always runs from the source tree, fixed at compile time.
/// `tauri dev` also copies `bundle.resources` next to the binary, into
/// src-tauri/target/debug/ (release/ with `--release`), and that copy
/// has package.json and build/ but, of data/, only the three
/// `*_default.yml` templates, and a dist/ of its own — so the resource
/// directory must never win there, or the Studio opens the templates
/// instead of the files being worked on and builds into the copy's
/// dist/. (It would still find node_modules: Node looks for it in every
/// directory above the copy, and the project's is one.)
///
/// In a bundled app the project ships as a resource directory, which
/// the caller passes in. tauri.conf.json maps each resource to its own
/// relative path, so the files land directly in it; `_up_` is where
/// Tauri 2 puts resources listed as `../x` in the list form of
/// `bundle.resources`, and it is accepted too.
fn project_root(resource_dir: Option<std::path::PathBuf>) -> std::path::PathBuf {
    if tauri::is_dev() {
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

/// How many of the server's last stderr lines a failure shows, and how
/// long a failure waits for the rest of them once the server has gone.
/// The wait is bounded because anything the server started that shares
/// the pipe can hold it open after the server itself has exited.
const STDERR_LINES: usize = 40;
const STDERR_WAIT: Duration = Duration::from_secs(1);

/// The last lines the server wrote to stderr.
///
/// stderr is a pipe, not inherited: a release build is a GUI program with
/// no stderr of its own, so whatever the server said about why it could
/// not start (a bad PYTHON, a missing module) went nowhere, and the window
/// could only say to run `npm run ui` to find out. `echo_stderr` still
/// passes every line on to this process's stderr, so `tauri dev` shows
/// them live as before.
#[derive(Default)]
struct StderrTail {
    state: Mutex<(VecDeque<String>, bool)>, // (the last lines, stderr has closed)
    closed: Condvar,
}

impl StderrTail {
    fn push(&self, line: String) {
        if let Ok(mut state) = self.state.lock() {
            if state.0.len() == STDERR_LINES {
                state.0.pop_front();
            }
            state.0.push_back(line);
        }
    }

    fn close(&self) {
        if let Ok(mut state) = self.state.lock() {
            state.1 = true;
        }
        self.closed.notify_all();
    }

    /// The lines, once stderr has closed or `wait` has passed.
    fn text(&self, wait: Duration) -> String {
        let Ok(state) = self.state.lock() else { return String::new() };
        let state = match self.closed.wait_timeout_while(state, wait, |s| !s.1) {
            Ok((state, _)) => state,
            Err(poisoned) => poisoned.into_inner().0,
        };
        state.0.iter().map(String::as_str).collect::<Vec<_>>().join("\n").trim().to_string()
    }
}

/// Read the server's stderr for the rest of its life: on to this
/// process's stderr as it comes, and into `tail`.
fn echo_stderr(stderr: ChildStderr, tail: Arc<StderrTail>) {
    let mut reader = BufReader::new(stderr);
    let mut bytes = Vec::new();
    loop {
        bytes.clear();
        match reader.read_until(b'\n', &mut bytes) {
            Ok(0) | Err(_) => break,
            Ok(_) => {
                // Not eprint!, which panics if the write fails.
                let _ = std::io::stderr().write_all(&bytes);
                tail.push(String::from_utf8_lossy(&bytes).trim_end().to_string());
            }
        }
    }
    tail.close();
}

/// The reason in a STARTUP_FAILED_PREFIX frame, if `line` is one.
fn failed_reason(line: &str) -> Option<String> {
    let rest = line.trim_end().strip_prefix(STARTUP_FAILED_PREFIX)?;
    let error = serde_json::from_str::<serde_json::Value>(rest)
        .ok()
        .and_then(|v| v.get("error").and_then(|e| e.as_str()).map(str::to_string));
    Some(error.unwrap_or_else(|| format!("The server failed; its message could not be read: {rest}")))
}

/// The URL and the launch token in a READY_PREFIX frame, if `line` is
/// one: the URL is what the window opens, and the token is what this
/// shell's own requests carry (post_visibility). A frame without either
/// is an error, since the Studio cannot be used without both.
fn ready_frame(line: &str) -> Option<Result<(String, String), String>> {
    let rest = line.trim_end().strip_prefix(READY_PREFIX)?;
    let parsed = match serde_json::from_str::<serde_json::Value>(rest) {
        Ok(parsed) => parsed,
        Err(e) => return Some(Err(format!("The server's ready message was malformed: {e}"))),
    };
    let field = |name: &str| parsed.get(name).and_then(|v| v.as_str()).map(str::to_string);
    Some(match (field("url"), field("token")) {
        (Some(url), Some(token)) => Ok((url, token)),
        (None, _) => Err("The server's ready message had no URL.".to_string()),
        (_, None) => Err("The server's ready message had no token.".to_string()),
    })
}

/// `reason`, then `said`, the last of what the server wrote to stderr,
/// if it wrote anything. A failed frame's reason is one line; what
/// failed underneath it (a missing Python package, a Chromium that is not
/// installed) is often only in those lines.
fn with_said(reason: &str, said: &str) -> String {
    if said.is_empty() {
        reason.to_string()
    } else {
        format!("{reason}\n\nIt said last:\n\n{said}")
    }
}

/// `line` with the launch token replaced by "…", as the server's own log
/// does (withoutTokens in build/studio_server.js). The server prints its
/// address with the token for a terminal of the user's own; echoed to the
/// `tauri dev` terminal, whose output studio.bat points people to when the
/// app fails, it would go wherever that output is pasted.
fn without_token<'a>(line: &'a str, token: &str) -> std::borrow::Cow<'a, str> {
    if token.is_empty() || !line.contains(token) {
        std::borrow::Cow::Borrowed(line)
    } else {
        std::borrow::Cow::Owned(line.replace(token, "…"))
    }
}

/// Start the server and block until it reports its URL.
///
/// Returns the URL, the launch token (`ready_frame`), the child handle,
/// and where the reason arrives if the server fails or stops after that,
/// with its heading (`server_stopped` shows both). An
/// error before the URL is fatal and shown to the user verbatim, with
/// the last of what the server wrote to stderr — "node is not installed"
/// and "the engine could not start Chromium" are different problems with
/// different fixes, and collapsing them into "failed to launch" would
/// send someone down the wrong path. A STARTUP_FAILED_PREFIX frame before
/// the URL (a bad `STUDIO_PORT`, a port in use) is such an error, its
/// reason shown rather than the frame. The engine itself finishes starting
/// after the URL (see engineReady in build/studio_server.js): if it
/// fails, the server sends the reason in a STARTUP_FAILED_PREFIX frame
/// and exits, and that reason arrives on the receiver, again with the
/// last of stderr.
fn start_server(root: &std::path::Path) -> Result<(String, String, Child, mpsc::Receiver<(&'static str, String)>), String> {
    let node = studio_node();
    let mut command = Command::new(node.as_deref().unwrap_or("node"));
    command
        .arg(root.join("build").join("studio_server.js"))
        .arg("--exit-with-parent")
        .current_dir(root)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
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
    let mut child = command.spawn().map_err(|e| match &node {
        Some(path) => format!(
            "Could not start Node from STUDIO_NODE ({e}).\n\n\
             STUDIO_NODE is set to {path}, so Resume Studio runs that rather \
             than the `node` on your PATH. Point it at a Node 24 (20.19 or \
             newer works) node executable, or remove it."
        ),
        None => format!(
            "Could not start Node ({e}).\n\n\
             Resume Studio runs the build pipeline with Node. Install Node 24 \
             (20.19 or newer works) and make sure `node` is on your PATH."
        ),
    })?;

    // Before the server can start anything of its own, so all of it is
    // in the job. Not fatal: without the job, the pipe still stops the
    // server in every case but a hard kill of this process mid-shutdown.
    #[cfg(windows)]
    if let Err(e) = job::contain(&child) {
        log_out!("     (could not tie the server's lifetime to this window: {e})");
    }

    // Drained from the start, for the same reason as stdout below.
    let tail = Arc::new(StderrTail::default());
    if let Some(stderr) = child.stderr.take() {
        let tail = tail.clone();
        std::thread::spawn(move || echo_stderr(stderr, tail));
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
    //
    // Read as bytes and decoded leniently: a line that is not UTF-8 (a
    // tool printing in the console's code page) is still a line, where
    // read_line failed on it and start-up with it.
    let mut reader = BufReader::new(stdout);
    let mut bytes = Vec::new();

    loop {
        bytes.clear();
        let read = match reader.read_until(b'\n', &mut bytes) {
            Ok(read) => read,
            Err(e) => {
                stop_server(child);
                return Err(format!("Lost contact with the server: {e}"));
            }
        };
        if read == 0 {
            let _ = child.kill();
            let said = tail.text(STDERR_WAIT);
            return Err(if said.is_empty() {
                "The server exited before it was ready. \
                 Run `npm run ui` in a terminal to see why."
                    .to_string()
            } else {
                format!("The server exited before it was ready. It said:\n\n{said}")
            });
        }

        let line = String::from_utf8_lossy(&bytes);
        if let Some(reason) = failed_reason(&line) {
            // The server exits after this frame by itself; stderr closes
            // as it does, so the wait for the tail is short.
            let _ = child.kill();
            return Err(with_said(&reason, &tail.text(STDERR_WAIT)));
        }
        if let Some(ready) = ready_frame(&line) {
            // A server whose ready frame cannot be used is stopped here:
            // nothing else holds it.
            let (url, token) = match ready {
                Ok(ready) => ready,
                Err(e) => {
                    stop_server(child);
                    return Err(e);
                }
            };

            // Hand the pipe to a thread that reads it for the rest of
            // the process's life, echoing to this stdout so `npm run
            // studio` shows the same log a terminal run would, less the
            // token (without_token). It also watches for the server
            // failing: a STARTUP_FAILED_PREFIX frame, or the pipe
            // closing. Each sends a heading and a reason, the frame's
            // with the tail of stderr (the server exits right after it,
            // closing stderr, so the wait is short); after a frame, the
            // pipe closing sends nothing more.
            let (stopped, reasons) = mpsc::channel();
            let hidden = token.clone();
            std::thread::spawn(move || {
                let mut bytes = Vec::new();
                let mut failed = false;
                loop {
                    bytes.clear();
                    match reader.read_until(b'\n', &mut bytes) {
                        Ok(0) | Err(_) => break,
                        Ok(_) => {
                            let line = String::from_utf8_lossy(&bytes);
                            match failed_reason(&line) {
                                Some(reason) if !failed => {
                                    failed = true;
                                    let said = tail.text(STDERR_WAIT);
                                    let _ = stopped.send((NOT_STARTED, with_said(&reason, &said)));
                                }
                                Some(_) => {}
                                None => {
                                    let line = without_token(&line, &hidden);
                                    let _ = std::io::stdout().write_all(line.as_bytes());
                                }
                            }
                        }
                    }
                }
                if !failed {
                    let said = tail.text(STDERR_WAIT);
                    let _ = stopped.send((STOPPED, with_said("The Studio server stopped.", &said)));
                }
            });

            return Ok((url, token, child, reasons));
        }
        // Not print!, which panics if the write fails (see log_out).
        let _ = std::io::stdout().write_all(line.as_bytes());
    }
}

/// The `node` to start the server with, if STUDIO_NODE names one; else
/// it is whichever `node` is on PATH.
fn studio_node() -> Option<String> {
    std::env::var("STUDIO_NODE")
        .ok()
        .map(|value| unquote(&value).to_string())
        .filter(|value| !value.is_empty())
}

/// `value` without surrounding whitespace and one pair of surrounding
/// quotes. `set STUDIO_NODE="C:\Program Files\nodejs\node.exe"` keeps the
/// quotes in the value, and a quoted path is not a name Windows can open
/// (os error 123).
fn unquote(value: &str) -> &str {
    let value = value.trim();
    for quote in ['"', '\''] {
        if let Some(inner) = value.strip_prefix(quote).and_then(|v| v.strip_suffix(quote)) {
            return inner.trim();
        }
    }
    value
}

/// Whether the window may go to `url`: the loading page, as Tauri serves
/// it, and the server — its exact origin, once it has reported one and
/// until it stops (server_stopped). The
/// URL it reports carries its launch token (`/?t=…`) and redirects to
/// `/`; only the origin is compared, so both pass.
///
/// The Studio page never leaves its own origin, so anything else is a
/// link, a redirect or a script taking the window somewhere it should
/// not be: a page elsewhere would sit in the app's own window, looking
/// like the app. Refused, and logged so it is not silently lost.
///
/// Tauri serves the loading page at tauri://localhost/ on Linux and
/// macOS and at http(s)://tauri.localhost/ on Windows. Under `tauri dev`
/// it is `loading` instead (see loading_page_url), on the CLI's own dev
/// server; that one was refused here once, so in `tauri dev` the window
/// stayed blank until the server was ready and a start-up error was
/// never shown. about:blank is allowed because some webviews pass
/// through it on the way to the first page; it has no content of its own.
fn navigation_allowed(
    url: &tauri::Url,
    loading: Option<&tauri::Url>,
    server: &Mutex<Option<tauri::Url>>,
) -> bool {
    let tauri_origin = match url.scheme() {
        "tauri" => url.host_str() == Some("localhost"),
        "http" | "https" => url.host_str() == Some("tauri.localhost"),
        _ => false,
    };
    if tauri_origin && url.path().strip_prefix('/') == Some(LOADING_PAGE) {
        return true;
    }
    if loading.is_some_and(|l| l.origin() == url.origin() && l.path() == url.path()) {
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
        log_out!("     (kept the window from going to {url})");
    }
    to_server
}

/// The loading page's URL, as the window is given it.
///
/// What `WebviewUrl::App` resolves to: in a dev build (`dev`), under the
/// config's devUrl when there is one — `tauri dev` fills that in with its
/// own dev server for `ui/` (http://127.0.0.1:1430/ when this config
/// names none), and the bundle is not served then. Otherwise where Tauri
/// serves the bundle; http://tauri.localhost/ is its Windows form for a
/// window that does not ask for the https scheme, as this one does not.
///
/// The caller passes `tauri::is_dev()` as `dev`: Tauri's own test for
/// serving the devUrl. Not `debug_assertions`, which `tauri dev
/// --release` builds without — the window then opened on the dev server
/// while this said tauri.localhost, and navigation_allowed refused it.
fn loading_page_url(dev_url: Option<&tauri::Url>, dev: bool) -> Option<tauri::Url> {
    if let Some(dev_url) = dev_url.filter(|_| dev) {
        return dev_url.join(LOADING_PAGE).ok();
    }
    let origin = if cfg!(windows) { "http://tauri.localhost/" } else { "tauri://localhost/" };
    format!("{origin}{LOADING_PAGE}").parse().ok()
}

/// The server failed or stopped after the window had gone to it: bring
/// the window back to the loading page (`loading`) and show the reason
/// there. Left where it was, the window showed WebView2's "can't reach
/// this page", which says nothing about why. Not while the window is
/// closing: the server stopping is then what was asked for.
///
/// The server's origin stops being one the window may go to (`server`,
/// as navigation_allowed reads it): nothing of the Studio's listens
/// there any more, and whatever listens there next is not the Studio.
/// Clearing it also ends the once-a-second minimize check in `main`.
fn server_stopped(
    app: &tauri::AppHandle,
    window: &WebviewWindow,
    loading: Option<tauri::Url>,
    server: &Mutex<Option<tauri::Url>>,
    (heading, message): (&'static str, String),
) {
    if app.try_state::<Server>().map_or(true, |s| s.closed.load(Ordering::SeqCst)) {
        return;
    }
    log_err!("{heading}.\n\n{message}");
    if let Ok(mut s) = server.lock() {
        *s = None;
    }
    // Nobody is left to tell about minimizing; this also ends report_visibility.
    if let Some(m) = app.try_state::<Minimized>() {
        if let Ok(mut r) = m.reports.lock() {
            *r = None;
        }
    }
    app.state::<StartupError>().report(window, heading, message);
    match loading {
        Some(url) => {
            if let Err(e) = window.navigate(url) {
                log_err!("     (could not bring the window back to the loading page: {e})");
            }
        }
        None => log_err!("     (could not bring the window back to the loading page)"),
    }
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
            log_out!("     (could not remember the window's size and position: {e})");
        }
    }
}

/// Whether a window placed at `p` would show its title bar on one of the
/// monitors connected now.
fn on_screen(monitors: &[tauri::Monitor], p: &Placement) -> bool {
    on_any(
        monitors.iter().map(|m| Placement {
            x: f64::from(m.position().x),
            y: f64::from(m.position().y),
            width: f64::from(m.size().width),
            height: f64::from(m.size().height),
        }),
        p,
    )
}

/// `on_screen`, with each monitor as the rectangle it covers (a
/// `tauri::Monitor` cannot be made outside Tauri, so the tests use this).
fn on_any(monitors: impl IntoIterator<Item = Placement>, p: &Placement) -> bool {
    monitors.into_iter().any(|m| {
        // A point a little inside the window's top-left corner, on its
        // title bar. All physical pixels, as monitors report themselves.
        let (x, y) = (p.x + 40.0, p.y + 10.0);
        x >= m.x && x < m.x + m.width && y >= m.y && y < m.y + m.height
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
            let loading = loading_page_url(app.config().build.dev_url.as_ref(), tauri::is_dev());
            let allowed_loading = loading.clone();

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
                .on_navigation(move |url| navigation_allowed(url, allowed_loading.as_ref(), &allowed))
                // Nothing in the Studio opens a window; a page that tries
                // to gets none, for the same reason as above.
                .on_new_window(|url, _| {
                    log_out!("     (kept the page from opening a window on {url})");
                    tauri::webview::NewWindowResponse::Deny
                })
                // Only the loading page's own load counts: the server's
                // page loads later and has no showStartupError.
                .on_page_load(|window, payload| {
                    let Some(errors) = window.app_handle().try_state::<StartupError>() else { return };
                    match payload.event() {
                        PageLoadEvent::Started => errors.page_started(),
                        PageLoadEvent::Finished if payload.url().path().ends_with(LOADING_PAGE) => {
                            errors.page_loaded(&window)
                        }
                        PageLoadEvent::Finished => {}
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
                let started = start_server(&root).and_then(|(url, token, child, stopped)| {
                    let url = match url.parse::<tauri::Url>() {
                        Ok(url) => url,
                        Err(e) => {
                            // Not adopted, so nothing else would stop it.
                            stop_server(child);
                            return Err(format!("bad server URL {url}: {e}"));
                        }
                    };
                    if !handle.state::<Server>().adopt(child) {
                        // The window was closed while the server started.
                        return Ok(None);
                    }
                    Ok(Some((url, token, stopped)))
                });
                match started {
                    Ok(Some((url, token, stopped))) => {
                        // Allowed before anything can send the window there.
                        if let Ok(mut s) = server_url.lock() {
                            *s = Some(url.clone());
                        }
                        // Where minimize reports go, then the once-a-second
                        // check (see `Minimized`), for as long as the server
                        // is: until the window closes, or the server stops
                        // and server_stopped clears its URL.
                        let addr = format!("{}:{}", url.host_str().unwrap_or("127.0.0.1"),
                                           url.port_or_known_default().unwrap_or(80));
                        let (reports, changes) = mpsc::channel();
                        std::thread::spawn(move || report_visibility(addr, token, changes));
                        if let Some(m) = handle.try_state::<Minimized>() {
                            if let Ok(mut r) = m.reports.lock() {
                                *r = Some(reports);
                            }
                        }
                        let (poll_handle, poll_window) = (handle.clone(), window.clone());
                        let poll_url = server_url.clone();
                        std::thread::spawn(move || loop {
                            std::thread::sleep(Duration::from_secs(1));
                            let closed = poll_handle
                                .try_state::<Server>()
                                .map_or(true, |s| s.closed.load(Ordering::SeqCst));
                            let stopped = poll_url.lock().map_or(true, |s| s.is_none());
                            if closed || stopped {
                                break;
                            }
                            if let Ok(minimized) = poll_window.is_minimized() {
                                note_minimized(&poll_handle, minimized);
                            }
                        });
                        if let Err(e) = window.navigate(url) {
                            let message = format!("Could not open the Studio page: {e}");
                            log_err!("{NOT_STARTED}.\n\n{message}");
                            handle.state::<StartupError>().report(&window, NOT_STARTED, message);
                        } else if let Ok(reason) = stopped.recv() {
                            // Waited for here, after the window has gone to
                            // the server, so that a failure right after the
                            // ready frame cannot be shown first and then
                            // covered by that navigation.
                            server_stopped(&handle, &window, loading, &server_url, reason);
                        }
                    }
                    Ok(None) => {}
                    Err(message) => {
                        // Printed too: anyone debugging a launch from a
                        // terminal (`npm run studio`) sees it there, and
                        // `npm run ui` reproduces the failure with full
                        // output.
                        log_err!("{NOT_STARTED}.\n\n{message}");
                        handle.state::<StartupError>().report(&window, NOT_STARTED, message);
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

/// The functions here that need no window. That the frames match the
/// server's is tests/test_shell_frames.js, which reads this file.
#[cfg(test)]
mod tests {
    use super::*;

    fn url(s: &str) -> tauri::Url {
        s.parse().unwrap()
    }

    #[test]
    fn the_loading_page_is_allowed_in_every_form_tauri_serves_it() {
        let server = Mutex::new(None);
        assert!(navigation_allowed(&url("tauri://localhost/loading.html"), None, &server));
        assert!(navigation_allowed(&url("http://tauri.localhost/loading.html"), None, &server));
        assert!(navigation_allowed(&url("https://tauri.localhost/loading.html"), None, &server));
        assert!(navigation_allowed(&url("about:blank"), None, &server));
        // Where server_stopped sends the window back to, in a bundle.
        let bundled = loading_page_url(None, false).unwrap();
        assert!(navigation_allowed(&bundled, Some(&bundled), &server));
        assert!(navigation_allowed(&bundled, None, &server));
        // A bundle has a devUrl in its config only if one is written
        // there; it is never used.
        let dev = url("http://127.0.0.1:1430/");
        assert_eq!(loading_page_url(Some(&dev), false), Some(bundled));
    }

    #[test]
    fn under_tauri_dev_the_loading_page_is_on_its_dev_server() {
        let server = Mutex::new(None);
        let dev = url("http://127.0.0.1:1430/");
        let loading = loading_page_url(Some(&dev), true).unwrap();
        assert_eq!(loading.as_str(), "http://127.0.0.1:1430/loading.html");
        // What main passes: tests build without custom-protocol, as
        // `tauri dev` does, with or without --release.
        assert!(tauri::is_dev());
        assert_eq!(loading_page_url(Some(&dev), tauri::is_dev()), Some(loading.clone()));
        assert!(navigation_allowed(&url("http://127.0.0.1:1430/loading.html"), Some(&loading), &server));
        assert!(!navigation_allowed(&url("http://127.0.0.1:1430/loading.html"), None, &server));
        assert!(!navigation_allowed(&url("http://127.0.0.1:1430/index.html"), Some(&loading), &server));
        assert!(!navigation_allowed(&url("http://127.0.0.1:1431/loading.html"), Some(&loading), &server));
    }

    #[test]
    fn nothing_else_on_tauris_origin_is_allowed() {
        let server = Mutex::new(None);
        assert!(!navigation_allowed(&url("http://tauri.localhost/index.html"), None, &server));
        assert!(!navigation_allowed(&url("http://tauri.localhost/"), None, &server));
        assert!(!navigation_allowed(&url("http://localhost/loading.html"), None, &server));
        assert!(!navigation_allowed(&url("http://evil.example/loading.html"), None, &server));
    }

    #[test]
    fn the_server_is_allowed_only_once_reported_and_only_its_origin() {
        let server = Mutex::new(None);
        assert!(!navigation_allowed(&url("http://127.0.0.1:4000/"), None, &server));
        *server.lock().unwrap() = Some(url("http://127.0.0.1:4000/"));
        assert!(navigation_allowed(&url("http://127.0.0.1:4000/"), None, &server));
        assert!(navigation_allowed(&url("http://127.0.0.1:4000/api/state?x=1"), None, &server));
        assert!(!navigation_allowed(&url("http://127.0.0.1:4001/"), None, &server));
        assert!(!navigation_allowed(&url("https://127.0.0.1:4000/"), None, &server));
        assert!(!navigation_allowed(&url("http://localhost:4000/"), None, &server));
        // As reported: with the launch token, which the server trades for
        // a cookie and a redirect to its page.
        *server.lock().unwrap() = Some(url("http://127.0.0.1:4000/?t=abc_-123"));
        assert!(navigation_allowed(&url("http://127.0.0.1:4000/?t=abc_-123"), None, &server));
        assert!(navigation_allowed(&url("http://127.0.0.1:4000/"), None, &server), "the redirect");
        assert!(!navigation_allowed(&url("http://127.0.0.1:4001/?t=abc_-123"), None, &server));
    }

    #[test]
    fn a_ready_frame_gives_the_url_and_the_token() {
        let frame = format!(
            "{READY_PREFIX}{}\r\n",
            r#"{"url":"http://127.0.0.1:4000/?t=abc","port":4000,"token":"abc"}"#
        );
        assert_eq!(
            ready_frame(&frame),
            Some(Ok(("http://127.0.0.1:4000/?t=abc".to_string(), "abc".to_string())))
        );
        let no_token = format!("{READY_PREFIX}{}", r#"{"url":"http://127.0.0.1:4000/","port":4000}"#);
        assert!(ready_frame(&no_token).unwrap().unwrap_err().contains("no token"));
        let no_url = format!("{READY_PREFIX}{}", r#"{"port":4000,"token":"abc"}"#);
        assert!(ready_frame(&no_url).unwrap().unwrap_err().contains("no URL"));
        assert!(ready_frame(&format!("{READY_PREFIX}not json")).unwrap().unwrap_err().contains("malformed"));
        assert_eq!(ready_frame("Resume Studio → http://127.0.0.1:4000/?t=abc\n"), None);
        assert_eq!(ready_frame(&format!("{STARTUP_FAILED_PREFIX}{{}}")), None);
    }

    /// One request read off a listener on loopback, answered with `status`.
    fn one_request(status: &'static str) -> (String, std::thread::JoinHandle<String>) {
        use std::io::{Read, Write};
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap().to_string();
        let served = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let _ = stream.set_read_timeout(Some(Duration::from_secs(5)));
            let mut got = Vec::new();
            let mut buf = [0u8; 1024];
            // Headers, then the body Content-Length says (small here).
            while let Ok(n) = stream.read(&mut buf) {
                if n == 0 {
                    break;
                }
                got.extend_from_slice(&buf[..n]);
                let text = String::from_utf8_lossy(&got);
                if let Some(end) = text.find("\r\n\r\n") {
                    if text[end + 4..].ends_with('}') {
                        break;
                    }
                }
            }
            let _ = stream.write_all(format!("HTTP/1.1 {status}\r\nContent-Length: 0\r\n\r\n").as_bytes());
            String::from_utf8_lossy(&got).to_string()
        });
        (addr, served)
    }

    #[test]
    fn the_visibility_report_carries_the_token() {
        let (addr, served) = one_request("200 OK");
        assert_eq!(post_visibility(&addr, "tok-123_x", true), Ok(()));
        let request = served.join().unwrap();
        assert!(request.starts_with("POST /api/visibility HTTP/1.1\r\n"), "{request}");
        assert!(request.contains(&format!("\r\nHost: {addr}\r\n")), "{request}");
        assert!(request.contains("\r\nX-Studio-Token: tok-123_x\r\n"), "{request}");
        assert!(request.ends_with("\r\n\r\n{\"hidden\":true}"), "{request}");

        // A refusal is a failure, which report_visibility tries again.
        let (addr, served) = one_request("403 Forbidden");
        assert!(post_visibility(&addr, "wrong", false).unwrap_err().contains("403"));
        served.join().unwrap();
    }

    fn rect(x: f64, y: f64, width: f64, height: f64) -> Placement {
        Placement { x, y, width, height }
    }

    #[test]
    fn a_window_is_on_screen_when_its_title_bar_is_on_a_monitor() {
        let monitors = [rect(0.0, 0.0, 1920.0, 1080.0), rect(1920.0, 0.0, 2560.0, 1440.0)];
        assert!(on_any(monitors, &rect(100.0, 100.0, 800.0, 600.0)));
        assert!(on_any(monitors, &rect(3000.0, 200.0, 800.0, 600.0)), "the second monitor");
        // The title bar point is 40 px in and 10 px down.
        assert!(on_any(monitors, &rect(-40.0, -10.0, 800.0, 600.0)), "the corner, just");
        assert!(!on_any(monitors, &rect(-41.0, 100.0, 800.0, 600.0)), "left of the first");
        assert!(!on_any(monitors, &rect(100.0, 1070.0, 800.0, 600.0)), "below the first");
        assert!(!on_any(monitors, &rect(5000.0, 100.0, 800.0, 600.0)), "a monitor now gone");
        assert!(!on_any([], &rect(100.0, 100.0, 800.0, 600.0)), "no monitors");
    }

    /// `WindowState::load` on a file holding `text` (no file at all for None).
    fn load(name: &str, text: Option<&str>) -> (Option<Placement>, Option<bool>, WindowState) {
        let path = std::env::temp_dir()
            .join(format!("resume-studio-window-state-{}-{name}.json", std::process::id()));
        let _ = std::fs::remove_file(&path);
        if let Some(text) = text {
            std::fs::write(&path, text).unwrap();
        }
        let state = WindowState { path: Some(path.clone()), ..WindowState::default() };
        let (placement, maximized) = state.load();
        let _ = std::fs::remove_file(&path);
        (placement, maximized, state)
    }

    #[test]
    fn a_saved_window_state_is_read_back() {
        let (p, maximized, state) = load(
            "full",
            Some(r#"{"maximized":false,"x":-1200,"y":40.5,"width":1440,"height":920}"#),
        );
        let p = p.expect("a placement");
        assert_eq!((p.x, p.y, p.width, p.height), (-1200.0, 40.5, 1440.0, 920.0));
        assert_eq!(maximized, Some(false));
        assert!(!state.maximized.load(Ordering::SeqCst));
        let normal = state.normal.lock().unwrap().expect("noted as the normal placement");
        assert_eq!((normal.x, normal.width), (-1200.0, 1440.0));
    }

    #[test]
    fn a_missing_or_unreadable_window_state_is_no_state() {
        let (p, maximized, _) = load("missing", None);
        assert!(p.is_none() && maximized.is_none());
        let (p, maximized, _) = load("garbage", Some("not json {"));
        assert!(p.is_none() && maximized.is_none());
    }

    #[test]
    fn an_implausible_placement_is_dropped_and_maximized_kept() {
        for text in [
            r#"{"maximized":false,"x":0,"y":0,"width":0,"height":920}"#,
            r#"{"maximized":false,"x":0,"y":0,"width":1440,"height":200000}"#,
            r#"{"maximized":false,"x":"0","y":0,"width":1440,"height":920}"#,
            r#"{"maximized":false,"y":0,"width":1440,"height":920}"#,
        ] {
            let (p, maximized, _) = load("implausible", Some(text));
            assert!(p.is_none(), "{text}");
            assert_eq!(maximized, Some(false), "{text}");
        }
    }

    #[test]
    fn a_state_without_maximized_opens_maximized() {
        let (p, maximized, state) = load("nomax", Some(r#"{"x":0,"y":0,"width":1440,"height":920}"#));
        assert!(p.is_some());
        assert_eq!(maximized, None);
        assert!(state.maximized.load(Ordering::SeqCst));
    }

    #[test]
    fn studio_node_loses_surrounding_quotes_and_whitespace() {
        assert_eq!(unquote(r#""C:\Program Files\nodejs\node.exe""#), r"C:\Program Files\nodejs\node.exe");
        assert_eq!(unquote(r#"  "C:\node\node.exe"  "#), r"C:\node\node.exe");
        assert_eq!(unquote("'/usr/bin/node'"), "/usr/bin/node");
        assert_eq!(unquote(" node \r\n"), "node");
        assert_eq!(unquote(r"C:\node\node.exe"), r"C:\node\node.exe");
        // Only a matching pair is taken off.
        assert_eq!(unquote(r#""C:\node\node.exe"#), r#""C:\node\node.exe"#);
        assert_eq!(unquote(r#""node'"#), r#""node'"#);
        assert_eq!(unquote(r#""""#), "");
    }

    #[test]
    fn a_failed_frame_gives_its_reason() {
        let frame = format!("{STARTUP_FAILED_PREFIX}{}\r\n", r#"{"error":"Python 3.10 or newer is needed"}"#);
        assert_eq!(failed_reason(&frame).as_deref(), Some("Python 3.10 or newer is needed"));
        let garbled = format!("{STARTUP_FAILED_PREFIX}not json\n");
        assert!(failed_reason(&garbled).unwrap().ends_with(": not json"));
        let no_error = format!("{STARTUP_FAILED_PREFIX}{{}}\n");
        assert!(failed_reason(&no_error).is_some(), "still a failure");
        assert_eq!(failed_reason("Resume Studio → http://127.0.0.1:4000/\n"), None);
        assert_eq!(failed_reason(&format!("{READY_PREFIX}{{}}\n")), None);
        assert_eq!(failed_reason(&format!("  {STARTUP_FAILED_PREFIX}{{}}")), None, "only at the start");
    }

    #[test]
    fn a_reason_carries_the_last_of_stderr() {
        assert_eq!(with_said("Python 3.10 or newer is needed", ""), "Python 3.10 or newer is needed");
        assert_eq!(
            with_said("The engine failed", "ModuleNotFoundError: No module named 'yaml'"),
            "The engine failed\n\nIt said last:\n\nModuleNotFoundError: No module named 'yaml'"
        );
    }

    #[test]
    fn the_echoed_log_hides_the_token() {
        let line = "Resume Studio → http://127.0.0.1:4000/?t=abc_-123\n";
        assert_eq!(without_token(line, "abc_-123"), "Resume Studio → http://127.0.0.1:4000/?t=…\n");
        assert_eq!(without_token("a abc b abc\n", "abc"), "a … b …\n", "every time it appears");
        assert!(matches!(without_token("nothing here\n", "abc"), std::borrow::Cow::Borrowed(_)));
        // An empty token would match everywhere.
        assert_eq!(without_token(line, ""), line);
    }

    #[test]
    fn the_loading_page_is_given_the_heading_and_the_reason() {
        assert_eq!(
            startup_error_script(STOPPED, "It said \"no\"\n</script>"),
            r#"window.showStartupError && window.showStartupError("It said \"no\"\n</script>", "The Studio stopped")"#
        );
        assert_ne!(NOT_STARTED, STOPPED);
    }

    #[test]
    fn the_stderr_tail_keeps_the_last_lines() {
        let tail = StderrTail::default();
        for i in 0..STDERR_LINES + 5 {
            tail.push(format!("line {i}"));
        }
        tail.close();
        let text = tail.text(Duration::from_secs(5));
        let lines: Vec<&str> = text.lines().collect();
        assert_eq!(lines.len(), STDERR_LINES);
        assert_eq!(lines[0], "line 5");
        assert_eq!(*lines.last().unwrap(), format!("line {}", STDERR_LINES + 4));
    }

    #[test]
    fn the_stderr_tail_waits_for_the_pipe_only_so_long() {
        let tail = Arc::new(StderrTail::default());
        tail.push("Error: PYTHON points at nothing".to_string());
        let started = Instant::now();
        assert_eq!(tail.text(Duration::from_millis(50)), "Error: PYTHON points at nothing");
        assert!(started.elapsed() >= Duration::from_millis(50), "waited while open");

        // And returns as soon as the pipe closes.
        let closer = tail.clone();
        let late = std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(20));
            closer.push("and one more".to_string());
            closer.close();
        });
        let started = Instant::now();
        let text = tail.text(Duration::from_secs(10));
        late.join().unwrap();
        assert!(started.elapsed() < Duration::from_secs(5));
        assert!(text.ends_with("and one more"), "{text}");
    }
}
