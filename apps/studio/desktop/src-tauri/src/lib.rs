mod backend;
mod commands;
mod port;
mod setup_html;

use std::sync::Mutex;
use tauri::Manager;

pub use backend::BackendHandle;

pub struct AppState {
    pub backend: Mutex<Option<BackendHandle>>,
}

/// Initialization script injected into every document before any page script.
///
/// Reads the port from the `#port=N` hash fragment and sets
/// `window.__STUDIO_API_BASE__` synchronously so the SPA's
/// `api.ts::resolveApiBase()` finds it at module-evaluation time.
const INIT_SCRIPT: &str = r#"
(function() {
  var m = location.hash.match(/[#&]port=(\d+)/);
  if (m) {
    window.__STUDIO_API_BASE__ = 'http://127.0.0.1:' + m[1];
  }
})();
"#;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .manage(AppState {
            backend: Mutex::new(None),
        })
        .setup(|app| {
            // Build the main window pointing at the bundled SPA root, with the
            // initialization script that extracts the port from the URL hash.
            // The window is initially invisible; we show it after the first
            // document is ready (either the setup page or the SPA).
            tauri::WebviewWindowBuilder::new(
                app.handle(),
                "main",
                tauri::WebviewUrl::App("index.html".into()),
            )
            .initialization_script(INIT_SCRIPT)
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
                // We write it directly into the webview via document.write()
                // so there is no external file dependency.
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

                match backend::launch_backend(&handle).await {
                    Ok(bh) => {
                        let port = bh.port;
                        {
                            let state = handle.state::<AppState>();
                            *state.backend.lock().unwrap() = Some(bh);
                        }
                        // Navigate to the SPA with port in hash fragment.
                        // INIT_SCRIPT fires in the new document before the
                        // SPA's module scripts and sets __STUDIO_API_BASE__.
                        if let Some(win) = handle.get_webview_window("main") {
                            let url_str = format!("tauri://localhost/index.html#port={port}");
                            if let Ok(url) = url_str.parse::<tauri::Url>() {
                                if let Err(e) = win.navigate(url) {
                                    log::error!("navigate to SPA failed: {e}");
                                }
                            }
                        }
                    }
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
                let state = window.state::<AppState>();
                if let Ok(mut guard) = state.backend.lock() {
                    if let Some(bh) = guard.take() {
                        bh.terminate();
                    };
                };
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
