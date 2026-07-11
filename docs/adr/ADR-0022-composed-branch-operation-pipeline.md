# ADR-0022: Composed branch operation pipeline

- **Status**: Accepted
- **Kind**: Retrospective
- **Area**: operations
- **Date**: 2026-07-09
- **Relations**: extends ADR-0021

## Context

`Branch.operate()` is the composed operation entry point: one call can select an API or CLI adapter,
ask for generated structured fields, expose actions to a model, validate the returned value, execute
accepted action requests, and attach action results. Four problems force the current coordinator
shape.

**P1 — A broad public call must become a small typed internal request.** The facade accepts an
`Instruct` or loose instruction/guidance/context values, model overrides, response types, field
specifications, action controls, persistence controls, and an optional `Middle`. Passing that bag
through every stage would make precedence and defaults implicit. `prepare_operate_kw()` resolves it
into `ChatParam` or `RunParam`, optional `ParseParam`, optional `ActionParam`, and coordinator flags
(`lionagi/session/branch.py`; `lionagi/operations/operate/operate.py`).

**P2 — The model request schema and the returned result schema are not identical.** When reasoning,
additional fields, or actions are enabled, the request may include `reason`, `action_required`, and
`action_requests`. `action_responses` cannot be requested from the model because it is produced by
the runtime after tool execution, but it must exist in the final response type. The internal
`Operative` materializes those two related models (`lionagi/operations/operate/step.py`;
`lionagi/operations/operate/operative.py`).

**P3 — Validation policy and action policy must have a defined order.** A raw adapter result can be
text, a mapping, or a Pydantic model. The caller can request return-value, return-`None`, or raise
behavior on a model mismatch. Action requests are safe to inspect only after the adapter has
returned and the outer validation policy has run. The shipped shortcut `skip_validation=True`
returns even earlier and therefore also bypasses the action phase.

**P4 — Tool execution must not fork by transport.** API-native and CLI-native model responses can
both request tools, but authorization, hook emission, event logging, message recording, error
conversion, and execution strategy belong to `act()`. If each adapter executed tools directly,
those governance paths could diverge.

The public contract has accumulated three known seams. A caller-supplied `operative` is accepted by
`Branch.operate()` and `prepare_operate_kw()` but intentionally replaced with `None`. `Structure`
exists in `ChatParam` and `ParseParam` and is used inside parsing, but no dedicated `structure`
argument exists on `Branch.operate()` or `prepare_operate_kw()`. Finally, the name
`skip_validation` describes only one of the two phases it disables.

| Concern | Decision |
|---------|----------|
| Public argument normalization | D1: the preparer produces typed chat/run, parse, and action parameter objects with fixed precedence. |
| Request/response shape | D2: one internal `Operative` generates a request model without action responses and a response model with them. |
| Adapter and validation order | D3: one `Middle` invocation precedes outer model-policy handling; raw short-circuit returns before all post-processing. |
| Action execution and enrichment | D4: explicit action enablement plus structured requests dispatch through `act()` and augment the original result. |

This ADR deliberately does **not** decide:

- The persistence and lifecycle behavior of the canonical `Middle` implementations; ADR-0021 owns
  the API/CLI adapter contracts.
- Multi-round LNDL behavior; ADR-0024 records that specialized adapter.
- Action-manager registration, tool-schema generation internals, or the governance policy itself;
  this ADR decides only that the common action path is used.
- Dependency ordering between multiple `operate()` calls; ADR-0023 owns graph execution.
- A new public `Structure` or caller-owned `Operative` contract. Their current non-selection and
  replacement behavior are recorded as retrospective deltas, not silently promoted to a target.

## Decision

### D1 — `prepare_operate_kw()` normalizes the public call into typed parameters

The implementation contract is:

```python
def prepare_operate_kw(
    branch: Branch,
    *,
    instruct: Instruct = None,
    instruction: Instruction | JsonValue = None,
    guidance: JsonValue = None,
    context: JsonValue = None,
    sender: SenderRecipient = None,
    recipient: SenderRecipient = None,
    progression: Progression = None,
    chat_model: iModel = None,
    invoke_actions: bool = True,
    tool_schemas: list[dict] = None,
    images: list = None,
    image_detail: Literal["low", "high", "auto"] = None,
    parse_model: iModel = None,
    skip_validation: bool = False,
    handle_validation: HandleValidation = "return_value",
    tools: ToolRef = None,
    operative: Operative = None,
    response_format: type[BaseModel] = None,
    actions: bool = False,
    reason: bool = False,
    call_params: AlcallParams = None,
    action_strategy: Literal["sequential", "concurrent"] = "concurrent",
    verbose_action: bool = False,
    field_models: list[FieldModel | Spec] = None,
    include_token_usage_to_model: bool = False,
    clear_messages: bool = False,
    stream_persist: bool = False,
    persist_dir: str | None = None,
    snapshot_dir: str | None = None,
    middle: Middle | None = None,
    **kwargs,
) -> dict: ...
```

The instruction model normalized by the preparer has these Pydantic fields:

```python
class Instruct(HashableModel):
    instruction: str | None = None
    guidance: JsonValue | None = None
    context: JsonValue | None = None
    reason: bool | None = None
    actions: bool | None = None
    action_strategy: Literal["sequential", "concurrent"] | None = None
```

The returned mapping has one fixed shape:

```python
{
    "instruction": instruct.instruction,
    "chat_param": ChatParam(...) | RunParam(...),
    "parse_param": ParseParam(...) | None,
    "action_param": ActionParam(...) | None,
    "handle_validation": "raise" | "return_value" | "return_none",
    "invoke_actions": bool,
    "skip_validation": bool,
    "clear_messages": bool,
    "operative": None,
    "middle": Middle | None,
    "field_models": list[FieldModel | Spec] | None,
    "reason": bool | None,
}
```

Exact normalization semantics:

- `chat_model` defaults to `branch.chat_model`; `parse_model` defaults to that selected chat model,
  not independently to `branch.parse_model`.
- A mapping passed as `instruct` is validated into `Instruct`. When no `instruct` is supplied, one is
  built from the loose instruction, guidance, and context arguments.
- Explicit `reason=True` sets `instruct.reason = True`. Explicit `actions=True` sets
  `instruct.actions = True` and copies a truthy `action_strategy` into the instruction model.
- `RunParam` is selected when the selected model is CLI-backed, `stream_persist` is true, or either
  `persist_dir` or `snapshot_dir` is not `None`. Otherwise the preparer builds `ChatParam`.
- `RunParam.stream_persist` is copied even when false. `persist_dir` and `snapshot_dir` override their
  dataclass defaults only when explicitly non-`None`.
- `response_format` is copied into the chat/run parameter. A `ParseParam` is created only when a
  response format exists and validation is not skipped.
- `ActionParam` is created only when `invoke_actions` is true **and** either the normalized
  instruction or the loose `actions` flag enables actions. The implementation expression is
  `action_strategy or instruct.action_strategy or "concurrent"`, and it always uses
  `suppress_errors=True`.
- At the public `Branch.operate()` boundary, `action_strategy` itself defaults to the truthy value
  `"concurrent"` and is always forwarded. Consequently an `Instruct(actions=True,
  action_strategy="sequential")` does **not** select sequential execution when the caller omits the
  loose keyword: the facade default wins. The intended-looking instruction fallback is reachable
  only through a direct internal preparer call that supplies a falsey strategy. The public API
  cannot currently distinguish “keyword omitted” from “explicitly concurrent.”
- Public `tool_schemas` are copied into the initial chat/run parameter, but D2 replaces the effective
  schemas from the selected branch tools whenever an `ActionParam` exists.
- `request_model`, `operative_model`, and `imodel` in `**kwargs` are rejected as removed names with
  guidance to `response_format=` or `chat_model=`. Remaining `**kwargs` become model invocation
  options.
- A caller-supplied `operative` is discarded by setting the returned value to `None`. This is
  deliberate in the current code so D2 has one construction site, but it makes the accepted public
  argument ineffective.
- `field_models` entries are converted later from `FieldModel` or `Spec`; any other entry raises
  `TypeError`. Entries whose resulting `Spec.name` is empty do not enter the generated field map.

The structured-parse default created by `make_parse_param()` uses:

```python
AlcallParams(
    retry_initial_delay=1,
    retry_backoff=1.85,
    retry_attempts=3,
    max_concurrent=1,
    throttle_period=1,
)
```

These inherited values serialize parsing attempts, allow three retries, and space retry work. The
operations code records no measurement or other rationale for the exact `1`, `1.85`, `3`, and `1`
values; they are centralized in `lionagi/operations/_defaults.py` so callers can replace the
parameter object where exposed.

**Why this way.** A preparer gives every downstream stage one small vocabulary and one precedence
rule. Selecting `RunParam` during preparation makes adapter choice structural rather than a late
collection of Boolean checks. The current ignored `operative` argument is retained only as shipped
truth; it is not evidence that two schema owners are desirable.

### D2 — One generated `Operative` separates model-request and runtime-response fields

The coordinator signature and schema factories are:

```python
async def operate(
    branch: Branch,
    instruction: JsonValue | Instruction,
    chat_param: ChatParam,
    action_param: ActionParam | None = None,
    parse_param: ParseParam | None = None,
    handle_validation: HandleValidation = "return_value",
    invoke_actions: bool = True,
    skip_validation: bool = False,
    clear_messages: bool = False,
    reason: bool = False,
    field_models: list[FieldModel | Spec] | None = None,
    operative: Operative | None = None,
    middle: Middle | None = None,
) -> BaseModel | dict | str | None: ...

class Step:
    @staticmethod
    def request_operative(
        *,
        name: str | None = None,
        operative_name: str | None = None,
        adapter: Literal["pydantic"] = "pydantic",
        reason: bool = False,
        actions: bool = False,
        fields: dict[str, Spec] | None = None,
        field_models: list | None = None,
        max_retries: int = 3,
        auto_retry_parse: bool = True,
        base_type: type[BaseModel] | None = None,
        **kwargs,
    ) -> Operative: ...

    @staticmethod
    def respond_operative(
        operative: Operative,
        additional_fields: dict[str, Spec] | None = None,
    ) -> Operative: ...
```

When actions are requested, the generated semantic fields are:

```python
class ActionRequestModel(HashableModel):
    function: str | None = None
    arguments: dict[str, Any] | None = None

class ActionResponseModel(HashableModel):
    function: str = Field(default_factory=str)
    arguments: dict[str, Any] = Field(default_factory=dict)
    output: Any = None

# Generated nullable/listable fields
action_required: bool | None
action_requests: list[ActionRequestModel] | None
action_responses: list[ActionResponseModel] | None
```

Exact schema semantics:

- The coordinator recognizes a caller model class when `chat_param.response_format` is a
  `BaseModel` subclass, or uses the type of a `BaseModel` instance. A dictionary response format is
  passed to adapters but does not become `model_class` for outer instance checking.
- An internal `Operative` is constructed when at least one of these is present: a base model class,
  action enablement, named field specs, or reasoning.
- `reason=True` adds the nullable `Reason` field. `actions=True` at construction adds
  `action_required`, `action_requests`, and `action_responses`. Named field specs are added by name.
- `Step.request_operative()` sets `request_exclude={"action_responses"}` when actions are present.
  `Step.respond_operative()` then materializes the response model from all specs. Therefore the
  provider request cannot fabricate runtime action results, while the returned model can hold them.
- Generated model names are based on the explicit name, the base type name, or `"Operative"`, with
  `Request` and `Response` suffixes. The only shipped adapter is `"pydantic"`.
- The generated response type replaces `_cctx.response_format` and `_pctx.response_format`, so both
  provider rendering and parsing target the same superset model.
- When an `ActionParam` exists, the coordinator calls `branch.acts.get_tool_schema()` for the
  selected `tools` (or all tools when the selector is absent), unwraps `{"tools": [...]}` to the
  list expected by an instruction, and replaces the effective chat/run tool schemas.
- `Operative.max_retries` defaults to 3, but `operate()` itself never loops over the `Middle`; retry
  behavior belongs to parsing or the selected adapter. The exact default is inherited and has no
  recorded operations-level measurement rationale.

**Why this way.** Request and response are related but not identical protocols. One `Operative`
keeps their shared base and generated fields aligned, while `request_exclude` expresses the one
runtime-only field. Building a completely separate result model would duplicate the base schema and
risk drift; requesting `action_responses` from the model would erase the boundary between proposed
calls and observed tool results.

### D3 — The coordinator invokes one adapter, then applies one outer validation policy

The execution order is normative as-built behavior:

```text
prepared params
    │
    ├─ publish selected tool schemas
    ├─ build request/response Operative when needed
    ├─ select or accept one Middle
    v
await middle(branch, instruction, chat_param, parse_param,
             clear_messages, skip_validation=skip_validation)
    │
    ├─ skip_validation=True ───────────────> return raw result
    ├─ requested model mismatch ───────────> handle_validation policy
    ├─ invoke_actions=False ───────────────> return validated/current result
    └─ otherwise ──────────────────────────> D4 action inspection
```

Exact adapter-selection and validation semantics:

- A caller-supplied `middle` wins without additional wrapping.
- Without one, `RunParam` or a CLI-backed `branch.chat_model` selects `run_and_collect`; otherwise
  the coordinator selects `communicate`.
- The selected `Middle` is awaited exactly once by `operate()`. An adapter may internally stream or
  loop under ADR-0021, but the coordinator does not retry it.
- `clear_messages` and `skip_validation` are passed to the adapter positionally/by keyword as shown
  in the `Middle` protocol. Message persistence remains adapter-owned.
- `skip_validation=True` returns immediately after the adapter. No outer model instance check,
  `invoke_actions` check, action-request inspection, tool execution, provider writeback, or result
  enrichment occurs.
- When a caller model class exists and the returned value is not an instance of that class,
  `handle_validation="return_value"` returns it unchanged, `"return_none"` returns `None`, and
  `"raise"` raises a `ValueError` naming the expected model and including at most the first 200
  characters of `repr(result)`.
- The model mismatch branch returns immediately for `return_value` and `return_none`. It does not
  continue into actions even if the returned mapping happens to contain `action_requests`.
- With no caller model class, a generated action/reason/field model can still be returned and
  inspected. The outer type policy is specifically relative to the caller's requested base model.
- `invoke_actions=False` returns after validation and before request inspection. This flag is a
  second action gate independent of whether an `ActionParam` was prepared.

**Why this way.** The adapter owns transport and inner parsing; the coordinator owns the caller's
outer contract. Applying the mismatch policy before tools prevents side effects from a value the
caller has declared invalid. The raw shortcut is intentionally the earliest return in shipped code,
although its broad scope is a naming/documentation debt captured below.

### D4 — Structured action requests execute through `act()` and augment the result

The action parameter and dispatcher contracts are:

```python
@dataclass(slots=True, frozen=True, init=False)
class ActionParam(MorphParam):
    action_call_params: AlcallParams = None
    tools: ToolRef = None
    strategy: Literal["concurrent", "sequential"] = "concurrent"
    suppress_errors: bool = True
    verbose_action: bool = False

async def act(
    branch: Branch,
    action_request: list | ActionRequest | BaseModel | dict,
    action_param: ActionParam,
) -> list[ActionResponse]: ...
```

The accepted action request payload is structurally:

```json
{
  "function": "registered_tool_name",
  "arguments": {"named_argument": "value"}
}
```

A list of those mappings, an `ActionRequest`, or a Pydantic model exposing both `function` and
`arguments` is accepted. Any other shape raises `ValueError` before invocation.

Exact action and enrichment semantics:

- The coordinator reads `action_requests` from any returned `BaseModel` attribute or mapping key.
  Plain text and other values produce no requests.
- Execution requires an `ActionParam` and `requests is not None`. An absent field does nothing; an
  empty list dispatches but yields no responses and therefore no enrichment.
- All requests go through `act()`. `"concurrent"` uses the configured `AlcallParams` callable;
  `"sequential"` awaits requests in input order; any other strategy raises `ConfigurationError`.
- The default action `AlcallParams` sets `output_dropna=True` and otherwise inherits library
  defaults. No operations-level rationale is recorded for additional concurrency or retry numbers
  because none are fixed here.
- Before invoking a tool, `_act()` asks `branch.authorize(ToolInvocation(...))`. Denial is returned
  as an `ActionResponseModel` with an error payload and is recorded in branch messages; it is not
  raised as a transport exception.
- When hooks exist, `_act()` emits `TOOL_PRE`, then `TOOL_POST` on success or `TOOL_ERROR` on an
  exception. Successful calls are emitted/logged and their action request/response messages are
  recorded.
- Tool-hook payloads truncate argument and successful-result summaries to 200 characters; verbose
  debug logging truncates its argument preview to 50 characters. These are observability bounds and
  do not truncate the actual invocation or result. The code records no rationale for the exact 200
  and 50 values (`lionagi/operations/act/act.py`).
- With `suppress_errors=True`, a tool exception is logged, recorded as an action response, and
  returned as an `ActionResponseModel` so a later model round can adapt. With false, the original
  exception is re-raised after the error hook and log path.
- `None` responses are removed before enrichment. If none remain, the original result is returned.
- When context providers are registered, every non-`None` action response model is offered to their
  writeback hook before enrichment. This includes governance-denial and suppressed tool-error
  values; the writeback path does not filter by success.
- For an internally generated Pydantic result, the coordinator sets `operative.response_model`,
  updates `action_responses`, and returns the updated model. For a mapping, it mutates
  `result["action_responses"]`. The action phase never replaces the original result with the list of
  tool responses.

**Why this way.** `act()` is the one path that carries authorization, hooks, durable event logging,
message state, suppression policy, and strategy. Keeping it outside adapters ensures API, CLI, and
custom `Middle` implementations cannot accidentally create different tool-governance contracts.
Augmentation preserves the model's original reasoning and fields alongside observed tool results.

## Consequences

- API and CLI adapters share one generated schema, validation policy, authorization path, and
  result-enrichment policy.
- A model sees only the action-request side of the protocol; runtime action responses remain
  distinguishable and are attached after execution.
- Explicit action enablement is required. Merely returning text that resembles a call, or returning
  `action_requests` when no `ActionParam` exists, causes no tool side effect.
- An instruction-embedded sequential strategy is currently shadowed by the facade's truthy
  `"concurrent"` default unless the loose keyword is also supplied. Callers must set
  `action_strategy="sequential"` explicitly at the branch boundary.
- Validation policy can prevent side effects: a caller-model mismatch returns or raises before
  action execution. Conversely, `skip_validation` is a raw-output mode that disables the entire
  outer action phase.
- The coordinator carries parameter normalization, schema materialization, adapter selection,
  validation, action orchestration, and enrichment in one module. This centralizes policy but raises
  the cost of changing its order; every return path must be checked for side effects.
- Contributors adding an adapter need only satisfy `Middle`; they must not duplicate outer action
  execution. Contributors changing generated fields must update both request exclusion and response
  enrichment semantics.
- Reversing D2 is medium-to-high cost because generated provider schemas and returned Pydantic types
  are coupled. Changing D3 or D4 ordering is behaviorally high-risk because it changes whether a
  tool executes. Replacing the default adapter selection in D1 is lower cost if ADR-0021 persistence
  semantics remain intact.
- The existing operation-boundary tests provide high testability (`τ ≈ 0.9` for the area), but they
  do not establish provider-side structured-output reliability or the safety of arbitrary tools.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|-------|------|-------|
| 1 | Deprecate and remove the ignored public `operative` argument, or define and implement a precedence rule that honors it while preserving generated request and response fields. | S | (filled at issue-open time) |
| 2 | Expose `Structure` through the chosen public branch APIs and parameter builders, or mark it internal and remove it from public parameter types; add one end-to-end public-path test. | S | (filled at issue-open time) |
| 3 | Split the raw-result shortcut into explicit validation and post-processing controls, or rename and document `skip_validation` so callers know that it also disables outer action execution. | S | (filled at issue-open time) |
| 4 | Make action-strategy precedence distinguish an omitted facade keyword from explicit `"concurrent"`, then add a public-path test proving that `Instruct.action_strategy` either governs execution or is deliberately removed. | S | (filled at issue-open time) |

## Alternatives considered

### Separate API and CLI operation coordinators

This would allow transport-specific parameter lists and simpler local control flow. It lost because
the generated request/response model, mismatch policy, authorization, hooks, and result enrichment
are transport-independent. Two coordinators would duplicate the highest-risk ordering rules and
could make the same structured request execute tools differently by endpoint family.

### Let each `Middle` execute its own action requests

This would keep the full model/tool conversation inside the adapter and could support provider-
native tool loops. It lost for the outer `operate()` phase because adapters would need to reproduce
authorization, hooks, logging, message recording, suppression, and enrichment. The LNDL adapter may
execute actions inside its bounded language loop, but it still routes those calls through `act()`;
that specialized behavior does not replace the common outer rule.

### Ask the model to return `action_responses`

This would permit one response schema and avoid post-execution model updates. It lost because a
model cannot authoritatively report a result that only the runtime observes. Including the field in
the request would invite fabricated tool outcomes and blur proposed calls with executed effects.

### Maintain unrelated request and result models

This would give complete freedom to shape each side. It lost because the base caller model,
reasoning field, and action-request fields must match on both sides. Independent construction would
duplicate those specs and create drift. `Operative` instead makes the response a materialized
superset of the request.

### Honor a caller-supplied `Operative` with implicit precedence

This could enable advanced schema customization immediately. It lost in the shipped preparer
because no precedence rule answers how caller fields combine with `response_format`, `reason`,
actions, and `field_models`, or who owns `request_exclude`. The code chooses one construction site
by discarding the argument; the delta requires an explicit contract before changing that behavior.

### Let `Instruct.action_strategy` win when the loose keyword is omitted

This would make a self-contained `Instruct` govern both action enablement and scheduling. It would
also preserve the apparent fallback order in `prepare_operate_kw()`. It did not ship at the public
boundary because `Branch.operate(action_strategy="concurrent")` always forwards a truthy default, so
the preparer cannot observe omission. Correcting it requires an omission sentinel or a single
authoritative strategy field; silently changing precedence would alter execution ordering for
existing calls.

### Treat `skip_validation` as “skip only the final instance check”

This interpretation would preserve parsing or tools while relaxing the caller-model assertion. It
lost against the current early return: the flag is passed into the adapter and then checked
immediately after it. Changing it would cause tool side effects for calls that are raw-only today,
so it requires a named behavioral decision rather than documentation sleight of hand.

### Execute actions before outer model mismatch handling

This could salvage valid action requests from an otherwise imperfect structured response. It lost
because a tool side effect would occur before the caller's declared validation policy was honored.
The current order is fail-before-effect for caller-model mismatches.

### Return tool responses instead of enriching the original result

This would simplify the return type after actions. It lost because it discards the model's original
fields and reasoning and changes the response shape depending on whether any tool ran. Enrichment
keeps one logical result and makes action outcomes an explicit field.

## Notes

Primary implementation anchors are `lionagi/session/branch.py`,
`lionagi/operations/operate/operate.py`, `lionagi/operations/operate/step.py`,
`lionagi/operations/operate/operative.py`, `lionagi/operations/fields.py`,
`lionagi/operations/_defaults.py`, `lionagi/operations/schema/structure.py`, and
`lionagi/operations/act/act.py`. Focused behavioral anchors live in
`tests/operations/test_operate.py`, `tests/operations/operate/`, and
`tests/operations/test_act.py`.
