# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""No operator-facing string names the store with its password still in it.

A store URL is a credential when the store is a server. Commands that report
which store they consulted, or refuse to open one, print that URL, and those
strings land in terminal scrollback, CI logs, and stored machine-mode JSON.

Two channels carry the same secret: the URL itself, and an exception's own
message, which is separate code and unaffected by masking the field beside it.

The end-to-end tests run the real CLI in a subprocess and read stdout and
stderr separately, so "it did not appear" says where. Each also asserts the
masked form is present, to distinguish a command that ran and masked from one
that never reached the store — an early probe of this fix mistyped the
invocation, got a clean password check from commands that all answered "no
such command", and read as a pass.

A store URL with no scheme is read as a filesystem path, so a credential in
one puts a password in a real path — evading a masker that only parses URLs.
That shape reaches a store setting only through the ``./`` spelling, since a
bare ``user:secret@host/db`` is refused before it resolves; the masker is
still tested against the bare form directly, since it is handed strings built
by drivers and older logs, not only strings that passed our own validation.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

import lionagi.state.db as db_mod

PASSWORD = "hunter2-correct-horse"
MASKED = "hunter…[21 chars]"

SERVER_URL = f"postgresql://dbuser:{PASSWORD}@127.0.0.1:59999/lionagi"
SCHEMELESS_URL = f"dbuser:{PASSWORD}@127.0.0.1/x"
# The same shape as a store setting. `./` is what says "this really is a path"
# to the check that otherwise refuses a scheme-less credential outright, and it
# changes nothing about what lands on disk: the credential is still in the name
# of a real file, which is the leak these arms are about.
CREDENTIALED_PATH = f"./{SCHEMELESS_URL}"


def _set_url(monkeypatch, url: str) -> None:
    """Settings are frozen, so redirect the whole object the module reads."""
    monkeypatch.setattr(
        db_mod, "settings", db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": url})
    )


def _assert_masked(text: str, where: str) -> None:
    assert PASSWORD not in text, f"the password reached {where}: {text!r}"
    assert MASKED in text, (
        f"{where} does not name the store at all, so it asserts nothing: {text!r}"
    )


# the masker itself


def test_a_url_with_no_scheme_is_masked_too():
    """The shape ``urlparse`` cannot decompose.

    Refused as a store setting, and still reaching this function from every
    other direction: a driver quoting what it was handed, an older log line,
    a path spelled with the ``./`` that makes it acceptable.
    """
    from lionagi.state.engine import mask_db_url

    _assert_masked(mask_db_url(SCHEMELESS_URL), "mask_db_url of a scheme-less URL")


def test_credentials_are_masked_inside_prose():
    from lionagi.state.engine import mask_credentials

    message = f"state.db not found at /var/lib/{SCHEMELESS_URL} — nothing was created"
    _assert_masked(mask_credentials(message), "mask_credentials of a message")


def test_masking_is_idempotent():
    """The sinks compose these, so masking an already-masked string must not re-mask it."""
    from lionagi.state.engine import mask_credentials, mask_db_url

    once = mask_db_url(SERVER_URL)
    assert mask_credentials(once) == once
    assert mask_credentials(mask_credentials(once)) == once


@pytest.mark.parametrize(
    "untouched",
    [
        "sqlite+aiosqlite:////var/lib/state.db",
        "postgresql://dbuser@127.0.0.1/lionagi",
        "[Errno 61] Connect call failed ('127.0.0.1', 59999)",
        "mail alice@example.com about the 12:30 window",
    ],
)
def test_strings_with_no_credential_are_returned_unchanged(untouched):
    """Over-masking would corrupt paths and messages, so the must-not-match arm
    matters as much as the must-match one."""
    from lionagi.state.engine import mask_credentials, mask_db_url

    assert mask_credentials(untouched) == untouched
    assert mask_db_url(untouched) == untouched


# per sink


def test_the_absent_store_refusal_names_a_masked_store(monkeypatch):
    from lionagi.cli.machine import state_db_absent

    _set_url(monkeypatch, SERVER_URL)
    _assert_masked(state_db_absent()["detail"], "the not-found detail")


def _raise_quoting_the_store(monkeypatch) -> None:
    """Make the open fail with a message that quotes the store, credential and all.

    The exception a closed port produces does not name the store, so asserting
    a password's absence from it asserts nothing: the string could not contain
    it under any condition. What is under test here is the sink's handling of a
    message that *does* carry the URL, so the message is supplied rather than
    hoped for. Our own store raises exactly this shape when a read-only open
    finds no file, and a driver that quotes its connection string is the case
    we do not control at all.
    """
    from lionagi.state.db import StateDB

    async def failing_open(self) -> None:
        raise RuntimeError(f"could not open {SERVER_URL}")

    monkeypatch.setattr(StateDB, "open", failing_open)


@pytest.mark.anyio
async def test_the_readonly_seam_masks_the_store_it_names(monkeypatch):
    from lionagi.cli.machine import readonly_state_db

    _set_url(monkeypatch, SERVER_URL)
    async with readonly_state_db() as (db, why):
        assert db is None, "this store cannot open, so the refusal is the thing under test"
        _assert_masked(why["detail"], "the seam's detail")


@pytest.mark.anyio
async def test_the_availability_wrapper_keeps_its_four_keys(monkeypatch):
    """Its key set is a published contract with its own test enumerating it.

    Pinned again from this side because the obvious home for an exception's
    message is a fifth key here, and the cost of putting one there is not
    visible from the code that would add it.
    """
    from lionagi.cli.machine import readonly_state_db

    _set_url(monkeypatch, SERVER_URL)
    async with readonly_state_db() as (db, why):
        assert set(why) == {"available", "value", "reason_code", "detail"}


@pytest.mark.anyio
async def test_the_writable_guard_masks_its_refusal(monkeypatch):
    from lionagi.cli.dispatch import _writable_state_db
    from lionagi.cli.machine import MachineError

    _set_url(monkeypatch, SERVER_URL)
    with pytest.raises(MachineError) as caught:
        async with _writable_state_db():
            pass

    _assert_masked(str(caught.value), "the write refusal's message")
    assert (caught.value.detail or {}).get("cause"), (
        f"the refusal carries no message, so the discriminator is not there: {caught.value.detail}"
    )
    # A refusal is an exception, not the availability wrapper, so its detail is
    # a free-form dict and carrying the message here costs no contract.


@pytest.mark.anyio
async def test_the_writable_guard_masks_a_message_that_quotes_the_store(monkeypatch):
    from lionagi.cli.dispatch import _writable_state_db
    from lionagi.cli.machine import MachineError

    _set_url(monkeypatch, SERVER_URL)
    _raise_quoting_the_store(monkeypatch)
    with pytest.raises(MachineError) as caught:
        async with _writable_state_db():
            pass

    _assert_masked((caught.value.detail or {}).get("cause", ""), "the write refusal's cause")


@pytest.mark.anyio
async def test_a_read_only_open_with_no_file_masks_the_path_it_names(monkeypatch, tmp_path):
    """The producer of the message that started this: a store URL with no
    scheme is read as a path, so the refusal names a path with a password in
    it."""
    from lionagi.state.db import StateDB

    monkeypatch.chdir(tmp_path)
    _set_url(monkeypatch, CREDENTIALED_PATH)
    with pytest.raises(FileNotFoundError) as caught:
        await StateDB(readonly=True).open()

    _assert_masked(str(caught.value), "the read-only open refusal")


def test_the_size_report_masks_a_server_url(monkeypatch):
    from lionagi.cli.state import _db_sizes

    _set_url(monkeypatch, SERVER_URL)
    sizes = _db_sizes()
    assert sizes["is_file"] is False
    _assert_masked(sizes["path"], "the size report's path")


def test_the_size_report_masks_a_credential_that_became_a_file_path(monkeypatch, tmp_path):
    """The branch that looks like it cannot carry a secret, and does.

    ``is_file`` is true here, so nothing about this answer suggests a
    credential is in it. That is exactly why masking only the server branch
    left the leak in place.
    """
    from lionagi.cli.state import _db_sizes

    monkeypatch.chdir(tmp_path)
    _set_url(monkeypatch, CREDENTIALED_PATH)
    sizes = _db_sizes()
    assert sizes["is_file"] is True, "this arm is meant to take the file branch"
    _assert_masked(sizes["path"], "the size report's path")


def test_a_newer_schema_refusal_names_a_masked_store(monkeypatch):
    """Raised on an ordinary open of a store a later release wrote, which is a
    routine upgrade-order mistake rather than a rare one."""
    from lionagi.state.db import SchemaTooNewError, StateDB

    _set_url(monkeypatch, SERVER_URL)
    db = StateDB()
    with pytest.raises(SchemaTooNewError) as caught:
        db._raise_if_schema_too_new("999999")

    _assert_masked(str(caught.value), "the schema-too-new refusal")


@pytest.mark.anyio
async def test_the_writable_guard_masks_the_store_it_reports_absent(monkeypatch, tmp_path):
    """The absent branch, which a credentialed path reaches whenever the file
    it names does not exist."""
    from lionagi.cli.dispatch import _writable_state_db
    from lionagi.cli.machine import MachineError

    monkeypatch.chdir(tmp_path)
    _set_url(monkeypatch, CREDENTIALED_PATH)
    with pytest.raises(MachineError) as caught:
        async with _writable_state_db():
            pass

    assert caught.value.kind == "not_found", f"this arm wanted the absent branch: {caught.value}"
    _assert_masked(str(caught.value), "the absent-store refusal")


def test_the_machine_dispatcher_masks_a_crash_that_quotes_the_store(monkeypatch, capfd):
    """The last sink before the envelope, and the only one that prints a message
    from code we do not own. A driver that quotes its connection string has
    nowhere else to be caught.

    ``capfd``, not ``capsys``: this dispatcher reserves stdout at the file
    descriptor level, so the envelope never passes through the object
    ``capsys`` replaces and the read comes back empty."""
    from lionagi.cli import machine

    def crash(argv):
        raise RuntimeError(f"could not reach {SERVER_URL}")

    monkeypatch.setattr(machine, "_run_machine_command", crash)
    assert machine.dispatch_machine(["handshake"]) == 0

    envelope = json.loads(capfd.readouterr().out)
    assert envelope["error"]["kind"] == "internal"
    _assert_masked(envelope["error"]["message"], "the crash envelope's message")


def test_the_lifecycle_read_masks_a_message_that_quotes_the_store(monkeypatch):
    from lionagi.cli.machine import lifecycle_data

    _set_url(monkeypatch, SERVER_URL)
    _raise_quoting_the_store(monkeypatch)
    why = lifecycle_data("run-that-does-not-matter")["lifecycle"]

    assert why["available"] is False
    _assert_masked(why["detail"], "the lifecycle detail")


def test_the_lifecycle_absent_answer_masks_the_path_it_names(monkeypatch, tmp_path):
    """``state_db_file()`` is a path, and a store URL with no scheme is how a
    password gets into one."""
    from lionagi.cli.machine import lifecycle_data

    monkeypatch.chdir(tmp_path)
    _set_url(monkeypatch, CREDENTIALED_PATH)
    why = lifecycle_data("run-that-does-not-matter")["lifecycle"]

    assert why["reason_code"] == "not_found", f"this arm wanted the absent branch: {why}"
    _assert_masked(why["detail"], "the lifecycle absent detail")


@pytest.mark.anyio
async def test_the_monitor_table_masks_a_message_that_quotes_the_store(monkeypatch):
    from lionagi.cli.monitor import _run_table

    _set_url(monkeypatch, SERVER_URL)
    _raise_quoting_the_store(monkeypatch)
    _assert_masked(
        await _run_table(since=None, entity_type=None, project=None),
        "the monitor table's error line",
    )


@pytest.mark.anyio
async def test_the_monitor_detail_masks_a_message_that_quotes_the_store(monkeypatch):
    from lionagi.cli.monitor import _run_detail

    _set_url(monkeypatch, SERVER_URL)
    _raise_quoting_the_store(monkeypatch)
    _assert_masked(await _run_detail("some-entity"), "the monitor detail's error line")


# end to end, both channels named


def _run_cli(args: list[str], url: str, cwd) -> subprocess.CompletedProcess:
    env = {**os.environ, "LIONAGI_STATE_DB_URL": url}
    return subprocess.run(
        [sys.executable, "-m", "lionagi.cli.main", *args],
        env=env,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=180,
    )


@pytest.mark.parametrize(
    "url", [SERVER_URL, CREDENTIALED_PATH], ids=["server", "credentialed-path"]
)
@pytest.mark.parametrize("args", [["--machine", "dispatch", "ls"], ["--machine", "stats", "runs"]])
def test_machine_mode_prints_the_password_on_neither_channel(tmp_path, url, args):
    """Machine mode is the channel that gets stored: its JSON lands in logs."""
    proc = _run_cli(args, url, tmp_path)

    assert PASSWORD not in proc.stdout, f"the password reached STDOUT: {proc.stdout!r}"
    assert PASSWORD not in proc.stderr, f"the password reached STDERR: {proc.stderr!r}"

    envelope = json.loads(proc.stdout)
    assert envelope["ok"] is True, f"the command did not run, so it masked nothing: {envelope}"
    assert MASKED in proc.stdout, (
        "no masked store name in the output, so this command never consulted the store and "
        f"the password check above is vacuous: {proc.stdout!r}"
    )


@pytest.mark.parametrize(
    "url", [SERVER_URL, CREDENTIALED_PATH], ids=["server", "credentialed-path"]
)
def test_the_human_size_report_prints_the_password_on_neither_channel(tmp_path, url):
    proc = _run_cli(["state", "stats"], url, tmp_path)

    assert PASSWORD not in proc.stdout, f"the password reached STDOUT: {proc.stdout!r}"
    assert PASSWORD not in proc.stderr, f"the password reached STDERR: {proc.stderr!r}"
    assert MASKED in proc.stdout, (
        "the report does not name the store at all, so it never reached the code under "
        f"test: {proc.stdout!r}"
    )
