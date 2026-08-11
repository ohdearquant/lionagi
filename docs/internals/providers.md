# Internals Reference — operations, providers, engines

Design rationale, protocol contracts, and measured facts pulled out of
inline comments in `lionagi/operations/`, `lionagi/providers/`, and
`lionagi/engines/`. Inline comments in those packages stay to one sentence;
anything longer lives here. Source pointers back to here read
`# See docs/internals/providers.md#<anchor>`.

## Turn-origin disposition

**`operations/_turn_origin.py`**

A model-submission turn is either genuinely user-originated (a public
ingress called with no upstream instruction) or purely internal (a repair
retry, a ReAct extension round, an interpret pre-pass, ...). Distinguishing
the two lets a single blocking hook point (`USER_PROMPT_SUBMIT`) fire exactly
once per user turn, no matter how many internal calls that turn triggers
underneath it.

Three explicit states, carried as a field on the operation context (never
ambient/task-local, since concurrent branch operations must not leak state
into each other):

- `unset` — the default a genuine outside caller produces. The
  model-submission boundary mints a fresh token and fires.
- `forwarded` — an already-minted token, carried through unchanged. Never
  re-originated; a caller that receives a forwarded disposition must pass it
  on as-is, not re-mint.
- `no-origin` — the call traverses without ever holding a token. The
  boundary stays silent.

## Run lifecycle signal ordering

**`operations/run/run.py`**, **`operations/chat/chat.py`**

`consume_turn_origin()` is consumed exactly once, as the first awaited
operation for a turn — before context providers run, before `RunStart`/the
chat equivalent is emitted, before anything is committed or yielded. A
handler that rejects the prompt must leave no lifecycle trace beyond the
rejection itself: no context-provider side effects, no `RunStart`, nothing
committed to `branch.messages`, nothing yielded to a consumer. The rejection
is still recorded as the run's failure (not silently dropped) so the
terminal signal reports it correctly.

`run()` emits at most one terminal signal per call when an observer is
attached: `RunEnd` on clean exit or consumer abandonment, `RunFailed` on any
failure. `_terminal_emitted` guards double emission on Python <3.11, where
`finally` also runs after `GeneratorExit`. `suppress_lifecycle_var`
suppresses nested signals inside `Branch.ReAct()` turns, since each ReAct
round is an internal continuation of the same call, not a fresh user turn.

## API post call contract

**`operations/_api_hooks.py`**

`emit_api_post_call()` fires once the call has settled — success,
provider-reported failure (`api_call.status`), or a raised exception
(`error`). Every `API_PRE_CALL` this adapter's caller emits is paired with
exactly one `API_POST_CALL` carrying whatever is actually known about how
the call ended:

- `status`: `"error"` when an exception was raised (`error` is set),
  otherwise `api_call.status` mapped onto the closed status vocabulary
  (`"completed"`/`"failed"`/... — anything else becomes `"unknown"`, never a
  raw provider string).
- `error`: populated whenever *either* an exception was raised *or* the call
  settled with a provider-reported failure and nothing was raised
  (`api_call.execution.error`) — a FAILED `APICalling` that never raises
  must not leave this field null just because raising wasn't how it failed.
  Always reduced to a class-name-only summary, never the raw message.
- `tokens`: typed numeric usage summary (`input_tokens`/`output_tokens`
  ints); `None` when the shape is unrecognized or the call never produced
  one. Never the raw provider usage mapping.

## Run stream cleanup cascade

**`operations/run/run.py`**

`_stream_with_deadline()` and `_stream_with_liveness()` explicitly close the
underlying provider stream on every exit path (normal completion, an
`"error"` chunk raise, a `_StopStream` control signal, `GeneratorExit`,
cancellation) instead of leaving it to async-generator GC. For a CLI
provider, an explicit close cascades down to the subprocess reader's own
`finally` and terminates the process group; left to GC finalization, an
abandoned generator can leave the CLI subprocess running to completion,
orphaned, after the caller already gave up.

The close chain (`ndjson_from_cli -> aterminate_process_group ->
asyncio.wait_for`) can raise `asyncio.CancelledError`, a `BaseException` a
plain `except Exception` will not catch. Left unguarded it would escape the
enclosing `finally` and replace whatever provider/control exception was
already propagating. Every cleanup site in `run.py` therefore checks
`sys.exc_info()[1] is not None` before deciding whether a close failure is
the primary error or a secondary one to log and swallow.

## Run worker liveness watchdog

**`operations/run/run.py`**

A worker whose subprocess dies at/near spawn (or otherwise produces
nothing) leaves an operation awaiting a stream chunk that never arrives —
the leg stays "running" forever and every dependent operation in a flow
deadlocks behind it. `_stream_with_liveness()` guards the *first* chunk
only: once any chunk has arrived, the subprocess is alive and the rest of
the stream is governed solely by `stream_deadline`.

On a first-output miss, the subprocess is retried once with an identical
invocation. A second miss raises `WorkerLivenessError` so the operation
transitions to FAILED and releases its dependents, instead of hanging as a
zombie "running" leg.

`liveness_timeout` of `None`/`<=0` disables the watchdog entirely
(deterministic/test runs). When the caller's own `stream_deadline` is
tighter than `liveness_timeout`, the deadline wins and its `TimeoutError` is
propagated unchanged — not treated as a liveness miss, not retried — since
the caller asked for that total-stream budget deliberately. The default
liveness timeout only applies to endpoints declaring
`streams_first_output_early`; a buffered transport (e.g. `gemini_code`,
whose first chunk arrives only once the whole result is in) would otherwise
have a healthy long call misdiagnosed as a dead worker.

## Review engine partial export on deadline

**`engines/review.py`**

`ReviewEngine._partial_export()` returns an already-computed verdict after
budget/deadline exhaustion instead of discarding it. A synthesis agent's
structured emission is captured onto the session bus via the branch's async
signal-emission side channel independently of whether the `synth.operate()`
call in `_verdict` itself ever returns — so a `ReviewVerdict` can already
exist in `run.by_type(ReviewVerdict)` even though the deadline watchdog
cancelled `_run_task` before `_verdict` reached its `return` statement (e.g.
a CLI-backed worker still retrying its emission). The base
`Engine._partial_export` no-op would silently drop that verdict; this
surfaces it, flagged via the normal `EngineResult` degrade signal.

## Flow-stream driver task

**`operations/flow.py`**

`flow_stream()` needs a detached task for its driver coroutine so the
generator can yield events as they arrive. `anyio.create_task_group` cannot
be used for this because the generator must outlive any single task-group
scope — yielding across a task group's `async with` is unsafe on Trio once
the consumer can close the generator early. asyncio has no
structured-concurrency requirement, so a plain task suffices there; Trio
requires a system task (`trio.lowlevel.spawn_system_task`), which is immune
to any enclosing cancel scope and is stopped via `driver_cancel_scope`
instead.

## Codex config profile resolution and effort clamping

**`providers/openai/_codex_profile.py`**, **`providers/openai/codex.py`**

codex reaches a model from another provider through a config profile: `-p
<name>` layers `$CODEX_HOME/<name>.config.toml` over the base config, and
that file names a `model` and the `model_provider` that serves it. So
`codex/<name>` in a lionagi model string should mean "run that profile," not
"run a model literally called `<name>`." lionagi can't forward this with
`-p` — codex accepts exactly one profile per invocation, and lionagi already
spends that slot on a generated profile carrying MCP server secrets. Instead
`resolve_codex_config_profile()` reads the file directly: the profile's
`model` becomes the request's model, and its remaining scalar keys become
`-c` overrides (table-valued keys, notably `mcp_servers`, are dropped and
logged — lionagi decides a leg's MCP servers itself, and silently
re-introducing servers from a config file would go around that).

Two limits keep this from misfiring on an ordinary model id. Only a bare
name is looked up (no dots, no separators), so a vendor id like
`deepseek/deepseek-v4-flash-0731` is never treated as a profile path — a
real model id carries dots or a slash, which the bare-name check rejects.
And a symlinked profile file whose target can't be read raises loudly rather
than silently falling through to "no profile" — `Path.is_file()` follows a
broken symlink and returns False either way, and the whole point of this
code is to stop a profile name from silently reaching codex as a literal
model id.

This resolution runs once, at the request-model level (`_resolve_config_profile`),
so both the CLI and library entry points reach it — for a while only the CLI
path resolved profiles, and a request built as
`Branch(chat_model="codex/deepseek-flash")` sent the profile name to codex as
a model id, which codex rejected as unsupported. It has to run *before*
effort clamping (`_resolve_profile_then_clamp_effort`), since the clamp's
ceilings are keyed on the model id and a profile names a different model
than the caller did. Both steps are folded into one `mode="before"` model
validator, deliberately not left as two separate validators relying on
pydantic's ordering — pydantic runs `mode="before"` validators in *reverse*
definition order, so getting this wrong silently applies one model's effort
ceiling to a different model's profile. Caller-supplied `config_overrides`
still win over the profile's, since an override at the call site is an
explicit instruction and the profile is only a default sitting in a file.

## Codex c override TOML serialization

**`providers/openai/codex.py`**

codex's `-c key=value` parses `value` as TOML, falling back to a raw string
literal only when TOML parsing fails (see `codex exec --help`). A
JSON-style dump of a dict/list is not valid TOML (`:` instead of `=`,
different unquoted-key rules) — it either mis-parses into the fallback
literal string (breaking any override whose target field expects a table,
e.g. `mcp_servers.<name>.env`) or, worse, coincidentally parses into a
different-than-intended TOML value. Every override value is therefore
serialized as syntactically valid TOML (`toml_override_value()`) instead of
JSON.

## CLI adapter error-chunk conformance

**`providers/anthropic/claude_code.py`, `providers/google/gemini_code.py`,
`providers/openai/codex.py`, `providers/pi/cli.py`**

Four CLI adapters each decide, independently, what a stream consumer sees
when a session ends in failure. Nothing compares the four against each
other, so a new adapter, or a refactor of an existing one, can reopen a gap
in silence.

The contract, per adapter:

1. a session finishing with `is_error` set yields exactly one chunk of type
   `error`;
2. that chunk carries `is_error`;
3. a session finishing without `is_error` yields zero `error` chunks.

"Exactly one" (not "at least one") is deliberate: it is the only phrasing
that can express both "this failure was reported" and "this failure was not
reported twice." Assertion 3 is the other direction — an adapter that
reports errors on healthy sessions would otherwise pass.

Where the error chunk gets built differs by adapter, and that difference is
what makes the guard against double-reporting reachable or not:

- `claude_code` builds it only in the endpoint, behind an
  already-reported guard. Its parser never builds one, so on any real event
  sequence today that guard cannot fire — it is pinned intent, not live
  cover.
- `gemini_code` builds it in the parser on the failing path; the endpoint
  guard is what stops a second one. This is the one adapter where "exactly
  one" is non-vacuous today.
- `codex` used to build one in both the parser and the endpoint with no
  guard between them, so a real `turn.failed` event was reported twice.
  That defect is why "at least one" would have been the wrong contract to
  test — it passed on codex while the bug was live. A guard was added and
  codex now satisfies all three assertions.
- `pi` builds none. A failed pi session instead yields a chunk of type
  `result` whose content is the error text — the failure survives, wearing
  the type that means success. A consumer keying on chunk type sees a clean
  result; one reading content sees an error string; neither can tell it from
  success by the documented contract. This is pi's tracked, open
  divergence.

Codex also yields an `error`-type chunk when a resumed session ends
normally. That is not a violation of assertion 3: the chunk carries
`is_error=False` and `benign_eos=True`, both set deliberately, and codex's
healthy fixtures use `turn.completed` so the two cases never collide. A
consumer keying on chunk type alone still can't distinguish this from a
failure, but the discriminator fields exist for one that reads them.

Not covered by this contract: the non-streaming path (`_call()` drives the
same generator and returns the session as a dict, nothing branches on the
flag), ReAct's final-answer turn (catches broadly and substitutes the last
response), and per-tool error carriers (`tool_result.is_error` is a separate
signal — gemini's wire format has no per-tool events to carry it at all).

Fixtures are labelled `RECORDED` or `AUTHORED`; the label matters. An
authored event dict is written from what the parser reads, so it agrees
with the parser by construction and inherits its blind spots — a fixture
built this way cannot reveal a CLI that signals failure through a channel
nobody reads, and would pass cleanly forever if it did. Only a recorded
transcript is evidence about the real CLI; an authored one is evidence
about the model of it encoded in the parser.

## Codex turn-completed usage delta

**`providers/openai/codex.py`**

`turn.completed` reports usage/cost as a running total-to-date, not a
per-turn delta. `run.py` stamps each `"result"` chunk's metadata onto
whichever `AssistantResponse` it next flushes, and branch usage collection
sums that metadata across every message on the branch — if a tool call
flushes a message between two `turn.completed` events, each cumulative
snapshot would land on a different message and earlier turns would get
counted again. `stream_codex_cli_events()` tracks the last-seen cumulative
values and emits only the marginal (this-turn-only) delta per event,
clamped at 0 in case a provider quirk ever reports a lower running total, so
summing across every flushed message reconstructs the true total exactly
once. `num_turns` is the exception: each `turn.completed` occurrence is
already a per-event delta (incremented locally, not read off the event), so
it is always safe to emit as `1`.

## A session-terminal error must be turned back into a stream chunk

**`providers/anthropic/claude_code.py`**, **`providers/openai/codex.py`**

Both CLI providers drive their subprocess through an internal generator that
yields ordinary `StreamChunk`s interleaved with, at the end, a `CLISession`
object carrying the run's terminal verdict (`is_error`, usage, cost, turns,
duration). `CLISession` is never itself yielded to the caller of `stream()`
— only the chunks are — so a failure recorded solely on that object reaches
nobody: the result chunk emitted beside it carries usage and cost but never
the error flag, and a run that ended in error looks exactly like one that
succeeded to anything consuming the stream. `stream()` therefore checks
`item.is_error` when it sees the `CLISession` and synthesizes one final
`StreamChunk(type="error", is_error=True, ...)` if no chunk already reported
an error — the guard exists because a chunk-level error should never be
double-reported, not because one currently occurs; today nothing else in
either module constructs one, so the guard is presently vacuous but is kept
as the natural place a future real error chunk would need it.

The fix is scoped to `stream()` only. `_call()` drives the same generator
and returns the session as a dict without branching on `is_error`, so the
one-shot (non-streaming) helpers built on `_call()` still return an ordinary
answer for a failed session — a separate gap, not closed by this. Per-tool
failures already have their own carriers and are outside this concern
entirely; this is about the session-terminal verdict only.

## CLI subprocess lifecycle

**`providers/_cli_subprocess.py`**

Every CLI-backed provider (codex, claude_code, gemini, pi, ...) spawns
through `ndjson_from_cli()`. Most of the module exists to answer one
question honestly: is the child, and everything it spawned, actually gone
when the caller is done with it?

A pid and its process-group id are both recyclable — once reaped, the
kernel is free to hand them to an unrelated process. `SpawnedProcess` (pid,
pgid, create_time) is what makes a later signal target provably still the
same child: `observe_spawned()` reads the group id bracketed by two
start-time reads, before and after, and reports no identity
(`create_time=None`) if the process was replaced mid-read or was already
reaped or zombied. A consumer that gets `create_time=None` must treat it as
"no identity captured," not as a statement about the child. `on_spawn`,
when given, fires with this observation once the child exists and is
awaited before any output is consumed, so a caller with durable accounting
has recorded the process before anything can happen to it; its exceptions
are not swallowed, since a failed recording leaves a live process nothing
is tracking.

Group cleanup only signals when it can prove the recorded group id is
still this child's: either the leader is unreaped (its pid can't have been
recycled yet, so no scan is needed), or, after reaping, a live member is
still found occupying that group id. A process-table read that comes back
incomplete and empty is *not* treated as an empty group — it is logged as
`"unproven"` and left alone, because an unprovable group and a reissued one
look identical from here. `end_child_group()` awaits a graceful terminate
bounded by `grace`, then does a synchronous group sweep in a `finally` so a
second cancellation can't interpose; the graceful path only runs on the
not-yet-waited branch, since calling it after a drain has already happened
would signal whatever now holds a possibly-recycled group id. A process
group is also not a containment boundary — a descendant that calls
`setsid()` leaves it, and cleanup then says nothing about that descendant.

One hole is stated rather than closed: if the coroutine awaiting subprocess
creation is cancelled after the OS has created the child but before the
pid comes back, nothing in this process can reach it — asyncio closes the
direct child's transport but never touches its group. This was measured,
not reasoned about: a leg spawned under a loop that shuts down mid-creation
leaves a SIGTERM-ignoring descendant running. Closing the window needs the
pid before the creation call returns, which means reimplementing
`create_subprocess_exec` on stdlib internals outside their public surface —
declined as too fragile across Python versions, so the loss is logged as a
warning instead.

Streaming has its own hazard. `stdin_data` feeds a large prompt through a
pipe (avoiding the OS argv length limit) via a task that runs concurrently
with the stdout/stderr readers — writing it sequentially would deadlock
once the data exceeds the pipe buffer and the child blocks writing output
nobody is draining yet. stdout EOF requires *every* holder of the pipe's
write end to close it, not just the spawned child: a CLI child that spawns
its own long-lived helpers (MCP servers, daemons) leaks the write end into
them, and when the child dies but its orphans keep the pipe open, a plain
`read()` waits forever — a finished leg reads as "running" for the rest of
the caller's budget. `ndjson_from_cli` races each read against the child's
exit, then bounds the post-exit drain to 10s (`_POST_EXIT_DRAIN_GRACE`)
once the child has exited; after that grace, teardown ends the process
group, which is what actually closes the orphans' copy of the pipe. Stderr
is drained concurrently and capped at 256 KiB; on a nonzero exit,
`_no_stderr_reason()` distinguishes "the child wrote nothing" from "the
drain itself failed" from "stderr was never opened," since a caller acts on
those differently and collapsing them makes a broken capture read exactly
like a quiet subprocess.

Every CLI provider's request model wraps its `env` and `on_spawn` fields in
`Redacted` at the top of every model-level `mode="before"` validator
(`redact_runtime_fields_in_place`), so a rejected request never carries a
child environment or a bound callback into `str(err)`, `err.errors()`, or
`err.json()`. This matters because pydantic keeps a failing validator's raw
input on the error — `repr=False`/`exclude` on a field don't help, since a
model-level validator holds the whole raw mapping regardless of per-field
settings. `Redacted` deliberately isn't a mapping, so serialization sees no
structure to walk, and it refuses (`TypeError`, not `ValueError` — pydantic
quotes a `ValueError` verbatim into the validation error) when the raw
input is immutable and can't be substituted in place.

## Declared secret lookup

**`providers/_secret_resolution.py`**

A CLI provider authenticates by reading its own process environment (an
`env_key`), so when a secret actually lives somewhere else — a keychain, a
password manager, a vault agent — the spawning process has nothing to pass
on, and the child dies with a missing-variable error that says nothing
about where the value should have come from. `secrets.lookup` in
`~/.lionagi/settings.yaml` (the **global** file only, never the
project-local one — where a secret lives is a property of the machine, and
a checked-out repo has no business naming the program that reads this
machine's secrets) names a fixed command and the variable names it may be
asked to resolve:

```yaml
secrets:
  lookup:
    argv: [security, find-generic-password, -s, "{name}", -a, lionagi, -w]
    names: [OPENROUTER_API_KEY]
```

`resolve_secret_lookup_config()` validates the block atomically — one bad
name rejects the whole lookup rather than being dropped from it, since a
config that silently resolves fewer names than it declares is
indistinguishable from a working one until something using the dropped
name fails. `fill_declared_secrets()` (called from `ndjson_from_cli` on
every CLI spawn) only looks up names still missing from the environment —
an already-set variable is never overwritten, so exporting one remains the
way to override the store for a single run — and a refused or misconfigured
lookup returns the environment unchanged, exactly like an absent one, but
logs the distinction first: without that, a child dying on a missing
variable gives no hint that the operator's own lookup config is what's
broken.

The resolved value reaches the child only through its environment: never a
file, an argv, or a log line. A lookup command's stdout can carry the
secret itself, so `_run_lookup()` only ever logs the program name, the
variable name, and the exit status — never the command's own output, on
success or failure. Lookups run on the spawn path with a 15s timeout; a
keychain that wants to prompt interactively will exceed it by design — the
prompt gets answered once, out of band, with the variable exported, rather
than blocking every spawn on a UI it can't display.

## Runtime-only endpoint state kept out of serialization

**`providers/_agentic_handlers.py`**

An `Endpoint`'s `EndpointConfig.kwargs` is both a supported way to pass
provider-specific options through and the thing `to_dict()` serializes —
which puts it on the path to `iModel.to_dict()`, `Branch.to_dict()`, and
the run snapshots written to disk. For CLI providers, two of those kwargs
are not safe to write down: a child `env` (credentials) and an `on_spawn`
callback (which closes over whatever supervisor object created it).
`AgenticHandlersMixin` moves anything named in `_runtime_state_fields` out
of `config.kwargs` into an in-memory-only `_runtime_state` dict, at every
point the config could otherwise be read or written down: construction,
and again before any serialization, since `EndpointConfig.update()` and
`iModel.from_dict()` can both reintroduce the values into `kwargs` after
construction.

Identity matters more than value here. `Endpoint.__init__` deep-copies a
supplied `EndpointConfig`, and deep-copying a bound method copies its
receiver too — so a naive copy silently hands a callback to a *copy* of the
supervisor, which then never hears from the real one, while every wiring
check still passes. The runtime fields are read off the caller's own
config *before* that copy happens, specifically to preserve identity, and
copied only shallowly afterwards, since these are live objects and a deep
copy would hand the copy a different object under the same name. A caller
that hands over an already-built `Endpoint` instance instead of a config
missed that window entirely, so `adopt_runtime_state()` writes the values
directly onto the supplied instance rather than dropping them.

`create_payload()` rebuilds the request model from `to_dict(request)`,
which is a `model_dump()` and so omits every field declared
`exclude=True`. Runtime-state fields are typically `exclude=True` on the
request model, so the rebuild would otherwise silently revert them to
defaults; `_carried_runtime_state()` re-attaches them from the original
request object, at lower precedence than anything already present from an
explicit kwarg or the endpoint config.
