//! Built-in setup / error screen HTML.
//!
//! Embedded as a Rust constant so the shell has no external file dependency.
//! Loaded via `document.write()` into the main window while the backend
//! is starting or when the CLI is not found.

pub const SETUP_HTML: &str = r#"<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Lion Studio</title>
  <style>
    :root {
      --bg:      #0C0D10;
      --surface: #13151A;
      --border:  rgba(255,255,255,0.08);
      --text:    #E8E6E1;
      --muted:   rgba(232,230,225,0.55);
      --accent:  #E8A33D;
      --error:   #E5604C;
      --mono:    "JetBrains Mono","SF Mono",ui-monospace,monospace;
      --sans:    -apple-system,"Inter",system-ui,sans-serif;
    }
    *{box-sizing:border-box;margin:0;padding:0}
    html,body{height:100%;background:var(--bg);color:var(--text);
      font-family:var(--sans);-webkit-font-smoothing:antialiased}
    .drag{position:fixed;top:0;left:0;right:0;height:52px;
      -webkit-app-region:drag;z-index:10}
    .wrap{display:flex;flex-direction:column;align-items:center;
      justify-content:center;min-height:100vh;padding:40px 24px}
    .card{background:var(--surface);border:1px solid var(--border);
      border-radius:12px;padding:40px 48px;max-width:520px;width:100%}
    .logo{display:flex;align-items:center;gap:12px;margin-bottom:32px}
    .mark{width:36px;height:36px;background:var(--accent);border-radius:8px;
      display:flex;align-items:center;justify-content:center;
      font-family:var(--mono);font-size:20px;font-weight:700;
      color:var(--bg);flex-shrink:0}
    .logname{font-size:18px;font-weight:600;letter-spacing:-0.01em}
    h1{font-size:22px;font-weight:600;letter-spacing:-0.02em;margin-bottom:12px}
    .err-box{display:flex;align-items:flex-start;gap:8px;padding:12px 14px;
      background:rgba(229,96,76,0.08);border:1px solid rgba(229,96,76,0.25);
      border-radius:6px;margin-bottom:28px}
    .err-glyph{color:var(--error);font-size:14px;line-height:1.5;flex-shrink:0}
    .err-text{font-family:var(--mono);font-size:12.5px;color:var(--text);
      line-height:1.5;word-break:break-word}
    .section{font-size:11px;font-weight:600;text-transform:uppercase;
      letter-spacing:0.08em;color:var(--muted);margin-bottom:8px}
    .block{background:var(--bg);border:1px solid var(--border);
      border-radius:6px;padding:12px 16px;margin-bottom:8px}
    .block code{font-family:var(--mono);font-size:13px;color:var(--accent)}
    .or{color:var(--muted);font-size:12px;text-align:center;margin:6px 0}
    .hint{font-size:12.5px;color:var(--muted);line-height:1.5;
      margin-bottom:28px;margin-top:8px}
    .hint code{font-family:var(--mono);font-size:11px;color:var(--muted)}
    .spinner{width:20px;height:20px;border:2px solid var(--border);
      border-top-color:var(--accent);border-radius:50%;
      animation:spin 0.8s linear infinite;margin:0 auto 12px}
    @keyframes spin{to{transform:rotate(360deg)}}
    .btn{width:100%;padding:11px 0;background:var(--accent);color:var(--bg);
      font-family:var(--sans);font-size:14px;font-weight:600;border:none;
      border-radius:7px;cursor:pointer;transition:opacity 0.12s;
      -webkit-app-region:no-drag}
    .btn:hover{opacity:.88}.btn:active{opacity:.76}
    .btn:disabled{opacity:.4;cursor:not-allowed}
    .status{margin-top:14px;font-family:var(--mono);font-size:12px;
      color:var(--muted);text-align:center;min-height:18px}
    .status.ok{color:#4FB477}.status.err-st{color:var(--error)}
    .hidden{display:none}
  </style>
</head>
<body>
  <div class="drag"></div>
  <div class="wrap">
    <div class="card">
      <div class="logo">
        <div class="mark">L</div>
        <span class="logname">Lion Studio</span>
      </div>
      <h1 id="hd">Starting up…</h1>
      <div id="spinner" class="spinner"></div>
      <div id="eb" class="err-box hidden">
        <span class="err-glyph">&#x2715;</span>
        <span class="err-text" id="et"></span>
      </div>
      <div id="install" class="hidden">
        <div class="section">Install lionagi</div>
        <div class="block"><code>uv pip install 'lionagi[studio]'</code></div>
        <div class="or">or</div>
        <div class="block"><code>pipx install 'lionagi[studio]'</code></div>
        <p class="hint">
          After installing, click <strong>Retry</strong>. The shell will
          re-detect the <code>li</code> CLI and start the backend.
          The CLI must be on PATH or at:<br>
          <code>~/.local/bin/li &middot; ~/.cargo/bin/li &middot; /opt/homebrew/bin/li</code>
        </p>
        <button class="btn" id="btn" onclick="doRetry()">Retry</button>
        <div class="status" id="st"></div>
      </div>
    </div>
  </div>
  <script>
    window.__showSetupScreen = function(err) {
      var msg = err || window.__STUDIO_LAUNCH_ERROR__;
      document.getElementById('spinner').classList.add('hidden');
      document.getElementById('hd').textContent =
        msg ? 'Backend not found' : 'Waiting for backend…';
      if (msg) {
        document.getElementById('et').textContent = msg;
        document.getElementById('eb').classList.remove('hidden');
        document.getElementById('install').classList.remove('hidden');
      }
    };
    if (window.__STUDIO_LAUNCH_ERROR__) window.__showSetupScreen();

    window.doRetry = async function() {
      var btn = document.getElementById('btn');
      var st  = document.getElementById('st');
      btn.disabled = true;
      btn.textContent = 'Retrying…';
      st.textContent = 'Searching for li CLI…';
      st.className = 'status';
      try {
        // Tauri 2: window.__TAURI__.core.invoke is the stable global
        var invoke = window.__TAURI__.core.invoke;
        var port = await invoke('retry_backend_launch');
        st.textContent = 'Backend started on port ' + port;
        st.className = 'status ok';
        document.getElementById('btn').textContent = 'Loading…';
      } catch(e) {
        var msg = String(e);
        document.getElementById('et').textContent = msg;
        document.getElementById('eb').classList.remove('hidden');
        st.textContent = msg;
        st.className = 'status err-st';
        btn.disabled = false;
        btn.textContent = 'Retry';
      }
    };
  </script>
</body>
</html>"#;
