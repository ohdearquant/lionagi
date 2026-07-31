# Agent runtime internals

Reference material extracted from comments/docstrings in `lionagi/ln/`,
`lionagi/agent/`, `lionagi/session/`, and `lionagi/tools/` during the
comment/docstring density pass. Each heading is pointed to by a
`# See docs/internals/agent-runtime.md#<anchor>` comment at its call site.

## create-agent-branch-origin

`CREATE_AGENT_BRANCH_ORIGIN_KEY` (`lionagi/agent/factory.py`) is stamped into
`branch.metadata` for every `Branch` `create_agent` produces. The key's
presence — not the current invocation's profile — is the durable, immutable
record that a branch's system message was composed via `create_agent` (role
header + policy block) rather than a bare profile body. It round-trips
through `Branch.to_dict()`/`from_dict()` with the rest of `metadata`, so a
later resume/continue-last leg can consult the *persisted* branch itself
instead of re-deriving "was this create_agent-composed?" from whatever
profile happens to be supplied on the resuming invocation (which may differ,
or may have since dropped its `role:` key). See `lionagi/cli/agent.py`
`_run_agent`'s system-prompt reapply guard.

## external-hook-wiring

`_wire_external_hooks` (`lionagi/agent/factory.py`) attaches
`hooks_external` entries (parsed by `apply_hooks_from_settings`) to the seam
their event maps to.

`PreToolUse`/`PostToolUse` attach to `branch.acts` (always present). The
remaining supported events attach to `branch._hooks` (a `HookBus`) — present
only once the branch is owned by a `Session`; a standalone branch built via
`create_agent` has none yet, so those entries are queued on
`branch._pending_hook_bus_entries` instead of dropped, and the queue is kept
for the branch's lifetime rather than cleared on first use.
`Branch.attach_hook_bus` — the only seam that ever assigns `branch._hooks`
(`Session.include_branches` and the lazy `Session.hooks` property) — syncs
unattached entries onto whichever bus is current, so a configured
`UserPromptSubmit`/`SessionStart`/`SessionEnd`/`PostToolUseFailure` hook
attaches once this branch joins a `Session` and re-attaches if it is later
reparented to another one, rather than silently never firing.

## mcp-trust-decision

`ActionManager.load_mcp_config()` no longer implies trust when its
`mcp_security` argument is omitted (an omitted policy falls through to the
wrapper's fail-closed default instead). `_load_mcp` (`lionagi/agent/factory.py`)
reaching the point where it calls `MCPSecurityConfig.trusted()` already
required an explicit trust act: either `spec.mcp_config_path` was set
directly, or `mcp_path` resolved from the operator's own home-level
`.mcp.json` (the same "global config is inherently trusted" precedent as
settings.yaml's always-loaded global file), or the caller opted into
`trust_project_settings=True` for a project-level file. This is the one,
explicit, documented compatibility decision for lionagi's own MCP auto-load
consumer — not a silent default buried in the generic library call.

## mcp-server-forwarding

`apply_forwarded_mcp_servers` (`lionagi/agent/factory.py`) writes a resolved
MCP server set into a CLI request's kwargs. Every spawn path that resolves a
server set applies it here, so "can this leg be given a set?" has one answer
and one implementation.

- `provider_accepts_forwarded_mcp` answers what a provider is *capable* of
  (two transports: the Claude CLI takes the set as a request kwarg, codex
  takes it as `-c mcp_servers.<name>.<field>` overrides) — not whether a
  given spawn actually handed anything over. For that, read
  `request_carries_forwarded_mcp` off the request the spawn produced.
- `exclusive` is the caller stating that the servers dict is the whole set
  rather than an addition to whatever the provider finds for itself. It
  makes an empty set mean "no servers": the Claude CLI needs
  `strict_mcp_config`; codex, which has no wholesale clear, needs each
  server it would otherwise load disabled by name (see
  `_discover_ambient_codex_mcp_server_names`, which raises
  `ConfigurationError` if it can't enumerate ambient servers — an allowlist
  that can't be enforced must fail closed, never silently pass every ambient
  server through).
- `allowed_names` is the caller's allowlist when it is wider than the set
  being forwarded — a name the caller allows but did not itself describe
  stays enabled rather than being disabled as excluded. `known_server_names`
  adds names the caller knows the provider may load which ambient discovery
  would not report. Both only matter under `exclusive`.
- For codex, only fields in `_CODEX_MCP_SERVER_FIELDS` (verified against the
  installed CLI: `codex mcp list --json` echoes back exactly this field set)
  are forwarded; an unsupported field is a caller mistake (loud
  `ConfigurationError`), not a value to silently drop. `env`/`http_headers`
  may carry secrets (API keys, tokens, a static `Authorization: Bearer ...`
  header) and must never land on argv (visible via `ps`, request logs,
  etc.), so they route through `_write_codex_mcp_secret_profile` to a
  private on-disk profile (`$CODEX_HOME/<name>.config.toml`, loaded via
  `-p <name>`) instead of a `-c` override. `env_http_headers` values are
  env-var *names*, not secrets, so those stay on the `-c` path.
- `_forward_mcp_to_cli_request` reaches the per-turn request kwargs a CLI
  provider subprocess actually reads (unlike `_load_mcp`, which only reaches
  lionagi-native `branch.acts` tools, inert for CLI providers). With
  `resolved_servers` given, that set is handed over as-is and no config file
  is looked for — a caller that already resolved a set is saying which
  servers this agent gets, and discovering a second one from the agent's
  working directory would silently replace it.
- A generated codex secret-profile name is a fixed prefix (`lionagi-mcp-`)
  plus 32 hex characters. The name has to carry this because a resumed leg's
  profile was written by a different process — an in-process record of what
  this run generated can't recognise it. A caller-supplied name in that
  exact shape is treated as ours and replaced (a resumed leg re-spawns from
  its persisted request, carrying the profile the first run generated and
  already deleted); anything else is the caller's and refused, never
  overwritten.

## safe-path-construction

`_build_safe_path` (`lionagi/ln/_utils.py`) is shared, symlink-safe path
construction for `create_path`/`acreate_path`. Both the sync and async
constructors call this so they share identical traversal/containment
semantics — fix the check once, here, rather than per-variant.

Containment is always checked against the resolved (symlink-safe)
candidate. The returned `_SafePath` pair carries that resolved candidate —
the only path callers may use for `mkdir`/existence side effects — alongside
the caller-facing spelling to return to the caller: a relative `directory`
yields a relative caller-facing path, an absolute one yields an absolute
caller-facing path. Filesystem side effects must act on `resolved`, never
`caller_facing`, which can still be redirected by a symlink swapped in after
validation but before use.

Validation happens once, at call time — nothing is reserved against later
filesystem changes, so a relative `directory` is only race-safe against
concurrent mutation for as long as the caller resolves it the same way
`create_path`/`acreate_path` did. Callers that need race-safety against a
concurrently mutating relative `directory` should pass an absolute directory
instead.

## dual-lock-ordering

`async_synchronized` (`lionagi/ln/_utils.py`): when the instance also
carries a `self._lock` (threading lock), the wrapper acquires *both* — the
async lock first, then the threading lock via a non-blocking spin — so
async-decorated methods mutually exclude `@synchronized` sync callers
running in other threads. The async lock serializes async callers, so at
most one task ever contends for the threading lock per instance, which keeps
`RLock` thread-ownership semantics intact and lets the decorated body
reenter `@synchronized` methods. Lock order is strictly async-then-sync;
sync holders never await, so the spin is bounded by a sync critical section
and never deadlocks.

## hook-bus-reattachment

`Branch.attach_hook_bus` (`lionagi/session/branch.py`) sets a branch's
`HookBus` and (re)registers any external handlers queued for bus
attachment.

A standalone branch built via `create_agent` has no bus yet, so
`hooks_external` entries bound to bus-only events (`UserPromptSubmit`,
`SessionStart`/`SessionEnd`/`PostToolUseFailure`) cannot attach at config
time; `lionagi.agent.factory._wire_external_hooks` queues them onto
`_pending_hook_bus_entries` instead of dropping them. That list is retained
for the branch's lifetime, not cleared after the first flush, so a branch
moved between sessions (`Session.remove_branch` then
`include_branches`/`new_branch` elsewhere) re-registers the same external
handlers on its new session's bus instead of silently losing them.
Re-attaching the same bus is a no-op for entries already registered on it —
only entries appended since the last sync onto the current bus are flushed.
Every seam that gives a branch a bus (`Session.include_branches` and the
lazy `Session.hooks` property) must route the assignment through this
method so queued handlers actually attach, rather than a configured guard
silently never firing.

Each registered handler is wrapped with an origin-branch filter (see
`Branch._origin_filtered_handler`) so a bus shared by multiple branches
never cross-fires one branch's hook for another branch's event. Switching to
a genuinely different bus — or detaching entirely via
`attach_hook_bus(None)` — first unregisters every wrapper this branch put on
the old bus, so a reparented or removed branch leaves no stale handler
behind.

## non-finite-float-detection

`lionagi/ln/_json_dump.py`: orjson writes `inf`, `-inf` and `nan` as `null`,
which is indistinguishable from a genuine null on read — the value silently
changes and no consumer can detect it. JSON has no representation for these,
so callers that ask for the check (`check_non_finite=True` on `json_dumpb`)
get a loud `ValueError` naming the offending path instead.

Detection (`_locate_non_finite`) walks the object the way orjson does. That
means covering every form orjson encodes natively, because those never
reach `default()`; a walk that follows only `default()` sees nothing inside
a dataclass, an `Enum`, or a numpy array and reports the payload clean. The
forms orjson encodes natively that can carry a float are: `float`, `dict`,
`list`, `tuple` (and subclasses), dataclass instances, `Enum` members
(written by value), and numpy arrays/scalars under `OPT_SERIALIZE_NUMPY`.
The remaining native forms — `str`, `int`, `bool`, `None`, `bytes`,
`datetime`, `date`, `time`, `UUID` — cannot contain a float.

Two forms are outside what any walk can decide:

- `orjson.Fragment` holds pre-serialized bytes that orjson copies into the
  output verbatim, without parsing them and without calling `default()`. A
  `null` inside a Fragment is indistinguishable from one a non-finite float
  would have produced, because neither exists as a Python float by the time
  the Fragment is built. Fragment contents are the caller's to validate; the
  walk skips them.
- A future orjson may encode a container type natively that this list does
  not name (the declared dependency floor is a minimum, not an exact
  version). The list is written against the native types orjson documents;
  it is not enforced against the installed version at run time.

Cost: on a 200-item object with one legitimate null, the traversal costs
roughly 20x the dump it guards; even scanning the output for `null` first
costs about half a dump again, so gating on it doesn't help a payload that
has any null at all (most of them do). This is why `check_non_finite` is off
by default. A null-free serialized result is provably clean, so the walk is
skipped in that case regardless.

`raise_if_non_finite` is for callers persisting through a serializer other
than `json_dumpb` — notably the standard library's `json`, which writes
`inf`/`-inf`/`nan` as the tokens `Infinity`/`NaN`. Python reads those tokens
back, so the writer never notices; every strict parser rejects them, so the
file breaks at whatever boundary reads it next downstream.

## signal-handler-takeover-and-restore

`lionagi/ln/concurrency/utils.py`'s `run_async` takes over `SIGINT`/`SIGTERM`
only when the previous handler can be given back. `signal.getsignal()`
reports `None` for a handler installed outside Python, and
`signal.signal(signum, None)` raises, so restoring one is impossible in that
case — taking it over would leave this runner's handler installed for the
rest of the process. Abstaining per signal costs the caller's own
cancellation wiring for that signal but keeps whatever was already there
working.

On the way out, each restore stands alone: this runs in a `finally`, often
while an exception is already propagating, and a failure restoring one
signal must not strand the others with this runner's handler still
installed. Restore failures are logged rather than raised, since raising
here would replace whatever the caller was actually failing on.

## untrusted-recall-wrapper

`KhiveInjectionProvider` (`lionagi/tools/khive_injection.py`) wraps recalled
content in an `<untrusted-context>` block before injecting it into a
branch's context, because that content originates from prior (possibly
attacker-influenced) tool output and repo content and must not be mistaken
for an instruction.

Two layers keep the wrapper from being escaped: (1) any literal closing-tag
substring inside the recalled text is neutralized so it can't terminate the
block early, and (2) a per-call random nonce on both tags means even an
attacker who knows this scheme can't guess the string that will actually
close the block.
