# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Test infrastructure for the Lion ecosystem: scripted endpoints, mock factories, and pytest fixtures."""

from __future__ import annotations

from ._branch import TestBranch, scripted_imodel
from ._endpoint import ENV_SCRIPT_PATH, ScriptedEndpoint
from ._env import (
    DEFAULT_SCRIPTED_MODEL,
    ENV_MODEL,
    ENV_PROVIDER,
    SCRIPTED_PROVIDER,
    is_scripted_provider_active,
    resolve_script_path,
    scripted_env,
    subprocess_env,
)
from ._legacy import LionAGIMockFactory, oai_chat_endpoint_config
from ._script import ScriptExhaustedError, ScriptModel
from ._types import (
    ErrorResponse,
    RecordedCall,
    ResponseEntry,
    StreamChunkSpec,
    StreamResponse,
    StructuredResponse,
    TextResponse,
    ToolCallResponse,
    WhenMatcher,
)
from .helpers import (
    AsyncTestHelpers,
    IModelKwargCaptor,
    MockClaudeCode,
    MockElement,
    TestDataHelpers,
    ValidationHelpers,
    make_mock_element_class,
)
from .loaders import (
    TestDataLoader,
    get_api_response,
    get_conversation,
    get_error_scenario,
    load_test_data,
)

__all__ = (
    # Scripted infrastructure
    "ENV_MODEL",
    "ENV_PROVIDER",
    "ENV_SCRIPT_PATH",
    "DEFAULT_SCRIPTED_MODEL",
    "SCRIPTED_PROVIDER",
    "ScriptedEndpoint",
    "ScriptExhaustedError",
    "ScriptModel",
    "TestBranch",
    "is_scripted_provider_active",
    "resolve_script_path",
    "scripted_env",
    "scripted_imodel",
    "subprocess_env",
    # Response entry types
    "ErrorResponse",
    "RecordedCall",
    "ResponseEntry",
    "StreamChunkSpec",
    "StreamResponse",
    "StructuredResponse",
    "TextResponse",
    "ToolCallResponse",
    "WhenMatcher",
    # Legacy
    "LionAGIMockFactory",
    "oai_chat_endpoint_config",
    # Helpers
    "AsyncTestHelpers",
    "IModelKwargCaptor",
    "MockClaudeCode",
    "MockElement",
    "TestDataHelpers",
    "TestDataLoader",
    "ValidationHelpers",
    "make_mock_element_class",
    "get_api_response",
    "get_conversation",
    "get_error_scenario",
    "load_test_data",
)
