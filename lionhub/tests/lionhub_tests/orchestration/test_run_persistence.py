# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A plain Session persists nothing by default; a hub run installs the persistence handlers."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from lionagi import Branch
from lionagi.hooks import HookPoint, register_handler, resolve_handler, route_message_persistence
from lionagi.hooks import loader as hook_loader
from lionagi.session.session import Session
from lionhub.orchestration import runs
from lionhub.orchestration.run_persistence import (
    RUN_PERSISTENCE_HOOKS,
    install_run_persistence,
    persist_branch_end,
    persist_branch_provenance,
    persist_message,
    persist_session_end,
    persist_session_start,
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


async def _on_message(message=None, **_kwargs):
    return None


@pytest.mark.parametrize("registered", ["before the bus is built", "after the bus is built"])
def test_a_handler_registered_under_the_store_name_stays_when_a_route_is_made(
    monkeypatch, registered
):
    """Routing live persistence removes the store's own message handler and nothing else."""

    async def replacement(**_kwargs):
        return None

    # Puts the store's own handler back under its name when the test ends.
    monkeypatch.setitem(
        hook_loader._REGISTRY, "persist_message", resolve_handler("persist_message")
    )
    branch = Branch(name="b1")
    session = Session(default_branch=branch)
    if registered == "before the bus is built":
        register_handler("persist_message", replacement)
        bus = session.hooks
        bus.on(HookPoint.MESSAGE_ADD, resolve_handler("persist_message"))
    else:
        bus = session.hooks
        bus.on(HookPoint.MESSAGE_ADD, replacement)
        register_handler("persist_message", replacement)
    bus.on(HookPoint.MESSAGE_ADD, persist_message)

    handler = route_message_persistence(session, branch, _on_message)

    assert bus.handlers_for(HookPoint.MESSAGE_ADD) == [replacement, handler]


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
