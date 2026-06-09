# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Comprehensive tests for select operations."""

from enum import Enum
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel

from lionagi.operations.select.select import select, select_v1
from lionagi.operations.select.utils import SelectionModel
from lionagi.session.branch import Branch


class TestSelectBasic:
    @pytest.mark.asyncio
    async def test_select_with_string_list(self):
        # Create mock branch
        branch = MagicMock(spec=Branch)

        # Mock the operate method to return SelectionModel
        async def mock_operate(**kwargs):
            return SelectionModel(selected=["option1"])

        branch.operate = AsyncMock(side_effect=mock_operate)

        result = await select(
            branch=branch,
            instruct={"instruction": "Choose best option"},
            choices=["option1", "option2", "option3"],
            max_num_selections=1,
        )

        assert isinstance(result, SelectionModel)
        assert result.selected == ["option1"]
        branch.operate.assert_called_once()

    @pytest.mark.asyncio
    async def test_select_with_return_branch(self):
        branch = MagicMock(spec=Branch)

        async def mock_operate(**kwargs):
            return SelectionModel(selected=["choice1"])

        branch.operate = AsyncMock(side_effect=mock_operate)

        result, returned_branch = await select(
            branch=branch,
            instruct={"instruction": "Select"},
            choices=["choice1", "choice2"],
            return_branch=True,
        )

        assert isinstance(result, SelectionModel)
        assert returned_branch == branch

    @pytest.mark.asyncio
    async def test_select_branch_creation_backwards_compat(self):
        # Mock Branch class to avoid real API calls
        mock_branch_instance = MagicMock(spec=Branch)

        async def mock_operate(**kwargs):
            return SelectionModel(selected=["a"])

        mock_branch_instance.operate = AsyncMock(side_effect=mock_operate)

        # When branch is None and branch_kwargs provided
        with patch("lionagi.session.branch.Branch", return_value=mock_branch_instance):
            result = await select(
                branch=None,
                instruct={"instruction": "Select"},
                choices=["a", "b"],
                branch_kwargs={"user": "test", "name": "TestBranch"},
                return_branch=True,
            )

        # Should return tuple
        assert isinstance(result, tuple)
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_select_verbose_mode(self, caplog):
        branch = MagicMock(spec=Branch)

        async def mock_operate(**kwargs):
            return SelectionModel(selected=["option1"])

        branch.operate = AsyncMock(side_effect=mock_operate)

        with caplog.at_level("DEBUG", logger="lionagi.operations.select.select"):
            await select(
                branch=branch,
                instruct={"instruction": "Choose"},
                choices=["option1", "option2"],
                verbose=True,
            )

        assert "Starting selection" in caplog.text


class TestSelectV1StringChoices:
    @pytest.mark.asyncio
    async def test_select_v1_single_selection(self):
        branch = MagicMock(spec=Branch)

        async def mock_operate(**kwargs):
            return SelectionModel(selected=["apple"])

        branch.operate = AsyncMock(side_effect=mock_operate)

        result = await select_v1(
            branch=branch,
            instruct={"instruction": "Choose a fruit"},
            choices=["apple", "banana", "orange"],
            max_num_selections=1,
        )

        assert isinstance(result, SelectionModel)
        assert "apple" in result.selected

        # Verify operate was called with correct format
        call_kwargs = branch.operate.call_args[1]
        assert call_kwargs["response_format"] == SelectionModel
        assert "instruction" in call_kwargs
        assert "context" in call_kwargs

    @pytest.mark.asyncio
    async def test_select_v1_multiple_selections(self):
        branch = MagicMock(spec=Branch)

        async def mock_operate(**kwargs):
            return SelectionModel(selected=["apple", "banana"])

        branch.operate = AsyncMock(side_effect=mock_operate)

        result = await select_v1(
            branch=branch,
            instruct={"instruction": "Choose fruits"},
            choices=["apple", "banana", "orange", "grape"],
            max_num_selections=2,
        )

        assert len(result.selected) == 2
        assert "apple" in result.selected
        assert "banana" in result.selected

    @pytest.mark.asyncio
    async def test_select_v1_verbose_mode(self, caplog):
        branch = MagicMock(spec=Branch)

        async def mock_operate(**kwargs):
            return SelectionModel(selected=["choice1"])

        branch.operate = AsyncMock(side_effect=mock_operate)

        with caplog.at_level("DEBUG", logger="lionagi.operations.select.select"):
            await select_v1(
                branch=branch,
                instruct={"instruction": "Select"},
                choices=["choice1", "choice2"],
                verbose=True,
            )

        assert "Received selection" in caplog.text


class TestSelectV1EnumChoices:
    @pytest.mark.asyncio
    async def test_select_v1_with_enum(self):

        class Color(Enum):
            RED = "red_value"
            GREEN = "green_value"
            BLUE = "blue_value"

        branch = MagicMock(spec=Branch)

        async def mock_operate(**kwargs):
            # Return the enum name that will be parsed
            return SelectionModel(selected=["RED"])

        branch.operate = AsyncMock(side_effect=mock_operate)

        result = await select_v1(
            branch=branch,
            instruct={"instruction": "Choose a color"},
            choices=Color,
            max_num_selections=1,
        )

        assert isinstance(result, SelectionModel)
        # Should be parsed to actual enum value
        assert Color.RED in result.selected

    @pytest.mark.asyncio
    async def test_select_v1_enum_multiple_selections(self):

        class Priority(Enum):
            LOW = 1
            MEDIUM = 2
            HIGH = 3
            CRITICAL = 4

        branch = MagicMock(spec=Branch)

        async def mock_operate(**kwargs):
            return SelectionModel(selected=["HIGH", "CRITICAL"])

        branch.operate = AsyncMock(side_effect=mock_operate)

        result = await select_v1(
            branch=branch,
            instruct={"instruction": "Select priorities"},
            choices=Priority,
            max_num_selections=2,
        )

        assert len(result.selected) == 2
        assert Priority.HIGH in result.selected
        assert Priority.CRITICAL in result.selected


class TestSelectV1DictChoices:
    @pytest.mark.asyncio
    async def test_select_v1_with_dict(self):
        choices = {
            "option_a": "First option description",
            "option_b": "Second option description",
            "option_c": "Third option description",
        }

        branch = MagicMock(spec=Branch)

        async def mock_operate(**kwargs):
            return SelectionModel(selected=["option_a"])

        branch.operate = AsyncMock(side_effect=mock_operate)

        result = await select_v1(
            branch=branch,
            instruct={"instruction": "Choose best option"},
            choices=choices,
            max_num_selections=1,
        )

        # Should return the value from dict
        assert "First option description" in result.selected

    @pytest.mark.asyncio
    async def test_select_v1_dict_multiple(self):
        choices = {
            "key1": {"nested": "value1"},
            "key2": {"nested": "value2"},
            "key3": {"nested": "value3"},
        }

        branch = MagicMock(spec=Branch)

        async def mock_operate(**kwargs):
            return SelectionModel(selected=["key1", "key3"])

        branch.operate = AsyncMock(side_effect=mock_operate)

        result = await select_v1(
            branch=branch,
            instruct={"instruction": "Select items"},
            choices=choices,
            max_num_selections=2,
        )

        assert len(result.selected) == 2


class TestSelectV1ModelChoices:
    @pytest.mark.asyncio
    async def test_select_v1_with_basemodel_instances(self):

        class Option(BaseModel):
            name: str
            value: int

        choices = [
            Option(name="first", value=1),
            Option(name="second", value=2),
            Option(name="third", value=3),
        ]

        branch = MagicMock(spec=Branch)

        async def mock_operate(**kwargs):
            # Return class name
            return SelectionModel(selected=["Option"])

        branch.operate = AsyncMock(side_effect=mock_operate)

        result = await select_v1(
            branch=branch,
            instruct={"instruction": "Select option"},
            choices=choices,
            max_num_selections=1,
        )

        # Should have selected something
        assert len(result.selected) > 0

    @pytest.mark.asyncio
    async def test_select_v1_with_basemodel_classes(self):

        class OptionA(BaseModel):
            field_a: str

        class OptionB(BaseModel):
            field_b: int

        choices = [OptionA, OptionB]

        branch = MagicMock(spec=Branch)

        async def mock_operate(**kwargs):
            return SelectionModel(selected=["OptionA"])

        branch.operate = AsyncMock(side_effect=mock_operate)

        result = await select_v1(
            branch=branch,
            instruct={"instruction": "Select model type"},
            choices=choices,
            max_num_selections=1,
        )

        assert len(result.selected) > 0


class TestSelectV1InstructHandling:
    @pytest.mark.asyncio
    async def test_select_v1_with_instruct_object(self):
        from lionagi.operations.fields import Instruct

        instruct = Instruct(instruction="Choose the best", context={"data": "value"})

        branch = MagicMock(spec=Branch)

        async def mock_operate(**kwargs):
            return SelectionModel(selected=["choice1"])

        branch.operate = AsyncMock(side_effect=mock_operate)

        result = await select_v1(
            branch=branch,
            instruct=instruct,
            choices=["choice1", "choice2"],
        )

        # Verify instruct was converted to dict and used
        call_kwargs = branch.operate.call_args[1]
        assert "instruction" in call_kwargs
        assert "Choose the best" in call_kwargs["instruction"]

    @pytest.mark.asyncio
    async def test_select_v1_with_dict_instruct(self):
        instruct_dict = {
            "instruction": "Select item",
            "context": [{"key": "value"}],
        }

        branch = MagicMock(spec=Branch)

        async def mock_operate(**kwargs):
            return SelectionModel(selected=["item1"])

        branch.operate = AsyncMock(side_effect=mock_operate)

        result = await select_v1(
            branch=branch,
            instruct=instruct_dict,
            choices=["item1", "item2"],
        )

        call_kwargs = branch.operate.call_args[1]
        assert "instruction" in call_kwargs
        assert "Select item" in call_kwargs["instruction"]

    @pytest.mark.asyncio
    async def test_select_v1_empty_instruct(self):
        branch = MagicMock(spec=Branch)

        async def mock_operate(**kwargs):
            return SelectionModel(selected=["opt1"])

        branch.operate = AsyncMock(side_effect=mock_operate)

        result = await select_v1(
            branch=branch,
            instruct=None,
            choices=["opt1", "opt2"],
        )

        # Should still work with selection prompt
        call_kwargs = branch.operate.call_args[1]
        assert "instruction" in call_kwargs

    @pytest.mark.asyncio
    async def test_select_v1_context_extension(self):
        instruct_dict = {
            "instruction": "Select",
            "context": [{"existing": "data"}],
        }

        branch = MagicMock(spec=Branch)

        async def mock_operate(**kwargs):
            return SelectionModel(selected=["a"])

        branch.operate = AsyncMock(side_effect=mock_operate)

        await select_v1(
            branch=branch,
            instruct=instruct_dict,
            choices=["a", "b", "c"],
        )

        call_kwargs = branch.operate.call_args[1]
        context = call_kwargs["context"]

        # Should have original context + choice representations
        assert len(context) >= 4  # 1 existing + 3 choices


class TestSelectV1ResponseParsing:
    @pytest.mark.asyncio
    async def test_select_v1_list_response(self):
        branch = MagicMock(spec=Branch)

        # SelectionModel requires list for selected field
        async def mock_operate(**kwargs):
            model = SelectionModel(selected=["single_choice"])
            return model

        branch.operate = AsyncMock(side_effect=mock_operate)

        result = await select_v1(
            branch=branch,
            instruct={"instruction": "Select"},
            choices=["single_choice", "other"],
        )

        # Verify it's a list
        assert isinstance(result.selected, list)
        assert len(result.selected) == 1

    @pytest.mark.asyncio
    async def test_select_v1_dict_response(self):
        branch = MagicMock(spec=Branch)

        async def mock_operate(**kwargs):
            # Return dict instead of model
            return {"selected": ["choice1"]}

        branch.operate = AsyncMock(side_effect=mock_operate)

        result = await select_v1(
            branch=branch,
            instruct={"instruction": "Select"},
            choices=["choice1", "choice2"],
        )

        # Should handle dict response
        assert result["selected"] is not None


class TestSelectV1EdgeCases:
    @pytest.mark.asyncio
    async def test_select_v1_empty_choices(self):
        branch = MagicMock(spec=Branch)

        async def mock_operate(**kwargs):
            return SelectionModel(selected=[])

        branch.operate = AsyncMock(side_effect=mock_operate)

        # Should handle empty choices without error
        result = await select_v1(
            branch=branch,
            instruct={"instruction": "Select"},
            choices=[],
        )

        assert result.selected == []

    @pytest.mark.asyncio
    async def test_select_v1_max_selections_zero(self):
        branch = MagicMock(spec=Branch)

        async def mock_operate(**kwargs):
            return SelectionModel(selected=[])

        branch.operate = AsyncMock(side_effect=mock_operate)

        result = await select_v1(
            branch=branch,
            instruct={"instruction": "Select"},
            choices=["a", "b"],
            max_num_selections=0,
        )

        assert result is not None

    @pytest.mark.asyncio
    async def test_select_v1_operate_kwargs_passed(self):
        branch = MagicMock(spec=Branch)

        async def mock_operate(**kwargs):
            return SelectionModel(selected=["choice"])

        branch.operate = AsyncMock(side_effect=mock_operate)

        await select_v1(
            branch=branch,
            instruct={"instruction": "Select"},
            choices=["choice"],
            temperature=0.7,
            custom_param="value",
        )

        call_kwargs = branch.operate.call_args[1]
        assert call_kwargs.get("temperature") == 0.7
        assert call_kwargs.get("custom_param") == "value"


class TestSelectV1Integration:
    @pytest.mark.asyncio
    async def test_select_v1_full_workflow_strings(self):
        branch = MagicMock(spec=Branch)

        choices = ["python", "javascript", "rust", "go"]

        async def mock_operate(**kwargs):
            # Simulate LLM selecting based on instruction
            return SelectionModel(selected=["python"])

        branch.operate = AsyncMock(side_effect=mock_operate)

        result = await select_v1(
            branch=branch,
            instruct={
                "instruction": "Select best language for AI",
                "context": {"priority": "ecosystem"},
            },
            choices=choices,
            max_num_selections=1,
        )

        assert "python" in result.selected
        assert len(result.selected) <= 1

    @pytest.mark.asyncio
    async def test_select_v1_full_workflow_enum(self):

        class Framework(Enum):
            PYTORCH = "PyTorch framework"
            TENSORFLOW = "TensorFlow framework"
            JAX = "JAX framework"

        branch = MagicMock(spec=Branch)

        async def mock_operate(**kwargs):
            return SelectionModel(selected=["PYTORCH"])

        branch.operate = AsyncMock(side_effect=mock_operate)

        result = await select_v1(
            branch=branch,
            instruct={"instruction": "Choose ML framework"},
            choices=Framework,
            max_num_selections=1,
        )

        assert Framework.PYTORCH in result.selected

    @pytest.mark.asyncio
    async def test_select_v1_full_workflow_dict(self):
        choices = {
            "fast": "Optimized for speed",
            "reliable": "Optimized for reliability",
            "cheap": "Optimized for cost",
        }

        branch = MagicMock(spec=Branch)

        async def mock_operate(**kwargs):
            return SelectionModel(selected=["reliable"])

        branch.operate = AsyncMock(side_effect=mock_operate)

        result = await select_v1(
            branch=branch,
            instruct={"instruction": "Choose strategy"},
            choices=choices,
            max_num_selections=1,
        )

        assert "Optimized for reliability" in result.selected
