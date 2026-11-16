# Remote legs: running agent legs on another machine

A Studio daemon started in **leg-host mode** runs agent legs for a caller on another machine.
It accepts one kind of work: an `agent` launch naming a repository it serves and a full commit.
It runs that work in a detached worktree it checks out itself, and keeps the leg's final
output for the caller. `li remote` is the client.

Leg-host mode trusts the caller with the daemon's bearer, and with nothing else. Everything a
leg-host daemon holds (its login, its configuration, its other repositories) stays out of the
leg's reach, and every request outside that one shape is refused.

## Setting up a leg host

On the machine that runs the legs, under the account they run as:

1. Install lionagi, and log in to the Claude Code CLI there.
2. Configure the repositories it serves in `~/.lionagi/leg_repos.yaml`. Both paths are absolute.
   The clone's `origin` is the only remote the host fetches from.

    ```yaml
    lionagi:
      clone: /Users/legs/repos/lionagi
      worktrees: /Users/legs/leg-worktrees/lionagi
    ```

3. Put the agent profiles legs may run as in `~/.lionagi/agents/`. They must be Claude Code
   profiles (`model: claude_code/...`).
4. Start Studio with:

    ```bash
    LIONAGI_STUDIO_LEG_HOST=1 \
    LIONAGI_STUDIO_AUTH_TOKEN=<a long random token> \
    LIONAGI_STUDIO_HOST=<the host's tailnet address> \
    li studio
    ```

`LIONAGI_STUDIO_LEG_HOST_MAX_LAUNCHES` sets how many legs run at once (default 2). A launch
beyond it is answered 429.

## Calling it

On the caller's machine, name the remote in `~/.lionagi/remotes.yaml`. The file holds the URL
and the name of the variable that carries the token, never the token itself:

```yaml
legbox:
  url: http://legbox:8765
  token_env: LIONAGI_REMOTE_LEGBOX_TOKEN
```

```bash
export LIONAGI_REMOTE_LEGBOX_TOKEN=<the leg host's token>
li remote run legbox -a reviewer --repo lionagi --head <40-hex commit> "Review the change at HEAD."
```

`li remote run` waits for the leg and prints its result. Ctrl-C cancels the remote leg.
`li remote status`, `li remote output` and `li remote cancel` take an invocation id.

## What a leg host refuses

Each of these is refused before an invocation is recorded or a process starts.

**At start-up.** The daemon does not start in leg-host mode when:

- `LIONAGI_STUDIO_AUTH_TOKEN` is unset;
- it would listen on an address that is neither loopback nor a tailnet address
  (100.64.0.0/10, fd7a:115c:a1e0::/48);
- `~/.lionagi/.mcp.json` or `~/.claude.json` names an MCP server;
- `leg_repos.yaml` configures no repository.

At start-up it also removes worktrees left behind by an earlier run.

**On a launch** (answered 422):

- any `action_kind` other than `agent`;
- any request field other than `action_kind`, `action_agent`, `action_prompt`, `action_repo` and
  `action_head`. That covers `action_cwd` (only a worktree the daemon made is a working
  directory there), `action_model` (the profile decides the model), extra arguments and a
  project;
- an `action_agent` that is not a bare name (`[a-z0-9_-]+`). The name is resolved only in the
  host's own `~/.lionagi/agents`; a repository's own `.lionagi/agents` is never searched;
- a profile whose provider is not `claude_code`, or that names no model. Other providers' child
  processes have no measured read denial for the account's credential files;
- an `action_head` that is not exactly 40 lowercase hex characters. Branch names, tags and
  short hashes are refused;
- an `action_repo` the host does not serve.

**When the leg starts** (the invocation fails with the reason):

- a head the clone cannot fetch from its `origin`;
- a worktree whose checkout is not at the requested head;
- a tree that carries configuration a leg would load: `.lionagi/`, `.mcp.json` or `.claude/`.

**Every other write route** answers 403. A caller holding the bearer can launch, read and cancel,
nothing more. A caller without it gets 401 from every route except `/health`.

## What a leg is handed

- **A working tree**: `<worktrees>/<invocation id>`, checked out detached at the requested head and
  removed when the leg ends, whether it completed, failed, timed out or was cancelled. Removal
  touches only that path, through `git worktree remove`. Before every leg launch the daemon also
  removes leftover worktrees whose invocation is no longer running.
- **An environment built from an allow-list**: `PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `LANG`,
  `LC_ALL`, `LC_CTYPE`, `TERM`, `TMPDIR`, and `CLAUDE_CODE_OAUTH_TOKEN` when the daemon has it.
  The daemon's bearer, any `LIONAGI_REMOTE_*` variable and every other credential variable stay
  with the daemon. Claude Code withholds `CLAUDE_CODE_OAUTH_TOKEN` from the processes it starts
  (observed with Claude Code 2.1.325), so the Claude login is the one credential the leg's Claude
  Code holds, and the commands it runs do not.
- **No MCP servers**: the leg runs with `--no-mcp-config`, and the start-up check above refuses a
  host that configures any.
- **The child sandbox** from the host's global `~/.lionagi/settings.yaml`. A repository's settings
  cannot relax it.

## What a leg host does not return

- **The result is the leg's final output only**: the last 1 MiB of its standard output, served by
  `GET /api/launches/{id}/result`, with `truncated` set when more was written.
- **Files a leg writes die with its worktree.** Nothing a leg leaves in its tree is kept or sent
  back.
- **Run records stay on the leg host.** The caller's own run history and `li monitor` do not show a
  remote leg.

## Protocol

| Route | Purpose |
|---|---|
| `GET /api/launches/capabilities` | `{"leg_host": true, "pinned_head": true, "max_concurrent": N}`; `li remote` refuses a daemon that is not a leg host |
| `POST /api/launches/` | start a leg; the 202 echoes `action_head` |
| `GET /api/invocations/{id}` | status |
| `GET /api/launches/{id}/result` | the result once the leg has exited |
| `POST /api/invocations/{id}/cancel` | stop the leg |
