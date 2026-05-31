# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""`li orchestrate charter` — Charter DSL compile, validate, and schema commands."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def add_charter_subparser(
    orch_sub: argparse._SubParsersAction,
) -> argparse.ArgumentParser:
    """Register `li orchestrate charter` with its sub-commands.

    Returns the charter ArgumentParser so callers can extend it if needed.
    """
    ch = orch_sub.add_parser(
        "charter",
        help="Charter DSL: compile, validate, and schema export.",
        description=(
            "Compile a Charter DSL YAML file into runtime targets, "
            "validate without compiling, or export the JSON Schema."
        ),
    )
    ch_sub = ch.add_subparsers(dest="charter_command", required=True)

    # ── compile ──────────────────────────────────────────────────────────
    cp = ch_sub.add_parser(
        "compile",
        help="Compile a charter file and report runtime targets.",
        description=(
            "Parse and compile a Charter DSL YAML file through all six "
            "phases.  Prints target counts and the first 16 chars of the "
            "activation hash.  Exits 1 on CharterActivationError."
        ),
    )
    cp.add_argument(
        "file",
        type=str,
        metavar="FILE",
        help="Path to the Charter DSL YAML file.",
    )

    # ── validate ─────────────────────────────────────────────────────────
    va = ch_sub.add_parser(
        "validate",
        help="Parse and validate a charter file without full compilation.",
        description=(
            "Parse the charter through the P13 parser and run the "
            "compiler validation phase.  Does not emit runtime targets "
            "or verify the ratification hash."
        ),
    )
    va.add_argument(
        "file",
        type=str,
        metavar="FILE",
        help="Path to the Charter DSL YAML file.",
    )

    # ── schema ───────────────────────────────────────────────────────────
    ch_sub.add_parser(
        "schema",
        help="Print the CharterDocument JSON Schema.",
        description="Export the Pydantic JSON Schema for Charter DSL v0.",
    )

    return ch


def run_charter(args: argparse.Namespace) -> int:
    """Dispatch `li orchestrate charter` sub-commands."""
    from lionagi.protocols.governance.charter import (
        parse_charter,
    )
    from lionagi.protocols.governance.compiler import (
        CharterActivationError,
        CharterCompiler,
    )
    from lionagi.protocols.governance.dsl import CharterDocument

    cmd = args.charter_command

    if cmd == "compile":
        path = Path(args.file)
        try:
            yaml_text = path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"Error reading file: {exc}", file=sys.stderr)
            return 1

        try:
            doc = parse_charter(yaml_text)
        except Exception as exc:
            print(f"Parse error: {exc}", file=sys.stderr)
            return 1

        try:
            result = CharterCompiler().compile(doc)
        except CharterActivationError as exc:
            print(f"Activation error: {exc}", file=sys.stderr)
            return 1

        pin = result.policy_pin
        print("Compiled successfully")
        print(f"  gates              : {len(result.gates)}")
        print(f"  registry entries   : {len(result.registry)}")
        print(f"  sod rules          : {len(result.sod_rules)}")
        print(f"  evidence reqs      : {len(result.evidence_reqs)}")
        print(f"  trace expectations : {len(result.trace_expectations)}")
        print(f"  permissions        : {len(result.permissions)}")
        print(f"  activation hash    : {pin.charter_hash[:16]}...")
        if result.warnings:
            print(f"  warnings           : {len(result.warnings)}")
            for w in result.warnings:
                print(f"    - {w}")
        return 0

    if cmd == "validate":
        path = Path(args.file)
        try:
            yaml_text = path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"Error reading file: {exc}", file=sys.stderr)
            return 1

        try:
            doc = parse_charter(yaml_text)
        except Exception as exc:
            print(f"Parse error: {exc}", file=sys.stderr)
            return 1

        warnings = CharterCompiler()._validate(doc)
        if warnings:
            print(f"Validation warnings ({len(warnings)}):")
            for w in warnings:
                print(f"  - {w}")
        else:
            print("Validation OK")
        return 0

    if cmd == "schema":
        schema = CharterDocument.model_json_schema()
        print(json.dumps(schema, indent=2))
        return 0

    print(f"Unknown charter command: {cmd}", file=sys.stderr)
    return 1
