# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Raw durable settings classification, isolated from legacy routing."""

from __future__ import annotations

import asyncio
import inspect
import os
import socket
import subprocess
from dataclasses import FrozenInstanceError, replace

import pytest

from lionagi import _settings
from lionagi.state.lifecycle import settings_delivery
from lionagi.state.lifecycle.callbacks import EntityRef, RunTerminalEnvelope
from lionagi.state.lifecycle.notify_settings import (
    classify_notify_provenance,
    resolve_durable_global_config,
    resolve_notify_config,
)
from lionagi.state.lifecycle.reconciler import DeliveryOutcome


def _forbid(*_args, **_kwargs):
    raise AssertionError("offline settings tests forbid process, signal, and network calls")


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    for name in ("Popen", "run", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, _forbid)
    for name in ("create_subprocess_exec", "create_subprocess_shell"):
        monkeypatch.setattr(asyncio, name, _forbid)
    for name in ("kill", "killpg", "system"):
        monkeypatch.setattr(os, name, _forbid, raising=False)
    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, _forbid)
    monkeypatch.setattr(socket, "create_connection", _forbid)


def _layer(value):
    return {"notify": {"on_terminal": value}}


def _envelope():
    return RunTerminalEnvelope(
        event_id="event-1",
        entity=EntityRef(kind="session", id="session-1"),
        previous_status="running",
        terminal_status="completed",
        reason_code="session.done",
        occurred_at=1.0,
    )


@pytest.mark.parametrize(
    ("settings", "reason"),
    [
        ({"notify": "bad"}, "notify_not_mapping"),
        ({"notify": None}, "notify_not_mapping"),
        (_layer(["bad"]), "on_terminal_not_string_or_mapping"),
        (
            _layer({"enabled": "false", "adapter": {"kind": "exec", "argv": ["x"]}}),
            "enabled_not_boolean",
        ),
        (_layer({"enabled": True, "adapter": "bad"}), "enabled_without_adapter"),
        (
            _layer({"filter": "bad", "adapter": {"kind": "exec", "argv": ["x"]}}),
            "filter_not_a_mapping",
        ),
        (_layer({}), "enabled_without_adapter"),
        ([], "settings_root_not_mapping"),
    ],
)
def test_durable_malformed_settings_are_failure_not_silence(settings, reason):
    result = resolve_durable_global_config(settings)
    assert result.reason == reason
    assert result.handler is None


@pytest.mark.parametrize("unused", [{"adapter": "bad"}, {"filter": "bad"}])
def test_disabled_precedes_unused_malformed_fields(unused):
    result = resolve_durable_global_config(_layer({"enabled": False, **unused}))
    assert result.handler is None and result.reason is None


@pytest.mark.parametrize("invalid_notify", ["bad", ["bad"], 7])
def test_legacy_merged_resolution_remains_unchanged_for_bad_notify_container(invalid_notify):
    # The inert strict resolver must not change the existing legacy resolver.
    assert resolve_notify_config(settings={"notify": invalid_notify}).reason is None
    assert resolve_durable_global_config({"notify": invalid_notify}).reason == "notify_not_mapping"


@pytest.mark.parametrize("settings", [{}, {"notify": {}}, _layer(None)])
def test_genuinely_absent_and_null_global_setting_is_silence(settings):
    result = resolve_durable_global_config(settings)
    assert result.handler is None and result.reason is None


@pytest.mark.parametrize(
    ("project", "declared", "argv", "reason"),
    [
        ({}, False, None, None),
        (_layer({"filter": {"kinds": ["session"]}}), False, None, None),
        (_layer({"enabled": False}), False, None, None),
        (_layer("project-command --arg"), True, ("project-command", "--arg"), None),
        (
            _layer({"adapter": {"kind": "exec"}}),
            True,
            None,
            "exec_adapter_argv_not_a_list_of_strings",
        ),
        (_layer({"enabled": False, "adapter": "bad"}), True, None, None),
        (_layer({"enabled": "no"}), False, None, None),
        (
            _layer({"enabled": "no", "adapter": {"kind": "exec", "argv": ["project"]}}),
            True,
            None,
            "enabled_not_boolean",
        ),
    ],
)
def test_project_source_does_not_borrow_global_adapter(project, declared, argv, reason):
    global_cfg = _layer({"adapter": {"kind": "exec", "argv": ["global-command"]}})
    result = classify_notify_provenance(global_cfg, project, override="run-command")
    assert result.global_config.handler.argv == ("global-command",)
    assert result.project_declared is declared
    assert result.project_config.reason == reason
    assert (result.project_config.handler.argv if result.project_config.handler else None) == argv
    assert result.run_declared and result.run_config.handler.argv == ("run-command",)
    with pytest.raises(FrozenInstanceError):
        result.project_declared = False


@pytest.mark.parametrize(
    ("project", "diagnostic"),
    [
        ({"notify": "bad"}, "notify_not_mapping"),
        ({"notify": None}, "notify_not_mapping"),
        (_layer(["bad"]), "on_terminal_not_string_or_mapping"),
        (_layer({"enabled": "no"}), "enabled_not_boolean"),
    ],
)
def test_malformed_project_layer_without_adapter_has_no_subscription(project, diagnostic):
    result = classify_notify_provenance(
        _layer({"adapter": {"kind": "exec", "argv": ["global-command"]}}), project
    )
    assert not result.project_declared
    assert result.project_config.handler is None and result.project_config.reason is None
    assert result.project_error == diagnostic
    assert result.global_config.handler.argv == ("global-command",)


@pytest.mark.parametrize("partial", [{"filter": "bad"}, {"enabled": False, "filter": "bad"}])
def test_project_partial_with_no_adapter_does_not_filter_or_disable_global(partial):
    result = classify_notify_provenance(
        _layer({"adapter": {"kind": "exec", "argv": ["global-command"]}}), _layer(partial)
    )
    assert not result.project_declared
    assert result.project_config.reason is None
    assert result.global_config.handler.argv == ("global-command",)


def test_raw_layers_find_nearest_without_merging_and_keep_legacy_merge(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = tmp_path / "project"
    nested = project / "nested"
    (home / ".lionagi").mkdir(parents=True)
    (project / ".lionagi").mkdir(parents=True)
    nested.mkdir()
    (home / ".lionagi" / "settings.yaml").write_text(
        "notify:\n  on_terminal:\n    adapter:\n      kind: exec\n      argv: [global]\n"
    )
    (project / ".lionagi" / "settings.yaml").write_text(
        "notify:\n  on_terminal:\n    filter:\n      kinds: [session]\n"
    )
    monkeypatch.setenv("HOME", str(home))
    global_layer = _settings.load_global_settings_layer()
    project_layer = _settings.load_project_settings_layer(nested)
    result = classify_notify_provenance(global_layer, project_layer)
    assert result.global_config.handler.argv == ("global",)
    assert not result.project_declared
    merged = _settings.load_settings(nested)
    assert merged["notify"]["on_terminal"]["adapter"]["argv"] == ["global"]
    assert merged["notify"]["on_terminal"]["filter"]["kinds"] == ["session"]


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("", None),
        ("  # no settings\n", None),
        ("---\n", None),
        ("{}\n", None),
        ("null\n", "settings_root_not_mapping"),
        ("~\n", "settings_root_not_mapping"),
        ('!!null ""\n', "settings_root_not_mapping"),
        ("notify:\n  on_terminal: null\n", None),
    ],
    ids=[
        "empty",
        "comment-only",
        "empty-document",
        "empty-mapping",
        "null",
        "tilde",
        "tagged-null",
        "null-on-terminal",
    ],
)
def test_real_yaml_null_root_is_not_an_absent_layer(tmp_path, monkeypatch, text, reason):
    home = tmp_path / "home"
    settings_dir = home / ".lionagi"
    settings_dir.mkdir(parents=True)
    (settings_dir / "settings.yaml").write_text(text)
    monkeypatch.setenv("HOME", str(home))
    result = resolve_durable_global_config(_settings.load_global_settings_layer())
    assert result.reason == reason
    assert result.handler is None


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        ("secret-settings-value | command", "on_terminal_command_requires_shell_features"),
        ('"secret-settings-value', "on_terminal_command_not_parseable"),
        (
            {"adapter": {"kind": "exec", "argv": ["secret-settings-value", 42]}},
            "exec_adapter_argv_not_a_list_of_strings",
        ),
        (
            {"adapter": {"kind": "python", "ref": "secret-settings-value"}},
            "python_adapter_ref_invalid",
        ),
        (
            {
                "adapter": {"kind": "exec", "argv": ["x"]},
                "filter": {"secret-settings-value": ["x"]},
            },
            "filter_has_unknown_keys",
        ),
    ],
)
def test_durable_resolution_logs_only_safe_reason(source, reason, caplog):
    result = resolve_durable_global_config(_layer(source))
    assert result.reason == reason
    assert reason in caplog.text
    assert "secret-settings-value" not in caplog.text


def test_legacy_warning_text_remains_unchanged(caplog):
    value = "legacy-command | forbidden"
    result = resolve_notify_config(settings=_layer(value))
    assert result.reason == "on_terminal_command_requires_shell_features"
    assert value in caplog.text


class Recorder:
    def __init__(self):
        self.calls = []

    def record(self, subscription, **kwargs):
        self.calls.append((subscription, kwargs))
        return None


async def _invoke(prepared):
    value = prepared.call()
    return await value if inspect.isawaitable(value) else value


@pytest.mark.asyncio
async def test_global_attempt_resolves_current_settings_and_records_exec_outcomes(monkeypatch):
    recorder = Recorder()
    current = [_layer({"adapter": {"kind": "exec", "argv": ["first"]}})]
    calls = []
    monkeypatch.setattr(settings_delivery, "load_global_settings_layer", lambda: current[0])

    def build(resolved, *, outcome_fn=None, **_kwargs):
        async def fake_handler(envelope):
            calls.append((resolved.argv, envelope.event_id))
            outcome_fn(ok=len(calls) > 1, exit_code=0 if len(calls) > 1 else 1, stderr_text=None)

        return fake_handler

    monkeypatch.setattr(settings_delivery, "build_handler", build)
    first = settings_delivery.prepare_current_global_delivery(_envelope(), recorder=recorder)
    assert calls == []
    assert await _invoke(first) is DeliveryOutcome.FAILURE
    current[0] = _layer({"adapter": {"kind": "exec", "argv": ["second"]}})
    assert (
        await _invoke(
            settings_delivery.prepare_current_global_delivery(_envelope(), recorder=recorder)
        )
        is DeliveryOutcome.SUCCESS
    )
    assert calls == [(("first",), "event-1"), (("second",), "event-1")]
    assert [item[0] for item in recorder.calls] == ["global", "global"]
    assert [item[1]["ok"] for item in recorder.calls] == [False, True]


@pytest.mark.asyncio
async def test_invalid_global_is_recorded_but_disabled_or_filtered_is_not(monkeypatch):
    recorder = Recorder()
    current = [{"notify": "bad"}]
    monkeypatch.setattr(settings_delivery, "load_global_settings_layer", lambda: current[0])
    assert (
        await _invoke(
            settings_delivery.prepare_current_global_delivery(_envelope(), recorder=recorder)
        )
        is DeliveryOutcome.FAILURE
    )
    assert recorder.calls == [
        (
            "global",
            {"ok": False, "exit_code": None, "stderr_text": None, "reason": "notify_not_mapping"},
        )
    ]
    current[0] = _layer({"enabled": False, "adapter": "bad"})
    assert (
        await _invoke(
            settings_delivery.prepare_current_global_delivery(_envelope(), recorder=recorder)
        )
        is DeliveryOutcome.NOT_APPLICABLE
    )
    current[0] = _layer({"adapter": {"kind": "exec", "argv": ["x"]}, "filter": {"ids": ["other"]}})
    assert (
        await _invoke(
            settings_delivery.prepare_current_global_delivery(_envelope(), recorder=recorder)
        )
        is DeliveryOutcome.NOT_APPLICABLE
    )
    assert len(recorder.calls) == 1


@pytest.mark.asyncio
async def test_global_load_failure_remains_owed_and_recording_failure_is_isolated(monkeypatch):
    def bad_load():
        raise OSError("sensitive settings path")

    class BrokenRecorder:
        def record(self, *_args, **_kwargs):
            raise OSError("sensitive output path")

    monkeypatch.setattr(settings_delivery, "load_global_settings_layer", bad_load)
    prepared = settings_delivery.prepare_current_global_delivery(
        _envelope(), recorder=BrokenRecorder()
    )
    assert await _invoke(prepared) is DeliveryOutcome.FAILURE


def test_global_preparation_refuses_synthetic_nondurable_event():
    with pytest.raises(ValueError, match="durable event"):
        settings_delivery.prepare_current_global_delivery(replace(_envelope(), durable=False))
