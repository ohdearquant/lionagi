# Agent runtime internals

Reference material extracted from comments/docstrings in `lionagi/ln/`,
`lionagi/agent/`, `lionagi/session/`, and `lionagi/tools/`. Each heading is
pointed to by a `# See docs/internals/agent-runtime.md#<anchor>` comment at
its call site.

<a id="create-agent-branch-origin"></a>

## create-agent-branch-origin

`CREATE_AGENT_BRANCH_ORIGIN_KEY` (`lionagi/agent/factory.py`) is stamped into
`branch.metadata` for every `Branch` `create_agent` produces. The key's
presence — not the current invocation's profile — is the durable record that
a branch's system message was composed via `create_agent` (role header +
policy block) rather than a bare profile body. It round-trips through
`Branch.to_dict()`/`from_dict()`, so a resumed leg can consult the persisted
branch itself instead of re-deriving "was this create_agent-composed?" from
whatever profile the resuming invocation happens to supply. See
`lionagi/cli/agent.py` `_run_agent`'s system-prompt reapply guard.

<a id="external-hook-wiring"></a>

## external-hook-wiring

`_wire_external_hooks` (`lionagi/agent/factory.py`) attaches
`hooks_external` entries (parsed by `apply_hooks_from_settings`) to the seam
their event maps to.

- `PreToolUse`/`PostToolUse` → `branch.acts` (always present).
- All other supported events → `branch._hooks` (a `HookBus`), present only
  once the branch is owned by a `Session`. A standalone `create_agent`
  branch has none yet, so those entries queue on
  `branch._pending_hook_bus_entries` (kept for the branch's lifetime, not
  cleared on first use) instead of being dropped.
- `Branch.attach_hook_bus` — the only seam that ever assigns `branch._hooks`
  (`Session.include_branches`, the lazy `Session.hooks` property) — syncs
  queued entries onto whichever bus is current, so a configured
  `UserPromptSubmit`/`SessionStart`/`SessionEnd`/`PostToolUseFailure` hook
  attaches once the branch joins a `Session` and re-attaches if reparented.

<a id="mcp-trust-decision"></a>

## mcp-trust-decision

`ActionManager.load_mcp_config()` no longer implies trust when its
`mcp_security` argument is omitted — an omitted policy falls through to the
wrapper's fail-closed default. `_load_mcp` (`lionagi/agent/factory.py`)
reaching `MCPSecurityConfig.trusted()` already required an explicit trust
act: `spec.mcp_config_path` set directly, `mcp_path` resolved from the
operator's own home-level `.mcp.json` (same "global config is inherently
trusted" precedent as settings.yaml's always-loaded global file), or
`trust_project_settings=True` for a project-level file. This is lionagi's
one documented compatibility decision for its own MCP auto-load consumer,
not a silent default in the generic library call.

<a id="mcp-server-forwarding"></a>

## mcp-server-forwarding

`apply_forwarded_mcp_servers` (`lionagi/agent/factory.py`) writes a resolved
MCP server set into a CLI request's kwargs. Every spawn path that resolves a
server set applies it here — one implementation for "can this leg be given a
set?".

- `provider_accepts_forwarded_mcp` reports capability, not delivery (Claude
  CLI takes the set as a request kwarg; codex takes it as
  `-c mcp_servers.<name>.<field>` overrides). Whether a spawn actually
  handed anything over: `request_carries_forwarded_mcp` on the produced
  request.
- `exclusive` means the servers dict is the whole set, not an addition —
  an empty set means "no servers": Claude CLI needs `strict_mcp_config`;
  codex, which has no wholesale clear, needs each otherwise-loaded server
  disabled by name via `_discover_ambient_codex_mcp_server_names`, which
  raises `ConfigurationError` if it can't enumerate ambient servers (an
  unenforceable allowlist must fail closed).
- `allowed_names` widens the caller's allowlist beyond the forwarded set — a
  name the caller allows but didn't describe stays enabled rather than
  excluded. `known_server_names` adds names ambient discovery wouldn't
  report. Both apply only under `exclusive`.
- For codex, only fields in `_CODEX_MCP_SERVER_FIELDS` (verified against
  `codex mcp list --json`'s field set) are forwarded; an unsupported field
  raises `ConfigurationError`. `env`/`http_headers` may carry secrets and
  must never land on argv, so they route through
  `_write_codex_mcp_secret_profile` to `$CODEX_HOME/<name>.config.toml`
  (loaded via `-p <name>`) instead of a `-c` override.
  `env_http_headers` values are env-var *names*, not secrets, so those stay
  on the `-c` path.
- `_forward_mcp_to_cli_request` reaches the per-turn request kwargs a CLI
  provider subprocess actually reads (unlike `_load_mcp`, inert for CLI
  providers). With `resolved_servers` given, that set is handed over as-is
  and no config file is looked for.
- A generated codex secret-profile name is `lionagi-mcp-` + 32 hex chars.
  The name must carry this because a resumed leg's profile was written by a
  different process. A caller-supplied name in that exact shape is treated
  as ours and replaced (a resumed leg re-spawns from its persisted request,
  carrying the profile the first run generated and already deleted);
  anything else is refused, never overwritten.

<a id="safe-path-construction"></a>

## safe-path-construction

`_build_safe_path` (`lionagi/ln/_utils.py`) is shared, symlink-safe path
construction for `create_path`/`acreate_path` — both call this so they share
identical traversal/containment semantics.

Containment is always checked against the resolved (symlink-safe)
candidate. The returned `_SafePath` pair carries that resolved candidate —
the only path callers may use for `mkdir`/existence side effects — alongside
the caller-facing spelling (relative `directory` → relative caller-facing
path; absolute → absolute). Filesystem side effects must act on `resolved`,
never `caller_facing`, which a symlink swapped in after validation can still
redirect.

Validation happens once, at call time; nothing is reserved against later
filesystem changes. Callers needing race-safety against a concurrently
mutating relative `directory` should pass an absolute directory instead.

<a id="dual-lock-ordering"></a>

## dual-lock-ordering

`async_synchronized` (`lionagi/ln/_utils.py`): when the instance also
carries a `self._lock` (threading lock), the wrapper acquires *both* — async
lock first, then the threading lock via a non-blocking spin — so
async-decorated methods mutually exclude `@synchronized` sync callers in
other threads. The async lock serializes async callers, so at most one task
ever contends for the threading lock per instance, keeping `RLock`
thread-ownership semantics intact and letting the decorated body reenter
`@synchronized` methods. Lock order is strictly async-then-sync; sync
holders never await, so the spin is bounded and never deadlocks.

<a id="hook-bus-reattachment"></a>

## hook-bus-reattachment

`Branch.attach_hook_bus` (`lionagi/session/branch.py`) sets a branch's
`HookBus` and (re)registers any external handlers queued for bus
attachment.

A standalone `create_agent` branch has no bus yet, so `hooks_external`
entries bound to bus-only events (`UserPromptSubmit`,
`SessionStart`/`SessionEnd`/`PostToolUseFailure`) queue on
`_pending_hook_bus_entries` (`lionagi.agent.factory._wire_external_hooks`)
rather than being dropped. That list is retained for the branch's lifetime,
so a branch moved between sessions (`Session.remove_branch` then
`include_branches`/`new_branch` elsewhere) re-registers the same external
handlers on its new session's bus. Re-attaching the same bus is a no-op for
entries already registered on it. Every seam that gives a branch a bus
(`Session.include_branches`, the lazy `Session.hooks` property) must route
through this method or queued handlers never attach.

Each registered handler is wrapped with an origin-branch filter
(`Branch._origin_filtered_handler`) so a bus shared by multiple branches
never cross-fires one branch's hook for another's event. Switching to a
different bus — or detaching via `attach_hook_bus(None)` — first unregisters
every wrapper this branch put on the old bus.

<a id="non-finite-float-detection"></a>

## non-finite-float-detection

`lionagi/ln/_json_dump.py`: orjson writes `inf`, `-inf`, `nan` as `null`,
indistinguishable from a genuine null on read. Callers that ask for the
check (`check_non_finite=True` on `json_dumpb`) get a loud `ValueError`
naming the offending path instead.

`_locate_non_finite` walks the object the way orjson does, covering every
form orjson encodes natively — a walk that follows only `default()` misses
values inside a dataclass, `Enum`, or numpy array. Forms that can carry a
float: `float`, `dict`, `list`, `tuple` (and subclasses), dataclasses,
`Enum` members (written by value), numpy arrays/scalars under
`OPT_SERIALIZE_NUMPY`. `str`, `int`, `bool`, `None`, `bytes`, `datetime`,
`date`, `time`, `UUID` cannot.

Two forms are outside what any walk can decide:

- `orjson.Fragment` copies pre-serialized bytes verbatim, without parsing or
  calling `default()` — a `null` inside is indistinguishable from one a
  non-finite float would have produced. Fragment contents are the caller's
  to validate.
- A future orjson may encode a container type natively that this list does
  not name; the list is written against orjson's documented native types,
  not enforced against the installed version at run time.

Cost: on a 200-item object with one legitimate null, the traversal costs
~20x the dump it guards; scanning the output for `null` first costs about
half a dump again, so gating on it doesn't help a payload with any null
(most have one) — hence `check_non_finite` defaults off. A null-free result
is provably clean, so the walk is skipped in that case.

`raise_if_non_finite` is for callers persisting through a serializer other
than `json_dumpb` — notably stdlib `json`, which writes `inf`/`-inf`/`nan`
as `Infinity`/`NaN`. Python reads those tokens back, so the writer never
notices; every strict parser rejects them, breaking whatever downstream
boundary reads the file next.

<a id="signal-handler-takeover-and-restore"></a>

## signal-handler-takeover-and-restore

`lionagi/ln/concurrency/utils.py`'s `run_async` takes over `SIGINT`/`SIGTERM`
only when the previous handler can be given back. `signal.getsignal()`
reports `None` for a handler installed outside Python, and
`signal.signal(signum, None)` raises, so restoring one is impossible in that
case — taking it over would leave this runner's handler installed for the
rest of the process. Abstaining per signal costs the caller's own
cancellation wiring for that signal but keeps whatever was already there
working.

On the way out, each restore stands alone (runs in a `finally`, often while
an exception is already propagating): a failure restoring one signal must
not strand the others with this runner's handler still installed. Restore
failures are logged rather than raised.

<a id="untrusted-recall-wrapper"></a>

## untrusted-recall-wrapper

`KhiveInjectionProvider` (`lionagi/tools/khive_injection.py`) wraps recalled
content in an `<untrusted-context>` block before injecting it into a
branch's context, since that content originates from prior (possibly
attacker-influenced) tool output/repo content and must not be mistaken for
an instruction.

Two layers keep the wrapper from being escaped: (1) any literal closing-tag
substring inside the recalled text is neutralized so it can't terminate the
block early, and (2) a per-call random nonce on both tags means even an
attacker who knows this scheme can't guess the string that will close the
block.
