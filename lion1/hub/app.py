"""The phone page at `/app`: one HTML shell, CSS and JS inlined, no framework, no build step. The
residents list (screen 1) and one resident's detail (screen 2, hash route `#r=<dir>`) both render
client-side from `/api/areas`, `/api/resident` and the `/events` SSE stream; `POST /control` and
`POST /send` are the only writes it makes. The service worker registers only over https and caches
the shell alone (no push, no offline data)."""

from __future__ import annotations

__all__ = ("app_shell", "manifest_webmanifest", "service_worker_js", "icon_svg")

_CSS = """
:root{color-scheme:light dark;--bg:#fff;--fg:#111;--muted:#666;--border:#ddd;--card:#f7f7f7;
  --on:#1a7f37;--wake:#9a6700;--off:#888;--held:#b00;--accent:#0a5cff}
@media(prefers-color-scheme:dark){:root{--bg:#0d0f12;--fg:#eee;--muted:#9aa0a6;--border:#2a2e33;
  --card:#16191d;--accent:#5b9dff}}
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:var(--bg);color:var(--fg);
  font:15px/1.4 -apple-system,BlinkMacSystemFont,Helvetica,sans-serif}
body{padding:env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom)
  env(safe-area-inset-left)}
header{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--border);
  padding:.6em .8em;padding-top:calc(.6em + env(safe-area-inset-top));z-index:10}
header .top{display:flex;justify-content:space-between;align-items:center}
#counts{color:var(--muted);font-size:.85em;margin-top:.2em}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:.35em}
.dot.on{background:var(--on)}.dot.off{background:var(--held)}
#screen{padding:.8em}
.cards{display:flex;flex-direction:column;gap:.6em}
.card,.detail{display:block;background:var(--card);border:1px solid var(--border);border-radius:10px;
  padding:.8em;text-decoration:none;color:inherit}
.card-top{display:flex;justify-content:space-between;align-items:center;gap:.5em}
.actor{font-weight:600}
.meta{color:var(--muted);font-size:.85em;margin-top:.25em}
.problem{color:var(--held);font-size:.85em;margin-top:.3em}
.pill{font-size:.78em;padding:.25em .6em;border-radius:999px;white-space:nowrap}
.pill.on{background:rgba(26,127,55,.15);color:var(--on)}
.pill.wake{background:rgba(154,103,0,.15);color:var(--wake)}
.pill.off{background:rgba(136,136,136,.15);color:var(--off)}
.pill.held{background:rgba(176,0,0,.15);color:var(--held)}
.pill.wait{background:rgba(10,92,255,.15);color:var(--accent)}
.back{display:inline-block;margin-bottom:.6em;color:var(--accent);text-decoration:none;
  min-height:44px;line-height:44px}
h3{margin:1em 0 .4em;font-size:.95em}
.row{display:flex;gap:.5em;flex-wrap:wrap;margin:.35em 0;align-items:center}
.row.inline{background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:.5em}
.hidden{display:none}
button,input,textarea{font:inherit;min-height:44px;border-radius:8px;border:1px solid var(--border);
  background:var(--bg);color:var(--fg);padding:.4em .7em}
button{background:var(--accent);color:#fff;border-color:transparent;min-width:44px}
button:disabled{opacity:.5}
input[type=number]{max-width:9em}
textarea{width:100%;min-height:5em}
.logbox{max-height:16em;overflow:auto;background:var(--bg);border:1px solid var(--border);
  border-radius:8px;padding:.4em;font:12px/1.4 ui-monospace,Menlo,monospace}
.logline{padding:.15em 0;border-bottom:1px solid var(--border)}
.logline.wake{color:var(--accent)}
table.wakes{width:100%;border-collapse:collapse;font-size:.85em}
table.wakes th,table.wakes td{text-align:left;padding:.3em .4em;border-bottom:1px solid var(--border)}
.msg{border-bottom:1px solid var(--border);padding:.4em 0}
.msg pre{white-space:pre-wrap;overflow-wrap:anywhere;margin:.2em 0}
#toast{position:fixed;left:50%;bottom:calc(1em + env(safe-area-inset-bottom));
  transform:translate(-50%,150%);background:#222;color:#fff;padding:.6em 1em;border-radius:8px;
  font-size:.9em;opacity:0;transition:transform .2s,opacity .2s;max-width:90vw;z-index:20}
#toast.show{transform:translate(-50%,0);opacity:1}
"""

# Kept to stdlib-only browser APIs (EventSource, fetch, URLSearchParams, crypto.randomUUID); no
# framework, no build step. Event delegation on #screen means the one listener survives every
# innerHTML re-render.
_JS = """
(function () {
'use strict';
var state = {
  areas: [], mode: 'list', dir: null, detail: null, detailDir: null, gen: 0, view: 0, logs: {}, pending: {},
};

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
  });
}
function money(n, unknown) {  // the mark hub.areas.partial() writes, beside the dollars it qualifies
  return '$' + (Number(n) || 0).toFixed(4) +
    (unknown === null ? ' (partial: calls of unknown cost before this count went uncounted)' :
     unknown > 0 ? ' (partial: ' + unknown + ' call(s) of unknown cost)' : '');
}
// 32 hex characters, the token the server keys a send on; getRandomValues also runs over plain http
function uid() {
  return Array.prototype.map.call(crypto.getRandomValues(new Uint8Array(16)), function (x) {
    return (x < 16 ? '0' : '') + x.toString(16);
  }).join('');
}

var toastTimer;
function toast(msg) {
  var t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(function () { t.classList.remove('show'); }, 3500);
}

setInterval(function () {
  document.getElementById('clock').textContent = new Date().toLocaleTimeString();
}, 1000);

function setConn(live) {
  document.getElementById('conn-dot').className = 'dot ' + (live ? 'on' : 'off');
  document.getElementById('conn-label').textContent = live ? 'live' : 'polling';
}

// ---- live data: SSE, with a 5s polling fallback when it errors ----
var pollTimer = null;
function startPoll() {
  setConn(false);
  if (pollTimer) return;
  pollTimer = setInterval(function () {
    fetch('/api/areas').then(function (r) { return r.json(); }).then(function (rows) {
      state.areas = rows;
      if (state.mode === 'list') renderList();
    }).catch(function () {});
  }, 5000);
}
function connect() {
  var es = new EventSource('/events');
  es.addEventListener('areas', function (ev) {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    setConn(true);
    state.areas = JSON.parse(ev.data);
    if (state.mode === 'list') renderList();
  });
  es.addEventListener('log', function (ev) {
    var d = JSON.parse(ev.data);
    var buf = state.logs[d.dir] || (state.logs[d.dir] = []);
    buf.push(d.line);
    if (buf.length > 400) buf.shift();
    if (state.mode === 'detail' && state.dir === d.dir && !d.history) appendLogLine(d.line);
  });
  es.onerror = function () { startPoll(); };
}

// ---- routing ----
function route() {
  state.view++;  // each navigation is a new page, a return to the same resident included
  var m = /^#r=(.+)$/.exec(location.hash);
  if (m) { state.mode = 'detail'; state.dir = decodeURIComponent(m[1]); loadDetail(); }
  else { state.mode = 'list'; state.dir = null; renderList(); }
}
window.addEventListener('hashchange', route);

// ---- screen 1: residents ----
function pill(r) {
  if (r.controls && r.controls.held_by_file) return ['pill held', 'HELD: ' + (r.controls.hold_reason || '')];
  if (!r.serving) return ['pill off', 'stopped'];
  if (r.in_wake) return ['pill wake', 'in a wake'];
  if (r.waiting) return ['pill wait', 'waiting ' + r.waiting];
  return ['pill on', 'serving'];
}
function renderList() {
  var serving = state.areas.filter(function (r) { return r.serving; }).length;
  var waking = state.areas.filter(function (r) { return r.in_wake; }).length;
  document.getElementById('counts').textContent =
    state.areas.length + ' resident(s) \\u00b7 ' + serving + ' serving \\u00b7 ' + waking + ' in a wake';
  document.getElementById('screen').innerHTML = state.areas.map(function (r) {
    var p = pill(r);
    return '<a class="card" href="#r=' + encodeURIComponent(r.dir) + '">' +
      '<div class="card-top"><span class="actor">' + esc(r.actor) + '</span>' +
      '<span class="' + p[0] + '">' + esc(p[1]) + '</span></div>' +
      '<div class="meta">' + esc(r.role) + ' \\u00b7 ' + r.wake + ' wake(s)' +
      (r.last_at ? ' \\u00b7 last ' + esc(r.last_at) + ' ' + esc(r.last_outcome) : '') + '</div>' +
      '<div class="meta">today ' + r.runs_today + ' run(s) \\u00b7 ' + r.mail_today + ' mail \\u00b7 ' +
      money(r.cost_today, r.unknown_today) + '</div>' +
      (r.problem ? '<div class="problem">' + esc(r.problem) + '</div>' : '') + '</a>';
  }).join('') || '<p class="meta">no residents</p>';
}

// ---- screen 2: one resident ----
function loadDetail() {
  // a response lands only on the route that asked for it: a later navigation makes it stale
  var dir = state.dir, gen = ++state.gen;
  function current() { return gen === state.gen && state.mode === 'detail' && state.dir === dir; }
  state.detailDir = null;  // no action targets a resident until its own page is shown
  document.getElementById('screen').innerHTML = '<p class="meta">loading\\u2026</p>';
  fetch('/api/resident?dir=' + encodeURIComponent(dir)).then(function (r) {
    if (!r.ok) throw new Error('resident not found');
    return r.json();
  }).then(function (data) {
    if (!current()) return;
    state.detail = data;
    state.detailDir = dir;
    renderDetail();
  }).catch(function (e) {
    if (!current()) return;
    document.getElementById('screen').innerHTML = '<a class="back" href="#">\\u2190 all residents</a>' +
      '<p class="problem">' + esc(String(e)) + '</p>';
  });
}

function parseWake(line) {
  var m = /(\\d+) in, (\\d+) out, \\$([\\d.]+)(?: \\(partial: (\\d+) call\\(s\\) of unknown cost\\))?/
    .exec(line);
  if (!m) return null;
  var ctx = /last turn stood in (\\d+) tokens/.exec(line);
  var rst = /(\\d+) session restart\\(s\\)/.exec(line);
  return { tin: m[1], tout: m[2], cost: m[3], unknown: m[4] ? +m[4] : 0,
    ctx: ctx ? ctx[1] : null, rst: rst ? +rst[1] : 0 };
}
function logLineHtml(line) {
  var w = parseWake(line);
  if (!w) return '<div class="logline">' + esc(line) + '</div>';
  return '<div class="logline wake">in ' + w.tin + ' \\u00b7 out ' + w.tout + ' \\u00b7 ' +
    money(w.cost, w.unknown) +
    (w.ctx ? ' \\u00b7 ctx ' + w.ctx : '') + (w.rst > 0 ? ' \\u00b7 ' + w.rst + ' restart(s)' : '') +
    '</div>';
}
function appendLogLine(line) {
  var box = document.getElementById('log-box');
  if (!box) return;
  var atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 8;
  box.insertAdjacentHTML('beforeend', logLineHtml(line));
  if (atBottom) box.scrollTop = box.scrollHeight;
}

function renderDetail() {
  var d = state.detail;
  if (!d) return;
  var r = d.resident, c = d.controls || {}, b = d.budget || {}, w = d.wake || {};
  var p = pill(Object.assign({}, r, { controls: c, waiting: (d.waiting || []).length }));
  var lines = state.logs[state.dir] || [];
  document.getElementById('screen').innerHTML =
    '<a class="back" href="#">\\u2190 all residents</a>' +
    '<div class="detail">' +
    '<div class="card-top"><span class="actor">' + esc(r.actor) + '</span><span class="' + p[0] +
      '">' + esc(p[1]) + '</span></div>' +
    '<div class="meta">' + esc(r.role) + ', ' + esc(r.model) + (r.pid ? ' \\u00b7 pid ' + r.pid : '') +
      (c.launchd_label ? ' \\u00b7 ' + esc(c.launchd_label) : '') + '</div>' +
    (r.problem ? '<div class="problem">' + esc(r.problem) + '</div>' : '') +

    '<h3>cap</h3><div class="row">' +
    '<input id="cap-cost" type="number" step="0.1" min="0" value="' +
      esc(b.cost_per_day != null ? b.cost_per_day : '') + '" placeholder="cost/day">' +
    '<input id="cap-wakes" type="number" step="1" min="1" value="' +
      esc(b.wakes_per_hour != null ? b.wakes_per_hour : '') + '" placeholder="wakes/hr">' +
    '<button data-act="cap">save</button></div>' +

    '<h3>controls</h3><div class="row">' +
    (c.held_by_file ? '<button data-act="resume">resume</button>' :
      '<button data-act="hold-open">hold</button>') +
    '<button data-act="wake">wake now</button>' +
    (c.idle ? '<button data-act="restart-open">restart</button>' :
      '<span class="meta">in a wake</span><button data-act="restart-open" data-force="1">' +
      'restart anyway</button>') +
    '</div>' +
    '<div id="hold-inline" class="row inline hidden"><input id="hold-reason" placeholder="reason">' +
      '<button data-act="hold-go">confirm hold</button><button data-act="cancel">cancel</button></div>' +
    '<div id="restart-inline" class="row inline hidden"><span>really restart?</span>' +
      '<button data-act="restart-go">restart</button><button data-act="cancel">cancel</button></div>' +

    '<h3>waiting' + (w.batch_after ? ' <small>batch after ' + esc(w.batch_after) + ', size ' +
      esc(String(w.batch_size || '')) + '</small>' : '') + '</h3>' +
    ((d.waiting || []).length ? '<div class="waiting">' + d.waiting.map(function (m) {
      return '<div class="msg"><b>' + esc(m.from) + '</b> ' + esc(m.subject || '') +
        ' <small>' + esc(m.deferred_at || m.at || '') + '</small></div>';
    }).join('') + '</div>' : '<p class="meta">none</p>') +

    '<h3>log</h3><div id="log-box" class="logbox">' + lines.map(logLineHtml).join('') + '</div>' +

    '<h3>wakes</h3><table class="wakes"><tr><th>time</th><th>wake</th><th>outcome</th><th>mail</th>' +
      '<th>in</th><th>$</th></tr>' + (d.wakes || []).slice(0, 30).filter(function (x) {
        return 'wake' in x;
      }).map(function (x) {
        return '<tr><td>' + esc(x.at) + '</td><td>' + x.wake + '</td><td>' + esc(x.outcome) +
          '</td><td>' + x.mail + '</td><td>' + x.tokens_in + '</td><td>' +
          money(x.cost, x.unknown) + '</td></tr>';
      }).join('') + '</table>' +

    '<h3>talk</h3><div id="talk-box" class="talk">' + (Array.isArray(d.talk) ? d.talk.map(function (m) {
      return '<div class="msg"><small>' + esc(m.at) + '</small> ' + esc(m.from) + ' \\u2192 ' +
        esc(m.to) + (m.subject ? ' <b>' + esc(m.subject) + '</b>' : '') + '<pre>' + esc(m.content) +
        '</pre></div>';
    }).join('') : '<p class="problem">' + esc(String(d.talk)) + '</p>') + '</div>' +
    '<div class="row"><input id="talk-subject" placeholder="subject (optional)"></div>' +
    '<div class="row"><textarea id="talk-content" rows="3" placeholder="message"></textarea></div>' +
    '<div class="row"><button id="talk-send">send</button></div>' +
    '</div>';
  var box = document.getElementById('log-box');
  if (box) box.scrollTop = box.scrollHeight;
}

function toggleInline(id, show, force) {
  var box = document.getElementById(id);
  if (!box) return;
  box.classList.toggle('hidden', !show);
  if (force) box.dataset.force = force;
}

// an unanswered request keeps its token, keyed by what it asks, so asking the same again replays it and
// an answer, or a different ask, never takes another's token
function token(key) {
  return state.pending[key] || (state.pending[key] = uid());
}

function sendControl(action, extra) {
  var dir = state.detailDir, view = state.view;
  if (!dir) return;
  var key = JSON.stringify(['control', dir, action, extra || null]);
  var body = Object.assign({ dir: dir, action: action, once: token(key) }, extra);
  fetch('/control', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(function (r) { return r.json(); }).then(function (j) {
    delete state.pending[key];  // answered, whatever the answer: the server keeps it for this token
    toast(j.detail || (j.ok ? 'ok' : 'failed'));
    if (state.view !== view) return;  // the page that asked is gone
    toggleInline('hold-inline', false);
    toggleInline('restart-inline', false);
    loadDetail();
  }).catch(function (e) { toast(String(e)); });
}

function sendTalk() {
  var btn = document.getElementById('talk-send');
  var subject = document.getElementById('talk-subject').value;
  var content = document.getElementById('talk-content').value;
  if (!content.trim() || !state.detailDir) return;
  btn.disabled = true;
  // an unsent message keeps its token: the same words to the same resident replay it, never mail twice
  var dir = state.detailDir, view = state.view, key = JSON.stringify(['send', dir, subject, content]);
  var form = new URLSearchParams({ dir: dir, subject: subject, content: content, once: token(key) });
  fetch('/send', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded', Accept: 'application/json' },
    body: form.toString(),
  }).then(function (r) { return r.json(); }).then(function (j) {
    if (!j.ok) throw new Error('send failed');
    delete state.pending[key];
    toast('sent');
    if (state.view !== view) return;  // the page that asked is gone
    document.getElementById('talk-content').value = '';
    loadDetail();
  }).catch(function (e) { toast(String(e)); }).then(function () { btn.disabled = false; });
}

document.getElementById('screen').addEventListener('click', function (e) {
  var t = e.target;
  if (t.id === 'talk-send') return sendTalk();
  var act = t.getAttribute && t.getAttribute('data-act');
  if (!act) return;
  if (act === 'hold-open') return toggleInline('hold-inline', true);
  if (act === 'restart-open') return toggleInline('restart-inline', true, t.dataset.force || '1x');
  if (act === 'cancel') { toggleInline('hold-inline', false); toggleInline('restart-inline', false); return; }
  if (act === 'hold-go') return sendControl('hold', { reason: document.getElementById('hold-reason').value });
  if (act === 'restart-go') {
    var forced = document.getElementById('restart-inline').dataset.force === '1';
    return sendControl('restart', forced ? { force: '1' } : {});
  }
  if (act === 'resume') return sendControl('resume', {});
  if (act === 'wake') return sendControl('wake', {});
  if (act === 'cap') {
    var cost = document.getElementById('cap-cost').value;
    var wakes = document.getElementById('cap-wakes').value;
    var body = {};
    if (cost !== '') body.cost_per_day = cost;
    if (wakes !== '') body.wakes_per_hour = wakes;
    return sendControl('cap', body);
  }
});

connect();
route();
})();
"""

_MANIFEST = """{
  "name": "lion areas",
  "short_name": "areas",
  "start_url": "/app",
  "scope": "/app",
  "display": "standalone",
  "background_color": "#0d0f12",
  "theme_color": "#0d0f12",
  "icons": [{"src": "/app/icon.svg", "sizes": "any", "type": "image/svg+xml"}]
}
"""

# cache the shell only: no offline data, no push (the page registers this worker only when served
# over https)
_SW = """const CACHE = "lion-areas-v1";
const SHELL = ["/app"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET" || new URL(event.request.url).pathname !== "/app") return;
  event.respondWith(fetch(event.request).catch(() => caches.match(event.request)));
});
"""

_ICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<rect width="64" height="64" rx="14" fill="#0d0f12"/>
<text x="32" y="43" font-family="-apple-system,Helvetica,sans-serif" font-size="30" font-weight="700"
  fill="#5b9dff" text-anchor="middle">L</text>
</svg>
"""


def app_shell() -> str:
    """The one-file phone page: inline CSS and JS, no external fonts or images. The service worker
    registers only over https; http still gets everything else."""
    return (
        "<!doctype html>\n<html>\n<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
        '<meta name="apple-mobile-web-app-capable" content="yes">\n'
        '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">\n'
        '<meta name="theme-color" content="#0d0f12" media="(prefers-color-scheme: dark)">\n'
        '<meta name="theme-color" content="#ffffff" media="(prefers-color-scheme: light)">\n'
        "<title>lion areas</title>\n"
        '<link rel="manifest" href="/app/manifest.webmanifest">\n'
        '<link rel="icon" href="/app/icon.svg" type="image/svg+xml">\n'
        '<link rel="apple-touch-icon" href="/app/icon.svg">\n'
        "<style>" + _CSS + "</style>\n"
        "</head>\n<body>\n"
        "<header>\n"
        '  <div class="top">\n'
        '    <div><span id="conn-dot" class="dot off"></span><span id="conn-label">connecting</span>'
        "</div>\n"
        '    <div id="clock"></div>\n'
        "  </div>\n"
        '  <div id="counts">loading&hellip;</div>\n'
        "</header>\n"
        '<div id="screen"></div>\n'
        '<div id="toast"></div>\n'
        "<script>" + _JS + "</script>\n"
        "<script>\n"
        'if (location.protocol === "https:" && "serviceWorker" in navigator) {\n'
        '  navigator.serviceWorker.register("/app/sw.js").catch(function () {});\n'
        "}\n"
        "</script>\n"
        "</body>\n</html>\n"
    )


def manifest_webmanifest() -> str:
    return _MANIFEST


def service_worker_js() -> str:
    return _SW


def icon_svg() -> str:
    return _ICON
