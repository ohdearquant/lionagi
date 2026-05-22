# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression test: AG2 GroupChat nlip_url SSRF bypass (issue #985 round 3).

AG2GroupChatEndpoint.stream() accepts caller-supplied agent_configs with an
arbitrary 'nlip_url' value.  Before the fix that value was copied into
AgentSpec and handed directly to NlipRemoteAgent(url=...), which opens its
own HTTP connection and never passes through the Endpoint SSRF guards.

These tests assert that a metadata/private nlip_url is rejected with
PermissionError before NlipRemoteAgent is ever constructed.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest


def _autogen_stubs() -> dict:
    """Minimal sys.modules stubs so build_group_chat can be imported and called
    without autogen installed.  The SSRF guard fires before any autogen object
    is actually used, so the stubs only need to be importable."""
    stub = MagicMock()
    return {
        "autogen": stub,
        "autogen.agentchat": stub,
        "autogen.agentchat.conversable_agent": stub,
        "autogen.agentchat.group": stub,
        "autogen.agentchat.group.patterns": stub,
        "autogen.agentchat.contrib": stub,
        "autogen.agentchat.contrib.nlip_agent": stub,
    }


# ---------------------------------------------------------------------------
# Unit-level: _assert_nlip_url_safe helper
# ---------------------------------------------------------------------------


class TestAssertNlipUrlSafe:
    """Direct tests for the shared SSRF guard helper."""

    def test_private_ip_raises_permission_error(self):
        from lionagi.providers.ag2.nlip.models import _assert_nlip_url_safe

        with patch("lionagi.providers.ag2.nlip.models.is_ssrf_safe", return_value=False):
            with pytest.raises(PermissionError, match="SSRF guard"):
                _assert_nlip_url_safe("http://169.254.169.254/")

    def test_loopback_raises_permission_error(self):
        from lionagi.providers.ag2.nlip.models import _assert_nlip_url_safe

        with patch("lionagi.providers.ag2.nlip.models.is_ssrf_safe", return_value=False):
            with pytest.raises(PermissionError, match="SSRF guard"):
                _assert_nlip_url_safe("http://127.0.0.1/")

    def test_bad_scheme_raises_value_error(self):
        from lionagi.providers.ag2.nlip.models import _assert_nlip_url_safe

        with pytest.raises(ValueError, match="unsupported scheme"):
            _assert_nlip_url_safe("ftp://example.com/")

    def test_public_url_passes(self):
        from lionagi.providers.ag2.nlip.models import _assert_nlip_url_safe

        # Patch where the name is actually looked up: in the nlip models module
        with patch("lionagi.providers.ag2.nlip.models.is_ssrf_safe", return_value=True):
            # Must not raise
            _assert_nlip_url_safe("https://nlip.example.com/")


# ---------------------------------------------------------------------------
# Integration: build_group_chat validates nlip_url before NlipRemoteAgent
# ---------------------------------------------------------------------------


class TestBuildGroupChatNlipUrlSSRF:
    """build_group_chat() must reject private nlip_url before constructing
    NlipRemoteAgent so the third-party agent never opens a connection."""

    def _make_spec(self, nlip_url: str):
        from lionagi.providers.ag2.groupchat.models import AgentSpec, GroupChatSpec

        return GroupChatSpec(
            name="test_chat",
            objective="test",
            agents=[
                AgentSpec(
                    name="evil-remote",
                    role="attacker",
                    nlip_url=nlip_url,
                )
            ],
        )

    def test_metadata_ip_blocked_before_nlip_remote_agent(self):
        """169.254.169.254 (AWS IMDS / link-local) must raise PermissionError.

        Stubs autogen so the outer ConversableAgent import succeeds; the SSRF
        guard fires in the per-agent loop before NlipRemoteAgent is reached.
        """
        from lionagi.providers.ag2.groupchat.models import build_group_chat

        spec = self._make_spec("http://169.254.169.254/")

        with patch.dict(sys.modules, _autogen_stubs()):
            with patch("lionagi.providers.ag2.nlip.models.is_ssrf_safe", return_value=False):
                with pytest.raises(PermissionError, match="SSRF guard"):
                    build_group_chat(spec, llm_config=None)

    def test_rfc1918_blocked(self):
        """10.x private range must also be blocked."""
        from lionagi.providers.ag2.groupchat.models import build_group_chat

        spec = self._make_spec("http://10.0.0.1/")

        with patch.dict(sys.modules, _autogen_stubs()):
            with patch("lionagi.providers.ag2.nlip.models.is_ssrf_safe", return_value=False):
                with pytest.raises(PermissionError, match="SSRF guard"):
                    build_group_chat(spec, llm_config=None)

    def test_loopback_blocked(self):
        """127.0.0.1 loopback must be blocked."""
        from lionagi.providers.ag2.groupchat.models import build_group_chat

        spec = self._make_spec("http://127.0.0.1:8080/")

        with patch.dict(sys.modules, _autogen_stubs()):
            with patch("lionagi.providers.ag2.nlip.models.is_ssrf_safe", return_value=False):
                with pytest.raises(PermissionError, match="SSRF guard"):
                    build_group_chat(spec, llm_config=None)

    def test_public_nlip_url_proceeds_past_guard(self):
        """A public nlip_url passes the guard. Downstream autogen calls may fail
        in the test env (no real AG2 install) but no PermissionError must fire."""
        from lionagi.providers.ag2.groupchat.models import build_group_chat

        spec = self._make_spec("https://nlip.example.com/")

        with patch.dict(sys.modules, _autogen_stubs()):
            with patch("lionagi.providers.ag2.nlip.models.is_ssrf_safe", return_value=True):
                try:
                    build_group_chat(spec, llm_config=None)
                except PermissionError:
                    pytest.fail("PermissionError raised for a public nlip_url")
                except Exception:
                    # Stub autogen returns MagicMocks; any downstream error
                    # (AttributeError, etc.) is acceptable — the SSRF guard passed.
                    pass


# ---------------------------------------------------------------------------
# Integration: AG2GroupChatEndpoint.stream() blocks private nlip_url
# ---------------------------------------------------------------------------


class TestAG2GroupChatEndpointNlipUrlSSRF:
    """Caller-supplied agent_configs[*].nlip_url must be SSRF-checked before
    NlipRemoteAgent is constructed in AG2GroupChatEndpoint.stream()."""

    def _make_endpoint(self):
        from lionagi.providers.ag2.groupchat.endpoint import AG2GroupChatEndpoint
        from lionagi.service.connections import EndpointConfig

        cfg = EndpointConfig(
            name="ag2-groupchat",
            provider="ag2",
            base_url="",
            endpoint="groupchat",
            method="stream",
            kwargs={},
        )
        return AG2GroupChatEndpoint(config=cfg)

    @pytest.mark.asyncio
    async def test_nlip_url_ssrf_blocked_in_stream(self):
        """Caller-supplied nlip_url for a remote NLIP agent must be SSRF-checked.

        Regression for issue #985 round 3: AG2GroupChatEndpoint.stream()
        previously handed agent_configs[*]['nlip_url'] directly to
        NlipRemoteAgent without any guard.
        """
        endpoint = self._make_endpoint()
        agent_configs = [
            {
                "name": "evil-remote",
                "role": "attacker",
                "nlip_url": "http://169.254.169.254/",
            }
        ]

        with patch.dict(sys.modules, _autogen_stubs()):
            with patch("lionagi.providers.ag2.nlip.models.is_ssrf_safe", return_value=False):
                with pytest.raises((PermissionError, ValueError), match="SSRF"):
                    async for _ in endpoint.stream(
                        request={"prompt": "test"},
                        agent_configs=agent_configs,
                    ):
                        pass

    @pytest.mark.asyncio
    async def test_nlip_url_blocked_before_nlip_remote_agent_constructed(self):
        """NlipRemoteAgent must never be instantiated for a blocked nlip_url.

        Patches _assert_nlip_url_safe directly so we can assert it was called,
        proving the guard fires before any NlipRemoteAgent construction attempt.
        """
        endpoint = self._make_endpoint()
        agent_configs = [
            {
                "name": "evil-remote",
                "role": "attacker",
                "nlip_url": "http://10.10.10.1/",
            }
        ]

        with patch.dict(sys.modules, _autogen_stubs()):
            with patch(
                "lionagi.providers.ag2.nlip.models._assert_nlip_url_safe",
                side_effect=PermissionError("SSRF guard: blocked"),
            ) as mock_guard:
                with pytest.raises(PermissionError, match="SSRF guard"):
                    async for _ in endpoint.stream(
                        request={"prompt": "test"},
                        agent_configs=agent_configs,
                    ):
                        pass
                # Guard must have been called with the attacker URL
                mock_guard.assert_called_once_with("http://10.10.10.1/")
