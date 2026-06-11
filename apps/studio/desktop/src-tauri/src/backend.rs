//! Backend process management: spawn `li studio --no-frontend`, health-poll,
//! and clean termination on app exit.
//!
//! ## Process-kill approach (unix)
//!
//! The child is spawned with `.process_group(0)` so it becomes the leader of
//! a new process group (pgid = child.pid).  On termination we issue
//! `kill(-pgid, SIGTERM)`, wait up to `SIGTERM_GRACE`, then
//! `kill(-pgid, SIGKILL)`.  This ensures all grandchildren (`uvicorn`, worker
//! threads, etc.) die even if `li` ignores signals.
//!
//! `BackendHandle` is owned by `AppState` behind a `Mutex`.  The
//! `on_window_event(Destroyed)` hook in `lib.rs` takes it and calls
//! `terminate()`.  If the app crashes the OS reclaims the child anyway.

#[cfg(unix)]
use std::os::unix::process::CommandExt as _;

use crate::port::{find_free_port, find_li_cli};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};
use tauri::{AppHandle, Manager};

/// How long to wait for the health endpoint before giving up.
const HEALTH_TIMEOUT: Duration = Duration::from_secs(30);
/// Interval between health poll attempts.
const HEALTH_POLL_INTERVAL: Duration = Duration::from_millis(250);
/// Grace period for SIGTERM before SIGKILL.
const SIGTERM_GRACE: Duration = Duration::from_secs(5);

#[derive(Debug, thiserror::Error)]
pub enum LaunchError {
    #[error("li CLI not found — install with: uv pip install 'lionagi[studio]'")]
    CliNotFound,
    #[error("failed to find a free port: {0}")]
    NoFreePort(#[from] std::io::Error),
    #[error("failed to spawn backend process: {0}")]
    SpawnFailed(String),
    #[error(
        "backend health check timed out after {0:.1}s — check backend logs for startup errors"
    )]
    HealthTimeout(f64),
}

impl serde::Serialize for LaunchError {
    fn serialize<S: serde::Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        s.serialize_str(&self.to_string())
    }
}

pub struct BackendHandle {
    pub port: u16,
    pub cli_path: PathBuf,
    child: Child,
}

impl BackendHandle {
    /// Gracefully stop the backend: SIGTERM the process group, wait up to
    /// `SIGTERM_GRACE`, then SIGKILL the group if it is still alive.
    pub fn terminate(mut self) {
        #[cfg(unix)]
        {
            let pid = self.child.id() as libc::pid_t;
            // Negative value → signal the whole process group
            unsafe { libc::kill(-pid, libc::SIGTERM) };

            let deadline = Instant::now() + SIGTERM_GRACE;
            loop {
                match self.child.try_wait() {
                    Ok(Some(_)) => return,
                    Ok(None) => {
                        if Instant::now() >= deadline {
                            unsafe { libc::kill(-pid, libc::SIGKILL) };
                            let _ = self.child.wait();
                            return;
                        }
                        std::thread::sleep(Duration::from_millis(50));
                    }
                    Err(_) => {
                        let _ = self.child.kill();
                        let _ = self.child.wait();
                        return;
                    }
                }
            }
        }
        #[cfg(not(unix))]
        {
            let _ = self.child.kill();
            let _ = self.child.wait();
        }
    }
}

/// Build a [`Stdio`] that appends to a log file in the app log directory.
/// Falls back to `Stdio::null()` if the directory is unavailable.
fn log_stdio(app: &AppHandle, suffix: &str) -> Stdio {
    (|| -> Option<std::fs::File> {
        let log_dir = app.path().app_log_dir().ok()?;
        std::fs::create_dir_all(&log_dir).ok()?;
        let path = log_dir.join(format!("studio-backend-{suffix}.log"));
        std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(path)
            .ok()
    })()
    .map(Stdio::from)
    .unwrap_or_else(Stdio::null)
}

/// Locate the CLI, spawn the backend, and poll the health endpoint.
///
/// Returns `Ok(BackendHandle)` once the backend is ready.  The caller
/// (in `lib.rs`) is responsible for navigating the main window to the SPA.
pub async fn launch_backend(app: &AppHandle) -> Result<BackendHandle, LaunchError> {
    let cli = find_li_cli().ok_or(LaunchError::CliNotFound)?;
    let port = find_free_port()?;

    log::info!(
        "launching backend: {} studio --no-frontend --port {port}",
        cli.display()
    );

    let mut cmd = Command::new(&cli);
    cmd.args(["studio", "--no-frontend", "--port", &port.to_string()])
        .env("LIONAGI_STUDIO_HOST", "127.0.0.1")
        .stdout(log_stdio(app, "stdout"))
        .stderr(log_stdio(app, "stderr"));

    // On unix, spawn into a new process group so kill(-pgid, sig) reaches
    // the entire subtree (uvicorn workers, etc.).
    #[cfg(unix)]
    cmd.process_group(0);

    let child = cmd
        .spawn()
        .map_err(|e: std::io::Error| LaunchError::SpawnFailed(e.to_string()))?;

    wait_for_health(&format!("http://127.0.0.1:{port}/health")).await?;

    log::info!("backend ready on port {port}");

    Ok(BackendHandle {
        port,
        cli_path: cli,
        child,
    })
}

/// Poll `url` until it returns HTTP 2xx or the timeout expires.
async fn wait_for_health(url: &str) -> Result<(), LaunchError> {
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(5))
        .build()
        .unwrap_or_default();

    let started = Instant::now();
    loop {
        match client.get(url).send().await {
            Ok(r) if r.status().is_success() => return Ok(()),
            _ => {}
        }
        if started.elapsed() >= HEALTH_TIMEOUT {
            return Err(LaunchError::HealthTimeout(started.elapsed().as_secs_f64()));
        }
        tokio::time::sleep(HEALTH_POLL_INTERVAL).await;
    }
}
