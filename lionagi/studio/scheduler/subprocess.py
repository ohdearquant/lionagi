# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0070 subprocess spawning for scheduled runs."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Awaitable, Callable
from importlib import metadata as importlib_metadata
from pathlib import Path

from lionagi.ln._proc import aterminate_process_group

_log = logging.getLogger(__name__)

_TEMPLATE_RE = re.compile(r"\{\{(\w+)\}\}")

# Default argv prefix for launching the CLI: relies on `uv run` resolving the
# project/venv from the daemon's own cwd, which breaks when the daemon starts
# from a directory with no discoverable pyproject.toml (e.g. "/"). Callers
# should prefer the absolute prefix from resolve_li_executable() instead.
_DEFAULT_LI_PREFIX: tuple[str, ...] = ("uv", "run", "li")

# ADR-0070 defines the closed set of action kinds.  The CLI parser accepts
# "playbook" as an alias for "play" for backward compatibility.
# The "command" kind is an allow-listed executable spawned directly
# (not through `li`), with templated argv rendered from trigger_context.
_VALID_ACTION_KINDS = frozenset(
    {"agent", "flow", "fanout", "play", "flow_yaml", "engine", "command"}
)
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


def _validate_identifier(value: str, field_name: str) -> None:
    """Raise ValueError if *value* starts with '-' — argparse would misinterpret it as a flag (CWE-88)."""
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


# action_command must be a bare, PATH-resolvable executable name: no path
# separators (it is looked up via PATH at spawn time, never treated as a
# filesystem path), and the same conservative identifier charset as the
# other single-token fields.
_COMMAND_RE = re.compile(r"^[a-zA-Z0-9_.-]+$")

# The environment variable gating which command names the "command" action
# kind is permitted to spawn. Comma-separated; unset or empty
# resolves to an empty allow-list, refusing every command -- a generic
# command runner without this gate would be an arbitrary-execution footgun.
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

    Called both at schedule build/validation time (services/schedules.py)
    and again here inside ``build_argv`` at actual spawn time, since the
    environment variable can change between schedule creation and fire.
    """
    allowlist = _command_allowlist()
    if command not in allowlist:
        raise ValueError(
            f"action_command {command!r} is not in {_COMMAND_ALLOWLIST_ENV} "
            f"(currently: {sorted(allowlist)!r}). Add it to the allow-list "
            "environment variable to permit this command."
        )


def _render_command_arg(template: str, context: dict) -> str:
    """Render one ``action_command_args`` element, validating the substituted
    portion for CWE-88 flag injection.

    A hand-authored literal token (e.g. ``"--repo"``, a legitimate flag for
    the target command -- the whole point of a generic command runner) is
    author-controlled, not attacker-influenceable, and passes through
    unchanged. A token containing a ``{{var}}`` placeholder pulls its value
    from ``trigger_context`` (e.g. a PR title or author), which an attacker
    may influence; the *rendered* result is checked so trigger-context
    content cannot masquerade as a new flag to the spawned command's own
    argument parser (mirrors the leading-'-' rejection already applied to
    ``action_extra_args``, plus the restricted charset already applied to
    ``action_engine_options`` string values).
    """
    if not _TEMPLATE_RE.search(template):
        return template
    rendered = _render_template(template, context)
    if rendered.startswith("-"):
        raise ValueError(
            f"action_command_args template {template!r} rendered to "
            f"{rendered!r}, which starts with '-' and would inject a flag "
            "into the spawned command."
        )
    if not _ENGINE_OPT_VALUE_RE.match(rendered):
        raise ValueError(
            f"action_command_args template {template!r} rendered to "
            f"{rendered!r}, which contains characters not allowed. Allowed "
            f"charset: {_ENGINE_OPT_VALUE_RE.pattern}"
        )
    return rendered


def _validate_prompt(prompt: str) -> None:
    """Raise ValueError if *prompt* is exactly the end-of-options sentinel '--'.

    build_argv places '--' before positionals, so a prompt value of exactly
    '--' would be consumed by argparse as the separator, never reaching the
    runner. All other content (including leading '-') is safe.
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
        events = context.get("github_events", [])  # check github events first
        if events and isinstance(events, list) and isinstance(events[0], dict):
            val = events[0].get(key)
            if val is not None:
                return str(val)
        # Stringify non-string values too (e.g. numeric threshold-alert
        # fields) -- re.sub's replacement callback requires a str return.
        if key in context:
            return str(context[key])
        return m.group(0)

    return _TEMPLATE_RE.sub(_replace, template)


def render_action_prompt(schedule: dict, trigger_context: dict) -> str | None:
    """Render a schedule's ``action_prompt`` with the trigger context.

    Returns ``None`` when the schedule has no prompt template, so callers can
    fall back to another field (e.g. ``action_playbook``) without confusing
    "no prompt" with "empty rendered prompt".
    """
    prompt = schedule.get("action_prompt") or ""
    if not prompt:
        return None
    return _render_template(prompt, trigger_context)


def resolve_li_executable() -> tuple[list[str] | None, str | None]:
    """Resolve an absolute argv prefix for launching the ``li`` CLI.

    ``uv run li`` depends on ``uv`` discovering a project/venv from the
    daemon's cwd, which fails with an opaque ENOENT when the daemon starts
    somewhere with no discoverable ``pyproject.toml``. An absolute path here
    sidesteps that cwd-dependent lookup entirely.

    Tries, in order:
      1. ``shutil.which("li")`` — the `li` script on PATH.
      2. A ``li`` file next to ``sys.executable`` (the venv running this
         daemon almost certainly installed `li` into the same bin dir).
      3. The ``li`` console-script entry point's target module, invoked via
         ``[sys.executable, "-m", <module>]`` — works even when no `li`
         script file exists on disk.

    Returns ``(argv_prefix, None)`` on success, or ``(None, detail)`` where
    *detail* names every strategy that was tried and why each failed.
    """
    tried: list[str] = []

    which_path = shutil.which("li")
    if which_path and os.path.isabs(which_path):
        return [which_path], None
    if which_path:
        # A relative PATH entry (e.g. "." or "relbin") would resolve the
        # child's argv[0] against the spawn-time cwd, reintroducing
        # cwd-dependent spawn / PATH-hijack risk. Reject and fall through.
        tried.append(
            f"shutil.which('li') found a non-absolute path ({which_path}); "
            "rejected to avoid cwd-dependent spawn/PATH-hijack"
        )
    else:
        tried.append("shutil.which('li') found nothing on PATH")

    # Both remaining strategies invoke sys.executable directly, so both
    # require it to be absolute (same cwd-dependent/PATH-hijack risk as tier 1).
    python_path = Path(sys.executable) if sys.executable else None
    python_is_absolute = python_path is not None and python_path.is_absolute()

    if python_is_absolute:
        venv_li = python_path.with_name("li")
        if venv_li.is_file() and os.access(venv_li, os.X_OK):
            return [str(venv_li)], None
        tried.append(f"no executable `li` file next to sys.executable ({venv_li})")
    else:
        tried.append(f"sys.executable is not an absolute path ({sys.executable!r})")

    if python_is_absolute:
        try:
            entry_points = importlib_metadata.entry_points(group="console_scripts")
        except Exception as exc:  # pragma: no cover - defensive, metadata API is stable
            entry_points = []
            tried.append(f"importlib.metadata.entry_points() raised {type(exc).__name__}: {exc}")
        for ep in entry_points:
            if ep.name == "li":
                module = ep.value.split(":", 1)[0]
                return [str(python_path), "-m", module], None
        tried.append("no 'li' console_scripts entry point registered")
    else:
        tried.append(
            "skipping console_scripts entry-point fallback: sys.executable is "
            "not absolute, so `python -m <module>` would leak the same "
            "relative prefix"
        )

    return None, "; ".join(tried)


def build_argv(
    schedule: dict,
    trigger_context: dict,
    *,
    executable_prefix: list[str] | None = None,
) -> tuple[list[str], str | None]:
    """Build the subprocess argv for a scheduled action.

    *executable_prefix* replaces the default ``["uv", "run", "li"]`` prefix
    (e.g. with the absolute path from ``resolve_li_executable()``) so the
    child process spawns independent of the daemon's own cwd/PATH. Omitting
    it preserves the pre-existing ``uv run li`` behavior.

    Returns ``(argv, tmp_path)`` where ``tmp_path`` is a temporary file that
    must be deleted after the subprocess exits (only set for ``flow_yaml``).
    """
    kind = schedule["action_kind"]
    kind = _ALIAS_ACTION_KINDS.get(kind, kind)  # normalize legacy alias
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

    # Reject flag-injection vectors before touching argv (defense-in-depth,
    # mirrors services/schedules.py). action_prompt is validated AFTER
    # rendering below since a templated '{{payload}}' can render to the
    # forbidden '--' sentinel; other fields aren't templated.
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

    argv = list(executable_prefix) if executable_prefix is not None else list(_DEFAULT_LI_PREFIX)
    tmp_path: str | None = None

    # CWE-88 hardening: named flags first, then the '--' end-of-options
    # sentinel, then positionals (model, prompt) — makes action_prompt
    # injection-proof (e.g. '--bypass' parses as prompt text, not a flag).
    # flow_yaml omits the prompt positional (CLI reads it from -f instead)
    # to avoid a second injection surface.

    if kind == "agent":
        flags: list[str] = []
        if agent:
            flags += ["--agent", agent]
        if project:
            flags += ["--project", project]
        # Omit the model positional when unset -- `li agent` then falls
        # through to the profile's default model instead of an explicit
        # blank spec that would crash Branch init. But with model omitted, a
        # single extra-args token brings positional arity back to 2 (matching
        # [model, prompt]), which would silently misroute the real prompt as
        # MODEL -- reject that combination instead of corrupting positions.
        if not model and isinstance(extra, list) and extra:
            raise ValueError(
                "action_extra_args is not supported together with an empty "
                "action_model for kind='agent': omitting the model "
                "positional makes the extra-args token(s) indistinguishable "
                "from an explicit [model, prompt] pair, which would silently "
                "misroute action_prompt as the model. Set action_model "
                "explicitly, or clear action_extra_args."
            )
        positionals = [model, prompt] if model else [prompt]
        argv += ["agent", *flags, "--", *positionals]

    elif kind == "flow":
        flags = []
        if project:
            flags += ["--project", project]
        argv += ["o", "flow", *flags, "--", model, prompt]

    elif kind == "fanout":
        flags = []
        if project:
            flags += ["--project", project]
        argv += ["o", "fanout", *flags, "--", model, prompt]

    elif kind == "play":
        # `li play NAME` is a positional-only subcommand; '--' is not needed
        # because playbook names are validated as identifiers above and there
        # is no freeform prompt positional.
        argv += ["play"]
        if playbook:
            argv.append(playbook)

    elif kind == "flow_yaml":
        # No positionals at all here (spec file carries prompt/model), so
        # extra tokens would soak into flow's optional model/prompt slots
        # and silently override the merged model below; fail at build time.
        if isinstance(extra, list) and extra:
            raise ValueError(
                "flow_yaml launches do not accept action_extra_args; "
                "set model/prompt inside the flow spec instead"
            )
        # Write the inline YAML spec to a temp file for `li o flow -f <path>`.
        # Caller deletes tmp_path after the subprocess exits.
        yaml_text = schedule.get("action_flow_yaml") or ""
        if model:
            # Merge action_model into the spec rather than pass it as a
            # positional: when the file has its own model/agent, `li o flow
            # -f` reclassifies a lone model positional as prompt text, so a
            # positional can't reliably override it. Unparseable/non-mapping
            # specs are written unchanged -- `li o flow` reports its own error.
            import yaml

            try:
                spec = yaml.safe_load(yaml_text)
            except Exception:
                spec = None
            if isinstance(spec, dict):
                spec["model"] = model
                yaml_text = yaml.safe_dump(spec, sort_keys=False, allow_unicode=True)
        fd, tmp_path = tempfile.mkstemp(suffix=".yaml", prefix="lionagi-sched-")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(yaml_text)
        except Exception:
            os.unlink(tmp_path)
            raise
        # No positionals: the YAML file supplies the prompt; model was merged above.
        flags = ["-f", tmp_path]
        if project:
            flags += ["--project", project]
        argv += ["o", "flow", *flags, "--"]

    elif kind == "engine":
        # argv shape: uv run li engine run [named flags] -- <engine_kind> <spec>
        # action_agent = engine kind (e.g. "research"); action_prompt = spec.
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

    elif kind == "command":
        # This kind spawns the allow-listed executable DIRECTLY -- it never
        # goes through `li`, so the executable_prefix/uv-run-li argv built
        # above is discarded entirely rather than extended.
        command = schedule.get("action_command") or ""
        _validate_action_command(command)
        _validate_command_allowlisted(command)
        if isinstance(extra, list) and extra:
            raise ValueError(
                "command launches do not accept action_extra_args; set "
                "arguments via action_command_args instead"
            )
        command_args = schedule.get("action_command_args") or []
        if not isinstance(command_args, list):
            raise ValueError("action_command_args must be a list of strings")
        rendered_args = [_render_command_arg(str(a), trigger_context) for a in command_args]
        argv = [command, *rendered_args]

    # Append validated extra positionals (safe, no leading '-').
    if kind not in ("engine", "command") and isinstance(extra, list):
        argv.extend(str(a) for a in extra)

    return argv, tmp_path


async def spawn_and_wait(
    argv: list[str],
    invocation_id: str,
    *,
    tmp_path: str | None = None,
    cwd: str | None = None,
    action_kind: str | None = None,
    on_launched: Callable[[], Awaitable[None]] | None = None,
) -> tuple[int, str]:
    """Spawn subprocess and wait for completion. Returns (exit_code, stderr_tail).

    If *tmp_path* is given it is deleted after the subprocess exits — used by
    the ``flow_yaml`` action kind which writes a temp spec file before spawning.

    *cwd* pins the subprocess working directory. ``None`` inherits the
    caller's own cwd (the daemon's launch directory) — callers should resolve
    a concrete path (e.g. from ``action_project``) before spawning so `uv run
    li` doesn't fail with "No such file or directory" when the daemon was
    started somewhere with no project (see SchedulerEngine._resolve_action_cwd).

    *action_kind* re-runs the command allow-list check right here, immediately
    before the process is spawned, when it is ``"command"``. ``build_argv``
    already re-checks the allow-list at argv-construction time, but callers
    (the scheduler engine, the worker, on-demand launches) perform awaited DB
    work between building argv and calling this function -- an await is a
    scheduling point, so revoking the allow-list env var during that window
    does not stop a spawn checked only at build_argv time. Passing
    *action_kind* here closes that gap: the check runs with no intervening
    await before ``create_subprocess_exec``.

    *on_launched*, if given, is awaited immediately after
    ``create_subprocess_exec`` returns -- i.e. once the OS process genuinely
    exists -- and before waiting on its completion. The scheduler engine uses
    this to durably mark a schedule_run "dispatched" the moment launch is
    confirmed, closing the window a delivery-contract recovery scan would
    otherwise treat as "committed but never launched" (see
    SchedulerEngine._fire_inner). A failing callback is logged and swallowed,
    never allowed to fail (or duplicate-spawn) the process that already
    launched.
    """
    if action_kind == "command":
        command = argv[0] if argv else ""
        _validate_action_command(command)
        _validate_command_allowlisted(command)

    env = {**os.environ, "LIONAGI_INVOCATION_ID": invocation_id}

    _log.info("Spawning: %s", " ".join(argv))
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=cwd,
        # `uv run li` may fork further; own session/process group lets a
        # cancel signal the whole GROUP, not just the direct child --
        # otherwise grandchildren survive scheduler shutdown as orphans.
        start_new_session=True,
    )
    # Pgid == proc.pid; pid-guard and platform check live in aterminate_process_group.

    if on_launched is not None:
        try:
            await on_launched()
        except Exception:
            _log.exception(
                "on_launched callback failed for invocation %s; the process "
                "is already running regardless",
                invocation_id,
            )

    try:
        _, stderr = await proc.communicate()
    except asyncio.CancelledError:
        # SIGTERM then SIGKILL the whole group before re-raising, so a
        # cancelled poll doesn't leave the spawned tree detached.
        _log.warning("spawn_and_wait cancelled; terminating child for %s", invocation_id)
        await aterminate_process_group(proc, grace=5.0)
        raise
    finally:
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)

    exit_code = proc.returncode or 0
    stderr_tail = (stderr[-2048:] if stderr else b"").decode(errors="replace")

    _log.info("Process exited with code %d", exit_code)
    return exit_code, stderr_tail
