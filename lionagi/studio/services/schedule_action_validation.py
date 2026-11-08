# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Validation for scheduled action fields and launch configuration."""

from __future__ import annotations

import os
import re
import shlex

from lionagi._spec_limits import MAX_SPEC_PROMPT_CHARS
from lionhub.orchestration.schedule.parsing import (
    _ALIAS_ACTION_KINDS,
    _MODEL_RE,
    _VALID_ACTION_KINDS,
    _validate_identifier,
)

_PLAY_SUBCOMMANDS: frozenset[str] = frozenset({"list", "check", "status"})


def _validate_action_model(model: str) -> None:
    """Raise ValueError if *model* could inject a CLI flag into the spawned li process (CWE-88)."""
    if not model:
        return
    if model.startswith("-"):
        raise ValueError(
            f"action_model {model!r} starts with '-' and would inject a CLI flag "
            "into the spawned li process. Provide a valid model identifier."
        )
    if not _MODEL_RE.match(model):
        raise ValueError(
            f"action_model {model!r} contains characters not allowed in a model "
            "identifier. Allowed: letters, digits, '_', '.', '/', ':', '@', '-'."
        )


def _validate_extra_args(extra: list) -> None:
    """Raise ValueError if any element starts with '-' (CWE-88 flag injection).

    Note: agent/flow/fanout parsers don't accept extra positionals; non-empty
    action_extra_args causes those subcommands to exit rc 2 at fire time.
    """
    for item in extra:
        token = str(item)
        if token.startswith("-"):
            raise ValueError(
                f"action_extra_args element {token!r} starts with '-' and would "
                "inject a CLI flag into the spawned li process. Only positional "
                "(non-flag) tokens are permitted in action_extra_args."
            )


_ENGINE_OPTION_KEYS = frozenset({"max_depth", "max_agents", "test_cmd", "export_dir"})
_ENGINE_OPT_VALUE_RE = re.compile(r"^[a-zA-Z0-9_./:@\-\+= ]+$")


def _validate_engine_options(opts: object) -> None:
    """Raise ValueError if *opts* fails engine-option safety checks."""
    if not opts:
        return
    if not isinstance(opts, dict):
        raise ValueError("action_engine_options must be an object")
    unknown = set(opts) - _ENGINE_OPTION_KEYS
    if unknown:
        raise ValueError(
            f"action_engine_options contains unknown key(s) {sorted(unknown)}. "
            f"Allowed: {sorted(_ENGINE_OPTION_KEYS)}"
        )
    for key in ("max_depth", "max_agents"):
        val = opts.get(key)
        if val is None:
            continue
        if not isinstance(val, int) or isinstance(val, bool) or not (1 <= val <= 100):
            raise ValueError(f"action_engine_options.{key} must be an integer in [1, 100]")
    for key in ("test_cmd", "export_dir"):
        val = opts.get(key)
        if val is None:
            continue
        if not isinstance(val, str) or val.startswith("-") or not _ENGINE_OPT_VALUE_RE.match(val):
            raise ValueError(
                f"action_engine_options.{key} must be a plain token that does not "
                "start with '-' and contains no shell metacharacters"
            )


_COMMAND_RE = re.compile(r"^[a-zA-Z0-9_.-]+$")
_COMMAND_ALLOWLIST_ENV = "LIONAGI_SCHEDULER_COMMAND_ALLOWLIST"


def _validate_action_command(command: str) -> None:
    """Raise ValueError if *command* is not a safe, bare executable name."""
    if not command:
        raise ValueError("action_command is required for action_kind='command'")
    if "/" in command or "\\" in command:
        raise ValueError(
            f"action_command {command!r} must not contain path separators; it "
            "is resolved via PATH at spawn time, not treated as a filesystem path."
        )
    if command.startswith("-"):
        raise ValueError(
            f"action_command {command!r} starts with '-' and is not a valid executable name."
        )
    if not _COMMAND_RE.match(command):
        raise ValueError(
            f"action_command {command!r} contains characters not allowed in an "
            "executable name. Allowed: letters, digits, '_', '.', '-'."
        )


def _command_allowlist() -> frozenset[str]:
    """Read the allow-listed command names from the environment, split on ','.

    Read fresh on every call rather than cached at import time, so a
    spawn-time re-check after the env has changed since schedule-build time
    actually observes the change.
    """
    raw = os.environ.get(_COMMAND_ALLOWLIST_ENV, "")
    return frozenset(tok.strip() for tok in raw.split(",") if tok.strip())


def _validate_command_allowlisted(command: str) -> None:
    """Raise ValueError if *command* is not on ``LIONAGI_SCHEDULER_COMMAND_ALLOWLIST``.

    Called at schedule build/validation time (services/schedules.py,
    services/schedule_declaration.py) and again here inside ``build_argv`` at
    actual spawn time, since the environment variable can change between
    schedule creation and fire.

    The declaration and spawn checks can run in processes with different
    environments. The declaration path checks its caller's environment; the
    spawn path checks the scheduler daemon's environment. The refusal identifies
    the process whose configuration it read.
    """
    allowlist = _command_allowlist()
    if command in allowlist:
        return
    raw = os.environ.get(_COMMAND_ALLOWLIST_ENV)
    if raw is None:
        read = "which is not set in the environment of this process"
    elif not allowlist:
        read = f"which this process's environment sets to {raw!r}, naming no command"
    else:
        read = f"which this process's environment sets to {sorted(allowlist)!r}"
    permitted = ",".join(sorted(allowlist | {command}))
    raise ValueError(
        f"action_command {command!r} is not in {_COMMAND_ALLOWLIST_ENV}, {read} "
        f"(pid {os.getpid()}). That is a property of the process running this check, not of the "
        "document being checked: the scheduler daemon reads the same variable from its OWN "
        "environment and checks again at spawn time, so an empty value here says nothing about "
        "what the daemon will permit. Set this variable in the scheduler daemon's "
        "environment and restart the scheduler daemon to permit the command: "
        f"{_COMMAND_ALLOWLIST_ENV}={shlex.quote(permitted)}"
    )


_RESERVED_FLOW_FLAGS: frozenset[str] | None = None


def _reserved_flow_flags() -> frozenset[str]:
    """Option strings of the base `li o flow` parser, derived not hand-kept.

    `li play NAME --key ...` rewrites to `li o flow -p NAME --key ...`, so a
    playbook-arg key whose flag form matches a base flow option would be
    parsed as that built-in flag (e.g. ``bypass`` -> ``--bypass`` disabling
    approval controls) instead of reaching the playbook's declared args.
    Deriving the set from the parser itself means a flag added to flow later
    is reserved here automatically.
    """
    global _RESERVED_FLOW_FLAGS
    if _RESERVED_FLOW_FLAGS is None:
        import argparse

        from lionhub.orchestration.flow.parsing import add_orchestrate_subparser

        probe = argparse.ArgumentParser(prog="li", add_help=False)
        flow = add_orchestrate_subparser(probe.add_subparsers(dest="command"))["flow"]
        _RESERVED_FLOW_FLAGS = frozenset(
            option
            for action in flow._actions
            for option in action.option_strings
            if option.startswith("--")
        )
    return _RESERVED_FLOW_FLAGS


def _playbook_args(schedule: dict) -> object:
    """The typed playbook args a play launch passes to ``li play``, as stored.

    A one-shot launch stores them on ``action_playbook_args``. A scheduled row
    has no such column: a declaration's args ride in the target snapshot the row
    resolves, so they are read from there. Either way the same validation and the
    same flags apply, so a declared arg reaches the child instead of being
    dropped. A stored value of the wrong shape is returned as it is, so the
    validation refuses it rather than the launch running with no args.
    """
    declared = schedule.get("action_playbook_args")
    # An empty object is what a launch with no args stores; anything else, an
    # empty list or string included, is a stored value and goes to validation.
    if declared is not None and declared != {}:
        return declared
    snapshot = schedule.get("resolved_target")
    if isinstance(snapshot, dict):
        target = snapshot.get("target")
        if isinstance(target, dict) and target.get("kind") == "playbook":
            args = target.get("args")
            if args is not None:
                return args
    return {}


def _validate_playbook_args(pb_args: object) -> None:
    """Validate a play launch's typed playbook args before argv construction.

    Keys become `--key` flags (underscores map to dashes, matching the play
    CLI's own schema-derived flags), so they must be clean identifiers that do
    not shadow a built-in flow flag; values ride as the following token, so a
    leading '-' would be re-parsed as a flag (CWE-88) and is rejected.
    """
    if not isinstance(pb_args, dict):
        raise ValueError("action_playbook_args must be an object of arg name -> value")
    import re

    for key, value in pb_args.items():
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", key):
            raise ValueError(f"action_playbook_args key {key!r} is not a valid playbook arg name")
        if "--" + key.replace("_", "-") in _reserved_flow_flags():
            raise ValueError(
                f"action_playbook_args key {key!r} collides with a built-in flow "
                "CLI flag and would change how the run is spawned rather than "
                "reach the playbook's own declared args"
            )
        if not isinstance(value, (str, int, float, bool)):
            raise ValueError(
                f"action_playbook_args[{key!r}] must be a scalar, got {type(value).__name__}"
            )
        if isinstance(value, str) and value.startswith("-"):
            raise ValueError(
                f"action_playbook_args[{key!r}] starts with '-' and would inject "
                "a CLI flag into the spawned li process"
            )


def _validate_prompt(prompt: str) -> None:
    """Raise ValueError when *prompt* cannot be admitted safely.

    build_argv places '--' before positionals, so a prompt value of exactly
    '--' would be consumed by argparse as the separator, never reaching the
    runner. The shared character cap also keeps stored and rendered schedule
    prompts aligned with the orchestration surfaces instead of accepting work
    that can only fail when it fires.
    """
    if len(prompt) > MAX_SPEC_PROMPT_CHARS:
        raise ValueError(
            f"action_prompt exceeds maximum length of {MAX_SPEC_PROMPT_CHARS} characters"
        )
    if prompt == "--":
        raise ValueError(
            "action_prompt value '--' is not allowed: the literal end-of-options "
            "token would be silently consumed by argparse rather than reaching the "
            "runner as prompt text. Use any other prompt content."
        )


def _sends_prompt(schedule: dict, kind: str | None) -> bool:
    # A command launch sends its arguments, a flow_yaml launch its spec file as written and a play
    # launch its prompt only beside a playbook; every other kind sends its action_prompt.
    if kind in ("command", "flow_yaml"):
        return False
    return kind != "play" or bool(schedule.get("action_playbook"))


def validate_launch_fields(schedule: dict) -> str:
    """Check every field a launch reads as stored, and return its action kind with a legacy
    alias resolved. ``build_argv`` runs this before it renders anything, and a manual trigger
    runs it before its placeholder check, so a schedule whose stored fields cannot launch is
    refused for that. The prompt as rendered is checked by ``build_argv`` after rendering.
    """
    kind = schedule.get("action_kind")
    kind = _ALIAS_ACTION_KINDS.get(kind, kind)  # normalize legacy alias
    if kind not in _VALID_ACTION_KINDS:
        raise ValueError(
            f"Unknown action_kind {kind!r}. Valid kinds: {sorted(_VALID_ACTION_KINDS)}"
        )
    agent = schedule.get("action_agent")
    playbook = schedule.get("action_playbook")
    project = schedule.get("action_project")
    extra = schedule.get("action_extra_args") or []

    # Reject flag-injection vectors before touching argv (defense-in-depth,
    # mirrors services/schedules.py). action_prompt is validated AFTER
    # rendering in build_argv since a templated '{{payload}}' can render to the
    # forbidden '--' sentinel; other fields aren't templated.
    _validate_action_model(schedule.get("action_model") or "")
    if agent:
        _validate_identifier(agent, "action_agent")
    if project:
        _validate_identifier(project, "action_project")
    if playbook:
        _validate_identifier(playbook, "action_playbook")
        if kind == "play" and playbook in _PLAY_SUBCOMMANDS:
            raise ValueError(
                f"action_playbook {playbook!r} names a `li play` subcommand, not a playbook"
            )
    if isinstance(extra, list):
        _validate_extra_args(extra)
    if kind == "play":
        _validate_playbook_args(_playbook_args(schedule))
        # `li play` has no '--' separator, so a sent prompt that starts with '-' re-parses as a
        # flag. A stored one is refused here: a manual trigger reads this before a run is
        # recorded, and a scheduled or chained fire records the refusal as its run's failure.
        # One that only its rendering makes start with '-' is refused in build_argv.
        if _sends_prompt(schedule, kind) and str(schedule.get("action_prompt") or "").startswith(
            "-"
        ):
            raise ValueError("action_prompt for a play launch must not start with '-'")
        # With no playbook, the first extra argument is the first word after `li play`.
        if (
            not playbook
            and isinstance(extra, list)
            and extra
            and str(extra[0]) in _PLAY_SUBCOMMANDS
        ):
            raise ValueError(
                f"action_extra_args starts with {str(extra[0])!r}, which a play launch with no "
                "playbook passes to `li play` as its subcommand"
            )
    if kind == "engine":
        _validate_engine_options(schedule.get("action_engine_options"))

    if kind == "agent" and not schedule.get("action_model") and isinstance(extra, list) and extra:
        # Omitting the model positional when unset lets `li agent` fall through to the profile
        # default. But with model omitted, a single extra-args token brings arity back to
        # [model, prompt] and would silently misroute the prompt as MODEL.
        raise ValueError(
            "action_extra_args is not supported together with an empty "
            "action_model for kind='agent': omitting the model "
            "positional makes the extra-args token(s) indistinguishable "
            "from an explicit [model, prompt] pair, which would silently "
            "misroute action_prompt as the model. Set action_model "
            "explicitly, or clear action_extra_args."
        )
    elif kind == "flow_yaml" and isinstance(extra, list) and extra:
        # No positionals here (spec file carries prompt/model), so extra tokens would soak into
        # flow's optional slots.
        raise ValueError(
            "flow_yaml launches do not accept action_extra_args; "
            "set model/prompt inside the flow spec instead"
        )
    elif kind == "engine":
        # action_agent = engine kind (e.g. "research"); action_prompt = spec.
        if not agent:
            raise ValueError("engine launches require action_agent (the engine kind)")
        if not schedule.get("action_prompt"):
            raise ValueError("engine launches require action_prompt (the engine spec)")
        # The engine CLI exits nonzero for 'coding' without --test-cmd (and a blank command
        # splits into an empty argv downstream); fail here so no invocation row is created for a
        # launch that cannot run.
        engine_opts = schedule.get("action_engine_options") or {}
        if agent == "coding" and not str(engine_opts.get("test_cmd") or "").strip():
            raise ValueError(
                "the 'coding' engine kind requires a non-blank action_engine_options.test_cmd"
            )
    elif kind == "command":
        command = schedule.get("action_command") or ""
        _validate_action_command(command)
        _validate_command_allowlisted(command)
        if isinstance(extra, list) and extra:
            raise ValueError(
                "command launches do not accept action_extra_args; set "
                "arguments via action_command_args instead"
            )
        if not isinstance(schedule.get("action_command_args") or [], list):
            raise ValueError("action_command_args must be a list of strings")
    return kind
