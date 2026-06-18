# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Round-trip tests for to_dict(mode=...) / from_dict() across message types."""

import pytest

from lionagi.protocols.messages.action_request import ActionRequest, ActionRequestContent
from lionagi.protocols.messages.action_response import ActionResponse, ActionResponseContent
from lionagi.protocols.messages.assistant_response import (
    AssistantResponse,
    AssistantResponseContent,
)
from lionagi.protocols.messages.instruction import Instruction, InstructionContent
from lionagi.protocols.messages.system import System, SystemContent
from lionagi.protocols.types import MessageRole

MODES = ["python", "json", "db"]


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------


@pytest.fixture()
def system_msg():
    return System(
        content=SystemContent(system_message="You are helpful.", system_datetime="2024-01-01T12:00")
    )


@pytest.mark.parametrize("mode", MODES)
def test_system_roundtrip(system_msg, mode):
    d = system_msg.to_dict(mode=mode)
    restored = System.from_dict(d)
    assert restored.content.system_message == system_msg.content.system_message
    assert restored.content.system_datetime == system_msg.content.system_datetime
    assert str(restored.id) == str(system_msg.id)
    assert restored.role == MessageRole.SYSTEM


@pytest.mark.parametrize("mode", MODES)
def test_system_to_dict_has_role(system_msg, mode):
    d = system_msg.to_dict(mode=mode)
    assert d["role"] == "system"


def test_system_db_mode_uses_node_metadata(system_msg):
    d = system_msg.to_dict(mode="db")
    assert "node_metadata" in d
    assert "metadata" not in d


def test_system_python_mode_uses_metadata(system_msg):
    d = system_msg.to_dict(mode="python")
    assert "metadata" in d
    assert "node_metadata" not in d


# ---------------------------------------------------------------------------
# Instruction
# ---------------------------------------------------------------------------


@pytest.fixture()
def instruction_msg():
    return Instruction(
        content=InstructionContent(
            instruction="Do something useful.",
            guidance="Be concise.",
            prompt_context=["context line 1", "context line 2"],
        )
    )


@pytest.mark.parametrize("mode", MODES)
def test_instruction_roundtrip(instruction_msg, mode):
    d = instruction_msg.to_dict(mode=mode)
    restored = Instruction.from_dict(d)
    assert restored.content.instruction == instruction_msg.content.instruction
    assert restored.content.guidance == instruction_msg.content.guidance
    assert restored.content.prompt_context == instruction_msg.content.prompt_context
    assert str(restored.id) == str(instruction_msg.id)
    assert restored.role == MessageRole.USER


@pytest.mark.parametrize("mode", MODES)
def test_instruction_to_dict_has_role(instruction_msg, mode):
    d = instruction_msg.to_dict(mode=mode)
    assert d["role"] == "user"


def test_instruction_db_mode_uses_node_metadata(instruction_msg):
    d = instruction_msg.to_dict(mode="db")
    assert "node_metadata" in d
    assert "metadata" not in d


# ---------------------------------------------------------------------------
# AssistantResponse
# ---------------------------------------------------------------------------


@pytest.fixture()
def assistant_msg():
    return AssistantResponse(
        content=AssistantResponseContent(assistant_response="Here is my answer."),
        sender=MessageRole.ASSISTANT,
        recipient=MessageRole.USER,
    )


@pytest.mark.parametrize("mode", MODES)
def test_assistant_response_roundtrip(assistant_msg, mode):
    d = assistant_msg.to_dict(mode=mode)
    restored = AssistantResponse.from_dict(d)
    assert restored.content.assistant_response == assistant_msg.content.assistant_response
    assert str(restored.id) == str(assistant_msg.id)
    assert restored.role == MessageRole.ASSISTANT


@pytest.mark.parametrize("mode", MODES)
def test_assistant_response_to_dict_has_role(assistant_msg, mode):
    d = assistant_msg.to_dict(mode=mode)
    assert d["role"] == "assistant"


def test_assistant_response_db_mode_uses_node_metadata(assistant_msg):
    d = assistant_msg.to_dict(mode="db")
    assert "node_metadata" in d
    assert "metadata" not in d


# ---------------------------------------------------------------------------
# ActionRequest
# ---------------------------------------------------------------------------


@pytest.fixture()
def action_request_msg():
    return ActionRequest(
        content=ActionRequestContent(
            function="my_tool",
            arguments={"x": 1, "y": "hello"},
        )
    )


@pytest.mark.parametrize("mode", MODES)
def test_action_request_roundtrip(action_request_msg, mode):
    d = action_request_msg.to_dict(mode=mode)
    restored = ActionRequest.from_dict(d)
    assert restored.content.function == action_request_msg.content.function
    assert restored.content.arguments == action_request_msg.content.arguments
    assert str(restored.id) == str(action_request_msg.id)
    assert restored.role == MessageRole.ACTION


@pytest.mark.parametrize("mode", MODES)
def test_action_request_to_dict_has_role(action_request_msg, mode):
    d = action_request_msg.to_dict(mode=mode)
    assert d["role"] == "action"


def test_action_request_db_mode_uses_node_metadata(action_request_msg):
    d = action_request_msg.to_dict(mode="db")
    assert "node_metadata" in d
    assert "metadata" not in d


def test_action_request_with_response_id_roundtrip():
    msg = ActionRequest(
        content=ActionRequestContent(
            function="tool_x",
            arguments={"n": 42},
            action_response_id="resp-abc",
        )
    )
    for mode in MODES:
        d = msg.to_dict(mode=mode)
        restored = ActionRequest.from_dict(d)
        assert restored.content.action_response_id == "resp-abc"


# ---------------------------------------------------------------------------
# ActionResponse
# ---------------------------------------------------------------------------


@pytest.fixture()
def action_response_msg():
    return ActionResponse(
        content=ActionResponseContent(
            function="my_tool",
            arguments={"x": 1},
            output={"result": "ok", "count": 3},
            action_request_id="req-123",
        )
    )


@pytest.mark.parametrize("mode", MODES)
def test_action_response_roundtrip(action_response_msg, mode):
    d = action_response_msg.to_dict(mode=mode)
    restored = ActionResponse.from_dict(d)
    assert restored.content.function == action_response_msg.content.function
    assert restored.content.arguments == action_response_msg.content.arguments
    assert restored.content.output == action_response_msg.content.output
    assert restored.content.action_request_id == action_response_msg.content.action_request_id
    assert str(restored.id) == str(action_response_msg.id)
    assert restored.role == MessageRole.ACTION


@pytest.mark.parametrize("mode", MODES)
def test_action_response_to_dict_has_role(action_response_msg, mode):
    d = action_response_msg.to_dict(mode=mode)
    assert d["role"] == "action"


def test_action_response_db_mode_uses_node_metadata(action_response_msg):
    d = action_response_msg.to_dict(mode="db")
    assert "node_metadata" in d
    assert "metadata" not in d


def test_action_response_with_error_roundtrip():
    msg = ActionResponse(
        content=ActionResponseContent(
            function="failing_tool",
            arguments={},
            output=None,
            error="Tool raised ValueError",
        )
    )
    for mode in MODES:
        d = msg.to_dict(mode=mode)
        restored = ActionResponse.from_dict(d)
        assert restored.content.error == "Tool raised ValueError"
        assert restored.content.output is None


# ---------------------------------------------------------------------------
# Cross-mode parity: python and db modes produce equal restored objects
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cls,obj",
    [
        (System, System(content=SystemContent(system_message="Parity check"))),
        (
            Instruction,
            Instruction(content=InstructionContent(instruction="Parity", guidance="Check")),
        ),
        (
            AssistantResponse,
            AssistantResponse(content=AssistantResponseContent(assistant_response="parity")),
        ),
        (
            ActionRequest,
            ActionRequest(content=ActionRequestContent(function="f", arguments={"k": "v"})),
        ),
        (
            ActionResponse,
            ActionResponse(
                content=ActionResponseContent(function="f", arguments={}, output="done")
            ),
        ),
    ],
)
def test_python_and_db_modes_produce_equal_content(cls, obj):
    d_py = obj.to_dict(mode="python")
    d_db = obj.to_dict(mode="db")
    restored_py = cls.from_dict(d_py)
    restored_db = cls.from_dict(d_db)
    assert restored_py.content.to_dict() == restored_db.content.to_dict()
    assert str(restored_py.id) == str(restored_db.id)
