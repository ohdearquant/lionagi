# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""``account.list``: the subscription accounts this machine declares and what its jobs did on them.

One row per account ``accounts.toml`` and the token files declare, as ``resolve_account``
resolves it. A row carries names and counts, never where a credential is kept: no token file
path, keychain item, ``.env`` file or Codex home.

The counts are derived when the verb is called, from two places, and the reply says when it
read them:

- ``slots_held`` and the enforced cap come from the machine-wide admission pools
  (``lionhub.providers._subagent_admission``), which count every CLI agent on the account,
  however it was started.
- ``running``, ``completed_24h`` and ``last_limit`` come from this server's job records, read
  through ``job.status``: an agent job counts for the account its run resolved (its
  ``run.json``) or, before the run has written one, the account its submission asked for.
  Records written before jobs carried an account have none and count for no account; nothing
  is backfilled.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from lionagi.ln import now_utc
from lionhub.agent.subscription_accounts import list_accounts
from lionhub.providers._subagent_admission import subagent_admission_status

from . import jobs

__all__ = ("COMPLETED_WINDOW_HOURS", "account_list")

#: How far back ``completed_24h`` counts, in hours.
COMPLETED_WINDOW_HOURS = 24

#: How a Codex usage-limit refusal reaches a run record: the provider error layer types it
#: ``ProviderQuotaError`` and the run's ``error`` field starts with that class name.
_CODEX_LIMIT_PREFIX = "ProviderQuotaError:"
#: The reset time Codex states, in its own words; the text carries no time zone.
_CODEX_RESET = re.compile(r"try again at (?P<at>.+?)\.?\s*$")

#: Why a row's ``last_limit`` is null.
LIMIT_NONE_IN_JOB_RECORDS = "none_in_job_records"
LIMIT_NOT_RECORDED = "not_recorded"

_HISTORY_NOTE = (
    "Counts come from this server's job records. A record written before jobs carried an "
    "account names none and counts for no account; nothing is backfilled."
)


def _parse_instant(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _job_account(st: dict[str, Any]) -> tuple[Any, Any]:
    """The account a job counts for: what its run resolved, else what was asked for."""
    run = st.get("run") if isinstance(st.get("run"), dict) else {}
    if isinstance(run.get("account"), str) and isinstance(run.get("account_provider"), str):
        return run["account"], run["account_provider"]
    return st.get("account"), st.get("account_provider")


def _codex_limit(st: dict[str, Any]) -> dict[str, Any] | None:
    run = st.get("run") if isinstance(st.get("run"), dict) else {}
    error = run.get("error")
    if not isinstance(error, str) or not error.startswith(_CODEX_LIMIT_PREFIX):
        return None
    reset = _CODEX_RESET.search(error)
    return {
        "run_id": st.get("run_id"),
        "at": st.get("finished_at"),
        "reset_text": reset.group("at") if reset else None,
    }


def _slots(status: dict[str, Any], provider: str, name: str) -> tuple[int | None, int | None]:
    entry = status.get("pools", {}).get(f"account.{provider}.{name}")
    if entry is None:
        return 0, None
    if "error" in entry:
        return None, None
    return entry.get("held"), entry.get("cap") or None


def account_list() -> dict[str, Any]:
    """The ``account.list`` reply. See the module docstring for where each field comes from.

    Raises:
        SubscriptionAccountError: the accounts directory setting or ``accounts.toml`` cannot
            be read.
    """
    accounts, unresolved = list_accounts()
    read_at = now_utc()
    since = read_at - timedelta(hours=COMPLETED_WINDOW_HOURS)

    try:
        admission = subagent_admission_status()
        slots_error = admission.get("error")
    except OSError as exc:
        admission, slots_error = {}, type(exc).__name__

    running: dict[tuple[str, str], int] = {}
    completed: dict[tuple[str, str], int] = {}
    limits: dict[tuple[str, str], dict[str, Any]] = {}
    unattributed_running = 0
    for run_id in jobs.job_run_ids():
        st = jobs.status(run_id)
        if st.get("kind") != "agent":
            continue
        name, provider = _job_account(st)
        if not isinstance(name, str):
            continue
        if not isinstance(provider, str):
            # Asked for by name, and its run has not yet said which provider's account it is.
            unattributed_running += 0 if st.get("terminal") else 1
            continue
        key = (provider, name)
        if not st.get("terminal"):
            running[key] = running.get(key, 0) + 1
            continue
        finished = _parse_instant(st.get("finished_at"))
        if finished is not None and finished >= since:
            completed[key] = completed.get(key, 0) + 1
        if provider == "codex":
            limit = _codex_limit(st)
            if limit is not None and finished is not None:
                newest = limits.get(key)
                if newest is None or finished > _parse_instant(newest["at"]):
                    limits[key] = limit

    rows = []
    for account in accounts:
        key = (account.provider, account.name)
        held, enforced_cap = (
            (None, None) if slots_error else _slots(admission, account.provider, account.name)
        )
        limit = limits.get(key)
        if account.provider == "codex":
            reason = None if limit is not None else LIMIT_NONE_IN_JOB_RECORDS
        else:
            # No refusal of this provider has been captured, so none is classified.
            reason = LIMIT_NOT_RECORDED
        rows.append(
            {
                "name": account.name,
                "provider": account.provider,
                "source": account.source,
                "max_concurrent": account.max_concurrent,
                "slots_held": held,
                "enforced_cap": enforced_cap,
                "running": running.get(key, 0),
                "completed_24h": completed.get(key, 0),
                "last_limit": limit,
                "last_limit_reason": reason,
            }
        )
    return {
        "read_at": read_at.isoformat(),
        "window_hours": COMPLETED_WINDOW_HOURS,
        "accounts": rows,
        "unresolved": [{"provider": p, "name": n} for p, n in unresolved],
        "unattributed_running": unattributed_running,
        "slots_error": slots_error,
        "history": _HISTORY_NOTE,
    }
