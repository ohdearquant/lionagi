# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""After a clean exit, the CLI's stderr reaches a caller that asked for it, credentials removed.

A provider that finds the turn empty quotes this stderr as the only evidence of why, so it
has to arrive after a status-0 exit, never after a failed one (that path raises with the
stderr already), and it has to pass the same redaction as every message the CLI layer raises.
"""

from __future__ import annotations

import sys

import pytest

from lionhub.providers._cli_subprocess import ndjson_from_cli

TOKEN = "sk-cleanexit-0123456789abcdef"


def _child(stderr_text: str, rc: int = 0) -> list[str]:
    code = (
        "import sys\n"
        f"sys.stderr.write({stderr_text!r})\n"
        "sys.stderr.flush()\n"
        'print(\'{"type": "hello"}\', flush=True)\n'
        f"sys.exit({rc})\n"
    )
    return [sys.executable, "-c", code]


async def test_a_clean_exit_hands_its_stderr_to_the_sink():
    seen: list[tuple[str, str | None]] = []
    objs = [
        obj
        async for obj in ndjson_from_cli(
            _child("a hook refused the prompt\n"),
            on_clean_exit=lambda stderr, unread: seen.append((stderr, unread)),
        )
    ]

    assert objs == [{"type": "hello"}]
    assert len(seen) == 1
    stderr, unread = seen[0]
    assert "a hook refused the prompt" in stderr
    assert unread is None


async def test_a_secret_in_that_stderr_is_removed_before_the_sink_sees_it():
    seen: list[tuple[str, str | None]] = []
    async for _ in ndjson_from_cli(
        _child(f"rejected {TOKEN}\n"),
        secret_values=[TOKEN],
        on_clean_exit=lambda stderr, unread: seen.append((stderr, unread)),
    ):
        pass

    assert len(seen) == 1
    assert TOKEN not in seen[0][0]
    assert "[redacted]" in seen[0][0]


async def test_a_failed_exit_never_calls_the_sink():
    seen: list[tuple[str, str | None]] = []
    with pytest.raises(RuntimeError, match="boom"):
        async for _ in ndjson_from_cli(
            _child("boom\n", rc=3),
            on_clean_exit=lambda stderr, unread: seen.append((stderr, unread)),
        ):
            pass

    assert seen == []


async def test_without_a_sink_the_stream_is_unchanged():
    assert [obj async for obj in ndjson_from_cli(_child("noise\n"))] == [{"type": "hello"}]
