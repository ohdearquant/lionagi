# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li schedule ... --machine` — the schedule family's machine results.

The envelope contract lives in :mod:`lionagi.cli.machine`; this module only
decides what each schedule subcommand's payload says. See docs/internals/cli.md
for the three rules shaping every payload (parser reuse, mutation-reports-only-
what-landed, and explicit `unavailable` when the schedule store is unreachable).
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from .machine import MachineError, available, unavailable

__all__ = ("dispatch_schedule",)

# A control-plane read against a local Studio. Longer than this is a Studio that
# has stopped answering rather than one still working, and the MCP layer's own
# timeout sits above it.
STUDIO_TIMEOUT_SECONDS = 15.0

# `li schedule runs --limit` is bounded by the route itself (1-200); refusing
# here names the bound instead of returning the API's own 422 body.
_RUNS_LIMIT_MIN = 1
_RUNS_LIMIT_MAX = 200


# ── the Studio HTTP client ───────────────────────────────────────────────────


def _studio_url() -> str:
    from lionagi.studio.cli import _base_url

    return _base_url()


def _studio(path: str, method: str = "GET", body: dict[str, Any] | None = None) -> Any:
    """One call to the Studio schedules API, or a refusal that says which kind.

    The human client collapses every failure into "returned nothing"; a machine
    caller has to tell a schedule that does not exist from a Studio that is not
    running, because the first is an answer and the second means it learned
    nothing at all.
    """
    import urllib.error
    import urllib.request

    url = f"{_studio_url()}/api/schedules{path}"
    data = json.dumps(body).encode() if body is not None else None
    declares_json = data is not None or method.upper() not in {"GET", "HEAD", "OPTIONS"}
    request = urllib.request.Request(  # noqa: S310 — fixed http(s) Studio base URL
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if declares_json else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=STUDIO_TIMEOUT_SECONDS) as response:  # noqa: S310
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:2000]
        raise MachineError(_error_kind(exc.code), _http_message(exc.code, detail)) from exc
    except OSError as exc:
        raise MachineError(
            "unavailable",
            f"could not reach Studio at {_studio_url()}: {exc}. The schedule store is "
            "served by `li studio`; nothing was read or written.",
        ) from exc
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MachineError(
            "internal", f"Studio answered {url} with something other than JSON: {exc}"
        ) from exc


def _error_kind(code: int) -> str:
    if code == 404:
        return "not_found"
    if code == 409:
        return "conflict"
    if 400 <= code < 500:
        return "invalid_input"
    return "internal"


def _http_message(code: int, detail: str) -> str:
    try:
        parsed = json.loads(detail)
        if isinstance(parsed, dict) and isinstance(parsed.get("detail"), str):
            detail = parsed["detail"]
    except json.JSONDecodeError:
        pass
    return f"Studio refused with HTTP {code}: {detail}"


# ── argv, parsed by the parser the CLI itself builds ─────────────────────────


def _schedule_subparsers() -> dict[str, argparse.ArgumentParser]:
    from lionagi.studio.cli import add_schedule_subparser

    root = argparse.ArgumentParser(prog="li", add_help=False)
    schedule = add_schedule_subparser(root.add_subparsers(dest="command"))
    for action in schedule._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    raise MachineError("internal", "the schedule parser declares no subcommands")


def _parse(name: str, argv: list[str], *, unhonoured: dict[str, str]) -> argparse.Namespace:
    """Parse *argv* with the real subcommand parser, refusing what is not acted on.

    argparse reports a bad invocation by printing usage and exiting, which would
    end the process without an envelope, so its exit is caught and restated as a
    refusal a caller can read.
    """
    parsers = _schedule_subparsers()
    parser = parsers.get(name)
    if parser is None:
        raise MachineError(
            "invalid_input", f"no such schedule subcommand: {name} (one of {sorted(parsers)})"
        )
    try:
        known, extras = parser.parse_known_args(argv)
    except SystemExit as exc:
        raise MachineError(
            "invalid_input", f"`li schedule {name}` could not parse its arguments"
        ) from exc
    if extras:
        raise MachineError("invalid_input", f"unrecognized arguments: {' '.join(extras)}")
    for dest, reason in unhonoured.items():
        value = getattr(known, dest, None)
        if value:
            raise MachineError(
                "invalid_input",
                f"`--{dest.replace('_', '-')}` has no effect on the machine result of "
                f"`li schedule {name}`: {reason}",
            )
    return known


def _absolute(path_value: str, label: str) -> Path:
    """A caller-supplied path, refused unless it is absolute.

    A relative path resolves against this process's working directory, which for
    a dispatched call is wherever the server was started — never where the
    caller thinks it is.
    """
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        raise MachineError(
            "invalid_input",
            f"{label} must be an absolute path, got {path_value!r}: a relative one would "
            "resolve against this command's own working directory, not the caller's",
        )
    return path


# ── the resolved next fire ───────────────────────────────────────────────────


def _resolved_next_fire(row: dict[str, Any]) -> dict[str, Any]:
    """When the trigger in *row* next fires, through the scheduler's resolver.

    Computed rather than read back: a freshly written row carries no
    ``next_fire_at`` until the running scheduler recomputes it, and the value a
    caller needs is the one its own expression resolves to. Cron fields are read
    in the scheduler's configured timezone, so a caller that meant 09:00 local
    can see whether it got 09:00 local.
    """
    try:
        from lionagi.studio.scheduler.engine import resolve_schedule_timezone, scheduler
    except Exception as exc:  # noqa: BLE001 — an unimportable resolver is not a create failure
        return unavailable("unresolved", f"the scheduler's resolver is unavailable here: {exc}")

    epoch = scheduler._compute_next_fire(row, time.time())
    if epoch is None:
        return unavailable(
            "unresolved",
            f"the scheduler's resolver returned no next occurrence for trigger_type "
            f"{row.get('trigger_type')!r}",
        )
    # The row's own zone if it declares one, else the configured default, with
    # an unloadable name falling back to UTC rather than raising. Asking the
    # scheduler's resolver for that is what keeps the zone reported here the
    # same one the fire is actually computed in.
    zone = resolve_schedule_timezone(row)
    from datetime import datetime

    return available(
        {
            "epoch": epoch,
            "rfc3339": datetime.fromtimestamp(epoch, tz=zone.tzinfo).isoformat(timespec="seconds"),
            "timezone": zone.name,
        }
    )


# ── read subcommands ─────────────────────────────────────────────────────────


def _list(argv: list[str]) -> dict[str, Any]:
    _parse("list", argv, unhonoured={})
    result = _studio("/") or {}
    schedules = result.get("schedules") or []
    return {"schedules": schedules, "count": len(schedules)}


def _get(argv: list[str]) -> dict[str, Any]:
    known = _parse("get", argv, unhonoured={})
    return {"schedule": _studio(f"/{_quote(known.id)}")}


def _status(argv: list[str]) -> dict[str, Any]:
    known = _parse(
        "status",
        argv,
        unhonoured={
            "wait": (
                "it blocks for as long as the run takes, which outlives the caller's "
                "call; poll this verb instead"
            ),
            "as_json": "the machine result is already the only thing written to stdout",
        },
    )
    return {"status": _studio(f"/{_quote(known.id)}/status")}


def _runs(argv: list[str]) -> dict[str, Any]:
    known = _parse(
        "runs",
        argv,
        unhonoured={"as_json": "the machine result is already the only thing written to stdout"},
    )
    if not _RUNS_LIMIT_MIN <= known.limit <= _RUNS_LIMIT_MAX:
        raise MachineError(
            "invalid_input",
            f"--limit must be between {_RUNS_LIMIT_MIN} and {_RUNS_LIMIT_MAX}, got {known.limit}",
        )
    path = f"/{_quote(known.id)}/runs?limit={known.limit}"
    for status in known.status or ():
        path += f"&status={_quote(status)}"
    result = _studio(path) or {}
    return {
        "schedule_id": known.id,
        "runs": result.get("runs") or [],
        "limit": result.get("limit"),
        "offset": result.get("offset"),
        "has_next": result.get("has_next"),
    }


def _limits(argv: list[str]) -> dict[str, Any]:
    _parse("limits", argv, unhonoured={})
    result = _studio("/limits") or {}
    cap = result.get("max_scheduled_concurrent")
    adhoc_cap = result.get("max_adhoc_concurrent")
    return {
        # Both are reported because the cap alone is ambiguous: the CLI reads a
        # falsy cap as no cap at all, and a caller should not have to know
        # whether that is spelled 0 or null.
        "max_scheduled_concurrent": cap,
        "unlimited": not cap,
        "current_inflight": result.get("current_inflight", 0),
        # The ad-hoc task-worker lane draws from its own independent
        # capacity pool (see MAX_ADHOC_CONCURRENT), additive to the
        # scheduled cap above -- a caller provisioning for the scheduled cap
        # alone would under-provision by this lane's own capacity.
        "max_adhoc_concurrent": adhoc_cap,
        "adhoc_unlimited": not adhoc_cap,
        "current_adhoc_inflight": result.get("current_adhoc_inflight", 0),
    }


def _validate(argv: list[str]) -> dict[str, Any]:
    """Whether a ScheduleSet file resolves. Never touches the database.

    An unreadable or unparseable file is a refusal, not ``valid: false``: the
    question this answers is which of a document's schedules are wrong, and
    there is no document to answer it about.
    """
    known = _parse(
        "validate",
        argv,
        unhonoured={"as_json": "the machine result is already the only thing written to stdout"},
    )
    from lionagi.studio.services.schedule_declaration import (
        ScheduleSetError,
        parse_schedule_set,
        resolve_schedule_set,
    )

    path = _absolute(known.file, "file")
    if not path.is_file():
        raise MachineError("not_found", f"no such ScheduleSet file: {path}")
    try:
        doc = parse_schedule_set(path.read_text(), source=str(path))
    except OSError as exc:
        raise MachineError("invalid_input", f"could not read {path}: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 — the parser raises its own error types
        raise MachineError("invalid_input", f"{path} is not a ScheduleSet document: {exc}") from exc

    owner_key = f"{doc.metadata.project}/{doc.metadata.name}"
    try:
        resolved = resolve_schedule_set(doc, path.resolve().parent)
    except ScheduleSetError as exc:
        return {
            "valid": False,
            "owner_key": owner_key,
            "errors": [{"name": name, "message": message} for name, message in exc.errors],
            "schedules": {},
        }
    return {
        "valid": True,
        "owner_key": owner_key,
        "errors": [],
        "schedules": json.loads(
            json.dumps({n: r.resolved for n, r in resolved.items()}, default=str)
        ),
    }


def _export(argv: list[str]) -> dict[str, Any]:
    """The ScheduleSet document(s) the current rows convert into.

    The documents come back in the result rather than written to a path: this
    process's working directory is not the caller's, so a written file would
    land somewhere the caller cannot name, and the result channel already
    carries the bytes.
    """
    known = _parse(
        "export",
        argv,
        unhonoured={
            "output": (
                "a path here resolves against this command's own working directory; the "
                "documents come back in this result instead"
            ),
            "report": (
                "the conversion report is in this result as structured lines, not as a written file"
            ),
        },
    )
    import asyncio

    from lionagi.state.db import StateDB
    from lionagi.studio.services.schedule_export import (
        build_managed_export_document,
        convert_legacy_rows,
        dump_schedule_set_yaml,
        is_legacy_row,
        is_managed_row,
    )

    async def _collect():
        async with StateDB() as db:
            rows = await db.list_schedules(limit=1_000_000)
            if known.legacy:
                return convert_legacy_rows(
                    [r for r in rows if is_legacy_row(r)],
                    flows_dir=Path.cwd() / "exported-flows",
                    manifest_dir=Path.cwd(),
                )
            return build_managed_export_document([r for r in rows if is_managed_row(r)])

    docs, lines = asyncio.run(_collect())
    report = [
        {"qualified_name": line.qualified_name, "status": line.status, "message": line.message}
        for line in lines
    ]
    return {
        "documents": [
            {"project": doc.metadata.project, "yaml": dump_schedule_set_yaml(doc)} for doc in docs
        ],
        "report": report,
        "blocked_count": sum(1 for line in lines if line.status == "BLOCKED"),
        "ready_count": sum(1 for line in lines if line.status == "READY"),
    }


# ── mutating subcommands ─────────────────────────────────────────────────────


def _create(argv: list[str]) -> dict[str, Any]:
    """A schedule row, written. Enabled and awaiting the scheduler's next tick.

    What a success here entitles a caller to conclude is exactly this: the row
    exists with this id, carrying the fields ``schedule`` reports, and its
    trigger next resolves at ``resolved_next_fire``. It does not mean a
    scheduler is running to fire it.
    """
    from lionagi.studio.cli import build_create_body

    known = _parse("create", argv, unhonoured={})
    if known.cwd:
        _absolute(known.cwd, "--cwd")
    if known.flow_yaml:
        _absolute(known.flow_yaml, "--flow-yaml")

    body, error = build_create_body(known)
    if error is not None:
        raise MachineError("invalid_input", error)
    assert body is not None

    created = _studio("/", method="POST", body=body) or {}
    schedule_id = created.get("id")
    if not isinstance(schedule_id, str):
        raise MachineError("internal", "Studio accepted the schedule without returning its id")

    # Read back rather than echo: what a caller needs to see is the execution
    # root and project that were actually persisted, which this command resolves
    # from its own environment when the caller named neither.
    try:
        row = _studio(f"/{_quote(schedule_id)}")
        persisted = available(row)
    except MachineError as exc:
        persisted = unavailable("unreadable", str(exc))
        row = None

    return {
        "id": schedule_id,
        "name": created.get("name"),
        "created_at": created.get("created_at"),
        "schedule": persisted,
        "resolved_next_fire": _resolved_next_fire(row if row is not None else body),
    }


def _trigger(argv: list[str]) -> dict[str, Any]:
    """A fire, accepted. Not a run that happened.

    Studio hands back a run id before the occurrence row is durably written, so
    the only thing established here is that the fire was admitted and this id
    allocated for it. The run's status is read afterwards with `schedule.status`
    or `schedule.runs`.
    """
    known = _parse(
        "trigger",
        argv,
        unhonoured={
            "wait": (
                "it blocks until the run reaches a terminal status, which outlives the "
                "caller's call; read the outcome with the status or runs verb"
            )
        },
    )
    result = _studio(f"/{_quote(known.id)}/trigger", method="POST") or {}
    run_id = result.get("run_id")
    if not isinstance(run_id, str):
        raise MachineError("internal", "Studio accepted the fire without returning a run id")
    return {"schedule_id": known.id, "run_id": run_id, "fire_accepted": True}


def _enable(argv: list[str]) -> dict[str, Any]:
    known = _parse("enable", argv, unhonoured={})
    _studio(f"/{_quote(known.id)}/enable", method="POST")
    return {"schedule_id": known.id, "enabled": True}


def _disable(argv: list[str]) -> dict[str, Any]:
    known = _parse("disable", argv, unhonoured={})
    _studio(f"/{_quote(known.id)}/disable", method="POST")
    return {"schedule_id": known.id, "enabled": False}


def _delete(argv: list[str]) -> dict[str, Any]:
    known = _parse("delete", argv, unhonoured={})
    _studio(f"/{_quote(known.id)}", method="DELETE")
    return {"schedule_id": known.id, "deleted": True}


def _quote(value: str) -> str:
    from urllib.parse import quote

    return quote(str(value), safe="")


_SUBCOMMANDS = {
    "list": _list,
    "get": _get,
    "status": _status,
    "runs": _runs,
    "limits": _limits,
    "validate": _validate,
    "create": _create,
    "trigger": _trigger,
    "enable": _enable,
    "disable": _disable,
    "delete": _delete,
    "export": _export,
}

# Named here rather than left to fall through, so asking for one says why it is
# absent instead of "no such subcommand" — which would read as a typo.
_WITHOUT_MACHINE_RESULT = {
    "apply": (
        "it writes a whole ScheduleSet atomically and reports a per-row plan; the plan's "
        "shape has not been decided as a machine result yet"
    ),
    "run": (
        "it reports one schedule run, which `li schedule runs` already returns in a machine result"
    ),
}


def dispatch_schedule(argv: list[str]) -> dict[str, Any]:
    """Route one `li schedule <sub> --machine` call to its payload builder."""
    if not argv:
        raise MachineError(
            "invalid_input", f"li schedule takes a subcommand: one of {sorted(_SUBCOMMANDS)}"
        )
    name, rest = argv[0], argv[1:]
    handler = _SUBCOMMANDS.get(name)
    if handler is not None:
        return handler(rest)
    reason = _WITHOUT_MACHINE_RESULT.get(name)
    if reason is not None:
        raise MachineError("unavailable", f"`li schedule {name}` has no machine result: {reason}")
    raise MachineError("invalid_input", f"no such schedule subcommand: {name}")
