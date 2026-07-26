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


def _deliver(
    argv: list[str], payload: dict[str, str], env: dict[str, str] | None = None
) -> dict[str, Any]:
    """Run the delivery command best-effort; return its outcome for the record.

    The outcome is recorded on the job so a dead completion notice surfaces in
    ``job_status`` instead of vanishing silently — a completion signal that can
    fail silently would cost the detached-spawn pattern its reliability. Only
    the exit code is kept: the command's stdout/stderr is free text that can
    carry a credential, so it goes to DEVNULL and is never captured.
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
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # never fail the run's terminal path; record the failure instead
        return {"attempted": True, "ok": False, "exit_code": None, "error": type(exc).__name__}
    return {
        "attempted": True,
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "error": None,
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

    job = jobs.mark_terminal(args.run_id, args.status)

    target = args.target or os.environ.get("LIONAGI_MCP_NOTIFY_TARGET") or ""
    sender = args.sender or os.environ.get("LIONAGI_MCP_NOTIFY_SENDER") or ""
    label = (job or {}).get("label") or (job or {}).get("kind") or "run"
    template, unusable = _resolve_command(
        args.command or os.environ.get("LIONAGI_MCP_NOTIFY_COMMAND"),
        cwd=(job or {}).get("cwd"),
    )
    if template:
        fields = {
            "run_id": args.run_id,
            "status": args.status,
            "label": label,
            "target": target,
            "sender": sender,
        }
        outcome = _deliver(_substitute(template, fields), fields, _delivery_env(sender))
    elif unusable:
        # Configured but unusable. Recorded as a failure so job_status shows a
        # notifier that cannot deliver, rather than the silence of one that was
        # never asked to.
        outcome = {"attempted": False, "ok": False, "exit_code": None, "error": unusable}
    else:
        outcome = {"attempted": False}  # nothing configured — not a failure
    jobs.record_notify_delivery(args.run_id, outcome)
    _note_failure_in_console_log(args.run_id, outcome)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
