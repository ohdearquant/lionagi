# MCP executor-admission: schema-sufficiency by known-safe allowlist

## Problem

`register_mcp_server` refuses to register a tool that exposes a generic
shell/command/process/script executor surface, while still admitting
legitimate bounded tools. A tool whose *name* looks like an executor
(`exec`, `bash`, `spawn_process`, ...) is admitted only if its input schema is
provably **bounded** — the caller cannot smuggle a free-form command through an
undeclared key or an unconstrained value. This bounded-ness test is the
*sufficiency proof*.

An earlier sufficiency proof recognized dangerous schema shapes one at a time:
an open `additionalProperties`, a non-object type, a union with a free-form
branch, and so on. Against JSON Schema — whose vocabulary is open-ended and
extensible — a proof built from a denylist of known-dangerous shapes cannot be
complete. Each new applicator keyword (`patternProperties`, `propertyNames`,
`unevaluatedProperties`, `dependentSchemas`, ...) is another way to introduce a
value the denylist never modeled.

## Problem, round two: the discriminator moved, the defect didn't

A later revision inverted the polarity to a known-safe allowlist: any keyword
outside an understood set makes the node insufficient. That closed the
keyword-enumeration gap, but reopened the same defect class through a
different axis. The allowlist gate only ran on nodes the recursion actually
visited, and a separate, narrower discriminator —
"is this declared property's *value* itself object-shaped or applicator-
bearing" — decided which property values got recursed into at all. That
discriminator enumerated `properties`, `patternProperties`,
`additionalProperties`, `unevaluatedProperties`, `propertyNames`,
`dependentSchemas`, `$ref`, `allOf`, `anyOf`, `oneOf` as the keywords that
made a value worth recursing into. It omitted the conditional applicators
(`if`/`then`/`else`/`not`) and the array applicators (`items`/`prefixItems`).
A property value carrying one of those was therefore **never visited at
all** — not walked, not allowlist-checked — regardless of what it contained:

- `{"if": {}}`, `{"then": {}}`, `{"else": {}}`, and `{"not": {"type": "null"}}`,
  each sitting as a declared property's value, all admitted.
- `service_id: {"type": "array", "items": {"not": {"type": "null"}}}` admitted,
  even though `items`' own subschema carries an unmodeled `not`.

Enumerating "which schema *positions* deserve the allowlist" is the same
mistake as enumerating "which *keywords* are dangerous" — just re-entered
through the traversal axis instead of the keyword axis. Closing it the same
way it was closed the first time (add the missing keywords to the
discriminator) only produces a third list to eventually miss the fourth
keyword. The fix removes the discriminator instead.

## Approach: one closed keyword registry, total traversal

Every JSON Schema Draft 2020-12 keyword is classified into **exactly one** of
four classes, once, in a single module-level registry:

- **Inert annotation** — carries no assertion, contributes nothing to
  admission: `title`, `description`, `default`, `examples`, `deprecated`,
  `readOnly`, `writeOnly`, `$comment`, `$schema`, `$id`, `$anchor`,
  `$vocabulary`, `format`, `pattern`, `contentEncoding`, `contentMediaType`,
  `contentSchema`.
- **Bounding** — narrows the admitted set, carries no recursable subschema of
  its own: `type`, `const`, `enum`, `required`, `dependentRequired`, and the
  standardized numeric/size bounds (`multipleOf`, `maximum`, `minLength`,
  `maxItems`, `maxContains`, `maxProperties`, ...). An explicit enumeration,
  never a `min*`/`max*` spelling heuristic — a prefix test would exempt an
  arbitrary `minCustomVocabulary` and reopen the bypass this registry exists
  to close.
- **Modeled applicator** — the proof recurses through and credits:
  `properties`, `additionalProperties` (only when its value is a schema
  Mapping — the `false`/bounding form is a closedness fact, not a recursion
  target), `allOf`, `anyOf`, `oneOf`, `$ref` (local `#/...` only), `$defs`,
  `definitions`.
- **Denied applicator** — recognized by name, never modeled: its mere
  *presence* denies the node outright, and the proof never recurses beneath
  it (an ancestor denial already covers everything nested inside).
  `patternProperties`, `propertyNames`, `unevaluatedProperties`,
  `unevaluatedItems`, `dependentSchemas`, `if`, `then`, `else`, `not`,
  `contains`, `items`, `prefixItems`, `$dynamicRef`, `$dynamicAnchor`,
  `$recursiveRef`, `$recursiveAnchor`. Promoting one of these to modeled is a
  separate, individually-argued change with its own oracle-soundness
  argument — never a silent reclassification.

A keyword outside all four classes is **unknown**, and is tolerated only when
its value cannot itself carry a subschema (a scalar, or a list/mapping built
entirely of scalars); a mapping- or subschema-shaped value denies.

The traversal that consumes this registry
(`_structural_coverage_insufficient`) visits **every** schema-bearing
position **unconditionally** — every `properties` value, the
`additionalProperties` schema, every composition branch, every resolved
local `$ref` target, and every `$defs`/`definitions` entry — instead of
deciding, node by node, whether a position is "worth" visiting. There is no
longer a discriminator that could omit a position; the registry alone
decides what a node's own keywords mean, and the traversal reaches every
node regardless of its shape.

This traversal deliberately applies **no type-gate and no closedness
requirement**. A scalar leaf `{"type": "string"}` is sufficient on its own —
no denied/unknown keyword is present — which is exactly what preserves a
free-form identifier-key property (`service_id`, `resource_path`,
`callback_url`) declared alongside a fixed `operation`: the identifier's own
value is visited, found to carry only bounding keywords, and the walker's
key-name policy (untouched by this change) governs it from there.

### Totality argument

Every schema-bearing position in a document reaches the registry by exactly
one of three routes, and no fourth route exists. (i) It sits under a chain of
**modeled** applicators from the root — the traversal's own recursion visits
it, because each modeled-applicator branch above it is expanded in turn. (ii)
It sits under a **denied** applicator — the ancestor node returns insufficient
the moment it sees that keyword, before it would ever need to look inside it,
so the position underneath is covered by the denial rather than by a visit.
(iii) It sits under an **unknown** keyword, which is itself checked for a
subschema-shaped value and denied at that node if so. Because inert and
bounding keywords never gate recursion in either direction, they cannot
create a fourth route out of this partition. Therefore no schema-bearing
position — at any depth, under any composition, inside any property value —
escapes classification by the registry.

### Two side obligations

1. The inert-annotation class admits **no** keyword the default Draft
   2020-12 dialect treats as an applicator or assertion — every entry in it
   is annotation-only by specification. `contentSchema` keeps its own
   individually-argued rationale rather than a generic "annotations are
   safe" excuse: its value is a mapping (schema-shaped, like `$defs`), but it
   describes the *decoded content* of a string instance and asserts nothing
   about the instance itself, and that holds only because the
   content-*assertion* vocabulary (as opposed to the content-*annotation*
   vocabulary `contentSchema` belongs to) stays disabled in the default
   dialect this module validates against. Enabling that vocabulary in a
   future dialect configuration would require `contentSchema` to leave this
   class.
2. `$defs`/`definitions` entries are visited **unconditionally**, not
   reachability-gated by whether some `$ref` in the document actually points
   at them. This is a deliberate fail-closed choice, not an oversight: an
   entry nobody currently references is still schema-bearing content sitting
   in the document, and a future edit that adds a reference to it (or a
   consumer that resolves `$defs` by convention rather than an explicit
   `$ref`) must not inherit a channel this proof never bothered to check.

## Object-boundedness: a second, orthogonal proof

The registry-driven traversal answers "does any position carry a keyword this
proof does not model" — it never asks whether the *object itself* is closed
to undeclared keys. That question is answered by a second, independent
function, `_object_boundedness_insufficient`, unchanged in shape from the
prior revision: `type` must include `"object"` (or a top-level `const`/`enum`
must pin the whole instance), applicators delegate before the omitted-type
denial (so a `$ref`/`allOf`/`anyOf`-rooted schema that legitimately carries
no local `type` is not false-denied), and a non-empty `properties` is only
bounded when `additionalProperties` is `false` (or itself restricted to a
finite `enum`/`const`). This proof also recurses into a declared property's
value, but only when that value could itself resolve to an object instance
(its own `type` includes `"object"`, or it carries a modeled-applicator
keyword) — a scalar, array, or annotation-only value is left alone, which is
what keeps the identifier-suffix exemption intact. Omitting a value here
because it is reached only through a *denied* applicator (`if`/`then`/
`items`, ...) is harmless: that applicator's bare presence already makes the
whole document insufficient via the registry traversal, independent of
whether this proof would have looked inside it.

**Overall verdict**: `_schema_is_insufficient(schema)` is `True` (insufficient)
if *either* the object-boundedness proof fails *or* the registry-driven
structural-coverage traversal finds a denied/unknown keyword anywhere in the
document. The two proofs run over the same document and are combined by OR;
neither substitutes for the other.

## Two mechanisms, kept separate

Admission composes two independent checks with two different jobs, and
keeping them separate is what lets a scary-named tool with a genuinely
bounded schema through:

- The **sufficiency proof** (the two functions above) is the STRUCTURAL
  gate. It covers the WHOLE DOCUMENT — every node, at every depth, including
  the interior of a declared property's own value — and asks only "is this
  shape provably closed". It never reasons about a key's *name*.
- The **schema walker** does KEY-NAME / command classification ONLY — is a
  declared property (by its name) a free-form command surface? It applies
  the identifier exemption (`service_id`, `resource_path`, `callback_url` are
  benign dynamic identifiers) and re-taints executor targets
  (`executable_path`, `script_path`). It is NEVER load-bearing for structural
  coverage — it does not, and must not be relied on to, prove a shape closed.
  This change does not touch the walker's own command-classification logic.

## Residuals — all fail closed

Every shape the proof does not model denies:

- Remote/non-local `$ref`, `$dynamicRef`/`$recursiveRef`, and dynamic anchors
  are not resolved → insufficient.
- Every denied applicator (`patternProperties`, `propertyNames`,
  `unevaluatedProperties`, `unevaluatedItems`, `dependentSchemas`,
  `if`/`then`/`else`/`not`, `contains`, `items`, `prefixItems`) denies at
  sight, regardless of what its own subschema contains. Two accepted
  over-blocks follow directly from this and are deliberate, not oversights:
  - A legitimately-bounded `unevaluatedProperties: false` object is denied in
    the core, even though it is a real Draft-2020-12 closing mechanism.
    Recovering that specific admit is a strictly-additive follow-up, gated on
    real demand, and must never relax the core allowlist.
  - An array property whose `items`/`prefixItems` subschema is itself fully
    bounded (e.g. `items: {"enum": ["a", "b"]}`, or a closed tuple with
    `items: false`) is now denied too, because `items`/`prefixItems` are
    denied applicators that deny at sight rather than being inspected for
    boundedness. The behavior-visible consequence: a previously registerable
    tool with a bounded-array arg on a strong name now refuses registration.
    Recovering bounded-array admission through `items` is, like
    `unevaluatedProperties: false`, a separate, individually-argued delta
    with its own oracle-soundness argument — not part of this change. A
    bounded schema wrongly denied is a support cost; an unbounded schema
    admitted is a security defect, so ties break to deny.
- Cyclic `$ref` and node/depth budget exhaustion → insufficient.
- An unknown keyword with a provably-inert scalar value is the one non-deny
  residual, and it is safe: a scalar cannot introduce a property value.

## Verification

The change is covered by:

- The full pre-existing admit/deny parametrized matrix, additive apart from
  the three array-applicator cases named above: those three moved from admit
  to deny expectations, renamed to state the new verdict, each keeping an
  oracle differential that now proves the opposite fact — the schema was
  never itself capable of admitting an injected command, so the new deny is
  an accepted over-block rather than a correction of a wrong test. Every
  other pre-existing case, including the named `patternProperties`-custom-key,
  omitted-`type`, and `unevaluatedProperties:false` necessity cases, the four
  applicator-root admits, and a `Draft202012Validator` differential for every
  admitted executor-signaling schema, is untouched.
- A **registry-generated** regression matrix, parametrized by iterating the
  keyword-classification registry's own frozensets directly rather than a
  hand-listed keyword table: for every modeled applicator, a benign
  closed/bounded payload admits (with an oracle differential) and a
  denied/unknown payload denies, at nesting depths 1, 2, and 3; for every
  denied applicator, denial is asserted at each of five subschema positions
  (declared-property value, `additionalProperties` schema, a composition
  branch, a `$ref` target, and an unreferenced `$defs` entry); a synthetic,
  never-registered keyword denies at every position when Mapping-valued and
  admits when its value is a provably-inert scalar. A keyword added to
  either registry frozenset later gains coverage here automatically, with no
  test-file edit required. A separate test asserts the four registry classes
  partition with no keyword classified twice, and that their union covers a
  representative Draft 2020-12 core-vocabulary keyword list.
- The five originally-reproduced false negatives — `{"if": {}}`,
  `{"then": {}}`, `{"else": {}}`, `{"not": {"type": "null"}}` in a declared
  property's value, and an `items`/`prefixItems` array position carrying an
  unmodeled `not` — now all deny.

Known, accepted consequence of this change (not a regression): the three
schemas whose only bounding mechanism was an `items`/`prefixItems`-restricted
array (`items: {"enum": [...]}`, a closed tuple via `prefixItems` +
`items: false`, and a nested array bounded by `enum` items at every level),
previously admitted under the enumerated discriminator, now deny under the
registry, because `items`/`prefixItems` are denied applicators that deny at
sight rather than being inspected for their own boundedness. The
behavior-visible consequence is the same one named above: a previously
registerable tool with a bounded-array arg on a strong name now refuses
registration. This is the same accepted-over-block shape as
`unevaluatedProperties: false`; the regression matrix asserts the deny
verdict directly, each case paired with an oracle differential proving the
denied schema was never itself capable of admitting an injected command —
the deny is a deliberate over-block, not a missed bypass. Recovering
admission for a bounded array position is future, separately-argued work
with its own oracle-soundness argument, not part of this change.
