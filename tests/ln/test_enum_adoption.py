"""Enumerations across the package share ``lionagi.ln.types.Enum``."""

from __future__ import annotations

import enum
from importlib import import_module

import pytest

from lionagi.ln.types import Enum

ENUMS = [
    ("lionagi.hooks.bus", "HookPoint"),
    ("lionagi.operations.builder", "ExpansionStrategy"),
    ("lionagi.plugins.registry", "PluginState"),
    ("lionagi.plugins.trust", "TrustState"),
    ("lionagi.protocols.messages.base", "MessageRole"),
    ("lionagi.protocols.messages.base", "MessageField"),
    ("lionagi.providers.exa._schemas", "LivecrawlType"),
    ("lionagi.providers.exa.search", "SearchCategory"),
    ("lionagi.providers.exa.search", "SearchType"),
    ("lionagi.providers.firecrawl._schemas", "OutputFormat"),
    ("lionagi.providers.perplexity.chat", "PerplexityModels"),
    ("lionagi.providers.tavily.search", "SearchDepth"),
    ("lionagi.providers.tavily.search", "SearchTopic"),
    ("lionagi.service.accounts._types", "RouterErrorCode"),
    ("lionagi.service.connections._chat_schemas", "ChatRole"),
    ("lionagi.service.resilience", "CircuitState"),
    ("lionagi.session.control", "LoopDirective"),
    ("lionagi.state.health", "SessionHealth"),
    ("lionagi.tools.base", "ResourceCategory"),
    ("lionagi.tools.code.search", "SearchAction"),
    ("lionagi.tools.coding", "SandboxAction"),
    ("lionagi.tools.communication.messenger", "MessengerAction"),
    ("lionagi.tools.context.context", "ContextAction"),
    ("lionagi.tools.file.editor", "EditorAction"),
    ("lionagi.tools.file.reader", "ReaderAction"),
]


@pytest.mark.parametrize(("module", "name"), ENUMS, ids=[name for _, name in ENUMS])
def test_each_enum_lists_its_values_and_stays_a_standard_enum(module, name):
    cls = getattr(import_module(module), name)
    assert issubclass(cls, Enum)
    assert issubclass(cls, enum.Enum)
    assert cls.allowed() == tuple(member.value for member in cls)


def test_message_fields_and_roles_read_from_their_enums():
    from lionagi.protocols.messages.base import (
        MESSAGE_FIELDS,
        MessageRole,
        validate_sender_recipient,
    )

    assert MESSAGE_FIELDS == [
        "created_at",
        "role",
        "content",
        "id",
        "sender",
        "recipient",
        "metadata",
    ]
    for role in ("system", "user", "assistant", "unset", "action"):
        assert validate_sender_recipient(role) is MessageRole(role)
