# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The persistence handlers' names: old attribute paths, registry lookup and its refusal.

Each check runs in a fresh interpreter, so what it sees does not depend on what an earlier test
imported.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]

HANDLERS = (
    "persist_session_start",
    "persist_session_end",
    "persist_branch_provenance",
    "persist_branch_end",
    "persist_message",
)


def _run(program: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- this interpreter, a fixed program
        [sys.executable, "-c", textwrap.dedent(program)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.parametrize("name", HANDLERS)
def test_the_old_builtins_name_warns_once_and_serves_the_handler(name):
    completed = _run(
        f"""
        import warnings

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            from lionagi.hooks.builtins import {name} as old
        deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert len(deprecations) == 1, [str(w.message) for w in deprecations]
        assert "lionhub.orchestration.run_persistence" in str(deprecations[0].message)

        from lionhub.orchestration import run_persistence

        assert old is run_persistence.{name}
        with warnings.catch_warnings(record=True) as again:
            warnings.simplefilter("always")
            from lionagi.hooks.builtins import {name} as second
        assert second is old
        assert not [w for w in again if issubclass(w.category, DeprecationWarning)]
        """
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("name", HANDLERS)
def test_a_persistence_name_looked_up_before_the_hub_module_names_that_module(name):
    completed = _run(
        f"""
        import sys

        from lionagi.hooks.loader import resolve_handler

        assert "lionhub.orchestration.run_persistence" not in sys.modules
        try:
            resolve_handler({name!r})
        except KeyError as exc:
            message = str(exc)
        else:
            raise AssertionError("resolved without the hub module")
        assert {name!r} in message, message
        assert "lionhub.orchestration.run_persistence" in message, message
        """
    )
    assert completed.returncode == 0, completed.stderr


def test_an_unknown_name_is_refused_without_naming_the_hub_module():
    completed = _run(
        """
        from lionagi.hooks.loader import resolve_handler

        try:
            resolve_handler("persist_everything")
        except KeyError as exc:
            message = str(exc)
        else:
            raise AssertionError("an unknown name resolved")
        assert "Unknown hook handler" in message, message
        assert "run_persistence" not in message, message
        """
    )
    assert completed.returncode == 0, completed.stderr


def test_importing_the_hub_module_registers_every_handler_by_name():
    completed = _run(
        f"""
        from lionagi.hooks.loader import load_hooks_for_agent, resolve_handler
        from lionhub.orchestration import run_persistence

        for name in {HANDLERS!r}:
            assert resolve_handler(name) is getattr(run_persistence, name), name
        resolved = load_hooks_for_agent({{"message.add": ["persist_message"]}})
        assert [list(handlers) for handlers in resolved.values()] == [
            [run_persistence.persist_message]
        ]
        """
    )
    assert completed.returncode == 0, completed.stderr
