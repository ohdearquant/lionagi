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
