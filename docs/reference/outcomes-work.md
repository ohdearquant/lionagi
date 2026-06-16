# outcomes and work — reference

## lionagi.outcomes (ADR-0021)

`lionagi.outcomes` holds domain types that skills produce and Studio renders as artifact cards.
Outcomes are persisted as `artifacts` table rows with `kind = outcome_kind`; the frontend's
kind-dispatched renderer picks the right card component for each kind.

Domain types (review, CI, gate) are intentionally separated from infrastructure types in
`lionagi.models` so a Studio-only consumer can import outcomes without pulling in framework
machinery.

### SkillOutcome

Base for all structured skill outputs. Concrete subclasses set `outcome_kind` to a literal
string. That string becomes `artifacts.kind` in the DB and the dispatch key for the
kind-aware frontend renderer.

### ReviewFinding

Ops-plane artifact contract (ADR-0021). Distinct from `lionagi.casts.emission.Finding`
(reactive-bus base used in the engines layer). Fields: `severity`, `category`, `file`,
`line`, `description`, `suggestion`. The `file` field is validated via `check_path_safe`
and must be a relative path.

### ReviewOutcome

Ops-plane artifact contract (ADR-0021 §A). Distinct from `lionagi.engines.review.ReviewVerdict`
(reactive-bus emission). The frontend renders `ReviewOutcome` as the `ReviewVerdictCard`
(ADR-0021 §E): severity/category breakdown on top, blocking findings expanded, minor
suggestions collapsed. `round` is 1-indexed; the codex-pr-review skill writes one
`ReviewOutcome` per iteration round.

---

## lionagi.work.rules — Rule/RuleSet contract

### CheckKind values and params

| check | required params | notes |
|-------|----------------|-------|
| `required` | (none) | field must not be None |
| `type` | `{"type": "<FieldType>"}` | one of `str`\|`int`\|`float`\|`bool`\|`list`\|`dict`; int accepted for float (numeric widening) |
| `range` | `{"min": <number>, "max": <number>}` | either bound optional; numeric fields only |
| `pattern` | `{"pattern": "<regex>", "flags": <int>}` | `flags` defaults to 0; **trusted patterns only** (see security note below) |
| `custom` | `{"callable": Callable[[Any], bool], "error": "<msg>"}` | `error` is the fallback message |

### Pattern rule security contract

Pattern rules use the stdlib `re` backtracking engine and can hold the GIL during
catastrophic matches, making thread-based timeouts ineffective.

**Pattern rules are intended for trusted patterns only** — for example, validating
application-controlled fields (phone formats, zip codes) where the pattern is authored by
the developer, not supplied by users.

Mitigation: inputs exceeding `REGEX_MAX_INPUT_LENGTH` (default 4096) characters are
rejected before the regex engine is invoked. This bounds the input dimension; it does not
bound the pattern dimension. Nested-quantifier patterns such as `(a+)+` remain pathological
regardless of input length.

If you need safe matching against untrusted patterns or very long inputs, use a
non-backtracking engine (e.g., `google-re2`) and provide a `custom` rule backed by that
engine instead.

### RuleSet behavior

Rules are applied in insertion order. All enabled rules are evaluated (no short-circuit), so
`apply_all` returns a complete list of errors. Each rule must have a unique `rule_id` within
a `RuleSet`; `add` raises `ValueError` on duplicate `rule_id`.
