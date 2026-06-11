//! Free-port finding and CLI location utilities.
//!
//! Both functions are tested against real filesystem/network operations —
//! no mocks required because they work with temp dirs and real TCP binding.

use std::net::TcpListener;
use std::path::{Path, PathBuf};

/// Bind port 0 on loopback; the OS assigns a free ephemeral port.
/// Returns the port number, or an error if binding fails.
pub fn find_free_port() -> std::io::Result<u16> {
    let listener = TcpListener::bind("127.0.0.1:0")?;
    Ok(listener.local_addr()?.port())
}

/// Search for the `li` CLI in priority order:
///
/// 1. `LIONAGI_CLI` env var (explicit override)
/// 2. `which li` via PATH
/// 3. `~/.local/bin/li`
/// 4. `~/.cargo/bin/li`
/// 5. `/opt/homebrew/bin/li`
/// 6. `/usr/local/bin/li`
///
/// Returns `Some(path)` for the first candidate that resolves to an executable
/// file, or `None` if none are found.
pub fn find_li_cli() -> Option<PathBuf> {
    // 1. Explicit env override
    if let Ok(p) = std::env::var("LIONAGI_CLI") {
        let path = PathBuf::from(&p);
        if is_executable(&path) {
            return Some(path);
        }
    }

    // 2. PATH resolution
    if let Some(p) = which_li() {
        if is_executable(&p) {
            return Some(p);
        }
    }

    // 3-6. Well-known install locations
    let candidates: &[&str] = &[
        "~/.local/bin/li",
        "~/.cargo/bin/li",
        "/opt/homebrew/bin/li",
        "/usr/local/bin/li",
    ];

    for &raw in candidates {
        if let Some(path) = expand_path(raw) {
            if is_executable(&path) {
                return Some(path);
            }
        }
    }

    None
}

/// Expand a `~`-prefixed path using the HOME env var.
fn expand_path(path: &str) -> Option<PathBuf> {
    if let Some(rest) = path.strip_prefix("~/") {
        let home = std::env::var("HOME").ok()?;
        Some(PathBuf::from(home).join(rest))
    } else {
        Some(PathBuf::from(path))
    }
}

/// Resolve `li` via PATH by scanning each directory.
fn which_li() -> Option<PathBuf> {
    let path_var = std::env::var("PATH").unwrap_or_default();
    for dir in path_var.split(':') {
        let candidate = Path::new(dir).join("li");
        if is_executable(&candidate) {
            return Some(candidate);
        }
    }
    None
}

/// True if `path` points to a regular file with execute permission.
fn is_executable(path: &Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    path.metadata()
        .map(|m| m.is_file() && (m.permissions().mode() & 0o111 != 0))
        .unwrap_or(false)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;
    use tempfile::TempDir;

    #[test]
    fn find_free_port_returns_nonzero() {
        let port = find_free_port().expect("should find a free port");
        assert!(port > 0, "port must be > 0");
    }

    #[test]
    fn find_free_port_returns_valid_twice() {
        let p1 = find_free_port().unwrap();
        let p2 = find_free_port().unwrap();
        assert!(p1 > 0 && p2 > 0);
    }

    #[test]
    fn find_free_port_is_actually_free() {
        let port = find_free_port().unwrap();
        // Should be able to bind immediately after release
        let listener = TcpListener::bind(format!("127.0.0.1:{port}"));
        assert!(
            listener.is_ok(),
            "port {port} reported free but couldn't be bound"
        );
    }

    fn make_executable(dir: &TempDir) -> std::path::PathBuf {
        let fake = dir.path().join("li");
        std::fs::write(&fake, b"#!/bin/sh\n").unwrap();
        let mut perms = fake.metadata().unwrap().permissions();
        perms.set_mode(0o755);
        std::fs::set_permissions(&fake, perms).unwrap();
        fake
    }

    fn with_env<F: FnOnce()>(key: &str, val: Option<&str>, f: F) {
        let old = std::env::var(key).ok();
        match val {
            Some(v) => std::env::set_var(key, v),
            None => std::env::remove_var(key),
        }
        f();
        match old {
            Some(v) => std::env::set_var(key, v),
            None => std::env::remove_var(key),
        }
    }

    #[test]
    fn find_li_cli_respects_env_var() {
        let tmp = TempDir::new().unwrap();
        let fake = make_executable(&tmp);

        with_env("LIONAGI_CLI", Some(fake.to_str().unwrap()), || {
            let found = find_li_cli();
            assert_eq!(found.as_deref(), Some(fake.as_path()));
        });
    }

    #[test]
    fn find_li_cli_ignores_nonexistent_env_var_path() {
        with_env("LIONAGI_CLI", Some("/nonexistent/path/to/li"), || {
            // Should not panic; may or may not find a real li
            let _ = find_li_cli();
        });
    }

    #[test]
    fn find_li_cli_finds_file_on_custom_path() {
        let tmp = TempDir::new().unwrap();
        let fake = make_executable(&tmp);

        let old_path = std::env::var("PATH").unwrap_or_default();
        let new_path = format!("{}:{old_path}", tmp.path().display());

        with_env("LIONAGI_CLI", None, || {
            with_env("PATH", Some(&new_path), || {
                let found = find_li_cli();
                assert_eq!(found.as_deref(), Some(fake.as_path()));
            });
        });
    }

    #[test]
    fn non_executable_file_is_not_returned_via_env() {
        let tmp = TempDir::new().unwrap();
        let fake = tmp.path().join("li");
        std::fs::write(&fake, b"#!/bin/sh\n").unwrap();
        let mut perms = fake.metadata().unwrap().permissions();
        perms.set_mode(0o644); // not executable
        std::fs::set_permissions(&fake, perms).unwrap();

        with_env("LIONAGI_CLI", Some(fake.to_str().unwrap()), || {
            let found = find_li_cli();
            assert_ne!(found.as_deref(), Some(fake.as_path()));
        });
    }

    #[test]
    fn expand_path_handles_tilde() {
        let home = std::env::var("HOME").unwrap_or_else(|_| "/tmp".to_string());
        let expanded = expand_path("~/.local/bin/li").unwrap();
        assert_eq!(expanded, PathBuf::from(&home).join(".local/bin/li"));
    }

    #[test]
    fn expand_path_absolute() {
        let p = expand_path("/usr/local/bin/li").unwrap();
        assert_eq!(p, PathBuf::from("/usr/local/bin/li"));
    }
}
