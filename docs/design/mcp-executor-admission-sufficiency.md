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
value the denylist never modeled. Two concrete gaps illustrate the class:

- A `patternProperties` whose pattern accepts a command-shaped key
  (`{"patternProperties": {"^command_custom$": {"type": "string"}}, "additionalProperties": false}`)
  looks closed but accepts `{"command_custom": "rm -rf /"}`.
- A schema that omits `type` entirely
  (`{"properties": {"operation": {"const": "status"}}, "additionalProperties": false}`)
  looks like a closed object but, because an absent `type` admits every instance
  type, also accepts a bare string.

## Approach: invert the polarity to a known-safe allowlist

The sufficiency proof models a **closed set of keywords it understands** and
denies on anything else. The load-bearing rule:

> Any keyword outside the understood set — present anywhere in the schema
> skeleton the proof walks — makes the node insufficient.

That single rule covers `patternProperties`, `propertyNames`,
`unevaluatedProperties`, `dependentSchemas`, the in-place applicators
(`if`/`then`/`else`/`not`, `contains`), `$dynamicRef`/`$recursiveRef`, and every
future vocabulary addition, without enumerating each one. "An unmodeled
applicator fails closed" holds *because* it is the primary rule, not as an
incidental consequence.

Membership on the understood set is decided by keyword and by value shape, not
by spelling:

- **Bounding constraints** that only ever narrow the admitted set: `type`,
  `const`, `enum`, `required`, `additionalProperties` (only `false` or an
  `enum`/`const`-restricted schema counts as closing), and the standardized
  numeric/size bounds (`minLength`, `maxItems`, `minimum`, `multipleOf`, ...).
  The numeric bounds are an explicit enumeration — never a `min*`/`max*` prefix
  test, which would exempt an arbitrary `minCustomVocabulary` and reopen the
  bypass.
- **Modeled applicators** the proof recurses through: `properties` (for
  closedness), `$ref` (local `#/...`, resolved and intersected with its
  Draft 2020-12 siblings), `allOf` (intersection — one provably-bounded branch
  suffices), and `anyOf`/`oneOf` (unions — every branch must independently prove
  bounded).
- **Inert annotations** that carry no assertion and are ignored: `description`,
  `title`, `$comment`, `examples`, `default`, `format`, `pattern`, `$schema`,
  `$id`, `$anchor`, `readOnly`, `deprecated`, `contentMediaType`, `$defs`, and
  the `x-*` vendor-extension convention. An `x-*`/`$comment` keyword is inert
  only when its *value* is demonstrably free of schema vocabulary — an extension
  whose value embeds `properties` is a hidden channel, not an annotation.
- **Unknown keywords** are tolerated only when their value cannot carry a
  subschema (a scalar, or a list/mapping built entirely of scalars). An unknown
  keyword with a mapping- or subschema-shaped value denies.

## Type-gate

An admittable object must constrain instances to objects. `type` must be
present and object-admitting (`"object"`, or a type array containing `"object"`
with no free-form alternative), **or** a top-level `const`/`enum` must pin the
whole instance to author-declared literals. A schema that omits `type` without a
`const`/`enum` pin is insufficient — an absent `type` admits every instance
type, so an object-shaped closure says nothing about a scalar instance.

This is a deliberate behavior change: a hand-authored executor schema that
relies on implicit typing (`properties` + `additionalProperties: false`, no
`type`) is now denied. The fix is to add `"type": "object"`. Denying it is the
correct posture — the same shape without `type` accepts a bare scalar.

## Ordering: applicators delegate before the type-gate

The type-gate's omitted-`type` denial applies to a **leaf object node**, not to
every node. A node that is an applicator root — `{"$ref": ...}`, `{"anyOf": ...}`,
`{"allOf": ...}` — legitimately omits a local `type` because the type constraint
lives in the resolved target or the branches. Applicator delegation therefore
runs first; the proof re-applies itself (allowlist gate and type-gate included)
inside every resolved target and branch, so delegation opens no omitted-type
hole. The omitted-`type` denial sits at the leaf-object branch, reached only
after no applicator has delegated. The allowlist pre-gate remains at the top of
every node — it only ever denies, so it never pushes an applicator node down the
leaf path.

## Two mechanisms, kept separate

Admission composes two independent checks, and keeping them separate is what
lets a scary-named tool with a genuinely bounded schema through:

- The **sufficiency proof** judges *shape closedness* — can an undeclared value
  ride through? It does not inspect individual property values.
- The **schema walker** judges *command channels by key name* — is a declared
  property a free-form command surface? It applies the identifier exemption
  (`service_id`, `resource_path`, `callback_url` are benign dynamic identifiers)
  and re-taints executor targets (`executable_path`, `script_path`).

A fixed-operation tool that also declares a free-form `service_id` string is
admitted: the object is closed (sufficiency) and the identifier key is benign
(walker). Folding value-boundedness into the sufficiency proof would erase that
distinction and deny the legitimate case. The two checks cover disjoint
territory — the sufficiency proof gates the shape skeleton (root, `$ref` target
and siblings, composition branches); the walker gates property-value interiors —
and their union is the whole document.

## Residuals — all fail closed

Every shape the proof does not model denies:

- Remote/non-local `$ref`, `$dynamicRef`/`$recursiveRef`, and dynamic anchors
  are not resolved → insufficient.
- The in-place and map applicators chosen to be denied rather than modeled
  (`unevaluatedProperties`, `dependentSchemas`, `if`/`then`/`else`, `not`,
  `contains`, `propertyNames`, `patternProperties`) → insufficient. A
  legitimately-bounded `unevaluatedProperties: false` object is among these; it
  is denied in the core. Recovering that specific admit is a strictly-additive
  follow-up, gated on real demand, and must never relax the core allowlist —
  a bounded schema wrongly denied is a support cost; an unbounded schema
  admitted is a security defect, so ties break to deny.
- Cyclic `$ref` and node/depth budget exhaustion → insufficient.
- An unknown keyword with a provably-inert scalar value is the one non-deny
  residual, and it is safe: a scalar cannot introduce a property value.

## Verification

The change is covered by the full admit/deny parametrized matrix plus: named
cases for the `patternProperties`-custom-key, omitted-`type`, and
`unevaluatedProperties:false` shapes; a synthetic never-modeled keyword asserted
to deny at five positions (root, property value, `allOf` branch, `$ref` sibling,
`anyOf` branch) with an inert-scalar control that still admits; and a
`Draft202012Validator` differential that, for every admitted executor-signaling
schema, asserts the schema rejects an injected free-form command instance. That
differential includes the applicator-root admits (`$ref`-root, `anyOf`-root), so
the delegation ordering is regression-tested, not review-checked.
