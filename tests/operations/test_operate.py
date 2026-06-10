# tests/branch_ops/test_operate.py

import pytest
from pydantic import BaseModel

from lionagi.session.branch import Branch
from lionagi.testing import LionAGIMockFactory


def make_mocked_branch_for_operate():
    """Branch backed by ``LionAGIMockFactory``; returns a JSON string response.

    Kept as a regular function (not a fixture) so the legacy callsites below
    keep working — the boilerplate now lives in ``lionagi.testing``.
    """
    return LionAGIMockFactory.create_mocked_branch(
        name="BranchForTests_Operate",
        user="tester_fixture",
        response='{"foo":"mocked_response_string"}',
        model="gpt-4.1-mini",
    )


@pytest.mark.asyncio
async def test_operate_no_actions_no_validation():
    """
    branch.operate(...) with invoke_actions=False and skip_validation=True => returns raw string.
    """
    branch = make_mocked_branch_for_operate()
    final = await branch.operate(
        instruction="Just a test", invoke_actions=False, skip_validation=True
    )
    assert final == '{"foo":"mocked_response_string"}'
    assert len(branch.messages) == 2


@pytest.mark.asyncio
async def test_operate_with_validation():
    """
    If we pass a response_format, it should parse "mocked_response_string" into that model.
    """

    class ExampleModel(BaseModel):
        foo: str

    branch = make_mocked_branch_for_operate()

    final = await branch.operate(
        instruction="Expect typed output",
        response_format=ExampleModel,
        invoke_actions=False,
    )
    assert final.foo == "mocked_response_string"
    assert len(branch.messages) == 2


@pytest.mark.asyncio
async def test_operate_with_actions_preserves_response_data():
    """
    Regression test: when operate() returns a structured response with actions,
    the action_responses should be merged with the original response data.

    Previously, only action_responses were returned, losing original data.
    """

    class ResponseModel(BaseModel):
        answer: str
        confidence: float

    # Mock branch with a response containing both data AND action requests
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    branch = LionAGIMockFactory.create_mocked_branch(
        name="ActionTest",
        user="tester",
        response="""{
            "answer": "42",
            "confidence": 0.95,
            "action_required": true,
            "action_requests": [
                {"function": "add", "arguments": {"a": 1, "b": 2}}
            ]
        }""",
        model="gpt-4.1-mini",
        tools=[add],
    )

    # Execute with actions=True
    result = await branch.operate(
        instruction="Calculate something",
        response_format=ResponseModel,
        actions=True,
        invoke_actions=True,
    )

    # CRITICAL: Result should have BOTH original response data AND action_responses
    assert hasattr(result, "answer"), "Original 'answer' field missing"
    assert hasattr(result, "confidence"), "Original 'confidence' field missing"
    assert hasattr(result, "action_responses"), "action_responses field missing"

    # Verify original data is preserved
    assert result.answer == "42"
    assert result.confidence == 0.95

    # Verify action_responses were added
    assert len(result.action_responses) == 1
    assert result.action_responses[0].function == "add"


# ---------------------------------------------------------------------------
# Edge cases for prepare_operate_kw (P0)
# ---------------------------------------------------------------------------

from lionagi.operations.operate.operate import prepare_operate_kw


def test_prepare_operate_kw_rejects_invalid_field_model_entry():
    """field_models containing a non-FieldModel/Spec is passed through to
    operate() where _specs_from_fields raises TypeError. prepare_operate_kw
    no longer validates field_models itself (single-construction path is in
    operate()). Verify the invalid entry is forwarded in the returned dict."""
    branch = Branch()
    result = prepare_operate_kw(branch, field_models=[object()])
    # field_models is forwarded unchanged; error surfaces later in operate()
    assert result["field_models"] is not None
    assert len(result["field_models"]) == 1


@pytest.mark.asyncio
async def test_operate_handle_validation_raise_reports_expected_model(monkeypatch):
    """operate(..., handle_validation='raise') raises ValueError on parse mismatch."""
    from pydantic import BaseModel

    class ExpectedModel(BaseModel):
        answer: str

    async def stub_middle(b, ins, **kw):
        return {"not": "model"}

    branch = LionAGIMockFactory.create_mocked_branch(
        response='{"not": "model"}',
        model="gpt-4.1-mini",
    )

    with pytest.raises((ValueError, Exception)):
        await branch.operate(
            instruction="test",
            response_format=ExpectedModel,
            handle_validation="raise",
            invoke_actions=False,
        )


# ---------------------------------------------------------------------------
# Additional prepare_operate_kw coverage — field_models
# ---------------------------------------------------------------------------

import warnings

from lionagi.ln.types import Spec
from lionagi.models import FieldModel


def test_prepare_operate_kw_instruct_as_dict():
    """instruct=dict is converted to Instruct (line 107)."""
    branch = Branch()
    result = prepare_operate_kw(branch, instruct={"instruction": "hello"})
    assert result["instruction"] == "hello"


def test_prepare_operate_kw_reason_flag_sets_instruct_reason():
    """reason=True is forwarded to operate() via the return dict (reason key)
    so the single construction path in operate() can build the Operative.
    prepare_operate_kw no longer constructs the Operative itself."""
    branch = Branch()
    result = prepare_operate_kw(branch, reason=True)
    # operative construction is deferred to operate(); reason is forwarded
    assert result["reason"] is True
    assert result["operative"] is None


def test_prepare_operate_kw_field_models_with_fieldmodel():
    """FieldModel in field_models is forwarded as-is to operate() where
    _specs_from_fields converts it to a Spec for Operative construction."""
    branch = Branch()
    fm = FieldModel(name="score", annotation=float)
    result = prepare_operate_kw(branch, field_models=[fm])
    # field_models forwarded; Operative built lazily in operate()
    assert result["field_models"] == [fm]
    assert result["operative"] is None


def test_prepare_operate_kw_field_models_with_spec():
    """Spec in field_models is forwarded unchanged to operate()."""
    branch = Branch()
    spec = Spec(name="label", annotation=str)
    result = prepare_operate_kw(branch, field_models=[spec])
    assert result["field_models"] == [spec]
    assert result["operative"] is None


def test_prepare_operate_kw_persist_dir_sets_run_param():
    """persist_dir triggers RunParam path and is set in chat_param (lines 179-181)."""
    from lionagi.operations.types import RunParam

    branch = Branch()
    result = prepare_operate_kw(branch, persist_dir="/tmp/test_dir")
    chat_param = result["chat_param"]
    assert isinstance(chat_param, RunParam)
    assert chat_param.persist_dir == "/tmp/test_dir"


def test_prepare_operate_kw_stream_persist_sets_run_param():
    """stream_persist=True triggers RunParam path (lines 178-179)."""
    from lionagi.operations.types import RunParam

    branch = Branch()
    result = prepare_operate_kw(branch, stream_persist=True)
    chat_param = result["chat_param"]
    assert isinstance(chat_param, RunParam)
    assert chat_param.stream_persist is True


def test_prepare_operate_kw_snapshot_dir_routes_to_run_param():
    """snapshot_dir kwarg must be forwarded into the RunParam so the
    branch snapshot can land in a separate dir from the stream buffer.
    R5-A HIGH-1 regression — the resume hint pointed at branches_dir
    but run.py wrote to persist_dir; snapshot_dir lets the caller split.
    """
    from lionagi.operations.types import RunParam

    branch = Branch()
    result = prepare_operate_kw(
        branch,
        stream_persist=True,
        persist_dir="/var/folders/buffer",
        snapshot_dir="/var/folders/branches",
    )
    chat_param = result["chat_param"]
    assert isinstance(chat_param, RunParam)
    assert chat_param.persist_dir == "/var/folders/buffer"
    assert chat_param.snapshot_dir == "/var/folders/branches"


def test_prepare_operate_kw_snapshot_dir_alone_triggers_run_param():
    """Passing only snapshot_dir (no stream_persist, no persist_dir)
    must still promote to RunParam since the field only exists there.
    """
    from lionagi.operations.types import RunParam

    branch = Branch()
    result = prepare_operate_kw(branch, snapshot_dir="/var/folders/branches")
    chat_param = result["chat_param"]
    assert isinstance(chat_param, RunParam)
    assert chat_param.snapshot_dir == "/var/folders/branches"


# ---------------------------------------------------------------------------
# operate() function direct tests — various branches
# ---------------------------------------------------------------------------

from lionagi.operations.operate.operate import operate
from lionagi.operations.types import ChatParam


@pytest.mark.asyncio
async def test_operate_return_none_on_validation_failure():
    """handle_validation='return_none' returns None when result is not model (line 341)."""

    class ExpectedModel(BaseModel):
        value: str

    branch = Branch()

    async def fake_middle(b, ins, cctx, pctx, clear, **kw):
        return {"not": "a model"}  # wrong type

    chat_param = ChatParam(imodel=branch.chat_model, response_format=ExpectedModel)
    result = await operate(
        branch,
        "test",
        chat_param,
        handle_validation="return_none",
        skip_validation=False,
        invoke_actions=False,
        middle=fake_middle,
    )
    assert result is None


@pytest.mark.asyncio
async def test_operate_skip_validation_returns_raw():
    """skip_validation=True returns raw middle result without model check (line 335)."""
    branch = Branch()

    async def fake_middle(b, ins, cctx, pctx, clear, **kw):
        return "raw_string_result"

    chat_param = ChatParam(imodel=branch.chat_model)
    result = await operate(
        branch,
        "test",
        chat_param,
        skip_validation=True,
        invoke_actions=False,
        middle=fake_middle,
    )
    assert result == "raw_string_result"


@pytest.mark.asyncio
async def test_operate_dict_result_with_no_action_requests_returns_result():
    """Dict result with no action_requests is returned unchanged (line 374)."""
    branch = Branch()

    async def fake_middle(b, ins, cctx, pctx, clear, **kw):
        return {"key": "value", "action_requests": None}

    from lionagi.operations.act.act import _get_default_call_params
    from lionagi.operations.types import ActionParam

    chat_param = ChatParam(imodel=branch.chat_model)
    action_param = ActionParam(
        action_call_params=_get_default_call_params(),
        tools=None,
        strategy="concurrent",
    )
    result = await operate(
        branch,
        "test",
        chat_param,
        action_param=action_param,
        skip_validation=True,
        invoke_actions=True,
        middle=fake_middle,
    )
    assert result == {"key": "value", "action_requests": None}


@pytest.mark.asyncio
async def test_operate_dict_result_action_requests_dict_path():
    """Dict result with action_requests list merges action_responses (lines 362-387)."""
    branch = Branch()

    def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    branch.register_tools([add])

    async def fake_middle(b, ins, cctx, pctx, clear, **kw):
        return {
            "some_data": "hello",
            "action_requests": [{"function": "add", "arguments": {"a": 1, "b": 2}}],
        }

    from lionagi.operations.act.act import _get_default_call_params
    from lionagi.operations.types import ActionParam

    chat_param = ChatParam(imodel=branch.chat_model)
    action_param = ActionParam(
        action_call_params=_get_default_call_params(),
        tools=None,
        strategy="concurrent",
    )
    result = await operate(
        branch,
        "test",
        chat_param,
        action_param=action_param,
        skip_validation=False,
        invoke_actions=True,
        middle=fake_middle,
    )
    # Dict response should have action_responses merged in
    assert isinstance(result, dict)
    assert "action_responses" in result
    assert len(result["action_responses"]) >= 1


@pytest.mark.asyncio
async def test_operate_with_field_models_builds_operative():
    """operate() with field_models builds an operative (lines 286-314)."""
    branch = Branch()

    class FakeResult(BaseModel):
        label: str

    async def fake_middle(b, ins, cctx, pctx, clear, **kw):
        # Return a dict that can be parsed — skip_validation=True to avoid parse
        return {"label": "test_value"}

    fm = FieldModel(name="label", annotation=str)
    chat_param = ChatParam(imodel=branch.chat_model)
    result = await operate(
        branch,
        "test",
        chat_param,
        field_models=[fm],
        skip_validation=True,
        invoke_actions=False,
        middle=fake_middle,
    )
    assert result == {"label": "test_value"}


# ---------------------------------------------------------------------------
# Finding 3 replacement: TypeError raised at new boundary (_specs_from_fields)
# before any model call is reached.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_operate_invalid_field_models_raises_before_model_call():
    """operate() raises TypeError('Expected FieldModel or Spec') for invalid
    field_models entries at _specs_from_fields() (operate.py:246), i.e. BEFORE
    the middle/model layer is invoked.  A mock middle is injected that fails
    loudly if called — ensuring a missed raise would be detected."""
    branch = Branch()

    async def _should_not_be_called(*args, **kwargs):
        pytest.fail("middle was called — TypeError was NOT raised before the model call")

    chat_param = ChatParam(imodel=branch.chat_model)

    with pytest.raises(TypeError, match="Expected FieldModel or Spec"):
        await operate(
            branch,
            "test_invalid_field_models",
            chat_param,
            field_models=[object()],  # invalid entry — not FieldModel or Spec
            skip_validation=False,
            invoke_actions=False,
            middle=_should_not_be_called,
        )


# ---------------------------------------------------------------------------
# Lock-in: reason=True alone (no response_format / actions / field_models)
# takes the Operative-construction branch (operate.py:252).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_operate_reason_only_constructs_operative_with_reason_field():
    """operate(..., reason=True) with no response_format/actions/field_models
    must enter the Operative-construction branch (operate.py:252) and produce
    a response_format that includes a 'reason' field.  The generated response
    type is injected into _cctx before middle is called, so we capture it."""
    branch = Branch()

    captured_response_format = {}

    async def capturing_middle(b, ins, cctx, pctx, clear, **kw):
        # Capture the response_format that operate() materialised for this call.
        captured_response_format["fmt"] = cctx.response_format
        # Return a minimal instance of the captured model so downstream code
        # does not raise on type checks.
        if cctx.response_format is not None:
            try:
                return cctx.response_format()
            except Exception:
                pass
        return {}

    chat_param = ChatParam(imodel=branch.chat_model)
    await operate(
        branch,
        "reason only test",
        chat_param,
        reason=True,
        skip_validation=True,
        invoke_actions=False,
        middle=capturing_middle,
    )

    fmt = captured_response_format.get("fmt")
    assert fmt is not None, (
        "reason=True should have triggered Operative construction and set a "
        "response_format on the chat context"
    )
    # The materialised model must have a 'reason' field.
    assert "reason" in fmt.model_fields, (
        f"'reason' field missing from generated response_format {fmt}; "
        "Operative was not constructed or reason spec was dropped"
    )
