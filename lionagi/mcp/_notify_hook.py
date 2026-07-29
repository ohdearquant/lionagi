# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Terminal hook for background MCP jobs, invoked by the CLI via ``--notify``.

The CLI runs this once a background run reaches a terminal status. It does two
things, both best-effort (the run has already finished, so nothing here may
raise into the CLI's terminal path):

1. Records the terminal status on the MCP job record, so ``job.status`` /
   ``job.list`` report an authoritative ``completed`` / ``failed`` / ``killed``
   / ``timeout`` instead of only inferring ``exited`` from a gone pid.

2. Delivers a terminal notice through a *configured command*, never a
   hardcoded one. The command comes from (in order) an explicit ``--command``
   override or lionagi's own ``notify.on_terminal`` setting; ``{run_id}``,
   ``{status}``, ``{label}`` and ``{target}`` are substituted into its argv and
   the same fields are also offered as a JSON payload on stdin. With nothing
   configured there is no delivery — the out-of-the-box default is silence.
   A notifier that *is* configured but cannot be used is recorded as a delivery
   failure with a named reason, so it never passes for that default silence.
   The command runs in the directory the run was *submitted* from, which the
   record carries, because a notifier that resolves its own identity from its
   working directory would otherwise sign the notice as whoever owns the
   directory the run happened to execute in. ``LIONAGI_MCP_NOTIFY_CWD``
   overrides that for a deployment whose notifier wants to be run elsewhere.

The command is run by absolute argv (never through a shell), so a caller wires
whatever notifier they use (a webhook client, a messaging CLI) without this
package knowing anything about it.
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

# A configured notifier's stdout/stderr is free text that can carry a
# credential the command obtained anywhere, so it is never captured or logged:
# the child inherits DEVNULL and this hook keeps nothing.
_DELIVERY_TIMEOUT_S = 30


def _resolve_command(
    override: str | None, *, cwd: str | None
) -> tuple[list[str] | None, str | None]:
    """The delivery argv template, paired with why there is none.

    Returns ``(argv, None)`` when a template resolved, ``(None, None)`` when
    nothing is configured, and ``(None, reason)`` when something *was*
    configured but cannot be used.

    That third case is why this returns a pair. "Nobody asked for a notice" and
    "a notice was asked for and this hook cannot send it" are opposite
    situations, and reporting both as no-delivery makes a broken notifier
    indistinguishable from an unconfigured one — which for a detached run is the
    worst outcome available, because the caller is waiting on a notice that will
    never come and nothing anywhere says so. Silence is only ever correct when
    it was chosen.

    *override* (a JSON argv list) wins outright. Otherwise lionagi's own
    ``notify.on_terminal`` setting is reused as the single delivery-config
    surface — its ``exec`` adapter's argv is the template. Nothing here raises:
    the run has already finished, so a resolution failure is reported through
    the returned reason, never thrown into the CLI's terminal path.
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
        # Read inside the guard too: a settings problem must never break the
        # terminal path, whichever step of the resolution it surfaces from.
        reason, resolved = resolution.reason, resolution.handler
    except Exception as exc:  # noqa: BLE001 — a settings problem must never break the terminal path
        return None, f"notify_settings_unreadable:{type(exc).__name__}"
    if reason is not None:
        # Settings named a notifier and the resolver refused it — a misconfigured
        # notifier, not an absent one. The reason is what tells the two apart.
        return None, reason
    if resolved is None:
        return None, None  # no notifier configured — silence by choice
    if resolved.argv is None:
        # A notifier is configured but is not an exec adapter, so it has no argv
        # this hook can run. Configured-and-unusable, not unconfigured.
        return None, "configured_notifier_has_no_delivery_command"
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

    A notifier that resolves who it is from its working directory reports the
    identity of whoever owns that directory, not the identity of whoever
    submitted the run. That misattribution is silent — the notice arrives, it
    just arrives signed by the wrong seat, and downstream routing rules act on
    the signature. Naming the sender here is the caller's answer to that.

    This publishes the value; it does not force any particular notifier to
    prefer it over its own directory-based resolution. A notifier whose
    identity precedence puts a working-directory config first has to be given
    the sender in its command line, which is what the ``{sender}`` placeholder
    is for.
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

    Returns ``(cwd, None)`` when one resolved, ``(None, None)`` when the record
    does not name one, and ``(None, reason)`` when one was named and cannot be
    used — the same three-way shape as :func:`_resolve_command`, for the same
    reason: a delivery that runs somewhere other than where it was meant to is
    not the same event as one with nowhere named, and reporting both as "no cwd"
    hides the case an operator has to fix.

    The order is *override*, then the run record's ``submit_cwd``. The override
    is the escape hatch for a deployment whose notifier wants to be run somewhere
    other than the submitting seat's directory; ``submit_cwd`` is the default
    because the notice is *about* a run and *from* the seat that submitted it,
    and a directory-anchored notifier run anywhere else signs it as somebody
    else. Nothing falls back to the current directory on purpose: inheriting is
    what the caller happens to have, and the two callers here do not have the
    same one.

    A named directory that is not there is a refusal, not a fallback. Running
    somewhere else instead would deliver the notice under an identity nobody
    chose, which is the outcome this whole path exists to prevent and the one
    that is invisible afterwards. A record with no ``submit_cwd`` is different
    and is not a refusal: it predates the field, and inheriting is what it always
    did.
    """
    named = override or (job or {}).get("submit_cwd")
    if not named:
        return None, None
    if not os.path.isdir(named):
        return None, "delivery_cwd_is_not_a_directory"
    return str(named), None


def _deliver(
    argv: list[str],
    payload: dict[str, str],
    env: dict[str, str] | None = None,
    *,
    program: str | None = None,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Run the delivery command best-effort; return its outcome for the record.

    The outcome is recorded on the job so a dead completion notice surfaces in
    ``job_status`` instead of vanishing silently — a completion signal that can
    fail silently would cost the detached-spawn pattern its reliability. Only
    the exit code is kept: the command's stdout/stderr is free text that can
    carry a credential, so it goes to DEVNULL and is never captured.

    *program* is recorded alongside it so the record names which notifier this
    was. It is the program token of the configured argv template, before any run
    field is substituted into it — operator configuration, which whoever wrote it
    can already read, and not something the command produced at runtime. That is
    the whole difference: it says *what* failed without keeping a byte of what
    the command said, which stays discarded.

    *cwd* is the directory the command runs in, and it is part of the notice's
    content rather than an incidental of the process that sent it: a notifier
    that resolves its own identity from its working directory signs with whoever
    owns that directory. Passed explicitly so both callers of
    ``deliver_terminal_notice`` send a notice signed the same way, which they
    cannot do while each inherits its own.
    """
    try:
        proc = subprocess.run(  # noqa: S603 — argv is the operator-configured delivery command, no shell
            argv,
            input=json.dumps(payload),
            text=True,
            timeout=_DELIVERY_TIMEOUT_S,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            env=env,
            cwd=cwd,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # never fail the run's terminal path; record the failure instead.
        # The exception *type* names how the delivery came apart — never started,
        # or ran past the timeout — and the exception's message is left out: it
        # is the one string here whose content this hook does not choose.
        return {
            "attempted": True,
            "ok": False,
            "exit_code": None,
            "error": type(exc).__name__,
            "command": program,
        }
    return {
        "attempted": True,
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "error": None,
        "command": program,
    }


def _note_failure_in_console_log(run_id: str, outcome: dict[str, Any]) -> None:
    """Append one line to the run's own log when a configured delivery failed.

    The outcome is already on the job record, but that record is only seen by
    someone who thinks to query it. A run whose notice never arrived is
    indistinguishable, in its log, from one still working: the log simply ends.
    Ending it with a stated failure is what lets the log serve as the fallback
    for a notice that did not.

    Nothing about *why* it failed is available here by design -- the command's
    output goes to DEVNULL because it is free text that can carry a credential
    -- so the line reports the exit code and no more.

    Best-effort like everything else in this hook: the run has already
    finished, and a log that cannot be appended to must not turn a delivered
    outcome into a crash.
    """
    if not outcome.get("attempted") and not outcome.get("error"):
        return  # nothing was configured; silence is the documented default
    if outcome.get("ok"):
        return
    detail = outcome.get("error") or f"exit code {outcome.get('exit_code')}"
    try:
        path = config.job_dir(run_id) / "console.log"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(
                f"\n[notify] terminal notice NOT delivered for run {run_id}: {detail}. "
                f"This run finished; its completion signal did not.\n"
            )
    except OSError:
        pass


def _note_persistence_failure(run_id: str, what: str) -> None:
    """Append one line to the run's own log when a record could not be written.

    The same fallback as a failed delivery, for the same reason: the record is
    where a failure would ordinarily be reported, and this is the case where the
    record is exactly what could not be written. The log is the one place left,
    and it is the place someone reading a run that stops mid-sentence looks.

    Best-effort in the same way. This runs in the dying process of a run that is
    already over, and a log that cannot be appended to must not turn a refusal
    that was handled into a crash.
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

    The whole of the delivery decision lives here: which command is configured,
    what the run's fields substitute into it, whether a missing sender makes it
    unusable, and how each of those is recorded. It is written as one function
    because it has two callers — this hook, running in the run's own dying
    process, and the job observer that publishes an end for a run whose process
    never got this far — and a notice sent by the second must be the one the
    first would have sent. Two resolution paths would mean two answers to
    "what is configured here", and the run that needs the notice most is the one
    whose own process is not around to be asked.

    The working directory is part of "what is configured here", which is why it
    is taken from the run's record and not from whatever this process happens to
    be sitting in. The two callers never share a directory — the hook runs in the
    run's, the observer in the server's — so a notifier that resolves its own
    identity from where it is run would sign the same notice with a different
    seat depending on which caller got there first, silently, and downstream
    routing acts on that signature. Reading it from the record is what makes the
    two callers agree by construction rather than by coincidence.

    Nothing raises: the caller is either a terminal path that has already
    finished or a read that has already published a durable end, and neither can
    be failed by a notifier. Every way a delivery does not happen comes back as
    an outcome describing it.
    """
    target = target or os.environ.get("LIONAGI_MCP_NOTIFY_TARGET") or ""
    sender = sender or os.environ.get("LIONAGI_MCP_NOTIFY_SENDER") or ""
    label = (job or {}).get("label") or (job or {}).get("kind") or "run"
    template, unusable = _resolve_command(
        command or os.environ.get("LIONAGI_MCP_NOTIFY_COMMAND"),
        cwd=(job or {}).get("cwd"),
    )
    # Taken before the sender check below can drop the template: a notifier
    # refused for want of a sender is one an operator most wants named, and the
    # program token is the only part of it that survives the refusal.
    program = template[0] if template else None
    delivery_cwd, cwd_unusable = _resolve_delivery_cwd(
        job, os.environ.get("LIONAGI_MCP_NOTIFY_CWD")
    )
    if template and cwd_unusable:
        # Recorded the same way an unusable command is, and checked before the
        # delivery rather than after: the directory decides which identity the
        # notice carries, so a template that would run in the wrong one is not a
        # template this hook can use.
        template, unusable = None, cwd_unusable
    if template and not sender and any("{sender}" in tok for tok in template):
        # The command asks who the notice is from and there is no answer. An
        # empty string is not one: it puts a blank where an identity belongs,
        # and a delivery tool that accepts it — or falls back to resolving a
        # sender from its own working directory — signs the notice with a seat
        # that did not send it, silently. Unusable in the same sense as a
        # template that cannot be parsed, and recorded the same way.
        template, unusable = None, "delivery_command_needs_a_sender_and_none_was_given"

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
        # Configured but unusable. Recorded as a failure so job_status shows a
        # notifier that cannot deliver, rather than the silence of one that was
        # never asked to. ``command`` is None when the configuration never
        # yielded a program to name — an unparseable override has no program in
        # it, and saying so beats inventing one.
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
        # The end is not on disk. A notice sent now would assert a completion
        # that every reader of the record contradicts, so it is not sent: the
        # record stays non-terminal and the next observation of this run ends it
        # the way a run whose hook never ran is ended. The refusal is reported
        # where the run's own log and this process's exit status are the two
        # places anything is left to look.
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
    _note_failure_in_console_log(args.run_id, outcome)
    if recorded.refused:
        # The notice was attempted against a durable end; what is missing is the
        # record of how it went. Reported the same way, because a delivery
        # nobody can read back is one an operator has to be told about.
        _note_persistence_failure(args.run_id, "delivery result")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
