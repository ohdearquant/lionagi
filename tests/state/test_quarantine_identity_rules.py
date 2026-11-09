# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Shared interpretation of row-bound process quarantine metadata."""

import json
import socket

import pytest

from lionagi.cli._pid_quarantine import quarantine_identity as cli_identity
from lionagi.state._quarantine_identity import (
    UNVERIFIABLE_PID_QUARANTINE_THRESHOLD,
    encode_identity,
    is_current_quarantine,
    quarantine_identity,
)


def row(**metadata):
    base = {
        "pid": 123,
        "pid_create_time": 42,
        "unverifiable_identity": "[123,42.0,null]",
        "unverifiable_quarantined": True,
        "unverifiable_count": 3,
        "unverifiable_epoch": 0,
    }
    base.update(metadata)
    return {"status": "running", "node_metadata": base}


def test_shared_identity_is_reexported_by_cli():
    assert cli_identity(row()) == quarantine_identity(row())
    assert UNVERIFIABLE_PID_QUARANTINE_THRESHOLD == 3
    assert encode_identity(123, 42, None) == "[123,42.0,null]"
    assert quarantine_identity(row()) == (123, 42.0, None)


@pytest.mark.parametrize("pid", [None, True, False, 0, -1, "0", "bad", 123.0, [], {}])
def test_invalid_pid_refuses_quarantine(pid):
    candidate = row(pid=pid)
    assert quarantine_identity(candidate) is None
    assert not is_current_quarantine(candidate)
    with pytest.raises(ValueError):
        encode_identity(pid, 42, None)


@pytest.mark.parametrize("pid", [123, "123", " 123 ", "+123"])
def test_row_pid_normalization_matches_encoder(pid):
    candidate = row(pid=pid)
    assert quarantine_identity(candidate) == (123, 42.0, None)
    assert encode_identity(pid, 42, None) == "[123,42.0,null]"
    assert is_current_quarantine(candidate)


@pytest.mark.parametrize(
    "created", [None, True, False, 0, -1, "bad", "nan", float("nan"), float("inf"), 10**400]
)
def test_invalid_creation_time_is_normalized_to_null(created):
    candidate = row(pid_create_time=created, unverifiable_identity="[123,null,null]")
    assert quarantine_identity(candidate) == (123, None, None)
    assert encode_identity(123, created, None) == "[123,null,null]"
    assert is_current_quarantine(candidate)


@pytest.mark.parametrize("created", [42, 42.0, "42", "42.0"])
def test_numeric_creation_time_has_one_canonical_encoding(created):
    assert encode_identity("123", created, "g") == '[123,42.0,"g"]'
    candidate = row(pid="123", pid_create_time=created, unverifiable_identity='[123,42.0,"g"]')
    candidate["cli_worker_generation"] = "g"
    assert is_current_quarantine(candidate)


@pytest.mark.parametrize("generation", [True, 0, [], {}])
def test_non_string_generation_is_refused(generation):
    candidate = row()
    candidate["cli_worker_generation"] = generation
    assert quarantine_identity(candidate) is None
    assert not is_current_quarantine(candidate)
    with pytest.raises(ValueError):
        encode_identity(123, 42, generation)


@pytest.mark.parametrize(
    "token", ["[123,42,null]", "[123, 42.0, null]", "[true,42.0,null]", "bad", None, []]
)
def test_only_exact_canonical_token_matches(token):
    assert not is_current_quarantine(row(unverifiable_identity=token))


@pytest.mark.parametrize(
    "updates",
    [
        {"unverifiable_quarantined": 1},
        {"unverifiable_quarantined": False},
        {"unverifiable_count": True},
        {"unverifiable_count": 3.0},
        {"unverifiable_count": 2},
        {"unverifiable_count": None},
        {"unverifiable_epoch": True},
        {"unverifiable_epoch": -1},
        {"unverifiable_epoch": 0.0},
        {"unverifiable_epoch": None},
    ],
)
def test_marker_and_history_require_strict_types_and_threshold(updates):
    assert not is_current_quarantine(row(**updates))


@pytest.mark.parametrize("mode", [None, True, "", "in_process", "remote", [], {}])
def test_present_nonlocal_mode_is_refused(mode):
    assert not is_current_quarantine(row(process_identity_mode=mode))


def test_missing_or_local_mode_is_allowed():
    assert is_current_quarantine(row())
    assert is_current_quarantine(row(process_identity_mode="local"))


@pytest.mark.parametrize("host", [None, "", 123, False, []])
def test_missing_or_non_string_host_keeps_existing_unknown_origin_behavior(host):
    assert is_current_quarantine(row(pid_host=host))


def test_only_nonempty_foreign_hostname_is_refused(monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "local-host")
    assert is_current_quarantine(row(pid_host="local-host"))
    assert not is_current_quarantine(row(pid_host="other-host"))


def test_boot_marker_is_not_probed_or_used(monkeypatch):
    import psutil

    def fail():
        raise AssertionError("boot time must not be read")

    monkeypatch.setattr(psutil, "boot_time", fail)
    assert is_current_quarantine(row(pid_boot_time="different"))


@pytest.mark.parametrize("metadata", [None, "bad", "[]", [], {}, "{" * 2000])
def test_invalid_metadata_is_not_quarantined(metadata):
    assert not is_current_quarantine({"status": "running", "node_metadata": metadata})


def test_json_metadata_and_terminal_rows():
    candidate = row()
    candidate["node_metadata"] = json.dumps(candidate["node_metadata"])
    assert is_current_quarantine(candidate)
    candidate["status"] = "completed"
    assert not is_current_quarantine(candidate)


def test_hostname_error_fails_closed(monkeypatch):
    def fail():
        raise OSError("hostname unavailable")

    monkeypatch.setattr(socket, "gethostname", fail)
    assert not is_current_quarantine(row(pid_host="recorded"))


def test_bad_custom_values_and_non_rows_do_not_raise():
    class BadFloat:
        def __float__(self):
            raise RuntimeError("cannot read value")

    assert quarantine_identity(row(pid_create_time=BadFloat())) is None
    assert not is_current_quarantine(row(pid_create_time=BadFloat()))
    for candidate in (None, [], "not a row"):
        assert quarantine_identity(candidate) is None
        assert not is_current_quarantine(candidate)
