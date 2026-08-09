# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A worker that cannot be built says which worker it was.

Building a worker resolves a model spec, a profile, an artifact directory and
any tool handoff, and each of those can fail for reasons specific to one role.
Unattributed, the whole orchestration ends on a generic error and the operator
is left to work out which of N workers it was about.
"""

from __future__ import annotations

import pytest

from lionagi._errors import TimeoutError as LionTimeoutError
from lionagi.cli.orchestrate._orchestration import (
    WorkerBuildError,
    attribute_worker_build_failure,
)


def test_a_build_failure_names_the_worker_and_its_role():
    original = RuntimeError("could not resolve model spec 'nope/nope'")

    with pytest.raises(WorkerBuildError) as caught:
        attribute_worker_build_failure(original, agent_id="researcher-2", role="assessor")

    message = str(caught.value)
    assert "researcher-2" in message, message
    assert "assessor" in message, message
    # The original survives as the cause rather than being replaced by a
    # summary of itself: whatever it said about the underlying failure is the
    # part that makes the attribution actionable.
    assert caught.value.__cause__ is original
    assert caught.value.agent_id == "researcher-2"
    assert caught.value.role == "assessor"


@pytest.mark.parametrize(
    "exc",
    [
        KeyboardInterrupt(),
        LionTimeoutError("worker build exceeded its deadline"),
        TimeoutError("worker build exceeded its deadline"),
    ],
    ids=["interrupt", "lion-timeout", "builtin-timeout"],
)
def test_an_exception_that_already_means_something_is_not_relabelled(exc):
    """Attribution must not change what a run's terminal status will say.

    Terminal status is decided by exception type, so wrapping a cancellation or
    a timeout in a ``RuntimeError`` subclass would record a deliberately stopped
    run as a failed one. This is the arm that separates "attributes build
    failures" from "wraps everything it sees", and it is the only one that does:
    the test above passes under either rule.

    Returning without raising is the contract -- the caller re-raises the
    original itself, so the exception reaching the classifier is untouched.
    """
    assert attribute_worker_build_failure(exc, agent_id="w-1", role="researcher") is None, (
        f"{type(exc).__name__} was re-labelled; its terminal status would change"
    )


def test_the_guard_tracks_the_classifier_rather_than_a_second_list():
    """Whatever the classifier treats as its own outcome is left alone.

    Stated as a property rather than by example so the two cannot drift: if a
    new exception type is ever given its own terminal status, this holds without
    anyone remembering to add it here.
    """
    from lionagi.cli._util import classify_exception

    for exc in (KeyboardInterrupt(), LionTimeoutError("x"), RuntimeError("x")):
        relabelled = True
        try:
            attribute_worker_build_failure(exc, agent_id="w", role="r")
            relabelled = False
        except WorkerBuildError:
            pass
        assert relabelled == (classify_exception(exc) == "failed"), (
            f"{type(exc).__name__} classifies as {classify_exception(exc)!r} "
            f"but attribution {'wrapped' if relabelled else 'passed'} it"
        )
