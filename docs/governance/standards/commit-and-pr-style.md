# Governance Commit And PR Style Standard

**Purpose**: Conventional commit format, PR title/body template, governance scopes, and review
checklist rules for the lionagi governance implementation.

Cross-references: [adr-style.md](adr-style.md), [test-style.md](test-style.md),
[trace-naming.md](trace-naming.md), [error-messages.md](error-messages.md)

---

## 1. Conventional Commit Format

```text
type(scope): summary
```

**Allowed types**:

```text
feat  fix  docs  test  refactor  perf  build  ci  chore
```

**Required governance scopes**:

```text
gov  charter  policy  trace  adapter  evidence  gate  registry
sod  certificate  break-glass  jit  adr  docs  tests
```

Use one primary scope per commit. If a change touches multiple governance domains, split commits
unless the code path is inseparable.

---

## 2. Commit Summary Rules

- Lowercase after the closing parenthesis, unless naming a type or acronym.
- Imperative mood.
- Maximum 72 characters.
- No trailing period.
- Mention ADR number in the body, not the summary, unless the commit is only an ADR change.

**Good**:

```text
feat(gate): fail closed on registry evaluator exceptions
test(charter): add invalid executable token fixtures
docs(adr): revise ADR-0044 gate result ownership
feat(evidence): add append-only audit pile with sha256 chain
fix(policy): deny on tie when specificity is equal
```

**Bad**:

```text
feat: governance stuff
fix(policy/registry/gates): lots of changes
docs: update
FEAT(GATE): Added gate stuff.
```

---

## 3. PR Title Rules

- Format: `[Pxx] type(scope): summary`.
- Maximum 90 characters.
- Scope must match the dominant commit scope.
- Do not use marketing language.

**Good**:

```text
[P12] docs(adr): reconcile gate result and operation context ownership
[P16] feat(charter): compile DSL registry and gate targets
[P17] feat(gate): implement execute_governed with fail-closed exceptions
[P20] feat(trace): emit permit and certificate lifecycle spans
```

**Bad**:

```text
[P12] Revolutionary governance overhaul
[P16] Charter stuff
feat(charter): compile DSL (missing play tag)
```

---

## 4. PR Body Template

```markdown
## Summary

- What changed.
- Which governance primitive or standard it implements.

## ADRs And Standards

- ADRs: ADR-0044, ADR-0050
- Standards: docs/governance/standards/trace-naming.md

## Runtime Behavior

- New public APIs:
- Changed invariants:
- Backward compatibility:

## Tests

- [ ] Unit
- [ ] Integration
- [ ] Adversarial
- [ ] Property
- [ ] CLI
- [ ] Trace/span validation

Commands run:

    uv run pytest tests/governance
    uv run pytest tests/providers
    uv run ruff check lionagi tests

## Evidence And Trace

- Evidence records emitted:
- Required spans emitted:
- Redaction considerations:

## Risks

- Known risks:
- Recovery rule mapping:

## Review Checklist

- [ ] Matches accepted ADRs or includes ADR revision.
- [ ] Public API has happy path and edge case tests.
- [ ] Fail-closed paths are tested.
- [ ] Evidence and trace records are linked by hash/id.
- [ ] No raw bypass path is introduced.
- [ ] Charter/registry/policy examples updated when behavior changes.
```

---

## 5. Review Checklist Rules

Every governance PR must answer:

1. Which ADR owns this behavior?
2. Which standard constrains naming, errors, tests, or traces?
3. Does this expose a raw bypass path?
4. Does it emit or update evidence?
5. Does it project a span, and is the span sampled 100% if immutable?
6. Does it preserve the zero-rewrite adapter posture?
7. Did tests assert runtime state rather than generated prose?

---

## 6. Example A: Commit Set For DSL Parser

```text
docs(charter): add canonical DSL style standard
feat(charter): parse v0 top-level blocks into typed AST
test(charter): reject wildcard registry values and executable tokens
```

Why correct: docs, parser behavior, and tests are in separate commits; each scope stays focused;
tests are written alongside the feature, not after.

---

## 7. Example B: PR Header And Body Summary

```markdown
# [P16] feat(charter): compile DSL registry and gate targets

## Summary

- Adds `Charter.compile()` producing registry entries, gate bindings, SoD rules, evidence
  requirements, trace expectations, and activation evidence.
- Activation fails closed when target resolution or hash checks fail.

## ADRs And Standards

- ADRs: ADR-0047, ADR-0051, ADR-0052
- Standards: `dsl-style.md`, `trace-naming.md`, `test-style.md`

## Runtime Behavior

- New public APIs: `Charter.compile() -> CompiledCharter`
- Changed invariants: charter activation now requires resolved gate registry before accepting requests
- Backward compatibility: `AgentCharter` from ADR-0047 is now compiler output, not direct user input

## Tests

- [x] Unit
- [x] Integration
- [ ] Adversarial (scheduled for P23)
- [x] Property
- [ ] CLI (included in P14 CLI pass)
- [x] Trace/span validation

Commands run:

    uv run pytest tests/governance/test_charter_compile.py -v
    uv run pytest tests/governance/ --co -q
    uv run ruff check lionagi/protocols/governance/
    uv run mypy lionagi/protocols/governance/compiler.py
```

Why correct: the title is short and scoped, the ADR/standard ownership is explicit, and the
test checklist shows what is done vs. deferred with a clear reason.
