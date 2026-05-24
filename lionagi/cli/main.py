# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li` — lionagi command line.

Examples:
    li agent claude/sonnet "Write a Python function to reverse a string."
    li agent codex/gpt-5.3-codex "..."
    li agent -r <branch-id> "follow-up prompt"
    li agent claude -c "follow-up prompt"

    li o fanout codex/gpt-5.4-xhigh "audit for dead code" -n 3
    li o fanout claude/sonnet "suggest approaches" -n 3 --with-synthesis claude/opus-4-6-medium
"""

from __future__ import annotations

import argparse
import signal
import sys

from ._logging import configure_cli_logging, log_error
from .agent import add_agent_subparser, run_agent
from .invoke import add_invoke_subparser, run_invoke
from .orchestrate import (
    add_orchestrate_subparser,
    inject_playbook_schema_into_parser,
    run_orchestrate,
)
from .skill import run_skill
from .state import add_state_subparser, run_state
from .studio import add_studio_subparser, run_studio
from .team import add_team_subparser, run_team


def _print_playbook_help(name: str) -> int:
    """Print playbook-specific help: description, arguments, and usage."""
    from .orchestrate import _load_flow_spec, _resolve_playbook_path

    path, err = _resolve_playbook_path(name)
    if err is not None:
        log_error(err)
        return 1
    spec = _load_flow_spec(str(path))
    if not isinstance(spec, dict):
        log_error(f"failed to load playbook: {name}")
        return 1

    desc = spec.get("description", "").strip()
    args_schema = spec.get("args", {})
    hint = spec.get("argument-hint", "")

    print(f"Playbook: {name}")
    if desc:
        print(f"\n  {desc}\n")
    print(f"Usage: li play {name} {hint or '[args...] PROMPT'}")

    if isinstance(args_schema, dict) and args_schema:
        print("\nArguments:")
        for arg_name, field in args_schema.items():
            if not isinstance(field, dict):
                continue
            flag = f"--{arg_name.replace('_', '-')}"
            help_text = field.get("help", "")
            default = field.get("default")
            default_str = f" (default: {default})" if default not in (None, "") else ""
            print(f"  {flag:<24} {help_text}{default_str}")

    print(f'\nRun: li play {name} "<prompt>"')
    return 0


_ARTIFACT_ENTRY_ALLOWED_KEYS = frozenset({"id", "path", "required", "description", "source"})


def _warn_unknown_artifact_keys(artifacts_block: dict, *, name: str) -> None:
    """Emit a CLI warning for unknown subfields under each artifact entry.

    ADR-0029 §2 declares `id, path, required, description` as v1. `kind`,
    `min_size`, `mime_type` are reserved for v1.1 — silently accepting
    them in v1 lets contract files drift into looking stricter than the
    executor actually is. Warn so the author sees what was ignored.
    """
    expected = artifacts_block.get("expected") or []
    if not isinstance(expected, list):
        return
    for entry in expected:
        if not isinstance(entry, dict):
            continue
        unknown = set(entry.keys()) - _ARTIFACT_ENTRY_ALLOWED_KEYS
        if unknown:
            print(
                f"warning: playbook '{name}' artifact entry "
                f"{entry.get('id', '<unnamed>')!r} has unknown subfield(s) "
                f"{sorted(unknown)} (ignored by v1; reserved for v1.1)."
            )


def _handle_play_check(argv: list[str]) -> int:
    """`li play check <name>` — ADR-0029 §9 pre-flight contract validation.

    Loads the playbook, optionally loads the agent profile it names so
    `artifact_defaults` participate in the merge (matching what a real
    invocation will see), resolves the contract via
    :func:`lionagi.state.artifact_verifier.resolve_artifact_contract`,
    and pretty-prints the result. Does not fire the playbook.
    """
    if not argv or argv[0].startswith("-"):
        print("Usage: li play check <name>")
        return 1
    name = argv[0]

    from lionagi.cli.orchestrate import _load_flow_spec, _resolve_playbook_path
    from lionagi.state.artifact_verifier import (
        ArtifactPathError,
        resolve_artifact_contract,
    )

    path, err = _resolve_playbook_path(name)
    if err is not None:
        log_error(err)
        return 1
    spec = _load_flow_spec(str(path))
    if not isinstance(spec, dict):
        log_error(f"could not parse playbook spec at {path}")
        return 1

    artifacts_block = spec.get("artifacts")
    # Load the agent profile the playbook names so its artifact_defaults
    # participate in the merge — a real invocation sees them.
    agent_defaults = None
    agent_name = spec.get("agent")
    if agent_name:
        try:
            from lionagi.cli._agents import load_agent_profile

            profile = load_agent_profile(agent_name)
            agent_defaults = getattr(profile, "artifact_defaults", None)
        except Exception as exc:  # noqa: BLE001 — best-effort pre-flight
            print(
                f"warning: could not load agent profile '{agent_name}' "
                f"({exc}); checking playbook contract only."
            )

    if not artifacts_block and not agent_defaults:
        print(f"playbook '{name}': no `artifacts:` block declared (verification skipped).")
        return 0

    if artifacts_block:
        _warn_unknown_artifact_keys(artifacts_block, name=name)

    try:
        resolved = resolve_artifact_contract(
            playbook_artifacts=artifacts_block,
            agent_defaults=agent_defaults,
        )
    except ArtifactPathError as exc:
        log_error(f"playbook '{name}' artifact contract invalid: {exc}")
        return 1

    if resolved is None:
        print(f"playbook '{name}': empty contract (no expected artifacts).")
        return 0

    expected = resolved.get("expected", [])
    required = [e for e in expected if e.get("required", True)]
    optional = [e for e in expected if not e.get("required", True)]
    sources = [e.get("source") for e in expected]
    from_playbook = sum(1 for s in sources if s == "playbook")
    from_agent = sum(1 for s in sources if s == "agent_profile")
    print(f"playbook '{name}' artifact contract:")
    print(f"  expected: {len(expected)} ({len(required)} required, {len(optional)} optional)")
    if from_playbook or from_agent:
        print(f"  sources:  {from_playbook} from playbook, {from_agent} from agent_profile")
    for e in expected:
        flag = "REQUIRED" if e.get("required", True) else "OPTIONAL"
        src = e.get("source", "?")
        desc = e.get("description") or ""
        suffix = f" — {desc}" if desc else ""
        print(f"  [{flag}] {e['id']}  →  {e['path']}  (from {src}){suffix}")
    return 0


def _handle_play_shortcut(argv: list[str]) -> list[str] | int:
    """Expand `li play` sugar into `li o flow -p NAME ...`.

    Returns the rewritten argv (list[str]), or an exit code (int) if the
    subcommand fully handled the invocation (e.g. `li play list`).
    """
    from pathlib import Path

    if not argv or argv[0] != "play":
        return argv
    rest = argv[1:]
    if not rest:
        print("Usage: li play <name> [args...]  |  li play list")
        return 1
    head = rest[0]
    if head == "list":
        root = Path("~/.lionagi/playbooks").expanduser()
        if not root.is_dir():
            print(f"(no playbooks directory at {root})")
            return 0
        names = sorted(p.name.removesuffix(".playbook.yaml") for p in root.glob("*.playbook.yaml"))
        if not names:
            print(f"(no playbooks in {root})")
            return 0
        for name in names:
            print(name)
        return 0
    if head == "check":
        # ADR-0029 §9: pre-flight artifact-contract validation.
        # `li play check <name>` loads the playbook, resolves its
        # artifact contract, and prints the result without firing.
        return _handle_play_check(rest[1:])
    if head.startswith("-"):
        log_error("li play NAME must come before flags")
        return 1
    if "--help" in rest[1:] or "-h" in rest[1:]:
        return _print_playbook_help(head)
    # Rewrite `play <name> [...]` → `o flow -p <name> [...]`
    return ["o", "flow", "-p", head, *rest[1:]]


def _get_version() -> str:
    from lionagi.version import __version__

    return __version__


def main(argv: list[str] | None = None) -> int:
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)

    # Resolve verbose once before any CLI code emits. argparse happens
    # below but we need the flag now to configure log levels.
    _argv = argv if argv is not None else sys.argv[1:]
    verbose = "-v" in _argv or "--verbose" in _argv
    configure_cli_logging(verbose)

    # `li skill NAME` prints a CC-compatible skill body to stdout.
    # Never falls through to argparse — dispatch directly.
    if _argv and _argv[0] == "skill":
        return run_skill(_argv[1:])

    # `li play NAME [...]` is sugar for `li o flow -p NAME [...]`.
    # Rewrite argv before argparse runs. Also handles `li play list`.
    rewritten = _handle_play_shortcut(_argv)
    if isinstance(rewritten, int):
        return rewritten
    _argv = rewritten

    parser = argparse.ArgumentParser(
        prog="li",
        description="lionagi command line — spawn subagents via any CLI-backed provider.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {_get_version()}",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    orch_parsers = add_orchestrate_subparser(sub)
    add_agent_subparser(sub)
    add_team_subparser(sub)
    add_studio_subparser(sub)
    add_state_subparser(sub)
    add_invoke_subparser(sub)

    # If the user is invoking `li o flow -p NAME`, inject the playbook's
    # declared args as flags on the flow sub-parser BEFORE argparse runs,
    # so positional prompts don't swallow flag values.
    inject_playbook_schema_into_parser(orch_parsers["flow"], _argv)

    args = parser.parse_args(_argv)

    if args.command in ("orchestrate", "o"):
        return run_orchestrate(args)

    if args.command == "agent":
        return run_agent(args)

    if args.command == "team":
        return run_team(args)

    if args.command == "studio":
        return run_studio(args)

    if args.command == "state":
        return run_state(args)

    if args.command == "invoke":
        return run_invoke(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
