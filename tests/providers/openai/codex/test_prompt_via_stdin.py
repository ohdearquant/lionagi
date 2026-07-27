# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The Codex prompt travels over the child's stdin, not on the command line.

A whole conversation passed as a single argv element eventually exceeds the OS
limit on total argument length, and the spawn fails with "Argument list too
long" before codex runs at all. These tests pin the two halves of the fix: the
command line no longer carries the prompt, and the prompt still arrives intact
at a child that writes more output than a pipe buffer holds before it reads its
input (the shape that deadlocks if the parent writes stdin to completion before
draining stdout).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import resource
import sys

import pytest

from lionagi.providers._cli_subprocess import ndjson_from_cli
from lionagi.providers.openai.codex import CodexCodeRequest

# Larger than a pipe buffer on every platform we run on (64 KiB on Linux and
# macOS), so both directions genuinely block and a sequential write-then-read
# implementation cannot pass.
PIPE_BUFFER_CEILING = 64 * 1024
PROMPT_SIZE = 8 * PIPE_BUFFER_CEILING
CHILD_STDOUT_PREFILL = 4 * PIPE_BUFFER_CEILING
CHILD_STDERR_PREFILL = 2 * PIPE_BUFFER_CEILING

# Emits more stdout and stderr than the pipes can hold, and only then reads
# stdin, echoing back what it received plus the argv it was given.
ECHO_CHILD = f"""
import hashlib, json, sys

sys.stdout.write(json.dumps({{"type": "prefill", "pad": "p" * {CHILD_STDOUT_PREFILL}}}) + "\\n")
sys.stdout.flush()
sys.stderr.write("e" * {CHILD_STDERR_PREFILL})
sys.stderr.flush()

received = sys.stdin.read()
sys.stdout.write(
    json.dumps(
        {{
            "type": "received",
            "argv": sys.argv[1:],
            "length": len(received),
            "sha256": hashlib.sha256(received.encode()).hexdigest(),
        }}
    )
    + "\\n"
)
sys.stdout.flush()
"""


def _big_prompt() -> str:
    # Distinct content per line so a truncated or reordered delivery cannot
    # coincidentally match the digest.
    lines = []
    while sum(len(x) for x in lines) < PROMPT_SIZE:
        lines.append(f"turn {len(lines)}: " + "conversation text " * 20)
    return "\n".join(lines)


async def _collect(cmd: list[str], prompt: str) -> list[dict]:
    async def run() -> list[dict]:
        return [obj async for obj in ndjson_from_cli(cmd, stdin_data=prompt)]

    # A deadlock must surface as a failure, not as a hung test session.
    return await asyncio.wait_for(run(), timeout=90)


class TestCommandLineNoLongerCarriesThePrompt:
    def test_prompt_absent_from_argv(self, tmp_path):
        prompt = "explain the failure in detail"
        args = CodexCodeRequest(prompt=prompt, repo=tmp_path).as_cmd_args()
        assert prompt not in args
        assert args[-2:] == ["--", "-"], (
            "codex reads instructions from stdin when the prompt argument is "
            "`-`; it stays behind `--` so it is never parsed as a flag"
        )

    def test_argv_length_independent_of_prompt_size(self, tmp_path):
        """The whole point of the change: growing the conversation must not
        grow the command line, which is what ran into ARG_MAX."""
        small = CodexCodeRequest(prompt="hi", repo=tmp_path).as_cmd_args()
        huge = CodexCodeRequest(prompt=_big_prompt(), repo=tmp_path).as_cmd_args()
        assert small == huge

    def test_prompt_far_larger_than_arg_max_still_builds_a_spawnable_command(self, tmp_path):
        """A prompt bigger than the OS argument-length limit used to make the
        spawn itself fail; the command line built for it now stays tiny."""
        arg_max = os.sysconf("SC_ARG_MAX")
        oversized = "x" * (arg_max + 1024)
        args = CodexCodeRequest(prompt=oversized, repo=tmp_path).as_cmd_args()
        total = sum(len(a) + 1 for a in args)
        assert total < arg_max, f"command line is {total} bytes against a limit of {arg_max}"


class TestPromptArrivesOverStdin:
    @pytest.mark.asyncio
    async def test_large_prompt_reaches_a_child_that_writes_before_it_reads(self, tmp_path):
        """Delivery is verified by digest, not by "stdin was configured": the
        child blocks on a full stdout pipe before it ever reads stdin, so the
        parent must write stdin concurrently with draining stdout."""
        script = tmp_path / "echo_child.py"
        script.write_text(ECHO_CHILD)
        prompt = _big_prompt()

        objs = await _collect([sys.executable, str(script)], prompt)

        received = next(o for o in objs if o.get("type") == "received")
        assert received["length"] == len(prompt)
        assert received["sha256"] == hashlib.sha256(prompt.encode()).hexdigest()

    @pytest.mark.asyncio
    async def test_child_sees_eof_and_exits(self, tmp_path):
        """`codex exec` reads until EOF, so the write end must be closed. A
        child that reads stdin to completion and exits 0 only happens if it
        did; otherwise this hangs until the timeout above."""
        script = tmp_path / "eof_child.py"
        script.write_text(
            "import sys\n"
            "data = sys.stdin.read()\n"
            'sys.stdout.write(\'{"type": "eof", "length": %d}\' % len(data) + "\\n")\n'
        )
        objs = await _collect([sys.executable, str(script)], "prompt body")
        assert objs == [{"type": "eof", "length": len("prompt body")}]

    @pytest.mark.asyncio
    async def test_codex_stream_path_delivers_the_prompt_and_keeps_argv_clean(
        self, tmp_path, monkeypatch
    ):
        """End to end through the codex spawn helper: the prompt arrives on
        stdin and appears nowhere in the arguments the CLI was invoked with."""
        from lionagi.providers.openai import codex as codex_mod

        fake_cli = tmp_path / "fake-codex"
        fake_cli.write_text(f"#!{sys.executable}\n{ECHO_CHILD}")
        fake_cli.chmod(0o755)
        monkeypatch.setattr(codex_mod, "CODEX_CLI", str(fake_cli))

        prompt = _big_prompt()
        request = CodexCodeRequest(prompt=prompt, repo=tmp_path)

        async def run() -> list[dict]:
            return [obj async for obj in codex_mod._ndjson_from_cli(request)]

        objs = await asyncio.wait_for(run(), timeout=90)

        received = next(o for o in objs if o.get("type") == "received")
        assert received["sha256"] == hashlib.sha256(prompt.encode()).hexdigest()
        assert "-" in received["argv"]
        assert not any(prompt in arg for arg in received["argv"])


class TestOtherProvidersUnchanged:
    """The prompt is still assembled the same way for everyone; only the codex
    request shape changed, so the other CLI providers must keep passing it on
    the command line."""

    def test_claude_code_still_passes_the_prompt_in_argv(self, tmp_path):
        from lionagi.providers.anthropic.claude_code import ClaudeCodeRequest

        args = ClaudeCodeRequest(prompt="hello there", repo=tmp_path).as_cmd_args()
        assert "hello there" in args

    def test_gemini_still_passes_the_prompt_in_argv(self, tmp_path):
        from lionagi.providers.google.gemini_code import GeminiCodeRequest

        args = GeminiCodeRequest(prompt="hello there", repo=tmp_path).as_cmd_args()
        assert "hello there" in args

    @pytest.mark.asyncio
    async def test_default_stdin_still_devnull_when_no_data_is_supplied(self, tmp_path):
        """Without stdin_data the child keeps its previous stdin, so a
        provider that never sends input sees an immediately-empty read rather
        than a pipe nobody writes to."""
        script = tmp_path / "no_input_child.py"
        script.write_text(
            "import sys\n"
            'sys.stdout.write(\'{"type": "read", "length": %d}\' % len(sys.stdin.read()) + "\\n")\n'
        )

        async def run() -> list[dict]:
            return [obj async for obj in ndjson_from_cli([sys.executable, str(script)])]

        objs = await asyncio.wait_for(run(), timeout=60)
        assert objs == [{"type": "read", "length": 0}]


def test_prompt_join_still_shared_and_unchanged():
    """The prompt is still built by joining the non-system messages — this
    change is about how it is handed to codex, not how it is assembled."""
    from lionagi.providers._cli_subprocess import validate_message_prompt

    data = validate_message_prompt(
        {
            "messages": [
                {"role": "system", "content": "be brief"},
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "second"},
            ]
        }
    )
    assert data["prompt"] == "first\nsecond"
    assert data["system_prompt"] == "be brief"


def test_reference_limits_are_what_the_test_assumes():
    """Guard the premise: if a platform ever reports an argument limit smaller
    than the prompt these tests build, the size constants above stop
    exercising the case they were chosen for."""
    assert os.sysconf("SC_ARG_MAX") > PROMPT_SIZE
    assert resource.getpagesize() <= PIPE_BUFFER_CEILING
