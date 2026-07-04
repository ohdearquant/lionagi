# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0027 subprocess spawning for scheduled runs."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import sys
import tempfile
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
    """Raise ValueError if *model* could inject CLI flags (CWE-88).

    A value starting with '-' is interpreted as a flag by the spawned li process.
    Fail loudly so callers discover bad data at write time, not fire time.
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
    """Raise ValueError if *value* starts with '-' (covers action_agent/project/playbook).

    A leading '-' causes argparse to misinterpret the value as a flag (CWE-88).
    Fail loudly at write time, same policy as _validate_action_model.
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
    """Raise ValueError if any element starts with '-' (CWE-88 flag injection).

    Extra tokens are appended verbatim to argv; a leading '-' would toggle a flag
    in the spawned li process. Fail loudly with the offending element named.

    Note: agent/flow/fanout parsers do not accept extra positionals; non-empty
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


def _validate_prompt(prompt: str) -> None:
    """Raise ValueError if *prompt* is the literal end-of-options sentinel '--'.

    build_argv places '--' before positionals, so a prompt value of '--' would be
    consumed by argparse as the separator token rather than reaching the runner.
    All other content (including leading '-') is safe. Only the exact singleton
    '--' is forbidden; '-- text' or '--' embedded in a longer string are fine.
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
    *current working directory*; a daemon started from a directory with no
    discoverable ``pyproject.toml`` (e.g. the filesystem root) fails to spawn
    with an opaque ENOENT for ``li``, unrelated to the schedule's own working
    directory. Resolving an absolute executable path here sidesteps that
    cwd-dependent lookup entirely.

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
        # A relative PATH entry (e.g. "." or "relbin") makes shutil.which
        # return a relative hit. Accepting it as-is would make the child's
        # argv[0] resolve against whatever cwd the spawn ends up using
        # (not the daemon environment that found it) — reintroducing
        # cwd-dependent spawn behavior and a PATH-hijack surface. Reject
        # and fall through to the next strategy instead.
        tried.append(
            f"shutil.which('li') found a non-absolute path ({which_path}); "
            "rejected to avoid cwd-dependent spawn/PATH-hijack"
        )
    else:
        tried.append("shutil.which('li') found nothing on PATH")

    python_path = Path(sys.executable) if sys.executable else None
    if python_path is not None and python_path.is_absolute():
        venv_li = python_path.with_name("li")
        if venv_li.is_file() and os.access(venv_li, os.X_OK):
            return [str(venv_li)], None
        tried.append(f"no executable `li` file next to sys.executable ({venv_li})")
    else:
        tried.append(f"sys.executable is not an absolute path ({sys.executable!r})")

    try:
        entry_points = importlib_metadata.entry_points(group="console_scripts")
    except Exception as exc:  # pragma: no cover - defensive, metadata API is stable
        entry_points = []
        tried.append(f"importlib.metadata.entry_points() raised {type(exc).__name__}: {exc}")
    for ep in entry_points:
        if ep.name == "li":
            module = ep.value.split(":", 1)[0]
            return [sys.executable, "-m", module], None
    tried.append("no 'li' console_scripts entry point registered")

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

    # Reject flag-injection vectors before touching argv (mirrors service-layer
    # boundary in services/schedules.py for defense-in-depth).
    #
    # action_prompt is validated AFTER rendering: a stored '{{payload}}' passes
    # pre-render checks but could render to the forbidden '--' sentinel when the
    # trigger context supplies {"payload": "--"}.  Other fields are NOT templated,
    # so validating them before rendering is correct and sufficient.
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

    # CWE-88 hardening: named flags first, then '--' end-of-options sentinel,
    # then positionals (model, prompt).  The sentinel makes action_prompt
    # injection-proof: '--bypass' is parsed as the prompt value, not a flag.
    #
    # flow_yaml omits the prompt positional entirely — the CLI reads prompt from
    # the -f spec file and overwrites args.prompt, so a positional would only
    # open a second injection surface.  Shape: li o flow -f <tmp> -- <model>

    if kind == "agent":
        # Named flags first (--agent must come before --)
        flags: list[str] = []
        if agent:
            flags += ["--agent", agent]
        if project:
            flags += ["--project", project]
        # Omit the model positional entirely when action_model is unset: `li
        # agent` treats a single positional as the prompt and falls through to
        # the --agent profile's own default model. Passing an empty string as
        # the model positional would instead be parsed as an explicit (blank)
        # model spec, overriding the profile default and crashing Branch init.
        #
        # But omitting the model positional only holds the "extra positionals
        # are rejected loudly" invariant when there are no extra args to
        # append: with model omitted, a single action_extra_args token brings
        # the positional count back up to 2 (the same arity as [model,
        # prompt]), so the CLI's resolver would silently reparse the real
        # prompt as MODEL and the extra-args token as PROMPT instead of
        # failing. Reject that combination here rather than let it corrupt
        # argument positions at fire time.
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
        # Extra positionals have nowhere safe to land here: this launch emits
        # no positionals at all (the spec file carries prompt and model), so
        # `li o flow` would soak extra tokens into its optional model/prompt
        # slots and silently override the model merged into the spec below.
        # Fail at build time so no invocation row is created for a launch
        # that cannot honor them.
        if isinstance(extra, list) and extra:
            raise ValueError(
                "flow_yaml launches do not accept action_extra_args; "
                "set model/prompt inside the flow spec instead"
            )
        # Write the inline YAML spec to a temp file so `li o flow -f <path>`
        # can read it.  The caller is responsible for deleting tmp_path after
        # the subprocess exits.
        yaml_text = schedule.get("action_flow_yaml") or ""
        if model:
            # An explicit action_model is merged into the spec itself, never
            # passed as a positional: when the file supplies its own model or
            # agent, `li o flow -f` reclassifies a lone model positional as
            # prompt text, so a positional cannot reliably override the file.
            # Merging into the spec wins deterministically and leaves no
            # positional surface at all. Unparseable or non-mapping specs are
            # written unchanged — `li o flow` rejects them with its own parse
            # error, which names the file.
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
        # Named flags first (-f must come before --), then the -- sentinel.
        # No positionals at all: the YAML file supplies the prompt, and any
        # explicit model was merged into the spec above.
        flags = ["-f", tmp_path]
        if project:
            flags += ["--project", project]
        argv += ["o", "flow", *flags, "--"]

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

    # Append validated extra positionals (safe, no leading '-').
    if kind != "engine" and isinstance(extra, list):
        argv.extend(str(a) for a in extra)

    return argv, tmp_path


async def spawn_and_wait(
    argv: list[str],
    invocation_id: str,
    *,
    tmp_path: str | None = None,
    cwd: str | None = None,
) -> tuple[int, str]:
    """Spawn subprocess and wait for completion. Returns (exit_code, stderr_tail).

    If *tmp_path* is given it is deleted after the subprocess exits — used by
    the ``flow_yaml`` action kind which writes a temp spec file before spawning.

    *cwd* pins the subprocess working directory. ``None`` inherits the
    caller's own cwd (the daemon's launch directory) — callers should resolve
    a concrete path (e.g. from ``action_project``) before spawning so `uv run
    li` doesn't fail with "No such file or directory" when the daemon was
    started somewhere with no project (see SchedulerEngine._resolve_action_cwd).
    """
    env = {**os.environ, "LIONAGI_INVOCATION_ID": invocation_id}

    _log.info("Spawning: %s", " ".join(argv))
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=cwd,
        # `uv run li` forks the real worker (and that worker may fork
        # further). Put the whole tree in its own session/process group so a
        # cancel can signal the GROUP, not just the direct child — otherwise
        # grandchildren survive scheduler shutdown as orphans.
        start_new_session=True,
    )
    # Pgid == proc.pid (start_new_session=True). The pid-guard and platform
    # check live in aterminate_process_group.

    try:
        _, stderr = await proc.communicate()
    except asyncio.CancelledError:
        # Cancellation (e.g. scheduler shutdown) must not leave the spawned
        # `uv run li` tree detached. SIGTERM the whole group, give it a moment
        # to exit, then SIGKILL the group, before re-raising so the caller can
        # record the cancel.
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
