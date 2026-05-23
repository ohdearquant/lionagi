# TDD Methodology

Detailed cycle steps, multi-cycle patterns, parallel agent TDD, anti-patterns, and quality gates.

## Detailed Cycle Steps

### RED: Write Failing Test

Write a test that expresses the desired behavior — before writing any implementation.

**Python**:
```python
def test_feature_does_x():
    result = feature()
    assert result == expected  # This MUST fail initially
```

**Rust**:
```rust
#[test]
fn test_feature_does_x() {
    let result = feature();
    assert_eq!(result, expected); // This MUST fail initially
}
```

**Gate**: Run the test — it MUST fail. If it passes immediately, it is not testing new behavior.

```bash
# Python (always use uv run — never naked pytest or python)
uv run pytest tests/test_feature.py::test_feature_does_x -v

# Rust
cargo test test_feature_does_x -- --nocapture
```

### GREEN: Minimal Implementation

Write the **minimum code** to make the test pass. Resist over-engineering at this stage —
the only goal is green.

**Gate**: Run the same test — it MUST pass.

```bash
# Python
uv run pytest tests/test_feature.py::test_feature_does_x -v

# Rust
cargo test test_feature_does_x
```

### REFACTOR: Improve While Green

With the test passing, clean up the implementation:
- Extract helpers
- Improve naming
- Remove duplication
- Simplify logic
- Add docstrings / type hints

**Gate**: Run ALL tests — they MUST pass. Never refactor with failing tests.

```bash
# Python — full suite
uv run pytest

# Python — with coverage
uv run pytest --cov=src --cov-report=term-missing

# Rust — full workspace
cargo test --workspace
```

Then run lint:

```bash
# Python
uv run ruff check . && uv run mypy .

# Or use the lionagi CI skill
/ci
```

## Multi-Cycle Pattern

For larger features, chain multiple TDD cycles:

```text
Cycle 1: Core behavior
  RED → test_basic_case → GREEN → minimal impl → REFACTOR

Cycle 2: Edge cases
  RED → test_empty_input → GREEN → handle edge → REFACTOR

Cycle 3: Error handling
  RED → test_invalid_input → GREEN → add validation → REFACTOR

Cycle 4: Integration
  RED → test_integration → GREEN → wire together → REFACTOR

Final: Run full suite + lint to verify everything (/ci)
```

## Parallel Agent TDD

For complex features, use `li o fanout` to run multiple hypothesis tests in parallel:

```bash
li o fanout \
  --prompt "Write failing tests for: [feature description]. Cover: happy path, edge cases, error paths." \
  --workers 2
```

Then synthesize the test files and implement against the combined suite.

## Coverage Gates (lionagi standard)

After each cycle, check coverage trend:

```bash
uv run pytest --cov=src --cov-report=term-missing
```

Target thresholds:
- Business logic: ≥ 90%
- API surface: ≥ 85%
- Utilities: ≥ 80%

If coverage drops below threshold, add missing cases before marking the cycle complete.

## Anti-Patterns

- Writing implementation before the test
- Test that passes immediately without any implementation (not testing new behavior)
- Skipping the refactor phase (leaves messy GREEN code in production)
- Large implementation changes without running tests between steps
- Using naked `python` or `pytest` instead of `uv run`
- Treating the RED phase as optional ("I'll add the test later")
- Writing multiple behaviors into one test
