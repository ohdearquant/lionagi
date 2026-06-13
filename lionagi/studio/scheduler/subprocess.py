# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0027 subprocess spawning for scheduled runs."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import signal
import tempfile

_log = logging.getLogger(__name__)

_TEMPLATE_RE = re.compile(r"\{\{(\w+)\}\}")

# ADR-0027 defines the closed set of action kinds.  The CLI parser accepts
# "playbook" as an alias for "play" for backward compatibility.
_VALID_ACTION_KINDS = frozenset({"agent", "flow", "fanout", "play", "flow_yaml", "engine"})
_ALIAS_ACTION_KINDS: dict[str, str] = {"playbook": "play"}

# action_model must be a safe model-spec token: alphanumerics, dots, slashes,
# colons, hyphens and underscores only.  Values starting with '-' are rejected
# unconditionally to block flag injection into the spawned li process (CWE-88).
_MODEL_RE = re.compile(r"^[a-zA-Z0-9_./:@-]+$")

# Identifier fields (action_agent, action_project, action_playbook) share the
# same character set as model specs: they name agents, projects, and playbooks
# and have no legitimate use for leading '-'.
_IDENT_RE = _MODEL_RE


def _validate_action_model(model: str) -> None:
    """Raise ValueError if *model* could inject CLI flags into the subprocess.

    A model value starting with '-' would be interpreted as a flag by the spawned
    ``li`` process.  Values containing characters outside the safe set are also
    rejected because they have no legitimate use in a model spec.

    Policy: reject loudly rather than silently filtering so callers discover bad
    data at write time rather than at fire time.
    """
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


def _validate_identifier(value: str, field_name: str) -> None:
    """Raise ValueError if *value* (an identifier field) starts with '-'.

    Covers action_agent, action_project, and action_playbook.  These fields are
    identifier-shaped (name a profile, project, or playbook) and must not start
    with '-'.  A leading '-' would cause argparse to misinterpret the value as a
    flag, producing an unexpected usage error rather than a flag toggle — still
    fragile behaviour that callers should never be able to trigger.

    Policy: loud rejection at write time (same as action_model).
    """
    if not value:
        return
    if value.startswith("-"):
        raise ValueError(
            f"{field_name} {value!r} starts with '-' and is not a valid identifier. "
            "Identifier fields must not begin with '-'."
        )
    if not _IDENT_RE.match(value):
        raise ValueError(
            f"{field_name} {value!r} contains characters not allowed in an identifier. "
            "Allowed: letters, digits, '_', '.', '/', ':', '@', '-'."
        )


def _validate_extra_args(extra: list) -> None:
    """Raise ValueError if any element of *extra* starts with '-'.

    Elements starting with '-' are CLI flags and would be injected verbatim into
    the argv of the spawned li process (CWE-88).  Positional tokens that do not
    start with '-' are accepted.

    Policy: reject loudly with the offending element named so callers can fix the
    schedule spec rather than silently receiving a process that behaves differently
    from what was intended.

    Note: extra tokens are appended after the subcommand's own positionals.
    Parsers for agent/flow/fanout do not accept extra positionals, so non-empty
    action_extra_args will cause those subcommands to exit rc 2 at fire time.
    action_extra_args is only meaningful for future subcommand extensions or the
    play kind; restrict its use accordingly.
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

# Engine option string values land in argv as flag values.  Reject tokens that
# argparse could mistake for option strings (leading '-') and anything outside
# a conservative character set; numeric options must be bounded integers.
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


def _validate_prompt(prompt: str) -> None:
    """Raise ValueError if *prompt* is the literal end-of-options sentinel '--'.

    The '--' sentinel is special to argparse: when placed as the first positional
    after our own '--' sentinel, argparse consumes it as the separator token and
    the actual prompt value reaches the runner as an empty string (or causes a
    'required' error), rather than arriving as the prompt text.

    Freeform prompts are otherwise unrestricted — any other content including
    leading '-' characters (e.g. '--bypass', '--verbose') is safe because the
    structural fix in build_argv places a '--' before all positionals.  The sole
    forbidden value is the exact two-character token '--'.

    Prompt values like '-- --', '-- text', or '--' embedded in longer strings
    are all permitted; only the exact singleton '--' is rejected.
    """
    if prompt == "--":
        raise ValueError(
            "action_prompt value '--' is not allowed: the literal end-of-options "
            "token would be silently consumed by argparse rather than reaching the "
            "runner as prompt text. Use any other prompt content."
        )


def _render_template(template: str, context: dict) -> str:
    """Replace {{var}} placeholders with values from trigger context."""

    def _replace(m: re.Match) -> str:
        key = m.group(1)
        # Look in github events first
        events = context.get("github_events", [])
        if events and isinstance(events, list) and isinstance(events[0], dict):
            val = events[0].get(key)
            if val is not None:
                return str(val)
        return context.get(key, m.group(0))

    return _TEMPLATE_RE.sub(_replace, template)


def build_argv(
    schedule: dict,
    trigger_context: dict,
    *,
    invocation_id: str | None = None,
) -> tuple[list[str], str | None]:
    """Build the subprocess argv for a scheduled action.

    Returns ``(argv, tmp_path)`` where ``tmp_path`` is a temporary file that
    must be deleted after the subprocess exits (only set for ``flow_yaml``).

    Pass *invocation_id* to forward ``--invocation <id>`` to CLI subcommands
    that accept it (``agent``, ``o flow``, ``o fanout``). This attributes the
    spawned session to the scheduler's invocation row in StateDB.
    """
    kind = schedule["action_kind"]
    # Normalize legacy alias and validate against the closed set.
    kind = _ALIAS_ACTION_KINDS.get(kind, kind)
    if kind not in _VALID_ACTION_KINDS:
        raise ValueError(
            f"Unknown action_kind {kind!r}. Valid kinds: {sorted(_VALID_ACTION_KINDS)}"
        )
    model = schedule.get("action_model") or ""
    prompt = schedule.get("action_prompt") or ""
    agent = schedule.get("action_agent")
    playbook = schedule.get("action_playbook")
    project = schedule.get("action_project")
    extra = schedule.get("action_extra_args") or []

    # Defensive validation: reject flag-injection vectors before touching argv.
    # These checks mirror the service-layer boundary in services/schedules.py;
    # having them here ensures the subprocess is never spawned with injected flags
    # regardless of how the schedule dict was created.
    #
    # IMPORTANT — order of operations for action_prompt:
    # action_prompt may contain {{var}} template placeholders that are expanded
    # from trigger_context at fire time.  A stored prompt like '{{payload}}' passes
    # pre-render validation but could render into the forbidden '--' sentinel when
    # a trigger context supplies {"payload": "--"}.  To close this window we
    # validate action_prompt AFTER rendering, not before.
    #
    # action_model, action_extra_args, action_agent, action_project, and
    # action_playbook are NOT passed through _render_template — they are used
    # verbatim from the schedule dict, so their validation before the render
    # step is correct and sufficient.
    _validate_action_model(model)
    if agent:
        _validate_identifier(agent, "action_agent")
    if project:
        _validate_identifier(project, "action_project")
    if playbook:
        _validate_identifier(playbook, "action_playbook")
    if isinstance(extra, list):
        _validate_extra_args(extra)
    if kind == "engine":
        _validate_engine_options(schedule.get("action_engine_options"))

    # Render template variables from trigger context FIRST, then validate the
    # rendered prompt so that template-injected values (e.g. '{{payload}}' →
    # '--') are caught before argv construction.
    if prompt:
        prompt = _render_template(prompt, trigger_context)
    if prompt:
        _validate_prompt(prompt)

    argv = ["uv", "run", "li"]
    tmp_path: str | None = None

    # argv structure (CWE-88 hardening):
    #
    #   Named flags (--agent, --project, -f …) FIRST, then the '--' end-of-options
    #   sentinel, then positional arguments (model, prompt).
    #
    # The '--' sentinel tells argparse to stop treating subsequent tokens as
    # option strings, so a prompt like '--bypass' is parsed as the prompt VALUE
    # rather than toggling the bypass flag — making action_prompt injection-proof
    # without restricting freeform prompt text at all.
    #
    # flow_yaml is a special case: the YAML file supplies the prompt, so the
    # prompt positional is OMITTED entirely.  Empirically verified: the CLI
    # parser reads the prompt from the -f spec file (prompt: key) and overwrites
    # args.prompt, so a positional prompt is redundant and would only open a
    # second injection surface.  Shape: li o flow -f <tmp> -- <model>

    if kind == "agent":
        # Named flags first (--agent must come before --)
        flags: list[str] = []
        if agent:
            flags += ["--agent", agent]
        if project:
            flags += ["--project", project]
        if invocation_id:
            flags += ["--invocation", invocation_id]
        argv += ["agent", *flags, "--", model, prompt]

    elif kind == "flow":
        flags = []
        if project:
            flags += ["--project", project]
        if invocation_id:
            flags += ["--invocation", invocation_id]
        argv += ["o", "flow", *flags, "--", model, prompt]

    elif kind == "fanout":
        flags = []
        if project:
            flags += ["--project", project]
        if invocation_id:
            flags += ["--invocation", invocation_id]
        argv += ["o", "fanout", *flags, "--", model, prompt]

    elif kind == "play":
        # `li play NAME` is a positional-only subcommand; '--' is not needed
        # because playbook names are validated as identifiers above and there
        # is no freeform prompt positional.
        argv += ["play"]
        if playbook:
            argv.append(playbook)

    elif kind == "flow_yaml":
        # Write the inline YAML spec to a temp file so `li o flow -f <path>`
        # can read it.  The caller is responsible for deleting tmp_path after
        # the subprocess exits.
        yaml_text = schedule.get("action_flow_yaml") or ""
        fd, tmp_path = tempfile.mkstemp(suffix=".yaml", prefix="lionagi-sched-")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(yaml_text)
        except Exception:
            os.unlink(tmp_path)
            raise
        # Named flags first (-f must come before --), then -- sentinel, then
        # model positional only (no prompt positional — YAML supplies it).
        flags = ["-f", tmp_path]
        if project:
            flags += ["--project", project]
        if invocation_id:
            flags += ["--invocation", invocation_id]
        argv += ["o", "flow", *flags, "--", model]

    elif kind == "engine":
        # engine def launch: argv shape is
        #   uv run li engine run [named flags] -- <engine_kind> <spec>
        # action_agent carries the engine kind (e.g. "research"); action_prompt
        # carries the engine spec.  Named flags go first (CWE-88 hardening).
        if not agent:
            raise ValueError("engine launches require action_agent (the engine kind)")
        if not prompt:
            raise ValueError("engine launches require action_prompt (the engine spec)")
        engine_kind = agent
        flags = []
        if model:
            flags += ["--model", model]
        engine_opts = schedule.get("action_engine_options") or {}
        # The engine CLI exits nonzero for 'coding' without --test-cmd (and a
        # blank command splits into an empty argv downstream); fail here so no
        # invocation row is created for a launch that cannot run.
        if engine_kind == "coding" and not str(engine_opts.get("test_cmd") or "").strip():
            raise ValueError(
                "the 'coding' engine kind requires a non-blank action_engine_options.test_cmd"
            )
        max_depth = engine_opts.get("max_depth")
        max_agents = engine_opts.get("max_agents")
        test_cmd = engine_opts.get("test_cmd")
        export_dir = engine_opts.get("export_dir")
        if max_depth is not None:
            flags += ["--max-depth", str(int(max_depth))]
        if max_agents is not None:
            flags += ["--max-agents", str(int(max_agents))]
        if test_cmd:
            flags += ["--test-cmd", test_cmd]
        if export_dir:
            flags += ["--export-dir", export_dir]
        argv += ["engine", "run", *flags, "--", engine_kind, prompt]

    # extra has already been validated above; extend argv with safe positional tokens.
    # These are appended AFTER the subcommand's positionals, after '--', so they
    # are treated as positional values by the subcommand's parser.
    if kind != "engine" and isinstance(extra, list):
        argv.extend(str(a) for a in extra)

    return argv, tmp_path


async def spawn_and_wait(
    argv: list[str],
    invocation_id: str,
    *,
    tmp_path: str | None = None,
) -> tuple[int, str]:
    """Spawn subprocess and wait for completion. Returns (exit_code, stderr_tail).

    If *tmp_path* is given it is deleted after the subprocess exits — used by
    the ``flow_yaml`` action kind which writes a temp spec file before spawning.
    """
    env = {**os.environ, "LIONAGI_INVOCATION_ID": invocation_id}

    _log.info("Spawning: %s", " ".join(argv))
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        # `uv run li` forks the real worker (and that worker may fork
        # further). Put the whole tree in its own session/process group so a
        # cancel can signal the GROUP, not just the direct child — otherwise
        # grandchildren survive scheduler shutdown as orphans.
        start_new_session=True,
    )
    # Capture the pgid NOW — once the child exits and is reaped,
    # os.getpgid(proc.pid) raises ProcessLookupError and we'd skip the group
    # kill. start_new_session=True makes pgid == proc.pid. Guard mocked pids
    # in tests: a MagicMock.pid coerces to 1, and killpg(1, …) hits init.
    # os.killpg is POSIX-only: on Windows leave _pgid None so the group-kill
    # path is skipped and cleanup falls through to proc.terminate()/kill()
    # instead of raising AttributeError.
    _pgid: int | None = (
        proc.pid if hasattr(os, "killpg") and isinstance(proc.pid, int) and proc.pid > 1 else None
    )

    try:
        _, stderr = await proc.communicate()
    except asyncio.CancelledError:
        # Cancellation (e.g. scheduler shutdown) must not leave the spawned
        # `uv run li` tree detached. SIGTERM the whole group, give it a moment
        # to exit, then SIGKILL the group, before re-raising so the caller can
        # record the cancel.
        _log.warning("spawn_and_wait cancelled; terminating child for %s", invocation_id)
        if _pgid is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(_pgid, signal.SIGTERM)
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except (TimeoutError, asyncio.TimeoutError):
            if _pgid is not None:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(_pgid, signal.SIGKILL)
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
        raise
    finally:
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)

    exit_code = proc.returncode or 0
    stderr_tail = (stderr[-2048:] if stderr else b"").decode(errors="replace")

    _log.info("Process exited with code %d", exit_code)
    return exit_code, stderr_tail
