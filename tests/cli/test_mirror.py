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
from tests._mirror_test_support import _OVERSIZED_TEXT as _OVERSIZED_TEXT
from tests._mirror_test_support import SID as SID
from tests._mirror_test_support import _assistant as _assistant
from tests._mirror_test_support import _claude_oversized_file as _claude_oversized_file
from tests._mirror_test_support import _conversation as _conversation
from tests._mirror_test_support import _iso as _iso
from tests._mirror_test_support import _response_functions as _response_functions
from tests._mirror_test_support import _session_contents as _session_contents
from tests._mirror_test_support import _tool_result as _tool_result
from tests._mirror_test_support import _user_text as _user_text
from tests._mirror_test_support import temp_db_path as temp_db_path

# Event builders (verified Claude JSONL shapes)


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
    # The result consumed its use's name: the map holds only uses still awaiting a result.
    assert tool_names == {}


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


# watcher helpers: tailing + parsing


# project attribution fallback


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


# restart durability: derived state persisted with the cursor


@pytest.mark.asyncio
async def test_a_repeated_tool_result_in_a_later_batch_keeps_the_use_name(
    temp_db_path: Path,
) -> None:
    # The first result consumes the use's entry; the repeat shares its row and must not
    # blank the name the first one wrote. The name comes from the stored use row.
    use = _assistant("a1", [{"type": "tool_use", "id": "tool_1", "name": "Bash", "input": {}}])
    names: dict[str, str] = {}
    async with StateDB() as db:
        await mirror_session(
            db,
            session_uid=SID,
            events=[_user_text("u1", "run it"), use, _tool_result("u2", "tool_1", "first")],
            tool_names=names,
        )
        assert names == {}
        await mirror_session(
            db, session_uid=SID, events=[_tool_result("u3", "tool_1", "again")], tool_names=names
        )
        contents = await _session_contents(db)
    (response,) = [c for c in contents if "action_request_id" in c]
    assert response["function"] == "Bash"
    assert response["output"] == "again"


@pytest.mark.asyncio
async def test_a_repeated_tool_result_in_the_same_batch_keeps_the_use_name(
    temp_db_path: Path,
) -> None:
    use = _assistant("a1", [{"type": "tool_use", "id": "tool_1", "name": "Bash", "input": {}}])
    events = [
        _user_text("u1", "run it"),
        use,
        _tool_result("u2", "tool_1", "first"),
        _tool_result("u3", "tool_1", "again"),
    ]
    async with StateDB() as db:
        await mirror_session(db, session_uid=SID, events=events, tool_names={})
        contents = await _session_contents(db)
    assert _response_functions(contents) == ["Bash"]


@pytest.mark.asyncio
async def test_a_tool_result_whose_use_was_never_mirrored_keeps_an_empty_name(
    temp_db_path: Path,
) -> None:
    async with StateDB() as db:
        await mirror_session(
            db,
            session_uid=SID,
            events=[_user_text("u1", "hi"), _tool_result("u2", "tool_9", "out")],
            tool_names={},
        )
        contents = await _session_contents(db)
    assert _response_functions(contents) == [""]


@pytest.mark.asyncio
async def test_naming_unnamed_results_reads_the_store_once_per_chunk_of_500(
    temp_db_path: Path,
) -> None:
    """A pass whose results link to uses never mirrored must not open one read per result.

    Each such result needs the name off its use's stored row; the naming step must batch
    those lookups into one query per 500 ids rather than one ``get_message`` each."""
    from sqlalchemy import event

    events = [_user_text("u1", "hi")] + [
        _tool_result(f"u{i + 2}", f"tool_{i}", f"out {i}") for i in range(40)
    ]
    async with StateDB() as db:
        reads: list[str] = []

        def _spy(conn, cursor, statement, parameters, context, executemany):
            upper = statement.lstrip().upper()
            if upper.startswith("SELECT") and "FROM MESSAGES" in upper:
                reads.append(statement)

        event.listen(db._engine.sync_engine, "before_cursor_execute", _spy)
        try:
            await mirror_session(db, session_uid=SID, events=events, tool_names={})
        finally:
            event.remove(db._engine.sync_engine, "before_cursor_execute", _spy)
        contents = await _session_contents(db)

    assert _response_functions(contents) == ["" for _ in range(40)]
    assert len(reads) == 1


@pytest.mark.asyncio
async def test_a_repeated_results_pointer_covers_the_named_row(
    temp_db_path: Path, tmp_path: Path
) -> None:
    """The source pointer of a repeated result must hash the row as stored, name included.

    The name is recovered from the use's stored row after the source pointer is built; a
    pointer hashing the pre-naming content would let a reader reconstruct an unnamed
    ActionResponse and accept it as the full content."""
    from sqlalchemy import event

    from lionagi.state._mirror_common import resolve_mirrored_content

    use = _assistant("a1", [{"type": "tool_use", "id": "tool_1", "name": "Bash", "input": {}}])
    first = _tool_result("u2", "tool_1", "first")
    repeat = _tool_result("u3", "tool_1", "again")
    path = tmp_path / "t.jsonl"

    def _write(lines: list[dict]) -> list[tuple[int, int, str]]:
        sources: list[tuple[int, int, str]] = []
        offset = 0
        blob = b""
        for line in lines:
            raw = (json.dumps(line) + "\n").encode()
            sources.append((offset, len(raw) - 1, hashlib.sha256(raw[:-1]).hexdigest()))
            offset += len(raw)
            blob += raw
        path.write_bytes(blob)
        return sources

    events = [_user_text("u1", "run it"), use, first]
    sources = _write(events)
    async with StateDB() as db:
        await mirror_session(
            db,
            session_uid=SID,
            events=events,
            tool_names={},
            source_path=str(path),
            event_sources=sources,
            max_preview_chars=500,
        )
        sources = _write([*events, repeat])[-1:]
        await mirror_session(
            db,
            session_uid=SID,
            events=[repeat],
            tool_names={},
            source_path=str(path),
            event_sources=sources,
            max_preview_chars=500,
        )
        row = await db.get_message(_det(SID, "toolresp", "tool_1"))

    assert row["content"]["function"] == "Bash"

    def _reconstruct(record, session_uid, message_id):
        for m in messages_for_event(record, session_uid, {}):
            if str(m.id) == message_id:
                return m.to_dict(mode="db")["content"]
        return None

    resolved = resolve_mirrored_content(row, reconstruct=_reconstruct)
    assert resolved.content["function"] == "Bash"


@pytest.mark.asyncio
async def test_a_mirror_pass_rewrites_each_progression_once(temp_db_path: Path) -> None:
    from sqlalchemy import event

    events = [_user_text("u1", "start")] + [
        _assistant(f"a{i}", [{"type": "text", "text": f"step {i}"}]) for i in range(6)
    ]
    async with StateDB() as db:
        updates: list[str] = []

        def _spy(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("UPDATE PROGRESSIONS"):
                updates.append(statement)

        event.listen(db._engine.sync_engine, "before_cursor_execute", _spy)
        written = await mirror_session(
            db, session_uid=SID, events=events, tool_names={}, status="running"
        )
        event.remove(db._engine.sync_engine, "before_cursor_execute", _spy)
        row = await db.get_session(session_db_id(SID))
        session_ids = await db.get_progression(row["progression_id"])
    assert written == 7
    assert len(session_ids) == 7
    # One rewrite of the branch progression and one of the session progression for the
    # whole batch, where the per-message path issued one of each per message.
    assert len(updates) == 2


# Orchestrated codex rollouts (headless `codex exec`)


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

    async def fake_codex_pass(db, root, states, *, since, live_window, threads):
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
    monkeypatch.setattr(mirror_mod, "_save_states", lambda states, previous=None: None)
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


# Bounded preview + source pointer codec


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
    from lionhub.serve import mirror_settings

    monkeypatch.setenv("LIONAGI_STUDIO_MIRROR_PREVIEW_CHARS", "-1")
    with pytest.raises(ValueError):
        importlib.reload(mirror_settings)
        importlib.reload(config_mod)
    monkeypatch.delenv("LIONAGI_STUDIO_MIRROR_PREVIEW_CHARS", raising=False)
    importlib.reload(mirror_settings)
    importlib.reload(config_mod)


def test_config_omitted_preview_chars_defaults_to_500(monkeypatch):
    import importlib

    monkeypatch.delenv("LIONAGI_STUDIO_MIRROR_PREVIEW_CHARS", raising=False)
    from lionagi.studio import config as config_mod
    from lionhub.serve import mirror_settings

    importlib.reload(mirror_settings)
    importlib.reload(config_mod)
    assert config_mod.MIRROR_PREVIEW_CHARS == 500


# Live-path mirror bounding: oversized ingestion through the three real
# writers (claude_mirror.py, codex_mirror.py, lionhub/src/lionhub/serve/mirror.py). A unit test on
# the codec alone (above) does not prove these are wired; each test here
# writes an oversized transcript to disk and reads the row back out of a real
# StateDB after it went through the module's own tailer, never constructing
# the pointer by hand.


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
    from lionhub.serve import mirror as service_mod

    monkeypatch.setattr(service_mod, "_OFFSETS_PATH", tmp_path / "offsets.json")

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
    from lionhub.serve import mirror as service_mod

    monkeypatch.setattr(service_mod, "_OFFSETS_PATH", tmp_path / "offsets.json")
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
    """The bounding codec must not end up with zero callers: both writers call it directly and the mirror service threads the source-pointer data into every live write."""
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

    service_text = (root.parent / "lionhub" / "src" / "lionhub" / "serve" / "mirror.py").read_text()
    assert "max_preview_chars" in service_text and "event_sources" in service_text, (
        "lionhub/src/lionhub/serve/mirror.py does not thread the mirror bounding budget/source pointers "
        "into mirror_session()"
    )


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


@pytest.mark.asyncio
async def test_a_result_takes_a_name_only_from_a_stored_use_row() -> None:
    from lionagi.protocols.messages.action_request import ActionRequest
    from lionagi.protocols.messages.action_response import ActionResponse
    from lionagi.state._mirror_common import name_answered_results

    def _stored(message) -> dict:
        # The shape StateDB.get_messages returns: content as to_dict(mode="db") left
        # it and the resolved class of the row as lion_class_str.
        row = message.to_dict(mode="db")
        return {"content": row["content"], "lion_class_str": message.class_name(full=True)}

    class _Store:
        async def get_messages(self, ids):
            rows = {
                "use": _stored(ActionRequest(content={"function": "Bash", "arguments": {}})),
                "result": _stored(
                    ActionResponse(
                        content={"function": "Edit", "output": "x", "action_request_id": None}
                    )
                ),
                # An ActionResponse whose output is None keeps arguments: {} once
                # to_dict(mode="db") drops the field, so it looks like a use by its fields
                # alone; only its recorded class tells it apart.
                "silent": _stored(
                    ActionResponse(
                        content={"function": "Edit", "output": None, "action_request_id": None}
                    )
                ),
                # A row with no recorded class (an older or non-mirror writer) is told
                # apart by its fields: no output and no arguments means it is no use.
                "bare": {"content": {"function": "Bash"}},
                "unclassed_use": {"content": {"function": "Grep", "arguments": {}}},
            }
            return {i: rows[i] for i in ids if i in rows}

    rows = [
        {"id": "r1", "content": {"function": "", "output": "a", "action_request_id": "use"}},
        {"id": "r2", "content": {"function": "", "output": "b", "action_request_id": "result"}},
        {"id": "r3", "content": {"function": "", "output": "c", "action_request_id": "silent"}},
        {"id": "r4", "content": {"function": "", "output": "d", "action_request_id": "bare"}},
        {
            "id": "r5",
            "content": {"function": "", "output": "e", "action_request_id": "unclassed_use"},
        },
    ]
    await name_answered_results(_Store(), rows)
    assert [row["content"]["function"] for row in rows] == ["Bash", "", "", "", "Grep"]


@pytest.mark.asyncio
async def test_a_stored_use_row_with_no_recorded_class_lends_its_name(temp_db_path: Path) -> None:
    """StateDB reads a row stored without a class back as ``__unknown__``, which is unclassed.

    Such a row is judged by its fields, so a use row (function and arguments, no output)
    still names the result linked to it.
    """
    from lionagi.state._mirror_common import name_answered_results

    async with StateDB() as db:
        await db.insert_message(
            {
                "id": "U1",
                "created_at": 1.0,
                "role": "assistant",
                "content": {"function": "Grep", "arguments": {}},
            }
        )
        stored = await db.get_messages(["U1"])
        rows = [{"id": "L4", "content": {"function": "", "output": "x", "action_request_id": "U1"}}]
        await name_answered_results(db, rows)
    assert stored["U1"]["lion_class_str"] == "__unknown__"
    assert rows[0]["content"]["function"] == "Grep"


@pytest.mark.asyncio
async def test_a_stored_row_without_arguments_is_not_a_use() -> None:
    """A result may take a name only from a stored use row, and a use row carries arguments.

    A stored row with a function name and no arguments (and no recorded class) is not a
    use row, so a result linked to it keeps the empty name rather than taking that name.
    """
    from lionagi.state._mirror_common import name_answered_results

    class _Store:
        async def get_messages(self, ids):
            rows = {"naked": {"content": {"function": "Bash"}}}
            return {i: rows[i] for i in ids if i in rows}

    rows = [
        {"id": "r1", "content": {"function": "", "output": "a", "action_request_id": "naked"}},
    ]
    await name_answered_results(_Store(), rows)
    assert rows[0]["content"]["function"] == ""


def test_mirror_service_is_defined_in_its_own_module() -> None:
    """The tail loop and its helpers live in the service module; the command module re-exports them."""
    import lionhub.serve.mirror as service
    from lionagi.cli import mirror

    assert service.mirror_forever.__module__ == "lionhub.serve.mirror"
    assert service._FileState.__module__ == "lionhub.serve.mirror"
    assert mirror.mirror_forever is service.mirror_forever
    assert mirror._FileState is service._FileState
    assert mirror._one_pass is service._one_pass
    assert mirror.CLAUDE_PROJECTS_DIR == service.CLAUDE_PROJECTS_DIR
    assert mirror.CODEX_SESSIONS_DIR == service.CODEX_SESSIONS_DIR


def test_studio_preview_limit_comes_from_the_settings_module() -> None:
    """The studio config and the mirror service read one preview limit, defined in one place."""
    import lionhub.serve.mirror as service
    from lionagi.studio import config
    from lionhub.serve import mirror_settings

    assert config.MIRROR_PREVIEW_CHARS == mirror_settings.MIRROR_PREVIEW_CHARS
    assert service.MIRROR_PREVIEW_CHARS == mirror_settings.MIRROR_PREVIEW_CHARS


def test_importing_the_studio_config_does_not_load_the_mirror_service() -> None:
    """A fresh interpreter that imports the studio config loads the settings module only."""
    import subprocess
    import sys

    probe = (
        "import sys, lionagi.studio.config; "
        "print(' '.join(sorted(m for m in sys.modules if m.startswith(('lionagi.cli', 'lionhub.serve')))))"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True, timeout=120
    )
    loaded = set(out.stdout.split())
    assert "lionhub.serve.mirror_settings" in loaded
    assert {"lionhub.serve.mirror", "lionagi.cli.mirror"}.isdisjoint(loaded)
