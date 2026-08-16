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
use std::io::Read;
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
    #[error("failed to generate auth token: {0}")]
    AuthTokenGenerationFailed(String),
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

fn generate_auth_token_from_reader(mut entropy: impl Read) -> Result<String, LaunchError> {
    let mut buf = [0u8; 16];
    entropy
        .read_exact(&mut buf)
        .map_err(|error| LaunchError::AuthTokenGenerationFailed(error.to_string()))?;
    Ok(buf.iter().map(|b| format!("{b:02x}")).collect())
}

/// Generate a 32-hex-char random token using `/dev/urandom` (macOS-only shell).
/// Refuse to start if the OS entropy source cannot provide all 16 bytes.
pub fn generate_auth_token() -> Result<String, LaunchError> {
    let entropy = std::fs::File::open("/dev/urandom")
        .map_err(|error| LaunchError::AuthTokenGenerationFailed(error.to_string()))?;
    generate_auth_token_from_reader(entropy)
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

#[derive(serde::Deserialize)]
struct ServerIdentity {
    identity: String,
    version: String,
}

/// Ceiling on the identity response body, in bytes. Our own answer is two
/// short strings; anything approaching this is not the backend we launched.
const MAX_IDENTITY_BODY_BYTES: usize = 64 * 1024;

/// After health 2xx, verify the backend with the cheap authenticated identity probe.
/// This ensures the health endpoint belongs to our process, not a port-race squatter.
pub async fn verify_identity(port: u16, auth_token: &str) -> Result<(), LaunchError> {
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(5))
        .build()
        .unwrap_or_default();

    // Exact route path — a trailing slash would bounce through a redirect,
    // and redirects can drop the Authorization header.
    let url = format!("http://127.0.0.1:{port}/api/identity");
    let mut resp = client
        .get(&url)
        .header("Authorization", format!("Bearer {auth_token}"))
        .send()
        .await
        .map_err(|e| LaunchError::IdentityCheckFailed(e.to_string()))?;

    if !resp.status().is_success() {
        return Err(LaunchError::IdentityCheckFailed(format!(
            "status {}",
            resp.status()
        )));
    }

    // Read the body in chunks against a ceiling rather than buffering whatever
    // arrives. This probe exists because the port may be held by something
    // that is not our backend, and that something answers the request: a
    // squatter that passes /health can stream an arbitrarily large identity
    // body, and the whole-body read would hold all of it in memory before
    // anyone got to look at the two fields we want. The response we expect is
    // two short strings; the ceiling is far above that and far below a
    // problem. The client's own timeout bounds the other shape of this, a
    // body delivered slowly rather than largely.
    let mut body_bytes: Vec<u8> = Vec::new();
    loop {
        let chunk = resp
            .chunk()
            .await
            .map_err(|e| LaunchError::IdentityCheckFailed(format!("invalid response: {e}")))?;
        let Some(chunk) = chunk else { break };
        if body_bytes.len() + chunk.len() > MAX_IDENTITY_BODY_BYTES {
            return Err(LaunchError::IdentityCheckFailed(format!(
                "identity response exceeded {MAX_IDENTITY_BODY_BYTES} bytes"
            )));
        }
        body_bytes.extend_from_slice(&chunk);
    }
    let body: ServerIdentity = serde_json::from_slice(&body_bytes)
        .map_err(|e| LaunchError::IdentityCheckFailed(format!("invalid response: {e}")))?;
    if body.identity != "lionagi-studio" || body.version.trim().is_empty() {
        return Err(LaunchError::IdentityCheckFailed(format!(
            "unexpected identity {:?} version {:?}",
            body.identity, body.version
        )));
    }

    log::info!("verified lionagi studio backend version {}", body.version);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Cursor, Error, Read};
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;

    struct FailingEntropy;

    impl Read for FailingEntropy {
        fn read(&mut self, _buf: &mut [u8]) -> std::io::Result<usize> {
            Err(Error::other("entropy source failed"))
        }
    }

    #[test]
    fn auth_token_generation_rejects_a_short_entropy_read() {
        let error = generate_auth_token_from_reader(Cursor::new([7u8; 15]))
            .expect_err("15 entropy bytes must not produce a token");

        assert!(error.to_string().contains("failed to generate auth token"));
    }

    #[test]
    fn auth_token_generation_propagates_entropy_read_failures() {
        let error = generate_auth_token_from_reader(FailingEntropy)
            .expect_err("an entropy read failure must not produce a token");

        assert!(error.to_string().contains("entropy source failed"));
    }

    async fn serve_once(body: &str) -> (u16, tokio::task::JoinHandle<String>) {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let body = body.to_owned();
        let handle = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.unwrap();
            let mut request = Vec::new();
            loop {
                let mut chunk = [0u8; 1024];
                let read = socket.read(&mut chunk).await.unwrap();
                if read == 0 {
                    break;
                }
                request.extend_from_slice(&chunk[..read]);
                if request.windows(4).any(|window| window == b"\r\n\r\n") {
                    break;
                }
            }
            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            );
            socket.write_all(response.as_bytes()).await.unwrap();
            String::from_utf8(request).unwrap()
        });
        (port, handle)
    }

    #[tokio::test]
    async fn identity_check_uses_cheap_authenticated_endpoint() {
        let (port, request) =
            serve_once(r#"{"identity":"lionagi-studio","version":"0.34.1"}"#).await;

        verify_identity(port, "desktop-test-token").await.unwrap();
        let request = request.await.unwrap();

        assert!(request.starts_with("GET /api/identity HTTP/1.1\r\n"));
        assert!(request
            .to_ascii_lowercase()
            .contains("authorization: bearer desktop-test-token\r\n"));
    }

    #[tokio::test]
    async fn identity_check_rejects_an_unrelated_success_response() {
        let (port, _request) = serve_once(r#"{"identity":"not-lionagi","version":"1"}"#).await;

        let error = verify_identity(port, "desktop-test-token")
            .await
            .expect_err("an unrelated 2xx server must not pass identity verification");

        assert!(error.to_string().contains("unexpected identity"));
    }

    /// Serves one response and tolerates the client hanging up before the body
    /// is fully written, which is what a client enforcing a size ceiling does.
    /// `serve_once` unwraps its write on purpose, so it cannot serve this case.
    async fn serve_once_allowing_disconnect(body: String) -> u16 {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.unwrap();
            let mut request = Vec::new();
            loop {
                let mut chunk = [0u8; 1024];
                let read = socket.read(&mut chunk).await.unwrap();
                if read == 0 {
                    break;
                }
                request.extend_from_slice(&chunk[..read]);
                if request.windows(4).any(|window| window == b"\r\n\r\n") {
                    break;
                }
            }
            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            );
            let _ = socket.write_all(response.as_bytes()).await;
        });
        port
    }

    /// The exact answer our own backend gives, padded to a chosen length with a
    /// field `ServerIdentity` ignores. Size is then the only thing that can
    /// decide the outcome.
    fn padded_identity(total: usize) -> String {
        let prefix = r#"{"identity":"lionagi-studio","version":"0.34.1","pad":""#;
        let suffix = r#""}"#;
        let pad = total.saturating_sub(prefix.len() + suffix.len());
        format!("{prefix}{}{suffix}", "x".repeat(pad))
    }

    #[tokio::test]
    async fn identity_check_stops_reading_a_body_past_the_ceiling() {
        let port =
            serve_once_allowing_disconnect(padded_identity(MAX_IDENTITY_BODY_BYTES + 1)).await;

        let error = verify_identity(port, "desktop-test-token")
            .await
            .expect_err("a body past the ceiling must not be read to the end");

        assert!(error.to_string().contains("exceeded"), "got: {error}");
    }

    #[tokio::test]
    async fn identity_check_accepts_a_large_body_inside_the_ceiling() {
        // One byte under, same shape. Without this the test above would also
        // pass with a ceiling of zero, which would reject our own backend.
        let (port, _request) = serve_once(&padded_identity(MAX_IDENTITY_BODY_BYTES - 1)).await;

        verify_identity(port, "desktop-test-token")
            .await
            .expect("a body inside the ceiling must still verify");
    }
}
