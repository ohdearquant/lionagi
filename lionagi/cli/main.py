# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li` — lionagi command line."""

from __future__ import annotations

import argparse
import signal
import sys
from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module
from types import ModuleType

from ._logging import configure_cli_logging, log_error


def _load_agent() -> ModuleType:
    return import_module(".agent", __package__)


def _load_casts() -> ModuleType:
    return import_module(".casts", __package__)


def _load_dispatch() -> ModuleType:
    return import_module(".dispatch", __package__)


def _load_doctor() -> ModuleType:
    return import_module(".doctor", __package__)


def _load_engine() -> ModuleType:
    return import_module(".engine", __package__)


def _load_hooks() -> ModuleType:
    return import_module(".hooks", __package__)


def _load_invoke() -> ModuleType:
    return import_module(".invoke", __package__)


def _load_kill() -> ModuleType:
    return import_module(".kill", __package__)


def _load_mirror() -> ModuleType:
    return import_module(".mirror", __package__)


def _load_monitor() -> ModuleType:
    return import_module(".monitor", __package__)


def _load_orchestrate() -> ModuleType:
    return import_module(".orchestrate", __package__)


def _load_plugin() -> ModuleType:
    return import_module(".plugin", __package__)


def _load_state() -> ModuleType:
    return import_module(".state", __package__)


def _load_stats() -> ModuleType:
    return import_module(".stats", __package__)


def _load_team() -> ModuleType:
    return import_module(".team", __package__)


def _load_studio() -> ModuleType:
    return import_module("lionagi.studio.cli")


@dataclass(frozen=True)
class _CommandSpec:
    name: str
    help: str
    loader: Callable[[], ModuleType]
    parser_factory: str
    handler: str
    aliases: tuple[str, ...] = ()


_COMMAND_REGISTRY = (
    _CommandSpec(
        "orchestrate",
        "Multi-agent orchestration patterns.",
        _load_orchestrate,
        "add_orchestrate_subparser",
        "run_orchestrate",
        ("o",),
    ),
    _CommandSpec(
        "agent",
        "Spawn one-shot subagent (blocking); prints final response.",
        _load_agent,
        "add_agent_subparser",
        "run_agent",
    ),
    _CommandSpec(
        "casts",
        "inspect built-in roles and modes",
        _load_casts,
        "add_casts_subparser",
        "run_casts",
    ),
    _CommandSpec(
        "engine",
        "Run domain-specific multi-agent engine pipelines.",
        _load_engine,
        "add_engine_subparser",
        "run_engine",
    ),
    _CommandSpec(
        "team",
        "Team messaging — send/receive between named agents.",
        _load_team,
        "add_team_subparser",
        "run_team",
    ),
    _CommandSpec(
        "studio",
        "Lion Studio server",
        _load_studio,
        "add_studio_subparser",
        "run_studio",
    ),
    _CommandSpec(
        "schedule",
        "Manage lionagi Studio schedules.",
        _load_studio,
        "add_schedule_subparser",
        "run_schedule",
    ),
    _CommandSpec(
        "state",
        "Inspect and migrate lionagi state.db.",
        _load_state,
        "add_state_subparser",
        "run_state",
    ),
    _CommandSpec(
        "invoke",
        "Track a skill-level orchestration.",
        _load_invoke,
        "add_invoke_subparser",
        "run_invoke",
    ),
    _CommandSpec(
        "kill",
        "Terminate a running entity (run/session/play/show).",
        _load_kill,
        "add_kill_subparser",
        "run_kill",
    ),
    _CommandSpec(
        "mirror",
        "Mirror Claude Code sessions into studio (live).",
        _load_mirror,
        "add_mirror_subparser",
        "run_mirror",
    ),
    _CommandSpec(
        "monitor",
        "Observe play/agent/run progress in real-time.",
        _load_monitor,
        "add_monitor_subparser",
        "run_monitor",
        ("mon",),
    ),
    _CommandSpec(
        "dispatch",
        "Inspect and acknowledge durable dispatch_outbox rows.",
        _load_dispatch,
        "add_dispatch_subparser",
        "run_dispatch",
    ),
    _CommandSpec(
        "doctor",
        "Check the lionagi CLI environment/install for common failure modes.",
        _load_doctor,
        "add_doctor_subparser",
        "run_doctor",
    ),
    _CommandSpec(
        "stats",
        "Read-only aggregate reporting over lionagi's StateDB.",
        _load_stats,
        "add_stats_subparser",
        "run_stats",
    ),
    _CommandSpec(
        "plugin",
        "Inspect, trust, and enable/disable LionAGI plugin bundles.",
        _load_plugin,
        "add_plugin_subparser",
        "run_plugin",
    ),
    _CommandSpec(
        "hooks",
        "Import Claude Code / Codex hook configs; trust imported hook commands.",
        _load_hooks,
        "add_hooks_subparser",
        "run_hooks",
    ),
)
_COMMAND_BY_NAME = {
    command_name: spec for spec in _COMMAND_REGISTRY for command_name in (spec.name, *spec.aliases)
}


def _build_parser(selected: _CommandSpec | None) -> tuple[argparse.ArgumentParser, object | None]:
    parser = argparse.ArgumentParser(
        prog="li",
        description="lionagi command line — spawn subagents via any CLI-backed provider.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {_get_version()}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    # Every command is registered for usage/error listing; only the selected
    # one loads its real parser module, the rest stay metadata-only stubs.
    selected_parser = None
    for spec in _COMMAND_REGISTRY:
        if selected is not None and spec.name == selected.name:
            factory = getattr(selected.loader(), selected.parser_factory)
            selected_parser = factory(subparsers)
        else:
            subparsers.add_parser(spec.name, aliases=list(spec.aliases), help=spec.help)
    return parser, selected_parser


# These forwarding functions preserve the main module's existing patch points
# without importing a command implementation until that command is dispatched.
def run_agent(args: argparse.Namespace) -> int:
    return _load_agent().run_agent(args)


def run_casts(args: argparse.Namespace) -> int:
    return _load_casts().run_casts(args)


def run_dispatch(args: argparse.Namespace) -> int:
    return _load_dispatch().run_dispatch(args)


def run_doctor(args: argparse.Namespace) -> int:
    return _load_doctor().run_doctor(args)


def run_engine(args: argparse.Namespace) -> int:
    return _load_engine().run_engine(args)


def run_invoke(args: argparse.Namespace) -> int:
    return _load_invoke().run_invoke(args)


def run_kill(args: argparse.Namespace) -> int:
    return _load_kill().run_kill(args)


def run_mirror(args: argparse.Namespace) -> int:
    return _load_mirror().run_mirror(args)


def run_monitor(args: argparse.Namespace) -> int:
    return _load_monitor().run_monitor(args)


def run_orchestrate(args: argparse.Namespace) -> int:
    return _load_orchestrate().run_orchestrate(args)


def run_hooks(args: argparse.Namespace) -> int:
    return _load_hooks().run_hooks(args)


def run_plugin(args: argparse.Namespace) -> int:
    return _load_plugin().run_plugin(args)


def run_schedule(args: argparse.Namespace) -> int:
    return _load_studio().run_schedule(args)


def run_state(args: argparse.Namespace) -> int:
    return _load_state().run_state(args)


def run_stats(args: argparse.Namespace) -> int:
    return _load_stats().run_stats(args)


def run_studio(args: argparse.Namespace) -> int:
    return _load_studio().run_studio(args)


def run_team(args: argparse.Namespace) -> int:
    return _load_team().run_team(args)


def run_skill(argv: list[str]) -> int:
    return import_module(".skill", __package__).run_skill(argv)


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
    print(
        "\nCommon flags (forwarded to `li o flow`):\n"
        "  --bypass              Bypass all codex approvals/sandbox\n"
        "  --team-mode [NAME]    Create a fresh team for this flow\n"
        "  --timeout SECONDS     Hard wall-clock timeout\n"
        "  --save DIR            Save outputs to directory\n"
        "  --cwd DIR             Working directory for CLI endpoints\n"
        "  --effort LEVEL        Override effort level\n"
        "  --yolo                Auto-approve tool calls\n"
        "\n  Full list: li o flow --help"
    )
    return 0


# The unknown-subfield warning is shared with the runtime spec validator
# (warn_unknown_artifact_keys in lionagi/state/artifact_verifier.py).


def _handle_play_check(argv: list[str]) -> int:
    """`li play check <name>` — ADR-0064 D3 pre-flight artifact-contract validation; does not fire the playbook."""
    if not argv or argv[0].startswith("-"):
        print("Usage: li play check <name>")
        return 1
    name = argv[0]

    from lionagi.cli.orchestrate import _load_flow_spec, _resolve_playbook_path
    from lionagi.state.artifact_verifier import (
        ArtifactPathError,
        resolve_artifact_contract,
        warn_unknown_artifact_keys,
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
    # Load the named agent profile so artifact_defaults join the merge; must
    # FAIL here (not green-light) when the real `li play` path would raise.
    agent_defaults = None
    agent_name = spec.get("agent")
    if agent_name:
        try:
            from lionagi.cli._providers import load_agent_profile

            profile = load_agent_profile(agent_name)
            agent_defaults = getattr(profile, "artifact_defaults", None)
        except Exception as exc:  # noqa: BLE001 — match runtime behaviour
            log_error(
                f"playbook '{name}' references agent profile "
                f"'{agent_name}' but it could not be loaded: {exc}. "
                f"Real `li play {name}` will fail at execution start; "
                f"fix the profile or remove the `agent:` field."
            )
            return 1

    if not artifacts_block and not agent_defaults:
        print(f"playbook '{name}': no `artifacts:` block declared (verification skipped).")
        return 0

    if artifacts_block:
        # Same warning the runtime validator emits via logger.warning;
        # printed here so it's visible on the pre-flight terminal.
        warn_unknown_artifact_keys(artifacts_block, source=f"playbook '{name}'")

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
        return _handle_play_check(rest[1:])
    if head == "status":
        from .status import run_play_status

        return run_play_status(rest[1:])
    if head == "--resume":
        return ["o", "flow", *rest]

    if not head.startswith("-"):
        # NAME comes first — fast path. Custom playbook args (from the
        # playbook's own `args:` schema) are only recognized once they
        # follow NAME, so this path leaves them untouched.
        name, other = head, rest[1:]
    else:
        # A flag precedes NAME; probe with the flow subparser's base flags
        # only (playbook-specific args aren't injected yet) just to locate
        # NAME — see docs/internals/cli.md. Custom flags before NAME aren't
        # supported; they must follow it.
        probe_parser = argparse.ArgumentParser(prog="li", add_help=False)
        probe_sub = probe_parser.add_subparsers(dest="command")
        fl_probe = _load_orchestrate().add_orchestrate_subparser(probe_sub)["flow"]
        if "--" in rest:
            i = rest.index("--")
            p_head, p_post = rest[:i], rest[i + 1 :]
        else:
            p_head, p_post = rest, []
        # Strip help tokens from the probe input only (argparse would print
        # flow help and exit); the reconstruction below still sees them.
        p_head_probe = [t for t in p_head if t not in ("--help", "-h")]
        p_ns, p_extras = fl_probe.parse_known_args(p_head_probe)
        unknown = [e for e in p_extras if e.startswith("-") and e != "-"]
        if unknown:
            log_error(f"unrecognized arguments: {' '.join(unknown)}")
            return 1
        bare = [*(p_ns.query or []), *p_extras, *p_post]
        if not bare:
            log_error(
                "playbook NAME is required\n"
                'Usage: li play <name> "<prompt>" [--bypass --team-mode TEAM --timeout N ...]\n'
                "Flags may appear anywhere relative to NAME and the prompt."
            )
            return 1
        name = bare[0]
        # Remove NAME from the partition it was selected from, never by
        # string value across argv (an earlier flag VALUE equal to NAME
        # must not be deleted in its place).
        if p_ns.query or p_extras:
            head_tokens = list(p_head)
            head_tokens.remove(name)
            other = head_tokens + (["--", *p_post] if p_post else [])
        else:
            other = [*p_head, "--", *p_post[1:]]

    if "--help" in other or "-h" in other:
        return _print_playbook_help(name)
    # Rewrite `play [...] <name> [...]` → `o flow -p <name> [...]`
    return ["o", "flow", "-p", name, *other]


def _get_version() -> str:
    from lionagi.version import __version__

    return __version__


def main(argv: list[str] | None = None) -> int:
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)

    # Resolve verbose before any CLI code emits (argparse hasn't run yet).
    _argv = argv if argv is not None else sys.argv[1:]
    # Scan only before the '--' sentinel so a scheduled action_prompt
    # containing '--verbose' can't flip verbose mode.
    try:
        _sentinel_idx = _argv.index("--")
        _pre_sentinel = _argv[:_sentinel_idx]
    except ValueError:
        _pre_sentinel = _argv
    verbose = "-v" in _pre_sentinel or "--verbose" in _pre_sentinel
    configure_cli_logging(verbose)

    # Same pre-argparse scan, so a project-scoped .lionagi/settings.yaml
    # next to a `--cwd DIR` target isn't missed in favor of the shell's cwd.
    _cwd_override: str | None = None
    for _i, _tok in enumerate(_pre_sentinel):
        if _tok == "--cwd" and _i + 1 < len(_pre_sentinel):
            _cwd_override = _pre_sentinel[_i + 1]
            break
        if _tok.startswith("--cwd="):
            _cwd_override = _tok.split("=", 1)[1]
            break

    # The first of the two settings-driven notify bootstrap points (Studio
    # service startup is the other); resolution failures are swallowed inside.
    from lionagi.state.lifecycle.notify_settings import register_settings_terminal_callback

    register_settings_terminal_callback(project_dir=_cwd_override)

    # `li skill NAME` dispatches directly, never falling through to argparse.
    if _argv and _argv[0] == "skill":
        return run_skill(_argv[1:])

    # `li play NAME [...]` is sugar for `li o flow -p NAME [...]`; rewrite
    # argv before argparse runs (also handles `li play list`).
    rewritten = _handle_play_shortcut(_argv)
    if isinstance(rewritten, int):
        return rewritten
    _argv = rewritten

    # `li agent status` is a pure-read surface, not a prompt to send — must
    # be intercepted before intermixed agent-flag parsing below.
    if _argv and _argv[0] == "agent" and len(_argv) > 1 and _argv[1] == "status":
        from .status import run_agent_status

        return run_agent_status(_argv[2:])

    # `li monitor run <id>` is a wait-for-terminal primitive; intercepted so
    # argparse's positional `id` slot doesn't swallow "run" as an entity-id.
    if _argv and _argv[0] in ("monitor", "mon") and len(_argv) > 1 and _argv[1] == "run":
        from .monitor import run_monitor_wait

        return run_monitor_wait(_argv[2:])

    # `li wait <id> [<id2> ...]` — ADR-0035 completion contract; intercepted
    # for the same reason as `monitor run` above.
    if _argv and _argv[0] == "wait":
        from .wait import run_wait

        return run_wait(_argv[1:])

    selected = _COMMAND_BY_NAME.get(_argv[0]) if _argv else None
    try:
        parser, selected_parser = _build_parser(selected)
    except Exception as exc:
        # A lazy command module that fails to import surfaces here at
        # dispatch; report it as a command-scoped error, not a traceback.
        log_error(f"command {_argv[0]!r} failed to load: {type(exc).__name__}: {exc}")
        return 1

    # `li o flow -p NAME`: inject the playbook's declared args as flags on
    # the flow sub-parser before argparse runs, so prompts don't swallow them.
    orch_parsers: dict[str, argparse.ArgumentParser] | None = None
    if selected is _COMMAND_BY_NAME["orchestrate"]:
        orch_parsers = selected_parser
        _load_orchestrate().inject_playbook_schema_into_parser(orch_parsers["flow"], _argv)

    # `li agent` parses standalone so flags may appear anywhere relative to
    # [MODEL] PROMPT. parse_intermixed_args is unusable: it drops the `--`
    # sentinel between passes, letting a prompt like "--bypass" after `--`
    # toggle real flags on re-parse. Split at the sentinel ourselves instead.
    if selected is _COMMAND_BY_NAME["agent"]:
        agent_parser = selected_parser
        tail = _argv[1:]
        if "--" in tail:
            i = tail.index("--")
            head, post = tail[:i], tail[i + 1 :]
        else:
            head, post = tail, []
        args, extras = agent_parser.parse_known_args(head)
        unknown = [e for e in extras if e.startswith("-") and e != "-"]
        if unknown:
            agent_parser.error(f"unrecognized arguments: {' '.join(unknown)}")
        args.query = [*(args.query or []), *extras, *post]
        return run_agent(args)

    # `li o flow` / `li o fanout` parse standalone for the same reason as
    # `agent` above (nested subparser dispatch can't intermix flags with
    # the [MODEL] PROMPT positionals). See docs/internals/cli.md.
    if (
        _argv
        and selected is _COMMAND_BY_NAME["orchestrate"]
        and len(_argv) > 1
        and _argv[1] in ("fanout", "flow")
    ):
        sub_name = _argv[1]
        assert orch_parsers is not None
        sub_parser = orch_parsers[sub_name]
        tail = _argv[2:]
        if "--" in tail:
            i = tail.index("--")
            head, post = tail[:i], tail[i + 1 :]
        else:
            head, post = tail, []
        args, extras = sub_parser.parse_known_args(head)
        unknown = [e for e in extras if e.startswith("-") and e != "-"]
        if unknown:
            sub_parser.error(f"unrecognized arguments: {' '.join(unknown)}")
        args.query = [*(args.query or []), *extras, *post]
        args.command = "orchestrate"
        args.orch_command = sub_name
        return run_orchestrate(args)

    # `li schedule ...` parses its own subparser directly (mirroring the
    # `agent` special-case above) so an unrecognized flag gets a one-line
    # "did you mean --X?" suggestion instead of argparse's generic usage dump.
    if selected is _COMMAND_BY_NAME["schedule"]:
        schedule_parser = selected_parser
        ns, extras = schedule_parser.parse_known_args(_argv[1:])
        if extras:
            from lionagi.studio.cli import suggest_schedule_flag

            # Did-you-mean only applies to dash-prefixed tokens; a bare
            # positional has no "real flag" to guess.
            for tok in extras:
                if tok.startswith("-") and tok != "-":
                    suggestion = suggest_schedule_flag(tok)
                    if suggestion:
                        log_error(f"unrecognized argument {tok!r} — did you mean {suggestion!r}?")
                        continue
                log_error(f"unrecognized argument: {tok}")
            return 2
        return run_schedule(ns)

    args = parser.parse_args(_argv)

    if selected is not None:
        return globals()[selected.handler](args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
