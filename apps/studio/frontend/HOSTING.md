# Hosting the Studio frontend

The Studio frontend is a static single-page app. Build it with `npm ci && npm run build`
and it produces plain HTML/CSS/JS in `dist/`, which any static host (Vercel, Netlify,
S3 + CDN, etc.) can serve as-is. On Vercel, set the project root directory to
`apps/studio/frontend`; the build command and output directory (`dist`) are already
declared in `vercel.json` alongside a catch-all rewrite to `index.html` (client-side
routing) and long-lived immutable caching for the hashed `/assets/*` bundle files.

At runtime the app resolves its API base URL in this order: an injected
`window.__STUDIO_API_BASE__` (desktop shell), a build-time `VITE_STUDIO_API_BASE` env
var, a same-hostname `:8765` guess when served from a Vite dev port, and otherwise the
same origin the page was loaded from — except when the page itself is served over
https from a non-localhost hostname (as a hosted static deploy is), in which case it
defaults to `http://127.0.0.1:8765`, since a real daemon never serves https itself.

This reflects the app's local-first architecture: the hosted page has no backend of
its own. It is a static client that talks directly to the operator's own `li studio`
daemon running on their machine, the same way it would if opened from `localhost`.
There is no login and no server-side state for the hosted deploy to manage — if the
daemon isn't running, the app shows a state explaining how to start it and keeps
retrying rather than rendering broken panels.

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

The frontend prompts for the token and sends it as `Authorization: Bearer <token>` on
every request. Rotate it like any credential; never commit it.
