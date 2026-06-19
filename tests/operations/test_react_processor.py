from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from lionagi.operations.ReAct.utils import Analysis, ReActAnalysis


def _make_branch_mock():
    from uuid import uuid4

    branch = MagicMock()
    branch.user = "tester"
    branch.id = uuid4()
    branch.chat_model = MagicMock()
    branch.msgs = MagicMock()
    branch.msgs.last_response = MagicMock()
    branch.msgs.last_response.response = "fallback_response"
    return branch


def _make_react_analysis(extension_needed: bool = False) -> ReActAnalysis:
    return ReActAnalysis(
        analysis="test reasoning",
        extension_needed=extension_needed,
    )


def _make_analysis(answer: str = "final answer") -> Analysis:
    return Analysis(answer=answer)


def _make_chat_param(branch):
    from lionagi.operations.types import ChatParam

    return ChatParam(
        guidance=None,
        context=None,
        sender=branch.user,
        recipient=branch.id,
        response_format=None,
        progression=None,
        tool_schemas=[],
        images=[],
        image_detail="auto",
        plain_content="",
        include_token_usage_to_model=False,
        imodel=branch.chat_model,
        imodel_kw={},
    )


def _make_parse_param():
    from lionagi.ln.fuzzy import FuzzyMatchKeysParams
    from lionagi.operations.parse.parse import get_default_call
    from lionagi.operations.types import ParseParam

    return ParseParam(
        response_format=ReActAnalysis,
        fuzzy_match_params=FuzzyMatchKeysParams(),
        handle_validation="return_value",
        alcall_params=get_default_call(),
        imodel=None,
        imodel_kw={},
    )


class TestReActV1ReturnAnalysis:
    async def test_return_analysis_returns_list(self):
        from lionagi.operations.ReAct.ReAct import ReAct_v1

        branch = _make_branch_mock()
        chat_param = _make_chat_param(branch)
        parse_param = _make_parse_param()

        analysis_obj = _make_react_analysis(extension_needed=False)
        final_obj = _make_analysis("done")

        call_count = 0

        async def mock_operate(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return analysis_obj if call_count == 1 else final_obj

        with patch(
            "lionagi.operations.operate.operate.operate",
            new=AsyncMock(side_effect=mock_operate),
        ):
            result = await asyncio.wait_for(
                ReAct_v1(
                    branch=branch,
                    instruction="What is 2+2?",
                    chat_param=chat_param,
                    parse_param=parse_param,
                    max_extensions=0,
                    extension_allowed=False,
                    return_analysis=True,
                ),
                timeout=5.0,
            )

        assert isinstance(result, list)
        assert len(result) >= 1


class TestReActV1VerboseAnalysis:
    async def test_verbose_analysis_runs_without_error(self):
        from lionagi.operations.ReAct.ReAct import ReAct_v1

        branch = _make_branch_mock()
        chat_param = _make_chat_param(branch)
        parse_param = _make_parse_param()

        analysis_obj = _make_react_analysis(extension_needed=False)
        final_obj = _make_analysis("verbose answer")

        call_count = 0

        async def mock_operate(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return analysis_obj if call_count == 1 else final_obj

        with (
            patch(
                "lionagi.operations.operate.operate.operate",
                new=AsyncMock(side_effect=mock_operate),
            ),
            patch("lionagi.libs.schema.as_readable.as_readable", return_value=""),
        ):
            result = await asyncio.wait_for(
                ReAct_v1(
                    branch=branch,
                    instruction="Test verbose",
                    chat_param=chat_param,
                    parse_param=parse_param,
                    max_extensions=0,
                    extension_allowed=False,
                    verbose_analysis=True,
                ),
                timeout=5.0,
            )

        assert result is not None


class TestReActV1FinalResultWithoutAnswer:
    async def test_final_result_no_answer_attribute(self):
        from lionagi.operations.ReAct.ReAct import ReAct_v1

        branch = _make_branch_mock()
        chat_param = _make_chat_param(branch)

        analysis_obj = _make_react_analysis(extension_needed=False)
        plain_result = "plain string result"

        call_count = 0

        async def mock_operate(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return analysis_obj if call_count == 1 else plain_result

        with patch(
            "lionagi.operations.operate.operate.operate",
            new=AsyncMock(side_effect=mock_operate),
        ):
            result = await asyncio.wait_for(
                ReAct_v1(
                    branch=branch,
                    instruction="Return raw",
                    chat_param=chat_param,
                    max_extensions=0,
                    extension_allowed=False,
                ),
                timeout=5.0,
            )

        assert result == plain_result


class TestReActStreamMaxExtensionsClamp:
    async def test_max_extensions_over_100_clamped(self):
        from lionagi.operations.ReAct.ReAct import ReActStream

        branch = _make_branch_mock()
        chat_param = _make_chat_param(branch)

        analysis_obj = _make_react_analysis(extension_needed=False)
        final_obj = _make_analysis("ok")

        call_count = 0

        async def mock_operate(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return analysis_obj if call_count == 1 else final_obj

        results = []
        with patch(
            "lionagi.operations.operate.operate.operate",
            new=AsyncMock(side_effect=mock_operate),
        ):
            async for item in ReActStream(
                branch=branch,
                instruction="test",
                chat_param=chat_param,
                max_extensions=150,
                extension_allowed=False,
            ):
                results.append(item)

        assert len(results) >= 1


class TestReActStreamContinueAfterFailedResponse:
    async def test_continue_after_failed_response(self):
        from lionagi.operations.ReAct.ReAct import ReActStream

        branch = _make_branch_mock()
        chat_param = _make_chat_param(branch)

        analysis_obj = _make_react_analysis(extension_needed=True)
        failed_dict = {"analysis": None, "extension_needed": None}
        final_obj = _make_analysis("recovered")

        call_count = 0

        async def mock_operate(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return analysis_obj
            elif call_count == 2:
                return failed_dict
            else:
                return final_obj

        results = []
        with patch(
            "lionagi.operations.operate.operate.operate",
            new=AsyncMock(side_effect=mock_operate),
        ):
            async for item in ReActStream(
                branch=branch,
                instruction="test recovery",
                chat_param=chat_param,
                max_extensions=1,
                extension_allowed=True,
                continue_after_failed_response=True,
            ):
                results.append(item)

        assert len(results) >= 1


class TestReActStreamExceptPath:
    async def test_except_path_returns_last_response(self):
        from lionagi.operations.ReAct.ReAct import ReActStream

        branch = _make_branch_mock()
        branch.msgs.last_response.response = "fallback"
        chat_param = _make_chat_param(branch)

        analysis_obj = _make_react_analysis(extension_needed=False)
        call_count = 0

        async def mock_operate(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return analysis_obj
            raise RuntimeError("final operate failed")

        results = []
        with patch(
            "lionagi.operations.operate.operate.operate",
            new=AsyncMock(side_effect=mock_operate),
        ):
            async for item in ReActStream(
                branch=branch,
                instruction="trigger except",
                chat_param=chat_param,
                max_extensions=0,
                extension_allowed=False,
            ):
                results.append(item)

        assert len(results) >= 1
        last = results[-1]
        assert last == "fallback"


class TestReActStreamBetweenRounds:
    async def test_between_rounds_with_injection(self):
        from lionagi.operations.ReAct.ReAct import ReActStream

        branch = _make_branch_mock()
        chat_param = _make_chat_param(branch)

        ext_analysis = _make_react_analysis(extension_needed=True)
        no_ext_analysis = _make_react_analysis(extension_needed=False)
        final_obj = _make_analysis("between rounds answer")

        call_count = 0

        async def mock_operate(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return ext_analysis
            elif call_count == 2:
                return no_ext_analysis
            else:
                return final_obj

        injection_called = []

        async def between_rounds(b, round_num):
            injection_called.append(round_num)
            return "injected instruction"

        results = []
        with patch(
            "lionagi.operations.operate.operate.operate",
            new=AsyncMock(side_effect=mock_operate),
        ):
            async for item in ReActStream(
                branch=branch,
                instruction="test between rounds",
                chat_param=chat_param,
                max_extensions=1,
                extension_allowed=True,
                between_rounds=between_rounds,
            ):
                results.append(item)

        assert len(injection_called) >= 1
        assert len(results) >= 2


class TestReActStreamBetweenRoundsNoInjection:
    async def test_between_rounds_no_injection(self):
        from lionagi.operations.ReAct.ReAct import ReActStream

        branch = _make_branch_mock()
        chat_param = _make_chat_param(branch)

        ext_analysis = _make_react_analysis(extension_needed=True)
        no_ext_analysis = _make_react_analysis(extension_needed=False)
        final_obj = _make_analysis("normal extension answer")

        call_count = 0

        async def mock_operate(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return ext_analysis
            elif call_count == 2:
                return no_ext_analysis
            else:
                return final_obj

        async def between_rounds(b, round_num):
            return None

        results = []
        with patch(
            "lionagi.operations.operate.operate.operate",
            new=AsyncMock(side_effect=mock_operate),
        ):
            async for item in ReActStream(
                branch=branch,
                instruction="test no injection",
                chat_param=chat_param,
                max_extensions=1,
                extension_allowed=True,
                between_rounds=between_rounds,
            ):
                results.append(item)

        assert len(results) >= 2


class TestReActStreamReasoningEffort:
    async def test_reasoning_effort_low(self):
        from lionagi.operations.ReAct.ReAct import ReActStream

        branch = _make_branch_mock()
        chat_param = _make_chat_param(branch)

        ext_analysis = _make_react_analysis(extension_needed=True)
        no_ext_analysis = _make_react_analysis(extension_needed=False)
        final_obj = _make_analysis("effort answer")

        call_count = 0

        async def mock_operate(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return ext_analysis
            elif call_count == 2:
                return no_ext_analysis
            else:
                return final_obj

        results = []
        with patch(
            "lionagi.operations.operate.operate.operate",
            new=AsyncMock(side_effect=mock_operate),
        ):
            async for item in ReActStream(
                branch=branch,
                instruction="test effort",
                chat_param=chat_param,
                max_extensions=1,
                extension_allowed=True,
                reasoning_effort="low",
            ):
                results.append(item)

        assert len(results) >= 2


class TestReActWrapper:
    async def test_react_with_verbose_kwarg(self):
        from lionagi.operations.ReAct.ReAct import ReAct

        branch = _make_branch_mock()

        analysis_obj = _make_react_analysis(extension_needed=False)
        final_obj = _make_analysis("wrapper answer")

        call_count = 0

        async def mock_operate(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return analysis_obj if call_count == 1 else final_obj

        with (
            patch(
                "lionagi.operations.operate.operate.operate",
                new=AsyncMock(side_effect=mock_operate),
            ),
            patch("lionagi.libs.schema.as_readable.as_readable", return_value=""),
        ):
            result = await asyncio.wait_for(
                ReAct(
                    branch=branch,
                    instruct={"instruction": "test verbose kwarg"},
                    extension_allowed=False,
                    verbose=True,
                ),
                timeout=5.0,
            )

        assert result is not None

    async def test_react_with_instruct_object(self):
        from lionagi.operations.fields import Instruct
        from lionagi.operations.ReAct.ReAct import ReAct

        branch = _make_branch_mock()
        analysis_obj = _make_react_analysis(extension_needed=False)
        final_obj = _make_analysis("instruct answer")

        call_count = 0

        async def mock_operate(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return analysis_obj if call_count == 1 else final_obj

        with patch(
            "lionagi.operations.operate.operate.operate",
            new=AsyncMock(side_effect=mock_operate),
        ):
            result = await asyncio.wait_for(
                ReAct(
                    branch=branch,
                    instruct=Instruct(instruction="test instruct object"),
                    extension_allowed=False,
                ),
                timeout=5.0,
            )

        assert result is not None

    async def test_react_return_analysis(self):
        from lionagi.operations.ReAct.ReAct import ReAct

        branch = _make_branch_mock()
        analysis_obj = _make_react_analysis(extension_needed=False)
        final_obj = _make_analysis("analysis list answer")

        call_count = 0

        async def mock_operate(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return analysis_obj if call_count == 1 else final_obj

        with patch(
            "lionagi.operations.operate.operate.operate",
            new=AsyncMock(side_effect=mock_operate),
        ):
            result = await asyncio.wait_for(
                ReAct(
                    branch=branch,
                    instruct={"instruction": "return list"},
                    extension_allowed=False,
                    return_analysis=True,
                ),
                timeout=5.0,
            )

        assert isinstance(result, list)
