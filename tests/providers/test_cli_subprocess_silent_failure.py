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
import os
import pathlib
import signal
import sys

import pytest

from lionagi.providers import _cli_subprocess as cs
from lionagi.providers import _secret_resolution
from lionagi.providers._cli_subprocess import (
    _abandoned_without_output_note,
    _no_stderr_reason,
    ndjson_from_cli,
)
from lionagi.providers._secret_resolution import fill_declared_secrets


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


# The shape a worker takes when a liveness watchdog gives up on it: it explains
# itself on stderr and never produces stdout.
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


async def _abandon_then(agen, between) -> None:
    """Abandon, running *between* while the child is up and before teardown logs.

    ``asyncio.wait_for`` cancels the pending step, which runs the generator's
    teardown then and there, so anything sequenced after it lands too late.
    """
    step = asyncio.create_task(agen.__anext__())
    try:
        await asyncio.sleep(1)
        assert not step.done(), "the child exited early, so nothing was live when between() ran"
        between()
    finally:
        # In a finally because a failing assertion or callback would otherwise
        # leave the five-minute child running with the pipe still open.
        step.cancel()
        # CancelledError is not an Exception, and cancelling the pending step is
        # how this helper abandons the child at all.
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await step
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await agen.aclose()


# Writes nothing anywhere and stays up, so the buffer is empty on teardown.
_SILENT_THEN_HANGS = "import time; time.sleep(300)"


def _escapes_with_the_pipe(pid_path) -> str:
    """A child that leaves a grandchild outside its process group holding stderr.

    Killing the child then produces no EOF and the drain cannot finish, which is
    the condition under test. The holder outlives any abandon rather than racing
    it on a timer, and it writes its pid out so the caller can end it afterwards
    instead of leaving an orphan behind on whatever machine ran the suite.
    """
    return (
        "import pathlib, subprocess, sys, time; "
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'], "
        "start_new_session=True, stderr=sys.stderr); "
        f"pathlib.Path({str(pid_path)!r}).write_text(str(p.pid)); "
        "time.sleep(300)"
    )


def _end_pipe_holder(pid_path) -> None:
    """Kill the grandchild recorded by :func:`_escapes_with_the_pipe`, if any."""
    if not pid_path.exists():
        return
    with contextlib.suppress(ValueError, OSError):
        os.kill(int(pid_path.read_text()), signal.SIGKILL)


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


# Reads a credential out of its own environment and puts it on stderr, which is
# how a real CLI reports an auth failure.
_LEAKS_ENV_SECRET_THEN_HANGS = (
    "import os, sys, time; "
    "sys.stderr.write('auth failed for ' + os.environ['LIONAGI_TEST_API_KEY']); "
    "sys.stderr.flush(); "
    "time.sleep(300)"
)
# A credential the child got from its own config, so nothing we injected matches it.
_LEAKS_TOKEN_SHAPE_THEN_HANGS = (
    "import sys, time; "
    "sys.stderr.write('refused: Authorization: Bearer sk-abcdefghijklmnopqrst'); "
    "sys.stderr.flush(); "
    "time.sleep(300)"
)

# A credential in a variable whose name says nothing about it and which nobody
# declared: no vocabulary can recognise it, so only the length rule can.
# Exactly at the length floor, the hardest case the rule still covers.
_UNNAMEABLE_SECRET = "hunter2!"
_LEAKS_UNNAMEABLE_ENV_THEN_HANGS = (
    "import os, sys, time; "
    "sys.stderr.write('rejected token ' + os.environ['LIONAGI_TEST_THING']); "
    "sys.stderr.flush(); "
    "time.sleep(300)"
)
# The same leak on the ordinary failure path, where the stderr leaves the
# process as an exception the caller may store or forward rather than a log.
_LEAKS_UNNAMEABLE_ENV_THEN_FAILS = (
    "import os, sys; "
    "sys.stderr.write('rejected token ' + os.environ['LIONAGI_TEST_THING']); "
    "sys.exit(5)"
)

# Handed to the child only as an argument, and echoed back with nothing beside
# it to say what it is: no flag, no assignment, so the anchored text rules
# cannot reach it and only an argument-derived candidate can.
# Shorter than the redaction floor, in a variable the name rules recognise, and
# echoed alone so no surrounding text can identify it.
_SHORT_NAMED_SECRET = "hunter2"
_LEAKS_SHORT_NAMED_ENV_THEN_FAILS = (
    "import os, sys; "
    "sys.stderr.write('rejected token ' + os.environ['LIONAGI_TEST_PASSWORD']); "
    "sys.exit(7)"
)

_ARGUMENT_ONLY_SECRET = "cmdonly9f21ab77"
_LEAKS_ITS_ARGUMENT_THEN_FAILS = (
    "import sys; sys.stderr.write('rejected by upstream ' + sys.argv[2]); sys.exit(6)"
)

_INJECTED_SECRET = "supersecretvalue1234"
# Long enough to be redactable and deliberately unlike any credential shape, so
# only the environment lookup can catch it.
_INHERITED_SECRET = "inherited-value-9d1c"
# What another task rotates the variable to while the spawn is in flight.
_ROTATED_SECRET = "rotated-value-4a7f"
# Matches no known token shape, so only the header rule can remove it.
_OPAQUE_HEADER_SECRET = "OPAQUE-ffb31a9c4d2e"
_LEAKS_OPAQUE_HEADER_THEN_HANGS = (
    "import sys, time; "
    f"sys.stderr.write('refused: Authorization: Bearer {_OPAQUE_HEADER_SECRET}'); "
    "sys.stderr.flush(); "
    "time.sleep(300)"
)
_FORGES_A_LOG_RECORD_THEN_HANGS = (
    "import sys, time; "
    r"sys.stderr.write('first line\nWARNING forged second record\x1b[31m'); "
    "sys.stderr.flush(); "
    "time.sleep(300)"
)


class TestWhatCountsAsASecretToRemove:
    """Direct on the redactor, because each arm is one credential shape and a
    real child adds five seconds of teardown without adding evidence."""

    def test_a_secret_named_for_its_purpose_is_removed_because_it_was_declared(self):
        """The name pattern is a guess about spelling; the declaration is the operator's word."""
        env = {"LIONAGI_TEST_VALUE": "arbitrary-secret-123"}
        selected = cs._secret_candidates(env, ["LIONAGI_TEST_VALUE"])
        out = cs._redact_secrets_for_log("auth failed for arbitrary-secret-123", selected)
        assert "arbitrary-secret-123" not in out, out

    def test_a_name_the_pattern_recognises_still_works_undeclared(self):
        """The control: declaration widens the set, it does not replace it."""
        selected = cs._secret_candidates({"LIONAGI_API_KEY": "known-secret-123"}, [])
        out = cs._redact_secrets_for_log("auth failed for known-secret-123", selected)
        assert "known-secret-123" not in out, out

    def test_a_password_inside_a_connection_string_is_removed(self):
        out = cs._redact_secrets_for_log(
            "could not connect: postgres://admin:hunter2pass@db.internal/app", {}
        )
        assert "hunter2pass" not in out, out
        assert "db.internal/app" in out, "the host was redacted too, losing the diagnostic: " + out

    def test_a_url_without_a_credential_is_left_alone(self):
        """The control: the rule keys on userinfo, not on the scheme."""
        text = "connected to postgres://db.internal/app in 4ms"
        assert cs._redact_secrets_for_log(text, {}) == text

    def test_a_header_value_wrapped_onto_a_continuation_line_is_removed_whole(self):
        out = cs._redact_secrets_for_log("refused:\nAuthorization: OPAQUE\n PART9876\n", {})
        assert "PART9876" not in out, "the folded tail survived: " + out

    def test_an_ordinary_environment_value_is_not_redacted(self):
        """Redacting every long value would cost the diagnostic the log exists for."""
        selected = cs._secret_candidates({"HOME": "/Users/someone"}, [])
        out = cs._redact_secrets_for_log("wrote /Users/someone/app.log", selected)
        assert "/Users/someone/app.log" in out, out

    def test_a_short_declared_secret_is_removed_because_length_only_qualifies_the_guess(self):
        selected = cs._secret_candidates({"LIONAGI_TEST_VALUE": "abc"}, ["LIONAGI_TEST_VALUE"])
        out = cs._redact_secrets_for_log("auth failed for abc", selected)
        assert "abc" not in out, out

    def test_a_short_value_the_pattern_guessed_at_is_bounded_rather_than_ignored(self):
        """The name is evidence, so length decides the REPLACEMENT, never whether to act."""
        env = {"LIONAGI_API_KEY": "shortie"}
        selected = cs._secret_candidates(env, [])
        assert selected == {}, "the floor still keeps it out of substring replacement"
        bounded = cs._bounded_env_values(env, selected)
        assert bounded == {"LIONAGI_API_KEY": "shortie"}
        out = cs._redact_secrets_for_log("auth failed for shortie", selected, {}, bounded)
        assert "shortie" not in out, out

    @pytest.mark.parametrize("text", ["xshortiey stays whole", "shortieness is one word"])
    def test_a_bounded_value_may_only_replace_a_whole_token(self, text):
        """The reason the floor existed: a substring pass splices into ordinary words."""
        bounded = cs._bounded_env_values({"LIONAGI_API_KEY": "shortie"}, {})
        assert cs._redact_secrets_for_log(text, {}, {}, bounded) == text

    def test_a_long_value_keeps_the_stronger_substring_replacement(self):
        """Bounding is additive; it must not weaken what already worked."""
        selected = cs._secret_candidates({"LIONAGI_API_KEY": "supersecretvalue1234"}, [])
        out = cs._redact_secrets_for_log("Bearersupersecretvalue1234x", selected)
        assert "supersecretvalue1234" not in out, out

    def test_a_declared_name_holding_nothing_does_not_redact_every_character(self):
        selected = cs._secret_candidates({"LIONAGI_TEST_VALUE": ""}, ["LIONAGI_TEST_VALUE"])
        assert cs._redact_secrets_for_log("connected in 4ms", selected) == "connected in 4ms"

    def test_a_value_no_vocabulary_recognises_is_still_not_echoed(self):
        env = {"LIONAGI_TEST_THING": "opaque-value-a41f9c2b"}
        secrets = cs._secret_candidates(env, [])
        assert secrets == {}, "the name rule must not be what catches this: " + repr(secrets)
        opaque = cs._opaque_env_values(env, secrets)
        out = cs._redact_secrets_for_log("rejected token opaque-value-a41f9c2b", secrets, opaque)
        assert "opaque-value-a41f9c2b" not in out, out
        assert "[$LIONAGI_TEST_THING]" in out, (
            "the value went without saying which variable held it: " + out
        )

    def test_a_short_unrecognised_value_is_left_alone(self):
        # Under the floor, where a value is not tellable from an ordinary word.
        env = {"LIONAGI_TEST_THING": "abc"}
        opaque = cs._opaque_env_values(env, {})
        assert opaque == {}, "the length floor is not being applied: " + repr(opaque)
        text = "rejected token abc"
        assert cs._redact_secrets_for_log(text, {}, opaque) == text

    def test_a_credential_only_just_long_enough_is_still_not_echoed(self):
        env = {"WORKER_PAYLOAD": "hunter2!"}
        assert cs._secret_candidates(env, []) == {}, "no name rule may be what catches this"
        opaque = cs._opaque_env_values(env, {})
        out = cs._redact_secrets_for_log("worker refused: hunter2!", {}, opaque)
        assert "hunter2!" not in out, out
        assert "[$WORKER_PAYLOAD]" in out, out

    def test_a_declared_secret_is_redacted_rather_than_named(self):
        env = {"LIONAGI_TEST_VALUE": "declared-value-77c3f1"}
        secrets = cs._secret_candidates(env, ["LIONAGI_TEST_VALUE"])
        opaque = cs._opaque_env_values(env, secrets)
        assert opaque == {}, "a known secret must not also be offered as nameable: " + repr(opaque)
        out = cs._redact_secrets_for_log("auth failed for declared-value-77c3f1", secrets, opaque)
        assert "declared-value-77c3f1" not in out, out
        assert "[redacted]" in out, out
        assert "LIONAGI_TEST_VALUE" not in out, "a known secret's variable was named: " + out

    def test_a_password_passed_as_a_query_parameter_is_removed(self):
        out = cs._redact_secrets_for_log(
            "could not connect: postgres://db.internal/app?password=hunter2pass&sslmode=require",
            {},
        )
        assert "hunter2pass" not in out, out
        assert "sslmode=require" in out, "the rest of the query went with it: " + out

    def test_a_percent_encoded_parameter_name_is_still_recognised(self):
        """`p%61ssword` names the same parameter to the server that reads it."""
        out = cs._redact_secrets_for_log(
            "GET https://api.example.com/v1?p%61ssword=hunter2pass&limit=10", {}
        )
        assert "hunter2pass" not in out, out
        assert "limit=10" in out, out

    def test_a_query_carrying_no_credential_is_left_alone(self):
        """The control: the rule keys on the parameter name, not on the query."""
        text = "connected to postgres://db.internal/app?sslmode=require&timeout=30 in 4ms"
        assert cs._redact_secrets_for_log(text, {}) == text

    @pytest.mark.parametrize("name", ["signature", "sig", "x-sig", "sig_v2", "bearer"])
    def test_a_signing_credential_is_recognised_by_its_own_vocabulary(self, name):
        """These name a credential without containing key, token, secret or auth."""
        out = cs._redact_secrets_for_log(
            f"rejected: https://api.example.com/v1?{name}=hunter2pass&limit=10", {}
        )
        assert "hunter2pass" not in out, out
        assert "limit=10" in out, out

    @pytest.mark.parametrize("name", ["assignee", "design", "signal", "sigma"])
    def test_an_ordinary_parameter_that_merely_contains_sig_is_left_alone(self, name):
        """The short name is bounded, or the rule would eat diagnostics."""
        text = f"GET https://api.example.com/v1?{name}=alice&limit=10"
        assert cs._redact_secrets_for_log(text, {}) == text

    def test_a_short_password_is_removed_when_the_text_says_what_it_is(self):
        """Anchored to the name, so the floor the guesses need does not apply."""
        out = cs._redact_secrets_for_log("child failed: PASSWORD=hunter2", {})
        assert "hunter2" not in out, out

    @pytest.mark.parametrize(
        "text,secret",
        [
            ("connect refused password=hunter2pass", "hunter2pass"),
            ("env dump: API_KEY=abcd1234efgh", "abcd1234efgh"),
            ("unrecognized argument --api-key=hunter2pass", "hunter2pass"),
            ("usage: cli --auth-token zzzz9999 --verbose", "zzzz9999"),
        ],
    )
    def test_a_credential_outside_query_syntax_is_still_removed(self, text, secret):
        """A plain assignment and an echoed flag carry it just as a query does."""
        assert secret not in cs._redact_secrets_for_log(text, {}), text

    @pytest.mark.parametrize(
        "text",
        [
            "rows limit=10 timeout=30",
            "usage: cli --verbose true --out report.txt",
            "retry after=30 attempts=3",
        ],
    )
    def test_an_ordinary_assignment_is_left_alone(self, text):
        """The name decides here too, or the rule eats the diagnostic."""
        assert cs._redact_secrets_for_log(text, {}) == text

    def test_redacting_one_parameter_does_not_swallow_the_next(self):
        """The value stops at the separator; greedy matching ate the neighbour."""
        out = cs._redact_secrets_for_log(
            "GET https://api.example.com/v1?sig=hunter2pass&limit=10", {}
        )
        assert "hunter2pass" not in out, out
        assert "limit=10" in out, out

    @pytest.mark.parametrize(
        "cmd,expected",
        [
            (["cli", "--api-key", "hunter2pass"], {"api-key": "hunter2pass"}),
            (["cli", "--api-key=hunter2pass"], {"api-key": "hunter2pass"}),
            (["cli", "--verbose", "true"], {}),
            (["cli", "--api-key", "--other"], {}),
            (["cli", "--api-key", "short"], {}),
            (["cli", "--limit", "100"], {}),
            (None, {}),
        ],
    )
    def test_a_credential_given_only_as_an_argument_becomes_a_candidate(self, cmd, expected):
        """A flag holding a value no environment mapping ever sees."""
        assert cs._cmd_secret_values(cmd) == expected

    def test_an_argument_credential_is_removed_even_echoed_without_its_flag(self):
        """The text rules need the flag beside it; this is why the candidate exists."""
        secrets = cs._cmd_secret_values(["cli", "--api-key", "hunter2pass"])
        out = cs._redact_secrets_for_log("server rejected hunter2pass", secrets)
        assert "hunter2pass" not in out, out


class TestTheQuotedStderrCarriesNoCredential:
    """Quoting the child's stderr is the point of this path, so the credential has to be removed rather than the quoting withheld."""

    @pytest.mark.asyncio
    async def test_a_secret_we_injected_does_not_reach_the_log(self, caplog):
        env = {**os.environ, "LIONAGI_TEST_API_KEY": _INJECTED_SECRET}
        with caplog.at_level(logging.WARNING, logger=_MODULE_LOGGER):
            await _abandon(ndjson_from_cli(_cmd(_LEAKS_ENV_SECRET_THEN_HANGS), env=env))

        assert _INJECTED_SECRET not in caplog.text, (
            "a credential this process handed the child came back out in a log line: " + caplog.text
        )
        assert "[redacted]" in caplog.text, (
            "the stderr was dropped rather than redacted, losing the diagnostic: " + caplog.text
        )

    @pytest.mark.asyncio
    async def test_a_secret_in_an_unremarkably_named_variable_does_not_reach_the_log(self, caplog):
        """Nothing marks this variable as holding a credential, so the name rules cannot save it."""
        env = {**os.environ, "LIONAGI_TEST_THING": _UNNAMEABLE_SECRET}
        with caplog.at_level(logging.WARNING, logger=_MODULE_LOGGER):
            await _abandon(ndjson_from_cli(_cmd(_LEAKS_UNNAMEABLE_ENV_THEN_HANGS), env=env))

        assert _UNNAMEABLE_SECRET not in caplog.text, (
            "a credential in a variable no rule recognises reached a log line: " + caplog.text
        )
        assert "[$LIONAGI_TEST_THING]" in caplog.text, (
            "the stderr was dropped rather than named, losing the diagnostic: " + caplog.text
        )

    @pytest.mark.asyncio
    async def test_a_short_named_credential_does_not_reach_the_caller(self):
        """Under the floor and echoed with nothing beside it: only the bounded pass can catch it."""
        env = {**os.environ, "LIONAGI_TEST_PASSWORD": _SHORT_NAMED_SECRET}

        async def run():
            async for _ in ndjson_from_cli(_cmd(_LEAKS_SHORT_NAMED_ENV_THEN_FAILS), env=env):
                pass  # pragma: no cover - the child writes no stdout

        with pytest.raises(RuntimeError) as exc_info:
            await asyncio.wait_for(asyncio.create_task(run()), timeout=30)

        message = str(exc_info.value)
        assert _SHORT_NAMED_SECRET not in message, (
            "a credential too short for the floor reached the caller: " + message
        )
        assert "[redacted]" in message, (
            "the stderr was dropped rather than redacted, losing the diagnostic: " + message
        )

    @pytest.mark.asyncio
    async def test_a_credential_passed_only_as_an_argument_does_not_reach_the_caller(self):
        """Nothing in the child's text names it, so the argument list is the only place it can be found."""
        cmd = [
            *_cmd(_LEAKS_ITS_ARGUMENT_THEN_FAILS),
            "--api-key",
            _ARGUMENT_ONLY_SECRET,
        ]

        async def run():
            async for _ in ndjson_from_cli(cmd):
                pass  # pragma: no cover - the child writes no stdout

        with pytest.raises(RuntimeError) as exc_info:
            await asyncio.wait_for(asyncio.create_task(run()), timeout=30)

        message = str(exc_info.value)
        assert _ARGUMENT_ONLY_SECRET not in message, (
            "a credential given only on the command line reached the caller: " + message
        )
        assert "[redacted]" in message, (
            "the stderr was dropped rather than redacted, losing the diagnostic: " + message
        )

    @pytest.mark.asyncio
    async def test_a_nonzero_exit_does_not_carry_a_credential_to_the_caller(self):
        """A nonzero exit quotes the same stderr into an exception, which is the other way it leaves this process."""
        env = {**os.environ, "LIONAGI_TEST_THING": _UNNAMEABLE_SECRET}

        async def run():
            async for _ in ndjson_from_cli(_cmd(_LEAKS_UNNAMEABLE_ENV_THEN_FAILS), env=env):
                pass  # pragma: no cover - the child writes no stdout

        with pytest.raises(RuntimeError) as exc_info:
            await asyncio.wait_for(asyncio.create_task(run()), timeout=30)

        message = str(exc_info.value)
        assert _UNNAMEABLE_SECRET not in message, (
            "a credential reached the caller through the exception: " + message
        )
        assert "[$LIONAGI_TEST_THING]" in message, (
            "the stderr was dropped rather than named, losing the diagnostic: " + message
        )

    @pytest.mark.asyncio
    async def test_a_secret_the_child_inherited_does_not_reach_the_log(self, caplog, monkeypatch):
        """Spawning without an ``env`` hands the child this process's environment, so that is the one its output can echo."""
        # With a secrets lookup configured, the spawn path materialises a full
        # env dict and the inheriting case never arises, so the machine running
        # the suite would decide whether this test can fail.
        monkeypatch.setattr(
            _secret_resolution,
            "resolve_secret_lookup_config",
            lambda **_: _secret_resolution._NOT_CONFIGURED,
        )
        assert await fill_declared_secrets(None) is None, "the child must actually be inheriting"
        monkeypatch.setenv("LIONAGI_TEST_API_KEY", _INHERITED_SECRET)
        with caplog.at_level(logging.WARNING, logger=_MODULE_LOGGER):
            await _abandon(ndjson_from_cli(_cmd(_LEAKS_ENV_SECRET_THEN_HANGS)))

        assert _INHERITED_SECRET not in caplog.text, (
            "a credential the child inherited from this process reached a log line: " + caplog.text
        )
        assert "[redacted]" in caplog.text, (
            "the stderr was dropped rather than redacted, losing the diagnostic: " + caplog.text
        )

    @pytest.mark.asyncio
    async def test_a_secret_unset_after_the_spawn_is_still_redacted(self, caplog, monkeypatch):
        """The child keeps what it was handed, so redaction reads the environment as of the spawn rather than as of the log line."""
        monkeypatch.setattr(
            _secret_resolution,
            "resolve_secret_lookup_config",
            lambda **_: _secret_resolution._NOT_CONFIGURED,
        )
        monkeypatch.setenv("LIONAGI_TEST_API_KEY", _INHERITED_SECRET)
        with caplog.at_level(logging.WARNING, logger=_MODULE_LOGGER):
            await _abandon_then(
                ndjson_from_cli(_cmd(_LEAKS_ENV_SECRET_THEN_HANGS)),
                between=lambda: os.environ.pop("LIONAGI_TEST_API_KEY", None),
            )

        assert _INHERITED_SECRET not in caplog.text, (
            "a credential the child still held reached a log line after this process dropped it: "
            + caplog.text
        )
        assert "[redacted]" in caplog.text, (
            "the stderr was dropped rather than redacted, losing the diagnostic: " + caplog.text
        )

    @pytest.mark.asyncio
    async def test_an_opaque_header_credential_does_not_reach_the_log(self, caplog):
        """The header rule must consume the whole value, not stop at the scheme word."""
        with caplog.at_level(logging.WARNING, logger=_MODULE_LOGGER):
            await _abandon(ndjson_from_cli(_cmd(_LEAKS_OPAQUE_HEADER_THEN_HANGS)))

        assert _OPAQUE_HEADER_SECRET not in caplog.text, (
            "a bearer token reached a log line because only its scheme word was removed: "
            + caplog.text
        )
        assert "[redacted]" in caplog.text, (
            "the stderr was dropped rather than redacted, losing the diagnostic: " + caplog.text
        )

    @pytest.mark.asyncio
    async def test_child_output_cannot_forge_a_second_log_record(self, caplog):
        """Child output is data; a newline in it must not read as the start of another record."""
        with caplog.at_level(logging.WARNING, logger=_MODULE_LOGGER):
            await _abandon(ndjson_from_cli(_cmd(_FORGES_A_LOG_RECORD_THEN_HANGS)))

        assert "first line" in caplog.text, (
            "the diagnostic was lost rather than escaped: " + caplog.text
        )
        assert "\nWARNING forged second record" not in caplog.text, (
            "child output opened a second log record: " + caplog.text
        )
        assert "\x1b[31m" not in caplog.text, (
            "a terminal control sequence reached the log unescaped: " + caplog.text
        )

    @pytest.mark.asyncio
    async def test_the_child_and_the_redactor_are_handed_one_environment_snapshot(
        self, caplog, monkeypatch
    ):
        """A child left to read os.environ at exec can get a credential no snapshot saw."""
        monkeypatch.setattr(
            _secret_resolution,
            "resolve_secret_lookup_config",
            lambda **_: _secret_resolution._NOT_CONFIGURED,
        )
        assert await fill_declared_secrets(None) is None, "the child must actually be inheriting"
        monkeypatch.setenv("LIONAGI_TEST_API_KEY", _INHERITED_SECRET)

        real_spawn = cs.asyncio.create_subprocess_exec
        handed_env: list[object] = []

        async def rotating_spawn(*cmd, **kwargs):
            # Another task rotating the credential between snapshot and exec.
            os.environ["LIONAGI_TEST_API_KEY"] = _ROTATED_SECRET
            handed_env.append(kwargs.get("env"))
            return await real_spawn(*cmd, **kwargs)

        monkeypatch.setattr(cs.asyncio, "create_subprocess_exec", rotating_spawn)
        with caplog.at_level(logging.WARNING, logger=_MODULE_LOGGER):
            await _abandon(ndjson_from_cli(_cmd(_LEAKS_ENV_SECRET_THEN_HANGS)))

        assert handed_env, "the spawn never ran, so this proves nothing"
        assert handed_env[0] is not None, (
            "the child was left to read os.environ at exec, which no earlier snapshot can describe"
        )
        assert _ROTATED_SECRET not in caplog.text, (
            "the child was handed a credential the redactor never saw: " + caplog.text
        )
        assert _INHERITED_SECRET not in caplog.text, (
            "the credential the child actually received reached a log line: " + caplog.text
        )
        assert "[redacted]" in caplog.text, (
            "the stderr was dropped rather than redacted, losing the diagnostic: " + caplog.text
        )

    @pytest.mark.asyncio
    async def test_a_credential_shape_the_child_sourced_itself_does_not_reach_the_log(self, caplog):
        with caplog.at_level(logging.WARNING, logger=_MODULE_LOGGER):
            await _abandon(ndjson_from_cli(_cmd(_LEAKS_TOKEN_SHAPE_THEN_HANGS)))

        assert "sk-abcdefghijklmnopqrst" not in caplog.text, (
            "a credential-shaped token in child output reached a log line: " + caplog.text
        )
        assert "[redacted]" in caplog.text, (
            "the stderr was dropped rather than redacted, losing the diagnostic: " + caplog.text
        )


class TestADrainThatDidNotFinishIsNotSilence:
    """An unread pipe is unknown. Reporting it as a quiet child is the failure this whole path exists to prevent."""

    @pytest.mark.asyncio
    async def test_an_unfinished_drain_is_not_reported_as_a_quiet_child(self, caplog, tmp_path):
        pid_file = tmp_path / "pipe-holder.pid"
        try:
            with caplog.at_level(logging.WARNING, logger=_MODULE_LOGGER):
                await _abandon(ndjson_from_cli(_cmd(_escapes_with_the_pipe(pid_file))))

            # The premise, asserted rather than timed: an earlier version of this
            # test let the holder exit on its own schedule, so on a machine where
            # the abandon happened to take longer the pipe was already closed and
            # the test measured a case it was not written for.
            assert pid_file.exists(), (
                "the child never recorded a pipe holder, so nothing was holding "
                "stderr open and this asserts nothing"
            )
            assert "it wrote nothing to stderr either" not in caplog.text, (
                "an undrained pipe was reported as a child that said nothing: " + caplog.text
            )
            assert "could not be drained in time" in caplog.text, caplog.text
        finally:
            _end_pipe_holder(pid_file)

    @pytest.mark.asyncio
    async def test_a_drain_that_finishes_still_reports_a_quiet_child_plainly(self, caplog):
        """The control: without it the arm above passes on a note that never says anything definite."""
        with caplog.at_level(logging.WARNING, logger=_MODULE_LOGGER):
            await _abandon(ndjson_from_cli(_cmd(_SILENT_THEN_HANGS)))

        assert "it wrote nothing to stderr either" in caplog.text, caplog.text

    def test_a_partial_capture_says_the_drain_was_cut_short(self):
        note = _abandoned_without_output_note("half a line", None, None, True)
        assert "half a line" in note
        assert "[stderr drain did not finish]" in note


def _cli_request_models() -> tuple[type, ...]:
    from lionagi.providers.anthropic.claude_code import ClaudeCodeRequest
    from lionagi.providers.openai.codex import CodexCodeRequest
    from lionagi.providers.pi.cli import PiCodeRequest

    return (PiCodeRequest, ClaudeCodeRequest, CodexCodeRequest)


_CLI_REQUEST_MODELS = _cli_request_models()


class TestACredentialCannotReachTheCommandLine:
    """The bare-positional residual holds only while no credential field is emitted to argv.

    Today nothing enforces that but a field description, so it is enforced here.
    """

    # Credential-shaped means the redactor's OWN name vocabulary matches, so this
    # guard and the redaction cannot drift apart. Names that match it while
    # holding a count rather than a secret are excluded by name: a new one fails
    # loudly and costs a line, where a new credential field must never pass quietly.
    _COUNTS_NOT_CREDENTIALS = frozenset({"max_thinking_tokens"})

    @classmethod
    def _credential_fields(cls) -> list[tuple[type, str]]:
        return [
            (model, name)
            for model in _CLI_REQUEST_MODELS
            for name in model.model_fields
            if name not in cls._COUNTS_NOT_CREDENTIALS and cs._name_reads_as_credential(name)
        ]

    def test_no_credential_field_declares_a_cli_flag(self):
        """Enumerated, not pinned: a field pinned by name certifies nothing about the next one."""
        found = self._credential_fields()
        # Same test, or an enumeration that selects nothing passes and proves nothing.
        assert found, "the vocabulary selected no field at all, so the assertion below is vacuous"
        emitted = [
            f"{model.__name__}.{name}"
            for model, name in found
            if "cli_flag" in (model.model_fields[name].json_schema_extra or {})
        ]
        assert not emitted, (
            "a credential would be built into argv, where the redactor cannot tell it "
            "from a path or a subcommand: " + ", ".join(emitted)
        )

    def test_every_model_that_builds_argv_is_in_the_enumeration(self):
        """A fourth provider adding a CLI request model must not walk past the guard above."""
        import ast

        builders = set()
        root = pathlib.Path(cs.__file__).parent
        sources = sorted(root.rglob("*.py"))
        assert sources, "no provider sources found, so this discovery proves nothing"
        for path in sources:
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                names = {
                    n.func.id
                    for n in ast.walk(node)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                }
                if "build_declarative_cli_args" in names:
                    builders.add(node.name)
        assert builders, "found no class building declarative CLI args, so the compare is vacuous"
        missing = builders - {m.__name__ for m in _CLI_REQUEST_MODELS}
        assert not missing, f"request models build argv but are not guarded: {sorted(missing)}"
