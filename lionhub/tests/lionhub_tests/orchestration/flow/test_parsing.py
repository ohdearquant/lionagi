# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"Flow and fanout parser construction."

from __future__ import annotations

from tests._cli_logging_test_support import _restore_cli_logging as _restore_cli_logging


def test_reactive_flag_parses_via_argparse():
    """`li o flow --reactive off` reaches args.reactive."""
    import argparse

    from lionhub.orchestration.flow.parsing import add_orchestrate_subparser

    parser = argparse.ArgumentParser(prog="li")
    sub = parser.add_subparsers(dest="command", required=True)
    add_orchestrate_subparser(sub)
    args = parser.parse_args(["o", "flow", "codex/gpt-5.5", "task", "--reactive", "off"])
    assert args.reactive == "off"
    # default is None (resolved to "all" at dispatch)
    args2 = parser.parse_args(["o", "flow", "codex/gpt-5.5", "task"])
    assert args2.reactive is None


def test_workers_flag_parses_via_argparse():
    """`li o flow --workers a,b` reaches args.workers (mixed-model flows)."""
    import argparse

    from lionhub.orchestration.flow.parsing import add_orchestrate_subparser

    parser = argparse.ArgumentParser(prog="li")
    sub = parser.add_subparsers(dest="command", required=True)
    add_orchestrate_subparser(sub)
    args = parser.parse_args(
        ["o", "flow", "codex/orc", "task", "--workers", "codex/cheap,codex/expensive"]
    )
    assert args.workers == "codex/cheap,codex/expensive"


def test_pack_flag_parses_via_argparse():
    """`li o flow --pack PATH` and `li o fanout --pack PATH` reach args.pack."""
    import argparse

    from lionhub.orchestration.flow.parsing import add_orchestrate_subparser

    parser = argparse.ArgumentParser(prog="li")
    sub = parser.add_subparsers(dest="command", required=True)
    add_orchestrate_subparser(sub)

    args = parser.parse_args(["o", "flow", "codex/gpt-5.5", "task", "--pack", "/tmp/r.yaml"])
    assert args.pack == "/tmp/r.yaml"

    args2 = parser.parse_args(["o", "flow", "codex/gpt-5.5", "task"])
    assert args2.pack is None

    args3 = parser.parse_args(["o", "fanout", "codex/gpt-5.5", "task", "--pack", "/tmp/r.yaml"])
    assert args3.pack == "/tmp/r.yaml"
