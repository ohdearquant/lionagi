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
//! ## Lifecycle state machine
//!
//! `AppState` holds a `Mutex<BackendState>` that is never `None`. The states
//! are `Idle`, `Launching(BackendHandle)`, `Running(BackendHandle)`, and
//! `ShuttingDown`. The spawned child is placed into `Launching` *before* the
//! health poll begins, so every exit path (timeout, window destroy, app quit)
//! can reach and terminate it.
//!
//! See `lib.rs` for the full state-machine transition diagram.

#[cfg(unix)]
use std::os::unix::process::CommandExt as _;

use crate::port::{find_free_port, find_li_cli};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};
use tauri::{AppHandle, Manager};

/// How long to wait for the health endpoint before giving up.
pub const HEALTH_TIMEOUT: Duration = Duration::from_secs(30);
/// Interval between health poll attempts.
pub const HEALTH_POLL_INTERVAL: Duration = Duration::from_millis(250);
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
    #[error("backend exited before health check completed")]
    ProcessExited,
    #[error("launch already in progress")]
    LaunchInProgress,
    #[error("server identity check failed after health: {0}")]
    IdentityCheckFailed(String),
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
    /// Returns `true` if the child process has already exited.
    pub fn has_exited(&mut self) -> bool {
        match self.child.try_wait() {
            Ok(Some(_)) | Err(_) => true,
            Ok(None) => false,
        }
    }

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

/// Generate a 32-hex-char random token using `/dev/urandom` (macOS-only shell).
pub fn generate_auth_token() -> String {
    let mut buf = [0u8; 16];
    // /dev/urandom is always available on macOS and never blocks.
    if let Ok(mut f) = std::fs::File::open("/dev/urandom") {
        use std::io::Read;
        let _ = f.read_exact(&mut buf);
    }
    buf.iter().map(|b| format!("{b:02x}")).collect()
}

/// Locate the CLI and spawn the backend process.  Health polling and state
/// transitions are owned by `lib.rs::do_launch`, which stores the returned
/// handle in the shared state machine before polling begins.
pub fn spawn_backend(app: &AppHandle, auth_token: &str) -> Result<BackendHandle, LaunchError> {
    let cli = find_li_cli().ok_or(LaunchError::CliNotFound)?;
    let port = find_free_port()?;

    log::info!(
        "launching backend: {} studio --no-frontend --port {port}",
        cli.display()
    );

    let mut cmd = Command::new(&cli);
    cmd.args(["studio", "--no-frontend", "--port", &port.to_string()])
        .env("LIONAGI_STUDIO_HOST", "127.0.0.1")
        .env("LIONAGI_STUDIO_AUTH_TOKEN", auth_token)
        // The webview loads the SPA from the tauri custom protocol, so API
        // calls are cross-origin; the backend's default CORS allowlist only
        // covers localhost dev ports.
        .env("CORS_ORIGINS", "tauri://localhost")
        .stdout(log_stdio(app, "stdout"))
        .stderr(log_stdio(app, "stderr"));

    // On unix, spawn into a new process group so kill(-pgid, sig) reaches
    // the entire subtree (uvicorn workers, etc.).
    #[cfg(unix)]
    cmd.process_group(0);

    let child = cmd
        .spawn()
        .map_err(|e: std::io::Error| LaunchError::SpawnFailed(e.to_string()))?;

    Ok(BackendHandle {
        port,
        cli_path: cli,
        child,
    })
}

/// After health 2xx, verify the backend identity with an authenticated GET /api/stats.
/// This ensures the health endpoint belongs to our process, not a port-race squatter.
pub async fn verify_identity(port: u16, auth_token: &str) -> Result<(), LaunchError> {
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(5))
        .build()
        .unwrap_or_default();

    // Exact route path — a trailing slash would bounce through a redirect,
    // and redirects can drop the Authorization header.
    let url = format!("http://127.0.0.1:{port}/api/stats");
    let resp = client
        .get(&url)
        .header("Authorization", format!("Bearer {auth_token}"))
        .send()
        .await
        .map_err(|e| LaunchError::IdentityCheckFailed(e.to_string()))?;

    if resp.status().is_success() {
        Ok(())
    } else {
        Err(LaunchError::IdentityCheckFailed(format!(
            "status {}",
            resp.status()
        )))
    }
}
