# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Terminal hook for background MCP jobs, invoked by the CLI via ``--notify``.

Runs once a background run reaches a terminal status, both steps best-effort
(the run has already finished; nothing here may raise into the CLI's terminal
path):

1. Records the terminal status on the MCP job record, so ``job.status`` /
   ``job.list`` report an authoritative status instead of only inferring
   ``exited`` from a gone pid.
2. Delivers a terminal notice through a *configured* command (``--command``
   override, or lionagi's ``notify.on_terminal`` setting) with ``{run_id}``/
   ``{status}``/``{label}``/``{target}`` substituted into argv and offered as
   JSON on stdin. Nothing configured means silent by default; configured but
   unusable is recorded as a delivery failure, never passed off as silence.
   Runs in the *submitting* directory (``LIONAGI_MCP_NOTIFY_CWD`` overrides),
   not wherever the run executed, so the notifier signs as the submitter.

Run by absolute argv, never through a shell.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from typing import Any

# The CLI runs this file's module by absolute interpreter path; lionagi is on
# the path because that interpreter is the one lionagi is installed in.
from . import config, jobs

_DELIVERY_TIMEOUT_S = 30

# A notifier's stdout/stderr is free text that may carry a credential, so it's
# read into memory, matched against the closed vocabulary below, and dropped —
# only the matched name is stored, keeping the field a bounded enum by
# construction rather than by a promise to sanitise.
_FAILURE_UNKNOWN = "unknown"
# First match wins, so a more specific phrase must precede a broader one it
# contains — e.g. "connection refused" above the policy class, which
# deliberately does NOT match a bare "refused" (too common in network errors).
# An unanticipated phrasing falls to `unknown`, the correct direction to be wrong.
_FAILURE_CLASSES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("command_not_found", ("command not found", "no such file or directory", "not found")),
    ("permission_denied", ("permission denied", "operation not permitted", "eacces")),
    (
        "connection_failed",
        ("connection refused", "no route to host", "unreachable", "network is", "dns"),
    ),
    ("authentication_failed", ("unauthorized", "authentication failed", "invalid token", "401")),
    ("refused_by_policy", ("refused by", "blocked by", "denied by policy", "forbidden", "403")),
    ("target_unknown", ("unknown recipient", "no such actor", "unknown actor", "no such user")),
    ("invalid_usage", ("usage:", "unrecognized argument", "invalid argument", "unknown option")),
    # A delivery command that verifies who it would be sending AS (kkernel's
    # --expect-actor) refuses when the working directory resolves to a different
    # identity than the record's sender. That is a producer-side configuration
    # fact — the submit named a sender its own directory cannot sign for — and
    # an operator reading "unknown" cannot tell it from a genuinely novel
    # failure, though it has exactly one fix (make notify_sender match the
    # submitting directory's actor, or omit it).
    ("sender_identity_mismatch", ("expect-actor mismatch",)),
)


_FAILURE_TIMEOUT = "timeout"

# Every name that can ever be stored: the classified ones, `unknown`, and
# `timeout` (assigned on the exception path, so it never passes the classifier).
_ALLOWED_FAILURE_CLASSES: frozenset[str] = frozenset(
    {name for name, _ in _FAILURE_CLASSES} | {_FAILURE_UNKNOWN, _FAILURE_TIMEOUT}
)


def _pin_failure_class(value: str | None) -> str | None:
    """Force a classification into the closed set, at the boundary that stores it.

    Enforced here rather than trusted of ``_classify_failure`` — a later edit
    that starts returning command output would otherwise persist it verbatim.
    ``None`` passes through unchanged: it means delivery succeeded.
    """
    if value is None:
        return None
    return value if value in _ALLOWED_FAILURE_CLASSES else _FAILURE_UNKNOWN


def _classify_failure(text: str) -> str:
    """Map a delivery command's output to one name from the closed set above.

    Fail-closed: unmatched text is ``unknown``, never a fragment of itself —
    the moment that changes, the stored field is free text again.
    """
    lowered = text.lower()
    for name, needles in _FAILURE_CLASSES:
        if any(needle in lowered for needle in needles):
            return name
    return _FAILURE_UNKNOWN


def _classify_quietly(text: str) -> str:
    """``_classify_failure`` with every escape route closed.

    An exception raised while classifying must not carry the text out with it.
    Python exceptions routinely embed the value that caused them, so this
    swallows the exception object entirely rather than logging it or putting
    its message anywhere near the record.
    """
    try:
        return _classify_failure(text)
    except Exception:  # noqa: BLE001 — deliberately not logged: the message could embed the text
        return _FAILURE_UNKNOWN


def _resolve_command(
    override: str | None, *, cwd: str | None
) -> tuple[list[str] | None, str | None]:
    """The delivery argv template, paired with why there is none.

    Returns ``(argv, None)`` resolved, ``(None, None)`` nothing configured, or
    ``(None, reason)`` configured-but-unusable — three-way rather than two, so
    a broken notifier is never indistinguishable from an unconfigured one
    (silence is correct only when chosen). *override* (a JSON argv list) wins
    outright; otherwise lionagi's ``notify.on_terminal`` setting resolves.
    Nothing here raises — the run has already finished.
    """
    if override:
        try:
            parsed = json.loads(override)
        except json.JSONDecodeError:
            return None, "delivery_command_is_not_valid_json"
        if not isinstance(parsed, list) or not all(isinstance(tok, str) for tok in parsed):
            return None, "delivery_command_is_not_a_list_of_strings"
        if not parsed:
            return None, "delivery_command_is_empty"
        return parsed, None

    try:
        from lionagi.state.lifecycle.notify_settings import resolve_notify_config

        resolution = resolve_notify_config(project_dir=cwd)
        reason, resolved = resolution.reason, resolution.handler
    except Exception as exc:  # noqa: BLE001 — a settings problem must never break the terminal path
        return None, f"notify_settings_unreadable:{type(exc).__name__}"
    if reason is not None:
        return None, reason  # misconfigured notifier, not an absent one
    if resolved is None:
        return None, None  # no notifier configured — silence by choice
    if resolved.argv is None:
        return None, "configured_notifier_has_no_delivery_command"  # not an exec adapter
    return list(resolved.argv), None


def _substitute(argv: list[str], fields: dict[str, str]) -> list[str]:
    """Replace ``{run_id}``/``{status}``/``{label}``/``{target}``/``{sender}`` per token."""
    out: list[str] = []
    for tok in argv:
        for key, value in fields.items():
            tok = tok.replace("{" + key + "}", value)
        out.append(tok)
    return out


def _delivery_env(sender: str) -> dict[str, str] | None:
    """Environment for the delivery command, carrying an explicit sender.

    Publishes the value only; doesn't force a notifier to prefer it over its
    own directory-based resolution (a directory-first notifier needs the
    ``{sender}`` placeholder in its command line instead).
    """
    if not sender:
        return None
    env = dict(os.environ)
    env["LIONAGI_NOTIFY_SENDER"] = sender
    return env


def _resolve_delivery_cwd(
    job: dict[str, Any] | None, override: str | None
) -> tuple[str | None, str | None]:
    """The directory to run the delivery command in, paired with why there is none.

    Same three-way shape as :func:`_resolve_command`, for the same reason: a
    delivery run somewhere other than intended is not the same event as one
    with nowhere named. Order: *override*, then the record's ``submit_cwd``
    (the notice is *about* and *from* the submitting seat; nothing falls back
    to the current directory since the two callers here don't share one). A
    named directory that doesn't exist is a refusal, not a fallback — running
    elsewhere would sign the notice under an identity nobody chose. An
    *absent* ``submit_cwd`` key (pre-field record, inherits as always) and a
    ``submit_cwd`` of ``None`` (this submission tried to capture it and
    couldn't — unavailable, not unasked-for) are different facts; only the
    first inherits.
    """
    if override:
        named: Any = override
    elif job is not None and "submit_cwd" in job:
        named = job["submit_cwd"]
        if not named:
            return None, "delivery_cwd_unavailable_at_submit"
    else:
        named = None
    if not named:
        return None, None
    if not os.path.isdir(named):
        return None, "delivery_cwd_is_not_a_directory"
    return str(named), None


def _unverifiable_reason(argv: list[str]) -> str | None:
    """Why a zero exit from *argv* would not actually mean delivered.

    ``kkernel exec`` returns 0 when *any* op in the request succeeded, so a
    multi-op notify whose send was refused still exits 0 (``--strict`` makes a
    refused op exit 1). Scoped to this one known adapter shape — reading
    someone else's argv is only defensible where the alternative is recording
    a lie, and it stops at marking the outcome; the command still runs exactly
    as configured.
    """
    if not argv:
        return None
    program = os.path.basename(argv[0])
    if program != "kkernel" or "exec" not in argv:
        return None
    if any(tok == "--strict" for tok in argv):
        return None
    return "kkernel_exec_without_strict_exits_zero_on_a_refused_op"


def _deliver(
    argv: list[str],
    payload: dict[str, str],
    env: dict[str, str] | None = None,
    *,
    program: str | None = None,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Run the delivery command best-effort; return its outcome for the record.

    Recorded on the job so a dead completion notice surfaces in ``job_status``
    instead of vanishing silently. stdout/stderr may carry a credential, so
    only the matched ``_FAILURE_CLASSES`` name is stored, in ``failure_class``.
    *program* is the argv template's program token (operator configuration,
    not runtime output). *cwd* is passed explicitly (not inherited) so both
    callers of ``deliver_terminal_notice`` sign the notice the same way.
    """
    try:
        proc = subprocess.run(  # noqa: S603 — argv is the operator-configured delivery command, no shell
            argv,
            input=json.dumps(payload),
            text=True,
            timeout=_DELIVERY_TIMEOUT_S,
            capture_output=True,
            check=False,
            env=env,
            cwd=cwd,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # Only the exception *type* is kept — TimeoutExpired carries the
        # child's captured output on .stdout/.stderr, so str(exc) would leak
        # exactly the free text this function exists to keep out.
        return {
            "attempted": True,
            "ok": False,
            "exit_code": None,
            "error": type(exc).__name__,
            "failure_class": _pin_failure_class(
                _FAILURE_TIMEOUT if isinstance(exc, subprocess.TimeoutExpired) else _FAILURE_UNKNOWN
            ),
            "command": program,
        }
    ok = proc.returncode == 0
    failure_class = _pin_failure_class(
        None if ok else _classify_quietly(f"{proc.stderr or ''}\n{proc.stdout or ''}")
    )
    outcome = {
        "attempted": True,
        "ok": ok,
        "exit_code": proc.returncode,
        "error": None,
        "failure_class": failure_class,
        "command": program,
    }
    unverifiable = _unverifiable_reason(argv) if ok else None
    if unverifiable:
        # Its own state, not folded into "delivered" (a claim this can't
        # support) or "failed" (probably didn't happen).
        outcome["ok"] = True
        outcome["delivery_verified"] = False
        outcome["unverified_reason"] = unverifiable
    return outcome


def _note_delivery_in_console_log(run_id: str, outcome: dict[str, Any]) -> None:
    """Append one line to the run's own log when the notice needs an operator's eye.

    The job record is only seen by someone who queries it; the log is the
    fallback for a notice that never arrived (which otherwise ends silently,
    indistinguishable from a still-working run). Two outcomes qualify: an
    outright failure, and a zero-exit delivery that *could not be verified*
    (worded distinctly from failure, since the notice probably did arrive).
    Every line carries only closed-set names, never anything the command said.
    """
    if not outcome.get("attempted") and not outcome.get("error"):
        return  # nothing was configured; silence is the documented default

    if outcome.get("ok"):
        if outcome.get("delivery_verified") is not False:
            return  # delivered, and the exit code is evidence we trust
        line = (
            f"\n[notify] WARNING: terminal notice for run {run_id} reported success but "
            f"could NOT be verified: {outcome.get('unverified_reason')}. "
            f"The notice may not have been delivered; do not read this run's "
            f"completion signal as confirmed.\n"
        )
    else:
        detail = outcome.get("error") or f"exit code {outcome.get('exit_code')}"
        failure_class = outcome.get("failure_class")
        if failure_class:
            detail = f"{detail} ({failure_class})"
        line = (
            f"\n[notify] terminal notice NOT delivered for run {run_id}: {detail}. "
            f"This run finished; its completion signal did not.\n"
        )

    try:
        path = config.job_dir(run_id) / "console.log"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


def _note_persistence_failure(run_id: str, what: str) -> None:
    """Append one line to the run's own log when a record could not be written.

    Same fallback as a failed delivery, for the same reason: the record is
    exactly what could not be written, so the log is the one place left.
    Best-effort — must not turn a handled refusal into a crash.
    """
    try:
        path = config.job_dir(run_id) / "console.log"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(
                f"\n[notify] could not record the {what} for run {run_id}: the job "
                f"record could not be locked. The record was left unchanged.\n"
            )
    except OSError:
        pass


def deliver_terminal_notice(
    run_id: str,
    job: dict[str, Any] | None,
    status: str,
    *,
    target: str | None = None,
    command: str | None = None,
    sender: str | None = None,
) -> dict[str, Any]:
    """Attempt this run's configured terminal notice and report what came of it.

    Has two callers (the dying run's own hook, and the job observer for runs
    whose process never got this far) that must resolve identically; see
    docs/internals/mcp.md#deliver-terminal-notice-two-callers. Nothing raises —
    every way a delivery does not happen comes back as an outcome describing it.
    """
    target = target or os.environ.get("LIONAGI_MCP_NOTIFY_TARGET") or ""
    sender = sender or os.environ.get("LIONAGI_MCP_NOTIFY_SENDER") or ""
    label = (job or {}).get("label") or (job or {}).get("kind") or "run"
    template, unusable = _resolve_command(
        command or os.environ.get("LIONAGI_MCP_NOTIFY_COMMAND"),
        cwd=(job or {}).get("cwd"),
    )
    # Taken before a missing sender can drop the template, so a refusal still
    # names the program that would have run.
    program = template[0] if template else None
    delivery_cwd, cwd_unusable = _resolve_delivery_cwd(
        job, os.environ.get("LIONAGI_MCP_NOTIFY_CWD")
    )
    # Both checks run so every blocking reason is reported at once, not one
    # round-trip per reason.
    blocking: list[str] = []
    if template:
        if cwd_unusable:
            blocking.append(cwd_unusable)
        if not sender and any("{sender}" in tok for tok in template):
            # A blank sender would still get signed and sent, silently
            # misattributing the notice, so it's treated as unusable.
            blocking.append("delivery_command_needs_a_sender_and_none_was_given")
    if blocking:
        template, unusable = None, ", ".join(blocking)

    if template:
        fields = {
            "run_id": run_id,
            "status": status,
            "label": label,
            "target": target,
            "sender": sender,
        }
        return _deliver(
            _substitute(template, fields),
            fields,
            _delivery_env(sender),
            program=program,
            cwd=delivery_cwd,
        )
    if unusable:
        # Configured but unusable — recorded as a failure, not silence.
        # ``command`` is None when configuration never yielded a program name.
        return {
            "attempted": False,
            "ok": False,
            "exit_code": None,
            "error": unusable,
            "command": program,
        }
    return {"attempted": False}  # nothing configured — not a failure


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="lionagi.mcp._notify_hook")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--status", default="completed")
    ap.add_argument("--target", default=None, help="value for the {target} placeholder")
    ap.add_argument("--command", default=None, help="delivery argv override (JSON list)")
    ap.add_argument(
        "--sender",
        default=None,
        help="value for the {sender} placeholder: who the notice is from",
    )
    args = ap.parse_args(argv)

    terminal = jobs.mark_terminal(args.run_id, args.status)
    if terminal.refused:
        # End not on disk — no notice sent, since it would assert a
        # completion the record contradicts; run stays non-terminal.
        _note_persistence_failure(args.run_id, "terminal status")
        return 1

    outcome = deliver_terminal_notice(
        args.run_id,
        terminal.record,
        args.status,
        target=args.target,
        command=args.command,
        sender=args.sender,
    )
    recorded = jobs.record_notify_delivery(args.run_id, outcome)
    _note_delivery_in_console_log(args.run_id, outcome)
    if recorded.refused:
        # Notice was attempted, but its outcome couldn't be recorded.
        _note_persistence_failure(args.run_id, "delivery result")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
