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
page is the frontend; the daemon it pairs with holds the execution and stored
history. Keep the command running while using Studio or its scheduler. `li studio`
opens the page with a one-time pairing code, which lets the daemon give the
bearer only to a page that presents a code from this launch: a code works once,
within two minutes. If you reload the page or open the hosted address yourself,
the page asks you to run `li studio` again; with the
daemon already running, that opens a newly paired page without starting a
second daemon. Use `--no-open` when you do not want LionAGI to open a browser;
`li studio` then prints the one-time link instead.

### Authentication and browser trust

By default, `LIONAGI_STUDIO_AUTH_TOKEN` is unset and the daemon requires no
bearer token for a request with neither an `Origin` nor a `Sec-Fetch-Site`
header, as from the command line or a script. A browser request without
`Origin` is judged by `Sec-Fetch-Site`: `none` and `same-origin` pass;
`same-site`, `cross-site`, any other value and repeated headers need the bearer
this launch paired the page with. A browser that sends no `Sec-Fetch-Site` is
not told apart from a script. This admission is a property of the request, not
proof the caller is local. A request carrying `Origin` passes when its origin
is loopback (`localhost`, `127.0.0.0/8`, or `::1`) or the daemon's own origin;
any other origin needs that bearer or is refused with 401. `li studio` opens the
hosted page with a one-time pairing code and the page exchanges that code for
the bearer, which is how the hosted page works without a token configured. Host
validation and CORS do not authenticate the caller and are not proof that a
human approved a request.

The default browser origins, `CORS_ORIGINS`, are `http://localhost:5173`,
`http://localhost:3000`, `http://localhost:3765`, and
`https://lion-studio.khive.ai`. Only a page on one of these origins can redeem
a pairing code; the first three are local development ports.

Set `LIONAGI_STUDIO_AUTH_TOKEN` in the daemon's environment before starting it
to require `Authorization: Bearer <token>` for API requests and the
schema/docs endpoints. `/health`, CORS preflight, and the `POST /api/studio/pair`
exchange remain unauthenticated; the pairing route needs no bearer because the
one-time code is its credential. The daemon also serves the single-page app shell
and its hashed assets without a bearer. Pairing hands a page the configured
token, so the hosted page keeps working once it is paired, and every other
client must supply the matching bearer. The
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

`--host` sets the address the daemon is reached on and defaults to `127.0.0.1`.
In the default, `--no-frontend`, and `--dev` modes it is the daemon's bind
address; in Docker mode it is the address the container's port is published
on, as described below. A non-loopback value such as `--host 0.0.0.0` exposes
the API to other machines; with no token set, the daemon warns at startup
whenever that address is one other machines can reach.

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

`li studio --docker` publishes the container's port on the address `--host`
names, which defaults to `127.0.0.1`, so only this machine reaches the
container. `--host` takes an address, not an address and port: the port comes
from `--port`, and a value carrying one (`[::1]:8765`) is refused before docker
runs. On `--docker` that address also has to be an IP address literal
(`127.0.0.1`, `0.0.0.0`, `192.168.1.5`, `::1`); a hostname is refused too,
because docker does not resolve one in the host half of a port mapping. A bare
IPv6 address is read whole, so `::1:8765` is the eight-group address
`::1:8765`, not `::1` at port `8765`.
To publish on another interface, either pass `--host` that address, or run the
image with your own `docker run -p`:

```bash
li studio --docker --host 192.168.1.5   # reachable at 192.168.1.5:8765
docker run --rm -p 192.168.1.5:8765:8765 \
  -e LIONAGI_STUDIO_HOST=192.168.1.5 \
  -v ~/.lionagi:/root/.lionagi \
  ghcr.io/ohdearquant/lion-studio:latest
```

On a docker release older than 28.0.0 a port published to a loopback address is
also reachable from hosts on the same L2 segment (the same switch); 28.0.0 and
later keep it to this machine.

Publishing anywhere other than loopback exposes the daemon to everyone who can
reach that address, and with `LIONAGI_STUDIO_AUTH_TOKEN` unset nothing else
stands between them and the API. Set the token before publishing on another
interface, as described in
[Hosting the Studio frontend](https://github.com/ohdearquant/lionagi/blob/main/apps/studio/frontend/HOSTING.md). The CLI
passes it to the container as `-e LIONAGI_STUDIO_AUTH_TOKEN` — the name only,
so docker reads the value from your environment and it never appears in the
command line.

The daemon checks a request's `Host` header against the addresses it expects.
This is a guard against DNS rebinding, not access control: it reads the header,
never the source address, so any client that can reach the port and sends an
admitted `Host` gets through — the bearer token is the access control.
`li studio --docker` publishes on a concrete address (`127.0.0.1`,
`192.168.1.5`, `::1`) and tells the container that address, so a client that
reaches it there and sends that address as `Host` is admitted. `0.0.0.0` and
`::` name no single address a client connects to, so a client elsewhere on the
network reaches the daemon by one of the machine's real addresses and must send
that address as `Host`: set `LIONAGI_STUDIO_HOST` on the container to it, or the
daemon admits only the loopback names `localhost`, `127.0.0.1` and `::1`. The
wildcard itself is never an admitted `Host`: it is refused like any other name
no page is opened at, so `Host: [::]:8765` answers 400.

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
