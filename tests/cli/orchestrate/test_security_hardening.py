# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for CLI security hardening: spec validation, path containment, topo sort."""

import argparse
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import yaml

from lionagi.cli.orchestrate import (
    _validate_spec_fields,
    add_orchestrate_subparser,
    run_orchestrate,
)
from lionagi.cli.orchestrate.flow import (
    FlowAgent,
    FlowOp,
    FlowPlan,
    _run_flow_inner,
    _topo_sort_ops,
)


def _parse_flow_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="li")
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_orchestrate_subparser(subparsers)
    return parser.parse_args(["o", "flow", *argv])


def _op(oid: str, deps: list[str] | None = None) -> FlowOp:
    return FlowOp(id=oid, agent_id="a1", instruction="do the thing", depends_on=deps)


# ── Spec field validation ─────────────────────────────────────────────────────


class TestSpecValidationRejectsBadTypes:
    def test_workers_as_string(self):
        err = _validate_spec_fields({"workers": "8"})
        assert err is not None
        assert "workers" in err

    def test_workers_negative(self):
        err = _validate_spec_fields({"workers": -1})
        assert err is not None
        assert "workers" in err

    def test_workers_zero(self):
        err = _validate_spec_fields({"workers": 0})
        assert err is not None

    def test_workers_too_large(self):
        err = _validate_spec_fields({"workers": 999999999})
        assert err is not None
        assert "workers" in err

    def test_workers_bool_rejected(self):
        # bool is a subclass of int in Python — must be rejected
        err = _validate_spec_fields({"workers": True})
        assert err is not None

    def test_max_agents_as_string(self):
        err = _validate_spec_fields({"max_agents": "10"})
        assert err is not None
        assert "max_agents" in err

    def test_max_agents_too_large(self):
        err = _validate_spec_fields({"max_agents": 51})
        assert err is not None

    def test_effort_invalid_value(self):
        err = _validate_spec_fields({"effort": "extreme"})
        assert err is not None
        assert "effort" in err

    def test_effort_accepts_all_provider_levels(self):
        # Spec validation must match cli/_providers.py EFFORT_LEVELS so
        # playbooks can't be rejected for values the CLI itself accepts.
        for level in ("none", "minimal", "low", "medium", "high", "xhigh", "max"):
            assert _validate_spec_fields({"effort": level}) is None, level

    def test_effort_as_int(self):
        err = _validate_spec_fields({"effort": 3})
        assert err is not None

    def test_bare_as_string(self):
        err = _validate_spec_fields({"bare": "true"})
        assert err is not None
        assert "bare" in err

    def test_dry_run_as_int(self):
        err = _validate_spec_fields({"dry_run": 1})
        assert err is not None

    def test_with_synthesis_accepts_string_model_spec(self):
        # `--with-synthesis [MODEL]` takes an optional model spec; spec
        # validation must accept both bool and str for parity.
        assert _validate_spec_fields({"with_synthesis": True}) is None
        assert _validate_spec_fields({"with_synthesis": False}) is None
        assert _validate_spec_fields({"with_synthesis": "claude-code/opus-4-7"}) is None

    def test_with_synthesis_rejects_non_bool_non_str(self):
        err = _validate_spec_fields({"with_synthesis": [1, 2]})
        assert err is not None
        assert "with_synthesis" in err
        err = _validate_spec_fields({"with_synthesis": 42})
        assert err is not None

    def test_prompt_too_long(self):
        err = _validate_spec_fields({"prompt": "x" * 8193})
        assert err is not None
        assert "prompt" in err

    def test_prompt_as_int(self):
        err = _validate_spec_fields({"prompt": 42})
        assert err is not None

    def test_save_as_int(self):
        err = _validate_spec_fields({"save": 123})
        assert err is not None

    def test_model_as_int(self):
        err = _validate_spec_fields({"model": 42})
        assert err is not None

    def test_agent_as_list(self):
        err = _validate_spec_fields({"agent": ["a"]})
        assert err is not None

    def test_team_mode_as_bool(self):
        err = _validate_spec_fields({"team_mode": True})
        assert err is not None

    # ── Present-null values must be rejected (YAML `null` → Python None) ──

    def test_workers_null_rejected(self):
        err = _validate_spec_fields({"workers": None})
        assert err is not None
        assert "workers" in err

    def test_max_agents_null_rejected(self):
        err = _validate_spec_fields({"max_agents": None})
        assert err is not None
        assert "max_agents" in err

    def test_bare_null_rejected(self):
        err = _validate_spec_fields({"bare": None})
        assert err is not None
        assert "bare" in err

    def test_dry_run_null_rejected(self):
        err = _validate_spec_fields({"dry_run": None})
        assert err is not None

    def test_with_synthesis_null_rejected(self):
        err = _validate_spec_fields({"with_synthesis": None})
        assert err is not None

    def test_prompt_null_rejected(self):
        err = _validate_spec_fields({"prompt": None})
        assert err is not None
        assert "prompt" in err

    def test_save_null_rejected(self):
        err = _validate_spec_fields({"save": None})
        assert err is not None
        assert "save" in err

    def test_model_null_rejected(self):
        err = _validate_spec_fields({"model": None})
        assert err is not None
        assert "model" in err

    def test_agent_null_rejected(self):
        err = _validate_spec_fields({"agent": None})
        assert err is not None

    def test_team_mode_null_rejected(self):
        err = _validate_spec_fields({"team_mode": None})
        assert err is not None


class TestSpecValidationAcceptsValidFields:
    def test_empty_spec(self):
        assert _validate_spec_fields({}) is None

    def test_valid_workers(self):
        assert _validate_spec_fields({"workers": 8}) is None

    def test_workers_boundary_values(self):
        assert _validate_spec_fields({"workers": 1}) is None
        assert _validate_spec_fields({"workers": 32}) is None

    def test_valid_max_agents(self):
        assert _validate_spec_fields({"max_agents": 12}) is None

    def test_max_agents_boundary_values(self):
        assert _validate_spec_fields({"max_agents": 1}) is None
        assert _validate_spec_fields({"max_agents": 50}) is None

    def test_max_ops_zero_means_unlimited(self):
        # CLI help documents `--max-ops 0` (and `--max-agents 0`) as
        # "unlimited" — spec validation must accept 0 to honor that contract.
        assert _validate_spec_fields({"max_ops": 0}) is None
        assert _validate_spec_fields({"max_agents": 0}) is None

    def test_valid_effort_values(self):
        for effort in ("low", "medium", "high", "xhigh"):
            assert _validate_spec_fields({"effort": effort}) is None

    def test_effort_null_accepted(self):
        # effort: null means "use profile default" — explicitly allowed
        assert _validate_spec_fields({"effort": None}) is None

    def test_valid_booleans(self):
        assert (
            _validate_spec_fields({"bare": True, "dry_run": False, "with_synthesis": True}) is None
        )

    def test_valid_prompt(self):
        assert _validate_spec_fields({"prompt": "Do the thing"}) is None

    def test_prompt_at_max_length(self):
        assert _validate_spec_fields({"prompt": "x" * 8192}) is None

    def test_valid_string_fields(self):
        spec = {
            "save": "./results",
            "model": "claude-code/opus-4-7",
            "agent": "orchestrator",
            "team_mode": "ws-terminal",
        }
        assert _validate_spec_fields(spec) is None

    def test_full_valid_spec(self):
        spec = {
            "agent": "orchestrator",
            "workers": 8,
            "max_agents": 20,
            "effort": "xhigh",
            "bare": False,
            "dry_run": False,
            "with_synthesis": True,
            "prompt": "Build the thing",
            "save": "./out",
            "model": "claude-code/opus-4-7",
            "team_mode": "ws-terminal",
        }
        assert _validate_spec_fields(spec) is None

    def test_run_orchestrate_rejects_bad_spec(self, tmp_path, caplog):
        spec_file = tmp_path / "bad.yaml"
        spec_file.write_text(
            yaml.dump({"model": "claude/opus", "workers": "not-an-int", "prompt": "hi"})
        )
        args = _parse_flow_args(["-f", str(spec_file)])
        code = run_orchestrate(args)
        assert code == 1
        assert "workers" in caplog.text

    def test_run_orchestrate_rejects_null_workers(self, tmp_path, caplog):
        # YAML `workers: null` is a present field with NoneType — must be rejected
        spec_file = tmp_path / "null_workers.yaml"
        spec_file.write_text("model: claude/opus\nprompt: hi\nworkers: null\n")
        args = _parse_flow_args(["-f", str(spec_file)])
        code = run_orchestrate(args)
        assert code == 1
        assert "workers" in caplog.text


# ── Save path containment ─────────────────────────────────────────────────────


class TestSavePathContainment:
    def test_save_path_rejects_escape(self, tmp_path, caplog):
        escape_path = str(Path.home().parent / "li_sec_test_escape_dir")
        spec_file = tmp_path / "spec.yaml"
        spec_file.write_text(
            yaml.dump({"model": "claude/opus", "prompt": "task", "save": escape_path})
        )
        args = _parse_flow_args(["-f", str(spec_file)])

        with patch(
            "lionagi.cli.orchestrate._run_flow",
            AsyncMock(return_value="should not reach"),
        ) as run_flow:
            code = run_orchestrate(args)

        assert code == 1
        assert "escapes allowed roots" in caplog.text
        run_flow.assert_not_called()

    def test_save_path_accepts_relative_subdirectory(self, tmp_path, caplog):
        # Use a true cwd-relative path — exercises the Path.cwd() branch of containment.
        save_dir = "./li_sec_test_output_hardening"
        spec_file = tmp_path / "spec.yaml"
        spec_file.write_text(
            yaml.dump({"model": "claude/opus", "prompt": "task", "save": save_dir})
        )
        args = _parse_flow_args(["-f", str(spec_file)])

        with patch(
            "lionagi.cli.orchestrate._run_flow",
            AsyncMock(return_value="flow done"),
        ) as run_flow:
            code = run_orchestrate(args)

        assert code == 0
        run_flow.assert_called_once()


# ── Topo sort: 1000-deep chain and >200 ops ───────────────────────────────────


def test_topo_sort_1000_deep_chain_no_crash():
    """Iterative Kahn's BFS handles a 1000-op linear chain without stack overflow."""
    n = 1000
    ops = [_op("op0")]
    for i in range(1, n):
        ops.append(_op(f"op{i}", [f"op{i - 1}"]))
    result = _topo_sort_ops(ops)
    assert len(result) == n
    for i in range(n):
        assert result[i].id == f"op{i}"


def test_topo_sort_no_cap_on_op_count():
    """_topo_sort_ops imposes no size cap; the 200-op limit lives in _run_flow_inner."""
    ops = [_op(f"op{i}") for i in range(201)]
    result = _topo_sort_ops(ops)
    assert len(result) == 201


# ── _run_flow_inner rejects >200 op plans ────────────────────────────────────


class _FakeBuilder:
    def __init__(self):
        self.added = []

    def add_operation(self, operation, **kwargs):
        node_id = f"node-{len(self.added) + 1}"
        self.added.append({"id": node_id, "operation": operation, "kwargs": kwargs})
        return node_id

    def get_graph(self):
        return object()


class _FakeSession:
    def __init__(self, builder, plan):
        self.builder = builder
        self.plan = plan

    async def flow(self, _graph, **_kwargs):
        plan_root = self.builder.added[0]["id"]
        return {"operation_results": {plan_root: SimpleNamespace(plan=self.plan)}}


@pytest.mark.asyncio
async def test_run_flow_inner_rejects_plan_over_200_ops(tmp_path):
    agents = [FlowAgent(id="a1", role="researcher")]
    operations = [_op(f"op{i}") for i in range(201)]
    plan = FlowPlan(agents=agents, operations=operations)
    builder = _FakeBuilder()
    env = SimpleNamespace(
        run=SimpleNamespace(
            artifact_root=tmp_path,
            dag_image_path=tmp_path / "dag.png",
        ),
        session=_FakeSession(builder, plan),
        orc_branch=SimpleNamespace(id=uuid4()),
        builder=builder,
        bare=True,
        effort=None,
        verbose=False,
        team_data=None,
    )

    output = await _run_flow_inner(
        "codex/gpt-5.5",
        "do the task",
        env=env,
        dry_run=True,
    )

    assert "200" in output or "Invalid plan" in output


# ── ADR-0029: artifacts: field validation in _validate_spec_fields ────────────


class TestArtifactsFieldValidation:
    def test_valid_artifacts_passes(self):
        spec = {
            "model": "codex/gpt-4o",
            "prompt": "do it",
            "artifacts": {"expected": [{"id": "report", "path": "report.md"}]},
        }
        assert _validate_spec_fields(spec) is None

    def test_none_artifacts_rejected(self):
        spec = {"model": "x", "prompt": "p", "artifacts": None}
        err = _validate_spec_fields(spec)
        assert err is not None
        assert "artifacts" in err

    def test_absolute_path_in_artifacts_rejected(self):
        spec = {
            "model": "x",
            "prompt": "p",
            "artifacts": {"expected": [{"id": "x", "path": "/etc/passwd"}]},
        }
        err = _validate_spec_fields(spec)
        assert err is not None
        assert "artifacts" in err.lower() or "absolute" in err.lower()

    def test_glob_in_artifact_path_rejected(self):
        spec = {
            "model": "x",
            "prompt": "p",
            "artifacts": {"expected": [{"id": "x", "path": "*.md"}]},
        }
        err = _validate_spec_fields(spec)
        assert err is not None

    def test_duplicate_id_in_artifacts_rejected(self):
        spec = {
            "model": "x",
            "prompt": "p",
            "artifacts": {
                "expected": [
                    {"id": "report", "path": "report.md"},
                    {"id": "report", "path": "other.md"},
                ]
            },
        }
        err = _validate_spec_fields(spec)
        assert err is not None

    def test_non_bool_required_in_artifacts_rejected(self):
        spec = {
            "model": "x",
            "prompt": "p",
            "artifacts": {"expected": [{"id": "x", "path": "x.md", "required": "yes"}]},
        }
        err = _validate_spec_fields(spec)
        assert err is not None
