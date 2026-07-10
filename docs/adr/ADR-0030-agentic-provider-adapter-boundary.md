# ADR-0030: Agentic provider-adapter boundary

- **Status**: Proposed
- **Kind**: Aspirational
- **Area**: service-providers
- **Date**: 2026-07-09
- **Relations**: extends ADR-0027, ADR-0029

## Context

`AgenticEndpoint` represents providers whose work is not a normal LionAGI HTTP request. Codex,
Claude Code, Gemini, and Pi launch local command-line processes; AG2 adapters run agents in process
or call a remote agent. They already share `APICalling`, `StreamChunk`, provider-session storage,
and the `iModel` lifecycle with API endpoints. That common boundary lets operations consume them
without selecting a vendor (`lionagi/service/connections/agentic_endpoint.py`;
`AgenticEndpoint`, and `lionagi/service/imodel.py`; `iModel`).

The implementation has also converged on reusable mechanics. The four subprocess adapters call the
same NDJSON reader, bounded stderr drain, and process-group terminator. Three use the shared
declarative flag builder; Codex, Claude Code, and Gemini use the shared workspace resolver while Pi
uses the same containment library for its file-bearing fields. Codex, Claude Code, Gemini, and Pi
all use the same callback-validation mixin.
The provider modules still correctly own their request models, command grammar, safety flags, event
parsers, defaults, and presentation callbacks (`lionagi/providers/_cli_subprocess.py`;
`ndjson_from_cli`, `lionagi/providers/_agentic_handlers.py`; `AgenticHandlersMixin`).

Six concrete problems remain.

**P1 — `is_cli` names two different facts.** `AgenticEndpoint.is_cli` is `True`, so it is inherited
by subprocess, in-process, remote, and scripted endpoints. `iModel`, `Branch`, and the run operation
read it as “use the agentic streaming path,” not “launches a CLI.” Code that needs actual process
behavior cannot use the same flag honestly (`lionagi/service/imodel.py`; `iModel.is_cli`,
`lionagi/session/branch.py`; `Branch.clone`, and `lionagi/operations/run/run.py`; `run`).

**P2 — The public stream contract ends at different layers.** The low-level Codex, Claude Code,
Gemini, and Pi parsers yield an internal session accumulator after their chunks. Endpoint wrappers
then suppress it, translate it into a result chunk, or use it to synthesize an error. Codex and
Claude Code normally expose no terminal result chunk; Gemini does; Pi exposes the accumulated final
text again as a result, even after text chunks. AG2 adapters synthesize their own result chunks.
Consumers therefore cannot assign one meaning to normal EOF or a result chunk
(`lionagi/service/types/cli_session.py`; `CLISession`, and the adapter `stream()` methods under
`lionagi/providers/`).

**P3 — Provider-declared failure is not normalized.** Gemini emits an error chunk with both
`type="error"` and `is_error=True`. Codex can emit the same chunk type with `is_error=False`,
including its endpoint-level fallback. Claude Code can finish with `CLISession.is_error=True` but
the endpoint suppresses the session without emitting an error chunk. Pi parser errors can be
represented only on an internal `PiChunk` that the endpoint does not map to an error chunk.
Operations currently infer failure from `type="error"`; other consumers may inspect `is_error`.
Both fields must agree (`lionagi/providers/openai/codex.py`,
`lionagi/providers/anthropic/claude_code.py`, `lionagi/providers/google/gemini_code.py`, and
`lionagi/providers/pi/cli.py`).

**P4 — Session observation is mistaken for resumability.** `iModel` injects its stored agentic
session as `resume` for every `AgenticEndpoint`. Claude Code and Gemini have typed `resume` fields;
Pi has no resume field and defaults to `no_session=True`; Codex emits a thread id but its current
`CodexCodeRequest` has no resume field. The shared layer needs a declared capability before it can
safely inject a provider identifier (`lionagi/service/imodel.py`; `iModel.create_api_calling`, and
the provider request models).

**P5 — Subprocess safety is shared but not stated as an adapter contract.** The helper already uses
argument-vector process creation, `start_new_session=True`, a 256 KiB stderr capture, concurrent
stderr drainage, a five-second process-group termination grace, and workspace containment. Those
properties are load-bearing: changing one adapter back to shell-string execution or failing to
close its generator can reintroduce injection, deadlock, or orphan-process failures
(`lionagi/providers/_cli_subprocess.py`; `ndjson_from_cli`, and `lionagi/ln/_proc.py`;
`aterminate_process_group`).

**P6 — Transport cleanup cannot be made identical.** Subprocess adapters terminate and reap a
process group. The AG2 beta adapter owns an `asyncio.Task` and stream subscriptions. Group chat owns
an in-process async iterator. NLIP owns an HTTP client and SSRF validation. A useful common boundary
must normalize observable results without pretending these resource mechanics are interchangeable.

The current and target boundary is:

```text
Branch / operations
        |
        v
      iModel ---------------------> admission + deadline supervisor (ADR-0029)
        |
        v
  AgenticEndpoint ----------------> APICalling ----------------> StreamChunk
        |
        +-- subprocess support ---- argv + NDJSON + process-group teardown
        +-- in-process support ---- task / iterator cancellation
        +-- remote support -------- SSRF-safe client lifetime
        |
        +-- vendor adapter -------- typed request + flags/events/defaults
```

| Concern | Decision |
|---------|----------|
| Capability and routing identity | D1: every adapter declares immutable transport/output capabilities; `is_agentic` becomes the operation-routing fact. |
| Request construction | D2: provider request models are validated before transport and runtime handlers are separated from serializable provider data. |
| Output, error, and session contract | D3: endpoints yield only normalized `StreamChunk` values and return one typed result mapping for supported one-shot calls. |
| Subprocess mechanics | D4: a shared support module owns argv execution, NDJSON framing, bounded stderr, workspace validation, and deterministic group teardown. |
| Non-subprocess lifecycle | D5: in-process and remote adapters implement the same observable contract while retaining transport-specific cancellation and safety. |
| Verification | D6: one capability-driven conformance suite runs against every agentic adapter, with additional process tests for subprocess transports. |

This ADR deliberately does **not** decide:

- Provider-specific request fields, model names, flags, permission modes, event grammars, or UX
  callbacks. They remain vendor-owned because the upstream interfaces change independently.
- Admission ordering, total request deadlines, event terminalization, or retry after stream setup.
  ADR-0029 owns the supervisor and the no-replay-after-first-output boundary.
- Provider catalog keys, aliases, bootstrap, or availability diagnostics. ADR-0028 owns selection.
- A universal agent protocol or remote-agent wire format. NLIP remains one adapter, not the generic
  shape every in-process or subprocess provider must implement.
- Durable provider sessions. This ADR moves confirmed identifiers between requests in one facade;
  persistence and cross-process recovery require a separate state contract.
- Whether thinking content is shown to end users. The boundary transports normalized thinking
  chunks; presentation belongs to the consuming operation.

## Decision

### D1 — Declare agentic capabilities and route operations by `is_agentic`

`AgenticEndpoint` remains the extension boundary. It gains an immutable class-level capability
record and a precise operation-routing marker:

```python
from dataclasses import dataclass
from typing import ClassVar, Literal

AgenticTransport = Literal["subprocess", "in_process", "remote"]

@dataclass(frozen=True, slots=True)
class AgenticCapabilities:
    transport: AgenticTransport
    resumable: bool
    emits_tool_events: bool
    reports_usage: bool

class Endpoint:
    is_agentic: ClassVar[bool] = False

class AgenticEndpoint(Endpoint):
    is_agentic: ClassVar[bool] = True
    capabilities: ClassVar[AgenticCapabilities]
    resume_field: ClassVar[str | None] = None

    # Deprecated compatibility spelling for “uses the agentic stream path”.
    # New code must use is_agentic or capabilities.transport.
    is_cli: ClassVar[bool] = True
```

`AgenticCapabilities` and `AgenticTransport` live in
`lionagi/providers/_agentic/capabilities.py` and are re-exported from the provider support package.
`AgenticEndpoint` remains in `lionagi/service/connections/agentic_endpoint.py`; the generic service
layer knows the vocabulary but not the vendor inventory.

The initial capability inventory is fixed as follows. “One-shot” records whether `_call()` returns
a result mapping; it is method behavior rather than a fifth capability flag because all agentic
operation routing uses `stream()`.

| Endpoint | Transport | Resumable | Tool events | Usage | One-shot |
|----------|-----------|-----------|-------------|-------|----------|
| `CodexCLIEndpoint` | subprocess | no | yes | yes | yes |
| `ClaudeCodeCLIEndpoint` | subprocess | yes, through request field `resume` | yes | yes | yes |
| `GeminiCLIEndpoint` | subprocess | yes, through request field `resume` | no | yes | yes |
| `PiCLIEndpoint` | subprocess | no | yes | yes | yes |
| `AG2BetaEndpoint` | in_process | no | yes | yes | stream-only |
| `AG2GroupChatEndpoint` | in_process | no | yes | no | stream-only |
| `AG2NlipEndpoint` | remote | no | no | no | stream-only |
| `ScriptedEndpoint` | in_process | no | yes | no | yes |

Codex is intentionally `resumable=False` in this contract. Its parser observes a `thread_id`, but
the shipped `CodexCodeRequest` has no typed resume input and filters unknown fields before model
construction. Observation alone is not a usable resume contract. A provider-local change may set
the capability to `True` only when it adds a typed field, command mapping, and round-trip test.
Pi is non-resumable because its request has no resume field and defaults `no_session=True`.

**Exact semantics**

- **Operation selection.** `iModel`, `Branch`, and run/communicate routing read `is_agentic`.
  Transport-specific code reads `capabilities.transport`. No new code branches on `is_cli`.
- **Compatibility.** `is_cli` continues to return `True` for every agentic endpoint during the
  deprecation window, so in-process and remote adapters do not change operation path while call
  sites migrate. It is then removed under the repository deprecation policy; it is never redefined
  as the precise subprocess test.
- **Missing capabilities.** Registering an `AgenticEndpoint` subclass without an
  `AgenticCapabilities` instance is a catalog validation error under ADR-0028. Lookup never returns
  a partially declared adapter.
- **False capability.** An adapter may omit an optional output only when the corresponding field is
  `False`. It must not synthesize tool or usage events merely to satisfy a common shape.
- **True capability.** Fixtures must exercise the declared behavior. A `reports_usage=True` adapter
  places provider-reported usage on its terminal result metadata; an `emits_tool_events=True`
  adapter maps at least one provider fixture to tool-use and tool-result chunks.
- **Copy and restart.** Capabilities are immutable class data. Copying an endpoint does not copy
  active transport resources. Process restart reconstructs the same declaration through catalog
  import.
- **Unsupported one-shot.** Calling `invoke()` on a stream-only endpoint raises
  `AgenticOperationUnsupported(operation="invoke")` before starting a task, process, or connection.

**Why this way.** The present inheritance seam is already the correct extension point; the missing
piece is an honest vocabulary. A transport enum prevents an in-process agent from masquerading as a
CLI while `is_agentic` preserves the behavior operations actually need. The matrix is conservative:
capabilities describe normalized output that an adapter can prove, not everything its upstream
provider might support in another client.

### D2 — Validate typed requests and separate runtime arguments before transport

Every adapter names one Pydantic request type and the runtime-only keys it accepts. The generic
payload envelope stays compatible with `APICalling`:

```python
from collections.abc import AsyncIterator, Callable
from typing import Any, ClassVar, TypedDict

class AgenticPayload(TypedDict):
    request: BaseModel

class AgenticEndpoint(Endpoint):
    request_type: ClassVar[type[BaseModel]]
    runtime_arg_keys: ClassVar[frozenset[str]] = frozenset()

    def create_payload(
        self,
        request: dict[str, Any] | BaseModel,
        extra_headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> tuple[AgenticPayload, dict[str, str]]: ...

    async def stream(
        self,
        request: dict[str, Any] | AgenticPayload,
        extra_headers: dict[str, str] | None = None,
        **runtime_args: Any,
    ) -> AsyncIterator[StreamChunk]: ...
```

The agentic envelope contains exactly `{"request": <validated provider model>}` and an empty
generic-header mapping. Remote adapters may build transport headers internally from their typed
configuration; secrets do not become payload fields.

`AgenticHandlersMixin` moves to `lionagi/providers/_agentic/handlers.py` with its existing runtime
surface preserved:

```python
class AgenticHandlersMixin:
    _handler_params: ClassVar[tuple[str, ...]]
    _handler_kwarg: ClassVar[str]
    request_type: ClassVar[type[BaseModel]]

    def update_handlers(self, **kwargs: Callable | None) -> None: ...
    def copy_runtime_state_to(self, other: Endpoint) -> None: ...
    def _runtime_handlers(self, kwargs: dict[str, Any]) -> dict[str, Callable]: ...
```

The compatibility names `transport_arg_keys`, `_request_model`,
`lionagi.providers._agentic_handlers`, and the vendor-specific handler properties remain re-exports
or aliases during migration. Their values are derived from `runtime_arg_keys` and `request_type`;
they are not second authorities.

**Exact semantics**

- **Classification order.** `iModel.create_api_calling()` removes declared runtime keys from the
  request mapping first and stores them in `APICalling.call_kwargs`. Remaining keys are provider
  request candidates. No callable is present in `APICalling.payload` or serialization.
- **Request model.** A dict is validated by `request_type`. A supplied `BaseModel` must already be an
  instance of `request_type` or is revalidated from its `model_dump`; it is never passed to another
  provider unchanged.
- **Unknown request field.** Any key left after runtime classification that is not accepted by the
  provider request model raises `AgenticRequestValidationError` before a subprocess, task, or HTTP
  client is created. The shared layer does not silently filter it.
- **Unknown handler.** A runtime handler name outside `runtime_arg_keys` raises
  `AgenticHandlerError`. A handler value must be callable or `None`; `None` explicitly disables a
  configured handler for that call.
- **Precedence.** Per-call handlers override endpoint-configured handlers. Missing per-call keys use
  the configured value. Handler mappings are copied on endpoint copy; callable objects themselves
  are shared deliberately.
- **Prompt construction.** Provider request models own messages-to-prompt conversion, system prompt
  placement, resume grammar, and empty-prompt rejection. The generic layer only guarantees that
  validation completes before transport.
- **Mutable values.** Request models and runtime mappings are new per call. An endpoint does not
  retain a caller-owned mutable request dict.
- **Error path.** Validation and handler errors are not provider failures, are not retried, and
  produce no stream chunks. ADR-0029 terminalizes the owning `APICalling` if it was already
  appended.
- **Headers.** `extra_headers` is rejected for subprocess and in-process adapters unless a concrete
  adapter documents a safe mapping. It is not forwarded into a command environment or model data.

**Why this way.** The current `{"request": BaseModel}` envelope is already the narrow seam used by
all agentic providers. Making classification explicit closes the silent-drop and callable-leak
paths without inventing another provider protocol. Request meaning remains beside vendor models;
the shared code decides only when input is valid enough to start work.

### D3 — Normalize chunks, terminal results, errors, and resumable sessions

The public stream item remains the existing dataclass
(`lionagi/service/types/stream_chunk.py`; `StreamChunk`):

```python
ChunkType = Literal[
    "system", "thinking", "text", "tool_use",
    "tool_result", "result", "error",
]

@dataclass(slots=True)
class StreamChunk:
    type: ChunkType
    content: str | None = None
    tool_name: str | None = None
    tool_id: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_output: Any | None = None
    is_error: bool = False
    is_delta: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
```

Supported one-shot adapters return one stable mapping. All keys are present; unavailable values use
`None` or an empty mapping rather than disappearing:

```python
class AgenticCallResult(TypedDict):
    result: str
    session_id: str | None
    model: str | None
    usage: dict[str, Any]
    metadata: dict[str, Any]

class AgenticResultMetadata(TypedDict, total=False):
    session_id: str
    model: str
    usage: dict[str, Any]
    total_cost_usd: float
    num_turns: int
    duration_ms: int
    duration_api_ms: int
```

Provider parsers may retain an internal accumulator with the shipped `CLISession` fields—session
id, model, chronological chunks, thinking/messages/tool views, result, usage, cost, turns,
durations, error flag, and summary—but neither `CLISession`, `PiChunk`, raw provider event dicts,
nor AG2 event objects are yielded by `AgenticEndpoint.stream()`.

The common error hierarchy extends the shipped `ProviderError` family in
`lionagi/providers/_agentic/errors.py`:

```python
class ProviderError(RuntimeError):
    stderr_tail: str
    raw: str

class ProviderQuotaError(ProviderError): ...
class ProviderAuthError(ProviderError): ...
class ProviderContextError(ProviderError): ...
class ProviderTransportError(ProviderError): ...
class ProviderExecutableNotFound(ProviderTransportError): ...

class ProviderProcessError(ProviderTransportError):
    returncode: int

class ProviderProtocolError(ProviderError): ...
class ProviderStreamError(ProviderError): ...
class AgenticRequestValidationError(ValueError): ...
class AgenticHandlerError(ValueError): ...
class AgenticOperationUnsupported(RuntimeError): ...
```

Compatibility imports from `lionagi.providers._provider_errors` continue to resolve to these
classes. `classify_provider_error()` retains its quota, authentication, and context classifiers and
falls back to `ProviderError`.

**Exact stream semantics**

- **System.** A system chunk carries provider/session/model/capability metadata. It does not carry
  assistant text. Multiple vendor system events may be projected when their metadata changes.
- **Thinking.** Reasoning trace appears only in thinking chunks. Missing reasoning is represented by
  no chunk, not an empty synthetic chunk.
- **Text.** User-visible assistant text appears only in text chunks. `is_delta=True` means content
  must be concatenated in order; `False` means the chunk is a complete text unit.
- **Tool use.** A tool-use chunk carries the provider id when one exists, the provider tool name,
  and parsed input. An adapter must not invent an id. Tool results preserve the matching id and set
  `is_error=True` only when that tool execution failed; a failed tool result is not automatically a
  failed provider stream.
- **Result.** A successful stream emits at most one result chunk, after all content chunks. Its
  `content` contains only terminal text not already emitted as text; otherwise it is `None`.
  `metadata` contains the fields from `AgenticResultMetadata` that the provider actually reported.
  A result chunk is permitted but not required for normal EOF.
- **Normal empty EOF.** A provider that produces no semantic output and raises no error completes
  normally. The adapter does not invent text, a result, or an error merely to make the stream
  non-empty.
- **Provider-declared error.** The adapter yields exactly one
  `StreamChunk(type="error", is_error=True, content=<safe message>)`, records the provider class in
  metadata when known, and yields nothing afterward. ADR-0029's supervisor marks the event failed
  and raises `ProviderStreamError` after the observable error-chunk boundary.
- **Transport or parser error.** A failure that has no provider error event raises the most specific
  `ProviderTransportError` or `ProviderProtocolError`; it does not synthesize normal EOF. The
  supervisor owns terminal event state and propagation.
- **Unknown vendor event.** A provider parser may ignore an explicitly allowlisted telemetry-only
  event. Any other unknown event that can contain text, tool activity, result, or error raises
  `ProviderProtocolError`; it is not projected as a raw system chunk.
- **Cancellation.** Cancellation yields no synthetic error/result chunk. It propagates after the
  transport-specific cleanup in D4 or D5; ADR-0029 marks the event `CANCELLED`.
- **Consumer close.** `aclose()` follows the cancellation cleanup path but returns cleanly after
  resources are reaped. No chunk is yielded from `finally`.

**Exact session semantics**

- **Capability gate.** `iModel` injects stored state only when `capabilities.resumable=True` and
  `resume_field` names a field present on `request_type`. A mismatch is catalog validation failure.
- **Publication.** A resumable adapter publishes the canonical provider identifier as
  `metadata["session_id"]` on a system chunk as soon as it is confirmed. It also returns it in
  `AgenticCallResult` and terminal result metadata when those outputs exist.
- **Storage.** `iModel` stores only a non-empty confirmed identifier. A later empty value does not
  erase the last confirmed id.
- **Injection.** Explicit caller values in `resume_field` or a provider-supported `session_id` field
  win. Otherwise `iModel` injects the stored identifier into `resume_field` before request-model
  validation.
- **Non-resumable adapters.** They do not receive automatic `resume`. An observational upstream id
  may be retained as vendor metadata under `provider_session_id`, but not under the canonical
  resumable `session_id` key.
- **Failure.** Failure or cancellation does not erase a confirmed id. An id seen only in an invalid
  or unparseable event is not stored.
- **Copy.** `iModel.copy(share_session=False)` starts without session state;
  `share_session=True` copies a confirmed id only for resumable adapters.

**Why this way.** `StreamChunk` is already the consumer contract; the defect is inconsistent
projection at its edges. Keeping `CLISession` internal lets parsers accumulate vendor state without
making a second public stream type. Explicit error and resume semantics remove inference from
content, final accumulator state, or the presence of any provider identifier.

### D4 — Centralize subprocess framing, safety, and deterministic teardown

The generic subprocess and handler helpers move under one internal support package with
compatibility re-exports:

```text
lionagi/providers/_agentic/
├── __init__.py          AgenticCapabilities and error re-exports
├── capabilities.py      D1 capability vocabulary
├── handlers.py          D2 runtime-handler validation/copying
├── subprocess.py        D4 argv, NDJSON, workspace, stderr, teardown
└── errors.py            D3 provider/transport/protocol error hierarchy

lionagi/providers/
├── _agentic_handlers.py  compatibility re-export
├── _cli_subprocess.py    compatibility re-export
└── _provider_errors.py   compatibility re-export
```

The subprocess entry point remains Python-native and provider-neutral:

```python
STDERR_CAPTURE_LIMIT = 256 * 1024
STDERR_DRAIN_TIMEOUT = 2.0
PROCESS_TERMINATE_GRACE = 5.0
READ_SIZE = 4096

async def ndjson_from_cli(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    stdin: Any = asyncio.subprocess.DEVNULL,
    tail_repair: Callable[[str], dict[str, Any] | None] | None = None,
) -> AsyncIterator[dict[str, Any]]: ...

def resolve_cli_workspace(repo: Path | None, workspace: str | None) -> Path: ...
def build_declarative_cli_args(model_instance: BaseModel) -> list[str]: ...
def discover_cli(binary: str) -> tuple[bool, str | None]: ...
```

**Exact semantics**

- **Argument vector.** The helper calls `asyncio.create_subprocess_exec(*cmd, ...)`. It never
  invokes a shell or accepts a prejoined command string. Provider request models place flags and
  values into distinct argv elements.
- **Working directory.** An absent repository uses `Path.cwd()`. An absent/empty workspace returns
  the repository. An absolute workspace or lexical traversal is rejected; the resolved path must
  remain contained by the repository. Provider-specific file fields receive their own containment
  validation before argv construction.
- **Process group.** Every process starts with `start_new_session=True`. The helper captures stdout
  and stderr and uses `DEVNULL` stdin by default. Pi may explicitly request inherited stdin through
  the compatibility sentinel; other adapters do not inherit it accidentally.
- **Framing.** Incremental UTF-8 decoding accepts newline-delimited or directly concatenated JSON
  values. Leading whitespace is ignored. Each parsed value must be a mapping; a scalar/array is
  `ProviderProtocolError`.
- **Tail.** A complete final JSON object is yielded. Claude Code may supply its existing JSON repair
  callback. If repair produces one mapping it is yielded; no repair, a `None` repair, or a repair
  failure raises `ProviderProtocolError` instead of silently dropping a potentially semantic tail.
- **Stderr.** Stderr drains concurrently with stdout so a full pipe cannot block the child. At most
  `STDERR_CAPTURE_LIMIT` leading bytes are retained for diagnostics; excess bytes are drained and
  discarded. Captured text is decoded with replacement and never treated as stdout events.
- **Exit zero.** Normal stdout EOF plus exit code zero ends the iterator. The `finally` path still
  reaps resources and the stderr task.
- **Exit nonzero.** The helper waits up to `STDERR_DRAIN_TIMEOUT` for the drain, classifies the
  bounded content, and raises a typed `ProviderProcessError` or the more specific
  quota/auth/context error. It never returns a successful result because stdout contained earlier
  chunks; already yielded chunks remain observable before the error.
- **Missing binary.** Discovery failure raises `ProviderExecutableNotFound` before process creation
  with the provider's installation hint. It is not retried.
- **Cancellation/close.** In `finally`, send SIGTERM to the process group and direct child, wait up
  to `PROCESS_TERMINATE_GRACE`, then SIGKILL the group and child if still alive, and await reaping.
  The stderr task is cancelled and awaited. `CancelledError` propagates after cleanup.
- **Primary error.** A secondary close/reap failure is logged and cannot replace an exception or
  cancellation already in flight. With no primary error, a cleanup failure remains visible.

The inherited numeric values are part of the record:

| Value | Meaning | Recorded rationale |
|-------|---------|--------------------|
| 256 KiB | Maximum retained stderr per subprocess; draining continues after the cap. | Bounds diagnostic memory while preventing pipe deadlock. The exact cap is inherited; no tuning evidence is recorded. |
| 4 KiB | Stdout/stderr read size. | Inherited implementation value; no design rationale is recorded. It is not a total-output cap. |
| 2 seconds | Extra wait for stderr drain after nonzero process exit. | Inherited; prevents diagnostics from blocking failure forever. The exact duration is not justified in source. |
| 5 seconds | SIGTERM-to-SIGKILL grace for the process group. | Inherited; gives cooperative cleanup a chance before preventing an orphan. The exact duration is not justified in source. |

**Why this way.** These mechanics are identical across four adapters and have dedicated regression
coverage. Centralizing them avoids four independent security and cancellation implementations.
The helper deliberately stops below command grammar and event meaning: Codex workspace flags,
Claude tail repair, Gemini model resolution, and Pi stdin behavior remain explicit provider choices.

### D5 — Retain transport-specific lifecycle below the normalized boundary

Subprocess, in-process, and remote adapters satisfy D2/D3 but own different resource cleanup. The
target does not insert a generic executor-provider protocol between `AgenticEndpoint` and these
mechanics.

**In-process adapters**

- `AG2BetaEndpoint` retains the current `run_beta_agent()` task/queue/subscription design. On normal
  completion it drains queued events before the response. On error, cancellation, or consumer close
  it unsubscribes both stream callbacks; if the agent task is unfinished it cancels and awaits it.
- Its current 0.1-second queue poll remains an internal responsiveness interval. The value is
  inherited with no recorded numeric rationale; it is not a request timeout and the caller deadline
  from ADR-0029 still governs the task.
- A pre-built agent continues to take precedence over `AgentConfig`. Without a pre-built agent,
  both a valid config and model configuration are required before the task starts. Unknown observer
  and policy names remain vendor-owned validation behavior until their provider model is tightened.
- `AG2GroupChatEndpoint` retains event mapping beside the AG2 module. The default `max_round=15`
  remains a provider request limit with Pydantic `gt=0` validation. It is inherited; source records
  no rationale for fifteen. Consumer close closes the AG2 iterator before releasing admission.
- Base AG2 agent/group-chat queue and concurrency defaults remain `3` waiting and `1` active, as
  recorded in ADR-0029. Serial active execution protects adapter-owned mutable agent state; the
  exact queue value is inherited.

**Remote adapter**

- `AG2NlipEndpoint` validates that its URL uses `http` or `https` and that the hostname passes the
  shared SSRF check before creating a client. Missing URL or prompt fails before connection.
- The HTTP client is scoped to one logical request and closes on success, exception, deadline, or
  cancellation. SDK and direct-HTTP paths return the same mapping:

  ```python
  {
      "content": str,
      "context": Any | None,
      "input_required": Any | None,
  }
  ```

- Its current local transport cap is 60 seconds and its current total attempt count is three.
  Both are inherited without recorded tuning rationale. Under ADR-0029 they become the endpoint
  local cap and `AttemptPolicy.max_attempts`; the adapter must not wrap that policy in a second
  hidden retry loop. Only timeout/connect failures are retryable before output; status and response
  validation failures propagate on their first occurrence.
- `max_attempts <= 0` is invalid target configuration. The shipped helper currently interprets
  `max_retries <= 0` as “make no request and return an empty result”; migration replaces that silent
  success with validation failure.
- Base NLIP queue/concurrency defaults remain `10` waiting and `3` active, as recorded in ADR-0029.

**Vendor ownership**

```text
lionagi/providers/
├── openai/codex.py             Codex request, flags, event mapping
├── anthropic/claude_code.py    Claude request, flags, tail repair, events
├── google/gemini_code.py       Gemini request, model mapping, terminal JSON
├── pi/cli.py                   Pi request, env/argv, incremental events
├── ag2/agent.py                in-process beta-agent construction/events
├── ag2/groupchat.py            group-chat models/event mapping
└── ag2/nlip.py                 remote NLIP request/wire adaptation
```

Provider request fields, permission/sandbox modes, model aliases, event allowlists, callbacks, and
fallback parsing remain in those modules. Shared code enters `_agentic/` only after at least two
adapters use the same semantics.

**Why this way.** Observable sameness does not require identical internals. Process signals cannot
cancel an in-process task, and an HTTP client has neither a process group nor an AG2 subscription.
Keeping cleanup beside the resource makes it testable while D2/D3 give operations one request and
output contract.

### D6 — Run one capability-driven conformance suite for every adapter

The suite lives under `tests/providers/agentic/` and parametrizes every registered
`AgenticEndpoint`, including scripted test support. Vendor parser fixtures stay beside their
providers; the conformance layer consumes adapter-level fakes rather than duplicating raw grammars.

| Case | Required assertions |
|------|---------------------|
| Capability declaration | Frozen record exists; transport is one closed value; resume field agrees with request model; catalog inventory matches the matrix in D1. |
| Request validation | Valid dict produces exactly `{"request": request_type(...)}` and empty headers; unknown request/runtime key and non-callable handler fail before transport. |
| Runtime separation | Callables appear only in `APICalling.call_kwargs`; payload serialization contains none; per-call handler override does not mutate endpoint defaults. |
| Stream type | Every yielded public item is `StreamChunk`; no `CLISession`, `PiChunk`, raw dict, or AG2 event escapes. |
| Chunk order | System/semantic chunks precede at most one result; nothing follows result or error; text is not duplicated into result content. |
| Empty success | Empty adapter fixture reaches normal EOF without a synthetic result or error. |
| Provider error | Exactly one error chunk has `type="error"` and `is_error=True`; no result/later chunk; supervisor observes typed failure. |
| Transport/parser error | Specific typed exception propagates; event fails; no conversion to normal EOF. |
| Resumable adapter | System chunk publishes confirmed id; one-shot/result mapping carries it; next request injects it unless caller supplied a value. |
| Non-resumable adapter | No automatic resume injection; observational ids do not use canonical `session_id`. |
| Tool capability | `True` adapters map fixture use/result with ids and error bit; `False` adapters do not fabricate tool events. |
| Usage capability | `True` adapters expose provider usage in terminal metadata/result; `False` adapters may omit it and do not synthesize zeros. |
| One-shot adapter | Returns all `AgenticCallResult` keys; ordinary failure follows ADR-0029 event semantics. |
| Stream-only adapter | `invoke()` raises `AgenticOperationUnsupported` before task/process/client creation. |
| Caller cancellation | Original cancellation propagates after cleanup; no result/error chunk is synthesized; no active resource remains. |
| Consumer close | `aclose()` reaps the transport and returns without a yield from `finally`. |
| Endpoint copy | Runtime handlers copy; active resource does not; session copies only when requested and resumable. |

Subprocess adapters additionally prove:

- argv elements are passed to `create_subprocess_exec` without shell joining;
- workspace traversal, absolute workspace, and symlink escape are rejected before launch;
- stdout supports split and concatenated JSON; invalid/non-mapping tail fails unless Claude's repair
  returns a mapping;
- stderr beyond 256 KiB is drained without being retained and cannot deadlock stdout;
- missing executable and nonzero exit produce typed errors with bounded stderr;
- normal EOF, parser failure, timeout, caller cancellation, and early consumer close all terminate
  and reap the process group, escalating after the five-second grace when required; and
- a cleanup exception never masks an already propagating provider or cancellation exception.

The suite preserves the existing focused provider tests for command arguments, path safety, Codex
benign end-of-stream classification, Gemini terminal JSON, Claude tail repair, Pi event mapping,
AG2 agent task teardown, group-chat event projection, NLIP SSRF/retry behavior, and scripted output.
Conformance tests add cross-adapter invariants; they do not replace grammar fixtures.

**Why this way.** Per-provider tests can all pass while adapters disagree at the shared boundary.
One suite pins the properties operations rely on, and capability predicates prevent it from forcing
fake tool, usage, or resume behavior on providers that do not have it.

## Consequences

- Operations select agentic streaming without confusing transport type. Subprocess-only behavior
  becomes explicit through `capabilities.transport == "subprocess"`.
- `APICalling` and `StreamChunk` remain the public service boundary. Existing endpoint/provider
  names remain stable, and helper moves retain compatibility re-exports.
- Provider errors become observable the same way from every adapter. Tightening `is_error` and
  raising parser/transport failures can change consumers that currently treat malformed or empty
  streams as success.
- Resumability becomes opt-in and provable. Codex no longer receives a silently filtered `resume`;
  adding real Codex continuation later is a provider-local typed change plus a matrix update.
- Internal `CLISession` remains useful for summaries and one-shot aggregation but is no longer a
  second public stream union member. Adapters must project its terminal data explicitly.
- Subprocess teardown and stderr behavior evolve once. A regression in the shared helper affects
  four providers, so its conformance coverage is a release gate.
- In-process and remote adapters keep their correct resource mechanics. The shared abstraction does
  not require subprocess concepts or duplicate ADR-0029 retry/deadline ownership.
- The target adds stricter validation: unknown request keys, handler keys, non-mapping provider
  events, silent malformed tails, and NLIP nonpositive attempts become typed failures. Compatibility
  warnings are required where a public caller previously relied on silent filtering.
- Reversing D1/D3 after consumers migrate is high cost because routing and error/session semantics
  become cross-operation contracts. Moving support files is low cost because compatibility imports
  remain. Adding a new adapter is medium cost: it must declare capabilities and pass conformance.
- Maintainers must know which layer owns each failure: adapter input validation (D2), provider event
  projection (D3), transport cleanup (D4/D5), or admission/deadline terminalization (ADR-0029).

## Alternatives considered

### Introduce a separate executor-provider protocol and registry

A new protocol could select a subprocess, in-process, or remote executor independently of endpoint
selection. It would make scheduling replaceable and give transport families their own registry. It
lost because `AgenticEndpoint` and `EndpointRegistry` are already the live extension and resolution
seams. A second registry would duplicate provider keys, aliases, availability, fallback, and
lifecycle rules while still needing the selected endpoint's request/event grammar.

### Keep `is_cli` as the permanent operation-routing flag

This avoids changing consumers and accurately describes the original CLI adapters. It lost because
AG2 in-process and remote endpoints already inherit `True`; new code cannot know whether the flag
means process transport or agentic execution. A deprecated alias preserves migration behavior while
`is_agentic` and `transport` name the two facts separately.

### Move every agentic adapter into one package

One directory would make the inventory easy to scan and could centralize all request and parser
types. It lost because it separates vendor request/flag/event grammar from the provider packages
that own API endpoints, aliases, fixtures, and release changes. The target centralizes only mechanics
already shared by multiple adapters.

### Force one universal request model or command schema

A single `AgenticRequest` could expose prompt, model, tools, sandbox, resume, and timeout for every
provider. It would make construction look uniform. It lost because permission modes, file grants,
session commands, model identifiers, and even transport inputs differ materially. Unsupported
fields would either be ignored—a recurrence of P2—or falsely promise capability. The invariant is
validated provider-owned input, not identical input.

### Yield `CLISession` as a public final stream item

Keeping the existing low-level union would expose summaries and usage without projection work and
would let callbacks consume the same object. It lost because operations are written against
`StreamChunk` and each endpoint currently treats the accumulator differently. A private accumulator
plus typed result metadata keeps useful state without requiring every consumer to branch on a second
type.

### Require exactly one result chunk from every successful stream

This would give EOF a visible terminal record and a uniform place for usage. It lost because some
providers have no terminal metadata and normal empty EOF is already valid under ADR-0029. Requiring
a chunk would create synthetic content. At most one result provides a stable ordering rule without
inventing data.

### Treat any provider identifier as resumable

The shared layer could continue storing every `session_id`/`thread_id` and injecting `resume`. This
would make continuation automatic for providers that add support incidentally. It lost because a
returned identifier does not prove a request field or command grammar; Codex demonstrates the
failure by publishing a thread id while filtering the injected field. Resumability requires an
explicit round trip.

### Use shell-string process execution

Shell execution would simplify command logging, quoting, and pipelines. It lost because prompts,
paths, model names, and provider arguments are caller-controlled. Argument-vector execution avoids
shell interpretation and matches the existing implementation and cancellation tests.

### Let every adapter own subprocess reading and teardown

Vendor-local process code would allow different framing and termination behavior. It lost because
the adapters already use the same NDJSON/process-group helper, and the shared properties prevent
deadlock and orphan processes. Provider-specific tail repair, stdin, cwd, and argv remain extension
points without duplicating the safety core.

### Normalize all failures as error chunks and never raise

Pure data-plane errors would make streams easy to record and avoid exception classification. It lost
because connection, parser, cancellation, and setup failures may occur before any valid chunk and
must remain typed control flow. Provider-declared errors get one observable error chunk; ADR-0029
then raises after the boundary so direct and operation consumers cannot mistake failure for EOF.

### Test adapters only with vendor-specific fixtures

Local fixtures are best at detecting event-grammar drift and require no abstraction. They lost as
the only test layer because each adapter can be locally correct while disagreeing on error flags,
terminal result duplication, session injection, or cleanup. The conformance suite asserts only the
shared boundary and leaves parser fixtures in place.

## Notes

This is a target-state ADR. `AgenticCapabilities`, `is_agentic`, the `_agentic/` support package,
strict request classification, the unified result mapping, and the conformance suite are not
shipped. The source contracts constraining migration are:

- `lionagi/service/connections/{agentic_endpoint,endpoint,api_calling}.py` and
  `lionagi/service/types/{stream_chunk,cli_session}.py`;
- `lionagi/service/imodel.py`, `lionagi/session/branch.py`, and
  `lionagi/operations/run/run.py`;
- `lionagi/providers/{_agentic_handlers,_cli_subprocess,_provider_errors}.py` and
  `lionagi/ln/_proc.py`;
- `lionagi/providers/openai/codex.py`, `anthropic/claude_code.py`,
  `google/gemini_code.py`, `pi/cli.py`, and `ag2/{agent,groupchat,nlip}.py`; and
- `lionagi/testing/_endpoint.py` plus the existing provider/service tests named in D6.

The compatibility migration order is: publish capabilities and aliases; update operation routing to
`is_agentic`; move helpers behind compatibility re-exports; normalize each adapter and make it pass
conformance; then deprecate and remove `is_cli` under repository policy.
