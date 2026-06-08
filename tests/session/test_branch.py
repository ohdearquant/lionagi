import pytest
from pydantic import BaseModel

from lionagi.operations.fields import ActionResponseModel
from lionagi.protocols.types import (
    ActionRequest,
    AssistantResponse,
    Instruction,
    LogManagerConfig,
    MessageRole,
)
from lionagi.service.manager import iModel
from lionagi.session.branch import Branch


@pytest.fixture
def branch_with_mock_imodel() -> Branch:
    """
    Creates a strongly-typed Branch with a mock chat_model & parse_model
    whose .invoke(...) always returns a MagicMock with .response = 'mocked_response'.
    """
    # 1) Create the Branch with minimal needed arguments
    branch = Branch(
        user="user",
        name="TestBranch",
        log_config=LogManagerConfig(),
    )

    # 2) Create a MagicMock simulating iModel
    mock_model = iModel(
        provider="groq",
        model="llama-3.3-70b-versatile",
    )

    async def invoke(*args, **kwargs):
        from lionagi.protocols.generic.event import EventStatus, Execution

        # Ensure messages field is present for validation
        if "messages" not in kwargs:
            kwargs["messages"] = []
        a = mock_model.create_api_calling(**kwargs)
        # Use real Execution object instead of MagicMock to avoid serialization warnings
        a.execution = Execution(
            status=EventStatus.COMPLETED,
            response="""{"foo": "mocked_response", "bar": 123}""",
            duration=0.1,
            error=None,
        )
        return a

    mock_model.invoke = invoke

    # 3) Inject it into the branch's iModelManager
    branch.mdls.register_imodel("chat", mock_model)
    branch.mdls.register_imodel("parse", mock_model)

    return branch


def test_branch_init_basic():
    branch = Branch(user="tester", name="MyBranch")
    assert branch.user == "tester"
    assert branch.name == "MyBranch"
    assert branch.msgs is not None
    assert branch.acts is not None
    assert branch.mdls is not None
    assert branch.logs is not None


def test_branch_init_system_message():
    branch = Branch(system="System online!")
    assert branch.system is not None
    assert "System online!" in branch.system.rendered
    assert len(branch.messages) == 1
    assert branch.messages[0].role == MessageRole.SYSTEM


@pytest.mark.asyncio
async def test_invoke_chat_basic(branch_with_mock_imodel: Branch):
    ins, res = await branch_with_mock_imodel.chat(
        instruction="Hello model!", return_ins_res_message=True
    )
    assert isinstance(ins, Instruction)
    assert isinstance(res, AssistantResponse)
    assert res.response == """{"foo": "mocked_response", "bar": 123}"""
    # By default, we don't store these messages
    assert len(branch_with_mock_imodel.messages) == 0


@pytest.mark.asyncio
async def test_communicate_no_validation(branch_with_mock_imodel: Branch):
    result = await branch_with_mock_imodel.communicate(
        instruction="Hello from user", skip_validation=True
    )
    assert result == """{"foo": "mocked_response", "bar": 123}"""
    # Now we should have user + assistant messages
    assert len(branch_with_mock_imodel.messages) == 2
    assert branch_with_mock_imodel.messages[0].role == MessageRole.USER
    assert branch_with_mock_imodel.messages[1].role == MessageRole.ASSISTANT


@pytest.mark.asyncio
async def test_communicate_with_response_format(branch_with_mock_imodel: Branch):
    class MyModel(BaseModel):
        foo: str = "bar"

    result = await branch_with_mock_imodel.communicate(
        instruction="We want typed output",
        response_format=MyModel,
    )
    assert result.foo == "mocked_response"
    # user + assistant stored
    msgs = branch_with_mock_imodel.messages
    assert len(msgs) == 2
    assert msgs[0].role == MessageRole.USER
    assert msgs[1].role == MessageRole.ASSISTANT


@pytest.mark.asyncio
async def test_operate_basic_flow_no_actions(branch_with_mock_imodel: Branch):
    final = await branch_with_mock_imodel.operate(
        instruction="No tools needed",
        invoke_actions=False,
        skip_validation=True,
    )
    assert final == """{"foo": "mocked_response", "bar": 123}"""
    # user + assistant stored
    assert len(branch_with_mock_imodel.messages) == 2


@pytest.mark.asyncio
async def test_operate_with_validation(branch_with_mock_imodel: Branch):
    class SomeResp(BaseModel):
        bar: int = 123

    final = await branch_with_mock_imodel.operate(
        instruction="Get typed result",
        invoke_actions=False,
        response_format=SomeResp,
    )
    assert final.bar == 123
    # user + assistant stored
    msgs = branch_with_mock_imodel.messages
    assert len(msgs) == 2


@pytest.mark.asyncio
async def test_parse_exceeds_retries_returns_value(
    branch_with_mock_imodel: Branch,
):
    from pydantic import BaseModel

    class BasicModel(BaseModel):
        bar: int
        foo: str

    # .invoke is already mocked => always "mocked_response" => won't parse => returns that string
    val = await branch_with_mock_imodel.parse(
        text="""{"foo": "mocked_response", "bar": 123}""",
        request_type=BasicModel,
        max_retries=2,
        handle_validation="return_value",
    )
    assert val == BasicModel(bar=123, foo="mocked_response")


@pytest.mark.asyncio
async def test_invoke_action_no_tools(branch_with_mock_imodel: Branch):
    req = ActionRequest(content={"function": "unregistered_tool", "arguments": {"x": 1}})
    resp = await branch_with_mock_imodel.act(req)

    # Now returns error response instead of empty list
    assert len(resp) == 1
    assert resp[0].function == "unregistered_tool"
    assert resp[0].arguments == {"x": 1}
    assert "error" in resp[0].output
    assert "not registered" in resp[0].output.get("message", "").lower()

    # logs => check the last entry for 'not registered'
    assert len(branch_with_mock_imodel.logs) == 1
    assert "not registered" in (branch_with_mock_imodel.logs[-1].content["error"] or "")


@pytest.mark.asyncio
async def test_invoke_action_ok(branch_with_mock_imodel: Branch):
    def echo_tool(text: str) -> str:
        return f"ECHO: {text}"

    # register the tool
    branch_with_mock_imodel.acts.register_tool(echo_tool)

    req = ActionRequest(content={"function": "echo_tool", "arguments": {"text": "hello"}})
    resp = await branch_with_mock_imodel.act(req)
    # Should get ActionResponseModel with output = "ECHO: hello"
    assert resp is not None
    assert resp[0].output == "ECHO: hello"
    # 2 messages => action_request + action_output
    assert len(branch_with_mock_imodel.messages) == 2
    assert branch_with_mock_imodel.messages[1].output == "ECHO: hello"


@pytest.mark.asyncio
async def test_invoke_action_suppress_errors(branch_with_mock_imodel: Branch):
    def fail_tool(**kwargs):
        raise RuntimeError("Tool error")

    branch_with_mock_imodel.acts.register_tool(fail_tool)
    req = ActionRequest(content={"function": "fail_tool", "arguments": {}})

    result = await branch_with_mock_imodel.act(req, suppress_errors=True)
    assert result == [ActionResponseModel(function="fail_tool", arguments={}, output=None)]
    logs = branch_with_mock_imodel.logs
    assert len(logs) == 1
    assert logs[-1].content["execution"]["response"] is None


def test_clone_with_id_sender(branch_with_mock_imodel: Branch):
    # add an instruction message
    msg = Instruction(
        content={"instruction": "Hello original"},
        sender=branch_with_mock_imodel.user,
        recipient=branch_with_mock_imodel.id,
    )
    branch_with_mock_imodel.messages.include(msg)

    cloned = branch_with_mock_imodel.clone(sender=msg.id)
    # cloned => has the same messages
    assert len(cloned.messages) == 1
    cm = cloned.messages[0]
    assert cm.sender == msg.id
    assert cm.recipient == cloned.id


@pytest.mark.asyncio
async def test_aclone(branch_with_mock_imodel: Branch):
    msg = Instruction(
        content={"instruction": "Async test"},
        sender=branch_with_mock_imodel.user,
        recipient=branch_with_mock_imodel.id,
    )
    branch_with_mock_imodel.messages.include(msg)

    cloned = await branch_with_mock_imodel.aclone()
    assert len(cloned.messages) == 1
    cmsg = cloned.messages[0]
    assert cmsg.recipient == cloned.id


def test_to_dict_from_dict(branch_with_mock_imodel: Branch):
    msg = Instruction(
        content={"instruction": "hello user"},
        sender=branch_with_mock_imodel.user,
        recipient=branch_with_mock_imodel.id,
    )
    branch_with_mock_imodel.messages.include(msg)

    d = branch_with_mock_imodel.to_dict()
    assert "messages" in d
    assert "chat_model" in d
    # logs only present when non-empty; parse_model only when differs from chat_model

    new_branch = Branch.from_dict(d)
    assert len(new_branch.messages) == 1
    nm = new_branch.messages[0]
    assert nm.content.instruction == "hello user"


# ---------------------------------------------------------------------------
# Edge cases (P1)
# ---------------------------------------------------------------------------


def test_branch_connect_rejects_duplicate_tool_name_without_update():
    from lionagi.service.imodel import iModel

    branch = Branch()
    imodel = iModel(provider="openai", model="gpt-4.1-mini", api_key="test_key")

    branch.connect("lookup", imodel=imodel, name="lookup")
    assert "lookup" in branch.tools

    with pytest.raises(ValueError, match="already exists"):
        branch.connect("lookup", imodel=imodel, name="lookup")

    # update=True replaces without error
    branch.connect("lookup", imodel=imodel, name="lookup", update=True)
    assert "lookup" in branch.tools


# ---------------------------------------------------------------------------
# branch.run() — lines 1370-1391
# ---------------------------------------------------------------------------


async def _drain(gen) -> list:
    out = []
    async for item in gen:
        out.append(item)
    return out


async def test_branch_run_yields_messages_from_inner_run(monkeypatch):
    from lionagi.protocols.messages import AssistantResponse, AssistantResponseContent

    branch = Branch()
    yielded = [
        AssistantResponse(
            content=AssistantResponseContent(assistant_response="streamed"),
            sender=branch.id,
            recipient="user",
        )
    ]

    async def fake_run(b, instruction, param):
        for msg in yielded:
            yield msg

    monkeypatch.setattr("lionagi.operations.run.run.run", fake_run)

    results = await _drain(branch.run("hello"))
    assert len(results) == 1
    assert results[0].response == "streamed"


async def test_branch_run_forwards_chat_model_kwarg(monkeypatch):
    """chat_model kwarg is forwarded to RunParam as imodel (line 1383-1384)."""
    from lionagi.service.imodel import iModel

    branch = Branch()
    captured_params: list = []

    async def fake_run(b, instruction, param):
        captured_params.append(param)
        return
        yield  # make it a generator

    monkeypatch.setattr("lionagi.operations.run.run.run", fake_run)

    extra_model = iModel(provider="openai", model="gpt-4.1-mini", api_key="key")
    await _drain(branch.run("hi", chat_model=extra_model))

    assert captured_params, "fake_run was not called"
    assert captured_params[0].imodel is extra_model


async def test_branch_run_forwards_persist_dir_kwarg(monkeypatch, tmp_path):
    """persist_dir kwarg is forwarded to RunParam (line 1385-1386)."""
    branch = Branch()
    captured_params: list = []

    async def fake_run(b, instruction, param):
        captured_params.append(param)
        return
        yield

    monkeypatch.setattr("lionagi.operations.run.run.run", fake_run)

    await _drain(branch.run("hi", persist_dir=tmp_path))

    assert captured_params[0].persist_dir == tmp_path


async def test_branch_run_forwards_extra_kwargs_as_imodel_kw(monkeypatch):
    """Extra **kwargs are forwarded as imodel_kw in RunParam (lines 1387-1388)."""
    branch = Branch()
    captured_params: list = []

    async def fake_run(b, instruction, param):
        captured_params.append(param)
        return
        yield

    monkeypatch.setattr("lionagi.operations.run.run.run", fake_run)

    await _drain(branch.run("hi", temperature=0.5, max_tokens=100))

    param = captured_params[0]
    assert param.imodel_kw == {"temperature": 0.5, "max_tokens": 100}


# ---------------------------------------------------------------------------
# branch.ReActStream() — lines 1235-1340
# ---------------------------------------------------------------------------


async def test_react_stream_yields_results_from_inner_generator(monkeypatch):
    """branch.ReActStream() iterates the inner ReActStream and re-yields results."""
    branch = Branch()
    sentinel = object()

    async def fake_react_stream(*args, **kwargs):
        yield sentinel

    monkeypatch.setattr("lionagi.operations.ReAct.ReAct.ReActStream", fake_react_stream)

    results = await _drain(branch.ReActStream({"instruction": "test"}))
    assert results == [sentinel]


async def test_react_stream_verbose_false_yields_raw_result(monkeypatch):
    """With verbose=False (default), results are yielded as-is (line 1340)."""
    branch = Branch()
    payload = {"analysis": "done"}

    async def fake_react_stream(*args, **kwargs):
        yield payload

    monkeypatch.setattr("lionagi.operations.ReAct.ReAct.ReActStream", fake_react_stream)

    results = await _drain(branch.ReActStream({"instruction": "q"}, verbose=False))
    assert results == [payload]


async def test_react_stream_verbose_true_yields_analysis(monkeypatch):
    """With verbose=True, result is (analysis, str_) tuple; analysis is yielded (lines 1332-1338)."""
    branch = Branch()
    analysis_obj = {"result": 42}

    async def fake_react_stream(*args, **kwargs):
        yield (analysis_obj, "some text output")

    monkeypatch.setattr("lionagi.operations.ReAct.ReAct.ReActStream", fake_react_stream)
    # as_readable needs to exist; stub it out
    monkeypatch.setattr("lionagi.libs.schema.as_readable.as_readable", lambda *a, **kw: None)

    results = await _drain(branch.ReActStream({"instruction": "q"}, verbose=True))
    assert results == [analysis_obj]


async def test_react_stream_with_interpret_builds_interpret_param(monkeypatch):
    """interpret=True causes intp_param to be constructed (lines 1251-1259)."""
    branch = Branch()
    captured_kwargs: list = []

    async def fake_react_stream(*args, **kwargs):
        captured_kwargs.append(kwargs)
        yield object()

    monkeypatch.setattr("lionagi.operations.ReAct.ReAct.ReActStream", fake_react_stream)

    await _drain(
        branch.ReActStream(
            {"instruction": "q"},
            interpret=True,
            interpret_domain="coding",
        )
    )

    assert captured_kwargs
    # intp_param was built and passed in
    assert captured_kwargs[0].get("intp_param") is not None


async def test_react_stream_with_tools_builds_action_param(monkeypatch):
    """Providing tools causes action_param to be constructed (lines 1279-1289)."""
    branch = Branch()
    captured_kwargs: list = []

    async def fake_react_stream(*args, **kwargs):
        captured_kwargs.append(kwargs)
        yield object()

    monkeypatch.setattr("lionagi.operations.ReAct.ReAct.ReActStream", fake_react_stream)

    await _drain(branch.ReActStream({"instruction": "q"}, tools=True))

    assert captured_kwargs[0].get("action_param") is not None


async def test_react_stream_with_response_format_sets_resp_ctx(monkeypatch):
    """response_format is added to resp_ctx when set (lines 1303-1306)."""
    from pydantic import BaseModel

    class Out(BaseModel):
        value: int

    branch = Branch()
    captured_kwargs: list = []

    async def fake_react_stream(*args, **kwargs):
        captured_kwargs.append(kwargs)
        yield object()

    monkeypatch.setattr("lionagi.operations.ReAct.ReAct.ReActStream", fake_react_stream)

    await _drain(
        branch.ReActStream(
            {"instruction": "q"},
            response_format=Out,
        )
    )

    resp_ctx = captured_kwargs[0].get("resp_ctx", {})
    assert resp_ctx.get("response_format") is Out


async def test_react_stream_instruct_object_converted_to_dict(monkeypatch):
    """Instruct object is converted via to_dict() before use (lines 1246-1248)."""
    from lionagi.operations.fields import Instruct

    branch = Branch()
    captured_instruction: list = []

    async def fake_react_stream(*args, **kwargs):
        captured_instruction.append(kwargs.get("instruction"))
        yield object()

    monkeypatch.setattr("lionagi.operations.ReAct.ReAct.ReActStream", fake_react_stream)

    instruct_obj = Instruct(instruction="from_object", guidance="g")
    await _drain(branch.ReActStream(instruct_obj))

    assert captured_instruction[0] == "from_object"


def test_branch_clone_rejects_invalid_sender_and_rewrites_valid_sender():
    import uuid

    branch = Branch(system="hello", user="tester")
    branch.msgs.add_message(
        instruction="test instruction",
        sender=branch.user or "user",
        recipient=branch.id,
    )

    with pytest.raises(ValueError, match="is not a valid sender"):
        branch.clone(sender="not-a-uuid")

    valid_sender_id = str(uuid.uuid4())
    clone = branch.clone(sender=valid_sender_id)
    assert clone is not branch
    for msg in clone.msgs.messages:
        assert str(msg.sender) == valid_sender_id


def test_branch_round_trips_without_duplicate_system_message():
    from lionagi.protocols.messages import System

    original = Branch(system="You are a helpful assistant.", user="tester")
    original.msgs.add_message(
        instruction="hello",
        sender=original.user or "user",
        recipient=original.id,
    )

    original_msg_count = len(original.msgs.messages)
    original_system_count = sum(1 for m in original.msgs.messages if isinstance(m, System))

    data = original.to_dict()
    restored = Branch.from_dict(data)

    restored_system_count = sum(1 for m in restored.msgs.messages if isinstance(m, System))

    assert len(restored.msgs.messages) == original_msg_count
    assert restored_system_count == original_system_count


# ---------------------------------------------------------------------------
# Edge cases: Branch.__init__ with chat_model as string / parse_model as dict
# ---------------------------------------------------------------------------


def test_branch_init_chat_model_as_string():
    branch = Branch(chat_model="gpt-4.1-mini")
    assert branch.chat_model is not None
    assert branch.chat_model.endpoint is not None


def test_branch_init_parse_model_as_dict():
    from lionagi.service.imodel import iModel

    m = iModel(provider="openai", model="gpt-4.1-mini", api_key="test_key")
    d = m.to_dict()
    branch = Branch(parse_model=d)
    assert branch.parse_model is not None


# ---------------------------------------------------------------------------
# Edge cases: Branch.to_dict() with flag combinations
# ---------------------------------------------------------------------------


def test_to_dict_include_logs_false_omits_logs():
    from lionagi.protocols.generic import Log

    branch = Branch()
    branch._log_manager.log(Log(content={"x": 1}))
    d = branch.to_dict(include_logs=False)
    assert "logs" not in d


def test_to_dict_include_log_config_true_present():
    branch = Branch()
    d = branch.to_dict(include_log_config=True)
    assert "log_config" in d
    assert isinstance(d["log_config"], dict)


def test_to_dict_include_processor_config_true_present():
    branch = Branch()
    d = branch.to_dict(include_processor_config=True)
    assert "chat_model" in d


# ---------------------------------------------------------------------------
# Edge cases: Branch.from_dict() with missing / malformed keys
# ---------------------------------------------------------------------------


def test_from_dict_minimal_empty_dict():
    branch = Branch.from_dict({})
    assert branch is not None
    assert branch.msgs is not None


def test_from_dict_malformed_messages_field():
    branch = Branch.from_dict({"messages": {"collections": [], "progression": {"order": []}}})
    assert len(branch.messages) == 0


def test_from_dict_nested_dict_with_id_key_expands():
    branch = Branch.from_dict({"user": "someone"})
    assert branch is not None


# ---------------------------------------------------------------------------
# Edge cases: Branch.progression metadata['current_progression'] override
# ---------------------------------------------------------------------------


def test_progression_metadata_current_progression_overrides_default():
    from lionagi.protocols.generic import Progression

    branch = Branch()
    custom = Progression()
    branch.metadata["current_progression"] = custom
    assert branch.progression is custom


def test_progression_metadata_none_falls_back_to_default():
    branch = Branch()
    assert branch.progression is branch._message_manager.progression


# ---------------------------------------------------------------------------
# Edge cases: Branch context manager (__aenter__ / __aexit__)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_manager_returns_branch():
    branch = Branch()
    async with branch as b:
        assert b is branch


@pytest.mark.asyncio
async def test_context_manager_aexit_calls_adump_logs(monkeypatch):
    branch = Branch()
    dumped = []

    async def fake_adump(**kwargs):
        dumped.append(kwargs)

    monkeypatch.setattr(branch._log_manager, "adump", fake_adump)
    async with branch:
        pass
    assert len(dumped) == 1
    assert dumped[0].get("clear") is True


# ---------------------------------------------------------------------------
# Edge cases: Branch._observed_run exception propagation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_observed_run_without_observer_propagates_exception():
    branch = Branch()

    async def failing_coro():
        raise ValueError("test error")

    with pytest.raises(ValueError, match="test error"):
        await branch._observed_run(failing_coro())


@pytest.mark.asyncio
async def test_observed_run_with_observer_emits_run_failed_on_exception():
    from lionagi.session.session import Session
    from lionagi.session.signal import RunFailed

    s = Session()
    branch = s.default_branch
    failures = []

    @s.observe(RunFailed)
    def on_fail(event, session):
        failures.append(event)

    async def failing_coro():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await branch._observed_run(failing_coro())

    assert len(failures) == 1
    assert isinstance(failures[0].data, RuntimeError)


@pytest.mark.asyncio
async def test_observed_run_base_exception_also_emits_run_failed():
    from lionagi.session.session import Session
    from lionagi.session.signal import RunFailed

    s = Session()
    branch = s.default_branch
    failures = []

    @s.observe(RunFailed)
    def on_fail(event, session):
        failures.append(event)

    async def base_exc_coro():
        raise KeyboardInterrupt("hard stop")

    with pytest.raises(KeyboardInterrupt):
        await branch._observed_run(base_exc_coro())

    assert len(failures) == 1


# ---------------------------------------------------------------------------
# Edge cases: Branch.clone() when chat_model.is_cli is True vs False
# ---------------------------------------------------------------------------


def test_clone_chat_model_not_cli_shares_model_reference():
    branch = Branch(chat_model="gpt-4.1-mini")
    assert not branch.chat_model.is_cli
    cloned = branch.clone()
    assert cloned.chat_model is branch.chat_model


def test_clone_with_tools_copies_tools():
    def my_tool(x: int) -> int:
        return x

    branch = Branch(tools=[my_tool])
    cloned = branch.clone()
    assert "my_tool" in cloned.tools


# ---------------------------------------------------------------------------
# Edge cases: Concurrent access to branch.messages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_aclone_and_message_include():
    import asyncio

    from lionagi.protocols.messages import Instruction

    branch = Branch(user="tester")

    async def add_messages():
        for i in range(5):
            msg = Instruction(
                content={"instruction": f"msg {i}"},
                sender=branch.user,
                recipient=branch.id,
            )
            branch.messages.include(msg)

    async def clone_branch():
        return await branch.aclone()

    results = await asyncio.gather(add_messages(), clone_branch(), return_exceptions=True)
    for r in results:
        assert not isinstance(r, Exception)
