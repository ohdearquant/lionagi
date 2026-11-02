# Hosting the Studio frontend

The Studio frontend is a static single-page app. Build it with `npm ci && npm run build`
and it produces plain HTML/CSS/JS in `dist/`, which any static host (Vercel, Netlify,
S3 + CDN, etc.) can serve as-is. On Vercel, set the project root directory to
`apps/studio/frontend`; the build command and output directory (`dist`) are already
declared in `vercel.json` alongside a catch-all rewrite to `index.html` (client-side
routing) and long-lived immutable caching for the hashed `/assets/*` bundle files.

At runtime the app resolves its API base URL in this order: a `?api=` query override
(highest priority, persisted per browser tab), an injected `window.__STUDIO_API_BASE__`,
a build-time `VITE_STUDIO_API_BASE` env var, and otherwise the same origin the page was
loaded from. The query override accepts HTTP(S) origins on `localhost`, any IPv4
loopback address in `127.0.0.0/8`, or IPv6 `::1`. Hostnames under `*.localhost`
and IPv4-mapped IPv6 addresses are deliberately excluded from this browser override;
use an explicit supported loopback address instead. For example,
`?api=http://127.0.0.2:8766` selects a daemon bound to that loopback address and port.

Same-origin is the right default for a single-origin deploy where one server (a Docker
image or reverse proxy) serves both the SPA and the API. It is the wrong default for a
hosted _static_ deploy, whose origin has no API at all — so those deploys must set the
base explicitly through injected `window.__STUDIO_API_BASE__` or the build-time
`VITE_STUDIO_API_BASE` environment variable. The `?api=` option is a viewer-selected
loopback override, not deployment configuration.

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
shows a recovery screen in place of the app with the fix for each case, and retries in
the background. A daemon that answers but refuses the page (401) gets its own screen,
which names `li studio` as the fix and does not retry. The data-dependent application
renders only after a daemon answers successfully; the recovery screen cannot be
dismissed to reveal it.

On the CLI side, bare `li studio` (equivalently `li studio --web`) is the mode built
for this deploy: it starts only the local daemon and prints/opens this hosted URL,
without building or serving any frontend locally. See the root README's "Lion
Studio" section for the other launch modes (`--docker`, `--no-frontend`, `--dev`).

## Auth: loopback vs everything else

The default daemon binds `127.0.0.1` and runs without a configured token. Loopback is
reachable by every local account and process. A request with neither an `Origin` nor a
`Sec-Fetch-Site` header, as from the command line or a script, is accepted. A browser
request without `Origin` is judged by `Sec-Fetch-Site`: `none` and `same-origin` pass;
`same-site`, `cross-site`, any other value and repeated headers need the launch bearer.
A browser that sends no `Sec-Fetch-Site` is not told apart from a script. This admission
is a property of the request, not proof the caller is local, so the OS user boundary is
not a security boundary: set a token before starting the daemon on a shared machine.
A page on another origin still has to show that
`li studio` opened it. With no token, the daemon refuses API requests from a browser
origin that is neither loopback nor its own unless they carry the bearer of its launch,
which it keeps in memory only. `li studio` opens the hosted page with a one-time
pairing code in the URL fragment (`#pair=<code>`, single use, 120 seconds); the
page removes the code from the address bar on load, exchanges it for that bearer
and a decision capability at `POST /api/studio/pair`, and keeps that launch bearer
in `localStorage` for its own origin, bound to the daemon's API base. A configured
`LIONAGI_STUDIO_AUTH_TOKEN` returned by the same route is held in memory for that
page load only and never stored. The capability is kept in `sessionStorage` for
that tab and sent in `X-Studio-Decision-Token` on the two Operator decision
requests only. The stored bearer is checked with one request before the app
mounts, and the app mounts only on a 2xx answer. The bearer stays usable across
reloads, tabs and bookmarks until the daemon refuses it (a restarted daemon
draws a new launch bearer); the page then forgets it and asks for `li studio`
again;
with the daemon already running, that opens a freshly paired page over the
daemon's same-user reconnect socket without starting a second daemon. No HTTP
route issues a code.

For **any non-loopback backend** — an SSH/Cloudflare tunnel, a LAN bind (`--host 0.0.0.0`),
or a reverse proxy in front of the daemon — token auth is a hard **MUST**, not a
recommendation. An unauthenticated non-loopback daemon exposes your full run history and
agent spawn control to anyone who can reach the port. Set a bearer token before exposing it:

```bash
export LIONAGI_STUDIO_AUTH_TOKEN="$(openssl rand -hex 32)"
li studio start --no-frontend
```

Clients send it as `Authorization: Bearer <token>` on every request. The hosted SPA has
no token-entry UI. It attaches a token injected as `window.__STUDIO_AUTH_TOKEN__` (the
desktop shell does this) or the one `li studio` pairs it with. `li studio` asks the
daemon on `127.0.0.1`, so pairing serves the machine the daemon runs on; a
token-protected daemon reached over a tunnel or a LAN is for API and desktop-shell use.
Rotate the token like any credential; never commit it.

The API destination is selected once at page load and shared by data requests,
health probes, pairing, and the endpoint display. Query or session storage changes
during client-side navigation take effect only after reloading the page.

When a viewer override selects that destination, the shell token is sent only if
the destination has the same absolute origin as the injected `__STUDIO_API_BASE__`
captured at page load. If the issuing origin is missing, cannot be parsed, or differs
from the destination, the override receives no token. Changing the injected base
after load does not change this issuer match.
Without a viewer override at load time, the loaded base retains token authentication,
including relative proxy paths such as `/proxy`, the empty same-origin base, and
injected or build-time absolute bases. Adding `?api=` after load no longer withholds
the token for that loaded base: both requests and their authentication continue to use
the loaded destination until reload. The token itself is read when each request is made,
so a token injected after load is sent to the loaded destination under these same rules;
a base injected after load changes neither the destination nor the issuing origin.
These deployment settings are trusted and used as supplied: the viewer-override origin
check does not validate configured URLs or prevent requests caused by malformed
deployment settings.
