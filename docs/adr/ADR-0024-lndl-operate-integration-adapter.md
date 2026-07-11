# ADR-0024: LNDL operate integration adapter

- **Status**: Accepted
- **Kind**: Retrospective
- **Area**: operations
- **Date**: 2026-07-09
- **Relations**: supersedes v0-0087; extends ADR-0021 and ADR-0022

## Context

LNDL is a fenced, tag-based language for assembling structured output from declared values and tool
calls. The operations integration is deliberately smaller than the language package: it is an
opt-in `Middle` passed to `Branch.operate()`. Five problems determine the shipped adapter.

**P1 — LNDL needs the existing branch transport and operation policy, not a second runtime.** API
models already use `communicate()`, CLI models use `run_and_collect()`, and tool requests already use
`act()`. A separate LNDL runtime would duplicate model selection, message persistence,
authorization, hooks, logging, and action-response messages.

**P2 — Provider-native tools and structured-output rendering conflict with the LNDL protocol.** LNDL
expects free text containing fenced `<lvar>`, `<lact>`, and `OUT{}` constructs. The provider must be
told the exact target fields, but it must not be asked to satisfy a native response schema or emit
native tool calls during each inner round. The adapter therefore renders a target summary into
guidance and strips both native surfaces (`lionagi/operations/lndl_middle/lndl_middle.py`).

**P3 — Tool-dependent structured output may require more than one exchange.** A first response can
declare actions without a final `OUT{}`; their results become branch messages and alias values for a
later round. Syntax, assembly, or target validation can also fail in a repairable way. The adapter
owns a bounded loop and carries the latest repair diagnostic forward.

**P4 — The implementation has two action-commit rules.** In a response with `OUT{}`, only action
calls reachable from the assembled output execute. In a response without `OUT{}`, every declared
action call executes before continuing. The latter enables information-gathering rounds but can
produce a side effect before any final output commits. It also conflicts with the superseded
record's output-gated statement; this retrospective ADR records the code as truth.

**P5 — Language vocabulary and adapter failure behavior are not aligned.** The LNDL package defines
`Success`, `Continue`, `Retry`, `Exhausted`, and `Failed`. The adapter directly constructs only the
first three, raises `LNDLError` on budget exhaustion, and lets non-LNDL exceptions propagate. The
language assembler accepts a scratchpad, and the prompt promises cross-round `note.X`, but the
operations adapter does not pass or accumulate that state.

| Concern | Decision |
|---------|----------|
| Integration and budget | D1: LNDL is an opt-in `Middle` with a configurable bounded round loop, default 3. |
| Inner request shaping | D2: round one carries the LNDL prompt and target field summary; all rounds disable native tools and response formatting. |
| Round classification | D3: fenced text is normalized, parsed, and assembled into `Continue`, `Retry`, or a `Success` candidate with exact repair behavior. |
| Action timing and bridge | D4: continuation rounds execute all declared actions; success rounds execute only output-reachable actions, always through `act()`. |
| Completion and failure | D5: resolved output is optionally target-validated; exhaustion raises and unexpected exceptions propagate. |

This ADR deliberately does **not** decide:

- A replacement LNDL grammar, lexer, parser, or general assembler API. The adapter consumes those
  language-package contracts but does not own them.
- A new default for ordinary `operate()` calls. LNDL remains opt-in and does not change ADR-0022's
  default API/CLI adapter selection.
- Expansion of the tool registry or dotted tool aliases. The adapter executes whichever tool names
  the branch advertises.
- Quantitative model-quality or cost gates. No measurement policy is asserted as implemented by
  this retrospective operations record.
- Cross-round `note.X` persistence as a shipped feature. The prompt/package seam exists, but the
  adapter does not thread scratchpad state; the delta below requires an explicit resolution.

## Decision

### D1 — LNDL is an opt-in, configurable `Middle`

The public module and callable contracts are:

```text
lionagi/
├── operations/lndl_middle/
│   ├── __init__.py          # public exports
│   └── lndl_middle.py       # adapter, round classifier, action bridge
└── lndl/
    ├── extract.py           # fenced-block extraction
    ├── normalize.py         # conservative syntax repair
    ├── lexer.py
    ├── parser.py
    ├── assembler.py         # target assembly and ActionCall placeholders
    ├── round_outcome.py     # outcome algebra
    ├── prompt.py
    └── errors.py
```

```python
DEFAULT_ROUND_BUDGET = 3

def build_lndl_middle(round_budget: int = DEFAULT_ROUND_BUDGET): ...

lndl_middle = build_lndl_middle()

# The returned callable satisfies Middle:
async def _lndl_middle(
    branch: Branch,
    instruction: JsonValue | Instruction,
    chat_param: ChatParam,
    parse_param: ParseParam | None = None,
    clear_messages: bool = False,
    skip_validation: bool = False,
) -> Any: ...
```

Callers select the ready-made or configured adapter explicitly:

```python
from lionagi.operations.lndl_middle import build_lndl_middle, lndl_middle

result = await branch.operate(
    instruction="Produce the requested structured result",
    response_format=TargetModel,
    middle=lndl_middle,
)

one_round = build_lndl_middle(round_budget=1)
```

Exact integration and budget semantics:

- `lionagi.operations.lndl_middle` exports exactly `DEFAULT_ROUND_BUDGET`,
  `build_lndl_middle`, and `lndl_middle`.
- Ordinary `operate()` behavior is unchanged unless the caller supplies this `Middle`.
- One adapter invocation may own several recorded inner exchanges. It clears branch messages once at
  entry when `clear_messages=True`; individual inner calls are not asked to clear again.
- Rounds are `range(1, round_budget + 1)`. A budget of 1 permits one provider exchange. Zero or a
  negative value performs no exchange and reaches exhaustion immediately; the builder does not
  validate positivity.
- A non-integer budget is also not validated by the builder; iteration raises `TypeError` before the
  first provider exchange.
- The default 3 is a hard bound on provider exchanges per adapter invocation, not a retry count per
  parse step or tool. The code records no experiment or other rationale for exactly 3; it is an
  inherited compromise between repair opportunity and bounded cost.
- `parse_param` is accepted for `Middle` conformance but is not read. The adapter validates through
  the target copied from `chat_param.response_format`.

**Why this way.** A `Middle` is the existing substitution seam for one logical exchange under
ADR-0021. The closure returned by `build_lndl_middle()` captures one budget without adding global
configuration or changing the `Middle` signature. Opt-in selection contains the behavioral and
cost difference to calls that ask for it.

### D2 — The adapter renders LNDL guidance and strips native provider contracts

Before round one, the adapter derives:

```python
target = chat_param.response_format
base_guidance = chat_param.guidance or ""
guidance_parts = [get_lndl_system_prompt()]

target_spec = _render_target_spec(target)
if target_spec:
    guidance_parts.append(target_spec)
if base_guidance:
    guidance_parts.append(base_guidance)

lndl_guidance = "\n\n".join(guidance_parts)
stripped_chat_param = chat_param.with_updates(
    tool_schemas=[],
    response_format=None,
)
```

The target summary format is derived from Pydantic `model_fields`:

```text
Specs: answer(str), detail(str, optional)
Specs: findings(list[Finding: name, score]), scores(dict[str, float])
```

Exact prompt and transport semantics:

- `target` is the effective response type supplied by ADR-0022. When `operate()` generated an
  action/reason response model, the adapter sees that generated type, not merely the caller's base
  type.
- A target without `model_fields` produces no additional `Specs:` line. Field order follows the
  target model declaration order.
- Optional single types are unwrapped and marked `", optional"`. Lists, mappings, and nested
  Pydantic models render recursively; nested models are summarized by model name and field names.
- Round-one guidance is the LNDL system prompt, target summary when available, then the caller's
  original guidance. Later rounds use `stripped_chat_param`, which retains the caller's original
  guidance but does not repeat the injected LNDL prompt/target summary.
- Although `ChatParam.guidance` accepts `JsonValue`, construction joins guidance parts as strings.
  A truthy non-string guidance value therefore raises `TypeError` before round one. No coercion or
  serialization is applied by this adapter.
- Every inner call receives `tool_schemas=[]` and `response_format=None`. LNDL tags remain free text;
  the provider transport neither invokes native tools nor performs native structured-output
  rendering.
- Round one sends the caller instruction unchanged. Later rounds send `"Round N of M."`; when the
  previous round supplied a repair error, the notice includes that error and asks for repair.
- `RunParam` or a CLI-backed branch model selects `run_and_collect`; otherwise a `ChatParam` selects
  `communicate`. Both are called with `skip_validation=True`, so the inner result is raw text while
  retaining ADR-0021 message persistence.

**Why this way.** Once native `response_format` is removed, the model still needs the exact field
names to construct `OUT{}`. Rendering those fields in LNDL's own vocabulary preserves that contract.
Disabling native tools prevents the provider from executing a call outside D4's parser/assembler
reachability rule and common `act()` path.

### D3 — Each raw response classifies into continue, repair, or a success candidate

The round outcome algebra in the language package is:

```python
@dataclass(slots=True, frozen=True)
class Success:
    output: Any

@dataclass(slots=True, frozen=True)
class Continue:
    notes_committed: tuple[str, ...] = ()

@dataclass(slots=True, frozen=True)
class Retry:
    error: str
    note_keys: tuple[str, ...] = ()

@dataclass(slots=True, frozen=True)
class Exhausted:
    last_error: str | None = None

@dataclass(slots=True, frozen=True)
class Failed:
    error: BaseException

RoundOutcome = Success | Continue | Retry | Exhausted | Failed
```

The operations adapter's classifier has the narrower contract:

```python
def _classify_round(
    text: str,
    target: Any,
    action_results: dict[str, Any],
) -> tuple[RoundOutcome, list[ActionCall], dict[str, Any] | None]: ...
```

Its actual state transition is:

```text
no fenced lndl block
    └─ Continue, no actions

fenced block(s)
    └─ normalize → lex → parse
       ├─ LNDLError ───────────────────────────────> Retry(error)
       ├─ no OUT{} ─> build every lact ActionCall
       │              ├─ invalid call ─────────────> Retry(error)
       │              └─ valid calls ──────────────> Continue(pending=all)
       └─ OUT{} ─────> assemble target-shaped dict
                      ├─ LNDLError ─────────────────> Retry(error)
                      └─ Success(output=dict,
                                 pending=reachable actions)
```

Exact parsing and assembly semantics:

- Only fenced blocks whose language tag is case-insensitively `lndl` are extracted. Backtick and
  tilde fences are accepted by the extractor. Multiple blocks are joined in source order before
  normalization.
- Empty-string output contains no fenced block and becomes `Continue`. CLI collection with no
  assistant text returns `None`, however; the classifier passes that value to the regular-expression
  extractor and raises `TypeError`. The adapter does not currently normalize no-text output across
  endpoint families.
- Unfenced tags are ignored by the adapter because extraction returns no blocks; the round becomes a
  `Continue`, not a syntax error.
- Normalization repairs supported curly-brace tags, XML-style `name=` attributes, an opening tag
  missing `>` when a parenthesized call is recognizable, and `Note.` casing in tag declarations.
  It does not promise arbitrary fuzzy grammar repair.
- The lexer and parser produce a `Program` containing lvars, lacts, and an optional out block.
- With no `OUT{}`, each declared lact is parsed into an `ActionCall`. A malformed function call is an
  `InvalidConstructorError`, a subclass of `LNDLError`, and therefore becomes `Retry` rather than
  escaping.
- With `OUT{}`, `assemble()` resolves only listed aliases, accepts historical action results by
  alias, constructs scalar/list/mapping/nested-model values, and checks that all required target
  fields appear. Missing aliases or required fields become `Retry` diagnostics.
- Recursive bracket groups inside `OUT{}` are capped at nesting depth 32 by the parser. Exceeding
  the cap raises a language `ParseError` and therefore becomes a repairable `Retry` in the adapter.
  The cap bounds recursive descent; the code records no rationale for exactly 32
  (`lionagi/lndl/parser.py`).
- A lact referenced from `OUT{}` becomes an `ActionCall` placeholder when no result exists yet.
  `collect_actions()` recursively finds placeholders in mappings and lists.
- `_classify_round()` catches `LNDLError` only. A non-language exception from normalization,
  parsing, assembly, or surrounding code propagates under D5.
- The assembler can accept `scratchpad=` and can collect `note.X` declarations. This adapter calls
  `assemble(program, target, action_results=action_results)` without a scratchpad and does not call
  `collect_notes()`. Consequently `note.X` values are not retained between adapter rounds despite
  prompt/package vocabulary that describes them.

**Why this way.** Language-shaped errors are actionable model feedback, so they consume another
bounded round. Missing LNDL text is treated as continued reasoning because the model may need a
second turn. Unexpected implementation/transport errors are not assumed repairable and therefore
are not flattened into a `Retry` string.

### D4 — Action execution is round-shape dependent and always goes through `act()`

The placeholder and bridge contracts are:

```python
@dataclass(slots=True, frozen=True)
class ActionCall:
    name: str
    function: str
    arguments: dict[str, Any]
    raw_call: str

async def _bridge_action_calls(
    branch: Branch,
    calls: list[ActionCall],
) -> dict[str, Any]: ...

_ACTION_PARAM = ActionParam(
    action_call_params=get_default_action_call(),
    tools=None,
    strategy="concurrent",
    suppress_errors=True,
    verbose_action=False,
)
```

The bridge transforms every placeholder into the ordinary branch request payload:

```json
{
  "function": "tool_name",
  "arguments": {"literal_argument": "value"}
}
```

Exact action semantics:

- A `Continue` round with no `OUT{}` executes every valid lact declared in that round. No reachability
  filter exists because there is no final output graph to traverse.
- A `Success` candidate executes only `ActionCall` placeholders reachable from the assembled
  `OUT{}` value. Unreferenced lacts in the same response do not execute.
- Calls are converted with `branch.msgs.create_action_request()` and sent together to the normal
  `act()` dispatcher with concurrent strategy and suppressed errors.
- Authorization denial, hooks, event logging, action request/response messages, and exception-as-tool-
  result behavior are inherited unchanged from ADR-0022 D4. The adapter does not call the action
  manager directly.
- Returned responses are zipped strictly to input calls and stored as `alias -> response.output`.
  A cardinality mismatch raises rather than silently associating the wrong result.
- `action_results` lives for one adapter invocation. A later `OUT{}` can reference an alias executed
  in an earlier continuation round without redeclaring the lact.
- Alias conflict behavior is last-write for continuation rounds: executing a no-`OUT{}` lact under
  an alias already present in `action_results` overwrites the earlier value. In contrast, an
  `OUT{}` round that redeclares an already-resolved lact alias reads the historical result during
  assembly, creates no `ActionCall` placeholder for the new body, and therefore does not execute
  the redeclared call. Aliases are identities for one adapter invocation, not versioned calls.
- After success-round actions execute, `replace_actions()` recursively substitutes results by alias.
  A placeholder with no corresponding result is retained and may fail target validation.
- Tool failures are values because suppression is enabled. They can be read from branch history and
  referenced/adapted by a later round instead of aborting the LNDL loop.
- `skip_validation=True` does **not** disable this inner LNDL action bridge. It disables final target
  validation under D5 and ADR-0022's outer action phase; any lact selected by D4 still executes.

**Why this way.** Continuation rounds exist to obtain information required for a later output, so the
current code eagerly runs their declarations. Output-bearing rounds have an explicit commit graph,
so only reachable calls run. Both policies retain the common governance path. The asymmetry is
intentional as-built behavior but remains an unresolved product/design choice because eager
continuation calls may have side effects.

### D5 — The first valid resolved output returns; exhaustion raises

After D4 resolves a `Success` candidate, the adapter applies:

```python
if skip_validation or target is None or not hasattr(target, "model_validate"):
    return assembled

try:
    return target.model_validate(assembled)
except ValidationError as e:
    last_error = str(e)
    continue
```

Exact completion and failure semantics:

- A success candidate with no target, a non-Pydantic target, or `skip_validation=True` returns the
  assembled mapping after action replacement.
- Otherwise `target.model_validate()` is authoritative. A Pydantic `ValidationError` becomes the
  next round's repair notice; the current round does not return a partially valid model.
- The adapter returns on the first successfully validated candidate. Unused budget is not consumed.
- `Retry` stores its error as `last_error` and starts the next round. `Continue` runs any pending
  actions and starts the next round without setting a new error, so an older repair diagnostic can
  remain the exhaustion detail.
- When the loop ends without return, the adapter raises `LNDLError`. The message names the configured
  budget and includes `last_error`, or states that no `OUT{}` was produced when no repair error
  exists.
- The adapter never returns `Exhausted` or `Failed`, and never directly constructs those variants.
  They remain broader language-package vocabulary rather than the external `Middle` result.
- A transport exception, action-bridge exception not suppressed by `act()`, programmer error, or
  any other non-`LNDLError` propagates unchanged. It is not converted to `Failed`.
- The pre-round `TypeError` paths for non-string guidance, non-integer budgets, and CLI no-text
  results are instances of that propagation rule; none consumes the remaining repair budget as a
  typed `Retry`.
- `Branch.operate()` then applies ADR-0022's outer validation to the adapter result unless the same
  `skip_validation` flag caused the raw short-circuit.

**Why this way.** A caller requesting a Pydantic result must not receive a bare error string or
`None` after repair budget exhaustion. Raising makes the absence of a value explicit. Returning the
first valid model bounds cost and preserves the `Middle` one-result contract, while unexpected
failures retain their original exception type for diagnosis.

## Consequences

- LNDL reuses established API/CLI transport, branch message persistence, authorization, hooks,
  logging, and action response messages.
- Non-LNDL `operate()` calls are unaffected; the extra prompt, parsing, actions, and round budget
  occur only when the adapter is selected.
- The target's exact field names remain visible after native response formatting is disabled.
- One logical operation may incur up to the configured number of provider exchanges and multiple
  tool batches. Cost and latency are therefore bounded but higher than a canonical one-turn adapter.
- Language errors are repairable; transport and implementation failures remain distinguishable.
- A continuation round can cause tool side effects before any `OUT{}` commits them. Tool authors and
  callers cannot infer output-gated execution from the prompt's single-round examples.
- Cross-round action results work by alias and chat history, but cross-round `note.X` values do not.
  The prompt currently promises more state than the adapter supplies.
- Alias reuse is asymmetric: a continuation round overwrites the stored result, while an output
  round's redeclared alias resolves to the historical value without running the new call.
- API empty text consumes a continuation round, while CLI no-text output raises `TypeError`; callers
  do not yet receive endpoint-neutral empty-output semantics.
- The public outcome vocabulary overstates what callers observe: exhaustion and unexpected failure
  are exceptions, not `RoundOutcome` values.
- Reversing D1 or D2 is low-to-medium cost because the adapter is opt-in. Changing D4 is behaviorally
  high-risk because it changes tool side effects. Changing D5 from exceptions to outcome values is a
  public return-contract migration.
- Focused classifier, dispatch, prompt, action-hook, exhaustion, and end-to-end operate tests support
  high testability, but do not establish model adherence or safe behavior for arbitrary side-effecting
  tools.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|-------|------|-------|
| 1 | Choose one LNDL action-commit rule for no-`OUT{}` rounds, then align the system prompt, adapter, architecture record, and tests so eager continuation actions or output-gated actions are stated consistently. | M | #2024 |
| 2 | Decide whether `note.X` is supported across adapter rounds; either thread and test bounded scratchpad state or remove the cross-round promise from the prompt and public language surface. | M | (filled at issue-open time) |
| 3 | Align the adapter's public failure contract with the round-outcome vocabulary by defining whether exhaustion and unexpected failures are typed outcomes or raised exceptions, and add end-to-end tests for the chosen policy. | S | #2025 |
| 4 | Normalize or explicitly reject non-string `JsonValue` guidance before prompt joining, and add an end-to-end test that fixes the selected wire rendering. | S | (filled at issue-open time) |
| 5 | Define one endpoint-neutral no-assistant-output transition (`Continue`, `Retry`, or terminal failure), normalize API and CLI adapters to it, and test both families. | S | (filled at issue-open time) |

## Alternatives considered

### Build a dedicated LNDL runtime

This would let the language own transport, state, tools, retries, and results end to end. It lost
because those capabilities already exist on `Branch`; recreating them would produce a second model
manager, message history, authorization path, hook path, and error policy. A `Middle` supplies the
needed bounded-loop substitution without duplicating the runtime.

### Make LNDL the default `operate()` adapter

This would give every structured request the same language and repair loop. It lost because it adds
prompt tokens, free-text parsing, possible tool rounds, and different failure/cost semantics to
callers that currently use native structured output successfully. Opt-in selection contains that
trade-off.

### Keep native provider tools and response formatting enabled inside rounds

This could exploit provider-native function calling and JSON schema enforcement. It lost because a
native tool call bypasses LNDL's `OUT{}` reachability rule, while a native response format asks the
provider to emit JSON rather than fenced LNDL. Running both protocols at once creates two competing
sources of action and schema truth.

### Run exactly one LNDL round

This would make cost and side effects easy to bound and preserve a literal one-turn `Middle`. It lost
because a model cannot incorporate newly executed tool results into an `OUT{}` in the same response.
The configurable budget retains a one-round option for callers that need it.

### Execute only `OUT{}`-reachable actions in every round

This would make the commit rule uniform and avoid side effects from an uncommitted continuation.
It lost in the current implementation because no-`OUT{}` rounds are the mechanism for gathering
tool results before the final structured response. It remains viable, but adopting it requires a
different explicit multi-round design, as recorded in delta 1.

### Execute every declared action even when `OUT{}` exists

This would make all rounds uniformly eager and simplify classification. It lost because output-
bearing responses provide an explicit reachability/commit set; running scratch lacts would execute
work the model deliberately omitted from its result.

### Invoke the action manager directly

This would remove `ActionRequest` construction and part of the outer operation dependency. It lost
because it would bypass `Branch.authorize`, tool hooks, event logging, and action messages. Routing
through `act()` is the governance invariant carried from ADR-0022.

### Return `Exhausted` and `Failed` values from the `Middle`

This would align the adapter with the full `RoundOutcome` algebra and make failure matching
explicit. It lost in shipped behavior because `operate()` and structured callers expect the target
result or an exception, not an outcome wrapper. The current raise policy prevents a bare terminal
outcome from passing outer validation as if it were the requested value.

### Version action identities by round instead of alias

This would permit the same alias to denote a fresh call in a later round and retain both results.
It lost to the simpler invocation-local `dict[alias, result]` bridge, which lets a later `OUT{}`
reference an earlier tool result without another syntax. The cost is the current asymmetric
collision rule: continuation execution overwrites, while output assembly prefers an existing value
and suppresses the redeclared call.

### Thread unbounded `note.X` scratchpad state automatically

This would fulfill the prompt's current promise. It was not taken because the adapter has no bound,
retention, collision, or serialization contract for that state, and currently carries only action
results. The alternative is deferred pending the explicit implement-or-remove decision in delta 2.

### Leave the round budget unbounded

This would maximize the opportunity for model repair. It lost because malformed output or repeated
continuations could consume provider calls indefinitely. A closure-captured finite budget makes the
failure and cost boundary visible, even though the exact default of 3 lacks a recorded measurement
rationale.

### Require a positive integer budget at adapter construction

This would fail configuration before prompt construction and give zero, negative, and non-integer
budgets one clear error. It did not ship: the closure stores the value unchanged and lets `range()`
or exhaustion define behavior. The permissive shape keeps the builder small but produces two
different failure classes, so callers should pass a positive integer until the contract is tightened.

## Notes

The superseded record included broader language-package, scratchpad, and measurement claims. This
ADR carries forward only the implemented `operate()` seam and its verified action/failure behavior.

Primary implementation anchors are `lionagi/operations/lndl_middle/__init__.py`,
`lionagi/operations/lndl_middle/lndl_middle.py`, `lionagi/lndl/extract.py`,
`lionagi/lndl/normalize.py`, `lionagi/lndl/lexer.py`, `lionagi/lndl/parser.py`,
`lionagi/lndl/assembler.py`, `lionagi/lndl/types.py`, `lionagi/lndl/round_outcome.py`,
`lionagi/lndl/prompt.py`, and `lionagi/operations/act/act.py`. Focused behavioral anchors live in
`tests/operations/test_lndl_middle.py`.
