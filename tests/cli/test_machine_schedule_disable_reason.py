# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The machine disable path sends the reason it was given.

``li schedule disable`` requires ``--reason`` and the route refuses a disable
without one. A bodyless POST reaches that route as ``reason=None``, so dropping
the value between the parser and the request turns every machine disable into a
400 while the invocation still looks correct from the caller's side.

The assertion is made on the outgoing request rather than on a stubbed helper,
because the defect was the request having no body at all.
"""

from __future__ import annotations

import json

import pytest

from lionagi.cli import machine_schedule


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list:
    captured: list = []

    def _urlopen(request, timeout=None):  # noqa: ANN001 - test double
        captured.append(request)
        return _FakeResponse({"ok": True})

    def _base_url() -> str:
        return "http://127.0.0.1:8765"

    monkeypatch.setattr(machine_schedule, "_studio_url", _base_url)
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    return captured


def test_machine_disable_forwards_the_reason_in_the_request_body(sent: list) -> None:
    result = machine_schedule._disable(  # noqa: SLF001
        ["sched-abc123", "--reason", "Pause while rotating the provider credential"]
    )

    assert result == {"schedule_id": "sched-abc123", "enabled": False}
    assert len(sent) == 1
    request = sent[0]
    assert request.method == "POST"
    assert request.full_url.endswith("/api/schedules/sched-abc123/disable")
    assert request.data is not None, "the disable POST carried no body, so the route sees no reason"
    assert json.loads(request.data.decode()) == {
        "reason": "Pause while rotating the provider credential"
    }


def test_machine_enable_still_sends_no_body(sent: list) -> None:
    """Control: enable takes no reason, so a bodyless POST is correct there.

    Without this arm the assertion above would also be satisfied by sending a
    body on every schedule POST, which is a different behaviour than forwarding
    the one value that was asked for.
    """
    machine_schedule._enable(["sched-abc123"])  # noqa: SLF001

    assert len(sent) == 1
    assert sent[0].data is None


def test_machine_disable_refuses_when_the_reason_is_missing(sent: list) -> None:
    """The value cannot be absent by the time the body is built.

    ``--reason`` is required by the real subcommand parser, so a caller that
    omits it is refused before any request is made. That is what makes reading
    ``known.reason`` unconditionally safe at the call site.
    """
    with pytest.raises(machine_schedule.MachineError):
        machine_schedule._disable(["sched-abc123"])  # noqa: SLF001

    assert sent == []
