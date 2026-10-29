# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""`li agent` on an API model: lionagi's own tool loop over the read set.

An API model has no tool loop of its own, so a leg on one runs ``Branch.ReAct``
over the tools the profile names. The read set (reader, search, context) runs
with this process's own permissions; the write set, whose editor and bash run
under the macOS sandbox, is covered in test_agent_api_write_set.py.
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


def _text(text: str) -> dict:
    reply = _reply({})
    reply["content"] = [{"type": "text", "text": text}]
    return reply


_LNDL_READ = '```lndl\n<lact r>reader(action="read", path="notes.txt")</lact>\n```'
_LNDL_RESULT_AS_ANSWER = "```lndl\nOUT{answer: [r]}\n```"
_LNDL_READ_AND_GUESS = (
    '```lndl\n<lact r>reader(action="read", path="notes.txt")</lact>\n'
    "<lvar a>a guess</lvar>\nOUT{answer: [a]}\n```"
)
_LNDL_MISSING = "```lndl\nOUT{answer: [missing]}\n```"


def _lndl_answer(text: str) -> dict:
    return _text(f"```lndl\n<lvar a>{text}</lvar>\nOUT{{answer: [a]}}\n```")


def _last_user_text(payload: dict) -> str:
    for message in reversed(payload.get("messages") or []):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


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
        replies=[],
        payloads=[],
        branch=None,
        status=None,
        exception=None,
        workspace=workspace,
        delay=None,
    )

    async def teardown(ctx, *, status="completed", **kw):
        seen.status = status
        seen.exception = kw.get("exception")
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

    def run(profile: AgentProfile, prompt: str = "what do the notes say?", model=MODEL, **kw):
        monkeypatch.setattr(agent_mod, "load_agent_profile", lambda name: profile)
        return agent_mod._run_agent(
            model, prompt, agent_name=profile.name, cwd=str(workspace), **kw
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
    record = leg.branch.metadata["api_launch"]
    assert set(record) == {
        "tools",
        "network",
        "max_extensions",
        "loop",
        "max_input_tokens",
        "spend",
        "tool_result_max_chars",
        "context_fold_at",
        "context_fold_target",
        "context_fold_tail_margin",
        "prompt_caching",
    }
    assert all(
        record[key] is None
        for key in (
            "tool_result_max_chars",
            "context_fold_at",
            "context_fold_target",
            "context_fold_tail_margin",
            "prompt_caching",
        )
    )
    assert (record["tools"], record["max_extensions"], record["loop"]) == ("read", 10, "react")
    assert record["max_input_tokens"] == "auto"
    assert record["network"] is False


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


@pytest.mark.parametrize("value", ["coding", "Write", True, None, ["read"]])
@pytest.mark.asyncio
async def test_a_tool_set_other_than_read_or_write_is_refused_before_a_run_exists(
    leg, monkeypatch, value
):
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
    with pytest.raises(ConfigurationError, match="--preset coding binds tools"):
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
        operated.append((self, kw))
        return "the earlier path"

    monkeypatch.setattr(Branch, "operate", operate)
    calls_before = len(leg.payloads)

    result, _, _, _, _ = await leg.run(
        _profile(api_tools="read"), "again", resume=branch_id, verbose=True, theme="dark"
    )

    assert result == "the earlier path"
    assert operated and operated[0][1]["stream_persist"] is True
    assert len(leg.payloads) == calls_before
    # Off the tool loop, the display flags reach the model's options as before.
    options = operated[0][0].chat_model.endpoint.config.kwargs
    assert options["verbose_output"] is True
    assert options["cli_display_theme"] == "dark"


@pytest.mark.parametrize(
    "record",
    [
        {"tools": "coding", "max_extensions": 5},
        {"tools": ["read"], "max_extensions": 5},
        {"tools": "read"},
        {"tools": "read", "max_extensions": "5"},
        {"tools": "read", "max_extensions": True},
        {"tools": "read", "max_extensions": 0},
        {"tools": "read", "max_extensions": 5, "loop": "batch"},
        {"tools": "read", "max_extensions": 5, "loop": None},
        {"tools": "read", "max_extensions": 5, "loop": "LNDL"},
        {"tools": "read", "max_extensions": 5, "network": True},
        {"tools": "write", "max_extensions": 5, "network": "yes"},
        "read",
        None,
    ],
)
@pytest.mark.parametrize("with_profile", [True, False], ids=["profile", "no-profile"])
@pytest.mark.asyncio
async def test_a_stored_record_this_version_cannot_run_is_refused_before_a_run_exists(
    leg, monkeypatch, record, with_profile
):
    import lionagi.cli.agent as agent_mod
    from lionagi.cli._runs import find_branch

    leg.replies = [_last_round(), _answer("first")]
    _, _, branch_id, _, _ = await leg.run(_profile(api_tools="read"))
    _, path = find_branch(branch_id)
    stored = json.loads(path.read_text())
    stored["metadata"]["api_launch"] = record
    path.write_text(json.dumps(stored))
    monkeypatch.setattr(
        agent_mod, "allocate_run", lambda: pytest.fail("a run was allocated for a refused leg")
    )

    with pytest.raises(ConfigurationError, match="stored launch record"):
        if with_profile:
            await leg.run(_profile(api_tools="read"), "again", resume=branch_id)
        else:
            await agent_mod._run_agent(
                MODEL, "again", agent_name=None, cwd=str(leg.workspace), resume=branch_id
            )


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


@pytest.mark.asyncio
async def test_the_read_set_is_guarded_to_the_working_directory(tmp_path):
    import lionagi.cli.agent as agent_mod

    spec = agent_mod._make_api_agent_spec(
        cwd=str(tmp_path), effort=None, system_prompt=None, role="reviewer", tools="read"
    )

    assert spec.coding_tools == ("reader", "search", "context")
    (guard,) = spec.hook_handlers["security_pre:reader"]
    assert await guard("reader", "read", {"path": "notes.txt"}) is None
    with pytest.raises(PermissionError):
        await guard("reader", "read", {"path": str(tmp_path.parent / "outside.txt")})


@pytest.mark.parametrize("resumed", [False, True], ids=["new", "resumed"])
@pytest.mark.asyncio
async def test_an_image_is_refused_for_an_api_model_before_a_run_exists(leg, monkeypatch, resumed):
    import lionagi.cli.agent as agent_mod

    kwargs = {}
    if resumed:
        leg.replies = [_last_round(), _answer("first")]
        _, _, kwargs["resume"], _, _ = await leg.run(_profile(api_tools="read"))
    monkeypatch.setattr(
        agent_mod, "allocate_run", lambda: pytest.fail("a run was allocated for a refused leg")
    )

    with pytest.raises(ConfigurationError, match="--image"):
        await leg.run(_profile(api_tools="read"), "look at this", images=["a.png"], **kwargs)


@pytest.mark.asyncio
async def test_a_resume_under_a_profile_without_the_read_set_is_refused(leg, monkeypatch):
    import lionagi.cli.agent as agent_mod

    leg.replies = [_last_round(), _answer("first")]
    _, _, branch_id, _, _ = await leg.run(_profile(api_tools="read"))
    monkeypatch.setattr(
        agent_mod, "allocate_run", lambda: pytest.fail("a run was allocated for a refused leg")
    )

    with pytest.raises(ConfigurationError, match="declares no api_tools"):
        await leg.run(_profile(), "and now?", resume=branch_id)


@pytest.mark.asyncio
async def test_a_resume_without_a_profile_runs_the_loop_from_the_record(leg):
    import lionagi.cli.agent as agent_mod

    leg.replies = [_last_round(), _answer("first")]
    _, _, branch_id, _, _ = await leg.run(_profile(api_tools="read"))

    leg.replies = [_read_round("notes.txt"), _last_round(), _answer("read it")]
    result, _, _, status, _ = await agent_mod._run_agent(
        MODEL, "read the notes now", agent_name=None, cwd=str(leg.workspace), resume=branch_id
    )

    assert (result, status) == ("read it", "completed")
    assert leg.replies == []


@pytest.mark.asyncio
async def test_a_resume_takes_the_round_bound_its_profile_names_now(leg):
    leg.replies = [_last_round(), _answer("first")]
    _, _, branch_id, _, _ = await leg.run(_profile(api_tools="read", max_extensions=10))

    leg.payloads.clear()
    leg.replies = [*(_read_round("notes.txt") for _ in range(3)), _answer("stopped at the bound")]
    result, _, _, status, _ = await leg.run(
        _profile(api_tools="read", max_extensions=2), "again", resume=branch_id
    )

    assert (result, status) == ("stopped at the bound", "completed")
    assert leg.replies == []
    assert len(leg.payloads) == 4


@pytest.mark.asyncio
async def test_display_flags_on_a_resume_stay_off_the_api_request(leg):
    leg.replies = [_last_round(), _answer("first")]
    _, _, branch_id, _, _ = await leg.run(_profile(api_tools="read"))

    leg.payloads.clear()
    leg.replies = [_last_round(), _answer("second")]
    result, _, _, status, _ = await leg.run(
        _profile(api_tools="read"), "and now?", resume=branch_id, verbose=True, theme="dark"
    )

    assert (result, status) == ("second", "completed")
    for payload in leg.payloads:
        assert "verbose_output" not in json.dumps(payload, default=str)
        assert "cli_display_theme" not in json.dumps(payload, default=str)


def _failed_read_then_good_read() -> dict:
    return _reply(
        {
            "analysis": "try the old name, then the notes",
            "action_requests": [
                {"function": "reader", "arguments": {"action": "read", "path": "missing.txt"}},
                {"function": "reader", "arguments": {"action": "read", "path": "notes.txt"}},
            ],
            "action_strategy": "sequential",
            "extension_needed": True,
            "reason": {"title": "read", "content": "one of these holds the answer"},
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "khive_injection",
    [True, {"writeback": {"enabled": True, "salience_cap": 0.4}}],
    ids=["opt-in", "writeback-on"],
)
async def test_an_api_leg_recalls_from_khive_but_writes_no_read_output_back(
    leg, monkeypatch, khive_injection
):
    import lionagi.tools.khive_injection as injection_mod

    monkeypatch.delenv("LIONAGI_KHIVE_INJECTION", raising=False)
    ops_seen: list[str] = []

    async def call_khive(ops, mcp_config):
        ops_seen.append(ops)
        tool = ops.split("(", 1)[0]
        result = [] if tool == "memory.recall" else {"id": "mem-1", "kind": "memory"}
        return {"results": [{"ok": True, "tool": tool, "result": result}]}

    monkeypatch.setattr(injection_mod, "_call_khive", call_khive)
    profile = _profile(api_tools="read")
    profile.khive_injection = khive_injection
    leg.replies = [_failed_read_then_good_read(), _last_round(), _answer("the notes say hello")]

    result, _, _, status, _ = await leg.run(profile)

    assert (result, status) == ("the notes say hello", "completed")
    assert any(op.startswith("memory.recall(") for op in ops_seen)
    assert not any(op.startswith("memory.remember(") for op in ops_seen)
    assert "hello from the notes" not in "".join(ops_seen)


# What a turn spent (ADR-0132 D3) and the batched LNDL loop (D1, D2).


def _capture_hints(monkeypatch) -> list[str]:
    import lionagi.cli.agent as agent_mod

    lines: list[str] = []
    monkeypatch.setattr(agent_mod, "hint", lines.append)
    return lines


@pytest.mark.asyncio
async def test_a_react_turn_records_and_prints_what_it_spent(leg, monkeypatch):
    from lionagi.cli._runs import find_branch

    hints = _capture_hints(monkeypatch)
    leg.replies = [_read_round("notes.txt"), _last_round(), _answer("the notes say hello")]

    _, _, branch_id, status, _ = await leg.run(_profile(api_tools="read"))

    assert status == "completed"
    spend = leg.branch.metadata["api_launch"]["spend"]
    assert (spend["requests"], spend["tool_calls"]) == (3, 1)
    assert isinstance(spend["wall_seconds"], float) and spend["wall_seconds"] >= 0
    assert f"[api] spend: 3 request(s), 1 tool call(s), {spend['wall_seconds']:.1f}s" in hints
    _, path = find_branch(branch_id)
    assert json.loads(path.read_text())["metadata"]["api_launch"]["spend"] == spend


@pytest.mark.asyncio
async def test_a_resumed_turn_counts_only_its_own_spend(leg):
    from lionagi.cli._runs import find_branch

    leg.replies = [_read_round("notes.txt"), _last_round(), _answer("first")]
    _, _, branch_id, _, _ = await leg.run(_profile(api_tools="read"))
    first = leg.branch.metadata["api_launch"]["spend"]
    assert (first["requests"], first["tool_calls"]) == (3, 1)

    leg.replies = [_read_round("notes.txt"), _last_round(), _answer("read it")]
    await leg.run(_profile(api_tools="read"), "read the notes now", resume=branch_id)

    # A resume rebuilds the branch from its stored form, so the record is read there.
    _, path = find_branch(branch_id)
    spend = json.loads(path.read_text())["metadata"]["api_launch"]["spend"]
    assert (spend["requests"], spend["tool_calls"]) == (3, 1)


@pytest.mark.asyncio
async def test_a_second_turn_in_the_same_invocation_adds_to_its_spend(leg, tmp_path):
    """A queued steer runs one more turn on the same branch and the same model; the
    record totals the invocation, each turn counted once from its own baseline."""
    import lionagi.cli.agent as agent_mod

    leg.replies = [_read_round("notes.txt"), _last_round(), _answer("first")]
    await leg.run(_profile(api_tools="read"))
    assert leg.branch.metadata["api_launch"]["spend"]["requests"] == 3

    leg.replies = [_read_round("notes.txt"), _last_round(), _answer("second")]
    result = await agent_mod._run_api_turn(
        leg.branch,
        "read them again",
        timeout=None,
        max_extensions=10,
        snapshot_dir=str(tmp_path / "branches"),
    )

    assert result == "second"
    spend = leg.branch.metadata["api_launch"]["spend"]
    assert (spend["requests"], spend["tool_calls"]) == (6, 2)


@pytest.mark.asyncio
async def test_a_turn_that_fails_still_records_its_spend(leg):
    from lionagi.cli._runs import find_branch

    leg.replies = [_read_round("notes.txt")]

    with pytest.raises(Exception):  # noqa: B017 — the script runs out; any error ends the turn
        await leg.run(_profile(api_tools="read"))

    assert leg.status == "failed"
    spend = leg.branch.metadata["api_launch"]["spend"]
    assert (spend["requests"], spend["tool_calls"]) == (2, 1)
    _, path = find_branch(str(leg.branch.id))
    assert json.loads(path.read_text())["metadata"]["api_launch"]["spend"] == spend


@pytest.mark.asyncio
async def test_an_lndl_leg_reads_then_answers_in_two_requests(leg):
    leg.replies = [_text(_LNDL_READ), _lndl_answer("the notes say hello")]

    result, _, _, status, _ = await leg.run(_profile(api_tools="read", api_loop="lndl"))

    assert (result, status) == ("the notes say hello", "completed")
    assert leg.replies == []
    record = leg.branch.metadata["api_launch"]
    assert record["loop"] == "lndl"
    assert (record["spend"]["requests"], record["spend"]["tool_calls"]) == (2, 1)
    responses = [m for m in leg.branch.msgs.messages if type(m).__name__ == "ActionResponse"]
    assert len(responses) == 1
    assert "hello from the notes" in json.dumps(responses[0].content.to_dict())
    # Every round names the tools a <lact> may call, whatever the endpoint caches.
    assert len(leg.payloads) == 2
    for payload in leg.payloads:
        sent = _last_user_text(payload)
        assert any(line.startswith("Tools:") for line in sent.splitlines())
        assert "reader" in sent


@pytest.mark.parametrize("caching", [False, True])
@pytest.mark.asyncio
async def test_an_lndl_calls_leg_shows_its_tools_as_calls_and_no_round_names_them(leg, caching):
    leg.replies = [_text(_LNDL_READ), _lndl_answer("the notes say hello")]

    profile = _profile(api_tools="read", api_loop="lndl-calls", prompt_caching=caching)
    result, _, _, status, _ = await leg.run(profile)

    assert (result, status) == ("the notes say hello", "completed")
    assert leg.branch.metadata["api_launch"]["loop"] == "lndl-calls"
    assert len(leg.payloads) == 2
    for payload in leg.payloads:
        sent = _last_user_text(payload)
        assert not any(line.startswith("Tools:") for line in sent.splitlines())
        assert "name: reader" not in sent
    first = [line.strip() for line in _last_user_text(leg.payloads[0]).splitlines()]
    assert "CALLING TOOLS" in first
    assert any(line.startswith("reader(") for line in first)
    # Nor does a cached system block carry the tool section, while an lndl leg's does.
    system = json.dumps([payload.get("system") for payload in leg.payloads])
    assert "Tools:" not in system
    if caching:
        leg.payloads.clear()
        leg.replies = [_text(_LNDL_READ), _lndl_answer("the notes say hello")]
        await leg.run(_profile(api_tools="read", api_loop="lndl", prompt_caching=True))
        assert "Tools:" in json.dumps([payload.get("system") for payload in leg.payloads])


@pytest.mark.parametrize("loop", ["lndl", "lndl-calls"])
@pytest.mark.asyncio
async def test_only_an_lndl_calls_leg_is_asked_to_declare_every_known_read_in_one_round(leg, loop):
    leg.replies = [_text(_LNDL_READ), _lndl_answer("the notes say hello")]

    await leg.run(_profile(api_tools="read", api_loop=loop))

    first = " ".join(_last_user_text(leg.payloads[0]).split())
    assert "a tool result is never the answer." in first
    asked = (
        "In each calling round, declare every read you already know you need, up to 16 calls, "
        "and answer only after reading what the answer depends on."
    )
    assert (asked in first) is (loop == "lndl-calls")


@pytest.mark.asyncio
async def test_an_lndl_answer_that_is_a_tool_result_is_refused_and_repaired(leg):
    leg.replies = [
        _text(_LNDL_READ),
        _text(_LNDL_RESULT_AS_ANSWER),
        _lndl_answer("the notes say hello"),
    ]

    result, _, _, status, _ = await leg.run(_profile(api_tools="read", api_loop="lndl"))

    assert (result, status) == ("the notes say hello", "completed")
    assert "the answer must be text you write" in _last_user_text(leg.payloads[2])
    spend = leg.branch.metadata["api_launch"]["spend"]
    assert (spend["requests"], spend["tool_calls"]) == (3, 1)


@pytest.mark.asyncio
async def test_an_lndl_round_that_calls_and_answers_runs_nothing_and_is_repaired(leg):
    leg.replies = [_text(_LNDL_READ_AND_GUESS), _text(_LNDL_READ), _lndl_answer("read it")]

    result, _, _, status, _ = await leg.run(_profile(api_tools="read", api_loop="lndl"))

    assert (result, status) == ("read it", "completed")
    assert "declares <lact> calls and also writes OUT{}" in _last_user_text(leg.payloads[1])
    spend = leg.branch.metadata["api_launch"]["spend"]
    # Only the second round's read ran; the guess was never taken as the answer.
    assert (spend["requests"], spend["tool_calls"]) == (3, 1)


@pytest.mark.asyncio
async def test_an_lndl_leg_is_told_to_keep_calls_and_the_answer_apart(leg):
    leg.replies = [_text(_LNDL_READ), _lndl_answer("the notes say hello")]

    await leg.run(_profile(api_tools="read", api_loop="lndl"))

    assert "either declares <lact> calls" in json.dumps(leg.payloads[0], default=str)


@pytest.mark.asyncio
async def test_an_lndl_turn_that_spends_its_rounds_ends_failed_with_the_cause(leg):
    from lionagi.lndl import LNDLError

    leg.replies = [_text(_LNDL_MISSING), _text(_LNDL_MISSING)]

    with pytest.raises(LNDLError) as exc:
        await leg.run(_profile(api_tools="read", api_loop="lndl", max_extensions=1))

    assert leg.replies == []
    assert leg.status == "failed"
    assert leg.exception is exc.value
    assert "undeclared alias" in str(exc.value)
    assert leg.branch.metadata["api_launch"]["spend"]["requests"] == 2


@pytest.mark.asyncio
async def test_an_lndl_round_over_the_call_limit_runs_none_of_its_calls(leg):
    lacts = "".join(
        f'<lact r{i}>reader(action="read", path="notes.txt")</lact>\n' for i in range(17)
    )
    leg.replies = [_text(f"```lndl\n{lacts}```"), _lndl_answer("split it")]

    result, _, _, status, _ = await leg.run(_profile(api_tools="read", api_loop="lndl"))

    assert (result, status) == ("split it", "completed")
    assert "declares 17 <lact> calls" in _last_user_text(leg.payloads[1])
    assert leg.branch.metadata["api_launch"]["spend"]["tool_calls"] == 0


@pytest.mark.parametrize("value", ["batch", "LNDL", "", None, True, ["lndl"]])
@pytest.mark.asyncio
async def test_a_loop_other_than_react_or_lndl_is_refused_before_a_run_exists(
    leg, monkeypatch, value
):
    import lionagi.cli.agent as agent_mod

    monkeypatch.setattr(
        agent_mod, "allocate_run", lambda: pytest.fail("a run was allocated for a refused leg")
    )

    with pytest.raises(ConfigurationError, match="sets api_loop to"):
        await leg.run(_profile(api_tools="read", api_loop=value))


@pytest.mark.asyncio
async def test_a_loop_without_the_read_set_is_refused_before_a_run_exists(leg, monkeypatch):
    import lionagi.cli.agent as agent_mod

    monkeypatch.setattr(
        agent_mod, "allocate_run", lambda: pytest.fail("a run was allocated for a refused leg")
    )

    with pytest.raises(ConfigurationError, match="sets api_loop but no api_tools"):
        await leg.run(_profile(api_loop="lndl"))


@pytest.mark.asyncio
async def test_a_record_without_a_loop_resumes_on_react(leg):
    from lionagi.cli._runs import find_branch

    leg.replies = [_last_round(), _answer("first")]
    _, _, branch_id, _, _ = await leg.run(_profile(api_tools="read"))
    _, path = find_branch(branch_id)
    stored = json.loads(path.read_text())
    del stored["metadata"]["api_launch"]["loop"]
    path.write_text(json.dumps(stored))

    # The record decides the loop on resume, whatever the profile now names.
    leg.replies = [_last_round(), _answer("second")]
    result, _, _, status, _ = await leg.run(
        _profile(api_tools="read", api_loop="lndl"), "again", resume=branch_id
    )

    assert (result, status) == ("second", "completed")
    assert leg.replies == []


@pytest.mark.asyncio
async def test_an_lndl_branch_resumes_on_lndl_from_its_record(leg):
    import lionagi.cli.agent as agent_mod
    from lionagi.cli._runs import find_branch

    leg.replies = [_text(_LNDL_READ), _lndl_answer("first")]
    _, _, branch_id, _, _ = await leg.run(_profile(api_tools="read", api_loop="lndl"))

    leg.replies = [_lndl_answer("second")]
    result, _, _, status, _ = await agent_mod._run_agent(
        MODEL, "and now?", agent_name=None, cwd=str(leg.workspace), resume=branch_id
    )

    assert (result, status) == ("second", "completed")
    assert leg.replies == []
    _, path = find_branch(branch_id)
    record = json.loads(path.read_text())["metadata"]["api_launch"]
    assert record["loop"] == "lndl"
    assert (record["spend"]["requests"], record["spend"]["tool_calls"]) == (1, 0)


@pytest.mark.asyncio
async def test_a_loop_without_the_read_set_is_refused_for_a_cli_provider(
    leg, monkeypatch, tmp_path
):
    import lionagi.cli.agent as agent_mod
    from lionagi import Branch

    monkeypatch.setattr(
        agent_mod, "allocate_run", lambda: pytest.fail("a run was allocated for a refused leg")
    )
    monkeypatch.setattr(agent_mod, "build_chat_model", lambda *a, **kw: "claude_code/sonnet")
    monkeypatch.setattr(agent_mod, "resolve_persisted_effort", lambda *a, **kw: None)
    monkeypatch.setattr(
        Branch, "operate", AsyncMock(side_effect=AssertionError("the refused leg ran a turn"))
    )
    monkeypatch.setattr(
        agent_mod,
        "_provenance",
        SimpleNamespace(
            resolve_model_spec=lambda p, m: f"{p}/{m}",
            agent_definition_hash=lambda n: "abc",
        ),
    )

    with pytest.raises(ConfigurationError, match="sets api_loop but no api_tools"):
        await leg.run(_profile(api_loop="lndl"), model="claude_code/sonnet")


@pytest.mark.asyncio
async def test_a_loop_without_the_read_set_is_refused_on_resume_without_a_record(leg, monkeypatch):
    import lionagi.cli.agent as agent_mod
    from lionagi.cli._runs import find_branch

    leg.replies = [_last_round(), _answer("first")]
    _, _, branch_id, _, _ = await leg.run(_profile(api_tools="read"))
    _, path = find_branch(branch_id)
    stored = json.loads(path.read_text())
    del stored["metadata"]["api_launch"]
    path.write_text(json.dumps(stored))
    monkeypatch.setattr(
        agent_mod, "allocate_run", lambda: pytest.fail("a run was allocated for a refused leg")
    )

    with pytest.raises(ConfigurationError, match="sets api_loop but no api_tools"):
        await leg.run(_profile(api_loop="lndl"), "again", resume=branch_id)


# ── the output limit and a pass that ends on a failed round ──────────────


def _cut_off(*blocks: dict) -> dict:
    """A response that stopped at its output token limit, carrying only ``blocks``."""
    reply = _reply({})
    reply["content"] = list(blocks) or [{"type": "thinking", "thinking": "...", "signature": "s"}]
    reply["stop_reason"] = "max_tokens"
    reply["usage"] = {"input_tokens": 1, "output_tokens": 32_768}
    return reply


# A reply with text that does not parse is repaired by reformat requests before the round gives
# up; a reply with no text is not repaired at all.
_REPAIRS = 4


@pytest.mark.asyncio
async def test_an_anthropic_leg_sends_the_api_output_limit(leg):
    import lionagi.cli.agent as agent_mod

    leg.replies = [_last_round(), _answer("hello")]

    await leg.run(_profile(api_tools="read"))

    assert [p["max_tokens"] for p in leg.payloads] == [agent_mod._API_MAX_OUTPUT_TOKENS] * 2


@pytest.mark.asyncio
async def test_the_failed_round_check_reads_the_last_round_and_the_answer_by_position(
    leg, monkeypatch
):
    import lionagi.cli.agent as agent_mod
    from lionagi.protocols.messages import AssistantResponse

    seen = []
    check = agent_mod._failed_round

    def spy(branch, before, rounds):
        added = [
            m
            for m in branch.msgs.messages
            if m.id not in before and isinstance(m, AssistantResponse)
        ]
        seen.append(([json.dumps(m.model_response) for m in added], list(rounds)))
        return check(branch, before, rounds)

    monkeypatch.setattr(agent_mod, "_failed_round", spy)
    leg.replies = [_read_round("notes.txt"), _last_round(), _answer("hello")]

    await leg.run(_profile(api_tools="read"))

    [(added, rounds)] = seen
    # Each round's analysis, then the answer: [-2] is the round that ended the loop.
    assert [getattr(r, "analysis", None) for r in rounds[:-1]] == [
        "read the notes first",
        "the file says hello",
    ]
    assert rounds[-1].answer == "hello"
    # The pass's last two responses are that round's and the answer turn's.
    assert len(added) == 3
    assert "the file says hello" in added[-2]
    assert '\\"answer\\": \\"hello\\"' in added[-1]


def test_a_model_that_names_its_output_limit_keeps_it():
    import lionagi.cli.agent as agent_mod
    from lionagi import Branch
    from lionagi.service.imodel import iModel

    named = iModel(provider="anthropic", model="claude-haiku-5-5", api_key="k", max_tokens=64)
    unnamed = iModel(provider="anthropic", model="claude-haiku-5-5", api_key="k")
    branch = Branch(chat_model=unnamed, parse_model=named)

    assert agent_mod._install_api_input_cap(branch, "none") is None

    assert branch.parse_model.endpoint.config.kwargs["max_tokens"] == 64
    assert branch.chat_model.endpoint.config.kwargs["max_tokens"] == (
        agent_mod._API_MAX_OUTPUT_TOKENS
    )
    # The model the caller built is left as it was.
    assert unnamed.endpoint.config.kwargs.get("max_tokens") is None


@pytest.mark.asyncio
async def test_an_anthropic_leg_request_waits_at_most_the_endpoint_timeout():
    import lionagi.cli.agent as agent_mod
    from lionagi import Branch
    from lionagi.service.imodel import iModel

    branch = Branch(chat_model=iModel(provider="anthropic", model="claude-haiku-5-5", api_key="k"))
    agent_mod._install_api_input_cap(branch, "none")
    endpoint = branch.chat_model.endpoint

    assert endpoint.config.timeout == 600
    # The client a request is sent on, direct or routed, takes the configured timeout.
    for client in (
        endpoint._create_http_session(),
        endpoint._create_http_session({}, timeout=endpoint.config.timeout),
    ):
        try:
            assert client.timeout.read == 600
            assert client.timeout.connect == 600
        finally:
            await client.aclose()


@pytest.mark.asyncio
async def test_a_round_spent_on_thinking_is_continued_and_the_turn_finishes(leg):
    leg.replies = [
        _read_round("notes.txt"),
        _cut_off(),
        _answer("half an answer"),
        _last_round(),
        _answer("the notes say hello"),
    ]

    result, _, _, status, _ = await leg.run(_profile(api_tools="read"))

    assert (result, status) == ("the notes say hello", "completed")
    assert leg.replies == []
    resumed = _last_user_text(leg.payloads[3])
    assert "reached its output token limit before it wrote any text or tool call" in resumed
    assert not any("text_to_format" in json.dumps(p, default=str) for p in leg.payloads)


@pytest.mark.asyncio
async def test_an_answer_with_no_text_is_continued_and_the_turn_finishes(leg):
    leg.replies = [
        _read_round("notes.txt"),
        _last_round(),
        _text(""),
        _last_round(),
        _answer("the notes say hello"),
    ]

    result, _, _, status, _ = await leg.run(_profile(api_tools="read"))

    assert (result, status) == ("the notes say hello", "completed")
    assert leg.replies == []
    assert "Your last reply had no text" in _last_user_text(leg.payloads[3])


@pytest.mark.asyncio
async def test_an_answer_that_keeps_having_no_text_fails_the_turn(leg):
    import lionagi.cli.agent as agent_mod
    from lionagi._errors import FailedRoundError

    for _ in range(agent_mod._API_ROUND_RESUMES + 1):
        leg.replies.extend([_last_round(), _text("")])

    with pytest.raises(FailedRoundError, match="the answer had no text") as raised:
        await leg.run(_profile(api_tools="read"))

    assert raised.value.details == {"continued": agent_mod._API_ROUND_RESUMES}
    assert leg.replies == []
    assert leg.status == "failed"


@pytest.mark.asyncio
async def test_an_answer_that_keeps_stopping_at_the_output_limit_fails_the_turn(leg):
    import lionagi.cli.agent as agent_mod
    from lionagi._errors import FailedRoundError

    for _ in range(agent_mod._API_ROUND_RESUMES + 1):
        leg.replies.extend([_last_round(), _cut_off()])

    with pytest.raises(FailedRoundError, match="no text and no tool call") as raised:
        await leg.run(_profile(api_tools="read"))

    assert raised.value.details == {"continued": agent_mod._API_ROUND_RESUMES}
    assert leg.replies == []
    assert len(leg.payloads) == 2 * (agent_mod._API_ROUND_RESUMES + 1)
    assert not any("text_to_format" in json.dumps(p, default=str) for p in leg.payloads)
    assert leg.status == "failed"
    assert leg.exception is raised.value
    spend = leg.branch.metadata["api_launch"]["spend"]
    assert spend["requests"] == 2 * (agent_mod._API_ROUND_RESUMES + 1)


@pytest.mark.asyncio
async def test_a_reply_that_does_not_parse_is_continued_with_the_reason(leg):
    leg.replies = [
        _text("I will read the notes next."),
        *(_text("Still no JSON here.") for _ in range(_REPAIRS)),
        _answer("read them"),
        _last_round(),
        _answer("the notes say hello"),
    ]

    result, _, _, status, _ = await leg.run(_profile(api_tools="read"))

    assert (result, status) == ("the notes say hello", "completed")
    assert leg.replies == []
    resumed = _last_user_text(leg.payloads[1 + _REPAIRS + 1])
    assert "could not be read in the required response format" in resumed
    assert "no JSON object could be read from it" in resumed
    assert "none of the actions in it ran" in resumed


@pytest.mark.asyncio
async def test_an_lndl_round_cut_off_with_nothing_is_repaired_inside_its_rounds(leg):
    leg.replies = [_cut_off(), _text(_LNDL_READ), _lndl_answer("the notes say hello")]

    result, _, _, status, _ = await leg.run(_profile(api_tools="read", api_loop="lndl"))

    assert (result, status) == ("the notes say hello", "completed")
    assert leg.replies == []
    assert len(leg.payloads) == 3


@pytest.mark.asyncio
async def test_actions_in_a_reply_whose_text_holds_a_fenced_block_run(leg):
    content = {
        "analysis": "the notes may hold a block like ```python\nprint(1)\n``` so read them",
        "action_requests": [
            {"function": "reader", "arguments": {"action": "read", "path": "notes.txt"}}
        ],
        "action_strategy": "sequential",
        "extension_needed": True,
        "reason": {"title": "read", "content": "the answer is in the file"},
    }
    leg.replies = [
        _text("Reading the notes.\n\n```json\n" + json.dumps(content, indent=2) + "\n```\n"),
        _last_round(),
        _answer("the notes say hello"),
    ]

    result, _, _, status, _ = await leg.run(_profile(api_tools="read"))

    assert (result, status) == ("the notes say hello", "completed")
    assert len(leg.payloads) == 3 and leg.replies == []
    responses = [m for m in leg.branch.msgs.messages if type(m).__name__ == "ActionResponse"]
    assert len(responses) == 1
    assert "hello from the notes" in json.dumps(responses[0].content.to_dict())


def test_li_agent_exits_failed_on_a_failed_round_without_a_traceback(monkeypatch):
    import lionagi.cli.agent as agent_mod
    from lionagi._errors import FailedRoundError

    async def failed_run(*args, **kwargs):
        raise FailedRoundError("3 passes of the tool loop in a row ended on a failed round: x")

    errors: list[str] = []
    monkeypatch.setattr(agent_mod, "log_error", lambda msg: errors.append(msg))
    monkeypatch.setattr(agent_mod, "_resolve_model_and_prompt", lambda args: (MODEL, "x"))
    monkeypatch.setattr(agent_mod, "_run_agent", failed_run)
    args = SimpleNamespace(
        list_profiles=False,
        form=None,
        image=None,
        agent="reader-leg",
        resume=None,
        continue_last=False,
        yolo=False,
        verbose=False,
        theme=None,
        effort=None,
        cwd=None,
        timeout=None,
        fast=False,
    )

    assert agent_mod.run_agent(args) == 1
    assert errors == []


_THINKING = {"type": "thinking", "thinking": "...", "signature": "s"}


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (_cut_off(_THINKING), True),
        (_cut_off(_THINKING, {"type": "text", "text": "  \n"}), True),
        (_cut_off(_THINKING, {"type": "text", "text": "partial"}), False),
        (
            _cut_off(_THINKING, {"type": "tool_use", "id": "t", "name": "reader", "input": {}}),
            False,
        ),
        ({**_cut_off(_THINKING), "stop_reason": "end_turn"}, False),
        ({"choices": [{"finish_reason": "length", "message": {"content": ""}}]}, True),
        ({"choices": [{"finish_reason": "length", "message": {"content": "part"}}]}, False),
        (
            {
                "choices": [
                    {"finish_reason": "length", "message": {"content": None, "tool_calls": [{}]}}
                ]
            },
            False,
        ),
        ({"choices": [{"finish_reason": "stop", "message": {"content": ""}}]}, False),
        (
            {
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [{"type": "reasoning", "summary": []}],
            },
            True,
        ),
        (
            {
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": "part"}]}
                ],
            },
            False,
        ),
        (
            {
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [{"type": "function_call", "name": "reader", "arguments": "{}"}],
            },
            False,
        ),
        (
            {
                "status": "incomplete",
                "incomplete_details": {"reason": "content_filter"},
                "output": [],
            },
            False,
        ),
        ([_cut_off(_THINKING)], False),
        ("not a response", False),
    ],
)
def test_a_response_cut_off_with_nothing_is_read_in_each_provider_shape(response, expected):
    import lionagi.cli.agent as agent_mod

    assert agent_mod._stopped_at_output_limit_with_nothing(response) is expected


@pytest.mark.asyncio
async def test_profile_effort_reaches_anthropic_wire_payload(leg, monkeypatch):
    profile = _profile(api_tools="read")
    profile.effort = "max"
    seen_payloads = []

    async def call(self, request, *args, **kwargs):
        payload, _ = self.create_payload(request, **kwargs)
        seen_payloads.append(payload)
        return leg.replies.pop(0)

    monkeypatch.setattr(AnthropicMessagesEndpoint, "call", call)
    leg.replies = [_last_round(), _answer("done")]
    _, _, _, status, _ = await leg.run(profile)
    assert status == "completed"
    assert seen_payloads
    assert all(payload["output_config"]["effort"] == "max" for payload in seen_payloads)


@pytest.mark.asyncio
async def test_profile_effort_persists_and_can_be_overridden_on_resume(leg):
    profile = _profile(api_tools="read")
    profile.effort = "max"
    leg.replies = [_last_round(), _answer("first")]
    _, _, branch_id, status, _ = await leg.run(profile)
    assert status == "completed"
    assert all(p["output_config"]["effort"] == "max" for p in leg.payloads)

    leg.payloads.clear()
    leg.replies = [_last_round(), _answer("second")]
    _, _, _, status, _ = await leg.run(_profile(api_tools="read"), resume=branch_id)
    assert status == "completed"
    assert all(p["output_config"]["effort"] == "max" for p in leg.payloads)

    leg.payloads.clear()
    leg.replies = [_last_round(), _answer("third")]
    _, _, _, status, _ = await leg.run(_profile(api_tools="read"), resume=branch_id, effort="low")
    assert status == "completed"
    assert all(p["output_config"]["effort"] == "low" for p in leg.payloads)


@pytest.mark.asyncio
async def test_openai_profile_effort_with_agent_tools_uses_no_native_tools(leg, monkeypatch):
    from lionagi.providers.openai.chat import OpenaiChatEndpoint

    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key")
    profile = _profile(api_tools="read")
    profile.effort = "high"
    replies = [
        {
            "analysis": "done",
            "action_requests": [],
            "action_strategy": "sequential",
            "extension_needed": False,
            "reason": {"title": "done", "content": "done"},
        },
        {"answer": "done"},
    ]
    payloads = []

    async def call(self, request, *args, **kwargs):
        payloads.append(request)
        return {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 0,
            "model": "gpt-6-luna",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": json.dumps(replies.pop(0))},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    monkeypatch.setattr(OpenaiChatEndpoint, "call", call)
    result, _, _, status, _ = await leg.run(profile, model="openai/gpt-6-luna")
    assert (result, status) == ("done", "completed")
    assert payloads
    assert all(p["reasoning_effort"] == "high" and "tools" not in p for p in payloads)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model,provider_class",
    [
        ("openai/gpt-4.1", "openai"),
        ("groq/example-model", "groq"),
    ],
)
async def test_unsupported_profile_effort_does_not_break_api_leg(
    leg, monkeypatch, model, provider_class
):
    from lionagi.providers.groq.chat import GroqChatEndpoint
    from lionagi.providers.openai.chat import OpenaiChatEndpoint

    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key")
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-key")
    profile = _profile(api_tools="read")
    profile.effort = "high"
    replies = [
        {
            "analysis": "done",
            "action_requests": [],
            "action_strategy": "sequential",
            "extension_needed": False,
            "reason": {"title": "done", "content": "done"},
        },
        {"answer": "done"},
    ]
    payloads = []

    async def call(self, request, *args, **kwargs):
        payloads.append(request)
        return {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 0,
            "model": model.split("/", 1)[1],
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": json.dumps(replies.pop(0))},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    cls = OpenaiChatEndpoint if provider_class == "openai" else GroqChatEndpoint
    monkeypatch.setattr(cls, "call", call)
    result, _, _, status, _ = await leg.run(profile, model=model)
    assert (result, status) == ("done", "completed")
    assert payloads and all("reasoning_effort" not in p for p in payloads)


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["openai/gpt-4.1", "groq/example-model", "gemini/example-model"])
async def test_explicit_cli_effort_on_unsupported_api_is_refused(leg, monkeypatch, model):
    for key in ("OPENAI_API_KEY", "GROQ_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.setenv(key, "synthetic-key")
    with pytest.raises(ConfigurationError, match="model.*high"):
        await leg.run(_profile(api_tools="read"), model=model, effort="high")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "profile_model,profile_effort,positional,expected",
    [
        ("openai/gpt-4.1-high", None, None, None),
        ("openai/gpt-6-luna-xhigh", None, None, "xhigh"),
        (None, "high", "openai/gpt-6-luna-xhigh", "high"),
    ],
)
async def test_api_model_suffix_origin_and_profile_precedence(
    leg, monkeypatch, profile_model, profile_effort, positional, expected
):
    from lionagi.providers.openai.chat import OpenaiChatEndpoint

    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key")
    profile = _profile(api_tools="read")
    profile.model = profile_model
    profile.effort = profile_effort
    replies = [
        {
            "analysis": "done",
            "action_requests": [],
            "action_strategy": "sequential",
            "extension_needed": False,
            "reason": {"title": "done", "content": "done"},
        },
        {"answer": "done"},
    ]
    payloads = []

    async def call(self, request, *args, **kwargs):
        payloads.append(request)
        return {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 0,
            "model": request["model"],
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": json.dumps(replies.pop(0))},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    monkeypatch.setattr(OpenaiChatEndpoint, "call", call)
    result, _, _, status, _ = await leg.run(profile, model=positional)
    assert (result, status) == ("done", "completed")
    assert payloads and all(p.get("reasoning_effort") == expected for p in payloads)
    if expected is None:
        assert all("reasoning_effort" not in p for p in payloads)
