# ADR-0087: LNDL operate() Seam, Scratchpad-as-Tool, and the Measurement Gate

**Status**: Proposed
**Date**: 2026-07-03

## Context

The `lionagi/lndl` package (lexer, parser, assembler, round-outcome ADT, diagnostics,
system-prompt contract) landed as a standalone, seam-independent port in #1629. It is
dormant: the only production entry point is `_extract_lndl` in `operations/parse/parse.py`,
which returns the raw first fenced block without ever running the lexer/parser/assembler.
Nothing constructs a `RoundOutcome`, nothing injects `LNDL_SYSTEM_PROMPT`, and the
`ActionCall` placeholders the assembler produces have no bridge to the action layer
(`ActionManager.match_tool` consumes `ActionRequest | BaseModel | dict`, never `ActionCall`).
Half-built plumbing exists but has zero non-test callers: `StructureFormat.LNDL`,
`CustomParser`/`CustomRenderer` protocols, and `prepare_messages_for_chat`'s
`system_prefix` / `round_notifications` / `scratchpad` parameters.

Separately, the package carries design debt the port intentionally deferred: a `note.X`
magic namespace baked into the prompt contract, parser, and assembler; six exception
classes that are exported as public API but never raised; a number lexer that cannot read
scientific notation; and a grammar that exists only implicitly across `lexer.py`,
`parser.py`, and `normalize.py`. This ADR decides how LNDL reaches `operate()`, resolves
each debt item, and — critically — pre-registers the measurement gate that decides whether
deeper investment (formal grammar work, expanded action vocabulary) proceeds at all.

## Decision

LNDL integrates as a **Middle** on the existing `branch.operate(middle=...)` seam. No new
runtime, no second execution engine, no changes to `Branch`'s manager architecture. One new
module, `operations/lndl_middle/` (final name at implementation), owns the full round loop:

```text
inject prompt → chat → normalize → lex → parse → assemble
   → ActionCall→ActionRequest bridge → act() → replace_actions/revalidate
   → classify RoundOutcome → return | loop | repair-reprompt
```

The decision decomposes into six parts, each independently reviewable.

### 1. The seam: a Middle over operate()

- A callable satisfying the existing `Middle` protocol (`operations/types.py`) advances the
  branch one LNDL round per invocation of the inner chat, looping internally up to a round
  budget (default 3).
- Prompt injection uses `get_lndl_system_prompt()` as a system prefix. The dormant
  `prepare_messages_for_chat(system_prefix=...)` path is the intended wire; if wiring it
  into `MessageManager.to_chat_msgs` proves invasive, the Middle prepends the contract to
  the instruction context instead — an implementation detail, not a contract change.
- The `ActionCall → ActionRequest` bridge is a pure translation function in the Middle:
  `ActionRequest(function=qualified_name, arguments=call.arguments)`, executed through the
  branch's normal `act()` path so permission policies and hooks apply unchanged. Results
  feed back through `replace_actions` / `revalidate_with_action_results`.
- The Middle is opt-in per call (`branch.operate(..., middle=lndl_middle)`) and via a
  convenience flag once stable. Nothing changes for callers who don't ask for it.

### 2. Round outcomes and repair semantics

`RoundOutcome` (`Success | Continue | Retry | Exhausted | Failed`) becomes the *only*
failure/repair vocabulary. The Middle classifies every round:

| Condition | Outcome | Next |
|---|---|---|
| OUT{} parsed, assembled, validated | `Success(output)` | return |
| no OUT{} block | `Continue` | next round (tool results already in history) |
| parse/assemble/validation failure | `Retry(error=...)` | re-prompt with the typed error |
| round budget exhausted | `Exhausted(last_error)` | raise/return per caller policy |
| non-LNDL exception | `Failed(error)` | re-raise |

The six declared-but-never-raised exception classes in `errors.py` are adjudicated:

- **Wired in** (raised by assembler/Middle, stringified into `Retry.error` so the model
  gets a precise repair instruction): `MissingLvarError` (OUT references an undeclared
  alias — today silently skipped), `MissingFieldError` (required schema field absent from
  OUT — today surfaces only as a downstream pydantic error), `InvalidConstructorError`
  (lact body isn't a parseable call — today swallowed to `(False, None)`).
- **Kept, documented as pydantic-deferred**: `TypeMismatchError`. The assembler's
  best-effort coercion plus pydantic's own validation errors already cover it; the class
  stays as a wrapper the Middle may use when sharpening a repair message.
- **Retired**: `MissingOutBlockError` (the condition is a *state*, `Continue`, not an
  error) and `AmbiguousMatchError` (a fuzzy-matcher concern; deterministic `normalize.py`
  replaced the fuzzy matcher and no code path can produce a tie). Both are public API —
  removal lands in the next minor with a CHANGELOG entry and a deprecation note in the
  current release; no re-export shims.

### 3. One grammar, named desugar pass, prompt as checked projection

- A **core EBNF** is committed into the package (`lionagi/lndl/GRAMMAR.md`), reconstructed
  from the de-facto grammar: token vocabulary, the four lvar/lact header forms, OUT-block
  entry forms (explicit spec, scalar literals, bare-alias shortcut, anonymous groups,
  nested lists to depth 32, dotted refs), and the mode-sensitivity rules (strings and
  negative numbers lex only inside OUT; lvar/lact bodies are opaque text recovered by
  source-regex, not token-driven).
- `normalize.py` is named for what it is: a **desugar/repair pass, explicitly outside the
  grammar's soundness story**. Its rewrite rules (fenced-block preference, missing-`>`
  repair, curly→angle tags, XML-attribute stripping with `name=` promotion, `Note.`→`note.`
  casing) are enumerated in GRAMMAR.md as input-repair transformations with no claim that
  repaired input is grammar-equivalent to what the model "meant". No formal proof of the
  forgiving parser is attempted or implied.
- `prompt.py` becomes a **CI-checked projection of the grammar**: a test extracts every
  syntax example taught in `LNDL_SYSTEM_PROMPT` and asserts each parses through the real
  lexer/parser. The existing SHA-256 prompt snapshot test stays; the projection test makes
  prompt/grammar drift a red build instead of a silent divergence.

### 4. Number-lexer correctness (pre-seam bugfix)

`read_number` (`lexer.py`) accepts only digits and `.`. `OUT{x: 6.022e23}` therefore
truncates to `6.022` and the stray `e23` self-registers as a phantom OUT field via the
bare-alias shortcut — silent corruption, no error. The fix extends `read_number` to
scientific notation (`e`/`E`, optional sign) and rejects multi-dot literals on all paths
(today `1.2.3` errors on the explicit-spec path but is mis-handled on the shortcut path).
This ships as its own small PR with lexer + parser tests before the seam work; the seam
must not build on a lexer that silently corrupts numeric output.

### 5. Scratchpad as a tool; note.X retired

The `note.X` magic namespace is removed from the prompt contract and the language surface.
Cross-round persistence becomes an ordinary tool:

- A `ScratchpadTool` (module under `lionagi/tools/`) registered on the branch like any
  other tool: `write(key, value)`, `read(key)`, `list()`, `delete(key)`, with hard caps
  (key ≤ 128 bytes, ≤ 200 keys, ≤ 200 KB total) and string-only values. Storage is
  **per-Branch** — the factory creates one instance per branch registration; no shared
  module-level state.
- The invariant becomes uniform: **only lacts referenced in OUT{} execute**. A scratchpad
  write persists if and only if its lact alias appears in OUT{} — the same rule as every
  other action, enforced at the single point where the Middle selects
  `lacts_to_execute = {a for a in lacts if a in out_refs}`. Everything not referenced is
  scratch thinking, discarded.
- lvars remain **untrusted data by default**: they bind values into the output structure
  and never execute; only the typed action registry (below) reaches the action layer.
- Removal surface (all in one PR): `NOTE_NAMESPACE`, `collect_notes`, `_is_note_ref`,
  `_note_key`, the assembler's scratchpad-resolution branches and `scratchpad` parameter
  threading, the parser's `note.X` dotted-ref special cases, `Continue.notes_committed` /
  `Retry.note_keys`, the ~41-line NOTE NAMESPACE prompt section (~370 tokens, ~16% of the
  prompt tax), and the top-level re-exports. A "Drafting — declare several, commit the
  best" example replaces the note.X multi-round example in the prompt.

### 6. Typed action registry, deterministic ops only

The vocabulary of callable operations inside `<lact>` is a **closed, versioned registry**:
explicit registration with signature validation (name, typed parameters, return type), no
open-ended dispatch. The initial registry admits only deterministic harness-side
operations (similarity/anchor/relevance/projection-class utilities and registered branch
tools). **Judgment-flavored operations** (synthesize, contradicts, confidence, complexity —
anything whose implementation would itself call a model) are explicitly **deferred**: they
are not in the registry, and adding them requires a new ADR. Registry versioning rides the
package version; a program written against registry vN parses under vN+1 or fails with a
typed registration error, never a silent behavior change.

## The measurement gate (pre-registered)

Deeper LNDL investment is gated on one pilot measurement, pre-registered here **before**
the seam is built so the numbers cannot be fitted to the outcome.

**Hypothesis.** One LNDL generation with inline lvars and OUT{}-gated actions replaces a
multi-call structured-reasoning chain at a lower total token cost per bound decision,
without losing output validity.

**Pilot leg.** A ReAct-driven analysis leg (interpret → initial analysis → up to 3
extension rounds → final answer; 3–6 model calls, ~7 bound decisions: 3 continue/stop
gates, 3 round analyses, 1 final answer), run via `li o flow` on an **API-provider chat
model**. API-provider is a hard requirement: CLI-subprocess providers do not persist
per-message token usage into branch manifests (verified empirically: 297 `play` rows and
28 `flow` rows in the state DB all carry zero token columns; only API-model rows populate),
so a CLI-provider pilot cannot produce a baseline. This instrumentation gap is tracked as
its own issue and does not block the pilot.

**Metric.** `tokens_per_decision = (input_tokens + output_tokens summed across the leg's
model calls, from branch-manifest AssistantResponse usage) / decisions_bound`. The LNDL
arm's input tokens **include the full system-prompt tax on every round** — measured today
at 8,979 chars ≈ **2,245 tokens** (≈ 1,875 after the note.X retirement in §5). No
accounting that amortizes or excludes the prompt tax is admissible.

**Arms.** Same task, same model, same context, N ≥ 10 runs per arm per model:
baseline = the existing ReAct chain with JSON structured output; treatment = one
LNDL-Middle `operate()` producing the same decision set.

**Fleet portability.** The gate is evaluated **per model** on at least two API models from
different providers (one Anthropic tier, one OpenAI tier). LNDL syntax adherence is known
to vary across model families; a single-model pass does not clear the gate.

**Pass / kill criteria (pre-registered).**

- **PASS**: treatment `tokens_per_decision` ≤ **0.8×** baseline on every tested model, AND
  treatment invalid-output rate (rounds ending `Exhausted`/`Failed`) ≤ baseline
  parse-failure rate.
- **KILL**: treatment > **1.0×** baseline on all tested models. Deeper investment stops;
  LNDL re-scopes its pitch to robustness only (structured-output parse-failure reduction —
  prior internal measurements put strict-JSON parse-fail at roughly 40–60% for weaker
  models vs <5% for LNDL's forgiving pipeline), and any further work requires a new ADR.
- **REVIEW**: every outcome that is neither PASS nor KILL (including mixed results, e.g.
  one model well over 1.0× while another clears 0.8×) goes to review with the raw per-run
  table; no automatic proceed. The three outcomes partition the result space — there is no
  unassigned region.

Sections 1–5 (seam, repair semantics, grammar doc, lexer fix, scratchpad-as-tool) are
worth landing on robustness grounds alone and are not gated. Section 6's registry
*expansion* and all further language-surface investment are gated.

## Consequences

**Positive**

- LNDL becomes reachable through the one blessed seam (`operate(middle=...)`) with zero
  impact on non-LNDL callers; the action layer, permission hooks, and logging all apply
  unchanged because execution goes through the normal `act()` path.
- The language surface shrinks and regularizes: one grammar file, one desugar pass with
  enumerated rules, one persistence mechanism (tools), one commit rule (OUT{}), one
  failure vocabulary (`RoundOutcome`), a ~16% smaller prompt tax.
- The dead-API problem is resolved rather than inherited: three error classes gain real
  semantics, two are removed, one is documented.
- Investment decisions downstream of this ADR are empirical, not aesthetic — the gate's
  numbers are fixed before any implementation exists to flatter them.

**Negative**

- Removing `MissingOutBlockError`, `AmbiguousMatchError`, `NOTE_NAMESPACE`, and
  `collect_notes` is an API-visible break (all are re-exported at top level and pinned by
  public-API tests); it costs a minor-version deprecation cycle.
- Every LNDL round pays ~1.9–2.2k prompt tokens; if the gate fails, the seam still carries
  that cost for robustness-only use cases.
- The per-Branch scratchpad is in-process state: it does not survive process restarts and
  is not shared across branches by design; anything durable must go through a real tool.
- The pilot depends on API-model legs; conclusions do not automatically transfer to
  CLI-subprocess providers until their usage persistence gap is closed.

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|--------------|
| Dedicated LNDL runtime / kernel outside operate() | A second execution engine to maintain, test, and secure; duplicates Branch's manager stack; violates the one-seam principle that keeps middles composable. |
| Keep note.X as the persistence namespace | A parallel magic mechanism with its own parser/assembler special cases, prompt section (~370 tokens), and commit rule; tools already exist and pass through permission policies uniformly. |
| Type-kind system for lvars (typed variable declarations) | Grammar and prompt-contract complexity for value the schema-side pydantic validation already provides; nothing downstream consumes lvar types. |
| Formally verify the forgiving parser (normalize + parse) | The desugar pass is deliberately heuristic; proving properties about it would freeze rules that must stay cheap to change. Formal effort, if any, belongs to confinement properties, not grammar soundness. |
| Open action dispatch (any callable name resolves) | Uncontrolled surface from model-authored text to execution; a closed signed registry keeps the text→action boundary auditable. |
| Skip the measurement gate, ship on design conviction | LNDL's cost story (prompt tax vs. saved calls) is quantitative; without pre-registered numbers the project self-justifies indefinitely. |

## References

- #1629 — LNDL package port (lexer/parser/assembler pipeline)
- #1635 — LNDL revival tracking issue (this ADR is its design vehicle)
- `lionagi/lndl/` — package under decision
- `lionagi/operations/types.py` — Middle protocol (the seam)
- `docs/adrs/ADR-0085-flow-control-plane.md` — the flow control plane the pilot leg runs on
