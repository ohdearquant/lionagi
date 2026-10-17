# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""`li agent` on an API model: lionagi's own tool loop, with the read set only.

An API model has no tool loop of its own, so a leg on one runs ``Branch.ReAct``
over the tools the profile names. The only tool set offered is the read set
(reader, search, context); tools that write files or run commands would run
with this process's own permissions, and this path has no sandbox for them.
The endpoint's call is scripted here, so no request leaves the process.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anyio
import pytest

from lionagi._errors import ConfigurationError
from lionagi.cli._providers import AgentProfile
from lionagi.providers.anthropic.messages import AnthropicMessagesEndpoint

MODEL = "anthropic/claude-haiku-5-5"


def _reply(content: dict) -> dict:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "content": [{"type": "text", "text": json.dumps(content)}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def _read_round(path: str) -> dict:
    return _reply(
        {
            "analysis": "read the notes first",
            "action_requests": [
                {"function": "reader", "arguments": {"action": "read", "path": path}}
            ],
            "action_strategy": "sequential",
            "extension_needed": True,
            "reason": {"title": "read", "content": "the answer is in the file"},
        }
    )


def _last_round() -> dict:
    return _reply(
        {
            "analysis": "the file says hello",
            "action_requests": [],
            "action_strategy": "sequential",
            "extension_needed": False,
            "reason": {"title": "done", "content": "nothing left to read"},
        }
    )


def _answer(text: str) -> dict:
    return _reply({"answer": text})


def _profile(**extra) -> AgentProfile:
    return AgentProfile(name="reader-leg", raw_body="You read files and answer.", extra=extra)


@pytest.fixture
def leg(monkeypatch, tmp_path):
    """Run `_run_agent` against a scripted Anthropic endpoint; return what it saw."""
    import lionagi.cli._runs as runs_mod
    import lionagi.cli.agent as agent_mod
    from lionagi.service.manager import iModelManager

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("hello from the notes\n")
    monkeypatch.setattr(runs_mod, "RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr(iModelManager, "shutdown", AsyncMock())
    monkeypatch.setattr(agent_mod, "setup_agent_persist", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_mod, "save_last_branch_pointer", lambda *a, **kw: None)
    monkeypatch.setattr(agent_mod, "resolve_artifact_contract", lambda **_: None)

    seen = SimpleNamespace(
        replies=[], payloads=[], branch=None, status=None, workspace=workspace, delay=None
    )

    async def teardown(ctx, *, status="completed", **kw):
        seen.status = status
        return status

    monkeypatch.setattr(agent_mod, "teardown_agent_persist", teardown)

    # Every Anthropic endpoint in the process is scripted, a resumed branch's
    # included: that one is rebuilt from its stored form, not by build_chat_model.
    async def call(self, request, *a, **kw):
        seen.payloads.append(request)
        if seen.delay is not None:
            await anyio.sleep(seen.delay)
        return seen.replies.pop(0)

    monkeypatch.setattr(AnthropicMessagesEndpoint, "call", call)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")

    import lionagi.agent.factory as factory_mod

    original = factory_mod.create_agent

    async def create_agent(*args, **kwargs):
        branch = await original(*args, **kwargs)
        seen.branch = branch
        return branch

    monkeypatch.setattr(factory_mod, "create_agent", create_agent)

    def run(profile: AgentProfile, prompt: str = "what do the notes say?", **kw):
        monkeypatch.setattr(agent_mod, "load_agent_profile", lambda name: profile)
        return agent_mod._run_agent(
            MODEL, prompt, agent_name=profile.name, cwd=str(workspace), **kw
        )

    seen.run = run
    return seen


@pytest.mark.asyncio
async def test_a_profile_with_the_read_set_runs_the_tool_loop_to_an_answer(leg):
    leg.replies = [_read_round("notes.txt"), _last_round(), _answer("the notes say hello")]

    result, provider, branch_id, status, _ = await leg.run(_profile(api_tools="read"))

    assert (result, provider, status) == ("the notes say hello", "anthropic", "completed")
    assert leg.replies == []
    responses = [m for m in leg.branch.msgs.messages if type(m).__name__ == "ActionResponse"]
    assert len(responses) == 1
    assert "hello from the notes" in json.dumps(responses[0].content.to_dict())
    assert branch_id == str(leg.branch.id)


@pytest.mark.asyncio
async def test_the_run_record_holds_every_message_of_the_loop_and_the_terminal_status(
    leg, monkeypatch, tmp_path
):
    import lionagi.cli._runs as runs_mod
    import lionagi.cli.agent as agent_mod
    from lionagi.state.db import StateDB

    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", tmp_path / "state.db")
    contexts = []

    async def setup(branch, **kw):
        ctx = await runs_mod.setup_agent_persist(branch, **kw)
        contexts.append(ctx)
        return ctx

    async def teardown(ctx, *, status="completed", **kw):
        leg.status = status
        return await runs_mod.teardown_agent_persist(ctx, status=status, **kw)

    monkeypatch.setattr(agent_mod, "setup_agent_persist", setup)
    monkeypatch.setattr(agent_mod, "teardown_agent_persist", teardown)
    leg.replies = [_read_round("notes.txt"), _last_round(), _answer("the notes say hello")]

    _, _, branch_id, status, _ = await leg.run(_profile(api_tools="read"))

    assert status == leg.status == "completed"
    assert len(contexts) == 1 and contexts[0] is not None
    async with StateDB() as db:
        session = await db.get_session(contexts[0]["session_id"])
        row = await db.get_branch(branch_id)
        stored = await db.get_branch_messages(branch_id)
    assert session["status"] == "completed"
    # The system message is stored on the branch row; the progression holds the rest.
    system_id = str(leg.branch.system.id)
    assert row["system_msg_id"] == system_id
    shown = [str(i) for i in leg.branch.progression if str(i) != system_id]
    assert [m["id"] for m in stored] == shown
    kinds = [type(leg.branch.messages[i]).__name__ for i in leg.branch.progression]
    assert kinds.count("ActionRequest") == kinds.count("ActionResponse") == 1
    assert kinds.count("AssistantResponse") == 3


@pytest.mark.asyncio
async def test_the_leg_is_offered_the_read_set_and_nothing_that_writes(leg):
    leg.replies = [_last_round(), _answer("done")]

    await leg.run(_profile(api_tools="read"))

    assert set(leg.branch.acts.registry) == {"reader", "search", "context"}
    assert leg.branch.metadata["api_launch"] == {"tools": "read", "max_extensions": 10}


@pytest.mark.asyncio
async def test_a_read_outside_the_working_directory_is_refused(leg, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("not for this leg\n")
    leg.replies = [_read_round(str(outside)), _last_round(), _answer("could not read it")]

    _, _, _, status, _ = await leg.run(_profile(api_tools="read"))

    assert status == "completed"
    responses = [m for m in leg.branch.msgs.messages if type(m).__name__ == "ActionResponse"]
    assert len(responses) == 1
    assert "not for this leg" not in json.dumps(responses[0].content.to_dict())


@pytest.mark.asyncio
async def test_a_search_does_not_reach_a_file_the_reader_refuses(leg):
    (leg.workspace / ".env").write_text("API_TOKEN=not-for-this-leg\n")
    search = _reply(
        {
            "analysis": "look for the token",
            "action_requests": [
                {"function": "search", "arguments": {"action": "grep", "pattern": "API_TOKEN"}}
            ],
            "action_strategy": "sequential",
            "extension_needed": True,
            "reason": {"title": "search", "content": "find the token"},
        }
    )
    leg.replies = [search, _last_round(), _answer("no token found")]

    _, _, _, status, _ = await leg.run(_profile(api_tools="read"))

    assert status == "completed"
    responses = [m for m in leg.branch.msgs.messages if type(m).__name__ == "ActionResponse"]
    assert len(responses) == 1
    assert "not-for-this-leg" not in json.dumps(responses[0].content.to_dict())
    sent = json.dumps(leg.payloads, default=str)
    assert "not-for-this-leg" not in sent


@pytest.mark.asyncio
async def test_the_round_bound_comes_from_the_profile(leg):
    rounds = [_read_round("notes.txt") for _ in range(3)]
    leg.replies = [*rounds, _answer("stopped at the bound")]

    result, _, _, status, _ = await leg.run(_profile(api_tools="read", max_extensions=2))

    assert (result, status) == ("stopped at the bound", "completed")
    assert leg.replies == []
    assert len(leg.payloads) == 4


@pytest.mark.asyncio
async def test_the_branch_is_written_where_a_resume_finds_it(leg):
    from lionagi.cli._runs import find_branch

    leg.replies = [_last_round(), _answer("first")]
    _, _, branch_id, _, _ = await leg.run(_profile(api_tools="read"))

    _, path = find_branch(branch_id)
    assert json.loads(path.read_text())["metadata"]["api_launch"]["tools"] == "read"

    leg.replies = [_last_round(), _answer("second")]
    result, _, resumed_id, status, _ = await leg.run(
        _profile(api_tools="read"), "and now?", resume=branch_id
    )

    assert (result, resumed_id, status) == ("second", branch_id, "completed")


@pytest.mark.asyncio
async def test_a_resumed_leg_calls_the_read_set_again(leg):
    from lionagi.cli._runs import find_branch

    leg.replies = [_last_round(), _answer("first")]
    _, _, branch_id, _, _ = await leg.run(_profile(api_tools="read"))

    leg.replies = [_read_round("notes.txt"), _last_round(), _answer("read it")]
    result, _, _, status, _ = await leg.run(
        _profile(api_tools="read"), "read the notes now", resume=branch_id
    )

    assert (result, status) == ("read it", "completed")
    _, path = find_branch(branch_id)
    # The tool result is in the stored branch only if the reader ran on resume.
    assert "hello from the notes" in path.read_text()


@pytest.mark.asyncio
async def test_display_flags_leave_an_api_model_on_its_own_key(leg):
    leg.replies = [_last_round(), _answer("done")]

    _, _, _, status, _ = await leg.run(_profile(api_tools="read"), verbose=True, theme="dark")

    assert status == "completed"
    from lionagi.service.imodel import iModel

    endpoint = leg.branch.chat_model.endpoint
    assert isinstance(endpoint, AnthropicMessagesEndpoint)
    # The key the spec alone resolves, not a placeholder a CLI model carries.
    assert endpoint.config._api_key == iModel(model=MODEL).endpoint.config._api_key
    assert endpoint.config._api_key != "dummy"
    assert "verbose_output" not in endpoint.config.kwargs


@pytest.mark.asyncio
async def test_a_turn_past_the_timeout_ends_timed_out(leg):
    leg.replies = [_last_round(), _answer("too late")]
    leg.delay = 5

    _, _, _, status, _ = await leg.run(_profile(api_tools="read"), timeout=1)

    assert status == "timed_out"


@pytest.mark.parametrize("value", ["coding", "write", True, None])
@pytest.mark.asyncio
async def test_a_tool_set_other_than_read_is_refused_before_a_run_exists(leg, monkeypatch, value):
    import lionagi.cli.agent as agent_mod

    monkeypatch.setattr(
        agent_mod, "allocate_run", lambda: pytest.fail("a run was allocated for a refused leg")
    )

    with pytest.raises(ConfigurationError, match="read set"):
        await leg.run(_profile(api_tools=value))


@pytest.mark.parametrize("value", [0, 101, "10", 2.5, True])
@pytest.mark.asyncio
async def test_a_round_bound_outside_one_to_a_hundred_is_refused(leg, value):
    with pytest.raises(ConfigurationError, match="max_extensions"):
        await leg.run(_profile(api_tools="read", max_extensions=value))


@pytest.mark.asyncio
async def test_a_profile_without_the_read_set_is_refused_as_before(leg, monkeypatch):
    import lionagi.cli.agent as agent_mod

    monkeypatch.setattr(
        agent_mod, "allocate_run", lambda: pytest.fail("a run was allocated for a refused leg")
    )

    with pytest.raises(ConfigurationError, match="only supports CLI endpoints") as exc:
        await leg.run(_profile())

    assert "api_tools: read" in str(exc.value)


@pytest.mark.asyncio
async def test_preset_coding_is_refused_for_an_api_model(leg):
    with pytest.raises(ConfigurationError, match="read set"):
        await leg.run(_profile(api_tools="read"), preset="coding")


@pytest.mark.asyncio
async def test_a_resumed_branch_without_the_launch_record_runs_as_before(leg, monkeypatch):
    from lionagi import Branch
    from lionagi.cli._runs import find_branch

    leg.replies = [_last_round(), _answer("first")]
    _, _, branch_id, _, _ = await leg.run(_profile(api_tools="read"))
    _, path = find_branch(branch_id)
    stored = json.loads(path.read_text())
    del stored["metadata"]["api_launch"]
    path.write_text(json.dumps(stored))

    operated = []

    async def operate(self, instruction=None, **kw):
        operated.append(kw)
        return "the earlier path"

    monkeypatch.setattr(Branch, "operate", operate)
    calls_before = len(leg.payloads)

    result, _, _, _, _ = await leg.run(_profile(api_tools="read"), "again", resume=branch_id)

    assert result == "the earlier path"
    assert operated and operated[0]["stream_persist"] is True
    assert len(leg.payloads) == calls_before


@pytest.mark.asyncio
async def test_a_queued_steer_runs_as_one_more_turn_of_the_tool_loop():
    from lionagi.cli.agent import _drain_pending_steers

    rows = [{"id": "c1", "verb": "message", "payload": {"text": "also check the readme"}}]

    class _Db:
        async def list_pending_session_controls(self, session_id):
            return list(rows)

        async def mark_session_control_applying(self, control_id, owner=None):
            rows.clear()
            return "claim"

        async def finalize_session_control(self, control_id, *, result, expect_claim):
            return True

    turns = []

    async def run_turn(instruction, timeout):
        turns.append((instruction, timeout))
        return "steered"

    class _Branch:
        async def operate(self, **kw):
            pytest.fail("an API leg's continuation must not be one model call")

    result = await _drain_pending_steers(
        {"session_id": "s", "db": _Db()},
        _Branch(),
        operate_kwargs={},
        deadline=None,
        run_turn=run_turn,
    )

    assert result == "steered"
    assert len(turns) == 1
    assert "also check the readme" in turns[0][0]
    assert turns[0][1] is None
