# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A plain Session persists nothing by default; a hub run installs the persistence handlers."""

from __future__ import annotations

import ast
from pathlib import Path

from lionagi.hooks import HookPoint
from lionagi.hooks.builtins import (
    persist_branch_end,
    persist_branch_provenance,
    persist_session_end,
    persist_session_start,
)
from lionagi.session.session import Session
from lionhub.orchestration import runs
from lionhub.orchestration.run_persistence import (
    RUN_PERSISTENCE_HOOKS,
    install_run_persistence,
)

_LIFECYCLE = (
    HookPoint.SESSION_START,
    HookPoint.SESSION_END,
    HookPoint.BRANCH_CREATE,
    HookPoint.BRANCH_END,
)


def test_a_plain_session_bus_has_no_persistence_handlers():
    bus = Session().hooks
    for point in _LIFECYCLE:
        assert bus.handlers_for(point) == []


def test_install_run_persistence_wires_each_lifecycle_point():
    bus = install_run_persistence(Session().hooks)
    assert bus.handlers_for(HookPoint.SESSION_START) == [persist_session_start]
    assert bus.handlers_for(HookPoint.SESSION_END) == [persist_session_end]
    assert bus.handlers_for(HookPoint.BRANCH_CREATE) == [persist_branch_provenance]
    assert bus.handlers_for(HookPoint.BRANCH_END) == [persist_branch_end]


def test_install_run_persistence_returns_the_bus_it_was_given():
    session = Session()
    assert install_run_persistence(session.hooks) is session.hooks


def test_installing_twice_registers_each_handler_once():
    session = Session()
    install_run_persistence(session.hooks)
    install_run_persistence(session.hooks)
    for point, handlers in RUN_PERSISTENCE_HOOKS.items():
        assert session.hooks.handlers_for(point) == list(handlers)


def test_handlers_already_on_the_bus_stay_ahead():
    session = Session()

    async def caller_handler(**_kwargs):
        return None

    session.hooks.on(HookPoint.SESSION_END, caller_handler)
    install_run_persistence(session.hooks)
    assert session.hooks.handlers_for(HookPoint.SESSION_END) == [
        caller_handler,
        persist_session_end,
    ]


def _lifecycle_emits_without_install(path: Path) -> list[int]:
    """Lines in *path* that emit a lifecycle point on a bus not taken from the installer."""
    names = {point.name for point in _LIFECYCLE}
    found = []
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "emit"
            and node.args
            and isinstance(node.args[0], ast.Attribute)
            and node.args[0].attr in names
        ):
            continue
        receiver = node.func.value
        from_installer = (
            isinstance(receiver, ast.Call)
            and isinstance(receiver.func, ast.Name)
            and receiver.func.id == "install_run_persistence"
        ) or (isinstance(receiver, ast.Name) and receiver.id == "run_hooks")
        if not from_installer:
            found.append(node.lineno)
    return found


def test_every_lifecycle_emit_in_the_run_path_goes_through_the_installer():
    path = Path(runs.__file__)
    emits = _lifecycle_emits_without_install(path)
    assert emits == [], f"lifecycle emits in {path.name} without the installer: {emits}"


def test_the_run_path_emits_every_lifecycle_point():
    source = Path(runs.__file__).read_text()
    for point in _LIFECYCLE:
        assert f"HookPoint.{point.name}" in source
