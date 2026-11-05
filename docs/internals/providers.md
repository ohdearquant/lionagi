# Internals Reference — operations, providers, engines

Design rationale, protocol contracts, and measured facts pulled out of
inline comments in `lionagi/operations/`, `lionhub/src/lionhub/providers/`, and
`lionhub/src/lionhub/orchestration/engines/`. Inline comments in those packages stay to one sentence;
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
  one. Never the raw provider usage mapping. A count that a routed account
  masked, because its text held a run of the key, is text by then and reads
  as 0; with neither count left, the summary has no tokens. The account's
  ledger prices the provider's own count.

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
deadlocks behind it. `_stream_with_liveness()` guards both the *first* chunk
and the gap between later chunks. The idle window resets after every chunk.

On a first-output miss, the subprocess is retried once with an identical
invocation. A second miss raises `WorkerLivenessError` so the operation
transitions to FAILED and releases its dependents, instead of hanging as a
zombie "running" leg.

Once any chunk has escaped the stream, a watchdog miss is not retried: the
consumer may already have acted on that partial output. Instead it raises
`WorkerLivenessError` with reason `worker.stream_idle`. The first-output and
idle windows are configured independently by `liveness_timeout` and
`idle_timeout`; non-positive values disable their respective window.

When the caller's own `stream_deadline` is tighter than either watchdog
window, the deadline wins and its `TimeoutError` is propagated unchanged —
not treated as a liveness miss, not retried — since the caller asked for that
total-stream budget deliberately. The default watchdogs only apply to endpoints declaring
`streams_first_output_early`; a buffered transport (e.g. `gemini_code`,
whose first chunk arrives only once the whole result is in) would otherwise
have a healthy long call misdiagnosed as a dead worker. Explicit per-run
values remain opt-in overrides for any endpoint.

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
re-introducing servers from a config file would go around that). A quoted
scalar key such as `"mcp_servers.x.command"` is dropped as well: TOML reads it
as one key, but codex splits a `-c` override on its dots and would declare
server `x`. The same holds for any key with an `mcp_servers` segment.

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

A request that also ignores the user config (`ignore_user_config`) takes only
the profile's model settings: `model` and every key whose first segment starts
with `model_`, which includes `model_provider` and the `model_providers.*`
routing. A profile carrying any other key, such as `sandbox_mode`,
`approval_policy` or `shell_environment_policy`, is refused
(`refuse_codex_profile_run_settings`), since those set the sandbox, approvals and
environment the run decides for itself. The CLI's spec builder applies the same
rule to a planned worker, and a run whose spawns can clone a codex orchestrator
applies it to the orchestrator's profile before the run starts.

Both reads use the codex home the *child* will use. A request whose `env`
replaces the child environment names its own `CODEX_HOME` (or `HOME`), and the
profile and config read are the ones that environment names. A `CODEX_HOME`,
or a `HOME` when `CODEX_HOME` is unset, that is not an absolute path is refused
before either read, whether it comes from the request's environment or this
process's; an environment naming neither falls back to the OS account's home.

When the caller sets no effort, the request emits the global config's
`model_reasoning_effort` clamped to the resolved model
(`codex_unset_effort_default`). Otherwise codex would apply the global value
unclamped, and a value above the model's ceiling is rejected by the API. A
profile that pins `model_reasoning_effort` keeps its pin, and a model the
ceiling tables don't know gets nothing emitted.

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

The non-streaming path (`_call()`) drives the same generator and then raises
a classified provider error when the session ended with `is_error`. The call
is therefore recorded as `FAILED` with no response, and `Branch.chat` raises
`ExecutionError` with that provider error as its cause, matching an API
provider's failed call. The error is classified on every failure text the
CLI reported, in order: Claude Code's result event (its `errors` ahead of its
`result`), and each error event for codex, gemini and pi, codex's result event
included when it reports a failure. An empty-payload placeholder, a bare status
or a reason counts only when no event named the failure, and assistant text the
turn produced is never read. The endpoint keeps the failed call's provider
session id, as it does for a returned or streamed call. Not covered by this contract: ReAct's final-answer
turn (catches broadly and substitutes the last response), and per-tool error
carriers (`tool_result.is_error` is a separate signal — gemini's wire format
has no per-tool events to carry it at all).

Fixtures are labelled `RECORDED` or `AUTHORED`; the label matters. An
authored event dict is written from what the parser reads, so it agrees
with the parser by construction and inherits its blind spots — a fixture
built this way cannot reveal a CLI that signals failure through a channel
nobody reads, and would pass cleanly forever if it did. Only a recorded
transcript is evidence about the real CLI; an authored one is evidence
about the model of it encoded in the parser.

## Codex call markup in a turn's final message

**`providers/openai/codex.py`**

A model served through the CLI can write a tool call into its reply as text,
in the model's own call markup, instead of making the call. The CLI accepts
that text as the turn's final message and still ends the turn with
`turn.completed`, so the run would otherwise be recorded as completed although
the agent stopped mid-task and the call never ran. When a turn ends — or the
stream ends without a turn end — and the turn's last text with visible content
carries call markup outside code, the parser marks the session failed
(`session.is_error`, with a `session.result` saying the model wrote a call as
text and the run stopped before it ran) and emits an error `StreamChunk` whose
metadata carries `call_markup_in_text: True`. When the turn ends with
`turn.completed`, the usage `result` chunk for that turn is still emitted
beside it; the stream-end path has no turn to report usage for. A streamed run
raises on that error chunk; a non-streamed call raises the classified provider
error for the failed session, as it does for any failed turn.

Only the turn's last text is read: markup in an earlier message was superseded
by a later one, a completed call after the message clears it, and a message
with no visible text leaves the text it followed in place. Text inside code
is a quotation of the markup rather than a call, so a call the model writes
inside code completes the turn and never runs.

The check parses the text as CommonMark with markdown-it-py
(`MarkdownIt("commonmark")`, a core dependency for this reason) and looks for
markup only in what a reader sees as prose: the text of paragraphs and
headings, link and image text, and raw HTML. Code spans, fenced code blocks
and indented code blocks are left out wherever quotes and lists put them, a
fence that nothing closes included: it runs to the end of its container, as
CommonMark reads it, and its information string is not shown. Link
destinations and titles and link reference definitions are not shown as text
either, so they are not read. Where a fence inside a list item closes or
ends, which list markers interrupt a paragraph, setext underlines, tab stops
and lazy continuation lines are all the parser's reading, not a second one
kept beside it. Each block is matched on
its own, and a code span separates the prose on either side of it, so no tag
is pieced together across a block boundary or a span. The parser normalises
line ends, so CRLF text reads like LF text. A legacy
`result`/`response`/`session.end` event's own text is read like a message:
markup in it fails the turn at the stream end, and text that is not a string
is not read. A tag in plain prose fails the reply as well; a reply that needs
to name one writes it between backticks. An HTML entity that spells the tag
names one too, since it reads as the tag on screen, but only where the
reference ends with a semicolon and names a real entity: a bare `&lt` is
literal text. The markup itself is the tuple `_TOOL_CALL_MARKUP_PATTERNS`, one
compiled pattern per call markup the CLI can surface; another model's markup
is supported by adding one entry there. The patterns match markup tags only —
never the word DSML or a bare `|` in ordinary prose.

One choice goes past CommonMark and errs toward failing a turn that was fine:
raw HTML is read as prose, the text of a `<pre>` or `<code>` element included.
Two err the other way, toward completing the turn as it completed before this
check existed. Text at the parser's nesting limit is not read: the limit is
20 levels, and a list and its item count as one each, so text inside 20 nested
quotes or 10 nested lists is dropped. And a message longer than 16,000
characters (`_MARKUP_CHECK_MAX_CHARS`) is not read at all. That bounds the
parse: the slowest text found for the parser is a paragraph of unclosed inline
HTML openers (`<?`, `<!--`, `<![CDATA[`), whose time grows with the square of
its length, and at that length it takes about half a second. A legacy
`result` event's text is read as written, before any whitespace is trimmed,
as a message's text is.

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

`_call()` drives the same generator and, after the session is complete,
raises `classify_provider_error(..., status=session.error_status)` when
`session.is_error` is set, on Claude Code's `session.result` (the result
event's `errors` when it lists any) and on every error chunk codex, gemini and
pi reported: for codex the stream end fills an empty `session.result` with the
turn's assistant text, which is not the failure, and a codex result event that
reports a failure with text yields an error chunk of its own. The one-shot (non-streaming) helpers built on
`_call()` therefore fail the same way an API provider does instead of
returning the error text as an answer. Per-tool failures already have their
own carriers and are outside this concern entirely; this is about the
session-terminal verdict only.

## CLI subprocess lifecycle

**`providers/_cli_subprocess.py`**

Every CLI-backed provider (codex, claude_code, gemini, pi, ...) spawns
through `ndjson_from_cli()`. Most of the module exists to answer one
question honestly: is the child, and everything it spawned, actually gone
when the caller is done with it?

A pid and its process-group id are both recyclable — once reaped, the kernel
is free to hand them to an unrelated process. Before spawn, the shared process
helper adds a fresh unguessable marker to the child's private environment; the
marker value is included in diagnostic redaction. `observe_spawned()` accepts a
start time only when that marker and the expected leader group are found inside
one start-time bracket. This covers both reassignment during the read and the
earlier case where a short-lived child was already reaped and its pid reused
before the first read. An unreadable environment, marker mismatch, changed
start time, reaped process, or zombie reports no identity
(`create_time=None`) and therefore grants no group-signal authority. `on_spawn`,
when given, fires with this observation once the child exists and is awaited
before any output is consumed; its exceptions are not swallowed, since failed
accounting must tear down the child.

An engine run installs a separate context-local observer while creating its
run task. That observer receives the live process handle and the same verified
spawn identity, so `EngineRun.cancel_active()` can reap a child even when the
worker task swallows cancellation and never unwinds this generator. Context
propagation associates nested CLI spawns with the owning run without adding a
runtime field to every provider request model; resetting the binding immediately
after task creation prevents concurrent runs from sharing ownership. The public
`on_spawn(SpawnedProcess)` contract remains identity-only.

Durable worker accounting uses a separate context-local recorder bound by the
persisted producer. The shared provider admits an attempt before creating its
spawn task and retains that task, registration, and late cleanup under the owner.
After observing the child, registration writes the stable attempt ID and its
exact session/generation association before the engine observer, public
`on_spawn` callback, or pipe I/O can expose output. Repeating the same attempt
keeps its ID and immutable fields. A registration refusal remains a typed provider
failure and enters the existing child-cleanup path.

The provider supplies only the six observed identity facts; the StateDB adapter
adds the association and diagnostic fields. Neither environment values nor the
private process-group marker are stored. Providers import the recorder protocol,
not StateDB. Closing the owner stops further admission and waits for retained
spawn/registration/cleanup work before the producer can terminalize or close its
database. These records do not themselves establish process-group absence.

Group cleanup signals only through that positively marker-bound observation.
The shielded spawn task captures `(pid, pgid, create_time)` before publishing
its result, including to the abandoned-spawn callback. At teardown, that spawn
identity anchors a scan which freezes `(pid, create_time)` member witnesses;
one of those exact members must remain live in the same group immediately
before any delayed signal. `returncode is None` is not ownership:
Python's child watcher may already have reaped the pid before the event loop
publishes its terminal return code. A *fresh* post-reap membership scan is not
proof either — an unrelated group may now occupy the same number — so teardown
reports `"unproven"` and refuses the group-wide signal. `end_child_group()`
awaits that witness-gated graceful terminate bounded by `grace`; its
synchronous `finally` backstop uses the same frozen witnesses, so a second
cancellation cannot turn a stale number into authority. A process group is
also not a containment boundary — a descendant that calls `setsid()` leaves
it, and cleanup then says nothing about that descendant.

The retained spawn task keeps ownership across caller cancellation while
creation and registration finish. An attempted spawn that returns no process
handle is an unconfirmed launch, even when it raises. The spawn path preserves
the original exception or cancellation and adds a safe `launch outcome unconfirmed`
diagnostic naming the admission pools still held. Their locks remain with the
spawning process until it exits; no exception-type or traceback heuristic turns
a missing handle into evidence that no child exists. The same retention applies
when group cleanup is unconfirmed, even if the direct child has exited.

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
`err.json()`. The Codex request model wraps its `config_overrides` the same
way, through `_redact_codex_runtime_fields_in_place`, because a `-c` override
holds the same kind of token a child environment does; its field validator
then refuses a key that is not a string by position and type, as the `env`
validator does, since pydantic would quote such a key. This matters because
pydantic keeps a failing validator's raw input on the error —
`repr=False`/`exclude` on a field don't help, since a
model-level validator holds the whole raw mapping regardless of per-field
settings. `Redacted` deliberately isn't a mapping, so serialization sees no
structure to walk, and it refuses (`TypeError`, not `ValueError` — pydantic
quotes a `ValueError` verbatim into the validation error) when the raw
input is immutable and can't be substituted in place.

## CLI agent admission

**`providers/_subagent_admission.py`** (design: ADR-0133)

`ndjson_from_cli()` first refuses to run inside an agent lionagi started
(`refuse_nested_agent()`, below), then takes a lease from `admit_subagent()`
before it starts the child, so the cap covers every CLI worker whatever started
it. A lease is one slot in each capped pool the worker counts against (its
engine, `heavy` for a heavy worker, and `all`). A slot is
`$LIONAGI_HOME/leases/subagents/<pool>/<index>.lock`, held with
`flock(LOCK_EX | LOCK_NB)` on a descriptor opened `O_CLOEXEC`: the lock belongs
to the lionagi process, never to the child, so a grandchild that daemonizes
cannot keep it, and the kernel drops it when that process exits by any path. The
record written into the slot names the holder pid, engine, workload, start and
the cap the holder read, and after a successful spawn the child's pid and create
time.

Five rules keep the count honest across processes that do not share memory:

- Counting and taking happen under each pool's admission lock
  (`<pool>/.admission`). An admission takes the admission locks of all its pools,
  in pool-name order, before it counts any, and takes a slot in each only once
  every pool has room, so it is admitted to every pool or to none and never
  reads another admission's half-taken slots. The status read counts under the
  same lock and writes nothing.
- A free lock whose recorded child is still the recorded process (pid plus start
  time, through `lionagi.ln._proc`) counts as held. The holder can die while its
  child, which leads its own session, keeps running; an unreadable process table
  counts as running, so a slot is never handed out on a probe that could not look.
- A pool is full at the smallest cap among the admission's own and those its
  holders recorded, so a process reading a larger cap cannot run past a holder
  that read a smaller one; a raised cap admits past those holders as they release.
- A locked slot with no record is a holder between clearing its record and
  unlocking (`release()` truncates, then unlocks). It counts as held, and a pool
  full only because of one is re-read within milliseconds, up to 25 times, before
  any wait, so an admission at wait 0 is not refused for a slot freeing that
  instant. A slot counted free and found locked when taken is treated the same.
- A cap or wait that does not parse, or a pool directory that cannot be created
  or written, refuses with `SubagentAdmissionRefusedError` rather than letting
  the worker start uncounted.

A full pool is re-tried every two seconds, jittered by ±25% so two processes
that collide on the last slot do not retry in step, until
`LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS` runs out. The refusal is one line naming
the pool, its cap, the variable that set it and the holder pids.

Before an OS spawn attempt, the executable is resolved using the child's
environment `PATH` and working directory, with `os.defpath` when `PATH` is absent.
An absolute or relative executable path must exist and permit execution. An
unresolvable or non-executable command is a pre-attempt refusal: no subprocess
creation is called and the unused admission capacity is returned. The stdin
payload is also encoded before admission, so encoding failure starts no child.

After an attempt, a missing handle or unconfirmed group cleanup keeps the
spawning process's admission locks until that process exits. This includes a
binary removed between the executable check and the actual spawn: the earlier
check does not prove that the later attempt created no child. The original
exception or cancellation is preserved, with the safe held-pool diagnostic.
These are holder locks, not locks transferred to a child that may already have
exited. In a resident host they continue to consume machine-wide capacity;
refusing common missing-executable cases before the attempt avoids consuming
that capacity for a command known not to be executable.

`iModel.stream()` re-raises every stream failure as a `ValueError` whose
`__cause__` is the provider's error. `li agent` walks that chain: a refusal
anywhere in it prints the refusal itself and exits `75`, and the MCP terminal
cause records the provider error's class rather than `unknown`, the same walk the
run row's reason takes in `cli/_runs.py`.

`subagent_admission_status()` writes no slot: it counts each pool under its
admission lock, as an admission does. `li monitor` shows it as one line under
its table and under `subagents` in machine mode.

### Nested agents

`refuse_nested_agent()` raises `NestedAgentRefusedError` (not retryable, and not
an admission refusal, so `li agent` exits `1` rather than `75`) when this process
inherited `LIONAGI_AGENT_DEPTH` of 1 or more, read once at import, or when an
ancestor started with one, read from each ancestor's start environment through
psutil. The spawn hands every child `LIONAGI_AGENT_DEPTH=child_agent_depth()`,
`1` unless `li agent` launched a profile named in `LIONAGI_SEAT_PROFILES` in this process
(`set_child_agent_depth(0)`), over whatever the environment held. The ancestor
read catches a `li agent` an agent's tool runs with a scrubbed environment, such
as a codex MCP server; it passes over an ancestor whose environment cannot be
read, which on macOS includes platform binaries like `/bin/sh` but not the agent
CLIs. ADR-0133 D7 records the design.

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

A declared name that is the `key_env` of a routed account (`ANTHROPIC_ACCOUNTS`,
ADR-0126 Amendment 2) is not filled: that key is read only by the account
router for the request it signs, so no CLI child receives it from the lookup.
The name still counts as declared for redaction, and a value the child
already inherits under it is left alone.

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
