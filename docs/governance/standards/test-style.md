# Governance Test Style Standard

**Purpose**: Test fixture naming, category markers, directory layout, coverage thresholds, and
assertion patterns for the `tests/governance/` suite.

Cross-references: [dsl-style.md](dsl-style.md), [trace-naming.md](trace-naming.md),
[error-messages.md](error-messages.md), [commit-and-pr-style.md](commit-and-pr-style.md)

---

## 1. Fixture Naming

Names follow `{module}_{scenario}_{variant}`. Use concrete nouns for modules and active verbs
for scenarios. Avoid names like `fixture1`, `happy_path`, or `bad_case`.

**Good**:

```text
gate_registry_denies_unknown_tool
evidence_chain_rejects_reordered_nodes
certificate_mint_blocks_failed_hard_gate
charter_parser_rejects_executable_token
sod_matrix_blocks_reviewer_author
trace_gate_evaluate_records_soft_justification
policy_resolver_tie_denies_access
break_glass_window_expires_after_30m
```

**Bad**:

```text
test1
happy_path_charter
gate_bad_case
```

---

## 2. Categories And Markers

| Category | Markers | Scope |
|----------|---------|-------|
| Unit | `governance` | Pure model, parser, validator, or gate behavior |
| Integration | `governance` | Branch, Session, ActionManager, DataLogger, CLI, or adapter boundary |
| Adversarial | `governance`, `adversarial` | Bypass, forgery, spoofing, replay, policy confusion, time manipulation |
| Property | `governance`, `property` or `fuzz` | Generated inputs for evidence chains, SoD matrices, policy tie-deny |
| Live provider | `governance`, `live_llm` | SDK or model behavior — opt-in, never blocks the main PR lane |
| Mutation | `governance`, `mutation` | Fail-closed and tamper checks |

Recommended marker set for `pyproject.toml`:

```toml
markers = [
    "governance: all governance tests",
    "adversarial: bypass and forgery tests",
    "live_llm: requires real credentials",
    "mutation: tamper and fail-closed tests",
    "fuzz: property and generative tests",
    "property: property-based tests",
]
```

---

## 3. Directory Structure

```text
tests/
  governance/
    conftest.py
    test_evidence_chain.py
    test_operation_context.py
    test_gate_runtime.py
    test_charter_parser.py
    test_policy_resolution.py
    test_registry_allowlist.py
    test_sod.py
    test_break_glass.py
    test_certificates.py
    test_trace_spans.py
    adversarial/
      test_gate_bypass.py
      test_evidence_forgery.py
      test_certificate_replay.py
      test_tool_shadowing.py
      test_charter_injection.py
    fixtures/
      charters/
      evidence/
      registry/
      traces/
  providers/
    test_pydantic_ai_governed_endpoint.py
    test_openai_agents_governed_endpoint.py
    test_anthropic_agents_governed_endpoint.py
    test_langgraph_governed_endpoint.py
    test_llamaindex_governed_endpoint.py
    test_crewai_governed_endpoint.py
```

---

## 4. Coverage Thresholds

- New public governance APIs require happy path, edge case, and failure-path tests before merge.
- Governance core modules: ≥90% line coverage, ≥85% branch coverage.
- Gate, evidence, certificate, policy, registry, and parser modules each require at least one
  adversarial test for the primary bypass class.
- Every fail-closed rule must have a test where the evaluator raises or input is malformed.
- Every DSL grammar addition: ≥1 valid fixture + ≥2 invalid fixtures.
- Every span type added to the trace taxonomy: ≥1 emission test + ≥1 attribute validation test.
- Property tests are required for: evidence chain ordering, policy tie-deny, registry exact
  matching, and SoD conflict symmetry.
- Live provider tests must not block the normal PR lane unless credentials and deterministic
  sandbox settings are present.

---

## 5. Assertion Patterns

Assert runtime state, evidence records, hashes, and spans. Do not assert model prose except for
exact parser errors or human-facing error messages.

Required assertions per category:

**Evidence**:

- Immutable evidence cannot be mutated or deleted through public APIs.
- Protected deletion emits an immutable deletion evidence record.
- Hash field starts with `sha256:`.

**Gates**:

- Hard gate exceptions deny the call; `result.verdict == "DENY"` and `result.enforcement == "HARD"`.
- Soft gate override requires both `justification` and `justification_actor_id`.
- Advisory gate failures produce warning evidence but do not block execution.

**Adapters**:

- Raw tool execution outside governed mode raises or returns an explicit error.
- Coarse adapters do not claim internal framework tool calls were governed unless fine-grain
  wrapping is active.

**Errors**:

- Assert both `message` (human-readable) and `agent_message` (machine-routable) fields — see
  [error-messages.md](error-messages.md).
- Assert `code` matches the GOV-XXXX taxonomy.

---

## 6. Example A: Unit Test With Runtime Assertions

```python
import pytest


@pytest.mark.governance
def test_gate_registry_denies_unknown_tool(gate_evaluator, operation_context):
    result = gate_evaluator.evaluate(
        tool_name="tool.write_file",
        actor_role="reader",
        operation_context=operation_context,
    )

    assert result.verdict == "DENY"
    assert result.enforcement == "HARD"
    assert result.reason_code == "GOV-2101"
    assert result.evidence_hash.startswith("sha256:")
```

---

## 7. Example B: Property Test For Evidence Chain Tamper Resistance

```python
import pytest
from hypothesis import given, strategies as st


@pytest.mark.governance
@pytest.mark.property
@given(
    st.lists(
        st.dictionaries(st.text(min_size=1), st.text()),
        min_size=1,
        max_size=20,
    )
)
def test_evidence_chain_rejects_reordered_nodes(evidence_chain_factory, payloads):
    chain = evidence_chain_factory(payloads)
    reordered = list(reversed(chain))

    assert evidence_chain_factory.verify(chain).ok is True
    assert evidence_chain_factory.verify(reordered).ok is False
```

---

## 8. Example C: Adversarial Parser Test

```python
import pytest


@pytest.mark.governance
@pytest.mark.adversarial
@pytest.mark.parametrize(
    "token", ["__import__", "eval(", "exec(", "lambda ", "subprocess"]
)
def test_charter_parser_rejects_executable_token(charter_loader, token):
    source = f"""
charter_dsl: "0.1"
kind: agent_charter
metadata:
  charter_id: charter.bad
  version: "1.0.0"
  status: draft
  policy_release: policy.gov.v1
  authored_by: human:tester
  implemented_by: agent:tester
  ratification: null
agents: []
registry: {{snapshot: ratification_time, entries: []}}
constraints:
  - constraint_id: gate.bad
    description: "{token}"
sod: {{active: true, rules: []}}
permissions:
  default: deny
  resolution:
    specificity_order: [resource, role, tenant, global]
    tie: deny
  allow: []
  deny: []
trace: {{stamp: [], require_spans: [], require_evidence: []}}
"""
    with pytest.raises(CharterParseError) as exc:
        charter_loader(source)

    assert exc.value.code == "GOV-1104"
```
