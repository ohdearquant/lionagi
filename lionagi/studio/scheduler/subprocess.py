# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0070 subprocess spawning for scheduled runs."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Awaitable, Callable
from importlib import metadata as importlib_metadata
from pathlib import Path

from lionagi.ln import (
    aterminate_process_group,
    capture_group_witnesses,
    observe_process_group_leader,
    prepare_process_group_env,
)
from lionagi.ln._proc import env_without_state_db_url
from lionagi.studio.services.schedule_action_validation import (
    _COMMAND_ALLOWLIST_ENV,  # noqa: F401
    _COMMAND_RE,  # noqa: F401
    _ENGINE_OPT_VALUE_RE,
    _ENGINE_OPTION_KEYS,  # noqa: F401
    _PLAY_SUBCOMMANDS,  # noqa: F401
    _command_allowlist,  # noqa: F401
    _playbook_args,
    _reserved_flow_flags,  # noqa: F401
    _sends_prompt,
    _validate_action_command,
    _validate_action_model,  # noqa: F401
    _validate_command_allowlisted,
    _validate_engine_options,  # noqa: F401
    _validate_extra_args,  # noqa: F401
    _validate_playbook_args,  # noqa: F401
    _validate_prompt,
    validate_launch_fields,
)
from lionhub.orchestration.schedule.parsing import (
    _ALIAS_ACTION_KINDS,
    _MODEL_RE,  # noqa: F401
    _VALID_ACTION_KINDS,  # noqa: F401
    _validate_identifier,  # noqa: F401
)

_log = logging.getLogger(__name__)

_TEMPLATE_RE = re.compile(r"\{\{(\w+)\}\}")

# Default argv prefix for launching the CLI: relies on `uv run` resolving the
# project/venv from the daemon's own cwd, which breaks when the daemon starts
# from a directory with no discoverable pyproject.toml (e.g. "/"). Callers
# should prefer the absolute prefix from resolve_li_executable() instead.
_DEFAULT_LI_PREFIX: tuple[str, ...] = ("uv", "run", "li")

# Names `li play` reads as its own subcommands before any playbook name (cli/main.py
# _handle_play_shortcut): a play launch whose first word after `li play` is one of them, as its
# playbook or, with no playbook, as its first extra argument, runs that subcommand and no playbook.

# Engine option string values land in argv as flag values.  Reject tokens that
# argparse could mistake for option strings (leading '-') and anything outside
# a conservative character set; numeric options must be bounded integers.

# action_command must be a bare, PATH-resolvable executable name: no path
# separators (it is looked up via PATH at spawn time, never treated as a
# filesystem path), and the same conservative identifier charset as the
# other single-token fields.

# The environment variable gating which command names the "command" action
# kind is permitted to spawn. Comma-separated; unset or empty
# resolves to an empty allow-list, refusing every command -- a generic
# command runner without this gate would be an arbitrary-execution footgun.


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
            # The rendered value is left out: it can carry a trigger event's payload, and this
            # message is stored on the run and served with it.
            f"action_command_args template {template!r} rendered to a value that starts "
            "with '-' and would inject a flag into the spawned command."
        )
    if not _ENGINE_OPT_VALUE_RE.match(rendered):
        raise ValueError(
            f"action_command_args template {template!r} rendered to a value that "
            "contains characters not allowed. Allowed "
            f"charset: {_ENGINE_OPT_VALUE_RE.pattern}"
        )
    return rendered


def _template_value(key: str, context: dict) -> str | None:
    """The value a ``{{key}}`` placeholder takes from *context*, or None when it has none."""
    events = context.get("github_events", [])  # check github events first
    if events and isinstance(events, list) and isinstance(events[0], dict):
        val = events[0].get(key)
        if val is not None:
            return str(val)
    # Stringify non-string values too (e.g. numeric threshold-alert
    # fields) -- re.sub's replacement callback requires a str return.
    if key in context:
        return str(context[key])
    return None


def _render_template(template: str, context: dict) -> str:
    """Replace {{var}} placeholders with values from trigger context."""

    def _replace(m: re.Match) -> str:
        val = _template_value(m.group(1), context)
        return m.group(0) if val is None else val

    return _TEMPLATE_RE.sub(_replace, template)


def unfilled_template_vars(schedule: dict, trigger_context: dict) -> list[str]:
    """The ``{{var}}`` names in what *schedule* sends that *trigger_context* has no value for.

    The templates a launch fills are a command launch's ``action_command_args`` and, for a kind
    that sends one, the ``action_prompt``; other fields, a ``flow_yaml`` spec among them, go as
    written. A placeholder with no value renders as itself, so a command argument holding one is
    refused at spawn and a prompt hands it to the agent as written.
    """
    kind = schedule.get("action_kind")
    kind = _ALIAS_ACTION_KINDS.get(kind, kind)
    if kind == "command":
        args = schedule.get("action_command_args")
        templates = [str(a) for a in args] if isinstance(args, list) else []
    elif _sends_prompt(schedule, kind):
        templates = [schedule.get("action_prompt") or ""]
    else:
        templates = []
    return sorted(
        {
            m.group(1)
            for template in templates
            for m in _TEMPLATE_RE.finditer(template)
            if _template_value(m.group(1), trigger_context) is None
        }
    )


def render_action_prompt(schedule: dict, trigger_context: dict) -> str | None:
    """Render a schedule's ``action_prompt`` with the trigger context.

    Returns ``None`` when the schedule has no prompt template or its launch sends none, so
    callers can fall back to another field (e.g. ``action_playbook``) without confusing
    "no prompt" with "empty rendered prompt".
    """
    kind = schedule.get("action_kind")
    prompt = schedule.get("action_prompt") or ""
    if not prompt or not _sends_prompt(schedule, _ALIAS_ACTION_KINDS.get(kind, kind)):
        return None
    rendered = _render_template(prompt, trigger_context)
    _validate_prompt(rendered)
    return rendered


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
    kind = validate_launch_fields(schedule)
    model = schedule.get("action_model") or ""
    prompt = schedule.get("action_prompt") or ""
    agent = schedule.get("action_agent")
    playbook = schedule.get("action_playbook")
    project = schedule.get("action_project")
    extra = schedule.get("action_extra_args") or []
    play_args: dict = {}
    if kind == "play":
        stored_args = _playbook_args(schedule)
        play_args = stored_args if isinstance(stored_args, dict) else {}

    # A kind that does not send its prompt neither renders nor checks it: a command launch sends
    # its arguments only, whatever its stored prompt would render to.
    if not _sends_prompt(schedule, kind):
        prompt = ""
    # Render template variables from trigger context FIRST, then validate the
    # rendered prompt so that template-injected values (e.g. '{{payload}}' →
    # '--') are caught before argv construction.
    if prompt:
        prompt = _render_template(prompt, trigger_context)
    if prompt:
        _validate_prompt(prompt)
    if kind == "play" and prompt.startswith("-"):
        # Checked AFTER template rendering: `li play` has no '--' separator,
        # so a leading-dash positional re-parses as a flag, and a template
        # like '{{pr_title}}' can render to one (e.g. '--bypass') even when
        # the stored template passes a pre-render check.
        raise ValueError("action_prompt for a play launch must not start with '-'")

    argv = list(executable_prefix) if executable_prefix is not None else list(_DEFAULT_LI_PREFIX)
    tmp_path: str | None = None

    # CWE-88 hardening: named flags, then '--', then positionals -- makes
    # action_prompt injection-proof ('--bypass' parses as text, not a flag).
    if kind == "agent":
        flags: list[str] = []
        if agent:
            flags += ["--agent", agent]
        if project:
            flags += ["--project", project]
        # validate_launch_fields refused extra args beside an empty model.
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
        # `li play NAME [--args...] [prompt]` — the playbook name is a
        # validated identifier; typed playbook args (validated above) become
        # the play CLI's own schema-derived flags; the prompt positional
        # interpolates into the template's {input}. Bool args are store_true
        # flags: present when truthy, absent otherwise.
        argv += ["play"]
        if playbook:
            argv.append(playbook)
            for key, value in play_args.items():
                flag = "--" + key.replace("_", "-")
                if isinstance(value, bool):
                    if value:
                        argv.append(flag)
                else:
                    argv += [flag, str(value)]
            if prompt:
                argv.append(prompt)

    elif kind == "flow_yaml":
        # validate_launch_fields refused extra args, which would soak into flow's optional slots.
        # Caller deletes tmp_path after the subprocess exits.
        yaml_text = schedule.get("action_flow_yaml") or ""
        if model:
            # Merge into the spec rather than pass as a positional: when the
            # file has its own model/agent, a lone model positional gets
            # reclassified as prompt text by `li o flow -f`.
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
        # action_agent = engine kind (e.g. "research"); action_prompt = spec. The stored fields
        # are checked in validate_launch_fields; a spec that renders empty is refused here.
        if not prompt:
            raise ValueError("engine launches require action_prompt (the engine spec)")
        engine_kind = agent
        flags = []
        if model:
            flags += ["--model", model]
        engine_opts = schedule.get("action_engine_options") or {}
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
        # The command, its allowlisting and its argument list are checked in
        # validate_launch_fields.
        command = schedule.get("action_command") or ""
        command_args = schedule.get("action_command_args") or []
        rendered_args = [_render_command_arg(str(a), trigger_context) for a in command_args]
        argv = [command, *rendered_args]

    # Append validated extra positionals (safe, no leading '-').
    if kind not in ("engine", "command") and isinstance(extra, list):
        argv.extend(str(a) for a in extra)

    return argv, tmp_path


_TAIL_BYTES = 2048
_PROCESS_TERMINATION_GRACE_SECONDS = 5.0


class SubprocessDeadlineExceededError(TimeoutError):
    """The owned subprocess group did not finish within its execution deadline."""

    def __init__(self, *, invocation_id: str, deadline_seconds: float) -> None:
        self.invocation_id = invocation_id
        self.deadline_seconds = deadline_seconds
        super().__init__(
            f"invocation {invocation_id} exceeded its {deadline_seconds:g}s execution deadline"
        )


def _validated_deadline_seconds(value: str | int | float) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"deadline_seconds must be a positive number, got {value!r}") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f"deadline_seconds must be positive and finite, got {value!r}")
    return seconds


async def _drain_tail(stream: asyncio.StreamReader | None, limit: int) -> bytes:
    """Read a pipe to EOF, keeping only its last *limit* bytes.

    Reading has to continue to EOF even though nearly all of it is thrown away.
    A child writing to a pipe nobody drains blocks once the OS buffer fills, so
    "read what I want and stop" is a deadlock for any process that outproduces
    the buffer — which is every streaming agent leg.
    """
    if stream is None:
        return b""
    tail = b""
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            return tail
        tail = (tail + chunk)[-limit:]


def _diagnostic_tail(stdout: bytes, stderr: bytes) -> str:
    """Compose what an operator needs from the two streams.

    A stderr-only failure returns exactly what it always did, byte for byte, so
    the runs that already record useful tracebacks cannot regress. stdout is
    added only where it carries something, and is labelled when it appears,
    because which stream said it changes where a reader goes looking next.
    """
    err = stderr.decode(errors="replace")
    out = stdout.decode(errors="replace")
    if not out.strip():
        return err
    if not err.strip():
        return f"[stdout]\n{out}"
    return f"{err}\n[stdout]\n{out}"


async def spawn_and_wait(
    argv: list[str],
    invocation_id: str,
    *,
    tmp_path: str | None = None,
    cwd: str | None = None,
    action_kind: str | None = None,
    on_launched: Callable[[], Awaitable[None]] | None = None,
    deadline_seconds: float | None = None,
) -> tuple[int, str]:
    """Spawn subprocess and wait for completion. Returns (exit_code, output_tail).

    Both streams are drained concurrently and bounded rather than buffered
    whole. *tmp_path*, if given, is deleted after exit. *cwd* pins the
    subprocess working directory (``None`` inherits the daemon's own --
    callers should resolve a concrete path first, see
    ``SchedulerEngine._resolve_action_cwd``). *on_launched*, if given, is
    awaited right after the OS process is confirmed to exist, before waiting
    on completion; a failing callback is logged and swallowed, never allowed
    to fail an already-launched process. See docs/internals/studio.md for
    the allow-list re-check race this closes and why streams can't drain
    sequentially.

    *deadline_seconds* defaults to the validated global/per-action-kind
    Studio setting; expiry terminates the owned process group before
    raising :class:`SubprocessDeadlineExceededError`.
    """
    if deadline_seconds is None:
        from lionagi.studio.config import invocation_deadline_seconds

        deadline_seconds = invocation_deadline_seconds(action_kind)
    deadline_seconds = _validated_deadline_seconds(deadline_seconds)

    if action_kind == "command":
        command = argv[0] if argv else ""
        _validate_action_command(command)
        _validate_command_allowlisted(command)

    base_env = {**os.environ, "LIONAGI_INVOCATION_ID": invocation_id}
    if action_kind == "command":
        # A command is an arbitrary program, often run by another interpreter with its own
        # lionagi; see env_without_state_db_url. Every `li` child keeps the URL, so its run
        # records into the daemon's store.
        base_env = env_without_state_db_url(base_env)
    env, group_marker = prepare_process_group_env(base_env)

    # DEBUG only: argv carries the whole prompt and any argument a caller passed, newlines included.
    _log.debug("Spawning: %s", " ".join(argv))
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=cwd,
        # `uv run li` may fork further; own session/process group lets a
        # cancel signal the whole GROUP, not just the direct child --
        # otherwise grandchildren survive scheduler shutdown as orphans.
        start_new_session=True,
    )
    leader_created = observe_process_group_leader(
        getattr(proc, "pid", None),
        expected_marker=group_marker,
    )
    # Pgid == proc.pid; cleanup authority remains bound to this spawn identity.

    drain = asyncio.gather(
        _drain_tail(proc.stdout, _TAIL_BYTES),
        _drain_tail(proc.stderr, _TAIL_BYTES),
    )

    async def _stop_child() -> None:
        # SIGTERM then SIGKILL the whole group. The readers are cancelled first: they
        # are blocked on pipes belonging to the process being killed, and an
        # un-cancelled reader outlives it.
        drain.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await drain
        group_witnesses = (
            capture_group_witnesses(
                proc,
                expected_leader_created=leader_created,
                expected_marker=group_marker,
            )
            if leader_created is not None
            else ()
        )
        await aterminate_process_group(
            proc,
            grace=_PROCESS_TERMINATION_GRACE_SECONDS,
            group_witnesses=group_witnesses,
        )

    try:

        async def _wait_for_exit() -> tuple[bytes, bytes]:
            if on_launched is not None:
                try:
                    await on_launched()
                except Exception:
                    _log.exception(
                        "on_launched callback failed for invocation %s; the process "
                        "is already running regardless",
                        invocation_id,
                    )
            stdout_tail, stderr_tail = await drain
            await proc.wait()
            return stdout_tail, stderr_tail

        stdout, stderr = await asyncio.wait_for(_wait_for_exit(), timeout=deadline_seconds)
    except asyncio.TimeoutError as exc:
        _log.warning(
            "spawn_and_wait deadline exceeded; terminating child for %s after %ss",
            invocation_id,
            deadline_seconds,
        )
        await _stop_child()
        raise SubprocessDeadlineExceededError(
            invocation_id=invocation_id,
            deadline_seconds=deadline_seconds,
        ) from exc
    except asyncio.CancelledError:
        # Stop the group before re-raising, so a cancelled poll doesn't leave the
        # spawned tree detached.
        _log.warning("spawn_and_wait cancelled; terminating child for %s", invocation_id)
        await _stop_child()
        raise
    finally:
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)

    exit_code = proc.returncode or 0
    output_tail = _diagnostic_tail(stdout, stderr)

    _log.info("Process exited with code %d", exit_code)
    return exit_code, output_tail
