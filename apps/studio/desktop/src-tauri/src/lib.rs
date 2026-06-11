mod backend;
mod commands;
mod port;
mod setup_html;

use std::sync::Mutex;
use tauri::{AppHandle, Manager};

pub use backend::{BackendHandle, LaunchError};

/// Single lifecycle state for the backend process.
///
/// Transitions:
///   Idle → Preparing  (single-flight slot claimed; nothing spawned yet)
///   Preparing → Launching  (child spawned and stored; health poll begins)
///   Preparing → Idle  (spawn failed)
///   Launching → Running  (health + identity check passed)
///   Launching → Idle  (health/identity failed; handle terminated)
///   Running → Preparing  (retry; old handle terminated before respawn)
///   any → ShuttingDown  (window Destroyed or app Exit; terminal)
///
/// The spawned child lives inside this state for its whole lifetime — the
/// health poll borrows it under the lock per iteration rather than taking it
/// out, so `shutdown()` can always reach and terminate it.
pub enum BackendState {
    Idle,
    Preparing,
    Launching(BackendHandle),
    Running(BackendHandle),
    ShuttingDown,
}

pub struct AppState {
    pub state: Mutex<BackendState>,
    /// Per-launch bearer token generated once at startup.
    pub auth_token: String,
}

impl AppState {
    /// Atomically take the current handle and set `ShuttingDown`.
    pub fn shutdown(&self) {
        let handle = {
            let mut guard = self.state.lock().unwrap();
            let prev = std::mem::replace(&mut *guard, BackendState::ShuttingDown);
            match prev {
                BackendState::Launching(h) | BackendState::Running(h) => Some(h),
                _ => None,
            }
        };
        if let Some(h) = handle {
            h.terminate();
        }
    }
}

/// Initialization script injected into every document before any page scripts.
///
/// Reads the port from the `#port=N` hash fragment, sets
/// `window.__STUDIO_API_BASE__`, and exposes the per-launch bearer token so
/// the SPA can attach `Authorization` headers to every API request.
fn build_init_script(auth_token: &str) -> String {
    format!(
        r#"(function() {{
  var m = location.hash.match(/[#&]port=(\d+)/);
  if (m) {{
    window.__STUDIO_API_BASE__ = 'http://127.0.0.1:' + m[1];
  }}
  window.__STUDIO_AUTH_TOKEN__ = '{auth_token}';
}})();"#
    )
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let auth_token = backend::generate_auth_token();

    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .manage(AppState {
            state: Mutex::new(BackendState::Idle),
            auth_token: auth_token.clone(),
        })
        .setup(move |app| {
            let init_script = build_init_script(&auth_token);

            // Window starts hidden; shown after the loading screen is written.
            tauri::WebviewWindowBuilder::new(
                app.handle(),
                "main",
                tauri::WebviewUrl::App("index.html".into()),
            )
            .initialization_script(&init_script)
            .title("")
            .inner_size(1440.0, 900.0)
            .min_inner_size(1100.0, 720.0)
            .title_bar_style(tauri::TitleBarStyle::Overlay)
            .hidden_title(true)
            .visible(false)
            .center()
            .build()?;

            let handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                // Show the setup/loading screen while we wait for the backend.
                if let Some(win) = handle.get_webview_window("main") {
                    let escaped = setup_html::SETUP_HTML
                        .replace('\\', "\\\\")
                        .replace('`', "\\`")
                        .replace("${", "\\${");
                    let _ = win.eval(format!(
                        "document.open(); document.write(`{escaped}`); document.close();"
                    ));
                    let _ = win.show();
                }

                match do_launch(&handle).await {
                    Ok(port) => navigate_to_spa(&handle, port),
                    Err(e) => {
                        log::error!("backend launch failed: {e}");
                        if let Some(win) = handle.get_webview_window("main") {
                            let msg = serde_json::to_string(&e.to_string())
                                .unwrap_or_else(|_| "\"unknown error\"".into());
                            let _ = win.eval(format!(
                                "window.__STUDIO_LAUNCH_ERROR__ = {msg}; \
                                 if (typeof window.__showSetupScreen === 'function') \
                                   window.__showSetupScreen();"
                            ));
                        }
                    }
                }
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            commands::retry_backend_launch,
            commands::get_api_base,
        ])
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::Destroyed = event {
                window.state::<AppState>().shutdown();
            }
        })
        .build(tauri::generate_context!())
        .expect("error while running tauri application")
        .run(|app, event| match event {
            tauri::RunEvent::ExitRequested { .. } | tauri::RunEvent::Exit => {
                app.state::<AppState>().shutdown();
            }
            _ => {}
        });
}

/// Spawn the backend through the state machine and advance it to `Running`.
///
/// Single-flight: the `Preparing` claim is taken under the lock before any
/// I/O, so two concurrent calls can never both spawn.  Replace-running:
/// terminates the existing `Running` handle before respawning.  Fail-closed:
/// on any error the handle is terminated and state resets to `Idle`.  The
/// child handle never leaves the shared state during the health poll, so
/// `shutdown()` can terminate it at any moment.
pub async fn do_launch(app: &AppHandle<tauri::Wry>) -> Result<u16, LaunchError> {
    let app_state = app.state::<AppState>();
    let auth_token = app_state.auth_token.clone();

    // --- Claim the single-flight slot (atomically, before any I/O) ---
    let old = {
        let mut guard = app_state.state.lock().unwrap();
        match &*guard {
            BackendState::ShuttingDown | BackendState::Preparing | BackendState::Launching(_) => {
                return Err(LaunchError::LaunchInProgress)
            }
            BackendState::Idle | BackendState::Running(_) => {
                std::mem::replace(&mut *guard, BackendState::Preparing)
            }
        }
    };
    if let BackendState::Running(old) = old {
        // terminate() blocks up to the SIGTERM grace period — run it off the
        // async executor so a slow old backend can't stall the runtime.
        let _ = tauri::async_runtime::spawn_blocking(move || old.terminate()).await;
    }

    // --- Spawn ---
    let handle = match backend::spawn_backend(app, &auth_token) {
        Ok(h) => h,
        Err(e) => {
            reset_preparing_to_idle(&app_state);
            return Err(e);
        }
    };
    let port = handle.port;

    // --- Store the child BEFORE the health poll so shutdown() can reach it ---
    {
        let mut guard = app_state.state.lock().unwrap();
        match &*guard {
            BackendState::Preparing => *guard = BackendState::Launching(handle),
            _ => {
                // Shutdown raced us between the claim and the store.
                drop(guard);
                handle.terminate();
                return Err(LaunchError::LaunchInProgress);
            }
        }
    }

    // --- Health poll: borrow the handle under the lock per iteration ---
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(5))
        .build()
        .unwrap_or_default();
    let url = format!("http://127.0.0.1:{port}/health");
    let started = std::time::Instant::now();
    loop {
        // Liveness + ownership check.  If shutdown() took the handle, stop.
        {
            let mut guard = app_state.state.lock().unwrap();
            match &mut *guard {
                BackendState::Launching(h) => {
                    if h.has_exited() {
                        let prev = std::mem::replace(&mut *guard, BackendState::Idle);
                        drop(guard);
                        if let BackendState::Launching(h) = prev {
                            h.terminate(); // reaps the exited child
                        }
                        return Err(LaunchError::ProcessExited);
                    }
                }
                _ => return Err(LaunchError::LaunchInProgress),
            }
        }

        match client.get(&url).send().await {
            Ok(r) if r.status().is_success() => break,
            _ => {}
        }
        if started.elapsed() >= backend::HEALTH_TIMEOUT {
            take_and_terminate_launching(&app_state);
            return Err(LaunchError::HealthTimeout(started.elapsed().as_secs_f64()));
        }
        tokio::time::sleep(backend::HEALTH_POLL_INTERVAL).await;
    }

    // --- Authenticated identity check: GET /api/stats with bearer token ---
    if let Err(e) = backend::verify_identity(port, &auth_token).await {
        take_and_terminate_launching(&app_state);
        return Err(e);
    }

    // --- Transition to Running (in place; handle never left the state) ---
    {
        let mut guard = app_state.state.lock().unwrap();
        let prev = std::mem::replace(&mut *guard, BackendState::Idle);
        match prev {
            BackendState::Launching(h) => *guard = BackendState::Running(h),
            other => {
                // Shutdown raced us; it already terminated the handle.
                *guard = other;
                return Err(LaunchError::LaunchInProgress);
            }
        }
    }

    log::info!("backend ready on port {port}");
    Ok(port)
}

/// Navigate the main window to the bundled SPA with the backend port in the
/// URL hash — the init script reads it and sets `window.__STUDIO_API_BASE__`.
/// Used by both the initial launch and the setup screen's retry command.
pub fn navigate_to_spa(app: &AppHandle<tauri::Wry>, port: u16) {
    if let Some(win) = app.get_webview_window("main") {
        let url_str = format!("tauri://localhost/index.html#port={port}");
        if let Ok(url) = url_str.parse::<tauri::Url>() {
            if let Err(e) = win.navigate(url) {
                log::error!("navigate to SPA failed: {e}");
            }
        }
    }
}

/// Reset `Preparing` → `Idle`; leaves any other state (e.g. `ShuttingDown`) alone.
fn reset_preparing_to_idle(app_state: &AppState) {
    let mut guard = app_state.state.lock().unwrap();
    if matches!(&*guard, BackendState::Preparing) {
        *guard = BackendState::Idle;
    }
}

/// Take the handle out of `Launching`, terminate it, and reset to `Idle`.
/// Any other state (e.g. `ShuttingDown` already took the handle) is preserved.
fn take_and_terminate_launching(app_state: &AppState) {
    let handle = {
        let mut guard = app_state.state.lock().unwrap();
        match std::mem::replace(&mut *guard, BackendState::Idle) {
            BackendState::Launching(h) => Some(h),
            other => {
                *guard = other;
                None
            }
        }
    };
    if let Some(h) = handle {
        h.terminate();
    }
}
