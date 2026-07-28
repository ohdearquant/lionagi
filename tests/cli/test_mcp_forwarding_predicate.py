"""One answer to "does this provider receive a forwarded MCP server set?".

The forwarding implementation and the spawn-time message a caller reads must
agree. They used to keep separate provider lists, so a codex leg was told the
resolved servers were unreachable and was then handed them anyway.
"""

import json
import logging
from types import SimpleNamespace

import pytest

from lionagi._errors import ConfigurationError
from lionagi.agent.factory import (
    _forward_mcp_to_cli_request,
    provider_accepts_forwarded_mcp,
)
from lionagi.cli._logging import _WARN_LOGGER_NAME
from lionagi.cli._mcp_resolve import McpResolution
from lionagi.cli.agent import _report_mcp_resolution

# Every provider name the CLI can route a leg to, plus one API provider, so a
# new CLI backend cannot quietly land on the wrong side of the predicate.
KNOWN_PROVIDERS = ["claude_code", "codex", "gemini-cli", "pi", "claude", "openai"]


class _FakeConfig:
    def __init__(self, provider):
        self.provider = provider
        self.kwargs: dict = {}


class _FakeModel:
    def __init__(self, provider):
        self.endpoint = SimpleNamespace(config=_FakeConfig(provider))

    def copy(self, **_):
        return self


def _fake_branch(provider):
    return SimpleNamespace(chat_model=_FakeModel(provider), id="branch-1")


def _fake_spec(mcp_config_path):
    return SimpleNamespace(
        mcp_config_path=str(mcp_config_path),
        mcp_servers=None,
        cwd=None,
        profile=SimpleNamespace(role=SimpleNamespace(name="implementer")),
    )


@pytest.fixture
def mcp_config(tmp_path):
    path = tmp_path / ".mcp.json"
    path.write_text(json.dumps({"mcpServers": {"khive": {"command": "kkernel"}}}))
    return path


def _capture_warnings(fn):
    records: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    logger = logging.getLogger(_WARN_LOGGER_NAME)
    handler = _Collect()
    logger.addHandler(handler)
    try:
        fn()
    finally:
        logger.removeHandler(handler)
    return records


@pytest.mark.parametrize("provider", KNOWN_PROVIDERS)
def test_predicate_matches_what_the_forwarder_does(provider, mcp_config):
    """The predicate is true exactly when the request comes back carrying servers."""
    branch = _fake_branch(provider)
    if provider == "gemini-cli":
        with pytest.raises(ConfigurationError, match="Antigravity.*does not support MCP"):
            _forward_mcp_to_cli_request(branch, _fake_spec(mcp_config))
        assert provider_accepts_forwarded_mcp(provider) is False
        return

    _forward_mcp_to_cli_request(branch, _fake_spec(mcp_config))

    kwargs = branch.chat_model.endpoint.config.kwargs
    request_carries_servers = bool(kwargs.get("mcp_servers")) or bool(
        kwargs.get("config_overrides")
    )

    assert request_carries_servers is provider_accepts_forwarded_mcp(provider)


def _antigravity_spec():
    """A spec that named no config of its own — the CLI hands the set over."""
    return SimpleNamespace(
        mcp_config_path=None,
        mcp_servers=None,
        cwd=None,
        profile=SimpleNamespace(role=SimpleNamespace(name="reviewer")),
    )


@pytest.mark.parametrize(
    ("resolved", "explicit", "case"),
    [
        ({"khive": {"command": "kkernel"}}, False, "discovered near the launch directory"),
        ({}, False, "handed an empty set, which is a refusal"),
        ({}, True, "named a config that declares no servers"),
        (None, False, "handed nothing at all"),
    ],
)
def test_antigravity_refusal_needs_servers_someone_asked_for(resolved, explicit, case):
    """Refusing to spawn is only right when a caller named servers by name.

    Every one of these arrives as the same handed-over dict a named config
    produces, so a guard that keys on "a set was handed over" turns working
    spawns into hard failures: the CLI hands over what it found near the
    submitting directory on every leg, and an orchestrator opting out of MCP
    hands over an empty set.
    """
    branch = _fake_branch("gemini-cli")

    _forward_mcp_to_cli_request(
        branch,
        _antigravity_spec(),
        resolved_servers=resolved,
        resolved_servers_explicit=explicit,
    )

    assert "mcp_servers" not in branch.chat_model.endpoint.config.kwargs, case


def test_antigravity_refusal_fires_for_a_named_server_set():
    """The counterpart: naming servers this provider cannot reach is an error."""
    branch = _fake_branch("gemini-cli")

    with pytest.raises(ConfigurationError, match="Antigravity.*does not support MCP"):
        _forward_mcp_to_cli_request(
            branch,
            _antigravity_spec(),
            resolved_servers={"khive": {"command": "kkernel"}},
            resolved_servers_explicit=True,
        )


def test_forwarding_providers_are_the_two_cli_transports():
    """Two transports, every spelling the CLI accepts for the Claude one: which
    alias a caller typed is not a capability of the provider behind it, and a
    `claude/...` leg reaches the same endpoint a `claude_code/...` leg does."""
    accepted = {p for p in KNOWN_PROVIDERS if provider_accepts_forwarded_mcp(p)}
    assert accepted == {"claude", "claude_code", "codex"}
    assert {"gemini-cli", "pi", "openai"}.isdisjoint(accepted)


def test_unknown_provider_is_not_assumed_to_accept():
    assert provider_accepts_forwarded_mcp(None) is False
    assert provider_accepts_forwarded_mcp("not-a-provider") is False


def test_codex_spawn_that_forwards_is_not_told_the_servers_are_unreachable():
    """The message a codex leg's caller reads must match what the spawn does."""
    resolution = McpResolution({"khive": {"command": "kkernel"}}, None, "/tmp/.mcp.json", "/tmp")
    messages = _capture_warnings(
        lambda: _report_mcp_resolution(resolution, provider="codex", cwd="/tmp", forwarded=True)
    )
    assert messages == []


def test_codex_spawn_that_does_not_forward_still_says_so():
    resolution = McpResolution({"khive": {"command": "kkernel"}}, None, "/tmp/.mcp.json", "/tmp")
    messages = _capture_warnings(
        lambda: _report_mcp_resolution(resolution, provider="codex", cwd="/tmp", forwarded=False)
    )
    assert len(messages) == 1
    assert "not carried" in messages[0].lower()


def test_build_chat_model_still_sets_the_server_kwarg_for_claude_only():
    """The request-field question is narrower than the capability question:
    the codex request model has no such field, so nothing is set there."""
    from lionagi.cli._providers import build_chat_model

    servers = {"khive": {"command": "kkernel"}}

    claude = build_chat_model(
        "claude_code", "claude-opus-4-5", False, False, None, mcp_servers=servers
    )
    assert claude.endpoint.config.kwargs["mcp_servers"] == servers

    codex = build_chat_model("codex", "gpt-5.3-codex", False, False, None, mcp_servers=servers)
    codex_kwargs = getattr(getattr(codex, "endpoint", None), "config", None)
    assert codex_kwargs is None or "mcp_servers" not in codex_kwargs.kwargs
