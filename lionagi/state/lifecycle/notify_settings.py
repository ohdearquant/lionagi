# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Settings-driven terminal-callback handler. Resolves ``notify.on_terminal``
into a handler installable on a ``TerminalCallbackRegistry``; no
configuration shape ever reaches a shell. See docs/internals/runtime.md.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import re
import shlex
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from lionagi.agent.settings import load_settings
from lionagi.ln._proc import aterminate_process_group
from lionagi.ln.concurrency import CancelScope, get_cancelled_exc_class

from .callbacks import (
    DEFAULT_TERMINAL_CALLBACKS,
    EXECUTION_ENTITY_KINDS,
    HANDLER_BUDGET_SECONDS,
    RunTerminalEnvelope,
    TerminalCallbackHandler,
    TerminalCallbackRegistry,
)

if TYPE_CHECKING:
    from lionagi.cli._runs import RunDir

logger = logging.getLogger(__name__)

__all__ = (
    "PayloadBuilder",
    "ResolvedNotifyHandler",
    "build_handler",
    "register_run_notify_outcome_scope",
    "register_settings_terminal_callback",
    "resolve_notify_config",
    "unregister_run_notify_outcome_scope",
)

# Matches inside a quoted span are stripped before this runs, so a literal
# pipe/ampersand/dollar-sign inside a quoted argument is never mistaken for
# shell syntax -- only bare, unquoted shell metacharacters trip it.
_QUOTED_SPAN_RE = re.compile(r"'[^']*'|\"[^\"]*\"")
_SHELL_FEATURE_RE = re.compile(r"\|\||&&|[|<>;&`]|\$\(|\$\{|\$[A-Za-z_]")


def _looks_like_shell(command: str) -> bool:
    unquoted = _QUOTED_SPAN_RE.sub("", command)
    return bool(_SHELL_FEATURE_RE.search(unquoted))


def _warn_empty_argv(scope: str) -> None:
    logger.warning(
        "notify.on_terminal (%s) resolved to an empty command; use the "
        "argv-list mapping form ({adapter: {kind: exec, argv: [...]}}) to "
        "configure a real command. Resolving to disabled.",
        scope,
    )


def _warn_shell_feature(command: str, scope: str) -> None:
    logger.warning(
        "notify.on_terminal (%s) value %r requires shell features (pipes, "
        "redirection, conjunction, or variable expansion) that are never "
        "honored -- no configuration shape reaches a shell. Use the "
        "argv-list mapping form instead. Resolving to disabled.",
        scope,
        command,
    )


@dataclass(frozen=True)
class ResolvedNotifyHandler:
    """A validated, launchable adapter -- exactly one of argv/python_ref is set."""

    argv: tuple[str, ...] | None = None
    python_ref: str | None = None
    filter_kinds: tuple[str, ...] | None = None
    filter_ids: tuple[str, ...] | None = None


def resolve_notify_config(
    *,
    settings: dict[str, Any] | None = None,
    override: str | dict[str, Any] | None = None,
    project_dir: str | None = None,
) -> ResolvedNotifyHandler | None:
    """Resolve ``notify.on_terminal`` to a handler spec, or ``None`` (disabled).
    *override* wins outright when supplied; otherwise settings are loaded
    once (snapshot semantics) via the project-then-global merge.
    """
    if override is not None:
        return _resolve_shape(override, scope="override")

    if settings is None:
        try:
            settings = load_settings(project_dir=project_dir)
        except Exception as exc:  # noqa: BLE001 -- malformed settings must never affect the run
            logger.warning("notify.on_terminal settings resolution failed: %s", exc)
            return None
    notify_cfg = settings.get("notify") if isinstance(settings, dict) else None
    source = notify_cfg.get("on_terminal") if isinstance(notify_cfg, dict) else None
    if source is None:
        return None
    return _resolve_shape(source, scope="settings")


def _resolve_shape(source: Any, *, scope: str) -> ResolvedNotifyHandler | None:
    if isinstance(source, str):
        return _resolve_string(source, scope=scope)
    if isinstance(source, dict):
        return _resolve_mapping(source, scope=scope)
    logger.warning(
        "notify.on_terminal (%s) must be a string or mapping, got %s: %r",
        scope,
        type(source).__name__,
        source,
    )
    return None


def _resolve_string(command: str, *, scope: str) -> ResolvedNotifyHandler | None:
    if not command.strip():
        _warn_empty_argv(scope)
        return None
    if _looks_like_shell(command):
        _warn_shell_feature(command, scope)
        return None
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        logger.warning(
            "notify.on_terminal (%s) value %r failed to parse as an argv "
            "command (%s); use the argv-list mapping form instead. "
            "Resolving to disabled.",
            scope,
            command,
            exc,
        )
        return None
    if not argv:
        _warn_empty_argv(scope)
        return None
    return ResolvedNotifyHandler(argv=tuple(argv))


def _resolve_mapping(cfg: dict[str, Any], *, scope: str) -> ResolvedNotifyHandler | None:
    if cfg.get("enabled") is False:
        return None

    filter_kinds: tuple[str, ...] | None = None
    filter_ids: tuple[str, ...] | None = None
    if "filter" in cfg:
        filt = cfg["filter"]
        if not isinstance(filt, Mapping) or not filt:
            logger.warning(
                "notify.on_terminal (%s) filter must be a non-empty mapping, "
                "got %r; resolving to disabled.",
                scope,
                filt,
            )
            return None

        unknown_keys = tuple(key for key in filt if key not in {"kinds", "ids"})
        if unknown_keys:
            logger.warning(
                "notify.on_terminal (%s) filter keys must be 'kinds' and/or "
                "'ids', got unknown keys %r; resolving to disabled.",
                scope,
                unknown_keys,
            )
            return None

        if "kinds" in filt:
            kinds = filt["kinds"]
            if (
                not isinstance(kinds, list)
                or not kinds
                or not all(isinstance(kind, str) for kind in kinds)
            ):
                logger.warning(
                    "notify.on_terminal (%s) filter.kinds must be a list of "
                    "strings with at least one value, got %r; resolving to disabled.",
                    scope,
                    kinds,
                )
                return None
            unsupported_kinds = tuple(kind for kind in kinds if kind not in EXECUTION_ENTITY_KINDS)
            if unsupported_kinds:
                logger.warning(
                    "notify.on_terminal (%s) filter.kinds contains unsupported "
                    "terminal entity kinds %r; expected only %r; resolving to disabled.",
                    scope,
                    unsupported_kinds,
                    tuple(sorted(EXECUTION_ENTITY_KINDS)),
                )
                return None
            filter_kinds = tuple(kinds)

        if "ids" in filt:
            ids = filt["ids"]
            if (
                not isinstance(ids, list)
                or not ids
                or not all(isinstance(entity_id, str) and bool(entity_id) for entity_id in ids)
            ):
                logger.warning(
                    "notify.on_terminal (%s) filter.ids must be a list of "
                    "strings with at least one value, got %r; resolving to disabled.",
                    scope,
                    ids,
                )
                return None
            filter_ids = tuple(ids)

    adapter = cfg.get("adapter")
    if not isinstance(adapter, dict):
        if cfg.get("enabled"):
            logger.warning(
                "notify.on_terminal (%s) mapping form is enabled with no "
                "adapter configured; resolving to disabled.",
                scope,
            )
        return None

    kind = adapter.get("kind")
    if kind == "exec":
        argv = adapter.get("argv")
        if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
            logger.warning(
                "notify.on_terminal (%s) exec adapter requires an argv "
                "list of strings, got %r; resolving to disabled.",
                scope,
                argv,
            )
            return None
        if not argv:
            _warn_empty_argv(scope)
            return None
        return ResolvedNotifyHandler(
            argv=tuple(argv), filter_kinds=filter_kinds, filter_ids=filter_ids
        )

    if kind == "python":
        ref = adapter.get("ref")
        if not isinstance(ref, str) or ":" not in ref:
            logger.warning(
                "notify.on_terminal (%s) python adapter requires a "
                "'module:callable' ref, got %r; resolving to disabled.",
                scope,
                ref,
            )
            return None
        return ResolvedNotifyHandler(
            python_ref=ref, filter_kinds=filter_kinds, filter_ids=filter_ids
        )

    logger.warning(
        "notify.on_terminal (%s) adapter kind must be 'exec' or 'python', "
        "got %r; resolving to disabled.",
        scope,
        kind,
    )
    return None


OutcomeFn = Callable[..., None]


def _record_notify_outcome_to_run(
    run: RunDir, *, ok: bool, exit_code: int | None, stderr_first_line: str | None
) -> None:
    """Best-effort: record the exec adapter's outcome into *run*'s own
    notify_outcome.json (a single file, replacement semantics, never merged
    with -- or written into -- run.json). Never raises: this must never
    affect the run itself.
    """
    try:
        run.write_notify_outcome(
            {
                "ok": ok,
                "exit_code": exit_code,
                "stderr_first_line": stderr_first_line,
            }
        )
    except Exception:  # noqa: BLE001 -- outcome bookkeeping must never affect the run
        logger.debug("failed to record notify.on_terminal outcome", exc_info=True)


def _warn_adapter_failure(msg: str) -> None:
    try:
        from lionagi.cli._logging import warn

        warn(msg)
    except Exception:  # noqa: BLE001 -- surfacing the failure must never affect the run
        logger.debug("failed to emit notify.on_terminal warn-channel line", exc_info=True)


# A notify.on_terminal adapter's argv routinely carries secrets (webhook
# URLs, tokens passed as args), and its stderr is adapter-controlled free
# text that can echo those secrets back (e.g. a shell script that prints
# its own invocation on error). User-facing surfaces -- the warn-channel
# line and the persisted notify_outcome.json -- must never carry either
# verbatim; developer-only channels (logger.warning/debug) may keep the
# full argv since those aren't shipped in user-visible/persisted artifacts.
STDERR_SNIPPET_LIMIT = 200


def _adapter_label(argv: Sequence[str]) -> str:
    """The adapter's display name for user-facing surfaces: argv[0]'s
    basename only, never the full argv (which may carry secret args)."""
    if not argv:
        return "<adapter>"
    return os.path.basename(str(argv[0])) or str(argv[0])


def _first_line(text: str) -> str | None:
    """First line of *text*, bounded to STDERR_SNIPPET_LIMIT chars. Stderr
    content is adapter-controlled and can echo back secrets from argv (e.g.
    a webhook URL in a "command not found" or curl error message); bounding
    the length caps -- without any false promise of full redaction -- how
    much of that free text ends up in the warn line or the persisted
    outcome file.
    """
    stripped = text.strip()
    if not stripped:
        return None
    line = stripped.splitlines()[0]
    if len(line) > STDERR_SNIPPET_LIMIT:
        line = line[:STDERR_SNIPPET_LIMIT] + "…"
    return line


async def _await_proc_dead(proc: asyncio.subprocess.Process, grace: float = 2.0) -> None:
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace)
    except Exception:  # noqa: BLE001 -- best-effort reap, never let this raise
        logger.debug(
            "timed out waiting for notify.on_terminal adapter process %s to exit",
            proc.pid,
            exc_info=True,
        )


PayloadBuilder = Callable[[RunTerminalEnvelope], dict[str, Any]]
ArgvBuilder = Callable[[tuple[str, ...], RunTerminalEnvelope], Sequence[str]]
EnvBuilder = Callable[[RunTerminalEnvelope], Mapping[str, str]]


def _default_payload(envelope: RunTerminalEnvelope) -> dict[str, Any]:
    return envelope.to_dict()


def _noop_outcome_fn(*, ok: bool, exit_code: int | None, stderr_first_line: str | None) -> None:
    """No run is bound to this handler -- outcome recording is skipped
    rather than guessing at a target (see register_run_notify_outcome_scope)."""


def _make_exec_handler(
    argv: Sequence[str],
    *,
    payload_fn: PayloadBuilder = _default_payload,
    argv_fn: ArgvBuilder | None = None,
    env_fn: EnvBuilder | None = None,
    outcome_fn: OutcomeFn = _noop_outcome_fn,
) -> TerminalCallbackHandler:
    static_argv = tuple(argv)

    async def _exec_handler(envelope: RunTerminalEnvelope) -> None:
        payload = json.dumps(payload_fn(envelope)).encode()
        launch_argv = tuple(argv_fn(static_argv, envelope)) if argv_fn is not None else static_argv
        env = {**os.environ, **env_fn(envelope)} if env_fn is not None else None
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *launch_argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=True,
            )
            _, stderr_bytes = await asyncio.wait_for(
                proc.communicate(payload), timeout=HANDLER_BUDGET_SECONDS
            )
        except asyncio.TimeoutError:
            if proc is not None:
                await aterminate_process_group(proc, grace=None)
                await _await_proc_dead(proc)
            logger.warning("notify.on_terminal exec adapter %r timed out", launch_argv)
            outcome_fn(ok=False, exit_code=None, stderr_first_line=None)
            _warn_adapter_failure(
                f"notify.on_terminal adapter {_adapter_label(launch_argv)} timed out"
            )
            return
        except get_cancelled_exc_class():
            # The registry's shared deadline races this call's own timeout and
            # typically wins; the child (its own process group) must still be
            # reaped, shielded since the enclosing scope is already cancelled.
            if proc is not None:
                with CancelScope(shield=True):
                    await aterminate_process_group(proc, grace=None)
                    await _await_proc_dead(proc)
            raise
        except Exception as exc:  # noqa: BLE001 -- an adapter failure must never affect the run
            logger.warning("notify.on_terminal exec adapter %r failed to run: %s", launch_argv, exc)
            detail = _first_line(str(exc))
            outcome_fn(ok=False, exit_code=None, stderr_first_line=detail)
            warn_suffix = f": {detail}" if detail else ""
            _warn_adapter_failure(
                f"notify.on_terminal adapter {_adapter_label(launch_argv)} failed to run{warn_suffix}"
            )
            return
        if proc.returncode != 0:
            detail = stderr_bytes.decode(errors="replace").strip()
            suffix = f": {detail}" if detail else ""
            logger.warning(
                "notify.on_terminal exec adapter %r exited %s%s",
                launch_argv,
                proc.returncode,
                suffix,
            )
            bounded_detail = _first_line(detail)
            outcome_fn(ok=False, exit_code=proc.returncode, stderr_first_line=bounded_detail)
            warn_suffix = f": {bounded_detail}" if bounded_detail else ""
            _warn_adapter_failure(
                f"notify.on_terminal adapter {_adapter_label(launch_argv)} exited "
                f"{proc.returncode}{warn_suffix}"
            )
        else:
            outcome_fn(ok=True, exit_code=0, stderr_first_line=None)

    return _exec_handler


def _make_python_handler(ref: str) -> TerminalCallbackHandler:
    module_path, _, func_name = ref.rpartition(":")
    module = importlib.import_module(module_path)
    return getattr(module, func_name)


def build_handler(
    resolved: ResolvedNotifyHandler,
    *,
    payload_fn: PayloadBuilder = _default_payload,
    argv_fn: ArgvBuilder | None = None,
    env_fn: EnvBuilder | None = None,
    outcome_fn: OutcomeFn | None = None,
) -> TerminalCallbackHandler | None:
    """Build the process-local handler for a resolved adapter spec, or
    ``None`` if the spec fails to build (never raises). A python adapter ref
    is imported eagerly here so a bad ref resolves to disabled, not a crash.

    *outcome_fn*, if given, is called with the exec adapter's outcome
    (``ok``, ``exit_code``, ``stderr_first_line``) -- omit it (the default)
    when no specific run is bound to this handler; outcome recording is then
    skipped rather than guessing at a target run. Never applies to a python
    adapter (only the exec adapter's process outcome is tracked).
    """
    if resolved.python_ref is not None:
        try:
            return _make_python_handler(resolved.python_ref)
        except Exception as exc:  # noqa: BLE001 -- a bad adapter ref must resolve to disabled
            logger.warning(
                "notify.on_terminal python adapter %r failed to import: %s; resolving to disabled.",
                resolved.python_ref,
                exc,
            )
            return None
    assert resolved.argv is not None  # _resolve_* never returns an empty spec
    kwargs: dict[str, Any] = {"payload_fn": payload_fn, "argv_fn": argv_fn, "env_fn": env_fn}
    if outcome_fn is not None:
        kwargs["outcome_fn"] = outcome_fn
    return _make_exec_handler(resolved.argv, **kwargs)


def register_settings_terminal_callback(
    registry: TerminalCallbackRegistry = DEFAULT_TERMINAL_CALLBACKS,
    *,
    name: str = "notify.settings.on_terminal",
    project_dir: str | None = None,
) -> bool:
    """Resolve ``notify.on_terminal`` from settings once and register it (the
    CLI entry point and Studio service startup each call this once per
    process). Returns ``True`` iff a handler was installed.
    """
    resolved = resolve_notify_config(project_dir=project_dir)
    if resolved is None:
        registry.unregister(name)
        return False
    handler = build_handler(resolved)
    if handler is None:
        registry.unregister(name)
        return False
    registry.register(
        name,
        handler,
        kinds=resolved.filter_kinds,
        ids=resolved.filter_ids,
    )
    return True


def register_run_notify_outcome_scope(
    run: RunDir,
    *,
    entity_kind: str,
    entity_id: str,
    registry: TerminalCallbackRegistry = DEFAULT_TERMINAL_CALLBACKS,
    project_dir: str | None = None,
) -> str | None:
    """Bind the settings-driven notify.on_terminal exec adapter's outcome to
    *run*, scoped to this run's own terminal entity (``entity_kind``/
    ``entity_id``), so a late-arriving outcome always lands on this run --
    or nowhere -- even if the process has since allocated other runs. The
    scoped registration is an override, so it takes over adapter dispatch
    for this entity from the process-wide default registered by
    ``register_settings_terminal_callback`` (which never attributes an
    outcome to any run). Returns the registration name (pass to
    ``unregister_run_notify_outcome_scope`` in a ``finally`` block), or
    ``None`` if notify.on_terminal resolved to disabled (never raises).
    """
    resolved = resolve_notify_config(project_dir=project_dir)
    if resolved is None:
        return None

    def _outcome_fn(*, ok: bool, exit_code: int | None, stderr_first_line: str | None) -> None:
        _record_notify_outcome_to_run(
            run, ok=ok, exit_code=exit_code, stderr_first_line=stderr_first_line
        )

    handler = build_handler(resolved, outcome_fn=_outcome_fn)
    if handler is None:
        return None
    name = f"notify.settings.on_terminal.{entity_kind}.{entity_id}"
    registry.register(name, handler, kinds=[entity_kind], ids=[entity_id], override=True)
    return name


def unregister_run_notify_outcome_scope(
    name: str | None,
    registry: TerminalCallbackRegistry = DEFAULT_TERMINAL_CALLBACKS,
) -> None:
    if name is not None:
        registry.unregister(name)
