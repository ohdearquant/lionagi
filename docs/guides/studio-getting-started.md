# Get started with Studio

Lion Studio is a web interface for local LionAGI work. The daemon runs on your
machine, where it can use your installed agents and durable state. The browser
connects to that daemon to display work and send commands.

## Install the Studio dependencies

In a project environment:

```bash
uv add "lionagi[studio]"
```

Or with an activated `pip` environment:

```bash
python -m pip install "lionagi[studio]"
```

## Prepare Claude Code for Operator

Operator uses the locally installed Claude Code CLI by default. The
`lionagi[studio]` extra installs Studio, but it does not install Claude Code or
authenticate a Claude account.

Install and authenticate Claude Code before sending the first Operator
instruction:

```bash
npm install -g @anthropic-ai/claude-code
claude --version
claude auth login
```

Claude Code can also authenticate when you launch `claude` interactively. Its
existing CLI login is used directly; no Anthropic API key is required in
LionAGI for a subscription-backed login. See the
[Claude Code setup guide](https://code.claude.com/docs/en/setup) for supported
installation and enterprise authentication options.

Operator runs that CLI on the host. The Studio image contains LionAGI and the
frontend bundle but no Claude Code CLI, so Operator does not run inside
`li studio --docker`; use a host install for Operator.

## Start Studio

```bash
li studio
```

The default starts the API daemon at `http://127.0.0.1:8765` and opens the hosted
web client at [lion-studio.khive.ai](https://lion-studio.khive.ai). The hosted
page is the frontend; execution and stored history remain on your machine.
Keep the command running while using Studio or its scheduler. `li studio` opens the page
with a one-time pairing code, which is how the daemon knows the page came from
you: a code works once, within two minutes. If you reload the page or open the
hosted address yourself, the page asks you to run `li studio` again; with the
daemon already running, that opens a newly paired page without starting a
second daemon. Use `--no-open` when you do not want LionAGI to open a browser;
`li studio` then prints the one-time link instead.

### Authentication and browser trust

By default, `LIONAGI_STUDIO_AUTH_TOKEN` is unset and the daemon requires no
bearer token, but its API is not open to every caller that can reach it. A
request with no `Origin` header, as from the CLI or a script, is accepted. A
browser page is accepted when its origin is loopback (`localhost`,
`127.0.0.0/8`, or `::1`), when it is the daemon's own origin, or when it sends
the bearer this launch paired it with. A page on any other origin is refused
with 401. `li studio` opens the hosted page with a one-time pairing code and
the page exchanges that code for the bearer, which is how the hosted page works
without a token configured. Host validation and CORS do not authenticate the
caller and are not proof that a human approved a request.

The default browser origins, `CORS_ORIGINS`, are `http://localhost:5173`,
`http://localhost:3000`, `http://localhost:3765`, and
`https://lion-studio.khive.ai`. Only a page on one of these origins can redeem
a pairing code; the first three are local development ports.

Set `LIONAGI_STUDIO_AUTH_TOKEN` in the daemon's environment before starting it
to require `Authorization: Bearer <token>` for API requests and the
schema/docs endpoints. `/health` and CORS preflight remain unauthenticated.
Pairing hands a page the configured token, so the hosted page keeps working
once it is paired, and every other client must supply the matching bearer. The
bundled UI in `--docker` mode has no token-entry field either, so with a token
set inside the container its API calls get 401; the desktop shell injects a
token for its own daemon into its frontend.

Authentication, Host validation, and browser origins are defined in
`lionagi/studio/app.py` and `lionagi/studio/config.py`, and pairing in
`lionagi/studio/services/pairing.py`. Operator decisions use
`lionagi/studio/services/operator.py`; frontend credential handling is defined
in `apps/studio/frontend/src/lib/api.ts`.

### Choose a launch mode

Choose a launch mode for your setup:

| Command | When to use it |
| --- | --- |
| `li studio` or `li studio --web` | Use the hosted client with your local daemon. |
| `li studio --no-open` | Start the default daemon without opening a browser; open the hosted client yourself. |
| `li studio --docker` | Run the bundled frontend and backend in Docker. |
| `li studio --no-frontend` | Run only the local API, for an existing client or API integration. |
| `li studio --dev` | Develop the frontend from a repository checkout, with Vite hot reload. |

`--web`, `--docker`, `--no-frontend`, and `--dev` are mutually exclusive.
`--no-open` applies to hosted-client mode. In a noninteractive terminal, the
default also leaves opening the browser to you.

`--host` sets the daemon's bind address and defaults to `127.0.0.1`; it applies
to the default, `--no-frontend`, and `--dev` modes. A non-loopback value such
as `--host 0.0.0.0` exposes the API to other machines, and the daemon also
reports that in a startup warning. Docker mode takes no host address:
`li studio --docker` publishes the port on all interfaces by default, as
described below. `LIONAGI_STUDIO_HOST` sets the same address for a daemon
started without the flag.

Docker mode pulls `ghcr.io/ohdearquant/lion-studio:latest` and serves the bundled
UI and API on port 8765 by default. It bind-mounts `~/.lionagi` read-write at
`/root/.lionagi` so state persists outside the container. If present,
`~/.claude/plugins` is mounted read-only. A symlink in the `agents`, `skills`,
`playbooks`, or `teams` directory under `~/.lionagi` is followed to its target,
and that target is mounted read-only when it passes the launcher's allowed-root
check; disallowed targets are skipped with a warning. A symlink to a directory
mounts the directory. A symlink to a file mounts the directory that contains
the file, so every sibling file in it becomes readable in the container too; a
target file that sits directly in the home directory would mount all of the
home directory.

The launcher currently uses `-p 8765:8765`, without a host address, and the
container listens on `0.0.0.0:8765`. With Docker's default port-binding
configuration, this publishes on all host interfaces. Docker network/daemon
settings and host firewalls can narrow actual reachability. The local-only
default for this launch mode is tracked in
[the Docker port-binding issue](https://github.com/ohdearquant/lionagi/issues/3561).
Until that changes, use a custom Docker invocation with an explicit loopback
mapping such as `-p 127.0.0.1:8765:8765` when access should stay local.

For bearer-token enforcement, `LIONAGI_STUDIO_AUTH_TOKEN` must be set
**inside the container**. `li studio --docker` does not forward that variable: exporting
it on the host alone does not configure container authentication. Supply the
container environment explicitly in a custom Docker invocation before allowing
remote access. A Host-header check is not a substitute for authentication.

If Docker is missing, the command exits with an installation message. Install
and start Docker, or use the default hosted-client mode. If the image pull
fails, Studio attempts to use a cached image. These launch and mount behaviors
are defined in `lionagi/studio/cli.py` and `apps/studio/Dockerfile`.

Development mode requires the repository's `apps/studio/frontend` directory
and Node.js. It is not a frontend mode for a package-only installation.
Launch-mode behavior is defined in `lionagi/studio/cli.py`.

## Confirm the daemon is reachable

In a second terminal:

```bash
li doctor
```

The `studio_daemon` check should be healthy. If the browser cannot connect,
confirm that `li studio` is still running and that you are connecting to the
same daemon. The default daemon address is `http://127.0.0.1:8765`.

## Find your way around

The left rail has **Mission Control**, **Library**, **Schedules**, and **Teams**,
with **System** near the bottom. Mission Control opens the overview of active
work, recent runs, and items needing attention. **Fleet** and **Attention** are
tabs beside **Overview**: Fleet browses execution history, while Attention
expands Mission Control's attention queue. These labels come from
`apps/studio/frontend/src/messages/en.json` and
`apps/studio/frontend/src/components/shell/IconRail.tsx`.

The speech-bubble button near the top of the rail opens **Operator**. This
dock stays available while you move between pages. The command palette opens
with <kbd>Cmd</kbd>/<kbd>Ctrl</kbd>+<kbd>K</kbd>.

See [Studio surfaces](../reference/studio-surfaces.md) for the current actions
on each page and [the Studio model](../concepts/studio-model.md) for how the
objects relate.

## Send your first Operator instruction

1. Open Operator with the speech-bubble button, or press
   <kbd>Cmd</kbd>/<kbd>Ctrl</kbd>+<kbd>J</kbd>.
2. Send `Reply with exactly: operator-ready` with <kbd>Enter</kbd>.
   Watch the reply and tool activity stream into the conversation.
3. Once the turn completes, open the **Fleet** tab beside Overview to inspect
   recorded Operator execution. A text-only reply does not show **Open run**;
   that control appears only on a successful tool result containing a run ID.
   Later turns can reuse the conversation's persisted branch and run record.
4. Reload the page and reopen Operator. The daemon restores the conversation
   history.

Continue with [Operator](studio-operator.md) for per-request permissions,
Stop, and follow-up instructions, or [Schedules](studio-schedules.md) to
arrange recurring work.
