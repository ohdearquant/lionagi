# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li hooks` — import Claude Code / Codex hook configs; trust imported hook commands.

``li hooks import claude|codex [path]`` translates a foreign hooks config
into the project's ``.lionagi/settings.yaml`` ``hooks_external:`` block,
tagging every entry it writes with ``source: imported:<harness>`` so the
loader's trust gate (see ``lionagi.hooks.external``) applies to it. This is a
one-shot import, not a live read: editing the foreign config afterward has no
effect until ``li hooks import`` runs again.

``li hooks trust`` lists every imported command hook that has no matching
hash record yet and, on confirmation, records
``sha256(json.dumps(argv))`` for each into ``~/.lionagi/settings.yaml``'s
``trusted_hook_commands`` list -- the same file/key the loader reads at
execution time.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
from pathlib import Path
from typing import Any

from ._logging import log_error

__all__ = ("add_hooks_subparser", "run_hooks")

# Characters that only have meaning under a shell -- a command string
# containing any of these cannot be safely tokenized into an argv vector, and
# is rejected-with-reason rather than reinterpreted (Dv1-3: LionAGI executes
# argv vectors only, never a shell).
_SHELL_METACHARACTERS = re.compile(r"""[|&;<>$`\\"'*?\[\]{}~#!\n]""")

_DEFAULT_CONFIG_PATH = {
    "claude": ".claude/settings.json",
    "codex": ".codex/hooks.json",
}


def add_hooks_subparser(subparsers: argparse._SubParsersAction) -> None:
    hooks = subparsers.add_parser(
        "hooks",
        help="Import Claude Code / Codex hook configs; trust imported hook commands.",
        description=(
            "`li hooks import` translates a foreign hooks config into this "
            "project's `.lionagi/settings.yaml` `hooks_external:` block. "
            "`li hooks trust` records approval (content-hashed argv) for the "
            "imported commands so they are allowed to execute."
        ),
    )
    hooks_sub = hooks.add_subparsers(dest="hooks_command", required=True)

    imp = hooks_sub.add_parser(
        "import",
        help="Translate a Claude Code / Codex hooks config into hooks_external:.",
    )
    imp.add_argument(
        "source", choices=("claude", "codex"), help="Which harness's config shape to read."
    )
    imp.add_argument(
        "path",
        nargs="?",
        default=None,
        help="Path to the config file (defaults to .claude/settings.json or .codex/hooks.json).",
    )
    imp.add_argument("--cwd", default=None, help="Project directory (defaults to cwd).")

    trust = hooks_sub.add_parser(
        "trust",
        help="List and approve pending imported hook commands (hash-pinned).",
    )
    trust.add_argument("--cwd", default=None, help="Project directory (defaults to cwd).")
    trust.add_argument(
        "--yes",
        action="store_true",
        help="Record trust without an interactive confirmation prompt.",
    )


def _tokenize_command(command: Any) -> tuple[list[str] | None, str | None]:
    """Return ``(argv, None)`` on success, or ``(None, reason)`` on rejection.

    A list-form command is validated directly (must already be a non-empty
    argv list of non-empty strings). A string-form command is tokenized with
    ``shlex.split`` only when it contains no shell metacharacters; anything
    else is rejected rather than heuristically reinterpreted.
    """
    from lionagi.hooks.external import ExternalHookConfigError, validate_argv

    if isinstance(command, list):
        try:
            return validate_argv(command), None
        except ExternalHookConfigError as exc:
            return None, str(exc)
    if isinstance(command, str):
        if _SHELL_METACHARACTERS.search(command):
            return (
                None,
                f"command {command!r} contains shell metacharacters; cannot "
                "translate to an argv vector without a shell",
            )
        tokens = shlex.split(command)
        if not tokens:
            return None, f"command {command!r} tokenized to an empty argv"
        return tokens, None
    return None, f"command must be a string or an argv list, got {command!r}"


def _translate_config(
    data: dict[str, Any], *, source_label: str
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    """Translate a foreign ``{"hooks": {...}}`` config into ``hooks_external``
    matcher-group entries, plus a per-entry report line (imported or
    rejected-with-reason).
    """
    from lionagi.hooks.external import SUPPORTED_EVENTS

    hooks_block = data.get("hooks", {}) if isinstance(data, dict) else None
    if not isinstance(hooks_block, dict):
        return {}, ["rejected: top-level 'hooks' key is missing or not a mapping; nothing imported"]

    external: dict[str, list[dict[str, Any]]] = {}
    report: list[str] = []

    for event, matcher_groups in hooks_block.items():
        if event not in SUPPORTED_EVENTS:
            report.append(f"rejected [{event}]: no LionAGI seam for this event (unmappable)")
            continue
        if not isinstance(matcher_groups, list):
            matcher_groups = [matcher_groups]
        for group in matcher_groups:
            if not isinstance(group, dict):
                report.append(f"rejected [{event}]: matcher group must be a mapping, got {group!r}")
                continue
            matcher = group.get("matcher")
            hook_specs = group.get("hooks", [])
            if not isinstance(hook_specs, list):
                hook_specs = [hook_specs]
            translated: list[dict[str, Any]] = []
            for spec in hook_specs:
                if not isinstance(spec, dict):
                    report.append(f"rejected [{event}]: hook entry must be a mapping, got {spec!r}")
                    continue
                hook_type = spec.get("type", "command")
                if hook_type != "command":
                    report.append(
                        f"rejected [{event}] matcher={matcher!r}: handler type "
                        f"{hook_type!r} not supported (v1 executes only 'command')"
                    )
                    continue
                argv, reason = _tokenize_command(spec.get("command"))
                if argv is None:
                    report.append(f"rejected [{event}] matcher={matcher!r}: {reason}")
                    continue
                entry: dict[str, Any] = {
                    "type": "command",
                    "command": argv,
                    "source": f"imported:{source_label}",
                }
                if "timeout" in spec:
                    entry["timeout"] = spec["timeout"]
                translated.append(entry)

                note = (
                    f"imported [{event}] matcher={matcher!r} argv={argv}; verify against profile v1"
                )
                if event == "UserPromptSubmit" and source_label == "codex":
                    note += (
                        " (divergence: Codex's UserPromptSubmit schema requires "
                        "transcript_path/turn_id, which LionAGI omits when absent)"
                    )
                if event in ("PreToolUse", "PostToolUse") and matcher:
                    note += (
                        " (divergence: matcher names a harness tool name -- confirm a "
                        "LionAGI tool is registered under this exact name)"
                    )
                if isinstance(spec.get("command"), str):
                    note += (
                        " (divergence: translated from a shell-string command to an argv vector)"
                    )
                report.append(note)
            if translated:
                group_entry: dict[str, Any] = {"hooks": translated}
                if matcher is not None:
                    group_entry = {"matcher": matcher, "hooks": translated}
                external.setdefault(event, []).append(group_entry)

    return external, report


def _run_import(source: str, path: str | None, cwd: str | None) -> int:
    import yaml

    project_dir = Path(cwd) if cwd else Path.cwd()
    config_path = Path(path) if path else project_dir / _DEFAULT_CONFIG_PATH[source]
    if not config_path.is_file():
        log_error(f"no config file found at {config_path}")
        return 1

    try:
        data = json.loads(config_path.read_text())
    except json.JSONDecodeError as exc:
        log_error(f"could not parse {config_path} as JSON: {exc}")
        return 1
    except OSError as exc:
        log_error(f"could not read {config_path}: {exc}")
        return 1

    external, report = _translate_config(data, source_label=source)

    settings_path = project_dir / ".lionagi" / "settings.yaml"
    existing: dict[str, Any] = {}
    if settings_path.is_file():
        loaded = yaml.safe_load(settings_path.read_text()) or {}
        if isinstance(loaded, dict):
            existing = loaded

    hooks_external = existing.get("hooks_external")
    if not isinstance(hooks_external, dict):
        hooks_external = {}
        existing["hooks_external"] = hooks_external

    imported_count = 0
    for event, groups in external.items():
        hooks_external.setdefault(event, [])
        hooks_external[event].extend(groups)
        imported_count += sum(len(g["hooks"]) for g in groups)

    if imported_count:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        with open(settings_path, "w") as f:
            yaml.safe_dump(existing, f, sort_keys=False, allow_unicode=True)

    for line in report:
        print(line)
    print(f"\nimported {imported_count} hook(s) from {config_path} -> {settings_path}")
    if imported_count:
        print("run `li hooks trust` to approve them before they will execute.")
    return 0


def _iter_untrusted_commands(project_dir: Path) -> list[dict[str, Any]]:
    import yaml

    from lionagi.hooks.external import compute_command_hash, is_command_trusted

    settings_path = project_dir / ".lionagi" / "settings.yaml"
    if not settings_path.is_file():
        return []
    data = yaml.safe_load(settings_path.read_text()) or {}
    hooks_external = data.get("hooks_external", {}) if isinstance(data, dict) else {}
    if not isinstance(hooks_external, dict):
        return []

    pending: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()
    for event, groups in hooks_external.items():
        if not isinstance(groups, list):
            groups = [groups]
        for group in groups:
            if not isinstance(group, dict):
                continue
            hook_specs = group.get("hooks", [])
            if not isinstance(hook_specs, list):
                hook_specs = [hook_specs]
            for spec in hook_specs:
                if not isinstance(spec, dict):
                    continue
                source = spec.get("source")
                command = spec.get("command")
                if not source or not isinstance(command, list):
                    continue
                if is_command_trusted(command, source=source):
                    continue
                command_hash = compute_command_hash(command)
                if command_hash in seen_hashes:
                    continue
                seen_hashes.add(command_hash)
                pending.append(
                    {"event": event, "command": command, "source": source, "hash": command_hash}
                )
    return pending


def _run_trust(cwd: str | None, *, assume_yes: bool) -> int:
    from lionagi.plugins._user_settings import read_user_settings, write_user_settings

    project_dir = Path(cwd) if cwd else Path.cwd()
    pending = _iter_untrusted_commands(project_dir)
    if not pending:
        print("(no pending imported hook commands)")
        return 0

    print("The following imported hook commands are pending trust:")
    for p in pending:
        print(f"  [{p['event']}] source={p['source']} argv={p['command']}")

    if not assume_yes:
        answer = input("\nTrust all of the above (content-hash-pinned)? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("not trusted.")
            return 1

    settings = read_user_settings()
    trusted = settings.get("trusted_hook_commands")
    if not isinstance(trusted, list):
        trusted = []
        settings["trusted_hook_commands"] = trusted

    added = 0
    for p in pending:
        if p["hash"] not in trusted:
            trusted.append(p["hash"])
            added += 1
    write_user_settings(settings)
    print(f"trusted {added} command(s).")
    return 0


def run_hooks(args: argparse.Namespace) -> int:
    if args.hooks_command == "import":
        return _run_import(args.source, args.path, args.cwd)
    if args.hooks_command == "trust":
        return _run_trust(args.cwd, assume_yes=args.yes)
    log_error(f"unknown hooks subcommand: {args.hooks_command!r}")
    return 1
