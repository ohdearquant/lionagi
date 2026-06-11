//! Tauri commands callable from the frontend.

use crate::{do_launch, navigate_to_spa, AppState, BackendState};
use tauri::{AppHandle, State};

/// Called by the setup/error screen's Retry button.
///
/// Single-flight: if a launch is already in progress the error is surfaced to
/// the UI rather than spawning a second backend process.  On success the
/// window is navigated to the SPA from here — the setup page cannot do it
/// itself because the destination URL replaces the page that would do it.
#[tauri::command]
pub async fn retry_backend_launch(
    app: AppHandle,
    _state: State<'_, AppState>,
) -> Result<u16, String> {
    let port = do_launch(&app).await.map_err(|e| e.to_string())?;
    navigate_to_spa(&app, port);
    Ok(port)
}

/// Return the current API base URL (useful for debug screens).
#[tauri::command]
pub fn get_api_base(state: State<'_, AppState>) -> Option<String> {
    let guard = state.state.lock().unwrap();
    match &*guard {
        BackendState::Running(bh) => Some(format!("http://127.0.0.1:{}", bh.port)),
        _ => None,
    }
}
