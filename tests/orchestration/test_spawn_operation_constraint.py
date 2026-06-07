# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Attack-driven regression tests for role_node_builder spawn operation constraint.

Issue (Blocker, security): role_node_builder passed req.operation directly to
create_operation without validating against the allowlist, enabling model output
to route spawned work to any registered session operation.

Fix: Defense-in-depth guard in role_node_builder falls back to 'operate' and
emits a warning when the operation is outside _SPAWN_ALLOWED_OPERATIONS.
The primary guard is SpawnRequest being typed as Literal[...], but this
routing-level check survives if the Literal enforcement is bypassed (e.g.
via a constructed SpawnRequest with model_construct).
"""

from __future__ import annotations

import inspect
import logging

import pytest

from lionagi.casts.emission import _SPAWN_ALLOWED_OPERATIONS, SpawnRequest
from lionagi.orchestration.patterns import role_node_builder
from lionagi.session.branch import Branch
from lionagi.session.session import Session


def _make_roles(*names: str) -> dict[str, Branch]:
    session = Session()
    roles: dict[str, Branch] = {}
    for n in names:
        b = Branch(name=n)
        session.include_branches(b)
        roles[n] = b
    return roles


class TestRoleNodeBuilderOperationConstraint:
    """The routing boundary must not pass untrusted operation names to create_operation."""

    @pytest.mark.parametrize("op", sorted(_SPAWN_ALLOWED_OPERATIONS))
    def test_allowed_operations_pass_through(self, op: str):
        """Documented operations must work unchanged after the guard."""
        roles = _make_roles("researcher")
        nb = role_node_builder(roles)
        req = SpawnRequest(instruction="do work", operation=op)
        node = nb(req, None)
        assert node.operation == op

    def test_bypass_via_model_construct_falls_back_to_operate(self, caplog):
        """If SpawnRequest is constructed bypassing Literal validation
        (model_construct, deserialization hack), the routing guard must
        still catch the unknown operation and fall back to 'operate'.

        This is the attack regression: SpawnRequest(operation='dangerous')
        must NOT execute the 'dangerous' session operation.
        """
        roles = _make_roles("researcher")
        nb = role_node_builder(roles)

        # Bypass Pydantic validation to simulate a constructed object that
        # somehow carries an unauthorized operation name.
        bad_req = SpawnRequest.model_construct(instruction="pwn", operation="dangerous")

        with caplog.at_level(logging.WARNING, logger="lionagi.orchestration.patterns"):
            node = nb(bad_req, None)

        # Guard must fall back to the safe default
        assert node.operation == "operate"
        # Warning must be logged to make the bypass visible
        assert any("dangerous" in msg for msg in caplog.messages)

    def test_bypass_with_custom_session_operation_blocked(self, caplog):
        """A session might register 'dangerous' as a custom operation.
        model_construct bypassing Literal must still be rejected at routing.
        """
        roles = _make_roles("researcher")
        nb = role_node_builder(roles)
        bad_req = SpawnRequest.model_construct(instruction="access system", operation="exec_shell")
        with caplog.at_level(logging.WARNING, logger="lionagi.orchestration.patterns"):
            node = nb(bad_req, None)
        assert node.operation == "operate"
        assert any("exec_shell" in msg for msg in caplog.messages)

    def test_none_operation_defaults_to_operate(self):
        """None/empty operation falls back to 'operate' without a warning."""
        roles = _make_roles("researcher")
        nb = role_node_builder(roles)
        # None is inside the Literal guard path (req.operation or "operate")
        req = SpawnRequest(instruction="x")
        node = nb(req, None)
        assert node.operation == "operate"


class TestSpawnedNodeParamsBindToTarget:
    """An allowed operation that routes cleanly must also be *invocable*.

    The node builder emits ``parameters={"instruction": ...}`` uniformly for
    every allowed operation. Routing alone is not enough — the parameters must
    bind to the target ``Branch`` method's signature, or the spawn fails at
    invocation. ``Branch.ReAct`` historically required ``instruct`` (not
    ``instruction``), so an allowed ReAct spawn raised ``TypeError`` at run time.
    """

    @pytest.mark.parametrize("op", sorted(_SPAWN_ALLOWED_OPERATIONS))
    def test_node_params_bind_to_branch_method(self, op: str):
        roles = _make_roles("researcher")
        nb = role_node_builder(roles)
        node = nb(SpawnRequest(instruction="do work", operation=op), None)

        method = getattr(Branch, op)
        # bind the built request against the real method signature (drop self).
        sig = inspect.signature(method)
        sig.bind_partial(roles["researcher"], **node.request)

    def test_react_accepts_instruction_keyword(self):
        """Regression: Branch.ReAct must accept a bare ``instruction=`` keyword."""
        node = role_node_builder(_make_roles("researcher"))(
            SpawnRequest(instruction="analyze the logs", operation="ReAct"), None
        )
        assert node.request == {"instruction": "analyze the logs"}
        params = inspect.signature(Branch.ReAct).parameters
        assert "instruction" in params
        # instruct must be optional now, not a required positional.
        assert params["instruct"].default is None
