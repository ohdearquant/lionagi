# Hosting the Studio frontend

The Studio frontend is a static single-page app. Build it with `npm ci && npm run build`
and it produces plain HTML/CSS/JS in `dist/`, which any static host (Vercel, Netlify,
S3 + CDN, etc.) can serve as-is. On Vercel, set the project root directory to
`apps/studio/frontend`; the build command and output directory (`dist`) are already
declared in `vercel.json` alongside a catch-all rewrite to `index.html` (client-side
routing) and long-lived immutable caching for the hashed `/assets/*` bundle files.

At runtime the app resolves its API base URL in this order: an injected
`window.__STUDIO_API_BASE__` (highest priority), a build-time `VITE_STUDIO_API_BASE`
env var, a same-hostname `:8765` guess when served from a Vite dev port, and otherwise
the same origin the page was loaded from. Same-origin is the right default for a
single-origin deploy where one server (a Docker image or reverse proxy) serves both the
SPA and the API. It is the wrong default for a hosted _static_ deploy, whose origin
has no API at all — so those deploys must set the base explicitly, via either of the
first two overrides.

The lion-studio hosted deploy is one of those static deploys, and it is local-first:
the hosted page has no backend of its own. It is a static client that talks directly to
the operator's own `li studio` daemon running on their machine, the same way it would if
opened from `localhost`. So the Vite build injects
`window.__STUDIO_API_BASE__ = "http://127.0.0.1:8765"` into `index.html` on Vercel builds
only — gated on the `VERCEL` env var every Vercel build sets, so source and Docker
single-origin builds (which run the same `vite build`) keep their same-origin default. It
points every visitor's page at their own loopback daemon. (Loopback `http` from an `https`
page is exempt from mixed-content blocking, so this works from a hosted TLS origin. Set
`STUDIO_HOSTED_API_BASE` in the Vercel project env to point at a different base.) There is
no login and no server-side state for the hosted deploy to manage — if `/health` at the
resolved API base doesn't answer, or something other than the daemon answers there (another
program already on port 8765), `NoDaemonGate` (`src/components/shell/NoDaemonGate.tsx`)
shows a dismissible banner with the fix for each case rather than blanking the app; the
rest of the shell keeps rendering underneath it, and it retries in the background.

On the CLI side, bare `li studio` (equivalently `li studio --web`) is the mode built
for this deploy: it starts only the local daemon and prints/opens this hosted URL,
without building or serving any frontend locally. See the root README's "Lion
Studio" section for the other launch modes (`--docker`, `--no-frontend`, `--dev`).

## Auth: loopback vs everything else

The default daemon binds `127.0.0.1` and runs without auth — the browser tab and the
daemon are the same person's machine, and the OS user boundary is the security boundary.

For **any non-loopback backend** — an SSH/Cloudflare tunnel, a LAN bind (`--host 0.0.0.0`),
or a reverse proxy in front of the daemon — token auth is a hard **MUST**, not a
recommendation. An unauthenticated non-loopback daemon exposes your full run history and
agent spawn control to anyone who can reach the port. Set a bearer token before exposing it:

```bash
export LIONAGI_STUDIO_AUTH_TOKEN="$(openssl rand -hex 32)"
li studio start --no-frontend
```

Clients send it as `Authorization: Bearer <token>` on every request. The hosted SPA has
no token-entry UI today — it only attaches a token injected as
`window.__STUDIO_AUTH_TOKEN__` (the desktop shell does this). So a token-protected
non-loopback daemon is currently for API/desktop-shell use; treat the hosted page as
loopback-only until a token prompt ships. Rotate the token like any credential; never
commit it.

## Page analytics on the hosted deploy

The hosted deploy reports aggregate, privacy-preserving page views to a
self-hosted Umami tracker at `https://analytics.khive.ai`. It is gated on the
hosted build: `hostedAnalyticsInjector` in `vite.config.mts` injects the tracker
only when the build sets `VERCEL=1` (the same signal the API-base injector
reads) AND `STUDIO_UMAMI_WEBSITE_ID` to a UUID, so the Vercel project sets both
and every other build sets neither. Both gates matter. `VERCEL` alone would
cover any Vercel deploy, and the id alone would cover the desktop build: the
Tauri shell loads this same `dist/`, its page holds the desktop's per-launch
auth token, and its CSP is null, so a packaged build that exported the id from
its environment would ship the tracker into the authenticated desktop document.
Local, source, Docker, desktop, and packaged builds contain no analytics script
and contact no analytics host. Vite does not copy `.env*` files into the
config's process environment, so a value set only in an `.env` file is ignored;
the build process itself must export both variables. A value that is not a UUID
fails the build; the error names the variable and reports the length of the
offending value rather than echoing it.

When both are set, the build injects one inline script into `<head>`: the
before-send hook (`analytics/before-send.js`) followed by a loader
(`analytics/load-tracker.js`) that is called with the tracker's attributes. The
loader creates the tracker `<script>` element only when the hook is present, so
the tracker loads only after the hook exists; if the inline script does not run,
no tracker is added and no page view is sent at all. The element is async: it
fetches and runs as soon as it arrives and does not hold up the page's own
parsing. The tracker is told the hook's name by `data-before-send` and is
restricted to the hosted domain by `data-domains`. The tracker sends with
`fetch` and calls the hook before every send, dropping the send when the hook
returns a falsy value.

The tracker element is pinned by Subresource Integrity: its `integrity`
attribute holds the SHA-384 of the tracker build `https://analytics.khive.ai`
serves today, and `crossorigin="anonymous"` lets the browser check that hash
(the host answers with `access-control-allow-origin: *`). A copy of that build
lives at `analytics/umami-script.pinned.js` purely so a test can recompute the
hash and compare it with the constant; the build never loads that copy. When the
host's tracker changes, its hash changes, the browser refuses the script, and
page views stop, so update the pinned copy and the `TRACKER_INTEGRITY` constant
in `vite.config.mts` together. A test in `src/lib/hostedAnalytics.test.ts` fails
when the copy and the constant disagree, so the two cannot drift apart silently.

A page view carries the allowlisted fields, `website`, `hostname`, `screen`,
`language`, `url`, `referrer`, and `title`, and each value comes from the page.
`hostname` is read from `location.hostname`, `screen` from the screen's width
and height, `language` from `navigator.language`, and `website` from the tracker
element's `data-website-id`, so a caller's own `hostname`, `screen`, `language`,
or `website` does not reach the tracker. The hook sends nothing when the tracker
element is absent, since there is then no website id to send. `url` and `title`
carry the route template; a same-origin referrer is reduced to the previous
route's template, and a referrer from another origin is reduced to its origin
alone. A referrer that is not a string is dropped rather than sent. The
templated routes are `/runs/<id>`, `/invocations/<id>`, `/playbooks/<name>`, and
`/playbooks/<name>/edit`; every other path that is not a known static route
collapses to `/(other)`. The hook sends nothing for any send type other than
`event`, and nothing for an `event` payload that has a `name`, so Studio's lack
of `identify` calls and custom events is enforced even if another script tries
them.

A new route needs a template entry. Adding a route under `src/routes/` without
adding its path to the hook's static list, or without a dynamic pattern if it
carries an id, breaks the route-coverage test in
`src/lib/hostedAnalytics.test.ts`, which walks `src/routes/` and fails naming the
file until the template exists.

If a Content-Security-Policy is added to this deploy, the inline script that
holds the hook and the loader needs its hash in `script-src`, and
`https://analytics.khive.ai` must be allowed in `script-src` for the tracker
element the loader creates (`https://analytics.khive.ai/script.js`; its
`integrity` attribute is checked by the browser and needs no CSP entry of its
own). Because the tracker sends with `fetch`, `https://analytics.khive.ai` must
also appear in `connect-src`.
