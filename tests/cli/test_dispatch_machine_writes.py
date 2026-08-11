# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li dispatch ack/retry/purge --machine` — the write seams.

Exercised against a real store rather than a mocked one. What these seams are
for is telling a caller which of several refusals happened, and every one of
those distinctions lives in the store's state; a mock would be asserting that
the code returns what the test told it to.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

pytest.importorskip("aiosqlite", reason="aiosqlite not installed")

from lionagi.cli.main import main


def _redirect_state_db(monkeypatch, tmp_path: Path) -> Path:
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    return db_path


async def _seed(db_path: Path, **kwargs) -> str:
    from lionagi.dispatch import enqueue_dispatch
    from lionagi.state.db import StateDB

    async with StateDB(db_path) as db:
        return await enqueue_dispatch(db, **kwargs)


async def _row(db_path: Path, dispatch_id: str):
    from lionagi.dispatch import get_dispatch
    from lionagi.state.db import StateDB

    async with StateDB(db_path) as db:
        return await get_dispatch(db, dispatch_id)


async def _force_status(db_path: Path, dispatch_id: str, status: str) -> None:
    """Put a row in a status the ordinary flow would take time to reach."""
    from sqlalchemy import text

    from lionagi.state.db import StateDB

    async with StateDB(db_path) as db, db._tx() as conn:
        await conn.execute(
            text("UPDATE dispatch_outbox SET status = :s WHERE id = :id"),
            {"s": status, "id": dispatch_id},
        )


def _envelope(capfd) -> dict:
    """The one JSON object the machine channel emitted.

    Captured at the file descriptor, not through `capsys`: the channel hands the
    envelope to a duplicate of the real fd 1 precisely so that Python-level
    rebinding cannot put anything else on it, and a Python-level capture
    therefore never sees it.
    """
    out = capfd.readouterr().out
    return json.loads(out)


def _run(argv: list[str], capfd) -> tuple[int, dict]:
    rc = main([*argv, "--machine"])
    return rc, _envelope(capfd)


# ack


def test_ack_applies_and_reports_the_row_acked(monkeypatch, tmp_path, capfd):
    db_path = _redirect_state_db(monkeypatch, tmp_path)
    dispatch_id = asyncio.run(
        _seed(db_path, kind="terminal_notify", deliver_to="seat-1", ack_required=True)
    )
    token = asyncio.run(_row(db_path, dispatch_id))["ack_token"]

    rc, envelope = _run(["dispatch", "ack", dispatch_id, token], capfd)

    assert rc == 0
    assert envelope["ok"] is True
    assert envelope["data"] == {
        "dispatch_id": dispatch_id,
        "acked": True,
        "status": "acked",
        "idempotent": True,
    }
    assert asyncio.run(_row(db_path, dispatch_id))["status"] == "acked"


def test_ack_with_a_wrong_token_is_invalid_input_and_does_not_leak_the_real_one(
    monkeypatch, tmp_path, capfd
):
    db_path = _redirect_state_db(monkeypatch, tmp_path)
    dispatch_id = asyncio.run(
        _seed(db_path, kind="terminal_notify", deliver_to="seat-1", ack_required=True)
    )
    real_token = asyncio.run(_row(db_path, dispatch_id))["ack_token"]

    rc, envelope = _run(["dispatch", "ack", dispatch_id, "not-the-token"], capfd)

    assert rc == 0, "the envelope is the answer; the exit status is transport only"
    assert envelope["ok"] is False
    # The caller's argument was wrong, which is input rather than row state.
    assert envelope["error"]["kind"] == "invalid_input"
    # An error that hands over the token would turn a failed attempt into a
    # successful one, so the whole envelope is searched, not just the message.
    assert real_token not in json.dumps(envelope)
    assert asyncio.run(_row(db_path, dispatch_id))["status"] != "acked"


def test_ack_on_a_row_that_does_not_require_one_is_a_conflict(monkeypatch, tmp_path, capfd):
    db_path = _redirect_state_db(monkeypatch, tmp_path)
    dispatch_id = asyncio.run(
        _seed(db_path, kind="terminal_notify", deliver_to="seat-1", ack_required=False)
    )

    rc, envelope = _run(["dispatch", "ack", dispatch_id, "anything"], capfd)

    assert envelope["ok"] is False
    # State, not input: the argument is fine and the row is the wrong shape for it.
    assert envelope["error"]["kind"] == "conflict"
    assert envelope["error"]["detail"]["outcome"] == "not_ack_required"


def test_ack_of_an_unknown_id_is_not_found(monkeypatch, tmp_path, capfd):
    _redirect_state_db(monkeypatch, tmp_path)
    asyncio.run(_seed(tmp_path / "state.db", kind="terminal_notify", deliver_to="seat-1"))

    rc, envelope = _run(["dispatch", "ack", "no-such-dispatch", "token"], capfd)

    assert envelope["ok"] is False
    assert envelope["error"]["kind"] == "not_found"


def test_ack_reports_conflict_not_success_when_the_row_moved_under_it(monkeypatch, tmp_path, capfd):
    """The unsafe direction, asserted.

    A lost race must not arrive as ok=true carrying acked=false: a caller that
    branches on `ok` alone would read "the row moved under me" as "done". The
    transition is refused because the row is no longer in the status it was read
    in, which is simulated here by moving it between the read and the write.
    """
    db_path = _redirect_state_db(monkeypatch, tmp_path)
    dispatch_id = asyncio.run(
        _seed(db_path, kind="terminal_notify", deliver_to="seat-1", ack_required=True)
    )
    token = asyncio.run(_row(db_path, dispatch_id))["ack_token"]

    from lionagi.dispatch import outbox

    real_transition = outbox.transition

    async def moving_transition(db, request, **kwargs):
        # Between the library's read and its guarded write, the row leaves the
        # status the request was built against.
        await _force_status(db_path, dispatch_id, "expired")
        return await real_transition(db, request, **kwargs)

    monkeypatch.setattr(outbox, "transition", moving_transition)
    rc, envelope = _run(["dispatch", "ack", dispatch_id, token], capfd)

    assert envelope["ok"] is False, "a lost race reported as ok would be read as success"
    assert envelope["error"]["kind"] == "conflict"
    assert envelope["error"]["detail"]["outcome"] == "status_changed"
    assert envelope["error"]["detail"]["status"] == "expired"


@pytest.mark.parametrize("verb", ["ack", "retry"])
def test_a_lost_race_on_a_row_that_is_then_deleted_is_not_found_not_a_null_conflict(
    monkeypatch, tmp_path, capfd, verb
):
    """Two things happened to the row, and the answer has to name the later one.

    The transition is refused because the row moved, and by the time the outcome
    is re-read the row is gone. Reporting `status_changed` with a null status
    gives one value two meanings: a caller cannot tell a surviving conflicting
    row from a deleted one, and null is also what a genuinely absent status would
    look like. The exception path already answers `not_found` for this state; this
    is the same state reached through the guarded write.
    """
    db_path = _redirect_state_db(monkeypatch, tmp_path)
    dispatch_id = asyncio.run(
        _seed(db_path, kind="terminal_notify", deliver_to="seat-1", ack_required=True)
    )
    token = asyncio.run(_row(db_path, dispatch_id))["ack_token"]
    if verb == "retry":
        asyncio.run(_force_status(db_path, dispatch_id, "dead_letter"))

    from lionagi.dispatch import outbox, purge_dispatch
    from lionagi.state.db import StateDB

    real_transition = outbox.transition

    async def vanishing_transition(db, request, **kwargs):
        # The row is deleted after the library read it and before its guarded
        # write lands, so the write matches nothing and the re-read finds nothing.
        # A real purge rather than a stubbed absence, so `get_dispatch` is
        # answering about a store that genuinely no longer holds the row.
        async with StateDB(db_path) as other:
            await purge_dispatch(other, dispatch_id)
        return await real_transition(db, request, **kwargs)

    monkeypatch.setattr(outbox, "transition", vanishing_transition)
    argv = ["dispatch", verb, dispatch_id] + ([token] if verb == "ack" else [])
    rc, envelope = _run(argv, capfd)

    assert envelope["ok"] is False
    assert envelope["error"]["kind"] == "not_found", (
        f"a deleted row was reported as {envelope['error']['kind']}: {envelope['error']}"
    )
    assert envelope["error"]["detail"] is None or "status" not in (
        envelope["error"]["detail"] or {}
    ), "an absent row was given a status field"


# retry


def test_retry_requeues_a_dead_letter_row(monkeypatch, tmp_path, capfd):
    db_path = _redirect_state_db(monkeypatch, tmp_path)
    dispatch_id = asyncio.run(_seed(db_path, kind="terminal_notify", deliver_to="seat-1"))
    asyncio.run(_force_status(db_path, dispatch_id, "dead_letter"))

    rc, envelope = _run(["dispatch", "retry", dispatch_id], capfd)

    assert envelope["ok"] is True
    assert envelope["data"]["requeued"] is True
    assert envelope["data"]["status"] == "pending"
    assert envelope["data"]["attempt"] == 0
    assert asyncio.run(_row(db_path, dispatch_id))["status"] == "pending"


def test_retry_of_a_pending_row_is_a_conflict_naming_the_status(monkeypatch, tmp_path, capfd):
    db_path = _redirect_state_db(monkeypatch, tmp_path)
    dispatch_id = asyncio.run(_seed(db_path, kind="terminal_notify", deliver_to="seat-1"))

    rc, envelope = _run(["dispatch", "retry", dispatch_id], capfd)

    assert envelope["ok"] is False
    assert envelope["error"]["kind"] == "conflict"
    assert envelope["error"]["detail"]["outcome"] == "not_retryable"
    assert envelope["error"]["detail"]["status"] == "pending"


def test_retry_of_an_unknown_id_is_not_found(monkeypatch, tmp_path, capfd):
    _redirect_state_db(monkeypatch, tmp_path)
    asyncio.run(_seed(tmp_path / "state.db", kind="terminal_notify", deliver_to="seat-1"))

    rc, envelope = _run(["dispatch", "retry", "no-such-dispatch"], capfd)

    assert envelope["ok"] is False
    assert envelope["error"]["kind"] == "not_found"


# purge


def test_purge_deletes_the_row_and_reports_the_status_it_had(monkeypatch, tmp_path, capfd):
    db_path = _redirect_state_db(monkeypatch, tmp_path)
    dispatch_id = asyncio.run(_seed(db_path, kind="terminal_notify", deliver_to="seat-1"))

    rc, envelope = _run(["dispatch", "purge", dispatch_id], capfd)

    assert envelope["ok"] is True
    assert envelope["data"]["purged"] is True
    # Read before the delete: afterwards there is nothing left to ask.
    assert envelope["data"]["status"] == "pending"
    assert asyncio.run(_row(db_path, dispatch_id)) is None


def test_purge_dry_run_previews_without_deleting(monkeypatch, tmp_path, capfd):
    db_path = _redirect_state_db(monkeypatch, tmp_path)
    dispatch_id = asyncio.run(_seed(db_path, kind="terminal_notify", deliver_to="seat-1"))

    rc, envelope = _run(["dispatch", "purge", dispatch_id, "--dry-run"], capfd)

    assert envelope["ok"] is True
    assert envelope["data"]["purged"] is False
    assert envelope["data"]["would_purge"] is True
    assert asyncio.run(_row(db_path, dispatch_id)) is not None

    async def _events():
        from lionagi.state.db import StateDB

        async with StateDB(db_path) as db:
            return await db.list_admin_events(action="dispatch_purge", target_id=dispatch_id)

    events = asyncio.run(_events())
    assert len(events) == 1, "the preview is audited, same as the human path"
    assert json.loads(events[0]["details"])["dry_run"] is True


def test_purge_of_an_unknown_id_is_not_found(monkeypatch, tmp_path, capfd):
    _redirect_state_db(monkeypatch, tmp_path)
    asyncio.run(_seed(tmp_path / "state.db", kind="terminal_notify", deliver_to="seat-1"))

    rc, envelope = _run(["dispatch", "purge", "no-such-dispatch"], capfd)

    assert envelope["ok"] is False
    assert envelope["error"]["kind"] == "not_found"


@pytest.mark.parametrize(
    "criteria",
    [["--status", "delivered"], ["--before", "1"], ["--status", "delivered", "--before", "1"]],
    ids=["status", "before", "both"],
)
def test_bulk_purge_is_refused_by_name_rather_than_silently_narrowed(
    monkeypatch, tmp_path, capfd, criteria
):
    """Passing criteria must not quietly do something else.

    The dangerous shape is accepting the call and purging nothing, or purging one
    row, while the caller believes a sweep ran. The refusal names the parameters
    so a caller learns their request did not apply.
    """
    db_path = _redirect_state_db(monkeypatch, tmp_path)
    dispatch_id = asyncio.run(_seed(db_path, kind="terminal_notify", deliver_to="seat-1"))
    asyncio.run(_force_status(db_path, dispatch_id, "delivered"))

    rc, envelope = _run(["dispatch", "purge", *criteria], capfd)

    assert envelope["ok"] is False
    assert envelope["error"]["kind"] == "unavailable"
    assert "--status" in envelope["error"]["message"]
    assert asyncio.run(_row(db_path, dispatch_id)) is not None, "nothing was deleted"


def test_purge_with_no_id_and_no_criteria_is_invalid_input(monkeypatch, tmp_path, capfd):
    _redirect_state_db(monkeypatch, tmp_path)
    rc, envelope = _run(["dispatch", "purge"], capfd)
    assert envelope["ok"] is False
    assert envelope["error"]["kind"] == "invalid_input"


# the store itself


def test_a_write_against_a_store_that_does_not_exist_is_not_found(monkeypatch, tmp_path, capfd):
    """Definitive, and distinguishable from a store that would not open.

    With no store there is no such row, which `not_found` says exactly. The other
    case — a store that exists and refuses — says nothing about what it holds and
    must not arrive as the same answer.
    """
    _redirect_state_db(monkeypatch, tmp_path)  # nothing seeded, so nothing created
    rc, envelope = _run(["dispatch", "ack", "any-id", "any-token"], capfd)

    assert envelope["ok"] is False
    assert envelope["error"]["kind"] == "not_found"
    assert "does not exist" in envelope["error"]["message"]


def test_every_dispatch_subcommand_now_answers_on_the_machine_channel():
    """No subcommand is left declared seamless.

    The catalog reads this: a subcommand in `without_seam` is reported to a
    caller as considered-and-unavailable, and one that has since gained a seam
    would keep being reported that way.
    """
    from lionagi.cli import dispatch as dispatch_cli
    from lionagi.cli.machine import MachineError

    for sub in ("ls", "show", "ack", "retry", "purge"):
        try:
            dispatch_cli.machine_result([sub, "--help-nonexistent-arg"])
        except MachineError as exc:
            assert "no machine result" not in str(exc), f"{sub} is still declared seamless"
        except SystemExit:
            pass  # argparse rejecting a bad flag means the handler was reached
