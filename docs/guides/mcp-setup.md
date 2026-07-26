# Connect an MCP Client to LionAGI

Install the server, register it with your MCP client, and confirm the
connection is live. No LionAGI source checkout is required at any point — the
server ships inside the published wheel.

For what the server exposes once connected, see the
[MCP Server Reference](../reference/mcp-server.md).

## 1. Install

The server needs the optional `mcp` extra:

```bash
pip install 'lionagi[mcp]'
```

or, with uv:

```bash
uv pip install 'lionagi[mcp]'
```

That installs two things you will use below: the `li` console script, and the
`lionagi.mcp` package the script serves from. Both land in the environment's
`bin/` and `site-packages/`, so a virtualenv that contains nothing else is
enough.

Importing `lionagi.mcp` does not pull the extra — only serving does. If the
extra is missing, `li mcp` refuses to start with a message naming the install
command and exits with the environment-error code, so a misconfigured client
sees a failed launch rather than a server that half-works.

## 2. Register it with your client

The server speaks **stdio**. There is no HTTP listener and no port: the client
launches the process and talks to it on its standard streams.

Point the client at the `li` executable **by absolute path**. A bare `li`
depends on whatever `PATH` your client inherits, which is frequently not your
shell's:

```json
{
  "mcpServers": {
    "lion": {
      "command": "/absolute/path/to/venv/bin/li",
      "args": ["mcp"]
    }
  }
}
```

`python -m lionagi.mcp` is equivalent if you would rather name the interpreter:

```json
{
  "mcpServers": {
    "lion": {
      "command": "/absolute/path/to/venv/bin/python",
      "args": ["-m", "lionagi.mcp"]
    }
  }
}
```

The key in `mcpServers` is your client's local name for the entry and is what
you address the server by. The name the server reports over the protocol is
`lion`.

**Your client will show one tool, `request`.** That is correct and current:
every operation is a namespaced verb behind that tool, and `request(help=true)`
lists them. A client showing one entry is a healthy connection, not a truncated
one.

## 3. Environment variables

The server itself reads four variables. Every one of them is optional.

| Variable | Effect | When unset |
|----------|--------|------------|
| `LIONAGI_HOME` | Root of all on-disk state. Runs go to `$LIONAGI_HOME/runs/`, the server's own job records to `$LIONAGI_HOME/mcp/jobs/`. | Defaults to `~/.lionagi`. The directory is created on first write; nothing fails if it does not exist yet. |
| `LIONAGI_MCP_LI_BIN` | Explicit argv prefix for the `li` CLI the server spawns, split on whitespace. | The server uses the `li` script next to its own interpreter, by absolute path. Failing that, `<that interpreter> -m lionagi.cli`. `PATH` is never consulted, so leaving this unset is the normal case. |
| `LIONAGI_MCP_NOTIFY_TARGET` | Value substituted for the `{target}` placeholder in the terminal-run notification command. | The placeholder resolves to an empty string. |
| `LIONAGI_MCP_NOTIFY_COMMAND` | JSON argv list delivering a notice when a background run reaches a terminal state. Overrides the configured `notify.on_terminal` adapter. | Falls back to lionagi's own `notify.on_terminal` setting. If that is unconfigured too, no notification is sent — deliberate silence, distinct from a notifier that was configured and failed, which is reported rather than swallowed. |

`LIONAGI_RUN_ID` is also part of this surface, but it is **written** by the
server into each run it spawns so the run id can be returned before the child
starts. Setting it in the client environment would force every spawned run to
share one id. Leave it alone.

Two further points about environment:

- **Provider credentials are not read by the server.** The runs it spawns
  inherit its environment, so any API key an agent needs (`OPENAI_API_KEY` and
  the rest) must be present in the environment your MCP client launches the
  server with — not merely in your shell profile. Clients commonly launch
  servers without a login shell.
- **Working directory matters.** Project-scoped configuration under `.lionagi/`
  is resolved from the launch directory upward. Set the client entry's working
  directory to the project root if you want project profiles and settings to
  apply.

## 4. Verify the connection

Three checks, in increasing strength.

**The client lists one tool.** Look for `request` in your client's tool list.
One entry is the expected count.

**The catalog answers.** Call `request` with:

```json
{"help": true}
```

You get every verb with its required parameters, plus `verb_count`,
`available_count`, and `max_ops`. If this returns, dispatch is working.

**A verb actually runs.** `server.info` is the cheapest one that proves the
whole path, because it reports which build answered:

```json
{"ops": [{"op": "server.info", "args": {}}]}
```

```json
{
  "status": "success",
  "ops": [{"ok": true, "op": "server.info", "result": {
    "lionagi_version": "0.30.2",
    "contract_version": 1,
    "verb_count": 40,
    "absent_verb_count": 28,
    "tool_count": 1,
    "uptime_seconds": 0.045,
    "pid": 55661
  }}]
}
```

Read `lionagi_version` and `verb_count` rather than assuming them. A server
process loads its code once, at start: upgrading the package on disk changes
nothing about a client session that is already connected, and a restart command
returning success is not evidence of which code answered. `server.info` is.
`handshake` is the companion check — it returns the machine-result contract
version and the filesystem path of the CLI module actually in use, which
settles "is this the build I just installed" without inference.

If a verb comes back `ok=false`, read its `error.kind`. A `not_found` means the
name was never registered — ask `help=true` for the real spelling. An
`unavailable` means the name is catalogued but not callable here, and the error
carries the reason.

## Troubleshooting

**The client shows the server as failed to start.** Run the exact command from
your config in a terminal. `li mcp` holds the connection open on stdin and
prints a startup banner to stderr; that banner is not an error. An immediate
exit with a message about a missing module means the `mcp` extra is not
installed in that environment.

**The client shows zero tools.** The process started but the handshake did not
complete. Check that the `command` path exists and is executable, and that the
client is not passing extra arguments — `li mcp` takes only the optional
`serve` action.

**Verbs run but find nothing.** Runs and job records are read from
`$LIONAGI_HOME`. If the client launches the server with a different
`LIONAGI_HOME` than your shell uses, the server is looking at a different store.
`server.info` and `job.list` will both succeed and both be empty.
