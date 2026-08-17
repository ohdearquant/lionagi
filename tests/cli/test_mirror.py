# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for `li mirror` — Claude Code transcript -> StateDB mirror."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lionagi.cli.mirror import (
    _derive_metadata,
    _fallback_project,
    _FileState,
    _first_prompt,
    _Lineage,
    _load_states,
    _one_pass,
    _parse_window,
    _read_new_events,
    _save_states,
    _seed_lineage,
    _since_window,
)
from lionagi.state._mirror_common import SourceLine
from lionagi.state.claude_mirror import (
    _det,
    messages_for_event,
    mirror_session,
    reconcile_session_status,
    session_db_id,
)
from lionagi.state.db import StateDB

SID = "11111111-2222-3333-4444-555555555555"


# Event builders (verified Claude JSONL shapes)


def _user_text(uuid: str, text: str, *, ts: str = "2026-06-20T00:00:00.000Z") -> dict:
    return {
        "type": "user",
        "uuid": uuid,
        "timestamp": ts,
        "sessionId": SID,
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def _assistant(uuid: str, blocks: list[dict], *, ts: str = "2026-06-20T00:00:01.000Z") -> dict:
    return {
        "type": "assistant",
        "uuid": uuid,
        "timestamp": ts,
        "sessionId": SID,
        "message": {"role": "assistant", "model": "claude-opus-4-8", "content": blocks},
    }


def _tool_result(uuid: str, tool_use_id: str, content, *, is_error: bool = False) -> dict:
    return {
        "type": "user",
        "uuid": uuid,
        "timestamp": "2026-06-20T00:00:02.000Z",
        "sessionId": SID,
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": content,
                    "is_error": is_error,
                }
            ],
        },
    }


def _db_content(msg) -> dict:
    c = msg.to_dict(mode="db")["content"]
    return json.loads(c) if isinstance(c, str) else c


# messages_for_event: mapping + ordering + linkage


def test_user_text_maps_to_single_instruction() -> None:
    out = messages_for_event(_user_text("u1", "hello there"), SID, {})
    assert [type(m).__name__ for m in out] == ["Instruction"]


def test_bare_string_user_content_supported() -> None:
    ev = {
        "type": "user",
        "uuid": "u1",
        "timestamp": "2026-06-20T00:00:00Z",
        "sessionId": SID,
        "message": {"role": "user", "content": "plain string content"},
    }
    out = messages_for_event(ev, SID, {})
    assert [type(m).__name__ for m in out] == ["Instruction"]


def test_command_noise_user_text_is_dropped() -> None:
    ev = _user_text("u1", "<command-name>/clear</command-name>")
    assert messages_for_event(ev, SID, {}) == []


def test_meta_event_is_dropped() -> None:
    ev = _user_text("u1", "real text")
    ev["isMeta"] = True
    assert messages_for_event(ev, SID, {}) == []


def test_assistant_text_then_tool_preserves_order() -> None:
    ev = _assistant(
        "a1",
        [
            {"type": "text", "text": "let me check"},
            {"type": "tool_use", "id": "tool_1", "name": "Bash", "input": {"command": "ls"}},
            {"type": "text", "text": "done"},
        ],
    )
    tool_names: dict[str, str] = {}
    out = messages_for_event(ev, SID, tool_names)
    assert [type(m).__name__ for m in out] == [
        "AssistantResponse",
        "ActionRequest",
        "AssistantResponse",
    ]
    # tool_use records its function name for the later tool_result to label.
    assert tool_names["tool_1"] == "Bash"
    # micro-incremented timestamps keep intra-event order stable.
    assert out[0].created_at < out[1].created_at < out[2].created_at


def test_thinking_block_is_skipped() -> None:
    ev = _assistant(
        "a1",
        [{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "answer"}],
    )
    out = messages_for_event(ev, SID, {})
    assert [type(m).__name__ for m in out] == ["AssistantResponse"]


def test_provider_error_marker_is_preserved_from_attested_event() -> None:
    ev = _assistant("a1", [{"type": "text", "text": "Prompt is too long"}])
    ev.update(
        {
            "isApiErrorMessage": True,
            "error": "invalid_request",
            "apiErrorStatus": 400,
            "errorDetails": "Prompt is too long",
        }
    )

    message = messages_for_event(ev, SID, {})[0].to_dict(mode="db")

    assert message["node_metadata"]["mirror_provider_error"] == {
        "error": "invalid_request",
        "status": 400,
    }


def test_action_request_response_linkage() -> None:
    tool_names: dict[str, str] = {}
    req = messages_for_event(
        _assistant("a1", [{"type": "tool_use", "id": "tool_x", "name": "Read", "input": {"p": 1}}]),
        SID,
        tool_names,
    )[0]
    resp = messages_for_event(_tool_result("u2", "tool_x", "file contents"), SID, tool_names)[0]
    assert type(req).__name__ == "ActionRequest"
    assert type(resp).__name__ == "ActionResponse"
    rc = _db_content(resp)
    # The response points back at the request id, with the recovered function name.
    assert rc["action_request_id"] == str(req.id)
    assert rc["function"] == "Read"
    assert rc["output"] == "file contents"


def test_tool_result_error_flag_recorded() -> None:
    out = messages_for_event(_tool_result("u2", "t", "boom", is_error=True), SID, {"t": "Bash"})
    assert _db_content(out[0])["error"] == "error"


def test_tool_result_block_list_flattened() -> None:
    out = messages_for_event(
        _tool_result("u2", "t", [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]),
        SID,
        {"t": "Grep"},
    )
    assert _db_content(out[0])["output"] == "a\nb"


def test_deterministic_ids_are_idempotent() -> None:
    ev = _assistant(
        "a1",
        [
            {"type": "text", "text": "x"},
            {"type": "tool_use", "id": "tool_1", "name": "Bash", "input": {}},
        ],
    )
    ids1 = [m.id for m in messages_for_event(ev, SID, {})]
    ids2 = [m.id for m in messages_for_event(ev, SID, {})]
    assert ids1 == ids2


# mirror_session: idempotent write + status lifecycle


def _conversation() -> list[dict]:
    return [
        _user_text("u1", "do the thing"),
        _assistant(
            "a1",
            [
                {"type": "text", "text": "okay"},
                {"type": "tool_use", "id": "tool_1", "name": "Bash", "input": {"command": "ls"}},
            ],
        ),
        _tool_result("u2", "tool_1", "total 0"),
    ]


@pytest.fixture
def temp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    return db_path


@pytest.mark.asyncio
async def test_mirror_session_creates_rich_session_row(temp_db_path: Path) -> None:
    async with StateDB() as db:
        n = await mirror_session(
            db,
            session_uid=SID,
            events=_conversation(),
            tool_names={},
            project="acme/widget",
            project_source="cwd",
            model="claude-opus-4-8",
            name="do the thing",
            status="running",
        )
        row = await db.get_session(session_db_id(SID))
    assert n > 0
    assert row["status"] == "running"
    assert row["invocation_kind"] == "agent"
    assert row["agent_name"] == "claude-code"
    assert row["project"] == "acme/widget"
    assert row["model"] == "claude-opus-4-8"


@pytest.mark.asyncio
async def test_mirror_session_persists_and_queries_cc_session_id(temp_db_path: Path) -> None:
    async with StateDB() as db:
        await mirror_session(
            db,
            session_uid=SID,
            events=_conversation(),
            tool_names={},
            status="running",
        )
        row = await db.get_session_by_cc_id(SID)
        missing = await db.get_session_by_cc_id("missing-session")

    assert row is not None
    assert row["id"] == session_db_id(SID)
    assert row["cc_session_id"] == SID
    assert missing is None


@pytest.mark.asyncio
async def test_mirror_session_backfills_cc_session_id_on_existing_row(
    temp_db_path: Path,
) -> None:
    sprog = _det(SID, "sprog")
    async with StateDB() as db:
        await db.create_progression(sprog)
        await db.create_session(
            {
                "id": session_db_id(SID),
                "progression_id": sprog,
                "name": "Legacy Claude Code session",
                "status": "running",
            }
        )
        before = await db.get_session(session_db_id(SID))

        await mirror_session(
            db,
            session_uid=SID,
            events=_conversation(),
            tool_names={},
            status="running",
        )
        after = await db.get_session_by_cc_id(SID)

    assert before is not None
    assert before["cc_session_id"] is None
    assert after is not None
    assert after["cc_session_id"] == SID


@pytest.mark.asyncio
async def test_cc_session_id_backfill_does_not_bump_updated_at(
    temp_db_path: Path,
) -> None:
    # The cc_session_id backfill is not activity: it must not move the liveness
    # clock, or a concurrent reconcile_session_status CAS (expected_updated_at)
    # can be silently defeated by this one-time backfill.
    from lionagi.state.reasons import RunReasons

    sprog = _det(SID, "sprog")
    sid = session_db_id(SID)
    async with StateDB() as db:
        await db.create_progression(sprog)
        await db.create_session(
            {
                "id": sid,
                "progression_id": sprog,
                "name": "Legacy Claude Code session",
                "status": "running",
                "updated_at": 1000.0,
                "last_message_at": 1000.0,
            }
        )
        before = await db.get_session(sid)
        expected_updated_at = before["updated_at"]

        # No new events on this pass -- only the one-time cc_session_id backfill
        # runs; a real new message would legitimately bump updated_at via
        # touch_session_activity, which is not what's under test here.
        await mirror_session(
            db,
            session_uid=SID,
            events=[],
            tool_names={},
            status="running",
        )
        after = await db.get_session(sid)
        assert after["cc_session_id"] == SID
        assert after["updated_at"] == expected_updated_at

        # A concurrent reconciler that read updated_at before the backfill must
        # still win its compare-and-set afterward.
        written = await db.update_status(
            "session",
            sid,
            new_status="completed",
            reason_code=RunReasons.COMPLETED_OK,
            expected_statuses={"running"},
            expected_updated_at=expected_updated_at,
        )
    assert written is True


@pytest.mark.asyncio
async def test_mirror_session_preserves_empty_cc_session_id(temp_db_path: Path) -> None:
    sprog = _det(SID, "sprog")
    async with StateDB() as db:
        await db.create_progression(sprog)
        await db.create_session(
            {
                "id": session_db_id(SID),
                "cc_session_id": "",
                "progression_id": sprog,
                "name": "Claude Code session with an empty external id",
                "status": "running",
            }
        )

        await mirror_session(
            db,
            session_uid=SID,
            events=_conversation(),
            tool_names={},
            status="running",
        )
        after = await db.get_session(session_db_id(SID))

    assert after is not None
    assert after["cc_session_id"] == ""


@pytest.mark.asyncio
async def test_mirror_session_is_idempotent(temp_db_path: Path) -> None:
    events = _conversation()
    async with StateDB() as db:
        await mirror_session(db, session_uid=SID, events=events, tool_names={}, status="completed")
        row = await db.get_session(session_db_id(SID))
        first = await db.get_progression(row["progression_id"])
        # Re-run from scratch (fresh tool_names, as after a restart).
        await mirror_session(db, session_uid=SID, events=events, tool_names={}, status="completed")
        second = await db.get_progression(row["progression_id"])
    assert len(first) > 0
    assert first == second  # no duplicate appends


# link_escalation_session: escalation-leg mirror attribution


@pytest.mark.asyncio
async def test_link_escalation_session_overwrites_orphan_attribution(
    temp_db_path: Path,
) -> None:
    """The mirror already created the orphan (cwd-guessed project, first-prompt
    name) before the escalation call site learned the CLI session id — the
    common case, since the mirror polls continuously while the leg is running.
    The link write must override both, and stamp a pointer back to the parent op.
    """
    from lionagi.state.claude_mirror import link_escalation_session

    async with StateDB() as db:
        await mirror_session(
            db,
            session_uid=SID,
            events=_conversation(),
            tool_names={},
            project="gate-runner-4",
            project_source="cwd_dir",
            name="Guidance: |-\n  LION_SYSTEM_MESSAGE",
            status="running",
        )
        before = await db.get_session(session_db_id(SID))
        assert before["run_id"] is None
        assert before["project"] == "gate-runner-4"

        linked = await link_escalation_session(
            db,
            session_uid=SID,
            run_id="run-20260806-abc123",
            name="escalation of gate-runner-4",
            project="acme/widget",
            project_source="escalation_parent",
            parent_op_id="parent-op-1",
        )
        after = await db.get_session(session_db_id(SID))

    assert linked is True
    assert after["run_id"] == "run-20260806-abc123"
    assert after["project"] == "acme/widget"
    assert after["project_source"] == "escalation_parent"
    assert after["name"] == "escalation of gate-runner-4"
    assert after["node_metadata"]["escalated_from_session"] == "parent-op-1"


@pytest.mark.asyncio
async def test_link_engine_child_session_stamps_marker_and_name(
    temp_db_path: Path,
) -> None:
    """An engine-backed actor's CLI transcript is mirrored with a name derived
    from its first prompt (the actor's own system prompt). The link write must
    replace that name and stamp the flat parent marker listings filter on."""
    from lionagi.state.claude_mirror import link_engine_child_session

    async with StateDB() as db:
        await mirror_session(
            db,
            session_uid=SID,
            events=_conversation(),
            tool_names={},
            name="You are the resident Operator for Lion Studio. Be concise",
            status="running",
        )
        linked = await link_engine_child_session(
            db,
            session_uid=SID,
            parent_run_id="parent-canonical-run",
            name="Operator · engine transcript",
        )
        after = await db.get_session(session_db_id(SID))

    assert linked is True
    assert after["name"] == "Operator · engine transcript"
    assert after["node_metadata"]["engine_parent_run_id"] == "parent-canonical-run"


@pytest.mark.asyncio
async def test_link_engine_child_session_missing_row_returns_false(
    temp_db_path: Path,
) -> None:
    """Before the mirror mints the row there is nothing to stamp; the caller
    retries on False rather than treating it as done."""
    from lionagi.state.claude_mirror import link_engine_child_session

    async with StateDB() as db:
        linked = await link_engine_child_session(
            db,
            session_uid=SID,
            parent_run_id="parent-canonical-run",
            name="Operator · engine transcript",
        )
    assert linked is False


@pytest.mark.asyncio
async def test_link_escalation_session_leaves_project_alone_when_run_project_unknown(
    temp_db_path: Path,
) -> None:
    """An unresolved run project is not evidence the mirror's cwd guess is wrong
    — name/run_id/pointer still get linked, but project is left as the mirror's
    best-effort value rather than overwritten with NULL."""
    from lionagi.state.claude_mirror import link_escalation_session

    async with StateDB() as db:
        await mirror_session(
            db,
            session_uid=SID,
            events=_conversation(),
            tool_names={},
            project="gate-runner-4",
            project_source="cwd_dir",
            name="Guidance: |-\n  LION_SYSTEM_MESSAGE",
            status="running",
        )

        linked = await link_escalation_session(
            db,
            session_uid=SID,
            run_id="run-20260806-abc123",
            name="escalation of gate-runner-4",
            project=None,
            project_source=None,
            parent_op_id="parent-op-1",
        )
        after = await db.get_session(session_db_id(SID))

    assert linked is True
    assert after["run_id"] == "run-20260806-abc123"
    assert after["name"] == "escalation of gate-runner-4"
    assert after["project"] == "gate-runner-4"
    assert after["project_source"] == "cwd_dir"


@pytest.mark.asyncio
async def test_link_escalation_session_returns_false_when_row_missing(
    temp_db_path: Path,
) -> None:
    """The escalation call site can learn the CLI session id before the mirror's
    next sweep has even created the row; the caller is expected to retry rather
    than lose the link, so this must report the miss, not raise or fabricate a row."""
    from lionagi.state.claude_mirror import link_escalation_session

    async with StateDB() as db:
        linked = await link_escalation_session(
            db,
            session_uid=SID,
            run_id="run-20260806-abc123",
            name="escalation of gate-runner-4",
            project="acme/widget",
            project_source="escalation_parent",
            parent_op_id="parent-op-1",
        )
        row = await db.get_session(session_db_id(SID))

    assert linked is False
    assert row is None


@pytest.mark.asyncio
async def test_link_escalation_session_survives_a_later_mirror_pass(
    temp_db_path: Path,
) -> None:
    """A later mirror pass replays its own (still cwd/first-prompt-derived, in
    the poller's in-memory _FileState) name/project on every call — mirror_session
    must not let that clobber a link already recorded, since the mirror has no
    way to know the row it is about to rewrite was already linked."""
    from lionagi.state.claude_mirror import link_escalation_session

    async with StateDB() as db:
        await mirror_session(
            db,
            session_uid=SID,
            events=_conversation(),
            tool_names={},
            project="gate-runner-4",
            project_source="cwd_dir",
            name="Guidance: |-\n  LION_SYSTEM_MESSAGE",
            status="running",
        )
        await link_escalation_session(
            db,
            session_uid=SID,
            run_id="run-20260806-abc123",
            name="escalation of gate-runner-4",
            project="acme/widget",
            project_source="escalation_parent",
            parent_op_id="parent-op-1",
        )

        # Next mirror poll pass: same stale in-memory name/project, more events.
        await mirror_session(
            db,
            session_uid=SID,
            events=_conversation(),
            tool_names={},
            project="gate-runner-4",
            project_source="cwd_dir",
            name="Guidance: |-\n  LION_SYSTEM_MESSAGE",
            status="running",
        )
        after = await db.get_session(session_db_id(SID))

    assert after["run_id"] == "run-20260806-abc123"
    assert after["project"] == "acme/widget"
    assert after["name"] == "escalation of gate-runner-4"


@pytest.mark.asyncio
async def test_non_escalation_transcript_gets_ordinary_cwd_attribution(
    temp_db_path: Path,
) -> None:
    """A top-level (non-escalation) CLI leg is never handed to
    link_escalation_session, so its cwd-derived project and first-prompt name
    are the mirror's own, unmodified best-effort attribution."""
    async with StateDB() as db:
        await mirror_session(
            db,
            session_uid=SID,
            events=_conversation(),
            tool_names={},
            project="acme/widget",
            project_source="cwd",
            name="do the thing",
            status="running",
        )
        row = await db.get_session(session_db_id(SID))

    assert row["run_id"] is None
    assert row["project"] == "acme/widget"
    assert row["name"] == "do the thing"
    assert (row.get("node_metadata") or {}).get("escalated_from_session") is None


@pytest.mark.asyncio
async def test_reconcile_flips_running_to_completed_when_idle(temp_db_path: Path) -> None:
    from lionagi.state.reasons import RunReasons

    async with StateDB() as db:
        await mirror_session(
            db, session_uid=SID, events=_conversation(), tool_names={}, status="running"
        )
        before = await db.get_session(session_db_id(SID))
        # Wall-clock well past the last message -> idle -> completed.
        settled = await reconcile_session_status(
            db, SID, now=before["updated_at"] + 10_000, live_window=300
        )
        after = await db.get_session(session_db_id(SID))
    assert before["status"] == "running"
    assert after["status"] == "completed"
    assert after["status_reason_code"] == RunReasons.COMPLETED_OK
    assert settled is True


@pytest.mark.asyncio
async def test_reconcile_marks_idle_provider_refusal_failed(temp_db_path: Path) -> None:
    from lionagi.state.reasons import RunReasons

    refusal = (
        "Prompt is too long · the request is ~225306 tokens (limit 200000). "
        "A single-exchange conversation cannot be compacted; reduce attached "
        "files/tools or start with less context."
    )
    provider_error = _assistant("a1", [{"type": "text", "text": refusal}])
    provider_error.update(
        {
            "isApiErrorMessage": True,
            "error": "invalid_request",
            "apiErrorStatus": 400,
            "errorDetails": refusal,
        }
    )
    events = [_user_text("u1", "do the thing"), provider_error]
    async with StateDB() as db:
        await mirror_session(db, session_uid=SID, events=events, tool_names={}, status="running")
        before = await db.get_session(session_db_id(SID))
        await reconcile_session_status(
            db,
            SID,
            now=before["last_message_at"] + 10_000,
            live_window=300,
        )
        after = await db.get_session(session_db_id(SID))

    assert after["status"] == "failed"
    assert after["status_reason_code"] == RunReasons.FAILED_PROVIDER_NONRETRYABLE
    assert after["status_reason_code"] != RunReasons.COMPLETED_OK


@pytest.mark.parametrize(
    "summary",
    [
        "Done. If you are not logged in, gh will prompt for auth.",
        "The retry helper backs off when a rate limit exceeded response comes back.",
        "The root cause was an invalid api key in the test fixture; it is fixed now.",
        "Deployment is scheduled; try again at 3pm when the quota resets.",
    ],
)
@pytest.mark.asyncio
async def test_reconcile_does_not_classify_successful_assistant_summary(
    temp_db_path: Path, summary: str
) -> None:
    from lionagi.state.reasons import RunReasons

    events = [
        _user_text("u1", "do the thing"),
        _assistant("a1", [{"type": "text", "text": summary}]),
    ]
    async with StateDB() as db:
        await mirror_session(db, session_uid=SID, events=events, tool_names={}, status="running")
        before = await db.get_session(session_db_id(SID))
        await reconcile_session_status(
            db,
            SID,
            now=before["last_message_at"] + 10_000,
            live_window=300,
        )
        after = await db.get_session(session_db_id(SID))

    assert after["status"] == "completed"
    assert after["status_reason_code"] == RunReasons.COMPLETED_OK


@pytest.mark.asyncio
async def test_reconcile_reactivates_completed_when_fresh(temp_db_path: Path) -> None:
    # A mirror session's "completed" is dormant, not terminal: when the transcript
    # resumes, reconcile brings it back to running (green check -> live spinner).
    async with StateDB() as db:
        await mirror_session(
            db, session_uid=SID, events=_conversation(), tool_names={}, status="completed"
        )
        before = await db.get_session(session_db_id(SID))
        # "now" within the live window of the last message -> running.
        settled = await reconcile_session_status(
            db, SID, now=before["updated_at"] + 1, live_window=300
        )
        after = await db.get_session(session_db_id(SID))
    assert before["status"] == "completed"
    assert after["status"] == "running"
    assert settled is False


@pytest.mark.asyncio
async def test_reconcile_reactivation_clears_terminal_stamps(temp_db_path: Path) -> None:
    """A reactivated session is running again: its terminal ended_at and
    duration_ms must not survive the flip, or listings read "running yet
    ended days ago" and elapsed-time surfaces keep growing off the stale
    end mark."""
    async with StateDB() as db:
        await mirror_session(
            db, session_uid=SID, events=_conversation(), tool_names={}, status="running"
        )
        row = await db.get_session(session_db_id(SID))
        lm = row["last_message_at"]
        # Go idle far past the window: terminal write stamps ended_at.
        await reconcile_session_status(db, SID, now=lm + 10_000, live_window=300)
        ended = await db.get_session(session_db_id(SID))
        assert ended["status"] == "completed"
        assert ended["ended_at"] is not None
        # Transcript resumes within the window of the last message.
        await reconcile_session_status(db, SID, now=lm + 1, live_window=300)
        after = await db.get_session(session_db_id(SID))
    assert after["status"] == "running"
    assert after["ended_at"] is None
    assert after["duration_ms"] is None


@pytest.mark.asyncio
async def test_reconcile_idle_session_stays_completed_across_passes(temp_db_path: Path) -> None:
    # The status write bumps updated_at; liveness must key off last_message_at
    # instead. Otherwise a just-completed idle session reads as fresh on the next
    # pass (its own status write moved updated_at) and oscillates back to running.
    async with StateDB() as db:
        await mirror_session(
            db, session_uid=SID, events=_conversation(), tool_names={}, status="running"
        )
        row = await db.get_session(session_db_id(SID))
        lm = row["last_message_at"]
        await reconcile_session_status(db, SID, now=lm + 10_000, live_window=300)
        first = await db.get_session(session_db_id(SID))
        # last_message_at is the liveness clock; the status write must not touch it.
        assert first["last_message_at"] == lm
        # Second pass a moment later must NOT resurrect the still-idle session.
        await reconcile_session_status(db, SID, now=lm + 10_001, live_window=300)
        second = await db.get_session(session_db_id(SID))
    assert first["status"] == "completed"
    assert second["status"] == "completed"


@pytest.mark.asyncio
async def test_reconcile_reactivates_cancelled_terminal_when_fresh(temp_db_path: Path) -> None:
    # A mirror session independently marked "cancelled" (or any non-"completed"
    # terminal status) must reactivate to "running" the same way a dormant
    # "completed" one does, instead of raising the ADR-0035 terminal-status
    # floor as an ordinary, unguarded transition attempt would.
    async with StateDB() as db:
        await mirror_session(
            db, session_uid=SID, events=_conversation(), tool_names={}, status="running"
        )
        before = await db.get_session(session_db_id(SID))
        await db.update_status(
            "session",
            session_db_id(SID),
            new_status="cancelled",
            reason_code="run.cancelled.system",
            source="admin",
            actor="test",
        )
        cancelled = await db.get_session(session_db_id(SID))
        # "now" within the live window of the last message -> reactivate to running.
        await reconcile_session_status(
            db, SID, now=cancelled["last_message_at"] + 1, live_window=300
        )
        after = await db.get_session(session_db_id(SID))
    assert before["status"] == "running"
    assert cancelled["status"] == "cancelled"
    assert after["status"] == "running"


@pytest.mark.asyncio
async def test_one_pass_non_completed_terminal_reactivation_does_not_crash(
    temp_db_path: Path, tmp_path: Path
) -> None:
    # The CLI's per-tail-pass loop must not propagate a TransitionRejectedError
    # out of _one_pass() when a live mirror session sits on a non-"completed"
    # terminal status (e.g. independently marked "failed" or "cancelled").
    root = tmp_path / "projects"
    uid = "dddddddd-0000-0000-0000-00000000000d"
    async with StateDB() as db:
        await mirror_session(
            db, session_uid=uid, events=_conversation(), tool_names={}, status="running"
        )
        await db.update_status(
            "session",
            session_db_id(uid),
            new_status="failed",
            reason_code="run.failed.exception",
            source="admin",
            actor="test",
        )
        _write_session_file(root / "-w-proj" / f"{uid}.jsonl", uid, age_secs=5)
        await _one_pass(db, root, {}, {}, since=None, live_window=300)
        row = await db.get_session(session_db_id(uid))
    assert row["status"] == "running"


@pytest.mark.asyncio
async def test_one_pass_terminal_reactivation_does_not_skip_later_uid_or_lineage(
    temp_db_path: Path, tmp_path: Path
) -> None:
    # A live, non-"completed" terminal session reconciled earlier in the same
    # pass must not abort the loop before it reaches lineage resolution for an
    # unrelated pair of sessions processed in the same pass.
    root = tmp_path / "projects"
    terminal_uid = "eeeeeeee-0000-0000-0000-00000000000e"
    a, b = "aaaaaaaa-1111-0000-0000-000000000001", "bbbbbbbb-1111-0000-0000-000000000002"
    async with StateDB() as db:
        await mirror_session(
            db, session_uid=terminal_uid, events=_conversation(), tool_names={}, status="running"
        )
        await db.update_status(
            "session",
            session_db_id(terminal_uid),
            new_status="cancelled",
            reason_code="run.cancelled.system",
            source="admin",
            actor="test",
        )
        _write_session_file(root / "-w-proj" / f"{terminal_uid}.jsonl", terminal_uid, age_secs=5)
        _write_lineage_file(
            root / "-w-proj" / f"{a}.jsonl",
            [
                _lineage_event(a, "a-1", None, "user", "start the work"),
                _lineage_event(a, "a-leaf", "a-1", "assistant", "done, ending here"),
            ],
        )
        _write_lineage_file(
            root / "-w-proj" / f"{b}.jsonl",
            [
                _lineage_event(b, "b-1", "a-leaf", "user", "continuing from before"),
                _lineage_event(b, "b-2", "b-1", "assistant", "picking it up"),
            ],
        )
        await _one_pass(db, root, {}, {}, since=None, live_window=300)
        terminal_row = await db.get_session(session_db_id(terminal_uid))
        child = await db.get_session(session_db_id(b))
    assert terminal_row["status"] == "running"
    lineage = child["node_metadata"]["lineage"]
    assert lineage["parent_session_uid"] == a


@pytest.mark.asyncio
async def test_mirror_session_repairs_partial_scaffold(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # If branch creation fails after the session row commits, a later pass must
    # repair the missing branch — not skip scaffolding just because the session
    # row now exists. (Idempotent INSERT OR IGNORE scaffold, run every call.)
    events = _conversation()
    branch_id = _det(SID, "branch")
    async with StateDB() as db:
        real_create_branch = db.create_branch

        async def _boom(*_a, **_k):
            raise RuntimeError("branch write failed")

        monkeypatch.setattr(db, "create_branch", _boom)
        with pytest.raises(RuntimeError, match="branch write failed"):
            await mirror_session(
                db, session_uid=SID, events=events, tool_names={}, status="running"
            )
        # Session row committed but the branch is missing — a partial scaffold.
        assert await db.get_session(session_db_id(SID)) is not None
        assert await db.get_branch(branch_id) is None

        # Next pass with the write restored repairs the scaffold.
        monkeypatch.setattr(db, "create_branch", real_create_branch)
        await mirror_session(db, session_uid=SID, events=events, tool_names={}, status="running")
        assert await db.get_branch(branch_id) is not None


@pytest.mark.asyncio
async def test_mirror_session_empty_no_session(temp_db_path: Path) -> None:
    async with StateDB() as db:
        n = await mirror_session(db, session_uid=SID, events=[], tool_names={}, status="running")
        row = await db.get_session(session_db_id(SID))
    assert n == 0
    assert row is None


# watcher passes: session-level liveness across multiple files


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _write_session_file(path: Path, uid: str, *, age_secs: float) -> None:
    """One transcript file for `uid` whose messages are `age_secs` old."""
    ts = _iso(datetime.now(timezone.utc) - timedelta(seconds=age_secs))
    stem = path.stem
    events = [
        {
            "type": "user",
            "uuid": f"{stem}-u",
            "timestamp": ts,
            "sessionId": uid,
            "message": {"role": "user", "content": [{"type": "text", "text": f"prompt {stem}"}]},
        },
        {
            "type": "assistant",
            "uuid": f"{stem}-a",
            "timestamp": ts,
            "sessionId": uid,
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-8",
                "content": [{"type": "text", "text": "ok"}],
            },
        },
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")


@pytest.mark.asyncio
async def test_multifile_session_stays_running_when_any_file_is_fresh(
    temp_db_path: Path, tmp_path: Path
) -> None:
    # A resumed session spans two transcript files sharing one sessionId: one old,
    # one with a recent message. The merged session must read as live (running) —
    # the regression guard against a per-file status decision burying an active one.
    root = tmp_path / "projects"
    uid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    _write_session_file(root / "-work-acme" / "old.jsonl", uid, age_secs=7200)
    _write_session_file(root / "-work-acme" / "fresh.jsonl", uid, age_secs=5)
    async with StateDB() as db:
        await _one_pass(db, root, {}, {}, since=None, live_window=300)
        row = await db.get_session(session_db_id(uid))
    assert row is not None
    assert row["status"] == "running"


@pytest.mark.asyncio
async def test_multifile_session_completes_when_all_files_idle(
    temp_db_path: Path, tmp_path: Path
) -> None:
    root = tmp_path / "projects"
    uid = "11112222-3333-4444-5555-666677778888"
    _write_session_file(root / "-work-acme" / "a.jsonl", uid, age_secs=7200)
    _write_session_file(root / "-work-acme" / "b.jsonl", uid, age_secs=3600)
    async with StateDB() as db:
        await _one_pass(db, root, {}, {}, since=None, live_window=300)
        row = await db.get_session(session_db_id(uid))
    assert row is not None
    assert row["status"] == "completed"


@pytest.mark.asyncio
async def test_idle_transcript_status_reconciliation_quiesces_after_cold_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Idle history costs one cold reconciliation, not one DB read per poll forever.

    This models Studio's default five-second ambient-mirror poll with a bounded
    but non-trivial transcript population.  Every file is already at EOF and
    older than the liveness window, so the first pass must still repair stale
    ``running`` rows after a daemon restart.  Once that check succeeds, another
    unchanged pass has no new liveness fact to ask StateDB about.
    """
    import lionagi.cli.mirror as mirror_mod

    root = tmp_path / "projects"
    now = 1_800_000_000.0
    states: dict[str, _FileState] = {}
    expected_uids: list[str] = []
    for index in range(64):
        uid = f"idle-{index:04d}"
        path = root / "-work-acme" / f"{uid}.jsonl"
        _write_session_file(path, uid, age_secs=3600)
        os.utime(path, (now - 600, now - 600))
        states[str(path)] = _FileState(
            session_uid=uid,
            offset=path.stat().st_size,
            project="acme",
            attr_peeked=True,
        )
        expected_uids.append(uid)

    calls: list[str] = []

    async def _record_reconcile(_db, uid, **_kwargs):
        calls.append(uid)
        return True

    monkeypatch.setattr(mirror_mod.time, "time", lambda: now)
    monkeypatch.setattr(
        "lionagi.state.claude_mirror.reconcile_session_status",
        _record_reconcile,
    )

    await _one_pass(None, root, states, {}, since=None, live_window=300)
    await _one_pass(None, root, states, {}, since=None, live_window=300)

    assert calls == expected_uids


@pytest.mark.asyncio
async def test_live_transcript_reconciles_until_one_final_idle_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The DB liveness verdict, not file mtime, controls the final idle check."""
    import lionagi.cli.mirror as mirror_mod

    root = tmp_path / "projects"
    uid = "live-then-idle"
    path = root / "-work-acme" / f"{uid}.jsonl"
    _write_session_file(path, uid, age_secs=10)
    modified_at = 1_800_000_000.0
    # A copied/restored transcript can have an old filesystem timestamp while
    # the event timestamp persisted in StateDB is still live.  File mtime is a
    # scan hint, never the liveness source of truth.
    os.utime(path, (modified_at - 600, modified_at - 600))
    states = {
        str(path): _FileState(
            session_uid=uid,
            offset=path.stat().st_size,
            project="acme",
            attr_peeked=True,
        )
    }
    calls: list[str] = []
    settled = {"value": False}

    async def _record_reconcile(_db, reconciled_uid, **_kwargs):
        calls.append(reconciled_uid)
        return settled["value"]

    monkeypatch.setattr(mirror_mod.time, "time", lambda: modified_at)
    monkeypatch.setattr(
        "lionagi.state.claude_mirror.reconcile_session_status",
        _record_reconcile,
    )

    # Still live according to the persisted message clock: keep observing even
    # though the file itself already looks old.
    await _one_pass(None, root, states, {}, since=None, live_window=300)
    await _one_pass(None, root, states, {}, since=None, live_window=300)
    # The DB reports the final idle transition as settled.  Later unchanged
    # polls are quiescent.
    settled["value"] = True
    await _one_pass(None, root, states, {}, since=None, live_window=300)
    await _one_pass(None, root, states, {}, since=None, live_window=300)

    assert calls == [uid, uid, uid]


@pytest.mark.asyncio
async def test_an_unwindowed_pass_does_not_stat_for_a_window_it_will_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`li mirror` defaults to no window, and mtime has no consumer but the
    window check. Reading it anyway costs one syscall per transcript per pass,
    on the loop whose whole purpose is not to touch settled files."""
    import lionagi.cli.mirror as mirror_mod

    root = tmp_path / "projects"
    uid = "unwindowed"
    path = root / "-work-acme" / f"{uid}.jsonl"
    _write_session_file(path, uid, age_secs=10)
    now = 1_800_000_000.0
    os.utime(path, (now - 600, now - 600))

    real_stat = Path.stat
    stats: list[Path] = []

    def _counting_stat(self: Path, *args, **kwargs):
        if self == path:
            stats.append(self)
        return real_stat(self, *args, **kwargs)

    async def _settled_reconcile(_db, _uid, **_kwargs):
        return True

    monkeypatch.setattr(mirror_mod.time, "time", lambda: now)
    monkeypatch.setattr(
        "lionagi.state.claude_mirror.reconcile_session_status",
        _settled_reconcile,
    )

    async def _count(since: float | None) -> int:
        states = {
            str(path): _FileState(
                session_uid=uid,
                offset=path.stat().st_size,
                project="acme",
                attr_peeked=True,
            )
        }
        stats.clear()
        monkeypatch.setattr(Path, "stat", _counting_stat)
        try:
            await _one_pass(None, root, states, {}, since=since, live_window=300)
        finally:
            monkeypatch.setattr(Path, "stat", real_stat)
        return len(stats)

    # One stat either way for the cursor's size check, plus the window's own
    # when there is a window to enforce.
    assert await _count(None) == 1
    assert await _count(3600.0) == 2


@pytest.mark.asyncio
async def test_an_unwindowed_codex_pass_does_not_stat_for_the_window_either(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same rule on the Codex sweep, which carries its own copy of the check."""
    import lionagi.cli.mirror as mirror_mod

    now = 1_800_000_000.0
    uid = "0199cccc-0000-0000-0000-0000000000cc"
    path = tmp_path / "2026" / "08" / "16" / "rollout-unwindowed.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("{}\n")
    os.utime(path, (now - 600, now - 600))

    real_stat = Path.stat
    stats: list[Path] = []

    def _counting_stat(self: Path, *args, **kwargs):
        if self == path:
            stats.append(self)
        return real_stat(self, *args, **kwargs)

    async def _settled_reconcile(_db, _uid, **_kwargs):
        return True

    monkeypatch.setattr(mirror_mod.time, "time", lambda: now)
    monkeypatch.setattr(
        "lionagi.state.codex_mirror.reconcile_session_status",
        _settled_reconcile,
    )

    async def _count(since: float | None) -> int:
        states = {
            str(path): _FileState(
                session_uid=uid,
                offset=path.stat().st_size,
                head_checked=True,
                codex_provenance_peeked=True,
            )
        }
        stats.clear()
        monkeypatch.setattr(Path, "stat", _counting_stat)
        try:
            await mirror_mod._codex_pass(
                None, tmp_path, states, {}, since=since, live_window=300, threads={}
            )
        finally:
            monkeypatch.setattr(Path, "stat", real_stat)
        return len(stats)

    windowed = await _count(3600.0)
    assert await _count(None) == windowed - 1


@pytest.mark.asyncio
async def test_idle_codex_rollout_status_reconciliation_also_quiesces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bounded polling rule covers Studio's default Codex tree too."""
    import lionagi.cli.mirror as mirror_mod

    now = 1_800_000_000.0
    uid = "0199bbbb-0000-0000-0000-000000000099"
    path = tmp_path / "2026" / "08" / "11" / "rollout-idle.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("{}\n")
    os.utime(path, (now - 600, now - 600))
    state = _FileState(
        session_uid=uid,
        offset=path.stat().st_size,
        head_checked=True,
        codex_provenance_peeked=True,
    )
    states = {str(path): state}
    calls: list[str] = []

    async def _record_reconcile(_db, reconciled_uid, **_kwargs):
        calls.append(reconciled_uid)
        return True

    monkeypatch.setattr(mirror_mod.time, "time", lambda: now)
    monkeypatch.setattr(
        "lionagi.state.codex_mirror.reconcile_session_status",
        _record_reconcile,
    )

    await mirror_mod._codex_pass(
        None, tmp_path, states, {}, since=None, live_window=300, threads={}
    )
    await mirror_mod._codex_pass(
        None, tmp_path, states, {}, since=None, live_window=300, threads={}
    )

    assert calls == [uid]


@pytest.mark.asyncio
async def test_one_pass_reprocesses_batch_when_write_fails(
    temp_db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A failed mirror write must not advance the persisted cursor past the
    # unmirrored events: the next pass re-reads and mirrors them (at-least-once +
    # idempotent), so a transient DB error never silently drops a batch.
    root = tmp_path / "projects"
    uid = "ffffffff-0000-0000-0000-00000000000a"
    _write_session_file(root / "-w-proj" / f"{uid}.jsonl", uid, age_secs=5)

    from lionagi.state import claude_mirror as cm

    real_mirror_session = cm.mirror_session
    fail = {"on": True}

    async def _maybe_fail(*a, **k):
        if fail["on"]:
            raise RuntimeError("db down")
        return await real_mirror_session(*a, **k)

    monkeypatch.setattr("lionagi.state.claude_mirror.mirror_session", _maybe_fail)

    offsets: dict[str, int] = {}
    async with StateDB() as db:
        # Pass 1: the write fails; the cursor must NOT be persisted past the batch.
        await _one_pass(db, root, {}, offsets, since=None, live_window=300)
        assert await db.get_session(session_db_id(uid)) is None
        assert offsets == {}  # cursor never advanced past the unmirrored batch

        # Pass 2: write restored; the same batch is mirrored, not lost.
        fail["on"] = False
        await _one_pass(db, root, {}, offsets, since=None, live_window=300)
        row = await db.get_session(session_db_id(uid))
        msgs = await db.get_progression(row["progression_id"])
    assert row is not None
    assert len(msgs) == 2  # both events survived the earlier failure


@pytest.mark.asyncio
async def test_mirror_forever_retries_after_connection_open_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A transient failure to OPEN the StateDB connection — e.g. a locked or
    # half-migrated state.db during first-run startup, when the studio creates the
    # schema and checkpoints on another connection — must be retried, not silently
    # end the in-process tail for the whole life of the studio process. Regression
    # guard: the connection used to be opened once outside the loop, so the first
    # transient open error killed the tail permanently and invisibly.
    import asyncio

    import lionagi.cli.mirror as mirror_mod
    import lionagi.state.db as dbmod

    monkeypatch.setattr(dbmod, "DEFAULT_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(mirror_mod, "_OFFSETS_PATH", tmp_path / "mirror" / "offsets.json")

    attempts = {"open": 0}
    real_open = dbmod.StateDB.open

    async def _flaky_open(self) -> None:
        attempts["open"] += 1
        if attempts["open"] == 1:
            raise OSError("database is locked (simulated first-run race)")
        await real_open(self)

    monkeypatch.setattr(dbmod.StateDB, "open", _flaky_open)

    stop = asyncio.Event()
    # root is an empty dir: passes find no transcripts, so this isolates the
    # connection-lifecycle behaviour from any mirroring work.
    task = asyncio.create_task(
        mirror_mod.mirror_forever(stop, root=tmp_path, since="24h", interval=0.05)
    )
    try:
        for _ in range(100):  # let it fail the first open, back off, and reconnect
            if attempts["open"] >= 2:
                break
            await asyncio.sleep(0.02)
        alive = not task.done()
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5)

    assert attempts["open"] >= 2, "open was not retried after the first failure"
    assert alive, "tail died on a transient open failure instead of retrying"


# watcher helpers: tailing + parsing


def test_read_new_events_buffers_partial_line(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text('{"a":1}\n{"a":2}\n{"a":3')  # last line incomplete
    state = _FileState(session_uid="x")
    first, sources, new_offset, _ = _read_new_events(path, state)
    assert [e["a"] for e in first] == [1, 2]
    assert len(sources) == len(first)
    state.offset = new_offset  # caller commits the cursor only after a durable mirror
    # Complete the dangling line; the next read picks up only the new event.
    with path.open("a") as fh:
        fh.write("}\n")
    second, sources2, _, _ = _read_new_events(path, state)
    assert [e["a"] for e in second] == [3]
    assert len(sources2) == len(second)


def test_read_new_events_resets_on_truncation(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text('{"a":1}\n')
    state = _FileState(session_uid="x", offset=9999)  # offset past EOF
    out, _, _, _ = _read_new_events(path, state)
    assert [e["a"] for e in out] == [1]


def test_read_new_events_skips_corrupt_lines(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text('{"a":1}\nnot json\n{"a":2}\n')
    state = _FileState(session_uid="x")
    out, _, _, unreadable = _read_new_events(path, state)
    assert [e["a"] for e in out] == [1, 2]
    # The dropped line is reported, not silently absorbed: a damaged transcript
    # and an uninteresting one must not read the same downstream.
    assert unreadable == 1


def test_read_new_events_skips_non_dict_json_without_losing_followers(tmp_path: Path) -> None:
    # Valid JSON of the wrong shape (a bare list / scalar) must be dropped as
    # malformed, not handed downstream as an event, and must not swallow the
    # valid events that follow it on later lines.
    path = tmp_path / "t.jsonl"
    path.write_text('[]\n{"a":1}\n42\n{"a":2}\n')
    state = _FileState(session_uid="x")
    out, _, _, unreadable = _read_new_events(path, state)
    assert [e["a"] for e in out] == [1, 2]
    assert unreadable == 2  # both the bare list and the bare scalar


def test_first_prompt_skips_meta_and_command_noise() -> None:
    events = [
        {
            "type": "user",
            "isMeta": True,
            "message": {"content": [{"type": "text", "text": "meta"}]},
        },
        {
            "type": "user",
            "message": {"content": [{"type": "text", "text": "<command-name>/x</command-name>"}]},
        },
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
        {"type": "user", "message": {"content": [{"type": "text", "text": "the real question"}]}},
    ]
    assert _first_prompt(events) == "the real question"


@pytest.mark.parametrize(
    ("spec", "expected"),
    [("30m", 1800.0), ("12h", 43200.0), ("7d", 604800.0), ("120", 120.0), ("", None)],
)
def test_parse_window(spec: str, expected: float | None) -> None:
    assert _parse_window(spec) == expected


def test_parse_window_raises_on_malformed() -> None:
    # A bad spec must fail loudly, not silently become an unbounded (None) scan.
    with pytest.raises(ValueError, match="unrecognized --since window"):
        _parse_window("bad")


def test_since_window_argparse_type() -> None:
    import argparse

    assert _since_window("12h") == 43200.0
    with pytest.raises(argparse.ArgumentTypeError):
        _since_window("nonsense")
    with pytest.raises(argparse.ArgumentTypeError):
        _since_window("")  # empty is rejected at the CLI boundary (default=None handles "all")


# project attribution fallback


def test_fallback_project_uses_folder_name_when_dir_exists(tmp_path: Path) -> None:
    work = tmp_path / "my-workspace"
    work.mkdir()
    assert _fallback_project(str(work)) == ("my-workspace", "cwd_dir")


def test_fallback_project_uses_others_when_dir_missing() -> None:
    assert _fallback_project("/no/such/dir/anymore") == ("others", "cwd_missing")


def test_derive_metadata_falls_back_to_folder_name(tmp_path: Path) -> None:
    # A cwd that detect_project can't place (no git remote / config / override)
    # is bucketed by its own folder name rather than left unattributed.
    work = tmp_path / "loose-scripts"
    work.mkdir()
    state = _FileState(session_uid=SID)
    _derive_metadata(
        state, [_user_text("u1", "hi", ts="2026-06-20T00:00:00.000Z") | {"cwd": str(work)}]
    )
    assert state.project == "loose-scripts"
    assert state.project_source == "cwd_dir"


def test_derive_metadata_others_when_cwd_gone() -> None:
    state = _FileState(session_uid=SID)
    _derive_metadata(state, [_user_text("u1", "hi") | {"cwd": "/gone/missing/path"}])
    assert state.project == "others"
    assert state.project_source == "cwd_missing"


def test_derive_metadata_captures_raw_cwd_as_artifact_root(tmp_path: Path) -> None:
    # the raw cwd (not the bucketed project name) is the session's
    # artifact root -- every file the CLI touched lives under it.
    work = tmp_path / "my-workspace"
    work.mkdir()
    state = _FileState(session_uid=SID)
    _derive_metadata(
        state, [_user_text("u1", "hi", ts="2026-06-20T00:00:00.000Z") | {"cwd": str(work)}]
    )
    assert state.cwd == str(work)


@pytest.mark.asyncio
async def test_mirror_session_backfills_missing_project(temp_db_path: Path) -> None:
    # A session first mirrored with no project must be backfilled on a later pass
    # once a project is derived — without disturbing updated_at (the liveness clock).
    events = _conversation()
    async with StateDB() as db:
        await mirror_session(db, session_uid=SID, events=events, tool_names={}, project=None)
        before = await db.get_session(session_db_id(SID))
        assert before["project"] is None

        await mirror_session(
            db,
            session_uid=SID,
            events=events,
            tool_names={},
            project="acme/widget",
            project_source="cwd_dir",
        )
        after = await db.get_session(session_db_id(SID))
    assert after["project"] == "acme/widget"
    assert after["project_source"] == "cwd_dir"
    # Provenance backfill is not activity: the liveness clock must not move.
    assert after["updated_at"] == before["updated_at"]


@pytest.mark.asyncio
async def test_mirror_session_writes_artifacts_path_from_cwd(temp_db_path: Path) -> None:
    # a mirrored CLI session's cwd is its artifact root -- the run
    # file viewer is otherwise structurally dead for every mirrored session.
    events = _conversation()
    async with StateDB() as db:
        await mirror_session(
            db, session_uid=SID, events=events, tool_names={}, cwd="/work/acme-widget"
        )
        row = await db.get_session(session_db_id(SID))
    assert row["artifacts_path"] == "/work/acme-widget"


@pytest.mark.asyncio
async def test_mirror_session_does_not_clobber_an_existing_artifacts_path(
    temp_db_path: Path,
) -> None:
    # A launcher-set artifact root is more precise than the mirror's cwd guess
    # and must never be overwritten by a later mirror pass.
    events = _conversation()
    async with StateDB() as db:
        await mirror_session(
            db, session_uid=SID, events=events, tool_names={}, cwd="/work/first-guess"
        )
        await mirror_session(
            db, session_uid=SID, events=events, tool_names={}, cwd="/work/second-guess"
        )
        row = await db.get_session(session_db_id(SID))
    assert row["artifacts_path"] == "/work/first-guess"


# conversation-lineage detector


def _lineage_event(uid: str, euid: str, parent: str | None, role: str, text: str) -> dict:
    ev = {
        "type": role,
        "uuid": euid,
        "parentUuid": parent,
        "timestamp": _iso(datetime.now(timezone.utc)),
        "sessionId": uid,
        "cwd": "/tmp",
        "message": {"role": role, "content": [{"type": "text", "text": text}]},
    }
    if role == "assistant":
        ev["message"]["model"] = "claude-opus-4-8"
    return ev


def _write_lineage_file(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")


@pytest.mark.asyncio
async def test_lineage_links_continued_session(temp_db_path: Path, tmp_path: Path) -> None:
    # Session B's first message points (parentUuid) at session A's last message:
    # B continues A. The mirror records that as a lineage link on B.
    root = tmp_path / "projects"
    a, b = "aaaaaaaa-0000-0000-0000-000000000001", "bbbbbbbb-0000-0000-0000-000000000002"
    _write_lineage_file(
        root / "-w-proj" / f"{a}.jsonl",
        [
            _lineage_event(a, "a-1", None, "user", "start the work"),
            _lineage_event(a, "a-leaf", "a-1", "assistant", "done, ending here"),
        ],
    )
    _write_lineage_file(
        root / "-w-proj" / f"{b}.jsonl",
        [
            _lineage_event(b, "b-1", "a-leaf", "user", "continuing from before"),
            _lineage_event(b, "b-2", "b-1", "assistant", "picking it up"),
        ],
    )
    async with StateDB() as db:
        await _one_pass(db, root, {}, {}, since=None, live_window=300)
        child = await db.get_session(session_db_id(b))
    lineage = child["node_metadata"]["lineage"]
    assert lineage["parent_session_id"] == session_db_id(a)
    assert lineage["parent_session_uid"] == a
    assert lineage["parent_event_uuid"] == "a-leaf"


@pytest.mark.asyncio
async def test_no_lineage_for_self_rooted_session(temp_db_path: Path, tmp_path: Path) -> None:
    root = tmp_path / "projects"
    s = "cccccccc-0000-0000-0000-000000000003"
    _write_lineage_file(
        root / "-w-proj" / f"{s}.jsonl",
        [
            _lineage_event(s, "c-1", None, "user", "fresh start"),
            _lineage_event(s, "c-2", "c-1", "assistant", "ok"),
        ],
    )
    async with StateDB() as db:
        await _one_pass(db, root, {}, {}, since=None, live_window=300)
        row = await db.get_session(session_db_id(s))
    assert "lineage" not in (row["node_metadata"] or {})


@pytest.mark.asyncio
async def test_no_lineage_for_same_session_across_files(temp_db_path: Path, tmp_path: Path) -> None:
    # Two files share one sessionId (a resumed session). File 2's head points at
    # file 1's leaf — same session, so it is NOT cross-session lineage.
    root = tmp_path / "projects"
    s = "dddddddd-0000-0000-0000-000000000004"
    _write_lineage_file(
        root / "-w-proj" / f"{s}-1.jsonl",
        [
            _lineage_event(s, "d-1", None, "user", "part one"),
            _lineage_event(s, "d-mid", "d-1", "assistant", "more"),
        ],
    )
    _write_lineage_file(
        root / "-w-proj" / f"{s}-2.jsonl",
        [
            _lineage_event(s, "d-3", "d-mid", "user", "part two same session"),
            _lineage_event(s, "d-4", "d-3", "assistant", "ok"),
        ],
    )
    async with StateDB() as db:
        await _one_pass(db, root, {}, {}, since=None, live_window=300)
        row = await db.get_session(session_db_id(s))
    assert "lineage" not in (row["node_metadata"] or {})


def test_lineage_resolve_skips_unindexed_and_same_session() -> None:
    lin = _Lineage()
    lin.leaf_owner = {"leaf-A": "sessA"}
    lin.pending = {
        "sessB": "leaf-A",  # resolves to a different session -> link
        "sessC": "unknown-leaf",  # parent not indexed -> stays pending
        "sessA": "leaf-A",  # resolves to itself -> not lineage
    }
    links = lin.resolve()
    assert links == [("sessB", "sessA", "leaf-A")]
    assert lin.pending == {"sessC": "unknown-leaf"}  # unresolved stays for next pass
    assert "sessB" in lin.linked


@pytest.mark.asyncio
async def test_idle_session_backfilled_with_project(temp_db_path: Path, tmp_path: Path) -> None:
    # A session fully mirrored before attribution (row has no project) and now
    # idle (file at EOF, no new events) is still backfilled from its head cwd.
    work = tmp_path / "ghost-proj"
    work.mkdir()
    uid = "eeeeeeee-0000-0000-0000-000000000005"
    root = tmp_path / "projects"
    path = root / "-w-proj" / f"{uid}.jsonl"
    events = [
        _lineage_event(uid, "e-1", None, "user", "hi") | {"cwd": str(work)},
        _lineage_event(uid, "e-2", "e-1", "assistant", "ok"),
    ]
    _write_lineage_file(path, events)
    async with StateDB() as db:
        await mirror_session(db, session_uid=uid, events=events, tool_names={}, project=None)
        assert (await db.get_session(session_db_id(uid)))["project"] is None
        # Idle pass: file already fully read (offset at EOF) -> no streamed events.
        offsets = {str(path): path.stat().st_size}
        await _one_pass(db, root, {}, offsets, since=None, live_window=300)
        row = await db.get_session(session_db_id(uid))
    assert row["project"] == "ghost-proj"
    assert row["project_source"] == "cwd_dir"


@pytest.mark.asyncio
async def test_idle_session_backfilled_with_artifacts_path_even_when_project_already_set(
    temp_db_path: Path, tmp_path: Path
) -> None:
    # the dominant NULL-artifacts_path population is sessions the
    # mirror already attributed a project to (in an earlier process) before this
    # fix existed -- artifacts_path must backfill on its own, not only when
    # project is also missing.
    work = tmp_path / "ghost-proj-2"
    work.mkdir()
    uid = "ffffffff-0000-0000-0000-000000000006"
    root = tmp_path / "projects"
    path = root / "-w-proj" / f"{uid}.jsonl"
    events = [
        _lineage_event(uid, "f-1", None, "user", "hi") | {"cwd": str(work)},
        _lineage_event(uid, "f-2", "f-1", "assistant", "ok"),
    ]
    _write_lineage_file(path, events)
    async with StateDB() as db:
        # Simulate a pre-fix row: project already attributed, artifacts_path never was.
        await mirror_session(
            db, session_uid=uid, events=events, tool_names={}, project="ghost-proj-2"
        )
        before = await db.get_session(session_db_id(uid))
        assert before["project"] == "ghost-proj-2"
        assert before["artifacts_path"] is None
        offsets = {str(path): path.stat().st_size}
        await _one_pass(db, root, {}, offsets, since=None, live_window=300)
        row = await db.get_session(session_db_id(uid))
    assert row["artifacts_path"] == str(work)
    assert row["project"] == "ghost-proj-2"


@pytest.mark.asyncio
async def test_attr_peeked_flag_not_set_on_backfill_failure(
    temp_db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same failure-atomicity requirement as the codex idle backfill
    (test_codex_provenance_peeked_flag_not_set_on_backfill_failure): a
    transient set_session_provenance failure inside the idle project
    backfill must not permanently mark this in-memory state as peeked, or no
    later pass in this process ever retries it."""
    from lionagi.cli import mirror as mirror_mod

    work = tmp_path / "ghost-proj-flaky"
    work.mkdir()
    uid = "eeeeeeee-0000-0000-0000-000000000099"
    root = tmp_path / "projects"
    path = root / "-w-proj" / f"{uid}.jsonl"
    events = [
        _lineage_event(uid, "g-1", None, "user", "hi") | {"cwd": str(work)},
        _lineage_event(uid, "g-2", "g-1", "assistant", "ok"),
    ]
    _write_lineage_file(path, events)

    attempts = []
    real_attribute = mirror_mod._attribute_idle

    async def flaky_attribute(db, state, cwd):
        attempts.append(state.session_uid)
        if len(attempts) == 1:
            raise RuntimeError("set_session_provenance transient failure")
        return await real_attribute(db, state, cwd)

    monkeypatch.setattr(mirror_mod, "_attribute_idle", flaky_attribute)

    async with StateDB() as db:
        await mirror_session(db, session_uid=uid, events=events, tool_names={}, project=None)
        assert (await db.get_session(session_db_id(uid)))["project"] is None

        offsets = {str(path): path.stat().st_size}
        states: dict[str, _FileState] = {}
        # _one_pass's own per-transcript exception handler swallows the raise.
        await _one_pass(db, root, states, offsets, since=None, live_window=300)
        state = states[str(path)]
        assert not state.attr_peeked, (
            "the flag was set even though the backfill raised, so no later pass will retry it"
        )

        await _one_pass(db, root, states, offsets, since=None, live_window=300)
        row = await db.get_session(session_db_id(uid))
    assert state.attr_peeked
    assert row["project"] == "ghost-proj-flaky"
    assert len(attempts) == 2, f"backfill was attempted {len(attempts)} time(s), not 2"


@pytest.mark.asyncio
async def test_peek_head_skips_non_dict_head_line(temp_db_path: Path, tmp_path: Path) -> None:
    # A valid-JSON-but-non-dict head line (e.g. `[]`) must not wedge the idle
    # reconcile path: _peek_head must skip it like _read_new_events, so an
    # already-mirrored, now-idle session still reconciles to completed instead of
    # being left stuck at running by an AttributeError swallowed mid-pass.
    root = tmp_path / "projects"
    uid = "abababab-0000-0000-0000-0000000000cd"
    path = root / "-w-proj" / f"{uid}.jsonl"
    _write_session_file(path, uid, age_secs=7200)  # idle -> should become completed
    events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    path.write_text("[]\n" + path.read_text())  # prepend a non-dict JSON head line
    async with StateDB() as db:
        await mirror_session(db, session_uid=uid, events=events, tool_names={}, status="running")
        assert (await db.get_session(session_db_id(uid)))["status"] == "running"
        # Idle pass: file already fully read (offset at EOF) -> no streamed events,
        # so _peek_head scans the head and hits the `[]` line.
        offsets = {str(path): path.stat().st_size}
        await _one_pass(db, root, {}, offsets, since=None, live_window=300)
        row = await db.get_session(session_db_id(uid))
    assert row["status"] == "completed"


# restart durability: derived state persisted with the cursor


@pytest.mark.asyncio
async def test_tool_name_survives_restart_between_use_and_result(
    temp_db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A restart between a tool_use and its tool_result must not drop the response's
    # function name. tool_names is rebuilt from the persisted per-file state, so the
    # ActionResponse still labels the call instead of falling back to "".
    monkeypatch.setattr("lionagi.cli.mirror._OFFSETS_PATH", tmp_path / "offsets.json")
    root = tmp_path / "projects"
    path = root / "-w-proj" / f"{SID}.jsonl"
    _write_lineage_file(
        path,
        [
            _user_text("u1", "run it"),
            _assistant("a1", [{"type": "tool_use", "id": "tool_1", "name": "Bash", "input": {}}]),
        ],
    )
    async with StateDB() as db:
        # Pass 1: mirror the tool_use, persist the cursor + tool_names, then drop
        # all in-memory state — a restart.
        states1: dict[str, _FileState] = {}
        await _one_pass(db, root, states1, {}, since=None, live_window=300)
        _save_states(states1)

        # Restart: state comes back from disk only.
        states2 = _load_states()
        offsets2 = {k: s.offset for k, s in states2.items()}

        # The tool_result lands after the restart.
        with path.open("a") as fh:
            fh.write(json.dumps(_tool_result("u2", "tool_1", "total 0")) + "\n")
        await _one_pass(db, root, states2, offsets2, since=None, live_window=300)

        row = await db.get_session(session_db_id(SID))
        contents = []
        for mid in await db.get_progression(row["progression_id"]):
            c = (await db.get_message(mid))["content"]
            contents.append(json.loads(c) if isinstance(c, str) else c)
    resp = next(c for c in contents if "action_request_id" in c)
    assert resp["function"] == "Bash"  # recovered from persisted tool_names, not ""


@pytest.mark.asyncio
async def test_lineage_resolves_after_restart(
    temp_db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The parent transcript is mirrored, then state is dropped (restart) before the
    # continuation arrives. Its leaf is re-indexed from persisted state on reload,
    # so the child still links to it instead of losing the cross-session lineage.
    monkeypatch.setattr("lionagi.cli.mirror._OFFSETS_PATH", tmp_path / "offsets.json")
    root = tmp_path / "projects"
    a = "a1a1a1a1-0000-0000-0000-000000000aaa"
    b = "b2b2b2b2-0000-0000-0000-000000000bbb"
    _write_lineage_file(
        root / "-w-proj" / f"{a}.jsonl",
        [
            _lineage_event(a, "a-1", None, "user", "start the work"),
            _lineage_event(a, "a-leaf", "a-1", "assistant", "done, ending here"),
        ],
    )
    async with StateDB() as db:
        # Pass 1: mirror only the parent; persist its leaf index, then restart.
        states1: dict[str, _FileState] = {}
        await _one_pass(db, root, states1, {}, since=None, live_window=300)
        _save_states(states1)

        # Restart: rebuild states + re-seed the leaf index from disk only.
        states2 = _load_states()
        offsets2 = {k: s.offset for k, s in states2.items()}
        lineage2 = _Lineage()
        _seed_lineage(lineage2, states2)

        # The continuation opens after the restart, pointing at the parent's leaf.
        _write_lineage_file(
            root / "-w-proj" / f"{b}.jsonl",
            [
                _lineage_event(b, "b-1", "a-leaf", "user", "continuing from before"),
                _lineage_event(b, "b-2", "b-1", "assistant", "picking it up"),
            ],
        )
        await _one_pass(db, root, states2, offsets2, since=None, live_window=300, lineage=lineage2)
        child = await db.get_session(session_db_id(b))
    lineage = child["node_metadata"]["lineage"]
    assert lineage["parent_session_uid"] == a
    assert lineage["parent_event_uuid"] == "a-leaf"


async def test_codex_file_that_mirrors_nothing_is_reported_not_skipped(tmp_path, caplog):
    """A rollout read in full that produces no messages is surfaced, not passed over.

    Without this it is indistinguishable from a file the mirror has not reached:
    mirror_session writes no session row when a batch yields nothing, so there is
    no row carrying the counts either. The measured cause on a real corpus is the
    pre-2025-09-20 flat rollout format, which matches no record type read here.
    """
    import logging

    from lionagi.cli.mirror import _FileState, _mirror_one_codex
    from lionagi.state.db import StateDB

    path = tmp_path / "rollout-legacy.jsonl"
    # Flat legacy records: a type at the top level and no payload envelope.
    path.write_text(
        '{"id":"x","instructions":"y","timestamp":"2025-09-01T09:34:37Z"}\n'
        '{"type":"message","role":"user","content":[{"text":"hi"}]}\n'
        '{"type":"function_call","name":"shell","arguments":"{}"}\n'
    )
    state = _FileState(session_uid="")

    async with StateDB(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}") as db:
        with caplog.at_level(logging.WARNING):
            written = await _mirror_one_codex(db, path, state, {})

    assert written == 0
    text = " ".join(r.message for r in caplog.records)
    assert "rollout-legacy.jsonl" in text
    assert "mirrored none" in text
    # The record types it did see are named, so the reason is diagnosable from the
    # log alone without re-reading the file.
    assert "function_call" in text and "message" in text
    # Reported once per file, not on every poll pass.
    assert state.barren_reported


# Orchestrated codex rollouts (headless `codex exec`)


def _codex_rollout_lines(uid: str, originator: str) -> str:
    meta = {
        "type": "session_meta",
        "timestamp": "2026-07-31T09:00:00Z",
        "payload": {"id": uid, "session_id": uid, "cwd": "/x", "originator": originator},
    }
    user = {
        "type": "response_item",
        "timestamp": "2026-07-31T09:00:01Z",
        "payload": {"type": "message", "role": "user", "id": "m1", "content": [{"text": "q"}]},
    }
    asst = {
        "type": "response_item",
        "timestamp": "2026-07-31T09:00:02Z",
        "payload": {"type": "message", "role": "assistant", "id": "m2", "content": [{"text": "a"}]},
    }
    return "".join(json.dumps(e) + "\n" for e in (meta, user, asst))


async def test_orchestrated_rollout_is_never_mirrored(tmp_path):
    """A `codex exec` rollout is an orchestrator's run — the run that spawned it
    already has a session of its own, so mirroring it would show the same work
    twice (the agent's session plus an extra "codex" one)."""
    from lionagi.cli.mirror import _FileState, _mirror_one_codex
    from lionagi.state.codex_mirror import session_db_id as codex_sid

    uid = "0199bbbb-0000-0000-0000-000000000001"
    path = tmp_path / "rollout-exec.jsonl"
    path.write_text(_codex_rollout_lines(uid, "codex_exec"))
    state = _FileState(session_uid="")

    async with StateDB(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}") as db:
        assert await _mirror_one_codex(db, path, state, {}) == 0
        assert state.orchestrated
        assert await db.get_session(codex_sid(uid)) is None
        # Later passes stay skipped without re-reading the file.
        assert await _mirror_one_codex(db, path, state, {}) == 0


async def test_an_imported_rollout_is_named_from_its_first_prompt(tmp_path):
    """Every imported rollout was landing under the same generic name.

    The name is derived by reading the instruction off a mirrored message, and
    the message carries it on a content model rather than in a mapping. Asked
    for a mapping key, the derivation found nothing for every rollout ever
    imported and each one fell through to the fallback -- so a board of live
    sessions showed one repeated title with nothing to tell them apart.

    Driven through the whole mirror rather than the derivation alone: the two
    halves each look right in isolation, and it is the seam between them that
    carried the defect.
    """
    from lionagi.cli.mirror import _FileState, _mirror_one_codex
    from lionagi.state.codex_mirror import session_db_id as codex_sid

    uid = "0199bbbb-0000-0000-0000-00000000000a"
    path = tmp_path / "rollout-named.jsonl"
    path.write_text(_codex_rollout_lines(uid, "Codex Desktop"))
    state = _FileState(session_uid="")

    async with StateDB(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}") as db:
        assert await _mirror_one_codex(db, path, state, {}) > 0
        session = await db.get_session(codex_sid(uid))

    assert session is not None
    assert session["name"] == "q", (
        f"named {session['name']!r}; the first prompt never reached the name"
    )


async def test_a_rollout_with_no_real_prompt_keeps_the_generic_name(tmp_path):
    """The arm that separates deriving the right name from deriving any name.

    Codex opens some rollouts with an injected context block and no human turn
    at all. Those carry nothing worth showing, so they are expected to keep the
    fallback -- without this, a derivation that returned the injected block
    would pass the test above while putting machine plumbing on the board.
    """
    import json as _json

    from lionagi.cli.mirror import _FileState, _mirror_one_codex
    from lionagi.state.codex_mirror import session_db_id as codex_sid

    uid = "0199bbbb-0000-0000-0000-00000000000b"
    lines = _codex_rollout_lines(uid, "Codex Desktop").splitlines()
    injected = _json.loads(lines[1])
    injected["payload"]["content"] = [{"text": "<environment_context>cwd=/x</environment_context>"}]
    lines[1] = _json.dumps(injected)
    path = tmp_path / "rollout-injected.jsonl"
    path.write_text("".join(line + "\n" for line in lines))
    state = _FileState(session_uid="")

    async with StateDB(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}") as db:
        await _mirror_one_codex(db, path, state, {})
        session = await db.get_session(codex_sid(uid))

    assert session is not None
    assert session["name"] == "Codex session", (
        f"named {session['name']!r}; an injected context block became the title"
    )


async def test_orchestrated_rollout_absorbs_an_earlier_import(tmp_path):
    """A row imported before this rule existed is removed the first time the
    mirror reclassifies its rollout, so old double entries heal on upgrade."""
    from lionagi.cli.mirror import _FileState, _mirror_one_codex
    from lionagi.state.codex_mirror import mirror_session as codex_mirror_session
    from lionagi.state.codex_mirror import session_db_id as codex_sid

    uid = "0199bbbb-0000-0000-0000-000000000002"
    path = tmp_path / "rollout-exec-old.jsonl"
    path.write_text(_codex_rollout_lines(uid, "codex_exec"))

    async with StateDB(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}") as db:
        await codex_mirror_session(
            db,
            rollout_uid=uid,
            records=[json.loads(line) for line in path.read_text().splitlines()],
            tool_names={},
            source_path=str(path),
        )
        assert await db.get_session(codex_sid(uid)) is not None

        state = _FileState(session_uid="")
        assert await _mirror_one_codex(db, path, state, {}) == 0
        assert await db.get_session(codex_sid(uid)) is None


async def test_interactive_rollout_still_mirrors(tmp_path):
    """The skip is scoped to orchestrated originators: desktop/TUI/IDE history —
    the mirror's actual subject — keeps mirroring exactly as before."""
    from lionagi.cli.mirror import _FileState, _mirror_one_codex
    from lionagi.state.codex_mirror import SOURCE_KIND as CODEX_SOURCE_KIND
    from lionagi.state.codex_mirror import session_db_id as codex_sid

    uid = "0199bbbb-0000-0000-0000-000000000003"
    path = tmp_path / "rollout-desktop.jsonl"
    path.write_text(_codex_rollout_lines(uid, "Codex Desktop"))
    state = _FileState(session_uid="")

    async with StateDB(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}") as db:
        written = await _mirror_one_codex(db, path, state, {})
        assert written == 2
        assert not state.orchestrated
        row = await db.get_session(codex_sid(uid))
        assert row is not None
        assert row["source_kind"] == CODEX_SOURCE_KIND


async def test_interactive_rollout_records_artifacts_path_from_header_cwd(tmp_path):
    # the session_meta header's cwd is the rollout's artifact root,
    # the same gap claude_mirror had.
    from lionagi.cli.mirror import _FileState, _mirror_one_codex
    from lionagi.state.codex_mirror import session_db_id as codex_sid

    uid = "0199bbbb-0000-0000-0000-000000000005"
    path = tmp_path / "rollout-artifacts.jsonl"
    path.write_text(_codex_rollout_lines(uid, "Codex Desktop"))
    state = _FileState(session_uid="")

    async with StateDB(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}") as db:
        await _mirror_one_codex(db, path, state, {})
        row = await db.get_session(codex_sid(uid))
    assert row["artifacts_path"] == "/x"


async def test_codex_mirror_session_does_not_clobber_an_existing_artifacts_path(tmp_path):
    from lionagi.state.codex_mirror import mirror_session as codex_mirror_session
    from lionagi.state.codex_mirror import session_db_id as codex_sid

    uid = "0199bbbb-0000-0000-0000-000000000006"
    records = [
        {
            "type": "response_item",
            "timestamp": "2026-07-31T09:00:01Z",
            "payload": {"type": "message", "role": "user", "id": "m1", "content": [{"text": "q"}]},
        }
    ]
    async with StateDB(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}") as db:
        await codex_mirror_session(
            db, rollout_uid=uid, records=records, tool_names={}, cwd="/work/first-guess"
        )
        await codex_mirror_session(
            db, rollout_uid=uid, records=records, tool_names={}, cwd="/work/second-guess"
        )
        row = await db.get_session(codex_sid(uid))
    assert row["artifacts_path"] == "/work/first-guess"


@pytest.mark.asyncio
async def test_codex_task_complete_provider_error_marks_idle_session_failed(tmp_path):
    from lionagi.state.codex_mirror import mirror_session as codex_mirror_session
    from lionagi.state.codex_mirror import reconcile_session_status as codex_reconcile_status
    from lionagi.state.codex_mirror import session_db_id as codex_sid
    from lionagi.state.reasons import RunReasons

    uid = "0199bbbb-0000-0000-0000-000000000020"
    records = [
        {
            "type": "response_item",
            "timestamp": "2026-07-29T21:20:00Z",
            "payload": {
                "type": "message",
                "role": "user",
                "id": "m1",
                "content": [{"type": "input_text", "text": "do the thing"}],
            },
        },
        {
            "type": "event_msg",
            "timestamp": "2026-07-29T21:21:44Z",
            "payload": {
                "type": "task_complete",
                "error": {
                    "codex_error_info": "cyber_policy",
                    "message": "request rejected by provider policy",
                },
                "last_agent_message": None,
            },
        },
    ]
    async with StateDB(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}") as db:
        await codex_mirror_session(db, rollout_uid=uid, records=records, tool_names={})
        before = await db.get_session(codex_sid(uid))
        assert before["node_metadata"]["mirror_provider_error"] == {"error": "cyber_policy"}
        await codex_reconcile_status(
            db,
            uid,
            now=before["last_message_at"] + 10_000,
            live_window=300,
        )
        after = await db.get_session(codex_sid(uid))

    assert after["status"] == "failed"
    assert after["status_reason_code"] == RunReasons.FAILED_PROVIDER_NONRETRYABLE


@pytest.mark.asyncio
async def test_codex_successful_task_complete_clears_prior_provider_error(tmp_path):
    from lionagi.state.codex_mirror import mirror_session as codex_mirror_session
    from lionagi.state.codex_mirror import session_db_id as codex_sid

    uid = "0199bbbb-0000-0000-0000-000000000021"
    user = {
        "type": "response_item",
        "timestamp": "2026-07-29T21:20:00Z",
        "payload": {
            "type": "message",
            "role": "user",
            "id": "m1",
            "content": [{"type": "input_text", "text": "do the thing"}],
        },
    }
    failed = {
        "type": "event_msg",
        "timestamp": "2026-07-29T21:21:44Z",
        "payload": {
            "type": "task_complete",
            "error": {"codex_error_info": "cyber_policy", "message": "rejected"},
        },
    }
    completed = {
        "type": "event_msg",
        "timestamp": "2026-07-29T21:22:44Z",
        "payload": {"type": "task_complete", "last_agent_message": "done"},
    }
    async with StateDB(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}") as db:
        await codex_mirror_session(db, rollout_uid=uid, records=[user, failed], tool_names={})
        failed_row = await db.get_session(codex_sid(uid))
        assert "mirror_provider_error" in failed_row["node_metadata"]

        await codex_mirror_session(db, rollout_uid=uid, records=[completed], tool_names={})
        completed_row = await db.get_session(codex_sid(uid))

    assert "mirror_provider_error" not in completed_row["node_metadata"]


async def test_idle_codex_rollout_backfills_artifacts_path_from_header_cwd(tmp_path):
    # An existing row (mirrored before cwd attribution existed, or by a process
    # that crashed before this pass) has artifacts_path=NULL. A later process
    # restarts with its offset already restored to EOF -- _read_new_events
    # yields no records even though the header (re-read on this fresh
    # _FileState) still carries cwd. Without an idle backfill this row's
    # artifacts_path would never be set.
    from lionagi.cli.mirror import _FileState, _mirror_one_codex
    from lionagi.state.codex_mirror import mirror_session as codex_mirror_session
    from lionagi.state.codex_mirror import session_db_id as codex_sid

    uid = "0199bbbb-0000-0000-0000-000000000007"
    path = tmp_path / "rollout-idle.jsonl"
    contents = _codex_rollout_lines(uid, "Codex Desktop")
    path.write_text(contents)

    records = [json.loads(line) for line in contents.splitlines()]
    async with StateDB(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}") as db:
        await codex_mirror_session(db, rollout_uid=uid, records=records, tool_names={}, cwd=None)
        before = await db.get_session(codex_sid(uid))
        assert before is not None
        assert before["artifacts_path"] is None

        # Fresh state (as after a restart), offset restored to EOF.
        state = _FileState(session_uid="", offset=len(contents.encode()))
        written = await _mirror_one_codex(db, path, state, {})
        assert written == 0

        row = await db.get_session(codex_sid(uid))
    assert row["artifacts_path"] == "/x"


async def test_codex_provenance_peeked_flag_not_set_on_backfill_failure(tmp_path, monkeypatch):
    """codex_provenance_peeked is never persisted -- it only exists to avoid a
    redundant DB round-trip on every idle pass within one process's lifetime.
    Setting it before the backfill it guards succeeds means a transient
    set_session_provenance failure (e.g. a DB hiccup) permanently blocks
    retry for the rest of that process, even though the flag's own docstring
    promise is "attempted", not "attempted once and given up on"."""
    from lionagi.cli import mirror as mirror_mod
    from lionagi.cli.mirror import _FileState, _mirror_one_codex
    from lionagi.state.codex_mirror import mirror_session as codex_mirror_session
    from lionagi.state.codex_mirror import session_db_id as codex_sid

    uid = "0199bbbb-0000-0000-0000-000000000010"
    path = tmp_path / "rollout-idle-flaky.jsonl"
    contents = _codex_rollout_lines(uid, "Codex Desktop")
    path.write_text(contents)
    records = [json.loads(line) for line in contents.splitlines()]

    attempts = []
    real_attribute = mirror_mod._attribute_idle_codex

    async def flaky_attribute(db, state):
        attempts.append(state.session_uid)
        if len(attempts) == 1:
            raise RuntimeError("set_session_provenance transient failure")
        return await real_attribute(db, state)

    monkeypatch.setattr(mirror_mod, "_attribute_idle_codex", flaky_attribute)

    async with StateDB(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}") as db:
        await codex_mirror_session(db, rollout_uid=uid, records=records, tool_names={}, cwd=None)
        before = await db.get_session(codex_sid(uid))
        assert before["artifacts_path"] is None

        state = _FileState(session_uid="", offset=len(contents.encode()))
        with pytest.raises(RuntimeError):
            await _mirror_one_codex(db, path, state, {})
        assert not state.codex_provenance_peeked, (
            "the flag was set even though the backfill raised, so no later pass will retry it"
        )

        written = await _mirror_one_codex(db, path, state, {})
        assert written == 0
        assert state.codex_provenance_peeked

        row = await db.get_session(codex_sid(uid))
    assert row["artifacts_path"] == "/x"
    assert len(attempts) == 2, f"backfill was attempted {len(attempts)} time(s), not 2"


async def test_partial_header_defers_classification_instead_of_bypassing_it(tmp_path):
    """A rollout whose first line is still being written must not settle its
    classification: committing the head check on an unreadable header would let
    an orchestrated rollout mirror forever once the header completed."""
    from lionagi.cli.mirror import _FileState, _mirror_one_codex
    from lionagi.state.codex_mirror import session_db_id as codex_sid

    uid = "0199bbbb-0000-0000-0000-000000000004"
    full = _codex_rollout_lines(uid, "codex_exec")
    header_line = full.splitlines(keepends=True)[0]
    path = tmp_path / "rollout-partial.jsonl"
    path.write_text(header_line[: len(header_line) // 2])  # torn mid-JSON, no newline

    state = _FileState(session_uid="")
    async with StateDB(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}") as db:
        assert await _mirror_one_codex(db, path, state, {}) == 0
        assert not state.head_checked  # classification deferred, not spent
        assert not state.orchestrated

        path.write_text(full)  # the writer finished the file
        assert await _mirror_one_codex(db, path, state, {}) == 0
        assert state.orchestrated
        assert await db.get_session(codex_sid(uid)) is None


async def test_reclassification_absorbs_a_row_keyed_by_the_stem_fallback(tmp_path):
    """An earlier version that failed the header peek imported the file under
    its path stem. When classification finally lands, that stem-keyed row is
    absorbed too, not just the rollout-uid one."""
    from lionagi.cli.mirror import _FileState, _mirror_one_codex
    from lionagi.state.codex_mirror import mirror_session as codex_mirror_session
    from lionagi.state.codex_mirror import session_db_id as codex_sid

    uid = "0199bbbb-0000-0000-0000-000000000005"
    path = tmp_path / "rollout-stem-import.jsonl"
    path.write_text(_codex_rollout_lines(uid, "codex_exec"))
    stem = path.stem

    async with StateDB(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}") as db:
        await codex_mirror_session(
            db,
            rollout_uid=stem,  # what the old fallback keyed the row by
            records=[json.loads(line) for line in path.read_text().splitlines()],
            tool_names={},
            source_path=str(path),
        )
        assert await db.get_session(codex_sid(stem)) is not None

        # Restart shape: the persisted state still carries the stem uid.
        state = _FileState(session_uid=stem)
        assert await _mirror_one_codex(db, path, state, {}) == 0
        assert state.orchestrated
        assert await db.get_session(codex_sid(stem)) is None
        assert await db.get_session(codex_sid(uid)) is None


async def test_torn_header_defers_mirroring_so_one_rollout_stays_one_session(tmp_path):
    """When the header is torn the whole file waits: writing records under the
    path stem while the real UID arrives next pass would split one interactive
    rollout into two sessions."""
    from lionagi.cli.mirror import _FileState, _mirror_one_codex
    from lionagi.state.codex_mirror import session_db_id as codex_sid

    uid = "0199bbbb-0000-0000-0000-000000000006"
    full = _codex_rollout_lines(uid, "Codex Desktop")
    header_line = full.splitlines(keepends=True)[0]
    path = tmp_path / "rollout-torn-interactive.jsonl"
    path.write_text(header_line[: len(header_line) // 2])  # torn mid-JSON, no newline

    state = _FileState(session_uid="")
    async with StateDB(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}") as db:
        assert await _mirror_one_codex(db, path, state, {}) == 0
        assert not state.head_checked
        assert not state.session_uid  # nothing was keyed under the stem
        assert await db.get_session(codex_sid(path.stem)) is None

        path.write_text(full)  # the writer finished the file
        assert await _mirror_one_codex(db, path, state, {}) == 2
        assert state.session_uid == uid
        assert await db.get_session(codex_sid(uid)) is not None
        assert await db.get_session(codex_sid(path.stem)) is None


async def test_headerless_rollout_still_mirrors_under_the_stem(tmp_path):
    """A complete first line that is not a session_meta settles classification:
    an append-only file never gains a header later, so the stem fallback is
    correct and the file keeps mirroring."""
    from lionagi.cli.mirror import _FileState, _mirror_one_codex
    from lionagi.state.codex_mirror import session_db_id as codex_sid

    full = _codex_rollout_lines("ignored-uid", "x")
    body = "".join(full.splitlines(keepends=True)[1:])  # drop the meta line
    path = tmp_path / "rollout-headerless.jsonl"
    path.write_text(body)

    state = _FileState(session_uid="")
    async with StateDB(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}") as db:
        assert await _mirror_one_codex(db, path, state, {}) == 2
        assert state.head_checked
        assert state.session_uid == path.stem
        assert await db.get_session(codex_sid(path.stem)) is not None


async def test_cli_tail_loop_retries_a_failed_backfill(tmp_path, monkeypatch):
    """The plain `li mirror` tail loop keeps retrying an unclean backfill sweep
    instead of retiring it after one failed attempt (parity with the studio's
    mirror_forever)."""
    import argparse

    from lionagi.cli import mirror as mirror_mod

    attempts: list[bool] = []

    async def fake_backfill(db):
        attempts.append(True)
        return len(attempts) >= 2  # first sweep fails, second is clean

    async def fake_codex_pass(db, root, states, offsets, *, since, live_window, threads):
        return 0

    class _Stop(Exception):
        pass

    sleeps: list[int] = []

    async def fake_sleep(_):
        sleeps.append(1)
        if len(sleeps) >= 3:
            raise _Stop

    class FakeDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(mirror_mod, "_absorb_backfill", fake_backfill)
    monkeypatch.setattr(mirror_mod, "_codex_pass", fake_codex_pass)
    monkeypatch.setattr(mirror_mod, "_load_states", lambda: {})
    monkeypatch.setattr(mirror_mod, "_save_states", lambda states: None)
    monkeypatch.setattr("lionagi.state.db.StateDB", lambda: FakeDB())
    monkeypatch.setattr("anyio.sleep", fake_sleep)

    args = argparse.Namespace(
        root=None,
        codex_root=str(tmp_path),
        source="codex",
        since=None,
        once=False,
        interval=0.01,
        live_window=300.0,
    )
    with pytest.raises(_Stop):
        await mirror_mod._run(args)
    # Iteration 1 attempted and failed, iteration 2 retried and succeeded,
    # iteration 3 stood down: exactly two attempts across three passes.
    assert len(attempts) == 2


async def test_complete_corrupt_header_settles_headerless_and_mirrors_the_body(tmp_path):
    """A newline-terminated first line that cannot parse is permanently corrupt
    (append-only file), not torn: the file settles as headerless and its valid
    body records still mirror instead of being suppressed forever."""
    from lionagi.cli.mirror import _FileState, _mirror_one_codex
    from lionagi.state.codex_mirror import session_db_id as codex_sid

    full = _codex_rollout_lines("ignored-uid", "x")
    body = "".join(full.splitlines(keepends=True)[1:])  # the two valid records

    corrupt = tmp_path / "rollout-corrupt-header.jsonl"
    corrupt.write_text('{"broken":\n' + body)  # complete but unparseable first line

    bad_utf8 = tmp_path / "rollout-bad-utf8-header.jsonl"
    bad_utf8.write_bytes(b"\xff\xfe garbage\n" + body.encode())

    # A BOM-shaped prefix surfaces as JSONDecodeError; a bare invalid byte
    # surfaces as UnicodeDecodeError — the body reader must survive both.
    bad_byte = tmp_path / "rollout-bad-byte-header.jsonl"
    bad_byte.write_bytes(b"\x80 invalid utf8\n" + body.encode())

    async with StateDB(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}") as db:
        for path in (corrupt, bad_utf8, bad_byte):
            state = _FileState(session_uid="")
            assert await _mirror_one_codex(db, path, state, {}) == 2
            assert state.head_checked
            assert state.session_uid == path.stem
            assert await db.get_session(codex_sid(path.stem)) is not None


async def test_valid_but_unterminated_header_stays_torn_until_the_newline(tmp_path):
    """A first line that parses as session_meta but has no trailing newline is
    still being written — appended bytes could extend or corrupt it — so the
    file defers instead of spending the identity fence on a provisional parse."""
    from lionagi.cli.mirror import _FileState, _mirror_one_codex, _peek_codex_head
    from lionagi.state.codex_mirror import session_db_id as codex_sid

    uid = "0199bbbb-0000-0000-0000-000000000007"
    full = _codex_rollout_lines(uid, "Codex Desktop")
    header_line = full.splitlines(keepends=True)[0]
    path = tmp_path / "rollout-unterminated-meta.jsonl"
    path.write_text(header_line.rstrip("\n"))  # valid JSON, newline not yet written

    assert _peek_codex_head(path) == ("torn", None)

    state = _FileState(session_uid="")
    async with StateDB(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}") as db:
        assert await _mirror_one_codex(db, path, state, {}) == 0
        assert not state.head_checked
        assert await db.get_session(codex_sid(path.stem)) is None

        path.write_text(full)  # the writer finished the header and the body
        assert await _mirror_one_codex(db, path, state, {}) == 2
        assert state.session_uid == uid
        assert await db.get_session(codex_sid(uid)) is not None


async def test_a_failed_absorption_does_not_retire_the_file(tmp_path, monkeypatch):
    """A contended teardown gives up rather than waiting, so absorption can fail
    for an ordinary reason. The file must stay eligible when it does.

    Marking the rollout absorbed before the absorption returns retires it on the
    early guard, and no later pass ever attempts the deletion again: a row nobody
    tears down, recorded as done. The discriminating assertion is the second
    attempt — with the flag set first, the observed attempt count is one.
    """
    from lionagi.cli import mirror as mirror_mod
    from lionagi.cli.mirror import _FileState, _mirror_one_codex

    uid = "0199bbbb-0000-0000-0000-00000000000f"
    path = tmp_path / "rollout-exec-contended.jsonl"
    path.write_text(_codex_rollout_lines(uid, "codex_exec"))
    state = _FileState(session_uid="")

    attempts = []

    async def flaky_absorb(db, rollout_uid):
        attempts.append(rollout_uid)
        if len(attempts) == 1:
            raise RuntimeError("lock not available")
        return True

    monkeypatch.setattr(mirror_mod, "absorb_orchestrated_session", flaky_absorb, raising=False)
    monkeypatch.setattr(
        "lionagi.state.codex_mirror.absorb_orchestrated_session", flaky_absorb, raising=False
    )

    async with StateDB(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}") as db:
        with pytest.raises(RuntimeError):
            await _mirror_one_codex(db, path, state, {})
        assert not state.orchestrated, (
            "the rollout was marked absorbed even though absorption failed, so no "
            "later pass will retry the teardown"
        )

        # The next sweep reaches absorption again and completes.
        assert await _mirror_one_codex(db, path, state, {}) == 0
        assert state.orchestrated

    assert len(attempts) == 2, f"absorption was attempted {len(attempts)} time(s), not 2"


async def test_a_failed_prior_stem_absorption_also_leaves_the_file_eligible(tmp_path, monkeypatch):
    """The orchestrated branch absorbs under two ids when the file was keyed by a
    stem before its header arrived. Failure-atomicity has to cover the second
    call as well as the first.

    The arm that matters is the id list on the retry. If any field were committed
    between the two calls, the retry would resolve `prior_uid` differently and the
    stem-keyed row would never be absorbed at all — a row nobody tears down, from
    a failure that looked like it had been retried.
    """
    from lionagi.cli import mirror as mirror_mod
    from lionagi.cli.mirror import _FileState, _mirror_one_codex

    uid = "0199bbbb-0000-0000-0000-000000000010"
    path = tmp_path / "rollout-exec-prior.jsonl"
    path.write_text(_codex_rollout_lines(uid, "codex_exec"))
    # A pre-header pass keyed this file by its stem.
    stem = path.stem
    state = _FileState(session_uid=stem)

    calls: list[str] = []

    async def flaky_absorb(db, rollout_uid):
        calls.append(rollout_uid)
        # Fail the SECOND call of the first pass, the prior-stem one.
        if len(calls) == 2:
            raise RuntimeError("lock not available")
        return True

    monkeypatch.setattr(mirror_mod, "absorb_orchestrated_session", flaky_absorb, raising=False)
    monkeypatch.setattr(
        "lionagi.state.codex_mirror.absorb_orchestrated_session", flaky_absorb, raising=False
    )

    async with StateDB(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}") as db:
        with pytest.raises(RuntimeError):
            await _mirror_one_codex(db, path, state, {})
        assert not state.orchestrated
        assert not state.head_checked
        assert state.session_uid == stem, (
            "session_uid was overwritten before both absorptions returned, so the "
            "retry can no longer tell which prior id this file was keyed by"
        )

        assert await _mirror_one_codex(db, path, state, {}) == 0
        assert state.orchestrated

    assert calls == [uid, stem, uid, stem], f"absorption ids across both passes: {calls}"


# Bounded preview + source pointer codec (mirror_spec.md)


def _source_line_for(raw: bytes, path: Path) -> SourceLine:
    from lionagi.state._mirror_common import SourceLine

    return SourceLine(
        value=json.loads(raw),
        source_path=str(path),
        source_offset=0,
        source_byte_count=len(raw),
        source_sha256=hashlib.sha256(raw).hexdigest(),
    )


def test_bound_mirror_content_default_truncates_long_instruction():
    from lionagi.state._mirror_common import bound_mirror_content

    long_text = "x" * 600
    content = {"instruction": long_text}
    line = SourceLine(
        value={},
        source_path="/tmp/t.jsonl",
        source_offset=0,
        source_byte_count=10,
        source_sha256="a" * 64,
    )
    preview, pointer = bound_mirror_content(
        content,
        "msg-1",
        line,
        source_kind="claude_jsonl",
        source_session_uid="sess-1",
        max_preview_chars=500,
    )
    assert preview == {"instruction": "x" * 500}
    assert pointer["truncated"] is True
    assert pointer["pointer_kind"] == "mirror_jsonl_v1"
    assert pointer["message_id"] == "msg-1"


def test_bound_mirror_content_zero_stores_empty_preview_with_valid_pointer():
    from lionagi.state._mirror_common import bound_mirror_content

    content = {"assistant_response": "hello world"}
    line = SourceLine(
        value={},
        source_path="/tmp/t.jsonl",
        source_offset=5,
        source_byte_count=20,
        source_sha256="b" * 64,
    )
    preview, pointer = bound_mirror_content(
        content,
        "msg-2",
        line,
        source_kind="claude_jsonl",
        source_session_uid="sess-1",
        max_preview_chars=0,
    )
    assert preview == {"assistant_response": ""}
    assert pointer["truncated"] is True
    assert pointer["source_offset"] == 5
    assert pointer["source_byte_count"] == 20


def test_bound_mirror_content_exact_boundary_not_truncated():
    from lionagi.state._mirror_common import bound_mirror_content

    text = "y" * 500
    line = SourceLine(
        value={},
        source_path="/tmp/t.jsonl",
        source_offset=0,
        source_byte_count=1,
        source_sha256="c" * 64,
    )
    preview, pointer = bound_mirror_content(
        {"assistant_response": text},
        "msg-3",
        line,
        source_kind="claude_jsonl",
        source_session_uid="sess-1",
        max_preview_chars=500,
    )
    assert preview == {"assistant_response": text}
    assert pointer["truncated"] is False


def test_bound_mirror_content_short_content_unchanged_and_not_truncated():
    from lionagi.state._mirror_common import bound_mirror_content

    line = SourceLine(
        value={},
        source_path="/tmp/t.jsonl",
        source_offset=0,
        source_byte_count=1,
        source_sha256="d" * 64,
    )
    preview, pointer = bound_mirror_content(
        {"assistant_response": "short"},
        "msg-4",
        line,
        source_kind="claude_jsonl",
        source_session_uid="sess-1",
        max_preview_chars=500,
    )
    assert preview == {"assistant_response": "short"}
    assert pointer["truncated"] is False


def test_bound_mirror_content_negative_budget_rejected():
    from lionagi.state._mirror_common import bound_mirror_content

    line = SourceLine(
        value={},
        source_path="/tmp/t.jsonl",
        source_offset=0,
        source_byte_count=1,
        source_sha256="e" * 64,
    )
    with pytest.raises(ValueError):
        bound_mirror_content(
            {"assistant_response": "x"},
            "msg-5",
            line,
            source_kind="claude_jsonl",
            source_session_uid="sess-1",
            max_preview_chars=-1,
        )


def test_bound_mirror_content_action_request_bounds_arguments_and_caps_function():
    from lionagi.state._mirror_common import bound_mirror_content, canonical_json

    content = {"function": "z" * 200, "arguments": {"payload": "w" * 900}}
    line = SourceLine(
        value={},
        source_path="/tmp/t.jsonl",
        source_offset=0,
        source_byte_count=1,
        source_sha256="f" * 64,
    )
    preview, pointer = bound_mirror_content(
        content,
        "msg-6",
        line,
        source_kind="claude_jsonl",
        source_session_uid="sess-1",
        max_preview_chars=500,
    )
    assert len(preview["function"]) == 128
    assert preview["arguments"]["_truncated"] is True
    assert pointer["truncated"] is True


def test_bound_mirror_content_metadata_merge_preserves_lion_class():
    from lionagi.state._mirror_common import bound_mirror_content

    line = SourceLine(
        value={},
        source_path="/tmp/t.jsonl",
        source_offset=0,
        source_byte_count=1,
        source_sha256="0" * 64,
    )
    _, pointer = bound_mirror_content(
        {"assistant_response": "hi"},
        "msg-7",
        line,
        source_kind="claude_jsonl",
        source_session_uid="sess-1",
        max_preview_chars=500,
    )
    node_metadata = {"lion_class": "AssistantResponse", "mirror_source": pointer}
    assert node_metadata["lion_class"] == "AssistantResponse"
    assert node_metadata["mirror_source"]["message_id"] == "msg-7"


def test_resolve_mirrored_content_legacy_row_without_pointer_unchanged():
    from lionagi.state._mirror_common import resolve_mirrored_content

    row = {"content": {"assistant_response": "hi"}, "node_metadata": {"lion_class": "X"}}
    resolved = resolve_mirrored_content(row)
    assert resolved.status == "legacy"
    assert resolved.content == row["content"]


def test_resolve_mirrored_content_round_trip_claude_assistant_response(tmp_path):
    """bound -> resolve reconstructs the exact full content via the real Claude
    adapter mapper, proving the pointer/hash/offset chain is internally consistent."""
    from lionagi.state._mirror_common import bound_mirror_content, resolve_mirrored_content

    session_uid = "sess-roundtrip"
    event = {
        "type": "assistant",
        "uuid": "evt-1",
        "timestamp": "2026-08-01T00:00:00Z",
        "message": {"content": [{"type": "text", "text": "z" * 700}]},
    }
    raw = json.dumps(event).encode("utf-8")
    path = tmp_path / "transcript.jsonl"
    path.write_bytes(raw + b"\n")

    msgs = messages_for_event(event, session_uid, {})
    assert len(msgs) == 1
    md = msgs[0].to_dict(mode="db")

    line = _source_line_for(raw, path)
    preview, pointer = bound_mirror_content(
        md["content"],
        md["id"],
        line,
        source_kind="claude_jsonl",
        source_session_uid=session_uid,
        max_preview_chars=500,
    )
    assert pointer["truncated"] is True
    row = {"content": preview, "node_metadata": {"mirror_source": pointer}}

    def reconstruct(record, source_session_uid, message_id):
        for m in messages_for_event(record, source_session_uid, {}):
            if str(m.id) == message_id:
                return m.to_dict(mode="db")["content"]
        return None

    resolved = resolve_mirrored_content(row, reconstruct=reconstruct)
    assert resolved.status == "resolved"
    assert resolved.content == md["content"]


def test_resolve_mirrored_content_source_missing_falls_back_to_preview(tmp_path):
    from lionagi.state._mirror_common import bound_mirror_content, resolve_mirrored_content

    path = tmp_path / "gone.jsonl"
    raw = b'{"a": 1}'
    path.write_bytes(raw)
    line = _source_line_for(raw, path)
    preview, pointer = bound_mirror_content(
        {"assistant_response": "hi"},
        "msg-8",
        line,
        source_kind="claude_jsonl",
        source_session_uid="sess-1",
        max_preview_chars=500,
    )
    path.unlink()
    row = {"content": preview, "node_metadata": {"mirror_source": pointer}}
    resolved = resolve_mirrored_content(row, reconstruct=lambda *a: None)
    assert resolved.status == "preview"
    assert resolved.reason == "source_missing"
    assert resolved.content == preview


def test_resolve_mirrored_content_source_replaced_hash_mismatch(tmp_path):
    from lionagi.state._mirror_common import bound_mirror_content, resolve_mirrored_content

    path = tmp_path / "replaced.jsonl"
    raw = b'{"a": 1}'
    path.write_bytes(raw)
    line = _source_line_for(raw, path)
    preview, pointer = bound_mirror_content(
        {"assistant_response": "hi"},
        "msg-9",
        line,
        source_kind="claude_jsonl",
        source_session_uid="sess-1",
        max_preview_chars=500,
    )
    path.write_bytes(b'{"a": 2}')  # same length, different bytes -> sha mismatch
    row = {"content": preview, "node_metadata": {"mirror_source": pointer}}
    resolved = resolve_mirrored_content(row, reconstruct=lambda *a: None)
    assert resolved.status == "preview"
    assert resolved.reason == "source_replaced"


def test_resolve_mirrored_content_short_read_falls_back_to_preview(tmp_path):
    from lionagi.state._mirror_common import bound_mirror_content, resolve_mirrored_content

    path = tmp_path / "short.jsonl"
    raw = b'{"a": 1}'
    path.write_bytes(raw)
    line = _source_line_for(raw, path)
    preview, pointer = bound_mirror_content(
        {"assistant_response": "hi"},
        "msg-10",
        line,
        source_kind="claude_jsonl",
        source_session_uid="sess-1",
        max_preview_chars=500,
    )
    path.write_bytes(raw[:3])  # now shorter than the recorded byte range
    row = {"content": preview, "node_metadata": {"mirror_source": pointer}}
    resolved = resolve_mirrored_content(row, reconstruct=lambda *a: None)
    assert resolved.status == "preview"
    assert resolved.reason == "short_read"


def test_resolve_mirrored_content_unsupported_pointer_version(tmp_path):
    from lionagi.state._mirror_common import resolve_mirrored_content

    row = {
        "content": {"assistant_response": "hi"},
        "node_metadata": {
            "mirror_source": {
                "pointer_kind": "mirror_jsonl_v2",
                "source_offset": 0,
                "source_byte_count": 1,
                "source_path": "/tmp/whatever.jsonl",
                "source_sha256": "0" * 64,
            }
        },
    }
    resolved = resolve_mirrored_content(row)
    assert resolved.status == "preview"
    assert resolved.reason == "unsupported_pointer"


def test_resolve_mirrored_content_no_message_match_falls_back(tmp_path):
    from lionagi.state._mirror_common import bound_mirror_content, resolve_mirrored_content

    path = tmp_path / "t.jsonl"
    raw = b'{"a": 1}'
    path.write_bytes(raw)
    line = _source_line_for(raw, path)
    preview, pointer = bound_mirror_content(
        {"assistant_response": "hi"},
        "msg-11",
        line,
        source_kind="claude_jsonl",
        source_session_uid="sess-1",
        max_preview_chars=500,
    )
    row = {"content": preview, "node_metadata": {"mirror_source": pointer}}
    resolved = resolve_mirrored_content(row, reconstruct=lambda *a: None)
    assert resolved.status == "preview"
    assert resolved.reason == "no_message_match"


def test_config_negative_preview_chars_rejected(monkeypatch):
    import importlib

    from lionagi.studio import config as config_mod

    monkeypatch.setenv("LIONAGI_STUDIO_MIRROR_PREVIEW_CHARS", "-1")
    with pytest.raises(ValueError):
        importlib.reload(config_mod)
    monkeypatch.delenv("LIONAGI_STUDIO_MIRROR_PREVIEW_CHARS", raising=False)
    importlib.reload(config_mod)


def test_config_omitted_preview_chars_defaults_to_500(monkeypatch):
    import importlib

    monkeypatch.delenv("LIONAGI_STUDIO_MIRROR_PREVIEW_CHARS", raising=False)
    from lionagi.studio import config as config_mod

    importlib.reload(config_mod)
    assert config_mod.MIRROR_PREVIEW_CHARS == 500


# Live-path mirror bounding: oversized ingestion through the three real
# writers (claude_mirror.py, codex_mirror.py, cli/mirror.py). A unit test on
# the codec alone (above) does not prove these are wired; each test here
# writes an oversized transcript to disk and reads the row back out of a real
# StateDB after it went through the module's own tailer, never constructing
# the pointer by hand.

_OVERSIZED_TEXT = "z" * 5000  # far beyond MIRROR_PREVIEW_CHARS default (500)


def _claude_oversized_file(root: Path, uid: str) -> Path:
    path = root / "-proj" / f"{uid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "type": "user",
        "uuid": f"{uid}-u",
        "timestamp": "2026-08-05T00:00:00.000Z",
        "sessionId": uid,
        "message": {"role": "user", "content": [{"type": "text", "text": _OVERSIZED_TEXT}]},
    }
    path.write_text(json.dumps(event) + "\n")
    return path


def _codex_oversized_file(root: Path, uid: str) -> Path:
    path = root / f"rollout-{uid}.jsonl"
    root.mkdir(parents=True, exist_ok=True)
    meta = {
        "type": "session_meta",
        "timestamp": "2026-08-05T00:00:00Z",
        "payload": {"id": uid, "session_id": uid, "cwd": "/x", "originator": "Codex Desktop"},
    }
    user = {
        "type": "response_item",
        "timestamp": "2026-08-05T00:00:01Z",
        "payload": {
            "type": "message",
            "role": "user",
            "id": "m1",
            "content": [{"text": _OVERSIZED_TEXT}],
        },
    }
    path.write_text("".join(json.dumps(e) + "\n" for e in (meta, user)))
    return path


@pytest.mark.asyncio
async def test_live_claude_ingest_bounds_oversized_message_via_one_pass(
    temp_db_path: Path, tmp_path: Path
) -> None:
    """`_one_pass` (claude_mirror.py's live ingestion sweep) must write a bounded
    preview plus a resolvable source pointer for an oversized message, not the
    raw unbounded text."""
    from lionagi.state._mirror_common import resolve_mirrored_content

    uid = "0199dddd-0000-0000-0000-000000000001"
    root = tmp_path / "projects"
    path = _claude_oversized_file(root, uid)

    async with StateDB() as db:
        await _one_pass(db, root, {}, {}, since=None, live_window=300)
        branch_id = _det(uid, "branch")
        rows = await db.get_branch_messages(branch_id)

    assert len(rows) == 1
    row = rows[0]
    stored = row["content"]["instruction"]
    assert stored != _OVERSIZED_TEXT
    assert len(stored) < len(_OVERSIZED_TEXT)
    pointer = row["node_metadata"]["mirror_source"]
    assert pointer["truncated"] is True
    assert pointer["source_path"] == str(path)
    assert pointer["pointer_kind"] == "mirror_jsonl_v1"

    def _reconstruct(record, session_uid, message_id):
        for m in messages_for_event(record, session_uid, {}):
            if str(m.id) == message_id:
                return m.to_dict(mode="db")["content"]
        return None

    resolved = resolve_mirrored_content(row, reconstruct=_reconstruct)
    assert resolved.status == "resolved"
    assert resolved.content["instruction"] == _OVERSIZED_TEXT


@pytest.mark.asyncio
async def test_live_codex_ingest_bounds_oversized_message_via_mirror_one_codex(
    tmp_path: Path,
) -> None:
    """`_mirror_one_codex` (codex_mirror.py's live ingestion path) must write a
    bounded preview plus a resolvable source pointer for an oversized message."""
    from lionagi.cli.mirror import _mirror_one_codex
    from lionagi.state._mirror_common import resolve_mirrored_content
    from lionagi.state.codex_mirror import _det as codex_det
    from lionagi.state.codex_mirror import messages_for_record

    uid = "0199dddd-0000-0000-0000-000000000002"
    root = tmp_path / "codex"
    path = _codex_oversized_file(root, uid)
    state = _FileState(session_uid="")

    async with StateDB(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}") as db:
        written = await _mirror_one_codex(db, path, state, {})
        branch_id = codex_det(uid, "branch")
        rows = await db.get_branch_messages(branch_id)

    assert written == 1
    assert len(rows) == 1
    row = rows[0]
    stored = row["content"]["instruction"]
    assert stored != _OVERSIZED_TEXT
    assert len(stored) < len(_OVERSIZED_TEXT)
    pointer = row["node_metadata"]["mirror_source"]
    assert pointer["truncated"] is True
    assert pointer["source_path"] == str(path)

    def _reconstruct(record, session_uid, message_id):
        for m in messages_for_record(record, session_uid, {}):
            if str(m.id) == message_id:
                return m.to_dict(mode="db")["content"]
        return None

    resolved = resolve_mirrored_content(row, reconstruct=_reconstruct)
    assert resolved.status == "resolved"
    assert resolved.content["instruction"] == _OVERSIZED_TEXT


@pytest.mark.asyncio
async def test_live_cli_mirror_run_once_bounds_oversized_message_end_to_end(
    temp_db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `li mirror --once` entry point (`_run` in cli/mirror.py) must produce
    a bounded, resolvable row through the full CLI codepath — argument parsing,
    state persistence and both the claude and codex passes — not just through
    the lower-level per-file helpers exercised by the other two live tests."""
    import argparse

    from lionagi.cli import mirror as mirror_mod
    from lionagi.state._mirror_common import resolve_mirrored_content

    monkeypatch.setattr(mirror_mod, "_OFFSETS_PATH", tmp_path / "offsets.json")

    uid = "0199dddd-0000-0000-0000-000000000003"
    root = tmp_path / "projects"
    path = _claude_oversized_file(root, uid)
    codex_root = tmp_path / "codex_root"
    codex_root.mkdir()

    args = argparse.Namespace(
        root=str(root),
        codex_root=str(codex_root),
        source="claude",
        since=None,
        once=True,
        interval=0.01,
        live_window=300.0,
    )
    rc = await mirror_mod._run(args)

    async with StateDB() as db:
        branch_id = _det(uid, "branch")
        rows = await db.get_branch_messages(branch_id)

    assert rc == 0
    assert len(rows) == 1
    row = rows[0]
    stored = row["content"]["instruction"]
    assert stored != _OVERSIZED_TEXT
    assert len(stored) < len(_OVERSIZED_TEXT)
    pointer = row["node_metadata"]["mirror_source"]
    assert pointer["truncated"] is True
    assert pointer["source_path"] == str(path)

    def _reconstruct(record, session_uid, message_id):
        for m in messages_for_event(record, session_uid, {}):
            if str(m.id) == message_id:
                return m.to_dict(mode="db")["content"]
        return None

    resolved = resolve_mirrored_content(row, reconstruct=_reconstruct)
    assert resolved.status == "resolved"
    assert resolved.content["instruction"] == _OVERSIZED_TEXT


@pytest.mark.asyncio
async def test_live_cli_mirror_run_once_relative_root_produces_resolvable_pointer(
    temp_db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative ``--root`` must still resolve to an absolute source pointer.

    Regression for the mirror review finding: ``_run`` only called
    ``expanduser()`` on ``args.root``, so a relative root stored a
    non-absolute ``source_path`` that ``resolve_mirrored_content`` rejects as
    ``unsupported_pointer`` even though the row itself was correctly bounded.
    """
    import argparse

    from lionagi.cli import mirror as mirror_mod
    from lionagi.state._mirror_common import resolve_mirrored_content

    monkeypatch.setattr(mirror_mod, "_OFFSETS_PATH", tmp_path / "offsets.json")
    monkeypatch.chdir(tmp_path)

    uid = "0199dddd-0000-0000-0000-000000000004"
    root = tmp_path / "relative-root" / "projects"
    path = _claude_oversized_file(root, uid)
    codex_root = tmp_path / "codex_root"
    codex_root.mkdir()

    args = argparse.Namespace(
        root="relative-root/projects",
        codex_root=str(codex_root),
        source="claude",
        since=None,
        once=True,
        interval=0.01,
        live_window=300.0,
    )
    rc = await mirror_mod._run(args)

    async with StateDB() as db:
        branch_id = _det(uid, "branch")
        rows = await db.get_branch_messages(branch_id)

    assert rc == 0
    assert len(rows) == 1
    row = rows[0]
    pointer = row["node_metadata"]["mirror_source"]
    assert Path(pointer["source_path"]).is_absolute()
    assert pointer["source_path"] == str(path.resolve())

    def _reconstruct(record, session_uid, message_id):
        for m in messages_for_event(record, session_uid, {}):
            if str(m.id) == message_id:
                return m.to_dict(mode="db")["content"]
        return None

    resolved = resolve_mirrored_content(row, reconstruct=_reconstruct)
    assert resolved.status == "resolved"
    assert resolved.content["instruction"] == _OVERSIZED_TEXT


def test_grep_evidence_live_mirror_paths_call_bound_mirror_content() -> None:
    """The bounding codec must not end up with zero callers: both writers call it directly and the CLI tailer threads the source-pointer data into every live write."""
    root = Path(__file__).resolve().parents[2] / "lionagi"
    codec_callers = {
        root / "state" / "claude_mirror.py": "claude_mirror.py",
        root / "state" / "codex_mirror.py": "codex_mirror.py",
    }
    missing = [
        label
        for path, label in codec_callers.items()
        if "bound_mirror_content" not in path.read_text()
    ]
    assert not missing, f"bound_mirror_content has zero callers in: {missing}"

    cli_text = (root / "cli" / "mirror.py").read_text()
    assert "max_preview_chars" in cli_text and "event_sources" in cli_text, (
        "cli/mirror.py does not thread the mirror bounding budget/source pointers "
        "into mirror_session()"
    )


async def test_an_imported_rollout_leaves_the_role_field_empty(tmp_path):
    """`agent_name` is a role field, and an imported desktop thread has no role.

    Writing the engine name there was wrong at the definition site, and it had
    a visible consequence: the role tier sits ahead of the prompt tier in
    `resolve_display_name`, so every imported row rendered the engine and
    shadowed the informative prompt-derived name the mirror had just computed.

    Both rows are asserted. The session and the branch each carried the label,
    so checking only the session would pass while the branch kept it.
    """
    from lionagi.cli.mirror import _FileState, _mirror_one_codex
    from lionagi.state.codex_mirror import session_db_id as codex_sid
    from lionagi.state.session_naming import resolve_display_name

    uid = "0199cccc-0000-0000-0000-00000000000b"
    path = tmp_path / "rollout-role.jsonl"
    path.write_text(_codex_rollout_lines(uid, "Codex Desktop"))
    state = _FileState(session_uid="")

    async with StateDB(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}") as db:
        assert await _mirror_one_codex(db, path, state, {}) > 0
        sid = codex_sid(uid)
        session = await db.get_session(sid)
        branches = await db.list_branches(sid)

    assert session is not None
    assert session["agent_name"] is None, (
        f"session carries role {session['agent_name']!r}; the engine is in a role field"
    )
    assert branches, "no branch was mirrored, so the branch assertion below proves nothing"
    for br in branches:
        assert br["agent_name"] is None, (
            f"branch carries role {br['agent_name']!r}; the session was cleared but its branch was not"
        )

    # The point of clearing the field: the prompt now reaches the display name.
    assert resolve_display_name(dict(session)) == "q"


# Mirror configuration refuses values it does not recognize. The flags below
# decide whether Studio reads the user's own transcript trees, so a value the
# parser cannot classify must stop startup rather than pick a side. Deciding by
# exclusion picked the reading side: anything that was not a known false
# spelling counted as true.


def _reload_config():
    import importlib

    from lionagi.studio import config as config_mod

    return importlib.reload(config_mod), config_mod


def _restore_config(monkeypatch):
    for var in (
        "LIONAGI_STUDIO_MIRROR_IMPORT_AMBIENT",
        "LIONAGI_STUDIO_MIRROR_CLAUDE",
        "LIONAGI_STUDIO_MIRROR_SOURCE",
        "LIONAGI_HOME",
    ):
        monkeypatch.delenv(var, raising=False)
    _reload_config()


@pytest.mark.parametrize("value", ["disabled", "none", "of", "off ,", "2", "yes please"])
def test_an_unrecognized_ambient_import_value_is_refused(monkeypatch, value):
    """The flagged case: these all read as ON under an exclusion test."""
    monkeypatch.setenv("LIONAGI_STUDIO_MIRROR_IMPORT_AMBIENT", value)
    try:
        with pytest.raises(ValueError, match="LIONAGI_STUDIO_MIRROR_IMPORT_AMBIENT"):
            _reload_config()
    finally:
        _restore_config(monkeypatch)


def test_an_unrecognized_mirror_enable_value_is_refused(monkeypatch):
    """Same construct, one flag over: the outer gate on the whole mirror."""
    monkeypatch.setenv("LIONAGI_STUDIO_MIRROR_CLAUDE", "disabled")
    try:
        with pytest.raises(ValueError, match="LIONAGI_STUDIO_MIRROR_CLAUDE"):
            _reload_config()
    finally:
        _restore_config(monkeypatch)


def test_an_unrecognized_mirror_source_is_refused(monkeypatch):
    """Same defect, different shape: this one fell back to the widest choice."""
    monkeypatch.setenv("LIONAGI_STUDIO_MIRROR_SOURCE", "cladue")
    try:
        with pytest.raises(ValueError, match="LIONAGI_STUDIO_MIRROR_SOURCE"):
            _reload_config()
    finally:
        _restore_config(monkeypatch)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0", False),
        ("false", False),
        ("no", False),
        ("off", False),
        ("", False),
        ("1", True),
        ("true", True),
        ("yes", True),
        ("on", True),
        ("ON", True),
        (" 1 ", True),
    ],
)
def test_recognized_spellings_still_decide_both_ways(monkeypatch, value, expected):
    """Regression guard, not a defect detector: these passed before the change too."""
    monkeypatch.setenv("LIONAGI_STUDIO_MIRROR_IMPORT_AMBIENT", value)
    try:
        config_mod, _ = _reload_config()
        assert config_mod.MIRROR_IMPORT_AMBIENT is expected
    finally:
        _restore_config(monkeypatch)


def test_an_unset_flag_still_takes_the_computed_default(monkeypatch, tmp_path):
    """An isolated LIONAGI_HOME opts out of ambient trees unless asked back in.

    This is the path the helper must not swallow: with the variable absent the
    default is computed, not parsed.
    """
    monkeypatch.delenv("LIONAGI_STUDIO_MIRROR_IMPORT_AMBIENT", raising=False)
    monkeypatch.setenv("LIONAGI_HOME", str(tmp_path / "isolated"))
    try:
        config_mod, _ = _reload_config()
        assert config_mod.MIRROR_IMPORT_AMBIENT is False
        # and an explicit opt-in still overrides that default
        monkeypatch.setenv("LIONAGI_STUDIO_MIRROR_IMPORT_AMBIENT", "1")
        config_mod, _ = _reload_config()
        assert config_mod.MIRROR_IMPORT_AMBIENT is True
    finally:
        _restore_config(monkeypatch)
