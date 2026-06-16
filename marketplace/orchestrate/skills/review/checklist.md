# Code Review Checklist

Full 9-section checklist for general-purpose code review.

## 1. Correctness

- Logic: does the code do what the commit message / PR description claims?
- Control flow: every branch reachable? Every branch correct? Early-return
  cases handled?
- Off-by-one: loop bounds, slicing, array indexing — inclusive vs exclusive
  boundaries match intent?
- Error handling: every possible exception / error case caught at the
  right layer? Not too broad (`except Exception`) or too narrow (missed
  subclass)?
- Invariants: anything the code *assumes* without checking? If the
  assumption breaks in production, what happens?
- Concurrency: shared state, locks, atomic operations — any TOCTOU or race?
- Numeric: overflow, underflow, division by zero, float precision?

## 2. API contract

- Public signatures: backward-compatible? Deprecation path if breaking?
- Type annotations match runtime behavior?
- Error types: raises the documented exception, not a vague generic?
- Return value shape: `None` vs `Optional[T]` vs sentinel — consistent
  with the surrounding codebase?

## 3. Tests

- Every new code path has a test? (Greenfield is an exception; bug fixes
  should include regression tests.)
- Edge cases covered: empty input, None, boundary values, error paths?
- Mocks: are they faithful to the real behavior, or could they hide bugs?
- Test names describe WHAT is being tested, not HOW.
- Flaky risk: does the test depend on timing, randomness, external
  services, filesystem state?

## 4. Readability & maintainability

- Names: do variable / function / class names communicate intent? Avoid
  `data`, `result`, `helper`, `utils` unless truly generic.
- Function length / complexity: too many responsibilities in one function?
  Cyclomatic complexity > 10 is a smell.
- Nesting depth: pyramid-of-doom indent levels?
- Comments: do they explain *why*, not *what*? Redundant comments ("set
  x to 5" as a comment on `x = 5`) are noise.
- Dead code: commented-out blocks, unused imports, unreferenced
  functions? Delete it.

## 5. Consistency with existing code

- Naming convention: matches file / module style?
- Error handling pattern: matches what the surrounding code does?
- Import ordering, formatter usage — consistent with project config?
- Abstraction level: fits with existing layers, or introduces a new
  level that's one-off?

## 6. Docs & changelog

- Public API changes: documented in docstring / README / CHANGELOG?
- Behavioral changes (perf, default values, ordering) — noted?
- New config options — documented with example?

## 7. Dependencies

- New dependency: is it necessary? Maintained? License compatible?
- Version constraints: pinned where appropriate, floating where safe?
- Removed dependency: any remaining references? Lockfile updated?

## 8. Performance (light-touch — not a perf review)

- Obvious complexity regressions (O(n²) loop where O(n) worked)?
- New allocations in a hot path?
- Database queries inside a loop instead of batched?
- N+1 query pattern?

Full perf analysis is a separate dimension (see `perf` in pr-review
skill) — flag the obvious, leave depth to the perf specialist.

## 9. Over-engineering / under-engineering

- Premature abstraction: a base class / interface introduced for a
  single implementation?
- Speculative flexibility: options / flags added for "we might need this
  later"?
- Conversely — is the code *too* simple to handle real inputs?
  (e.g. assumes UTF-8, ignores locale, skips error handling).

## Source Code Reference

| File | Purpose |
|---|---|
| `lionagi/protocols/generic/element.py` | Base Element class (UUID + timestamp + metadata) |
| `lionagi/protocols/generic/pile.py` | Thread/async-safe O(1) UUID-keyed collection |
| `lionagi/protocols/generic/progression.py` | Ordered UUID deque, decoupled from Pile |
| `lionagi/protocols/messages/` | RoledMessage subtypes (System, Instruction, ActionRequest, etc.) |
| `lionagi/session/branch.py` | Branch facade — primary API surface |
| `lionagi/operations/types.py` | Middle protocol definition |
| `lionagi/operations/` | chat, parse, operate, ReAct, select, act, communicate, run |
| `lionagi/ln/` | Utilities: alcall, bcall, retry, fuzzy_json, sentinels |
| `lionagi/agent/` | AgentSpec, create_agent, PermissionPolicy, hooks |
