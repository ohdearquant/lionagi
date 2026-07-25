# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import argparse

import lionagi.cli.main as cli_main
from lionagi.cli.orchestrate.fanout import _parse_worker_pool


def _fanout_parser() -> argparse.ArgumentParser:
    parser, _ = cli_main._build_parser(cli_main._COMMAND_BY_NAME["orchestrate"])
    top_subparsers = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )
    orchestrate = top_subparsers.choices["orchestrate"]
    subparsers = next(
        action for action in orchestrate._actions if isinstance(action, argparse._SubParsersAction)
    )
    return subparsers.choices["fanout"]


def test_num_workers_help_describes_assignment_cap():
    action = next(action for action in _fanout_parser()._actions if action.dest == "num_workers")

    assert "Maximum assignments" in action.help
    assert "Ignored" not in action.help


def test_worker_pool_warns_when_specs_exceed_assignment_cap(monkeypatch):
    warnings: list[str] = []
    monkeypatch.setattr("lionagi.cli.orchestrate.fanout.warn", warnings.append)

    pool = _parse_worker_pool("model-a, model-b, model-c", num_workers=2)

    assert pool == ["model-a", "model-b", "model-c"]
    assert warnings == [
        "3 worker model specs provided, but --num-workers caps fanout at 2 assignments; "
        "1 model spec will not be used."
    ]
