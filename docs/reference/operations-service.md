# Operations & Service Reference

## Middle Protocol

`Middle` is a callable with signature `(branch, instruction, **kwargs) -> text | dict | BaseModel`
that advances a branch by exactly one assistant turn.

Two built-in implementations:

- `operations/communicate/communicate.py` — `communicate()`: one-shot chat + parse for API endpoints
- `operations/run/run.run_and_collect()`: stream accumulation + optional parse for CLI endpoints

Override per call:

```python
branch.operate(instruction=..., middle=my_callable)
```

Force streaming (e.g. CLI endpoint on an API branch):

```python
branch.operate(instruction=..., stream_persist=True)
```

## Operation Lifecycle

### `run()` (CLI stream generator)

`run()` yields `Instruction / AssistantResponse / ActionRequest / ActionResponse` messages.
It emits exactly one `RunEnd` (clean exit or consumer abandon) or `RunFailed` per `RunStart`.
`suppress_lifecycle_var` suppresses nested signals inside `Branch.ReAct()` turns.

### `ReActStream`

Core reactive loop. By default, it yields each analysis/result object followed by the
final-answer object. With `verbose_analysis=True`, every item is instead a
`(result, formatted_markdown)` tuple, including the final answer. `ReAct_v1` collects
all outputs; `ReAct` is the legacy public wrapper.

### `operate()`

Single construction path for all callers. Delegates to `Operative` / `Step` for structured
output request/response model construction.

## Observation & Governance (`_observe.py`)

Transport-neutral observer layer. Handles:

- Capability emission (structured-output events)
- Governance events (permission checks, ADR-0076 governance gate)
- Control directives

Denied tool calls surface as tool results, not exceptions, so ReAct loops can adapt.

## DependencyAwareExecutor / ReactiveExecutor

`DependencyAwareExecutor` runs a static operation graph respecting dependency order.
`ReactiveExecutor` extends this for self-expanding DAGs: operations may emit `SpawnRequest`
to inject new nodes into the running flow.

## Service Layer

### Token Budget

Context window resolution order: endpoint config > provider longest-prefix lookup > default (128k).

### EndpointRegistry

`register_endpoint` decorator injects `_ENDPOINT_META` and registers the class. Single-endpoint
providers (claude_code, codex, pi) always match on provider name alone.

### HookedEvent

Template-method mixin adding pre/post hooks around `_core_invoke()` / `_core_stream()`.
Post-stream hook failures are logged at WARNING only — data was already sent and reraising
would corrupt the stream.

### parse_model_spec

Strips effort suffix, expands backend aliases, validates effort support per provider.
Invariant: a provider cannot appear in both `PROVIDERS_NO_EFFORT` and `PROVIDER_EFFORT_KWARG`
(checked at import time with `RuntimeError`).
