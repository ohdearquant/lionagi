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

_DELIVERY_TIMEOUT_S = 30

# A configured notifier's stdout/stderr is free text that can carry a credential
# the command obtained anywhere, so none of it is ever stored. It used to be
# discarded at the pipe (DEVNULL), which kept that promise and cost the record
# any way to tell a failure apart: every failed delivery recorded a bare exit
# code and a null error, so "the notifier binary is missing" and "the notifier
# refused the message" were the same row.
#
# The output is now read into memory, matched against the closed vocabulary
# below, and dropped. Only the matched name is stored. That keeps the stored
# field a bounded enum rather than free text, so the invariant holds by
# construction instead of by a promise to sanitise.
_FAILURE_UNKNOWN = "unknown"
# First match wins, so the more specific phrase must precede any broader one it
# contains. "connection refused" sits above the policy class for exactly that
# reason, and the policy class deliberately does NOT match a bare "refused":
# that word appears in network errors far more often than in policy ones, and a
# needle that broad silently reclassifies them. A refusal phrased in a way none
# of these anticipate falls to `unknown`, which is the correct direction to be
# wrong in.
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
)


_FAILURE_TIMEOUT = "timeout"

# The one allowed set, and the reason it is defined here rather than derived at
# each use: this field is persisted, so the guarantee that has to hold is about
# the value that reaches the record, not about what the classifier happens to
# return today. Every name that can ever be stored appears here -- the classified
# ones, ``unknown``, and ``timeout``, which is assigned on the exception path and
# so never passes through the classifier at all.
_ALLOWED_FAILURE_CLASSES: frozenset[str] = frozenset(
    {name for name, _ in _FAILURE_CLASSES} | {_FAILURE_UNKNOWN, _FAILURE_TIMEOUT}
)


def _pin_failure_class(value: str | None) -> str | None:
    """Force a classification into the closed set, at the boundary that stores it.

    ``_classify_failure`` is fail-closed today, and pinning here does not trust
    that it stays so. A later edit that returns part of the command's output --
    the change any diagnostics-minded reader is tempted to make -- would
    otherwise be persisted verbatim and reopen the leak this module exists to
    close. Placing the check immediately before the record is built means the
    invariant is enforced where it is needed rather than where it is produced.

    ``None`` is a real value here and passes through: it means the delivery
    succeeded and there is no failure to name.
    """
    if value is None:
        return None
    return value if value in _ALLOWED_FAILURE_CLASSES else _FAILURE_UNKNOWN


def _classify_failure(text: str) -> str:
    """Map a delivery command's output to one name from the closed set above.

    Fail-closed: anything that does not match is ``unknown``. A fragment of the
    text is never returned, however diagnostic it looks — the moment an
    unmatched case is allowed to contribute its own words, the stored field is
    free text again and the invariant is gone.
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
    that is invisible afterwards.

    An *absent* ``submit_cwd`` key and a ``submit_cwd`` of ``None`` are different
    facts and only one of them is allowed to inherit. Absent means the record
    predates the field, and inheriting is what it always did. Present-and-null
    means this run's own submission tried to record the anchor and could not —
    the server's working directory was gone by the time it asked — so the anchor
    is *unavailable*, not *unasked-for*. Inheriting there would put every job
    submitted after that moment back on the old caller-dependent behaviour and
    say nothing, which is the same silence this function exists to refuse.
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

    Exit code is this hook's only delivery evidence, and for one adapter shape
    we know that evidence is weak: ``kkernel exec`` returns 0 when *any* op in
    the request succeeded, so a multi-op notify whose send was refused still
    exits 0 and records as delivered. ``--strict`` makes a refused op exit 1.

    Scoped to a known adapter shape on purpose. Reading someone else's argv is
    only defensible where the alternative is recording a lie, and it stops at
    marking the outcome — the command still runs exactly as configured, because
    refusing to run what an operator wrote is not this hook's call.
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

    The outcome is recorded on the job so a dead completion notice surfaces in
    ``job_status`` instead of vanishing silently — a completion signal that can
    fail silently would cost the detached-spawn pattern its reliability.

    The command's stdout/stderr is free text that can carry a credential, so
    none of it is stored. It is read, matched against the closed vocabulary in
    ``_FAILURE_CLASSES``, and dropped; only the matched name reaches the record,
    in ``failure_class``. That is what turns a bare exit code into something
    actionable while keeping the stored field a bounded enum by construction.

    *program* is recorded alongside it so the record names which notifier this
    was. It is the program token of the configured argv template, before any run
    field is substituted into it — operator configuration, which whoever wrote it
    can already read, and not something the command produced at runtime.

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
            capture_output=True,
            check=False,
            env=env,
            cwd=cwd,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # Never fail the run's terminal path; record the failure instead.
        #
        # The exception *type* names how the delivery came apart — never
        # started, or ran past the timeout — and nothing else from the
        # exception is kept. This matters more than it looks:
        # subprocess.TimeoutExpired carries the child's captured output on its
        # own .stdout/.stderr attributes, so logging the exception, or storing
        # its str(), would put back exactly the free text this function exists
        # to keep out.
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
    # Classified here and not retained: `proc` goes out of scope with the
    # function, and only the returned name outlives it. Pinned on the way into
    # the record, so what is persisted is a member of the closed set whatever
    # the classifier returned.
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
        # Delivered on the only evidence available, and that evidence is known
        # to be weak for this shape. Recorded as its own state rather than
        # folded into either "delivered" or "failed": calling it delivered
        # records a claim this hook cannot support, and calling it failed
        # reports a failure that probably did not happen.
        outcome["ok"] = True
        outcome["delivery_verified"] = False
        outcome["unverified_reason"] = unverifiable
    return outcome


def _note_delivery_in_console_log(run_id: str, outcome: dict[str, Any]) -> None:
    """Append one line to the run's own log when the notice needs an operator's eye.

    The outcome is already on the job record, but that record is only seen by
    someone who thinks to query it. A run whose notice never arrived is
    indistinguishable, in its log, from one still working: the log simply ends.
    Ending it with a stated failure is what lets the log serve as the fallback
    for a notice that did not.

    Two outcomes qualify, and the second is the reason this is not simply a
    failure note. A delivery that ran, exited zero, and *could not be verified*
    is recorded as such on the job record, but an operator reading the log or a
    job listing would otherwise see an ordinary success. A degraded result that
    only the record knows about is a degraded result nobody acts on, so it gets
    a line of its own -- distinct in wording from a failure, because the notice
    probably did arrive and reporting it as failed would be its own lie.

    Every line carries only names from closed sets: the classified reason, or
    the fixed ``unverified_reason``. Never anything the command said, so the log
    stays as free of the command's own text as the job record is -- a log is if
    anything the easier of the two to read by accident.

    Best-effort like everything else in this hook: the run has already
    finished, and a log that cannot be appended to must not turn a delivered
    outcome into a crash.
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
    # Both identity checks run against the template, and every reason they raise
    # is reported. Stopping at the first would hand an operator one thing to fix
    # and then a second failure on the next run for the other, which is two
    # round-trips to learn what one record could have said. A single reason still
    # reads exactly as it did: one reason joins to itself.
    blocking: list[str] = []
    if template:
        if cwd_unusable:
            # The directory decides which identity the notice carries, so a
            # template that would run in the wrong one is not one this hook can
            # use. Checked before the delivery rather than after it.
            blocking.append(cwd_unusable)
        if not sender and any("{sender}" in tok for tok in template):
            # The command asks who the notice is from and there is no answer. An
            # empty string is not one: it puts a blank where an identity belongs,
            # and a delivery tool that accepts it — or falls back to resolving a
            # sender from its own working directory — signs the notice with a seat
            # that did not send it, silently. Unusable in the same sense as a
            # template that cannot be parsed, and recorded the same way.
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
    _note_delivery_in_console_log(args.run_id, outcome)
    if recorded.refused:
        # The notice was attempted against a durable end; what is missing is the
        # record of how it went. Reported the same way, because a delivery
        # nobody can read back is one an operator has to be told about.
        _note_persistence_failure(args.run_id, "delivery result")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
