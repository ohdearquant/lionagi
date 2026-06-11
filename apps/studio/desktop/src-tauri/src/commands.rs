//! Tauri commands callable from the frontend.

use crate::backend::launch_backend;
use crate::AppState;
use tauri::{AppHandle, State};

/// Called by the setup/error screen's Retry button.
/// Attempts to locate the CLI and launch the backend again.
#[tauri::command]
pub async fn retry_backend_launch(
    app: AppHandle,
    state: State<'_, AppState>,
) -> Result<u16, String> {
    // Terminate any lingering backend
    {
        let mut guard = state.backend.lock().unwrap();
        if let Some(old) = guard.take() {
            old.terminate();
        }
    }

    match launch_backend(&app).await {
        Ok(bh) => {
            let port = bh.port;
            *state.backend.lock().unwrap() = Some(bh);
            Ok(port)
        }
        Err(e) => Err(e.to_string()),
    }
}

/// Return the current API base URL (useful for debug screens).
#[tauri::command]
pub fn get_api_base(state: State<'_, AppState>) -> Option<String> {
    state
        .backend
        .lock()
        .unwrap()
        .as_ref()
        .map(|bh| format!("http://127.0.0.1:{}", bh.port))
}
