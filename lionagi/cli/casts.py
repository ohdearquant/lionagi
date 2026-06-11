# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""`li casts` — inspect the built-in roles and modes catalog."""

from __future__ import annotations

import argparse

from ._logging import log_error


def add_casts_subparser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("casts", help="inspect built-in roles and modes")
    p.add_argument(
        "name",
        nargs="?",
        default=None,
        help="role or mode name; omit to list all",
    )
    p.add_argument(
        "--modes",
        action="store_true",
        default=False,
        help="list modes instead of roles when no name is given",
    )


def run_casts(args: argparse.Namespace) -> int:
    from lionagi.casts._catalog import build_catalog

    catalog = build_catalog()

    if args.name is None:
        if args.modes:
            _print_table(catalog["modes"], kind="mode")
        else:
            _print_table(catalog["roles"], kind="role")
        return 0

    name = args.name
    role = next((r for r in catalog["roles"] if r["name"] == name), None)
    mode = next((m for m in catalog["modes"] if m["name"] == name), None)

    if role is not None:
        _print_role_detail(role)
        return 0
    if mode is not None:
        _print_mode_detail(mode)
        return 0

    log_error(f"unknown role or mode: {name!r}")
    return 1


def _print_table(entries: list[dict], *, kind: str) -> None:
    if not entries:
        print(f"(no {kind}s)")
        return
    col = max(len(e["name"]) for e in entries) + 2
    print(f"{'NAME':<{col}}  DESCRIPTION")
    print("-" * (col + 2) + "  " + "-" * 40)
    for e in entries:
        desc = e["description"]
        if len(desc) > 72:
            desc = desc[:69] + "..."
        print(f"{e['name']:<{col}}  {desc}")


def _print_role_detail(role: dict) -> None:
    print(f"Role: {role['name']}")
    print()
    print(role["description"])
    if role["emits"]:
        print()
        print("Emits: " + ", ".join(role["emits"]))
    if role["body"]:
        print()
        print(role["body"].rstrip())


def _print_mode_detail(mode: dict) -> None:
    print(f"Mode: {mode['name']}")
    print()
    print(mode["description"])
    if mode["conflicts_with"]:
        print()
        print("Conflicts with: " + ", ".join(mode["conflicts_with"]))
    if mode["behaviors"]:
        print()
        print(mode["behaviors"].rstrip())
