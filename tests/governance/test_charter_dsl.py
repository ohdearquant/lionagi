# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for Charter DSL v0 parser, models, and validator.

Covers: parse/reject, Pydantic schema validation, semantic validation,
JSON Schema export, and all three canonical examples from the spec.
"""

from __future__ import annotations

import json
import textwrap

import pytest
from pydantic import ValidationError

from lionagi.governance.charter import (
    CharterValidationError,
    charter_json_schema,
    parse_charter,
    validate_charter,
)
from lionagi.governance.dsl import (
    CharterDocument,
    CharterKind,
    CharterStatus,
    Enforcement,
    ManagerSurface,
    RegistryCategory,
)

# ──────────────────── Fixture: minimal valid charter ──────────────────

MINIMAL_AGENT_CHARTER = textwrap.dedent("""\
    charter_dsl: "0.1"
    kind: agent_charter
    metadata:
      charter_id: charter.test.minimal
      version: "1.0.0"
      status: accepted
      policy_release: policy.gov.v1
      authored_by: human:governance
      implemented_by: agent:implementer
      ratification:
        hash: sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
        signed_at: "2026-05-27T00:00:00Z"
    agents:
      - agent_id: agent.reader
        actor_id_source: branch_id
        role: reader
        allowed_models: [openai:gpt-5.4]
        allowed_tools: [tool.read_file]
    registry:
      snapshot: ratification_time
      entries:
        - category: tool
          value: tool.read_file
          scope: agent
          scope_id: agent.reader
          reason: "Reader can read files."
          evidence_refs: [ev.registry.reader.read_file]
    constraints:
      - constraint_id: gate.registry.read_file
        description: "Read-file calls must match registry."
        gate_id: verify_in_registry
        manager_surface: ActionManager
        enforcement: hard
        attach:
          level: action
          action: tool_call
          tools: [tool.read_file]
        evidence:
          required: [GateResult]
    sod:
      active: true
      rules: []
    permissions:
      default: deny
      resolution:
        specificity_order: [resource, role, tenant, global]
        tie: deny
      allow:
        - rule_id: allow.reader.read
          scope: role
          roles: [reader]
          action: tool_call
          tools: [tool.read_file]
          requires_evidence: [GateResult]
          because: "Reader needs file access."
      deny:
        - rule_id: deny.reader.write
          scope: role
          roles: [reader]
          action: tool_call
          tools: [tool.write_file]
          because: "Reader cannot write."
    trace:
      stamp: [charter_id, policy_release, agent_id, role]
      require_spans:
        - governance.operation
        - gate.evaluate
        - evidence.emit
      require_evidence:
        - GateResult
""")


# ──────────────────── Parse happy path ────────────────────────────────


class TestParseCharter:
    def test_parse_minimal(self):
        doc = parse_charter(MINIMAL_AGENT_CHARTER)
        assert doc.charter_dsl == "0.1"
        assert doc.kind == CharterKind.AGENT
        assert doc.metadata.charter_id == "charter.test.minimal"
        assert doc.metadata.status == CharterStatus.ACCEPTED
        assert len(doc.agents) == 1
        assert doc.agents[0].agent_id == "agent.reader"
        assert len(doc.constraints) == 1
        assert doc.permissions.default == "deny"

    def test_parse_returns_charter_document(self):
        doc = parse_charter(MINIMAL_AGENT_CHARTER)
        assert isinstance(doc, CharterDocument)

    def test_enum_values(self):
        doc = parse_charter(MINIMAL_AGENT_CHARTER)
        assert doc.constraints[0].enforcement == Enforcement.HARD
        assert doc.constraints[0].manager_surface == ManagerSurface.ACTION
        assert doc.registry.entries[0].category == RegistryCategory.TOOL


# ──────────────────── Reject invalid YAML ─────────────────────────────


class TestRejectInvalid:
    def test_reject_tabs(self):
        bad = MINIMAL_AGENT_CHARTER.replace("  ", "\t")
        with pytest.raises(ValueError, match="Tabs"):
            parse_charter(bad)

    def test_reject_unsupported_version(self):
        bad = MINIMAL_AGENT_CHARTER.replace('charter_dsl: "0.1"', 'charter_dsl: "0.2"')
        with pytest.raises(ValidationError, match="Unsupported charter_dsl"):
            parse_charter(bad)

    def test_reject_unknown_keys(self):
        bad = MINIMAL_AGENT_CHARTER + "custom_field: oops\n"
        with pytest.raises(ValidationError):
            parse_charter(bad)

    def test_reject_wildcard_in_tool(self):
        bad = MINIMAL_AGENT_CHARTER.replace(
            "allowed_tools: [tool.read_file]",
            "allowed_tools: [tool.*]",
        )
        with pytest.raises(ValidationError, match="Wildcards"):
            parse_charter(bad)

    def test_reject_wildcard_in_registry_value(self):
        bad = MINIMAL_AGENT_CHARTER.replace(
            "value: tool.read_file",
            "value: tool.*",
        )
        with pytest.raises(ValidationError, match="Wildcards"):
            parse_charter(bad)

    def test_reject_executable_token_eval(self):
        bad = MINIMAL_AGENT_CHARTER.replace(
            "Reader can read files.",
            "eval(malicious_code)",
        )
        with pytest.raises(ValidationError, match="Executable token"):
            parse_charter(bad)

    def test_reject_executable_token_import(self):
        bad = MINIMAL_AGENT_CHARTER.replace(
            "Reader can read files.",
            "__import__('os').system('rm -rf /')",
        )
        with pytest.raises(ValidationError, match="Executable token"):
            parse_charter(bad)

    def test_reject_executable_token_subprocess(self):
        bad = MINIMAL_AGENT_CHARTER.replace(
            "Reader can read files.",
            "subprocess.run(['rm', '-rf', '/'])",
        )
        with pytest.raises(ValidationError, match="Executable token"):
            parse_charter(bad)

    def test_reject_both_gate_and_hook(self):
        bad = MINIMAL_AGENT_CHARTER.replace(
            "    gate_id: verify_in_registry\n",
            "    gate_id: verify_in_registry\n    hook_name: pre_tool_call\n    hook_phase: pre\n",
        )
        with pytest.raises(ValidationError, match="both gate_id and hook_name"):
            parse_charter(bad)

    def test_reject_neither_gate_nor_hook(self):
        bad = MINIMAL_AGENT_CHARTER.replace(
            "    gate_id: verify_in_registry\n",
            "",
        )
        with pytest.raises(ValidationError, match="neither gate_id nor hook_name"):
            parse_charter(bad)

    def test_reject_hook_without_phase(self):
        bad = MINIMAL_AGENT_CHARTER.replace(
            "gate_id: verify_in_registry",
            "hook_name: pre_tool_call",
        )
        with pytest.raises(ValidationError, match="hook_phase"):
            parse_charter(bad)

    def test_reject_class_attach_without_tool_class(self):
        bad = MINIMAL_AGENT_CHARTER.replace(
            "level: action\n      action: tool_call\n      tools: [tool.read_file]",
            "level: class",
        )
        with pytest.raises(ValidationError, match="tool_class"):
            parse_charter(bad)

    def test_reject_action_attach_without_action(self):
        bad = MINIMAL_AGENT_CHARTER.replace(
            "level: action\n      action: tool_call\n      tools: [tool.read_file]",
            "level: action",
        )
        with pytest.raises(ValidationError, match="requires 'action'"):
            parse_charter(bad)

    def test_reject_bad_semver(self):
        bad = MINIMAL_AGENT_CHARTER.replace('version: "1.0.0"', 'version: "v1"')
        with pytest.raises(ValidationError, match="semver"):
            parse_charter(bad)

    def test_reject_bad_hash_format(self):
        bad = MINIMAL_AGENT_CHARTER.replace(
            "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "md5:abcdef",
        )
        with pytest.raises(ValidationError, match="sha256"):
            parse_charter(bad)

    def test_reject_invalid_kind(self):
        bad = MINIMAL_AGENT_CHARTER.replace("kind: agent_charter", "kind: team_charter")
        with pytest.raises(ValidationError):
            parse_charter(bad)

    def test_reject_sod_rule_one_role(self):
        charter_with_sod = MINIMAL_AGENT_CHARTER.replace(
            "rules: []",
            textwrap.dedent("""\
                rules:
                    - rule_id: sod.bad
                      conflict_type: audit_independence
                      roles: [reviewer]
                      scope: task
                      because: "Only one role."
            """),
        )
        with pytest.raises(ValidationError, match="exactly 2 roles"):
            parse_charter(charter_with_sod)

    def test_reject_sod_rule_same_roles(self):
        charter_with_sod = MINIMAL_AGENT_CHARTER.replace(
            "rules: []",
            textwrap.dedent("""\
                rules:
                    - rule_id: sod.same
                      conflict_type: audit_independence
                      roles: [reader, reader]
                      scope: task
                      because: "Same roles."
            """),
        )
        with pytest.raises(ValidationError, match="distinct"):
            parse_charter(charter_with_sod)

    def test_reject_path_prefix_without_slashes(self):
        charter = MINIMAL_AGENT_CHARTER.replace(
            "entries:\n",
            textwrap.dedent("""\
                entries:
                    - category: path_prefix
                      value: workspace
                      scope: agent
                      scope_id: agent.reader
                      reason: "Bad path."
                      evidence_refs: [ev.bad]
            """),
        )
        with pytest.raises(ValidationError, match="path_prefix must start and end"):
            parse_charter(charter)


# ──────────────────── Semantic validation ─────────────────────────────


class TestValidateCharter:
    def test_valid_minimal_no_errors(self):
        doc = parse_charter(MINIMAL_AGENT_CHARTER)
        errors = validate_charter(doc, raw_yaml=MINIMAL_AGENT_CHARTER)
        assert len(errors) == 0, [str(e) for e in errors]

    def test_agent_count_for_agent_charter(self):
        doc = parse_charter(MINIMAL_AGENT_CHARTER)
        doc.agents.append(doc.agents[0].model_copy(update={"agent_id": "agent.extra"}))
        errors = validate_charter(doc)
        assert any("exactly 1 agent" in e.message for e in errors)

    def test_session_charter_needs_two_agents(self):
        doc = parse_charter(MINIMAL_AGENT_CHARTER)
        doc = doc.model_copy(update={"kind": CharterKind.SESSION})
        errors = validate_charter(doc)
        assert any("at least 2 agents" in e.message for e in errors)

    def test_accepted_requires_hash(self):
        doc = parse_charter(MINIMAL_AGENT_CHARTER)
        doc.metadata.ratification.hash = None
        errors = validate_charter(doc)
        assert any("Hash required" in e.message for e in errors)

    def test_non_draft_same_author_implementer(self):
        doc = parse_charter(MINIMAL_AGENT_CHARTER)
        doc.metadata.authored_by = "agent:implementer"
        errors = validate_charter(doc)
        assert any("authored_by and implemented_by must differ" in e.message for e in errors)

    def test_permissions_default_not_deny(self):
        doc = parse_charter(MINIMAL_AGENT_CHARTER)
        doc.permissions.default = "allow"
        errors = validate_charter(doc)
        assert any("Must be 'deny'" in e.message for e in errors)

    def test_permissions_wrong_specificity(self):
        doc = parse_charter(MINIMAL_AGENT_CHARTER)
        doc.permissions.resolution.specificity_order = ["global", "role"]
        errors = validate_charter(doc)
        assert any("specificity_order" in e.path for e in errors)

    def test_permissions_tie_not_deny(self):
        doc = parse_charter(MINIMAL_AGENT_CHARTER)
        doc.permissions.resolution.tie = "allow"
        errors = validate_charter(doc)
        assert any("tie" in e.path for e in errors)

    def test_duplicate_agent_ids(self):
        doc = parse_charter(MINIMAL_AGENT_CHARTER)
        doc.agents.append(doc.agents[0].model_copy())
        errors = validate_charter(doc)
        assert any("Duplicate agent_id" in e.message for e in errors)

    def test_duplicate_constraint_ids(self):
        doc = parse_charter(MINIMAL_AGENT_CHARTER)
        doc.constraints.append(doc.constraints[0].model_copy())
        errors = validate_charter(doc)
        assert any("Duplicate constraint_id" in e.message for e in errors)

    def test_tool_not_in_registry(self):
        doc = parse_charter(MINIMAL_AGENT_CHARTER)
        doc.agents[0].allowed_tools.append("tool.missing")
        errors = validate_charter(doc)
        assert any("not in registry" in e.message for e in errors)

    def test_sod_role_not_declared(self):
        doc = parse_charter(MINIMAL_AGENT_CHARTER)
        from lionagi.governance.dsl import ConflictType, SodRule, SodScope

        doc.sod.rules.append(
            SodRule(
                rule_id="sod.test",
                conflict_type=ConflictType.AUDIT_INDEPENDENCE,
                roles=["reader", "undeclared_role"],
                scope=SodScope.TASK,
                because="Test.",
            )
        )
        errors = validate_charter(doc)
        assert any("not declared" in e.message for e in errors)

    def test_sod_inactive_for_accepted(self):
        doc = parse_charter(MINIMAL_AGENT_CHARTER)
        doc.sod.active = False
        errors = validate_charter(doc)
        assert any("sod.active" in e.path for e in errors)

    def test_unknown_top_key_detected(self):
        bad_yaml = MINIMAL_AGENT_CHARTER + "custom_block: {}\n"
        try:
            doc = parse_charter(MINIMAL_AGENT_CHARTER)
        except Exception:
            pytest.skip("parse failed")
        errors = validate_charter(doc, raw_yaml=bad_yaml)
        assert any("Unknown top-level key" in e.message for e in errors)

    def test_deep_scan_executable_in_nested(self):
        doc = parse_charter(MINIMAL_AGENT_CHARTER)
        doc.constraints[0].description = "safe description with eval("
        errors = validate_charter(doc)
        assert any("Executable token" in e.message for e in errors)


# ──────────────────── JSON Schema export ──────────────────────────────


class TestJsonSchemaExport:
    def test_schema_is_valid_json(self):
        schema = charter_json_schema()
        assert isinstance(schema, dict)
        json_str = json.dumps(schema)
        assert json.loads(json_str) == schema

    def test_schema_has_title(self):
        schema = charter_json_schema()
        assert "CharterDocument" in schema.get("title", "")

    def test_schema_has_required_fields(self):
        schema = charter_json_schema()
        required = schema.get("required", [])
        for key in [
            "charter_dsl",
            "kind",
            "metadata",
            "agents",
            "registry",
            "constraints",
            "sod",
            "permissions",
            "trace",
        ]:
            assert key in required, f"Missing required key: {key}"

    def test_break_glass_optional(self):
        schema = charter_json_schema()
        required = schema.get("required", [])
        assert "break_glass" not in required

    def test_schema_enum_values(self):
        schema = charter_json_schema()
        defs = schema.get("$defs", {})
        kind_enum = defs.get("CharterKind", {}).get("enum", [])
        assert "agent_charter" in kind_enum
        assert "session_charter" in kind_enum


# ──────────────────── Canonical examples from spec ────────────────────

EXAMPLE_A = textwrap.dedent("""\
    charter_dsl: "0.1"
    kind: agent_charter
    metadata:
      charter_id: charter.simple_reader
      version: "1.0.0"
      status: accepted
      policy_release: policy.gov.v1
      authored_by: human:governance
      implemented_by: agent:implementer
      ratification:
        hash: sha256:1000abcd00000000000000000000000000000000000000000000000000000001
        signed_at: "2026-05-27T00:00:00Z"
    agents:
      - agent_id: agent.simple_reader
        actor_id_source: branch_id
        role: reader
        allowed_models: [openai:gpt-5.4]
        allowed_tools: [tool.read_file]
    registry:
      snapshot: ratification_time
      entries:
        - category: tool
          value: tool.read_file
          scope: agent
          scope_id: agent.simple_reader
          reason: "The reader can inspect approved workspace files."
          evidence_refs: [ev.registry.simple_reader.read_file]
        - category: path_prefix
          value: /workspace/
          scope: agent
          scope_id: agent.simple_reader
          reason: "File access is bounded to the workspace."
          evidence_refs: [ev.registry.simple_reader.workspace]
    constraints:
      - constraint_id: gate.registry.read_file
        description: "Read-file calls must match the ratified registry snapshot."
        gate_id: verify_in_registry
        manager_surface: ActionManager
        enforcement: hard
        attach:
          level: action
          action: tool_call
          tools: [tool.read_file]
        evidence:
          required: [GateResult, ToolCallEvidence]
      - constraint_id: gate.path.workspace
        description: "Read-file paths must stay under the workspace prefix."
        gate_id: enforce_path_prefix
        manager_surface: ActionManager
        enforcement: hard
        attach:
          level: action
          action: tool_call
          tools: [tool.read_file]
        evidence:
          required: [GateResult]
    sod:
      active: true
      rules: []
    permissions:
      default: deny
      resolution:
        specificity_order: [resource, role, tenant, global]
        tie: deny
      allow:
        - rule_id: allow.reader.read_workspace
          scope: role
          roles: [reader]
          action: tool_call
          tools: [tool.read_file]
          resources: [/workspace/]
          requires_evidence: [GateResult, ToolCallEvidence]
          because: "Reader role requires file inspection only."
      deny:
        - rule_id: deny.reader.write_or_shell
          scope: role
          roles: [reader]
          action: tool_call
          tools: [tool.write_file, tool.exec_command]
          because: "Reader role cannot mutate files or run shell commands."
    trace:
      stamp: [charter_id, policy_release, agent_id, role]
      require_spans:
        - governance.operation
        - registry.lookup
        - gate.evaluate
        - evidence.emit
      require_evidence:
        - GateResult
        - ToolCallEvidence
""")


EXAMPLE_B = textwrap.dedent("""\
    charter_dsl: "0.1"
    kind: session_charter
    metadata:
      charter_id: charter.gov_orchestration
      version: "1.0.0"
      status: accepted
      policy_release: policy.gov.v1
      authored_by: human:governance
      implemented_by: agent:implementer
      ratification:
        hash: sha256:2000abcd00000000000000000000000000000000000000000000000000000002
        signed_at: "2026-05-27T00:00:00Z"
    agents:
      - agent_id: agent.orchestrator
        actor_id_source: branch_id
        role: orchestrator
        allowed_models: [openai:gpt-5.4]
        allowed_tools: []
        allowed_operations: [delegate, assign_role]
      - agent_id: agent.researcher
        actor_id_source: branch_id
        role: researcher
        allowed_models: [openai:gpt-5.4]
        allowed_tools: [tool.search, tool.read_file]
      - agent_id: agent.implementer
        actor_id_source: branch_id
        role: implementer
        allowed_models: [openai:gpt-5.4]
        allowed_tools: [tool.read_file, tool.write_file]
      - agent_id: agent.reviewer
        actor_id_source: branch_id
        role: reviewer
        allowed_models: [openai:gpt-5.4]
        allowed_tools: [tool.read_file]
    registry:
      snapshot: ratification_time
      entries:
        - category: tool
          value: tool.search
          scope: role
          scope_id: researcher
          reason: "Researcher must locate source and documentation."
          evidence_refs: [ev.registry.orchestration.search]
        - category: tool
          value: tool.read_file
          scope: session
          scope_id: session.gov_orchestration
          reason: "Researcher, implementer, and reviewer inspect files."
          evidence_refs: [ev.registry.orchestration.read]
        - category: tool
          value: tool.write_file
          scope: role
          scope_id: implementer
          reason: "Only implementer applies code changes."
          evidence_refs: [ev.registry.orchestration.write]
    constraints:
      - constraint_id: gate.registry.all_effects
        description: "Every external effect must resolve through registry policy."
        gate_id: verify_in_registry
        manager_surface: ActionManager
        enforcement: hard
        attach:
          level: class
          tool_class: external_effect
        evidence:
          required: [GateResult]
      - constraint_id: gate.sod.review_independence
        description: "Reviewer must be independent from implementer for the same task."
        gate_id: assert_sod_independence
        manager_surface: ActionManager
        enforcement: hard
        attach:
          level: action
          action: certificate_mint
        evidence:
          required: [SoDCheckEvidence, GateResult]
      - constraint_id: gate.orchestrator.no_task_tools
        description: "Orchestrator coordinates work but does not invoke task tools directly."
        gate_id: deny_direct_tool_call_for_role
        manager_surface: ActionManager
        enforcement: hard
        attach:
          level: action
          action: tool_call
        evidence:
          required: [GateResult]
    sod:
      active: true
      rules:
        - rule_id: sod.implementer_reviewer.independent
          conflict_type: audit_independence
          roles: [implementer, reviewer]
          scope: task
          because: "The same actor cannot both implement and approve the same task."
        - rule_id: sod.grant_requester_approver.split
          conflict_type: approval_chain
          roles: [implementer, orchestrator]
          scope: session
          because: "Grant requester and approver must be distinct actors."
    permissions:
      default: deny
      resolution:
        specificity_order: [resource, role, tenant, global]
        tie: deny
      allow:
        - rule_id: allow.orchestrator.delegate
          scope: role
          roles: [orchestrator]
          action: delegate
          resources: [session.gov_orchestration]
          requires_evidence: [DelegationEvidence]
          because: "Orchestrator manages work assignment only."
        - rule_id: allow.researcher.search_read
          scope: role
          roles: [researcher]
          action: tool_call
          tools: [tool.search, tool.read_file]
          requires_evidence: [GateResult, ToolCallEvidence]
          because: "Researcher may gather source evidence."
        - rule_id: allow.implementer.read_write
          scope: role
          roles: [implementer]
          action: tool_call
          tools: [tool.read_file, tool.write_file]
          requires_evidence: [GateResult, ToolCallEvidence]
          because: "Implementer may inspect and edit files."
        - rule_id: allow.reviewer.read
          scope: role
          roles: [reviewer]
          action: tool_call
          tools: [tool.read_file]
          requires_evidence: [GateResult, ToolCallEvidence]
          because: "Reviewer may inspect results without editing."
      deny:
        - rule_id: deny.orchestrator.task_tools
          scope: role
          roles: [orchestrator]
          action: tool_call
          tools: [tool.search, tool.read_file, tool.write_file]
          because: "Coordination authority is separate from task execution authority."
        - rule_id: deny.reviewer.write
          scope: role
          roles: [reviewer]
          action: tool_call
          tools: [tool.write_file]
          because: "Reviewer role must remain read-only."
    trace:
      stamp: [charter_id, policy_release, agent_id, role, flow_id]
      require_spans:
        - governance.session
        - governance.flow
        - governance.operation
        - sod.check
        - registry.lookup
        - gate.evaluate
        - evidence.emit
        - certificate.state
        - certificate.mint
      require_evidence:
        - GateResult
        - ToolCallEvidence
        - SoDCheckEvidence
        - DelegationEvidence
        - TaskCertificate
""")


EXAMPLE_C = textwrap.dedent("""\
    charter_dsl: "0.1"
    kind: agent_charter
    metadata:
      charter_id: charter.adapter.langgraph_boundary
      version: "1.0.0"
      status: accepted
      policy_release: policy.gov.v1
      authored_by: human:governance
      implemented_by: agent:adapter_owner
      ratification:
        hash: sha256:3000abcd00000000000000000000000000000000000000000000000000000003
        signed_at: "2026-05-27T00:00:00Z"
    agents:
      - agent_id: agent.graph_runner
        actor_id_source: branch_id
        role: adapter_runner
        allowed_models: [openai:gpt-5.4]
        allowed_tools: [adapter.langgraph.invoke]
    registry:
      snapshot: ratification_time
      entries:
        - category: tool
          value: adapter.langgraph.invoke
          scope: agent
          scope_id: agent.graph_runner
          reason: "The existing compiled graph is governed at the invocation boundary."
          evidence_refs: [ev.registry.adapter.langgraph.invoke]
        - category: path_prefix
          value: /workspace/
          scope: agent
          scope_id: agent.graph_runner
          reason: "Adapter inputs and artifacts are bounded to the workspace."
          evidence_refs: [ev.registry.adapter.langgraph.workspace]
    constraints:
      - constraint_id: gate.adapter.boundary_registry
        description: "The adapter invocation must be registered before execution."
        gate_id: verify_in_registry
        manager_surface: ActionManager
        enforcement: hard
        attach:
          level: action
          action: tool_call
          tools: [adapter.langgraph.invoke]
        evidence:
          required: [GateResult, AdapterInvocationEvidence]
      - constraint_id: gate.adapter.input_contract
        description: "Adapter input must pass the graph boundary schema."
        gate_id: validate_adapter_input
        manager_surface: ActionManager
        enforcement: hard
        attach:
          level: action
          action: tool_call
          tools: [adapter.langgraph.invoke]
        evidence:
          required: [GateResult]
      - constraint_id: gate.adapter.no_internal_claim
        description: "Coarse boundary mode must not emit per-internal-tool governed evidence."
        gate_id: assert_boundary_only_evidence
        manager_surface: DataLogger
        enforcement: hard
        attach:
          level: action
          action: evidence_emit
        evidence:
          required: [AdapterInvocationEvidence]
    sod:
      active: true
      rules: []
    permissions:
      default: deny
      resolution:
        specificity_order: [resource, role, tenant, global]
        tie: deny
      allow:
        - rule_id: allow.adapter_runner.invoke_graph
          scope: role
          roles: [adapter_runner]
          action: tool_call
          tools: [adapter.langgraph.invoke]
          requires_evidence: [GateResult, AdapterInvocationEvidence]
          because: "Adapter runner may invoke the wrapped graph boundary."
      deny:
        - rule_id: deny.adapter_runner.raw_internal_tools
          scope: role
          roles: [adapter_runner]
          action: tool_call
          tools: [tool.internal_graph_tool]
          because: "Internal graph tools are not governed in coarse boundary mode."
    trace:
      stamp: [charter_id, policy_release, agent_id, role, adapter_mode]
      require_spans:
        - governance.operation
        - registry.lookup
        - gate.evaluate
        - evidence.emit
      require_evidence:
        - GateResult
        - AdapterInvocationEvidence
""")


class TestCanonicalExamples:
    def test_example_a_parses(self):
        doc = parse_charter(EXAMPLE_A)
        assert doc.metadata.charter_id == "charter.simple_reader"
        assert doc.kind == CharterKind.AGENT
        assert len(doc.agents) == 1
        assert len(doc.constraints) == 2
        assert len(doc.registry.entries) == 2

    def test_example_a_validates(self):
        doc = parse_charter(EXAMPLE_A)
        errors = validate_charter(doc, raw_yaml=EXAMPLE_A)
        assert len(errors) == 0, [str(e) for e in errors]

    def test_example_b_parses(self):
        doc = parse_charter(EXAMPLE_B)
        assert doc.metadata.charter_id == "charter.gov_orchestration"
        assert doc.kind == CharterKind.SESSION
        assert len(doc.agents) == 4
        assert len(doc.sod.rules) == 2
        assert len(doc.constraints) == 3

    def test_example_b_validates(self):
        doc = parse_charter(EXAMPLE_B)
        errors = validate_charter(doc, raw_yaml=EXAMPLE_B)
        assert len(errors) == 0, [str(e) for e in errors]

    def test_example_c_parses(self):
        doc = parse_charter(EXAMPLE_C)
        assert doc.metadata.charter_id == "charter.adapter.langgraph_boundary"
        assert doc.kind == CharterKind.AGENT
        assert len(doc.constraints) == 3
        assert doc.constraints[2].manager_surface == ManagerSurface.DATALOGGER

    def test_example_c_validates(self):
        doc = parse_charter(EXAMPLE_C)
        errors = validate_charter(doc, raw_yaml=EXAMPLE_C)
        assert len(errors) == 0, [str(e) for e in errors]


# ──────────────────── Break-glass model ───────────────────────────────


class TestBreakGlass:
    BREAK_GLASS_CHARTER = textwrap.dedent("""\
        charter_dsl: "0.1"
        kind: agent_charter
        metadata:
          charter_id: charter.breakglass.test
          version: "1.0.0"
          status: accepted
          policy_release: policy.gov.v1
          authored_by: human:governance
          implemented_by: agent:implementer
          ratification:
            hash: sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
            signed_at: "2026-05-27T00:00:00Z"
        agents:
          - agent_id: agent.support
            actor_id_source: branch_id
            role: support
            allowed_models: [openai:gpt-5.4]
            allowed_tools: [tool.read_file]
        registry:
          snapshot: ratification_time
          entries:
            - category: tool
              value: tool.read_file
              scope: agent
              scope_id: agent.support
              reason: "Support reads files."
              evidence_refs: [ev.registry.support.read]
        constraints:
          - constraint_id: gate.registry.support
            description: "Support tool calls match registry."
            gate_id: verify_in_registry
            manager_surface: ActionManager
            enforcement: hard
            attach:
              level: action
              action: tool_call
              tools: [tool.read_file]
            evidence:
              required: [GateResult]
        sod:
          active: true
          rules: []
        permissions:
          default: deny
          resolution:
            specificity_order: [resource, role, tenant, global]
            tie: deny
          allow:
            - rule_id: allow.support.read
              scope: role
              roles: [support]
              action: tool_call
              tools: [tool.read_file]
              requires_evidence: [GateResult]
              because: "Support reads for diagnostics."
          deny:
            - rule_id: deny.support.write
              scope: role
              roles: [support]
              action: tool_call
              tools: [tool.write_file]
              because: "Support cannot write."
        break_glass:
          enabled: true
          expires_after: 15m
          attestation:
            approver_role: oncall_lead
            requires_reason: true
          temporary_grants: [tool.write_file]
          notifications:
            - target: oncall_channel
              on_events: [open, close, expire]
          evidence:
            required: [BreakGlassEvent]
        trace:
          stamp: [charter_id, policy_release, agent_id, role]
          require_spans:
            - governance.operation
            - gate.evaluate
            - evidence.emit
            - breakglass.open
          require_evidence:
            - GateResult
            - BreakGlassEvent
    """)

    def test_break_glass_parses(self):
        doc = parse_charter(self.BREAK_GLASS_CHARTER)
        assert doc.break_glass is not None
        assert doc.break_glass.enabled is True
        assert doc.break_glass.expires_after == "15m"
        assert doc.break_glass.attestation.approver_role == "oncall_lead"
        assert len(doc.break_glass.temporary_grants) == 1

    def test_break_glass_validates(self):
        doc = parse_charter(self.BREAK_GLASS_CHARTER)
        errors = validate_charter(doc, raw_yaml=self.BREAK_GLASS_CHARTER)
        assert len(errors) == 0, [str(e) for e in errors]

    def test_break_glass_exceeds_30m(self):
        bad = self.BREAK_GLASS_CHARTER.replace("expires_after: 15m", "expires_after: 1h")
        with pytest.raises(ValidationError, match="30m"):
            parse_charter(bad)

    def test_break_glass_wildcard_grant_rejected(self):
        bad = self.BREAK_GLASS_CHARTER.replace(
            "temporary_grants: [tool.write_file]",
            "temporary_grants: [tool.*]",
        )
        with pytest.raises(ValidationError, match="Wildcards"):
            parse_charter(bad)


# ──────────────────── Round-trip and serialization ────────────────────


class TestRoundTrip:
    def test_model_dump_and_validate(self):
        doc = parse_charter(MINIMAL_AGENT_CHARTER)
        data = doc.model_dump(mode="python")
        doc2 = CharterDocument.model_validate(data)
        assert doc2.metadata.charter_id == doc.metadata.charter_id
        assert doc2.charter_dsl == doc.charter_dsl

    def test_json_round_trip(self):
        doc = parse_charter(MINIMAL_AGENT_CHARTER)
        json_str = doc.model_dump_json()
        doc2 = CharterDocument.model_validate_json(json_str)
        assert doc2.metadata.charter_id == doc.metadata.charter_id

    def test_yaml_dump_and_reparse(self):
        import yaml as _yaml

        doc = parse_charter(MINIMAL_AGENT_CHARTER)
        dumped = doc.model_dump(mode="json")
        yaml_text = _yaml.dump(dumped, default_flow_style=False, allow_unicode=True)
        doc2 = parse_charter(yaml_text)
        assert doc2.metadata.charter_id == doc.metadata.charter_id
        assert doc2.charter_dsl == doc.charter_dsl
        assert doc2.kind == doc.kind


# ──────────────────── Enforcement normalization ───────────────────────


class TestEnforcementNormalization:
    def test_lowercase_hard_normalizes(self):
        doc = parse_charter(MINIMAL_AGENT_CHARTER)  # uses enforcement: hard
        assert doc.constraints[0].enforcement == Enforcement.HARD
        assert doc.constraints[0].enforcement.value == "HARD"

    def test_mixed_case_Hard_normalizes(self):
        yaml_text = MINIMAL_AGENT_CHARTER.replace("enforcement: hard", "enforcement: Hard")
        doc = parse_charter(yaml_text)
        assert doc.constraints[0].enforcement == Enforcement.HARD
        assert doc.constraints[0].enforcement.value == "HARD"

    def test_uppercase_HARD_normalizes(self):
        yaml_text = MINIMAL_AGENT_CHARTER.replace("enforcement: hard", "enforcement: HARD")
        doc = parse_charter(yaml_text)
        assert doc.constraints[0].enforcement == Enforcement.HARD
        assert doc.constraints[0].enforcement.value == "HARD"

    def test_soft_normalization(self):
        yaml_text = MINIMAL_AGENT_CHARTER.replace("enforcement: hard", "enforcement: soft")
        doc = parse_charter(yaml_text)
        assert doc.constraints[0].enforcement == Enforcement.SOFT
        assert doc.constraints[0].enforcement.value == "SOFT"

    def test_advisory_normalization(self):
        yaml_text = MINIMAL_AGENT_CHARTER.replace("enforcement: hard", "enforcement: Advisory")
        doc = parse_charter(yaml_text)
        assert doc.constraints[0].enforcement == Enforcement.ADVISORY
        assert doc.constraints[0].enforcement.value == "ADVISORY"


# ──────────────────── CharterParser class ─────────────────────────────


class TestCharterParser:
    def test_parse_returns_document(self):
        from lionagi.governance.charter import CharterParser

        doc = CharterParser.parse(MINIMAL_AGENT_CHARTER)
        assert isinstance(doc, CharterDocument)
        assert doc.charter_dsl == "0.1"

    def test_schema_returns_dict_with_properties(self):
        from lionagi.governance.charter import CharterParser

        schema = CharterParser.schema()
        assert isinstance(schema, dict)
        assert "properties" in schema

    def test_legacy_apiVersion_rejected(self):
        from lionagi.governance.charter import (
            CharterParseError,
            CharterParser,
        )

        yaml_text = "apiVersion: v1\n" + MINIMAL_AGENT_CHARTER
        with pytest.raises(CharterParseError, match="Legacy format"):
            CharterParser.parse(yaml_text)

    def test_legacy_apiVersion_error_includes_migrate_hint(self):
        from lionagi.governance.charter import (
            CharterParseError,
            CharterParser,
        )

        yaml_text = "apiVersion: v1\n" + MINIMAL_AGENT_CHARTER
        with pytest.raises(CharterParseError) as exc_info:
            CharterParser.parse(yaml_text)
        assert "charter.version" in str(exc_info.value)

    def test_wildcard_raises_charter_parse_error(self):
        from lionagi.governance.charter import (
            CharterParseError,
            CharterParser,
        )

        bad = MINIMAL_AGENT_CHARTER.replace(
            "allowed_tools: [tool.read_file]", "allowed_tools: [tool.*]"
        )
        with pytest.raises(CharterParseError, match="Wildcards"):
            CharterParser.parse(bad)

    def test_tab_raises_charter_parse_error(self):
        from lionagi.governance.charter import (
            CharterParseError,
            CharterParser,
        )

        bad = 'charter_dsl: "0.1"\n\tkind: agent_charter\n'
        with pytest.raises(CharterParseError, match="Tabs"):
            CharterParser.parse(bad)

    def test_parse_example_a(self):
        from lionagi.governance.charter import CharterParser

        doc = CharterParser.parse(EXAMPLE_A)
        assert doc.metadata.charter_id == "charter.simple_reader"

    def test_import_from_package(self):
        from lionagi.governance import CharterParseError, CharterParser

        assert CharterParser is not None
        assert CharterParseError is not None

    def test_prose_field_with_question_mark_accepted(self):
        from lionagi.governance import CharterParser

        # '?' in a prose 'because' field must not trigger wildcard rejection
        yaml_text = MINIMAL_AGENT_CHARTER.replace(
            '"Reader needs file access."',
            '"Why not allow reading? It is perfectly safe."',
        )
        doc = CharterParser.parse(yaml_text)
        assert "?" in doc.permissions.allow[0].because

    def test_dsl_module_has_no_top_level_yaml_import(self):
        import ast
        import inspect

        import lionagi.governance.dsl as dsl_mod

        # Read source via file path to avoid sys.modules manipulation
        # that would poison other test modules' import caches.
        src_path = inspect.getsourcefile(dsl_mod)
        if src_path is None:
            src = inspect.getsource(dsl_mod)
        else:
            with open(src_path) as fh:
                src = fh.read()
        tree = ast.parse(src)
        top_imports = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Import | ast.ImportFrom)
            and isinstance(getattr(n, "col_offset", 1), int)
            and n.col_offset == 0
        ]
        yaml_at_top = any(
            "yaml" in (getattr(alias, "name", "") or getattr(n, "module", "") or "")
            for n in top_imports
            for alias in getattr(n, "names", [ast.alias(name="", asname=None)])
        )
        assert not yaml_at_top, "yaml must not be imported at top level of dsl.py"


# ──────────────── Constraint binding exclusivity ──────────────────────


_EXCLUSIVITY_CHARTER = textwrap.dedent("""\
    charter_dsl: "0.1"
    kind: agent_charter
    metadata:
      charter_id: charter.test.exclusivity
      version: "1.0.0"
      status: draft
      policy_release: policy.gov.v1
      authored_by: human:governance
      implemented_by: agent:implementer
      ratification:
        hash: null
        signed_at: null
    agents:
      - agent_id: agent.reader
        actor_id_source: branch_id
        role: reader
        allowed_models: [openai:gpt-5.4]
        allowed_tools: [tool.read_file]
    registry:
      snapshot: ratification_time
      entries:
        - category: tool
          value: tool.read_file
          scope: agent
          scope_id: agent.reader
          reason: "Reader reads files."
          evidence_refs: [ev.registry.reader]
    constraints:
      - constraint_id: gate.read_file
        description: "Gate on read-file."
        gate_id: verify_in_registry
        manager_surface: ActionManager
        enforcement: hard
        attach:
          level: action
          action: tool_call
          tools: [tool.read_file]
        evidence:
          required: [GateResult]
      - constraint_id: hook.read_file
        description: "Hook also on read-file — exclusivity violation."
        hook_name: pre_read
        hook_phase: pre
        manager_surface: ActionManager
        enforcement: hard
        attach:
          level: action
          action: tool_call
          tools: [tool.read_file]
        evidence:
          required: [GateResult]
    sod:
      active: true
      rules: []
    permissions:
      default: deny
      resolution:
        specificity_order: [resource, role, tenant, global]
        tie: deny
      allow:
        - rule_id: allow.reader.read
          scope: role
          roles: [reader]
          action: tool_call
          tools: [tool.read_file]
          requires_evidence: [GateResult]
          because: "Reader needs file access."
      deny:
        - rule_id: deny.reader.write
          scope: role
          roles: [reader]
          action: tool_call
          tools: [tool.write_file]
          because: "Reader cannot write."
    trace:
      stamp: [charter_id]
      require_spans: [governance.operation]
      require_evidence: [GateResult]
""")


class TestConstraintBindingExclusivity:
    def test_gate_and_hook_overlap_is_error(self):
        doc = parse_charter(_EXCLUSIVITY_CHARTER)
        errors = validate_charter(doc)
        assert any(
            "exclusivity" in e.message.lower() or "gate-bound and hook-bound" in e.message
            for e in errors
        ), f"Expected exclusivity error, got: {[str(e) for e in errors]}"

    def test_gate_only_no_exclusivity_error(self):
        doc = parse_charter(MINIMAL_AGENT_CHARTER)
        errors = validate_charter(doc)
        exclusivity_errors = [
            e
            for e in errors
            if "exclusivity" in e.message.lower() or "gate-bound and hook-bound" in e.message
        ]
        assert len(exclusivity_errors) == 0

    def test_no_tool_overlap_no_error(self):
        doc = parse_charter(EXAMPLE_B)
        errors = validate_charter(doc, raw_yaml=EXAMPLE_B)
        exclusivity_errors = [e for e in errors if "exclusivity" in e.message.lower()]
        assert len(exclusivity_errors) == 0


# ──────────────────── Hypothesis property test ────────────────────────


try:
    from hypothesis import HealthCheck, given, settings
    from hypothesis import strategies as st

    _HAS_HYPOTHESIS = True
except ImportError:
    _HAS_HYPOTHESIS = False

if _HAS_HYPOTHESIS:

    @st.composite
    def _valid_charter_strategy(draw: st.DrawFn) -> CharterDocument:
        tool_suffix = draw(st.from_regex(r"[a-z][a-z0-9_]{1,8}", fullmatch=True))
        agent_suffix = draw(st.from_regex(r"[a-z][a-z0-9_]{1,8}", fullmatch=True))
        tool_id = f"tool.{tool_suffix}"
        agent_id = f"agent.{agent_suffix}"
        enforcement = draw(st.sampled_from(["hard", "Hard", "HARD", "soft", "SOFT", "advisory"]))

        data: dict = {
            "charter_dsl": "0.1",
            "kind": "agent_charter",
            "metadata": {
                "charter_id": f"charter.hyp.{agent_suffix}",
                "version": "1.0.0",
                "status": "draft",
                "policy_release": "policy.hyp.v1",
                "authored_by": "human:tester",
                "implemented_by": "agent:tester",
                "ratification": {"hash": None, "signed_at": None},
            },
            "agents": [
                {
                    "agent_id": agent_id,
                    "actor_id_source": "branch_id",
                    "role": "tester",
                    "allowed_models": ["test:model"],
                    "allowed_tools": [tool_id],
                }
            ],
            "registry": {
                "snapshot": "ratification_time",
                "entries": [
                    {
                        "category": "tool",
                        "value": tool_id,
                        "scope": "agent",
                        "scope_id": agent_id,
                        "reason": "Test tool registration.",
                        "evidence_refs": ["ev.hyp.tool"],
                    }
                ],
            },
            "constraints": [
                {
                    "constraint_id": "gate.hyp.test",
                    "description": "Hypothesis test gate.",
                    "gate_id": "hyp_gate",
                    "manager_surface": "ActionManager",
                    "enforcement": enforcement,
                    "attach": {
                        "level": "action",
                        "action": "tool_call",
                        "tools": [tool_id],
                    },
                    "evidence": {"required": ["GateResult"]},
                }
            ],
            "sod": {"active": True, "rules": []},
            "permissions": {
                "default": "deny",
                "resolution": {
                    "specificity_order": ["resource", "role", "tenant", "global"],
                    "tie": "deny",
                },
                "allow": [
                    {
                        "rule_id": "allow.hyp.tester",
                        "scope": "role",
                        "roles": ["tester"],
                        "action": "tool_call",
                        "tools": [tool_id],
                        "requires_evidence": ["GateResult"],
                        "because": "Hypothesis tester needs access.",
                    }
                ],
                "deny": [
                    {
                        "rule_id": "deny.hyp.tester",
                        "scope": "role",
                        "roles": ["tester"],
                        "action": "tool_call",
                        "tools": ["tool.forbidden_hyp"],
                        "because": "Hypothesis tester cannot use forbidden tools.",
                    }
                ],
            },
            "trace": {
                "stamp": ["charter_id"],
                "require_spans": ["governance.operation"],
                "require_evidence": ["GateResult"],
            },
        }
        return CharterDocument.model_validate(data)

    @given(_valid_charter_strategy())
    @settings(max_examples=25, suppress_health_check=[HealthCheck.too_slow])
    def test_hypothesis_round_trip(charter: CharterDocument) -> None:
        """Serialize to JSON dict and re-parse; structural identity must hold."""
        dumped = charter.model_dump(mode="json")
        restored = CharterDocument.model_validate(dumped)
        assert restored.metadata.charter_id == charter.metadata.charter_id
        assert restored.charter_dsl == charter.charter_dsl
        assert restored.kind == charter.kind
        assert len(restored.agents) == len(charter.agents)
        assert restored.agents[0].agent_id == charter.agents[0].agent_id
        assert restored.constraints[0].enforcement.value == "HARD" or restored.constraints[
            0
        ].enforcement.value in ("SOFT", "ADVISORY")
