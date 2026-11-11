# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Human schedule limit adapter behavior."""

from __future__ import annotations

import argparse
import threading
import time
import types
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from lionagi.ln import _ssrf
from lionhub.cli import schedule as schedule_cli

_HOLDERS = [
    {"schedule_id": "poll-project", "path": "github_event", "acquired_at": 1700000000.0},
    {"schedule_id": "nightly", "path": "scheduled", "acquired_at": 1700000001.0},
]


def test_human_limits_displays_each_holder_and_acquisition_time(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        schedule_cli,
        "_api",
        lambda path: {
            "max_scheduled_concurrent": 4,
            "current_inflight": 2,
            "current_holders": _HOLDERS,
        },
    )

    assert schedule_cli._cmd_limits(argparse.Namespace()) == 0

    output = capsys.readouterr().out
    for holder in _HOLDERS:
        assert holder["schedule_id"] in output
        assert holder["path"] in output
        assert schedule_cli._fmt_rfc3339(holder["acquired_at"]) in output
