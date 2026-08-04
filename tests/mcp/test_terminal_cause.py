# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The typed cause of a terminal exception, from the CLI process to the job record.

The loss being closed: a failed job record said a run failed and nothing about
why, so every caller re-derived the reason from console prose. These arms hold
the two ends of that channel to the same schema, and hold the failure paths to
fail-closed — a run whose cause cannot be read must still get its record.
"""

from __future__ import annotations

import json

import pytest

from lionagi.mcp import _notify_hook, config, jobs
from lionagi.mcp._terminal_cause import (
    UNKNOWN_CAUSE,
    allowed_cause_classes,
    read_terminal_cause,
    write_terminal_cause,
)
from lionagi.providers._provider_errors import (
    ProviderAuthError,
    ProviderError,
    ProviderQuotaError,
)


@pytest.fixture
def cause_file(monkeypatch, tmp_path):
    """A configured cause path, as a spawned run would see it."""
    path = tmp_path / "terminal_cause.json"
    monkeypatch.setenv(config.CAUSE_FILE_ENV_VAR, str(path))
    return path


class TestTheClosedSetIsDerivedFromTheHierarchy:
    def test_it_holds_the_provider_errors_and_nothing_wider(self):
        """A hand-maintained list would drift from the classes it names; this
        asserts both directions, since a set that admitted everything would
        satisfy a membership check on its own."""
        allowed = allowed_cause_classes()
        assert "ProviderQuotaError" in allowed
        assert "ProviderError" in allowed
        assert UNKNOWN_CAUSE in allowed
        assert "RuntimeError" not in allowed
        assert "Exception" not in allowed

    def test_a_subclass_defined_later_is_admissible_without_a_second_edit(self):
        class ProviderInventedForThisTest(ProviderError):
            retryable = True

        assert "ProviderInventedForThisTest" in allowed_cause_classes()


class TestWhatTheWriterRecords:
    def test_a_provider_error_round_trips_with_its_retry_hint(self, cause_file):
        write_terminal_cause(ProviderQuotaError("out of quota"))
        assert read_terminal_cause(cause_file) == {
            "class": "ProviderQuotaError",
            "retryable": True,
        }

    def test_a_non_retryable_provider_error_says_so(self, cause_file):
        write_terminal_cause(ProviderAuthError("bad key"))
        assert read_terminal_cause(cause_file) == {
            "class": "ProviderAuthError",
            "retryable": False,
        }

    def test_a_plain_exception_is_recorded_as_looked_at_and_not_a_provider_error(self, cause_file):
        """`unknown` and absent are different answers: this one says the cause
        was read and was not one of ours, which tells a caller to stop looking
        for a provider reason rather than to go and look."""
        write_terminal_cause(RuntimeError("something else entirely"))
        assert read_terminal_cause(cause_file) == {
            "class": UNKNOWN_CAUSE,
            "retryable": False,
        }

    def test_the_retry_hint_comes_from_the_class_not_from_the_instance(self, cause_file):
        """`retryable` is a class-level classification. An instance attribute of
        the same name belongs to some other subsystem's runtime state, and
        letting it through would make the stored hint mean two things."""
        exc = ProviderQuotaError("out of quota")
        exc.retryable = False  # shadows the ClassVar on this instance only
        write_terminal_cause(exc)
        assert read_terminal_cause(cause_file)["retryable"] is True

    def test_the_exceptions_message_reaches_no_part_of_the_file(self, cause_file):
        """The message is deliberately not carried, and a provider quoting a
        credential back is exactly why. Asserted over the raw bytes, not the
        parsed fields, so a message smuggled into a key or an unread field
        still fails this."""
        secret = "sk-ant-notarealkey-000111222333"
        write_terminal_cause(ProviderAuthError(f"401 from provider using {secret}"))
        raw = cause_file.read_text()
        assert secret not in raw
        assert "401" not in raw
        assert "provider using" not in raw


class TestTheWriterNeverRaisesIntoTheTerminalPath:
    """It is called while a run's real failure is propagating. Anything it
    raised would replace that failure with this one."""

    def test_no_configured_cause_file_is_a_silent_no_op(self, monkeypatch):
        monkeypatch.delenv(config.CAUSE_FILE_ENV_VAR, raising=False)
        write_terminal_cause(ProviderQuotaError("out of quota"))  # must not raise

    def test_an_unwritable_target_is_a_silent_no_op(self, monkeypatch, tmp_path):
        monkeypatch.setenv(
            config.CAUSE_FILE_ENV_VAR, str(tmp_path / "no" / "such" / "dir" / "c.json")
        )
        write_terminal_cause(ProviderQuotaError("out of quota"))  # must not raise


class TestTheReaderFailsClosed:
    def test_an_absent_file_reads_as_nothing_rather_than_as_a_cause(self, tmp_path):
        assert read_terminal_cause(tmp_path / "never-written.json") is None

    def test_no_configured_path_reads_as_nothing(self):
        assert read_terminal_cause(None) is None

    def test_malformed_json_reads_as_nothing_rather_than_raising(self, tmp_path):
        p = tmp_path / "c.json"
        p.write_text("{not json at all")
        assert read_terminal_cause(p) is None

    def test_a_json_document_that_is_not_an_object_reads_as_nothing(self, tmp_path):
        p = tmp_path / "c.json"
        p.write_text('["ProviderQuotaError"]')
        assert read_terminal_cause(p) is None

    def test_an_oversized_file_is_refused_rather_than_parsed(self, tmp_path):
        """Something other than this writer produced it, so parsing it would be
        answering about a different file."""
        p = tmp_path / "c.json"
        p.write_text(json.dumps({"class": "ProviderQuotaError", "pad": "x" * 8192}))
        assert read_terminal_cause(p) is None

    def test_a_class_name_outside_the_set_is_pinned_never_stored_verbatim(self, tmp_path):
        """The writer runs in another process at another version, so an
        unrecognised value is what this should expect rather than treat as
        impossible. Pinned at the boundary that stores it."""
        p = tmp_path / "c.json"
        p.write_text(json.dumps({"class": "rm -rf /; DROP TABLE runs", "retryable": True}))
        assert read_terminal_cause(p) == {"class": UNKNOWN_CAUSE, "retryable": True}

    def test_a_non_boolean_retry_hint_becomes_false_rather_than_truthy(self, tmp_path):
        p = tmp_path / "c.json"
        p.write_text(json.dumps({"class": "ProviderQuotaError", "retryable": "yes"}))
        assert read_terminal_cause(p) == {"class": "ProviderQuotaError", "retryable": False}


def _job(monkeypatch, tmp_path) -> str:
    """A recorded job with a real directory, as submit() would have left it."""
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.delenv("LIONAGI_MCP_NOTIFY_COMMAND", raising=False)
    monkeypatch.delenv("LIONAGI_MCP_NOTIFY_SENDER", raising=False)
    monkeypatch.delenv("LIONAGI_MCP_NOTIFY_TARGET", raising=False)
    rid = jobs.new_run_id()
    config.job_dir(rid).mkdir(parents=True)
    jobs._write_job(
        {
            "run_id": rid,
            "pid": 1,
            "kind": "agent",
            "label": "t",
            "cwd": None,
            "status": "running",
            "log": None,
        }
    )
    return rid


class TestTheHookLiftsItIntoTheRecord:
    def test_a_failed_run_with_a_cause_gets_it_on_the_record(self, monkeypatch, tmp_path):
        rid = _job(monkeypatch, tmp_path)
        jobs.failure_cause_path(rid).write_text(
            json.dumps({"class": "ProviderQuotaError", "retryable": True})
        )

        assert _notify_hook.main(["--run-id", rid, "--status", "failed"]) == 0

        record = jobs._read_job(rid)
        assert record["status"] == "failed"
        assert record["failure_cause"] == {"class": "ProviderQuotaError", "retryable": True}

    def test_a_run_with_no_cause_file_still_gets_its_record(self, monkeypatch, tmp_path):
        """The whole point of fail-closed here: the cause is an addition to the
        record, never a precondition for having one."""
        rid = _job(monkeypatch, tmp_path)
        assert not jobs.failure_cause_path(rid).exists()

        assert _notify_hook.main(["--run-id", rid, "--status", "failed"]) == 0

        record = jobs._read_job(rid)
        assert record["status"] == "failed"
        assert record["finished_at"] is not None
        # Absent, not a placeholder: nobody reported a cause, which a caller
        # must be able to tell from a cause that was read and was `unknown`.
        assert "failure_cause" not in record

    def test_a_corrupt_cause_file_costs_the_run_neither_its_record_nor_the_hooks_exit(
        self, monkeypatch, tmp_path
    ):
        rid = _job(monkeypatch, tmp_path)
        jobs.failure_cause_path(rid).write_text("\x00\x00 not json \xff")

        assert _notify_hook.main(["--run-id", rid, "--status", "failed"]) == 0

        record = jobs._read_job(rid)
        assert record["status"] == "failed"
        assert "failure_cause" not in record

    def test_the_path_resolves_through_the_same_function_the_spawn_used(
        self, monkeypatch, tmp_path
    ):
        """Writer and reader agreeing by construction rather than by two paths
        that match today."""
        rid = _job(monkeypatch, tmp_path)
        assert jobs.failure_cause_path(rid) == config.job_dir(rid) / config.CAUSE_FILENAME

    def test_an_unknown_run_has_no_cause_path_rather_than_an_invented_one(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
        assert jobs.failure_cause_path("no-such-run") is None
