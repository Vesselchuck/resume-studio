//! Resume Studio — the desktop shell.
//!
//! This file has one job: start `build/studio_server.js`, wait for it to
//! say which URL it is listening on, and show that URL in a window.
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
//! finds a stale port. The child is therefore killed on window close
//! and on drop, and the server is additionally started with
//! `--exit-with-parent`: its stdin is a pipe held open by this process,
//! so if this process is killed in a way that skips both handlers, the
//! pipe closes and the server stops itself.

#![cfg_attr(all(not(debug_assertions), target_os = "windows"), windows_subsystem = "windows")]

use std::io::{BufRead, BufReader};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;

use tauri::{Manager, WebviewUrl, WebviewWindowBuilder};

/// Frame the server prints once it is listening. Must match
/// READY_PREFIX in build/studio_server.js.
const READY_PREFIX: &str = "\u{1e}STUDIO_READY ";

/// Kept in Tauri's managed state so the child is stopped when the app
/// exits, however it exits.
struct Server(Mutex<Option<Child>>);

impl Drop for Server {
    fn drop(&mut self) {
        if let Ok(mut guard) = self.0.lock() {
            if let Some(mut child) = guard.take() {
                let _ = child.kill();
                let _ = child.wait();
            }
        }
    }
}

/// The project root: the directory holding package.json, build/ and ui/.
///
/// In development the binary lives in src-tauri/target/…, so walk up
/// until package.json appears. In a bundled app the project ships as a
/// resource directory, which the caller passes in instead.
fn project_root(resource_dir: Option<std::path::PathBuf>) -> std::path::PathBuf {
    if let Some(dir) = resource_dir {
        if dir.join("package.json").exists() {
            return dir;
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

            match start_server(&root) {
                Ok((url, child)) => {
                    app.manage(Server(Mutex::new(Some(child))));
                    let parsed = url.parse().map_err(|e| format!("bad server URL: {e}"))?;
                    WebviewWindowBuilder::new(app, "main", WebviewUrl::External(parsed))
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
                        .build()?;
                }
                Err(message) => {
                    // No window exists yet, so there is nowhere to render
                    // a styled error. Print it and stop: a dock icon that
                    // never opens anything, with no explanation, is the
                    // worst of the available failures. Anyone debugging a
                    // launch is already at a terminal, and `npm run ui`
                    // reproduces the same failure with full output.
                    eprintln!("Resume Studio could not start.\n\n{message}");
                    std::process::exit(1);
                }
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::Destroyed = event {
                if let Some(server) = window.app_handle().try_state::<Server>() {
                    if let Ok(mut guard) = server.0.lock() {
                        if let Some(mut child) = guard.take() {
                            let _ = child.kill();
                            let _ = child.wait();
                        }
                    }
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running Resume Studio");
}
