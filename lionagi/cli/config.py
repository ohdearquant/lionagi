# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li config` utilities.

Commands:
  li config agents-md [--project PATH] [--output PATH]
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from lionagi.config_resolution import ResourceKind, list_resource_names, resolve_config

from ._logging import hint, log_error


def add_config_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register the `li config` top-level command."""
    config = subparsers.add_parser(
        "config",
        help="Configuration helpers and discoverability outputs.",
        description="Configuration helpers for .lionagi resources.",
    )
    cfg = config.add_subparsers(dest="config_command", required=True)

    agents_md = cfg.add_parser(
        "agents-md",
        help=(
            "Generate `.lionagi/agents.md` from discovered agent configs."
            " Used for discoverability and quick reference."
        ),
    )
    agents_md.add_argument(
        "--project",
        metavar="PATH",
        default=None,
        help="Project root to use for resource discovery (defaults to current cwd path search).",
    )
    agents_md.add_argument(
        "--output",
        metavar="PATH",
        default=None,
        help="Output file path (default: <project>/.lionagi/agents.md or .lionagi/agents.md).",
    )


def _default_agents_md_path(project: str | None) -> Path:
    if project:
        return Path(project).expanduser() / ".lionagi" / "agents.md"
    return Path(".lionagi/agents.md").expanduser()


def _format_provenance_block(provenance: dict) -> list[str]:
    lines: list[str] = []

    sources = provenance.get("sources")
    if isinstance(sources, dict):
        lines.append("### Resolution sources")
        for tier in ("default", "user", "project", "env", "cli"):
            value = sources.get(tier)
            if value:
                lines.append(f"- {tier}: `{value}`")

    keys = provenance.get("keys")
    if isinstance(keys, dict):
        lines.append("")
        lines.append("### Key provenance")
        for k in sorted(keys):
            lines.append(f"- `{k}` ← {keys[k]}")

    return lines


def _build_agents_md(project: str | None = None) -> str:
    names = list_resource_names(ResourceKind.AGENT, project=project)
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    lines = [
        "# Agents",
        "",
        f"Generated: {generated_at}",
        f"Discovered: {len(names)}",
        "",
    ]

    if not names:
        lines.append("No agent configs discovered.")
        return "\n".join(lines)

    for name in names:
        cfg = resolve_config(ResourceKind.AGENT, name, project=project)
        provenance = cfg.pop("_provenance", {})

        lines.append(f"## {name}")
        body_keys = sorted(cfg.keys())
        if not body_keys:
            lines.append("No agent settings defined.")
        else:
            lines.append("")
            for key in body_keys:
                lines.append(f"- **{key}**")

        if provenance:
            lines.extend(_format_provenance_block(provenance))
        lines.append("")

    return "\n".join(lines)


def run_config(args: argparse.Namespace) -> int:
    """Dispatch ``li config`` subcommands."""
    if args.config_command != "agents-md":
        log_error(f"unknown config command: {args.config_command}")
        return 1

    output = Path(args.output or _default_agents_md_path(args.project))
    content = _build_agents_md(project=args.project)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content)
    hint(f"generated {output}")
    return 0
