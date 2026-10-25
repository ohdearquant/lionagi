# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Start-time identity decisions with finite, module-scoped process doubles."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import lionagi.cli.kill as kill_module


def _fake_process(monkeypatch, *, create_time=100.0, environ=None):
    proc = MagicMock()
    proc.cmdline.return_value = ["li"]
    proc.environ.return_value = environ or {}
    proc.create_time.return_value = create_time
    proc._proc = proc
    no_such_process = type("NoSuchProcess", (Exception,), {})
    fake_psutil = SimpleNamespace(
        Process=MagicMock(return_value=proc),
        NoSuchProcess=no_such_process,
        AccessDenied=type("AccessDenied", (Exception,), {}),
        ZombieProcess=type("ZombieProcess", (no_such_process,), {}),
    )
    monkeypatch.setattr(kill_module, "psutil", fake_psutil)
    return proc


class TestStartTimeBehavior:
    @pytest.mark.parametrize(
        ("observed", "expected_verdict"),
        [
            (-0.1000001, "not_ours"),
            (-0.1, "ours"),
            (0.0, "ours"),
            (0.099, "ours"),
            (0.1, "ours"),
            (0.1000001, "not_ours"),
            (float("nan"), "not_ours"),
            (float("inf"), "not_ours"),
        ],
    )
    def test_recorded_start_time_keeps_inclusive_tenth_second_tolerance(
        self, monkeypatch, observed, expected_verdict
    ):
        proc = _fake_process(monkeypatch, create_time=observed)
        probes = []

        def read_start_time(candidate):
            probes.append(candidate)
            return observed

        monkeypatch.setattr(kill_module, "raw_create_time", read_start_time)
        result = kill_module._check_pid_identity(42, "lionagi", expected_create_time=0.0)
        assert result == expected_verdict
        assert probes == [proc]
        kill_module.psutil.Process.assert_called_once_with(42)

    @pytest.mark.parametrize(
        ("error_name", "expected_verdict"),
        [
            ("ZombieProcess", "zombie"),
            ("NoSuchProcess", "not_ours"),
            ("AccessDenied", "unverifiable"),
        ],
    )
    def test_recorded_start_time_keeps_probe_failure_verdicts(
        self, monkeypatch, error_name, expected_verdict
    ):
        proc = _fake_process(monkeypatch)
        error = getattr(kill_module.psutil, error_name)("probe failed")
        probes = []

        def unreadable_start_time(candidate):
            probes.append(candidate)
            raise error

        monkeypatch.setattr(kill_module, "raw_create_time", unreadable_start_time)
        result = kill_module._check_pid_identity(42, "lionagi", expected_create_time=100.0)
        assert result == expected_verdict
        assert probes == [proc]

    def test_without_recorded_start_time_does_not_probe_it(self, monkeypatch):
        _fake_process(monkeypatch, environ={"LIONAGI_SESSION_ID": "recorded-session"})

        def unexpected_probe(proc):
            pytest.fail("a missing recorded start time must not trigger a live start-time read")

        monkeypatch.setattr(kill_module, "raw_create_time", unexpected_probe)
        result = kill_module._check_pid_identity(
            42, "lionagi", expected_session_id="recorded-session"
        )
        assert result == "ours"

    def test_recorded_start_time_keeps_unexpected_probe_error(self, monkeypatch):
        proc = _fake_process(monkeypatch)
        error = RuntimeError("unanticipated probe failure")
        probes = []

        def broken_probe(candidate):
            probes.append(candidate)
            raise error

        monkeypatch.setattr(kill_module, "raw_create_time", broken_probe)
        with pytest.raises(RuntimeError, match="unanticipated probe failure") as raised:
            kill_module._check_pid_identity(42, "lionagi", expected_create_time=100.0)
        assert raised.value is error
        assert probes == [proc]


class TestStartTimeDelegation:
    @pytest.mark.parametrize("matches", [True, False])
    def test_recorded_start_time_delegates_without_changing_arguments(self, monkeypatch, matches):
        _fake_process(monkeypatch)
        calls = []
        monkeypatch.setattr(kill_module, "raw_create_time", lambda proc: 100.05)

        def compare_start_times(observed, recorded):
            calls.append((observed, recorded))
            return matches

        monkeypatch.setattr(kill_module, "_start_time_matches", compare_start_times, raising=False)
        result = kill_module._check_pid_identity(42, "lionagi", expected_create_time=100.0)
        assert calls == [(100.05, 100.0)]
        assert result == ("ours" if matches else "not_ours")
