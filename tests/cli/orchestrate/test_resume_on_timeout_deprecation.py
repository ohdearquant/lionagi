# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The orchestrate surfaces accept --resume-on-timeout and say it does nothing.

Neither `li o flow` nor `li o fanout` implements the auto-resume contract, so
the value is discarded. Deleting the flag would break callers whose invocations
parse today; accepting it silently is the defect that motivated this change.
The contract is therefore: still parses, and the caller is told at dispatch.
"""

from __future__ import annotations

import argparse
import warnings as _warnings

import pytest

from lionagi.cli.orchestrate import _warn_resume_on_timeout_is_inert


def _args(**kw) -> argparse.Namespace:
    ns = argparse.Namespace(orch_command="flow")
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def test_passing_the_flag_emits_a_deprecation_warning_naming_the_command(caplog):
    with _warnings.catch_warnings(record=True) as caught:
        _warnings.simplefilter("always")
        _warn_resume_on_timeout_is_inert(_args(resume_on_timeout=True))

    deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert len(deprecations) == 1
    message = str(deprecations[0].message)
    assert "--resume-on-timeout" in message
    assert "no effect" in message
    assert "li orchestrate flow" in message
    assert "li agent" in message


def test_not_passing_the_flag_warns_nothing():
    """Control: the notice must discriminate, or every run cries wolf."""
    with _warnings.catch_warnings(record=True) as caught:
        _warnings.simplefilter("always")
        _warn_resume_on_timeout_is_inert(_args(resume_on_timeout=False))
        _warn_resume_on_timeout_is_inert(_args())  # attribute absent entirely

    assert [w for w in caught if issubclass(w.category, DeprecationWarning)] == []


@pytest.mark.parametrize("surface", [["o", "flow"], ["o", "fanout"]])
def test_the_flag_still_parses_on_both_orchestrate_surfaces(surface):
    """The point of the deprecation: an invocation passing it must keep parsing."""
    from lionagi.cli.orchestrate import add_orchestrate_subparser

    root = argparse.ArgumentParser(prog="li", add_help=False)
    add_orchestrate_subparser(root.add_subparsers(dest="command"))
    ns = root.parse_args([*surface, "--resume-on-timeout", "probe prompt"])
    assert ns.resume_on_timeout is True
