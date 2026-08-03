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
    "NotifyConfigResolution",
    "PayloadBuilder",
    "ResolvedNotifyHandler",
    "build_handler",
    "record_notify_outcome_to_run",
    "record_notify_rejection_to_run",
    "register_run_notify_outcome_scope",
    "register_settings_terminal_callback",
    "resolve_notify_config",
    "unregister_run_notify_outcome_scope",
)

# quoted spans are stripped first so a literal pipe/&/$ inside quotes never
# reads as shell syntax -- only bare, unquoted metacharacters trip it.
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


@dataclass(frozen=True)
class NotifyConfigResolution:
    """The outcome of resolving ``notify.on_terminal``: a handler, or why not.

    ``reason`` is set iff a notifier was asked for and rejected -- chosen
    silence (nothing configured, or explicitly off) carries no reason, so a
    misconfigured notifier stays distinguishable from an absent one. Reasons
    are short stable identifiers, never interpolated user data (persisted and
    read back); the offending value goes in the matching warning instead.
    """

    handler: ResolvedNotifyHandler | None = None
    reason: str | None = None


# Nothing was configured, so there is nothing to report -- silence by choice.
_NOT_CONFIGURED = NotifyConfigResolution()


def _rejected(reason: str) -> NotifyConfigResolution:
    """A notifier was configured and this resolution refused it."""
    return NotifyConfigResolution(reason=reason)


def resolve_notify_config(
    *,
    settings: dict[str, Any] | None = None,
    override: str | dict[str, Any] | None = None,
    project_dir: str | None = None,
) -> NotifyConfigResolution:
    """Resolve ``notify.on_terminal`` to a handler spec, or to why there is none.
    *override* wins outright when supplied; otherwise settings are loaded
    once (snapshot semantics) via the project-then-global merge.

    Always returns a :class:`NotifyConfigResolution`; see it for how a rejected
    notifier is distinguished from an unconfigured one.
    """
    if override is not None:
        return _resolve_shape(override, scope="override")

    if settings is None:
        try:
            settings = load_settings(project_dir=project_dir)
        except Exception as exc:  # noqa: BLE001 -- malformed settings must never affect the run
            logger.warning("notify.on_terminal settings resolution failed: %s", exc)
            return _rejected("settings_load_failed")
    notify_cfg = settings.get("notify") if isinstance(settings, dict) else None
    source = notify_cfg.get("on_terminal") if isinstance(notify_cfg, dict) else None
    if source is None:
        return _NOT_CONFIGURED  # no notifier configured -- the documented default
    return _resolve_shape(source, scope="settings")


def _resolve_shape(source: Any, *, scope: str) -> NotifyConfigResolution:
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
    return _rejected("on_terminal_not_string_or_mapping")


def _resolve_string(command: str, *, scope: str) -> NotifyConfigResolution:
    if not command.strip():
        _warn_empty_argv(scope)
        return _rejected("on_terminal_command_is_empty")
    if _looks_like_shell(command):
        _warn_shell_feature(command, scope)
        return _rejected("on_terminal_command_requires_shell_features")
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
        return _rejected("on_terminal_command_not_parseable")
    if not argv:
        _warn_empty_argv(scope)
        return _rejected("on_terminal_command_is_empty")
    return NotifyConfigResolution(handler=ResolvedNotifyHandler(argv=tuple(argv)))


def _resolve_mapping(cfg: dict[str, Any], *, scope: str) -> NotifyConfigResolution:
    if cfg.get("enabled") is False:
        # Notification was configured off. Nothing was rejected, so this is the
        # chosen silence, not a failure to report back to the operator.
        return _NOT_CONFIGURED

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
            return _rejected("filter_not_a_mapping")

        unknown_keys = tuple(key for key in filt if key not in {"kinds", "ids"})
        if unknown_keys:
            logger.warning(
                "notify.on_terminal (%s) filter keys must be 'kinds' and/or "
                "'ids', got unknown keys %r; resolving to disabled.",
                scope,
                unknown_keys,
            )
            return _rejected("filter_has_unknown_keys")

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
                return _rejected("filter_kinds_not_a_list_of_strings")
            unsupported_kinds = tuple(kind for kind in kinds if kind not in EXECUTION_ENTITY_KINDS)
            if unsupported_kinds:
                logger.warning(
                    "notify.on_terminal (%s) filter.kinds contains unsupported "
                    "terminal entity kinds %r; expected only %r; resolving to disabled.",
                    scope,
                    unsupported_kinds,
                    tuple(sorted(EXECUTION_ENTITY_KINDS)),
                )
                return _rejected("filter_kinds_unsupported")
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
                return _rejected("filter_ids_not_a_list_of_strings")
            filter_ids = tuple(ids)

    adapter = cfg.get("adapter")
    if not isinstance(adapter, dict):
        if cfg.get("enabled"):
            logger.warning(
                "notify.on_terminal (%s) mapping form is enabled with no "
                "adapter configured; resolving to disabled.",
                scope,
            )
            return _rejected("enabled_without_adapter")
        # A mapping that never asked to be enabled and names no adapter asked
        # for nothing; that is the chosen silence, not a rejected notifier.
        return _NOT_CONFIGURED

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
            return _rejected("exec_adapter_argv_not_a_list_of_strings")
        if not argv:
            _warn_empty_argv(scope)
            return _rejected("on_terminal_command_is_empty")
        return NotifyConfigResolution(
            handler=ResolvedNotifyHandler(
                argv=tuple(argv), filter_kinds=filter_kinds, filter_ids=filter_ids
            )
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
            return _rejected("python_adapter_ref_invalid")
        return NotifyConfigResolution(
            handler=ResolvedNotifyHandler(
                python_ref=ref, filter_kinds=filter_kinds, filter_ids=filter_ids
            )
        )

    logger.warning(
        "notify.on_terminal (%s) adapter kind must be 'exec' or 'python', "
        "got %r; resolving to disabled.",
        scope,
        kind,
    )
    return _rejected("adapter_kind_unsupported")


# Returns the path the adapter's stderr was captured to, or None.
OutcomeFn = Callable[..., "str | None"]


def record_notify_outcome_to_run(
    run: RunDir, *, ok: bool, exit_code: int | None, stderr_text: str | None
) -> str | None:
    """Best-effort: record the exec adapter's outcome into *run*'s own
    notify_outcome.json (a single file, replacement semantics, never merged
    with -- or written into -- run.json), and capture the adapter's stderr
    to an owner-readable file beside it. Returns that file's path, or None
    if there was no stderr to capture. Never raises: this must never affect
    the run itself.

    The stderr text itself is never placed in the outcome record. Adapter
    output is free text that can contain a credential from any source --
    an inherited environment variable, a file the adapter read -- so it
    cannot be scrubbed by matching against values we know. Keeping it in
    one owner-only file, referenced by path, is what bounds the exposure:
    the aggregated surfaces (this record, the log, the warning channel)
    carry the path instead of the content.
    """
    stderr_path: str | None = None
    try:
        if stderr_text:
            stderr_path = str(run.write_notify_stderr(stderr_text))
    except Exception:  # noqa: BLE001 -- outcome bookkeeping must never affect the run
        logger.debug("failed to capture notify.on_terminal adapter stderr", exc_info=True)
    try:
        run.write_notify_outcome(
            {
                "ok": ok,
                "exit_code": exit_code,
                "stderr_path": stderr_path,
            }
        )
    except Exception:  # noqa: BLE001 -- outcome bookkeeping must never affect the run
        logger.debug("failed to record notify.on_terminal outcome", exc_info=True)
    return stderr_path


def record_notify_rejection_to_run(run: RunDir, reason: str) -> None:
    """Best-effort: record into *run*'s own notify_outcome.json that a notifier
    was configured for this run and refused, so the run says which of the two
    silences it is. A run with nothing configured writes no file at all; a run
    whose notifier could not be resolved or built writes an unsuccessful
    outcome carrying the stable reason. Without this, a caller waiting on a
    terminal notice that can never arrive sees exactly what a caller that never
    asked for one sees. Never raises.

    ``exit_code`` and ``stderr_path`` are null because nothing was launched:
    the adapter was refused before it could run.
    """
    try:
        run.write_notify_outcome(
            {"ok": False, "exit_code": None, "stderr_path": None, "reason": reason}
        )
    except Exception:  # noqa: BLE001 -- outcome bookkeeping must never affect the run
        logger.debug("failed to record notify.on_terminal rejection", exc_info=True)


def _warn_adapter_failure(msg: str) -> None:
    try:
        from lionagi.cli._logging import warn

        warn(msg)
    except Exception:  # noqa: BLE001 -- surfacing the failure must never affect the run
        logger.debug("failed to emit notify.on_terminal warn-channel line", exc_info=True)


# Redaction contract for adapter argv/stderr: see docs/internals/runtime.md.
STDERR_SNIPPET_LIMIT = 200

MIN_REDACTABLE_ARG_LEN = 4


def _adapter_label(argv: Sequence[str]) -> str:
    """The adapter's display name for every surface: argv[0]'s basename
    only, never the full argv (which may carry secret args)."""
    if not argv:
        return "<adapter>"
    return os.path.basename(str(argv[0])) or str(argv[0])


def _redact_arg_values(text: str, argv: Sequence[str]) -> str:
    """Replace any adapter argument value that appears verbatim in *text*.
    Longest values first so a substring never leaves a partial value behind;
    not a general secret scanner, see docs/internals/runtime.md."""
    values = sorted(
        (str(arg) for arg in tuple(argv)[1:] if len(str(arg)) >= MIN_REDACTABLE_ARG_LEN),
        key=len,
        reverse=True,
    )
    for value in values:
        text = text.replace(value, "***")
    return text


def _first_line(text: str, argv: Sequence[str] = ()) -> str | None:
    """First line of *text* with adapter argument values replaced, bounded
    to STDERR_SNIPPET_LIMIT chars.

    Redaction runs before bounding so a value straddling the limit cannot
    leave a partial value in the truncated result.
    """
    stripped = text.strip()
    if not stripped:
        return None
    line = _redact_arg_values(stripped.splitlines()[0], argv)
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


def _noop_outcome_fn(*, ok: bool, exit_code: int | None, stderr_text: str | None) -> str | None:
    """No run is bound to this handler -- outcome recording is skipped
    rather than guessing at a target (see register_run_notify_outcome_scope).
    With no run directory there is nowhere owner-only to put the adapter's
    stderr, so it is dropped rather than routed to a shared surface."""
    return None


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
            logger.warning(
                "notify.on_terminal exec adapter %s timed out", _adapter_label(launch_argv)
            )
            outcome_fn(ok=False, exit_code=None, stderr_text=None)
            _warn_adapter_failure(
                f"notify.on_terminal adapter {_adapter_label(launch_argv)} timed out"
            )
            return
        except get_cancelled_exc_class():
            # deadline race: child must still be reaped, shielded since scope is cancelled
            if proc is not None:
                with CancelScope(shield=True):
                    await aterminate_process_group(proc, grace=None)
                    await _await_proc_dead(proc)
            raise
        except Exception as exc:  # noqa: BLE001 -- an adapter failure must never affect the run
            detail = _first_line(str(exc), launch_argv)
            logger.warning(
                "notify.on_terminal exec adapter %s failed to run: %s",
                _adapter_label(launch_argv),
                detail,
            )
            outcome_fn(ok=False, exit_code=None, stderr_text=None)
            warn_suffix = f": {detail}" if detail else ""
            _warn_adapter_failure(
                f"notify.on_terminal adapter {_adapter_label(launch_argv)} failed to run{warn_suffix}"
            )
            return
        if proc.returncode != 0:
            # raw stderr never reaches log/warn/outcome -- captured to an owner-only
            # file and referenced by path instead, see docs/internals/runtime.md
            stderr_text = stderr_bytes.decode(errors="replace").strip()
            stderr_path = outcome_fn(
                ok=False, exit_code=proc.returncode, stderr_text=stderr_text or None
            )
            if stderr_path:
                where = f"; stderr captured at {stderr_path}"
            elif stderr_text:
                where = "; stderr not captured (no run directory bound to this handler)"
            else:
                where = ""
            logger.warning(
                "notify.on_terminal exec adapter %s exited %s%s",
                _adapter_label(launch_argv),
                proc.returncode,
                where,
            )
            _warn_adapter_failure(
                f"notify.on_terminal adapter {_adapter_label(launch_argv)} exited "
                f"{proc.returncode}{where}"
            )
        else:
            outcome_fn(ok=True, exit_code=0, stderr_text=None)

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
    on_build_failure: Callable[[str], None] | None = None,
) -> TerminalCallbackHandler | None:
    """Build the process-local handler for a resolved adapter spec, or
    ``None`` if the spec fails to build (never raises). A python adapter ref
    is imported eagerly here so a bad ref resolves to disabled, not a crash.

    *outcome_fn*, if given, is called with the exec adapter's outcome
    (``ok``, ``exit_code``, ``stderr_text``) and returns the path its stderr
    was captured to, if any -- omit it (the default)
    when no specific run is bound to this handler; outcome recording is then
    skipped rather than guessing at a target run. Never applies to a python
    adapter (only the exec adapter's process outcome is tracked).

    *on_build_failure*, if given, is called with a stable reason when the spec
    is refused here. A spec that resolves and then fails to build is still a
    configured notifier that will never fire, and this is the only place that
    knows why; a caller that can record it (one bound to a run) passes this so
    the failure does not reduce to the same ``None`` as nothing configured.
    """
    if resolved.python_ref is not None:
        try:
            handler = _make_python_handler(resolved.python_ref)
        except Exception as exc:  # noqa: BLE001 -- a bad adapter ref must resolve to disabled
            logger.warning(
                "notify.on_terminal python adapter %r failed to import: %s; resolving to disabled.",
                resolved.python_ref,
                exc,
            )
            # One identifier covers a missing module and a missing attribute:
            # the reason is the stable contract, and the warning above carries
            # which of the two it was.
            if on_build_failure is not None:
                on_build_failure("python_adapter_unresolvable")
            return None
        if not callable(handler):
            logger.warning(
                "notify.on_terminal python adapter %r resolved to a non-callable "
                "%s; resolving to disabled.",
                resolved.python_ref,
                type(handler).__name__,
            )
            if on_build_failure is not None:
                on_build_failure("python_adapter_not_callable")
            return None
        return handler
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
    process). Returns ``True`` iff a handler was installed; a rejected
    configuration is already reported through the resolver's own warning."""
    resolved = resolve_notify_config(project_dir=project_dir).handler
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
    *run*, scoped to this run's own terminal entity. An override registration,
    so it takes over dispatch for this entity from the process-wide default.
    Returns the registration name (pass to
    ``unregister_run_notify_outcome_scope`` in a ``finally`` block), or
    ``None`` if disabled or filtered out (never raises); a notifier this run
    asked for and could not have is recorded onto the run before returning --
    see docs/internals/runtime.md for the silence-vs-rejection contract."""
    resolution = resolve_notify_config(project_dir=project_dir)
    if resolution.reason is not None:
        record_notify_rejection_to_run(run, resolution.reason)
        return None
    resolved = resolution.handler
    if resolved is None:
        return None
    # override dispatches on its own match, so it must re-apply the configured
    # filter itself rather than deferring to the process-wide registration's
    if resolved.filter_kinds is not None and entity_kind not in resolved.filter_kinds:
        return None
    if resolved.filter_ids is not None and entity_id not in resolved.filter_ids:
        return None

    def _outcome_fn(*, ok: bool, exit_code: int | None, stderr_text: str | None) -> str | None:
        return record_notify_outcome_to_run(
            run, ok=ok, exit_code=exit_code, stderr_text=stderr_text
        )

    def _build_failure(reason: str) -> None:
        record_notify_rejection_to_run(run, reason)

    handler = build_handler(resolved, outcome_fn=_outcome_fn, on_build_failure=_build_failure)
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
