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
# lionagi Python
scripts/ci.sh test-python tests/test_feature.py::test_feature_does_x

# Rust
cargo test test_feature_does_x -- --nocapture
```

### GREEN: Minimal Implementation

Write the **minimum code** to make the test pass. Resist over-engineering at this stage —
the only goal is green.

**Gate**: Run the same test — it MUST pass.

```bash
# lionagi Python
scripts/ci.sh test-python tests/test_feature.py::test_feature_does_x

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
# lionagi Python — full suite
scripts/ci.sh test-python

# lionagi Python — with coverage
scripts/ci.sh test-python-cov

# Rust — full workspace
cargo test --workspace
```

Then run lint:

```bash
# lionagi Python
scripts/ci.sh lint-python

# Full configured pipeline
scripts/ci.sh ci
```

Use the repository's configured type checker as a separate gate. Lionagi configures Pyright,
not mypy.

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

Final: Run the full configured CI pipeline to verify everything
```

## Parallel Agent TDD

For complex features, run multiple hypothesis tests in parallel through the plugin's MCP
server, `fanout.submit`. It is a spawn verb, so its op needs the current `schema_fingerprint`
— ask for it first:

```json
{"help": "fanout.submit"}
```

Then submit:

```json
{
  "ops": [
    {
      "op": "fanout.submit",
      "args": {
        "query": ["claude"],
        "prompt": "Write failing tests for: [feature description]. Cover: happy path, edge cases, error paths.",
        "num_workers": 2,
        "cwd": "/absolute/path/to/repository"
      },
      "schema_fingerprint": "<from the help call above>"
    }
  ]
}
```

The reply carries a `run_id` for the fan-out, not the results. Check the submit op's `ok`
field, then wait for it:

```json
{"ops": [{"op": "job.wait", "args": {"run_ids": ["<run_id>"]}}]}
```

`job.wait` is bounded. Check its op's `ok` field and the result's `all_terminal` field,
repeating while the run remains pending. Then read the output:

```json
{"ops": [{"op": "job.output", "args": {"run_id": "<run_id>"}}]}
```

**Checkout-local alternative.** Inside a lionagi checkout,
`li o fanout claude -n 2 "..." --cwd "$(pwd)"` runs the same fan-out as a foreground call.
The model and prompt are positional, `-n` is the worker count, and `--workers` takes a
comma-separated list of model specs instead.

Then synthesize the test files and implement against the combined suite.

## Coverage Gates

After each cycle, check coverage trend:

```bash
scripts/ci.sh test-python-cov
```

Meet the repository's configured threshold. If coverage drops below it, add missing cases
before marking the cycle complete.

## Anti-Patterns

- Writing implementation before the test
- Test that passes immediately without any implementation (not testing new behavior)
- Skipping the refactor phase (leaves messy GREEN code in production)
- Large implementation changes without running tests between steps
- Bypassing the repository's configured test runner
- Treating the RED phase as optional ("I'll add the test later")
- Writing multiple behaviors into one test
