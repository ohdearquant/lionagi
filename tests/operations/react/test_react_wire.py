# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""ReAct against a local OpenAI-compatible server: what each round puts on the wire.

The server records every request body, so these tests see the messages exactly as
the provider would, after the branch history has been rendered.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from lionagi._errors import ExecutionError
from lionagi.hooks.bus import HookBus, HookPoint
from lionagi.operations._defaults import make_parse_param
from lionagi.operations.chat import _prepare
from lionagi.operations.chat._prepare import (
    _carries_context,
    _history_carriers,
    _render_context,
)
from lionagi.operations.ReAct.ReAct import ReActStream
from lionagi.operations.ReAct.utils import ReActAnalysis
from lionagi.operations.types import ChatParam
from lionagi.protocols.action.tool import Tool
from lionagi.protocols.generic.progression import Progression
from lionagi.protocols.messages import AssistantResponse, Instruction
from lionagi.service.imodel import iModel
from lionagi.session.branch import Branch

MARKER = "candidate-menu-7f3a"

PROVIDER_ERROR = {
    "id": "gen-1",
    "error": {
        "message": "Your input exceeds the context window of this model.",
        "code": 400,
        "metadata": {"error_type": "invalid_request", "provider_code": "context_length_exceeded"},
    },
}


def _completion(content: dict) -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "gpt-4.1-mini",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": json.dumps(content)},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


@pytest.fixture
async def chat_server():
    servers = []

    async def _run(replies: list[dict]) -> tuple[str, list[dict]]:
        received: list[dict] = []

        async def handler(request: web.Request):
            received.append(await request.json())
            return web.json_response(replies[len(received) - 1])

        app = web.Application()
        app.router.add_route("POST", "/{tail:.*}", handler)
        server = TestServer(app)
        await server.start_server()
        servers.append(server)
        return str(server.make_url("/")).rstrip("/"), received

    yield _run

    for server in servers:
        await server.close()


def _branch(base_url: str, **kwargs) -> Branch:
    model = iModel(
        provider="openai",
        endpoint="chat",
        model="gpt-4.1-mini",
        api_key="test-key",
        base_url=base_url,
        allow_local_network=True,
        max_retries=3,
    )
    return Branch(chat_model=model, parse_model=model, **kwargs)


@pytest.mark.asyncio
async def test_every_round_carries_the_context_once(chat_server):
    base_url, received = await chat_server(
        [
            _completion({"analysis": "round 1", "extension_needed": True}),
            _completion({"analysis": "round 2", "extension_needed": True}),
            _completion({"analysis": "round 3", "extension_needed": False}),
            _completion({"answer": "done"}),
        ]
    )
    branch = _branch(base_url)

    result = await branch.ReAct(
        {"instruction": "pick the codes", "context": {"menu": MARKER}}, max_extensions=3
    )

    assert result == "done"
    assert len(received) == 4
    assert [json.dumps(body["messages"]).count(MARKER) for body in received] == [1, 1, 1, 1]


@pytest.mark.asyncio
async def test_every_round_keeps_the_context_it_was_sent_with(chat_server):
    base_url, received = await chat_server(
        [
            _completion({"analysis": "round 1", "extension_needed": True}),
            _completion({"analysis": "round 2", "extension_needed": True}),
            _completion({"analysis": "round 3", "extension_needed": False}),
            _completion({"answer": "done"}),
        ]
        + [_completion({"answer": "and after"})] * 3
    )
    branch = _branch(base_url)

    await branch.ReAct(
        {"instruction": "pick the codes", "context": {"menu": MARKER}}, max_extensions=2
    )
    rounds = [m for m in branch.msgs.messages if isinstance(m, Instruction)]
    for instruction in rounds[:3]:
        await branch.chat(instruction="and now?", progression=[instruction.id])

    # Each request of the run shows the context once, and leaving it out of a request
    # changes no stored round: a later chat rendering any one of them alone shows it.
    assert len(rounds) == 4
    assert all({"menu": MARKER} in m.content.prompt_context for m in rounds)
    assert [json.dumps(body["messages"]).count(MARKER) for body in received] == [1] * 7


@pytest.mark.asyncio
async def test_a_round_a_hook_stores_before_its_call_returns_keeps_the_context(chat_server):
    base_url, received = await chat_server(
        [
            _completion({"analysis": "round 1", "extension_needed": True}),
            _completion({"analysis": "round 2", "extension_needed": False}),
            _completion({"answer": "done"}),
            _completion({"answer": "and after"}),
        ]
    )
    branch = _branch(base_url, system="the system")
    gathered = []

    class Remember:
        async def provide(self, branch, instruction):
            gathered.append(instruction)

    branch.providers.register(Remember(), name="remember")
    bus = HookBus()
    branch.attach_hook_bus(bus)
    calls = []

    async def store_the_extension(**kwargs):
        calls.append(kwargs)
        if len(calls) == 2:
            branch.msgs.add_message(instruction=gathered[-1])

    bus.on(HookPoint.API_PRE_CALL, store_the_extension)

    await branch.ReAct(
        {"instruction": "pick the codes", "context": {"menu": MARKER}}, max_extensions=1
    )
    extension = gathered[1]
    await branch.chat(instruction="and now?", progression=[extension.id])

    # The hook stores the extension round's instruction after its request was prepared;
    # the request left the context out of its own copy only.
    assert extension in branch.msgs.messages
    assert {"menu": MARKER} in extension.content.prompt_context
    assert [json.dumps(body["messages"]).count(MARKER) for body in received] == [1, 1, 1, 1]


def test_a_request_shows_the_context_where_its_history_first_does():
    branch = Branch()
    held = []
    for n in (1, 2, 3):
        held.append(
            branch.msgs.add_message(instruction=f"question {n}", context=[{"menu": MARKER}])
        )
        branch.msgs.add_message(assistant_response=f"answer {n}")
        if n == 2:
            request = branch.msgs.add_message(
                action_function="lookup", action_arguments={"key": "k"}
            )
            branch.msgs.add_message(
                action_request=request, action_output={"value": "a distinctive result"}
            )
    current = Instruction(content={"instruction": "and now?", "context": [{"menu": MARKER}]})
    param = ChatParam.from_branch(branch)

    token = _prepare._react_context.set([{"menu": MARKER}])
    try:
        _, kw = _prepare._prepare_run_kwargs(branch, current, param, ins=current)
    finally:
        _prepare._react_context.reset(token)

    contents = [m["content"] for m in kw["messages"]]
    assert [MARKER in c for c in contents] == [True, False, False, False, False, False, False]
    assert "question 3" in contents[4] and "a distinctive result" in contents[4]
    assert all({"menu": MARKER} in m.content.prompt_context for m in held)
    assert {"menu": MARKER} in current.content.prompt_context


@pytest.mark.asyncio
async def test_a_chat_after_the_run_attaches_its_context_as_before(chat_server):
    base_url, received = await chat_server(
        [
            _completion({"analysis": "round 1", "extension_needed": False}),
            _completion({"answer": "done"}),
            _completion({"answer": "and after"}),
        ]
    )
    branch = _branch(base_url)

    await branch.ReAct({"instruction": "pick the codes", "context": {"menu": MARKER}})
    await branch.chat(instruction="and now?", context={"menu": MARKER})

    # Only ReAct's own later calls leave the context out; a call after the run is
    # outside it and attaches the context beside every copy its history renders: the
    # first round's and the final answer's, which keep it as stored.
    assert len(received) == 3
    assert [json.dumps(body["messages"]).count(MARKER) for body in received] == [1, 1, 3]


@pytest.mark.asyncio
async def test_a_final_answer_that_clears_the_messages_still_carries_the_context(chat_server):
    base_url, received = await chat_server(
        [
            _completion({"analysis": "round 1", "extension_needed": False}),
            _completion({"answer": "done"}),
        ]
    )
    branch = _branch(base_url)

    await branch.ReAct(
        {"instruction": "pick the codes", "context": {"menu": MARKER}},
        response_kwargs={"clear_messages": True},
    )

    assert len(received) == 2
    assert [json.dumps(body["messages"]).count(MARKER) for body in received] == [1, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize("as_progression", [False, True], ids=["list", "Progression"])
async def test_a_caller_fixed_progression_keeps_the_context_on_every_round(
    chat_server, as_progression
):
    base_url, received = await chat_server(
        [
            _completion({"analysis": "round 1", "extension_needed": True}),
            _completion({"analysis": "round 2", "extension_needed": False}),
            _completion({"answer": "done"}),
        ]
    )
    branch = _branch(base_url)
    earlier = branch.msgs.add_message(instruction="an earlier question")
    order = [earlier.id]
    chat_param = ChatParam.from_branch(
        branch,
        context={"menu": MARKER},
        progression=Progression(order=order) if as_progression else order,
    )
    parse_param = make_parse_param(ReActAnalysis, branch.parse_model)

    async for _ in ReActStream(
        branch, "pick the codes", chat_param=chat_param, parse_param=parse_param, max_extensions=1
    ):
        pass

    # The fixed progression never renders the first round's instruction, so every
    # call has to carry the context itself.
    assert len(received) == 3
    assert [json.dumps(body["messages"]).count(MARKER) for body in received] == [1, 1, 1]


@pytest.mark.asyncio
async def test_a_branch_progression_subset_keeps_the_context_on_every_round(chat_server):
    base_url, received = await chat_server(
        [
            _completion({"analysis": "round 1", "extension_needed": True}),
            _completion({"analysis": "round 2", "extension_needed": False}),
            _completion({"answer": "done"}),
        ]
    )
    branch = _branch(base_url)
    earlier = branch.msgs.add_message(instruction="an earlier question")
    branch.metadata["current_progression"] = Progression(order=[earlier.id])

    async for _ in branch.ReActStream(
        {"instruction": "pick the codes", "context": {"menu": MARKER}}, max_extensions=1
    ):
        pass

    assert len(received) == 3
    assert [json.dumps(body["messages"]).count(MARKER) for body in received] == [1, 1, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["gains", "loses"])
async def test_a_progression_changed_between_rounds_still_carries_the_context_once(
    chat_server, change
):
    base_url, received = await chat_server(
        [
            _completion({"analysis": "round 1", "extension_needed": True}),
            _completion({"analysis": "injected round", "extension_needed": False}),
            _completion({"answer": "done"}),
        ]
    )
    branch = _branch(base_url)
    if change == "gains":
        earlier = branch.msgs.add_message(instruction="an earlier question")
        reply = branch.msgs.add_message(assistant_response="an earlier reply")
        branch.metadata["current_progression"] = Progression(order=[earlier.id, reply.id])

    async def between_rounds(branch, round_count):
        first = branch.msgs.last_instruction
        if change == "gains":
            branch.progression.append(first.id)
        else:
            kept = [uid for uid in branch.msgs.progression if uid != first.id]
            branch.metadata["current_progression"] = Progression(order=kept)
        return "check the picks again"

    await branch.ReAct(
        {"instruction": "pick the codes", "context": {"menu": MARKER}},
        max_extensions=1,
        between_rounds=between_rounds,
    )

    assert len(received) == 3
    assert [json.dumps(body["messages"]).count(MARKER) for body in received] == [1, 1, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ref", [lambda m: m.id, lambda m: str(m.id), lambda m: m], ids=["UUID", "str", "message"]
)
async def test_a_caller_progression_gaining_the_first_instruction_carries_the_context_once(
    chat_server, ref
):
    base_url, received = await chat_server(
        [
            _completion({"analysis": "round 1", "extension_needed": True}),
            _completion({"analysis": "injected round", "extension_needed": False}),
            _completion({"answer": "done"}),
        ]
    )
    branch = _branch(base_url)
    earlier = branch.msgs.add_message(instruction="an earlier question")
    reply = branch.msgs.add_message(assistant_response="an earlier reply")
    order = [earlier.id, reply.id]
    chat_param = ChatParam.from_branch(branch, context={"menu": MARKER}, progression=order)
    parse_param = make_parse_param(ReActAnalysis, branch.parse_model)

    async def between_rounds(branch, round_count):
        order.append(ref(branch.msgs.last_instruction))
        return "check the picks again"

    async for _ in ReActStream(
        branch,
        "pick the codes",
        chat_param=chat_param,
        parse_param=parse_param,
        max_extensions=1,
        between_rounds=between_rounds,
    ):
        pass

    assert len(received) == 3
    assert [json.dumps(body["messages"]).count(MARKER) for body in received] == [1, 1, 1]


@pytest.mark.asyncio
async def test_an_instruction_the_progression_holds_keeps_the_context_when_it_is_sent_again(
    chat_server,
):
    base_url, received = await chat_server(
        [
            _completion({"analysis": "round 1", "extension_needed": True}),
            _completion({"analysis": "injected round", "extension_needed": False}),
            _completion({"answer": "done"}),
        ]
    )
    branch = _branch(base_url)
    earlier = branch.msgs.add_message(
        instruction="an earlier selection", context=[{"other": "kept"}]
    )
    chat_param = ChatParam.from_branch(branch, context=[{"menu": MARKER}], progression=[earlier.id])
    parse_param = make_parse_param(ReActAnalysis, branch.parse_model)

    async def between_rounds(branch, round_count):
        return earlier

    async for _ in ReActStream(
        branch,
        "pick the codes",
        chat_param=chat_param,
        parse_param=parse_param,
        max_extensions=1,
        between_rounds=between_rounds,
    ):
        pass

    # The injected turn is the instruction the progression already holds, so the history
    # renders that same object: it keeps the context there, and only the copy sent as the
    # current turn leaves it out.
    assert len(received) == 3
    assert [json.dumps(body["messages"]).count(MARKER) for body in received] == [1, 1, 1]
    assert {"menu": MARKER} in earlier.content.prompt_context


@pytest.mark.asyncio
@pytest.mark.parametrize("held_by", ["branch", "branch outside its progression", "another branch"])
async def test_an_instruction_the_branch_holds_outside_the_progression_keeps_the_context(
    chat_server, held_by
):
    base_url, received = await chat_server(
        [
            _completion({"analysis": "round 1", "extension_needed": True}),
            _completion({"analysis": "injected round", "extension_needed": False}),
            _completion({"answer": "done"}),
            _completion({"answer": "and after"}),
        ]
    )
    branch = _branch(base_url)
    carrier = branch.msgs.add_message(
        instruction="an earlier selection", context=[{"menu": MARKER}]
    )
    holder = Branch() if held_by == "another branch" else branch
    stored = holder.msgs.add_message(instruction="a stored selection", context=[{"other": "kept"}])
    if held_by == "branch outside its progression":
        branch.msgs.progression.remove(stored.id)
    chat_param = ChatParam.from_branch(branch, context=[{"menu": MARKER}], progression=[carrier.id])
    parse_param = make_parse_param(ReActAnalysis, branch.parse_model)

    async def between_rounds(branch, round_count):
        return stored

    async for _ in ReActStream(
        branch,
        "pick the codes",
        chat_param=chat_param,
        parse_param=parse_param,
        max_extensions=1,
        between_rounds=between_rounds,
    ):
        pass
    await branch.chat(instruction="and now?", progression=[stored.id])

    # The first call attaches the context beside the carrier's copy. The injected turn is an
    # instruction outside the progression, so another history can render it: it keeps the
    # context, and a later request whose history shows it carries it.
    assert len(received) == 4
    assert [json.dumps(body["messages"]).count(MARKER) for body in received] == [2, 1, 1, 1]
    assert {"menu": MARKER} in stored.content.prompt_context


def test_an_instruction_the_progression_holds_takes_the_action_results_after_it_without_the_context():
    branch = Branch()
    earlier = branch.msgs.add_message(
        instruction="an earlier selection", context=[{"menu": MARKER}]
    )
    request = branch.msgs.add_message(action_function="lookup", action_arguments={"key": "k"})
    branch.msgs.add_message(action_request=request, action_output={"value": "a distinctive result"})
    param = ChatParam.from_branch(branch, progression=[m.id for m in branch.msgs.messages])

    token = _prepare._react_context.set([{"menu": MARKER}])
    try:
        _, kw = _prepare._prepare_run_kwargs(branch, earlier, param, ins=earlier)
    finally:
        _prepare._react_context.reset(token)

    # The action results after the instruction fold into the turn sent again, which leaves
    # out the context the history's copy of that instruction already shows.
    assert json.dumps(kw["messages"]).count(MARKER) == 1
    assert "a distinctive result" in kw["messages"][-1]["content"]
    assert MARKER not in kw["messages"][-1]["content"]
    assert {"menu": MARKER} in earlier.content.prompt_context


@pytest.mark.asyncio
async def test_a_first_instruction_the_history_does_not_render_still_sends_the_context_once(
    chat_server,
):
    base_url, received = await chat_server(
        [
            _completion({"analysis": "round 1", "extension_needed": True}),
            _completion({"analysis": "round 2", "extension_needed": True}),
            _completion({"analysis": "round 3", "extension_needed": False}),
            _completion({"answer": "done"}),
        ]
    )
    branch = _branch(base_url)
    # An instruction that never got a reply: history keeps only the first of
    # consecutive instructions, so the first round's instruction is not rendered.
    branch.msgs.add_message(instruction="an unanswered question")

    await branch.ReAct(
        {"instruction": "pick the codes", "context": {"menu": MARKER}}, max_extensions=3
    )

    assert len(received) == 4
    assert [json.dumps(body["messages"]).count(MARKER) for body in received] == [1, 1, 1, 1]


LOOKUP = {"action_required": True, "action_requests": [{"function": "lookup", "arguments": {}}]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rounds", "expected"),
    [
        ([{"extension_needed": True, **LOOKUP}, {"extension_needed": False}], [1, 1, 1, 1]),
        (
            [
                {"extension_needed": True},
                {"extension_needed": True, **LOOKUP},
                {"extension_needed": False},
            ],
            # The tool's own request renders only the first round's reply.
            [1, 1, 0, 1, 1],
        ),
    ],
    ids=["first round", "extension round"],
)
async def test_an_instruction_a_tool_stores_is_not_taken_for_the_one_carrying_the_context(
    chat_server, rounds, expected
):
    replies = []
    for n, reply in enumerate(rounds, 1):
        replies.append(_completion({"analysis": f"round {n}", **reply}))
        if "action_requests" in reply:
            replies.append(_completion({"note": "looked up"}))
    replies.append(_completion({"answer": "done"}))
    base_url, received = await chat_server(replies)
    branch = _branch(base_url)

    async def lookup():
        return await branch.operate(instruction="look something up")

    branch.register_tools(Tool(func_callable=lookup))

    async def between_rounds(branch, round_count):
        # Evict each ReAct round's instruction so far and keep the tool's, which the
        # tool's call stored after the round's own.
        instructions = [m.id for m in branch.msgs.messages if isinstance(m, Instruction)]
        react_rounds = instructions[:round_count]
        kept = [uid for uid in branch.msgs.progression if uid not in react_rounds]
        branch.metadata["current_progression"] = Progression(order=kept)

    await branch.ReAct(
        {"instruction": "pick the codes", "context": {"menu": MARKER}},
        max_extensions=len(rounds) - 1,
        between_rounds=between_rounds,
    )

    assert len(received) == len(expected)
    assert [json.dumps(body["messages"]).count(MARKER) for body in received] == expected


@pytest.mark.asyncio
async def test_a_tools_own_call_in_a_later_round_keeps_what_it_adds_beside_the_callers_context(
    chat_server,
):
    note = "tool-note-5c1e"
    base_url, received = await chat_server(
        [
            _completion({"analysis": "round 1", "extension_needed": True}),
            _completion({"analysis": "round 2", "extension_needed": False, **LOOKUP}),
            _completion({"note": "looked up"}),
            _completion({"answer": "done"}),
        ]
    )
    branch = _branch(base_url)

    async def lookup():
        return await branch.operate(
            instruction="look something up", context=[{"menu": MARKER}, {"note": note}]
        )

    branch.register_tools(Tool(func_callable=lookup))

    await branch.ReAct(
        {"instruction": "pick the codes", "context": {"menu": MARKER}}, max_extensions=1
    )

    # The tool's call, made inside the extension round, renders the history that
    # carries the caller's context: its own instruction leaves that item out and keeps
    # the item it adds.
    assert len(received) == 4
    assert [json.dumps(body["messages"]).count(MARKER) for body in received] == [1, 1, 1, 1]
    assert [json.dumps(body["messages"]).count(note) for body in received] == [0, 0, 1, 1]


@pytest.mark.asyncio
async def test_a_task_a_tool_starts_attaches_its_context_as_before_once_the_run_is_over(
    chat_server,
):
    base_url, received = await chat_server(
        [
            _completion({"analysis": "round 1", "extension_needed": True}),
            _completion({"analysis": "round 2", "extension_needed": False, **LOOKUP}),
            _completion({"answer": "done"}),
            _completion({"answer": "and after"}),
        ]
    )
    branch = _branch(base_url)
    run_over = asyncio.Event()
    started = []

    async def later():
        await run_over.wait()
        await branch.chat(instruction="and now?", context=[{"menu": MARKER}])

    async def lookup():
        started.append(asyncio.create_task(later()))
        return "started"

    branch.register_tools(Tool(func_callable=lookup))

    try:
        await branch.ReAct(
            {"instruction": "pick the codes", "context": {"menu": MARKER}}, max_extensions=1
        )
        run_over.set()
        await asyncio.gather(*started)
    finally:
        run_over.set()
        await asyncio.gather(*started, return_exceptions=True)

    # The task started inside the extension round runs its chat after the run returned,
    # so the chat attaches its context beside every copy its history renders (both
    # rounds' and the final answer's), as any call outside the run does.
    assert len(started) == 1
    assert len(received) == 4
    assert [json.dumps(body["messages"]).count(MARKER) for body in received] == [1, 1, 1, 4]


@pytest.mark.asyncio
async def test_a_tools_call_after_a_react_of_its_own_still_reads_the_rounds_history(chat_server):
    base_url, received = await chat_server(
        [
            _completion({"analysis": "round 1", "extension_needed": True}),
            _completion({"analysis": "round 2", "extension_needed": False, **LOOKUP}),
            _completion({"analysis": "inner round 1", "extension_needed": True}),
            _completion({"analysis": "inner round 2", "extension_needed": False}),
            _completion({"answer": "inner done"}),
            _completion({"note": "looked up"}),
            _completion({"answer": "done"}),
        ]
    )
    branch = _branch(base_url)

    async def lookup():
        inner = _branch(base_url)
        await inner.ReAct(
            {"instruction": "look it up", "context": {"inner": "inner-menu"}}, max_extensions=1
        )
        return await branch.operate(instruction="note what was found", context=[{"menu": MARKER}])

    branch.register_tools(Tool(func_callable=lookup))

    await branch.ReAct(
        {"instruction": "pick the codes", "context": {"menu": MARKER}}, max_extensions=1
    )

    # The ReAct the tool runs on another branch is over before the tool's own call on this
    # branch, which still reads this round's history and leaves the caller's context out.
    assert len(received) == 7
    assert [json.dumps(body["messages"]).count(MARKER) for body in received] == [
        1,
        1,
        0,
        0,
        0,
        1,
        1,
    ]


@pytest.mark.asyncio
async def test_a_tool_that_clears_the_messages_leaves_the_next_round_to_attach_the_context(
    chat_server,
):
    base_url, received = await chat_server(
        [
            _completion({"analysis": "round 1", "extension_needed": True, **LOOKUP}),
            _completion({"note": "looked up"}),
            _completion({"analysis": "round 2", "extension_needed": False}),
            _completion({"answer": "done"}),
        ]
    )
    branch = _branch(base_url)

    async def lookup():
        return await branch.operate(instruction="look something up", clear_messages=True)

    branch.register_tools(Tool(func_callable=lookup))

    await branch.ReAct(
        {"instruction": "pick the codes", "context": {"menu": MARKER}}, max_extensions=1
    )

    # The tool's call removed the first round's instruction; the extension round
    # attaches the context again and the final answer renders it from there.
    assert len(received) == 4
    assert [json.dumps(body["messages"]).count(MARKER) for body in received] == [1, 0, 1, 1]


@pytest.mark.asyncio
async def test_a_context_provider_that_clears_the_messages_leaves_its_request_to_attach_the_context(
    chat_server,
):
    base_url, received = await chat_server(
        [
            _completion({"analysis": "round 1", "extension_needed": True}),
            _completion({"analysis": "round 2", "extension_needed": False}),
            _completion({"answer": "done"}),
        ]
    )
    branch = _branch(base_url, system="the system")
    gathered = []

    class ClearOnSecondGather:
        async def provide(self, branch, instruction):
            gathered.append(instruction)
            if len(gathered) == 2:
                branch.msgs.clear_messages()

    branch.providers.register(ClearOnSecondGather(), name="clear-on-second-gather")

    await branch.ReAct(
        {"instruction": "pick the codes", "context": {"menu": MARKER}}, max_extensions=1
    )

    # The provider runs inside the extension round's call, after the call began and
    # before its request is rendered, and clears the history that carried the context:
    # that request attaches it, and the final answer renders it from there.
    assert len(gathered) == 3 and len(received) == 3
    assert [json.dumps(body["messages"]).count(MARKER) for body in received] == [1, 1, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("held", "sent"),
    [(True, 1), (0.0, -0.0)],
    ids=["true for 1", "0.0 for -0.0"],
)
async def test_an_earlier_instruction_holding_other_values_does_not_stand_in_for_the_context(
    chat_server, held, sent
):
    base_url, received = await chat_server(
        [
            _completion({"analysis": "round 1", "extension_needed": True}),
            _completion({"analysis": "round 2", "extension_needed": False}),
            _completion({"answer": "done"}),
        ]
    )
    branch = _branch(base_url)
    # Equal under ==, but rendered as different text.
    earlier = branch.msgs.add_message(
        instruction="an earlier question", context=[{"menu": MARKER, "value": held}]
    )
    branch.metadata["current_progression"] = Progression(order=[earlier.id])

    async def between_rounds(branch, round_count):
        branch.metadata["current_progression"] = Progression(order=[earlier.id])

    await branch.ReAct(
        {"instruction": "pick the codes", "context": {"menu": MARKER, "value": sent}},
        max_extensions=1,
        between_rounds=between_rounds,
    )

    # Each request renders the earlier instruction and carries the caller's own context.
    assert len(received) == 3
    assert [json.dumps(body["messages"]).count(MARKER) for body in received] == [2, 2, 2]


@pytest.mark.asyncio
async def test_an_earlier_instruction_whose_plain_content_replaces_its_context_does_not_carry_it(
    chat_server,
):
    base_url, received = await chat_server(
        [
            _completion({"analysis": "round 1", "extension_needed": True}),
            _completion({"analysis": "round 2", "extension_needed": False}),
            _completion({"answer": "done"}),
        ]
    )
    branch = _branch(base_url)
    # The instruction holds the context but renders only its plain content.
    earlier = branch.msgs.add_message(
        instruction="an earlier question",
        context=[{"menu": MARKER}],
        plain_content="an earlier question, asked in plain text",
    )
    branch.metadata["current_progression"] = Progression(order=[earlier.id])

    async def between_rounds(branch, round_count):
        branch.metadata["current_progression"] = Progression(order=[earlier.id])

    await branch.ReAct(
        {"instruction": "pick the codes", "context": {"menu": MARKER}},
        max_extensions=1,
        between_rounds=between_rounds,
    )

    assert len(received) == 3
    assert [json.dumps(body["messages"]).count(MARKER) for body in received] == [1, 1, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("menus", "counts"),
    [
        ((MARKER, "another-menu"), [2, 1, 1, 1, 1, 1]),
        (("another-menu", MARKER), [1, 1, 1, 2, 1, 1]),
    ],
    ids=["changed away from it", "changed to it"],
)
async def test_an_earlier_instruction_whose_context_changed_in_place_is_judged_as_it_renders_now(
    chat_server, menus, counts
):
    base_url, received = await chat_server(
        [
            _completion({"analysis": "round 1", "extension_needed": True}),
            _completion({"analysis": "round 2", "extension_needed": False}),
            _completion({"answer": "done"}),
        ]
        * 2
    )
    branch = _branch(base_url)
    selection = {"menu": MARKER}
    earlier = branch.msgs.add_message(instruction="an earlier question", context=[(selection,)])
    reply = branch.msgs.add_message(assistant_response="an earlier answer")

    async def between_rounds(branch, round_count):
        branch.metadata["current_progression"] = Progression(order=[earlier.id, reply.id])

    for menu in menus:
        # Before the second run the earlier instruction's context changes in place.
        selection["menu"] = menu
        await between_rounds(branch, 0)
        await branch.ReAct(
            {"instruction": "pick the codes", "context": ({"menu": MARKER},)},
            max_extensions=1,
            between_rounds=between_rounds,
        )

    # A run relies on the earlier instruction only while it renders the context.
    assert len(received) == 6
    assert [json.dumps(body["messages"]).count(MARKER) for body in received] == counts


@pytest.mark.parametrize(
    ("held", "context", "carries"),
    [
        ([{"menu": MARKER}], [{"menu": MARKER}], True),
        ([MARKER, "codes"], [MARKER, "codes"], True),
        ([MARKER], [MARKER, "codes"], False),
        ([MARKER, "codes"], [MARKER, "menus"], False),
        ([], [MARKER], False),
        ([{"menu": [MARKER, (1, 2)]}], [{"menu": [MARKER, (1, 2)]}], True),
        ([{"menu": True}], [{"menu": 1}], False),
        ([{"menu": [1, 2]}], [{"menu": (1, 2)}], True),
        ([{"menu": 0.0}], [{"menu": -0.0}], False),
        ([{1: MARKER}], [{True: MARKER}], False),
        ([MARKER, "codes"], [MARKER, MARKER], False),
    ],
    ids=[
        "equal copy",
        "every item",
        "some items",
        "another item",
        "none",
        "nested copy",
        "true is not 1",
        "a list renders as a tuple does",
        "0.0 is not -0.0",
        "a key of another type",
        "an item repeated",
    ],
)
def test_an_instruction_carries_the_context_only_when_it_renders_every_item(held, context, carries):
    instruction = Instruction(content={"instruction": "a question", "context": held})

    assert _carries_context(instruction, _render_context(context)) is carries


def test_a_second_check_over_an_unchanged_history_renders_no_held_context_again(monkeypatch):
    branch = Branch()
    for _ in range(3):
        branch.msgs.add_message(instruction="a question", context=[{"menu": "another-menu"}])
        branch.msgs.add_message(assistant_response="an answer")
    shown = _render_context([{"menu": MARKER}])
    assert _history_carriers(branch, branch.progression, shown) == []
    rendered = []
    render = _prepare._render_context
    monkeypatch.setattr(
        _prepare, "_render_context", lambda context: rendered.append(context) or render(context)
    )

    assert _history_carriers(branch, branch.progression, shown) == []
    assert rendered == []  # every held context comes from the render cache

    first = branch.msgs.messages[branch.progression[0]]
    first.content.prompt_context[0]["menu"] = MARKER
    assert _history_carriers(branch, branch.progression, shown) == [0]
    assert len(rendered) == 1  # only the context changed in place renders again


@pytest.mark.asyncio
async def test_a_provider_error_in_a_200_ends_the_run_and_is_never_read_as_a_reply(chat_server):
    base_url, received = await chat_server(
        [
            _completion({"analysis": "round 1", "extension_needed": True}),
            PROVIDER_ERROR,
            PROVIDER_ERROR,
            PROVIDER_ERROR,
        ]
    )
    branch = _branch(base_url)

    with pytest.raises(ExecutionError, match="context window"):
        await branch.ReAct({"instruction": "pick the codes"}, max_extensions=3)

    # A 400 is not retried and no later round runs.
    assert len(received) == 2
    replies = [m.response for m in branch.messages if isinstance(m, AssistantResponse)]
    assert len(replies) == 1
    assert not any("context window" in str(r) for r in replies)


@pytest.mark.asyncio
async def test_a_provider_error_on_the_final_answer_is_raised_not_replaced(chat_server):
    base_url, received = await chat_server(
        [
            _completion({"analysis": "round 1", "extension_needed": False}),
            PROVIDER_ERROR,
        ]
    )
    branch = _branch(base_url)

    with pytest.raises(ExecutionError, match="context window"):
        await branch.ReAct({"instruction": "pick the codes"}, max_extensions=3)

    assert len(received) == 2
