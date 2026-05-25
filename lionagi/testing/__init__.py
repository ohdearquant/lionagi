# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""``lionagi.testing`` — test infrastructure for the Lion ecosystem.

The two big things this module gives you:

1. **``TestBranch`` / ``ScriptedEndpoint``** — a real endpoint registered as
   ``provider="scripted"`` that serves canned responses from a YAML/JSON/dict
   fixture. Branches built via ``TestBranch.from_*`` go through the production
   code path (rate limiter, payload builder, AssistantResponse parser) but
   never touch the network. Lets you assert on what the agent actually sent.

2. **``LionAGIMockFactory``** — the legacy ``AsyncMock``-backed factory,
   preserved here so existing tests keep working. New tests should prefer
   ``TestBranch``.

Plus helpers (``AsyncTestHelpers``, ``ValidationHelpers``, ``TestDataHelpers``),
fixture loaders (``TestDataLoader`` + bundled JSON), and a pytest plugin
exposing all the standard fixtures.

Quick start::

    from lionagi.testing import TestBranch

    branch = TestBranch.from_text("hello back")
    assert await branch.chat("hello") == "hello back"
    assert TestBranch.calls(branch)[0].last_user_message == "hello"

Subprocess tests::

    from lionagi.testing import scripted_env
    import subprocess

    with scripted_env("tests/fixtures/scripts/foo.yaml"):
        r = subprocess.run(["li", "agent", "hi"], capture_output=True, text=True)
"""

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
