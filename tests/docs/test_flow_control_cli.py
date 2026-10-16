# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Flow monitoring examples use supported storage entity types."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from lionagi.cli.monitor import add_monitor_subparser


def test_monitor_help_explains_flow_entity_types():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    add_monitor_subparser(subparsers)
    monitor = subparsers.choices["monitor"]
    for entity_type in ("session", "invocation", "show", "play", "command"):
        assert parser.parse_args(["monitor", "--type", entity_type]).entity_type == entity_type
    with pytest.raises(SystemExit):
        parser.parse_args(["monitor", "--type", "flow"])
    help_text = " ".join(monitor.format_help().split())
    assert "Use `session` for flow runs" in help_text
    assert "`invocation` for registered invocation records" in help_text
    reference = (Path(__file__).parents[2] / "docs" / "cli-reference.md").read_text()
    controls = reference.split("### `li o ctl`", 1)[1].split("\n---", 1)[0]
    assert "li monitor --type session" in controls
    assert "Flow runs are sessions" in controls
