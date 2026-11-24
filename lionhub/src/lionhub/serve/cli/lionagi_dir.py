# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li lionagi-dir` — trust a project's `.lionagi` directory for runs the MCP server starts.

With ``lionagi_dir_trust.gate: mcp`` in ``~/.lionagi/settings.yaml``, a run the MCP server starts
takes agent profiles, playbooks and settings from a project ``.lionagi`` directory only when that
directory is recorded here (ADR-0088 Amendment 2). This command is the record's only writer, and a
run the server started refuses to run its trust step.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import yaml

from lionagi._auto import CliDeclaration, auto_register
from lionhub.orchestration.console import log_error

__all__ = ("add_lionagi_dir_subparser", "run_lionagi_dir")

# Shown for each profile before trust is recorded: what it may do, and what holds it in.
_PROFILE_FIELDS = (
    "model",
    "bypass",
    "yolo",
    "mcp",
    "account",
    "codex_home",
    "contain_account_integrations",
    "account_connectors",
    "contain_user_settings",
    "deny_read",
    "workload",
)


def add_lionagi_dir_subparser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "lionagi-dir",
        help="Trust a project's .lionagi directory for runs the MCP server starts.",
        description=(
            "With lionagi_dir_trust.gate set to mcp in ~/.lionagi/settings.yaml, a run the MCP "
            "server starts takes agent profiles, playbooks and settings from a project .lionagi "
            "directory only when that directory is trusted here. Trust is of the path: files "
            "added or edited there later are used as they are."
        ),
    )
    sub = parser.add_subparsers(dest="lionagi_dir_command", required=True)

    trust = sub.add_parser(
        "trust",
        help="List what DIR/.lionagi supplies, then record it as trusted.",
        description=(
            "Lists every agent profile and playbook in DIR/.lionagi and every notify and "
            "dispatch.notify_template entry in its settings.yaml, then asks before recording "
            "the directory, by its resolved path, in ~/.lionagi/settings.yaml. Use --yes to "
            "skip the prompt."
        ),
    )
    trust.add_argument(
        "dir",
        nargs="?",
        default=None,
        help="Directory holding the .lionagi to trust (default: the working directory).",
    )
    trust.add_argument(
        "--yes",
        action="store_true",
        help="Record trust without an interactive confirmation prompt.",
    )

    untrust = sub.add_parser(
        "untrust",
        help="Remove DIR/.lionagi from the trusted directories.",
    )
    untrust.add_argument(
        "dir",
        nargs="?",
        default=None,
        help="Directory holding the .lionagi to stop trusting (default: the working directory).",
    )

    sub.add_parser("list", help="List the trusted .lionagi directories.")


def _lionagi_dir_of(dir_arg: str | None) -> Path:
    base = Path(dir_arg).expanduser() if dir_arg else Path.cwd()
    return base / ".lionagi"


def _profiles(agents_dir: Path) -> list[tuple[str, Path]]:
    """Every profile file in one agents directory, in both layouts the resolver reads."""
    found: list[tuple[str, Path]] = []
    if not agents_dir.is_dir():
        return found
    for child in sorted(agents_dir.iterdir()):
        nested = child / f"{child.name}.md"
        if child.is_dir() and nested.is_file():
            found.append((child.name, nested))
        elif child.suffix == ".md" and child.is_file():
            found.append((child.stem, child))
    return found


def _profile_line(name: str, path: Path) -> str:
    from lionhub.orchestration.spawn import _parse_profile

    try:
        profile = _parse_profile(name, path.read_text())
    except Exception as exc:  # noqa: BLE001 — shown, so the person sees what could not be read
        return f"  agent    {name} ({path}): could not be read: {type(exc).__name__}: {exc}"
    fields = ", ".join(f"{field}={getattr(profile, field, None)!r}" for field in _PROFILE_FIELDS)
    return f"  agent    {name} ({path}): {fields}"


def _settings_lines(settings_path: Path) -> list[str]:
    """The settings entries a run acts on, or raises when the file cannot be read."""
    if not settings_path.is_file():
        return []
    data = yaml.safe_load(settings_path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{settings_path} does not hold a mapping")
    lines: list[str] = []
    if "notify" in data:
        lines.append(f"  settings notify: {data['notify']!r}")
    dispatch = data.get("dispatch")
    if isinstance(dispatch, dict) and "notify_template" in dispatch:
        lines.append(f"  settings dispatch.notify_template: {dispatch['notify_template']!r}")
    others = sorted(str(key) for key in data if key not in ("notify", "dispatch"))
    if others:
        lines.append(f"  settings other keys: {', '.join(others)}")
    return lines


def _disclosure(lionagi_dir: Path) -> list[str]:
    lines = [_profile_line(name, path) for name, path in _profiles(lionagi_dir / "agents")]
    playbooks = lionagi_dir / "playbooks"
    if playbooks.is_dir():
        for path in sorted(playbooks.glob("*.playbook.yaml")):
            lines.append(f"  playbook {path.name[: -len('.playbook.yaml')]} ({path})")
    lines.extend(_settings_lines(lionagi_dir / "settings.yaml"))
    return lines


def _run_trust(dir_arg: str | None, *, assume_yes: bool) -> int:
    from lionagi._lionagi_dir_trust import RECORD_KEY, trust_step_refusal
    from lionagi._paths import is_account_lionagi_dir, is_user_lionagi_dir
    from lionagi.plugins._user_settings import locked_user_settings

    refusal = trust_step_refusal("lionagi-dir trust")
    if refusal is not None:
        log_error(refusal)
        return 1

    lionagi_dir = _lionagi_dir_of(dir_arg)
    if not lionagi_dir.is_dir():
        log_error(f"there is no .lionagi directory in {lionagi_dir.parent}; nothing to trust")
        return 1
    if is_user_lionagi_dir(lionagi_dir) or is_account_lionagi_dir(lionagi_dir):
        log_error(
            f"{os.path.realpath(lionagi_dir)} is the user-level .lionagi directory, which is "
            "never gated; there is nothing to trust"
        )
        return 1
    target = os.path.realpath(lionagi_dir)

    try:
        lines = _disclosure(Path(target))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        log_error(f"cannot list what {target} supplies, so it is not trusted: {exc}")
        return 1
    print(f"Directory: {target}")
    print("A run the MCP server starts may use all of the following, and anything added later:")
    for line in lines or ["  (no profiles, playbooks or settings entries today)"]:
        print(line)

    if not assume_yes:
        answer = input("\nTrust this directory? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("not trusted.")
            return 1

    with locked_user_settings() as settings:
        record = settings.get(RECORD_KEY)
        if not isinstance(record, list):
            record = []
            settings[RECORD_KEY] = record
        already = target in record
        if not already:
            record.append(target)
    print(f"{'already trusted' if already else 'trusted'}: {target}")
    return 0


def _run_untrust(dir_arg: str | None) -> int:
    from lionagi._lionagi_dir_trust import RECORD_KEY
    from lionagi.plugins._user_settings import locked_user_settings

    target = os.path.realpath(_lionagi_dir_of(dir_arg))
    with locked_user_settings() as settings:
        record = settings.get(RECORD_KEY)
        present = isinstance(record, list) and target in record
        if present:
            settings[RECORD_KEY] = [entry for entry in record if entry != target]
    print(f"{'untrusted' if present else 'was not trusted'}: {target}")
    return 0


def _run_list() -> int:
    from lionagi._lionagi_dir_trust import trusted_lionagi_dirs

    entries = trusted_lionagi_dirs()
    if not entries:
        print("(no trusted .lionagi directories)")
        return 0
    for entry in entries:
        print(entry if Path(entry).is_dir() else f"{entry} (missing)")
    return 0


@auto_register(
    area="lionagi-dir",
    cli=CliDeclaration(seed="lionagi-dir", parser_factory=add_lionagi_dir_subparser),
)
def run_lionagi_dir(args: argparse.Namespace) -> int:
    if args.lionagi_dir_command == "trust":
        return _run_trust(args.dir, assume_yes=args.yes)
    if args.lionagi_dir_command == "untrust":
        return _run_untrust(args.dir)
    if args.lionagi_dir_command == "list":
        return _run_list()
    log_error(f"unknown lionagi-dir subcommand: {args.lionagi_dir_command!r}")
    return 1
