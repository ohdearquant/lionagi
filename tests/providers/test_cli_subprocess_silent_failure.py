# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A nonzero exit with nothing to quote has more than one cause, and the
message a caller stores has to say which.

`ndjson_from_cli` raises with the child's stderr when there is any, and with a
constructed reason when there is not. Before this, every reason was the same
sentence — the exit code — so a child that failed silently, a child whose
stderr was never opened, and a drain that raised while reading all arrived as
one fact. That direction hides the instrument: a broken capture reads exactly
like a quiet subprocess, and a reader who takes it at face value goes looking
at the child.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys

import pytest

from lionagi.providers._cli_subprocess import _no_stderr_reason, ndjson_from_cli


def _cmd(script: str) -> list[str]:
    return [sys.executable, "-c", script]


# Exits nonzero having written nothing anywhere.
_SILENT_FAILURE = "import sys; sys.exit(3)"
# Exits nonzero with something on stderr, so the quoting path is exercised by
# the same test file that covers its absence.
_SPEAKS_THEN_FAILS = "import sys; sys.stderr.write('the child explained itself'); sys.exit(4)"


class TestTheReasonsAreDistinguishable:
    """Mutual distinctness, not any one wording: per-sentence asserts miss two collapsing into one."""

    def test_every_no_stderr_reason_differs_from_every_other(self):
        reasons = [
            _no_stderr_reason(3, None, None),
            _no_stderr_reason(3, "its stderr was never opened", None),
            _no_stderr_reason(3, None, "OSError"),
        ]
        assert len(set(reasons)) == len(reasons), (
            f"two of these carry the same text, so a caller cannot tell them apart: {reasons}"
        )

    def test_a_drain_failure_is_not_reported_as_a_quiet_child(self):
        quiet = _no_stderr_reason(3, None, None)
        broken = _no_stderr_reason(3, None, "OSError")
        assert "wrote nothing" in quiet
        assert "wrote nothing" not in broken
        assert "OSError" in broken

    def test_the_drain_exceptions_message_is_never_carried_only_its_type(self):
        """The drain exception can embed the bytes it was reading, and callers store this string."""
        reason = _no_stderr_reason(3, None, "OSError")
        assert "OSError" in reason
        # A type name is what the caller records; anything the exception said
        # about its input is not available to be recorded in the first place,
        # which is why the parameter is a type name and not an exception.
        assert reason.count("OSError") == 1

    def test_the_exit_code_survives_in_every_arm(self):
        """The one fact present in all three arms; adding detail must not drop it."""
        for reason in (
            _no_stderr_reason(7, None, None),
            _no_stderr_reason(7, "its stderr was never opened", None),
            _no_stderr_reason(7, None, "OSError"),
        ):
            assert "7" in reason


class TestAgainstARealChild:
    @pytest.mark.asyncio
    async def test_a_silent_nonzero_exit_says_the_stderr_was_empty(self):
        async def run():
            async for _ in ndjson_from_cli(_cmd(_SILENT_FAILURE)):
                pass  # pragma: no cover - the child writes no stdout

        with pytest.raises(RuntimeError) as exc_info:
            await asyncio.wait_for(asyncio.create_task(run()), timeout=30)

        message = str(exc_info.value)
        assert "wrote nothing to stderr" in message, message
        assert "3" in message, message

    @pytest.mark.asyncio
    async def test_a_child_that_does_speak_is_still_quoted_verbatim(self):
        """When there is stderr it is the message, and no constructed reason appears."""

        async def run():
            async for _ in ndjson_from_cli(_cmd(_SPEAKS_THEN_FAILS)):
                pass  # pragma: no cover - the child writes no stdout

        with pytest.raises(RuntimeError) as exc_info:
            await asyncio.wait_for(asyncio.create_task(run()), timeout=30)

        message = str(exc_info.value)
        assert "the child explained itself" in message, message
        assert "wrote nothing to stderr" not in message, message


# Writes a diagnostic to stderr and then hangs without ever producing stdout.
# This is the shape a worker takes when a liveness watchdog gives up on it: the
# reason it is stuck is on the pipe nobody was going to read.
_SPEAKS_ON_STDERR_THEN_HANGS = (
    "import sys, time; "
    "sys.stderr.write('quota exhausted for this model'); "
    "sys.stderr.flush(); "
    "time.sleep(300)"
)

# Produces one object and only then hangs, still writing to stderr. Abandoning
# this child is an ordinary cancellation, not a silent worker.
_EMITS_THEN_HANGS = (
    "import sys, time; "
    "sys.stdout.write('{\"ok\": 1}\\n'); "
    "sys.stdout.flush(); "
    "sys.stderr.write('ordinary progress chatter'); "
    "sys.stderr.flush(); "
    "time.sleep(300)"
)

_MODULE_LOGGER = "lionagi.providers._cli_subprocess"


async def _abandon(agen) -> None:
    """Close *agen* the way a liveness watchdog does: give up, then close."""
    with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
        await asyncio.wait_for(agen.__anext__(), timeout=5)
    with contextlib.suppress(Exception):
        await agen.aclose()


class TestAChildAbandonedBeforeProducingAnything:
    """Neither existing path quotes stderr for a stuck child, the case where it is the only evidence."""

    @pytest.mark.asyncio
    async def test_the_abandoned_childs_stderr_is_not_discarded(self, caplog):
        with caplog.at_level(logging.WARNING, logger=_MODULE_LOGGER):
            await _abandon(ndjson_from_cli(_cmd(_SPEAKS_ON_STDERR_THEN_HANGS)))

        assert "quota exhausted for this model" in caplog.text, (
            "the child's own account of why it was silent was dropped: " + caplog.text
        )

    @pytest.mark.asyncio
    async def test_a_child_that_did_produce_output_is_not_reported_as_silent(self, caplog):
        """The control: workers write progress to stderr routinely, so warning on every unwind buries the signal."""
        with caplog.at_level(logging.WARNING, logger=_MODULE_LOGGER):
            await _abandon(ndjson_from_cli(_cmd(_EMITS_THEN_HANGS)))

        assert "ordinary progress chatter" not in caplog.text, (
            "a cancelled child that had already produced output was reported as silent: "
            + caplog.text
        )
