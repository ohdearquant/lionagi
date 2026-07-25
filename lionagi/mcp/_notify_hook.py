# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Terminal hook for background MCP jobs, invoked by the CLI via ``--notify``.

The CLI runs this once a background run reaches a terminal status. It does two
things, both best-effort (the run has already finished, so nothing here may
raise into the CLI's terminal path):

1. Records the terminal status on the MCP job record, so ``job_status`` /
   ``jobs_list`` report an authoritative ``completed`` / ``failed`` / ``killed``
   / ``timeout`` instead of only inferring ``exited`` from a gone pid.

2. Delivers a terminal notice through a *configured command*, never a
   hardcoded one. The command comes from (in order) an explicit ``--command``
   override or lionagi's own ``notify.on_terminal`` setting; ``{run_id}``,
   ``{status}``, ``{label}`` and ``{target}`` are substituted into its argv and
   the same fields are also offered as a JSON payload on stdin. With nothing
   configured there is no delivery — the out-of-the-box default is silence.

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
from . import jobs

# A configured notifier's stdout/stderr is free text that can carry a
# credential the command obtained anywhere, so it is never captured or logged:
# the child inherits DEVNULL and this hook keeps nothing.
_DELIVERY_TIMEOUT_S = 30


def _resolve_command(override: str | None, *, cwd: str | None) -> list[str] | None:
    """The delivery argv template, or None when nothing is configured.

    *override* (a JSON argv list) wins outright. Otherwise lionagi's own
    ``notify.on_terminal`` setting is reused as the single delivery-config
    surface — its ``exec`` adapter's argv is the template. Any resolution
    failure yields None (no delivery), never an exception.
    """
    if override:
        try:
            parsed = json.loads(override)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, list) and all(isinstance(tok, str) for tok in parsed) and parsed:
            return parsed
        return None

    try:
        from lionagi.state.lifecycle.notify_settings import resolve_notify_config

        resolved = resolve_notify_config(project_dir=cwd)
    except Exception:  # noqa: BLE001 — a settings problem must never break the terminal path
        return None
    if resolved is None or resolved.argv is None:
        return None
    return list(resolved.argv)


def _substitute(argv: list[str], fields: dict[str, str]) -> list[str]:
    """Replace ``{run_id}``/``{status}``/``{label}``/``{target}`` per token."""
    out: list[str] = []
    for tok in argv:
        for key, value in fields.items():
            tok = tok.replace("{" + key + "}", value)
        out.append(tok)
    return out


def _deliver(argv: list[str], payload: dict[str, str]) -> dict[str, Any]:
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="lionagi.mcp._notify_hook")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--status", default="completed")
    ap.add_argument("--target", default=None, help="value for the {target} placeholder")
    ap.add_argument("--command", default=None, help="delivery argv override (JSON list)")
    args = ap.parse_args(argv)

    job = jobs.mark_terminal(args.run_id, args.status)

    target = args.target or os.environ.get("LIONAGI_MCP_NOTIFY_TARGET") or ""
    label = (job or {}).get("label") or (job or {}).get("kind") or "run"
    template = _resolve_command(
        args.command or os.environ.get("LIONAGI_MCP_NOTIFY_COMMAND"),
        cwd=(job or {}).get("cwd"),
    )
    if template:
        fields = {
            "run_id": args.run_id,
            "status": args.status,
            "label": label,
            "target": target,
        }
        outcome = _deliver(_substitute(template, fields), fields)
    else:
        outcome = {"attempted": False}  # nothing configured — not a failure
    jobs.record_notify_delivery(args.run_id, outcome)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
