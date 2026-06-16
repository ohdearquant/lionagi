# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""``LionAGIMockFactory`` — legacy AsyncMock-backed factory; prefer ``TestBranch`` for new tests."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

from lionagi.protocols.generic.event import EventStatus
from lionagi.providers.openai.chat.models import OpenAIChatCompletionsRequest
from lionagi.service.connections.api_calling import APICalling
from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig
from lionagi.service.imodel import iModel
from lionagi.session.branch import Branch


def oai_chat_endpoint_config(
    name: str = "openai_chat/completions",
    endpoint: str = "chat/completions",
    request_options: type | None = None,
    kwargs: dict | None = None,
) -> EndpointConfig:
    """OpenAI chat endpoint config for tests that need a real ``Endpoint`` shell.

    Replaces the ad-hoc ``_get_oai_config`` helper duplicated across the legacy
    test suite. Tests that build their own ``APICalling`` for cancellation /
    streaming / hook scenarios should use this rather than re-copying the
    config kwargs.
    """
    return EndpointConfig(
        name=name,
        provider="openai",
        base_url="https://api.openai.com/v1",
        endpoint=endpoint,
        api_key="dummy-key-for-testing",
        request_options=request_options,
        auth_type="bearer",
        content_type="application/json",
        method="POST",
        requires_tokens=True,
        kwargs=kwargs or {},
    )


# Back-compat alias for code that imported the private helper directly.
_get_oai_config = oai_chat_endpoint_config


class LionAGIMockFactory:
    """Centralized factory for AsyncMock-based test branches and iModels. Prefer ``TestBranch`` for new tests."""

    @staticmethod
    def create_mocked_branch(
        name: str = "TestBranch",
        user: str = "tester",
        response: str | dict[str, Any] = "mocked_response_string",
        responses: list[str | dict[str, Any]] | None = None,
        status: EventStatus = EventStatus.COMPLETED,
        provider: str = "openai",
        model: str = "gpt-4o-mini",
        system: Any = None,
        tools: Any = None,
        api_key: str = "test_key",
    ) -> Branch:
        """Build a Branch with a mocked iModel and optional system message/tools.

        Subsumes every per-file ``make_mocked_branch_for_*`` /
        ``_fake_invoke`` pattern in the legacy test suite. Pass
        ``responses=[...]`` for multi-call tests that need a sequence.
        """
        branch = Branch(user=user, name=name, system=system)
        mock_chat_model = LionAGIMockFactory.create_mocked_imodel(
            provider=provider,
            model=model,
            response=response,
            responses=responses,
            status=status,
            api_key=api_key,
        )
        branch.chat_model = mock_chat_model
        branch.parse_model = mock_chat_model
        if tools:
            branch.register_tools(tools)
        return branch

    @staticmethod
    def create_mocked_imodel(
        provider: str = "openai",
        model: str = "gpt-4o-mini",
        response: str | dict[str, Any] = "mocked_response_string",
        responses: list[str | dict[str, Any]] | None = None,
        status: EventStatus = EventStatus.COMPLETED,
        api_key: str = "test_key",
    ) -> iModel:
        mock_model = iModel(provider=provider, model=model, api_key=api_key)

        if responses:
            response_iter = iter(responses)

            async def _sequence_invoke(**kwargs: Any) -> APICalling:
                try:
                    current_response = next(response_iter)
                except StopIteration:
                    current_response = responses[-1] if responses else response
                return LionAGIMockFactory.create_api_calling_mock(
                    response_data=current_response, status=status, model=model
                )

            mock_model.invoke = AsyncMock(side_effect=_sequence_invoke)
        else:

            async def _single_invoke(**kwargs: Any) -> APICalling:
                return LionAGIMockFactory.create_api_calling_mock(
                    response_data=response, status=status, model=model
                )

            mock_model.invoke = AsyncMock(side_effect=_single_invoke)

        return mock_model

    @staticmethod
    def create_api_calling_mock(
        response_data: str | dict[str, Any] = "mocked_response_string",
        status: EventStatus = EventStatus.COMPLETED,
        model: str = "gpt-4o-mini",
        endpoint_config: dict[str, Any] | None = None,
    ) -> APICalling:
        if endpoint_config is None:
            endpoint_config = _get_oai_config(
                name="oai_chat",
                endpoint="chat/completions",
                request_options=OpenAIChatCompletionsRequest,
                kwargs={"model": model},
            )
        endpoint = Endpoint(config=endpoint_config)
        api_call = APICalling(
            payload={"model": model, "messages": []},
            headers={"Authorization": "Bearer test"},
            endpoint=endpoint,
        )
        api_call.execution.response = response_data
        api_call.execution.status = status
        return api_call

    @staticmethod
    def create_mocked_session(
        branches: list[str] | None = None,
        default_branch_response: str | dict[str, Any] = "mocked_response_string",
    ):
        from lionagi.session.session import Session

        session = Session()
        if branches:
            for branch_name in branches:
                branch = LionAGIMockFactory.create_mocked_branch(
                    name=branch_name, response=default_branch_response
                )
                session.branches[branch_name] = branch
        return session

    @staticmethod
    def create_error_response_mock(
        error_message: str = "Mocked API Error",
        error_code: str = "test_error",
        status: EventStatus = EventStatus.FAILED,
    ) -> APICalling:
        return LionAGIMockFactory.create_api_calling_mock(
            response_data={"error": {"message": error_message, "code": error_code}},
            status=status,
        )


__all__ = ("LionAGIMockFactory", "oai_chat_endpoint_config")
