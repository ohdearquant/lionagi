# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for zero-rewrite governed provider adapters.

All framework dependencies (LangChain, CrewAI, openai-agents, Anthropic
Agent SDK) are mocked — tests pass without any of them installed.

The governance module (lionagi.protocols.governance) is also mocked so
these tests work against the worktree without requiring the full
governance stack.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import sys
import types
import warnings
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256(value: Any) -> str:
    try:
        s = json.dumps(value, sort_keys=True, default=str)
    except Exception:
        s = repr(value)
    return hashlib.sha256(s.encode()).hexdigest()


# ---------------------------------------------------------------------------
# GovernedAdapter base tests
# ---------------------------------------------------------------------------


class TestGovernedAdapterBase:
    """Tests for GovernedAdapter without governance (charter=None)."""

    def _make_adapter(self, wrapped=None, **kwargs):
        from lionagi.adapters.governed_base import GovernedAdapter

        class ConcreteAdapter(GovernedAdapter):
            def _get_tool_name(self) -> str:
                return "test.tool"

            async def _call_wrapped(self, *args, **kw):
                if asyncio_callable := getattr(wrapped, "run", None):
                    if callable(asyncio_callable):
                        return asyncio_callable(*args, **kw)
                return "mock_result"

        return ConcreteAdapter(wrapped or MagicMock(), **kwargs)

    @pytest.mark.asyncio
    async def test_passthrough_without_charter(self):
        """Without charter, execute returns (result, None)."""
        adapter = self._make_adapter()
        result, cert = await adapter.execute()
        assert result == "mock_result"
        assert cert is None

    @pytest.mark.asyncio
    async def test_passthrough_result_preserved(self):
        """The wrapped object's return value is forwarded unchanged."""
        from lionagi.adapters.governed_base import GovernedAdapter

        wrapped = MagicMock()
        wrapped.run.return_value = {"answer": 42}

        class PassAdapter(GovernedAdapter):
            def _get_tool_name(self):
                return "test.pass"

            async def _call_wrapped(self, *args, **kw):
                return self._wrapped.run(*args, **kw)

        adapter = PassAdapter(wrapped)
        result, cert = await adapter.execute("question")
        assert result == {"answer": 42}
        assert cert is None

    def test_invalid_on_deny_raises(self):
        from lionagi.adapters.governed_base import GovernedAdapter

        class Adapter1(GovernedAdapter):
            def _get_tool_name(self):
                return "t"

            async def _call_wrapped(self, *a, **kw):
                return "ok"

        with pytest.raises(ValueError, match="on_deny"):
            Adapter1(MagicMock(), on_deny="explode")

    def test_get_tool_name_default(self):
        """Default tool name uses the wrapped class name."""
        from lionagi.adapters.governed_base import GovernedAdapter

        class Adapter2(GovernedAdapter):
            async def _call_wrapped(self, *a, **kw):
                return "ok"

        mock = MagicMock()
        mock.__class__.__name__ = "MyChain"
        adapter = Adapter2(mock)
        assert adapter._get_tool_name() == "governed.mychain"

    def test_hash_args_stability(self):
        """Same args produce same hash; different args produce different hash."""
        from lionagi.adapters.governed_base import GovernedAdapter

        class Adapter3(GovernedAdapter):
            def _get_tool_name(self):
                return "t"

            async def _call_wrapped(self, *a, **kw):
                return "ok"

        adapter = Adapter3(MagicMock())
        h1 = adapter._hash_args(("hello",), {"k": 1})
        h2 = adapter._hash_args(("hello",), {"k": 1})
        h3 = adapter._hash_args(("world",), {"k": 1})
        assert h1 == h2
        assert h1 != h3

    def test_hash_result_stability(self):
        """Same result produces same hash."""
        from lionagi.adapters.governed_base import GovernedAdapter

        class Adapter4(GovernedAdapter):
            def _get_tool_name(self):
                return "t"

            async def _call_wrapped(self, *a, **kw):
                return "ok"

        adapter = Adapter4(MagicMock())
        assert adapter._hash_result("hello") == adapter._hash_result("hello")
        assert adapter._hash_result("hello") != adapter._hash_result("world")

    @pytest.mark.asyncio
    async def test_call_wrapped_not_implemented(self):
        """GovernedAdapter.execute raises NotImplementedError if _call_wrapped not overridden."""
        from lionagi.adapters.governed_base import GovernedAdapter

        adapter = GovernedAdapter(MagicMock())
        with pytest.raises(NotImplementedError):
            await adapter.execute()


# ---------------------------------------------------------------------------
# Governance integration tests (with mocked governance module)
# ---------------------------------------------------------------------------


class TestGovernedAdapterWithGovernance:
    """Tests for GovernedAdapter WITH a charter (governance mocked)."""

    def _mock_gate_result(self, verdict_value: str):
        """Build a fake GateResult-like object."""
        gr = MagicMock()
        verdict = MagicMock()
        verdict.value = verdict_value
        gr.verdict = verdict
        gr.gate_id = "gate-001"
        gr.justification = f"Test justification ({verdict_value})"
        return gr

    def _mock_controller(self, verdict_value: str = "allow"):
        ctrl = MagicMock()
        ctrl.pre_op_check.return_value = self._mock_gate_result(verdict_value)
        ctrl.post_op_record.return_value = None
        fake_cert = MagicMock()
        fake_cert.certificate_id = "cert-abc"
        ctrl.mint_certificate.return_value = fake_cert
        return ctrl

    @pytest.mark.asyncio
    async def test_certificate_minted_with_charter(self):
        """When charter is provided, execute returns a certificate."""
        from lionagi.adapters.governed_base import GovernedAdapter

        class AdapterE(GovernedAdapter):
            def _get_tool_name(self):
                return "test.governed"

            async def _call_wrapped(self, *a, **kw):
                return "governed_result"

        wrapped = MagicMock()
        adapter = AdapterE(wrapped)
        # Inject a mock controller directly (bypass import machinery)
        adapter._controller = self._mock_controller("allow")

        result, cert = await adapter.execute("input")
        assert result == "governed_result"
        assert cert is not None
        assert cert.certificate_id == "cert-abc"

    @pytest.mark.asyncio
    async def test_on_deny_raise(self):
        """on_deny='raise' raises GovernanceViolationError on DENY verdict."""
        from lionagi.adapters.governed_base import GovernanceViolationError, GovernedAdapter

        class AdapterF(GovernedAdapter):
            def _get_tool_name(self):
                return "test.deny"

            async def _call_wrapped(self, *a, **kw):
                return "should_not_reach"

        adapter = AdapterF(MagicMock(), on_deny="raise")
        ctrl = self._mock_controller("deny")

        adapter._controller = ctrl

        # Patch _is_denied to return True for deny verdict
        with patch.object(adapter, "_is_denied", return_value=True):
            with pytest.raises(GovernanceViolationError):
                await adapter.execute()

    @pytest.mark.asyncio
    async def test_on_deny_skip(self):
        """on_deny='skip' returns (None, None) on DENY verdict."""
        from lionagi.adapters.governed_base import GovernedAdapter

        class AdapterG(GovernedAdapter):
            def _get_tool_name(self):
                return "test.skip"

            async def _call_wrapped(self, *a, **kw):
                return "should_not_reach"

        adapter = AdapterG(MagicMock(), on_deny="skip")
        adapter._controller = self._mock_controller("deny")

        with patch.object(adapter, "_is_denied", return_value=True):
            result, cert = await adapter.execute()
        assert result is None
        assert cert is None

    @pytest.mark.asyncio
    async def test_on_deny_log(self):
        """on_deny='log' emits a warning but CONTINUES execution (returns real result)."""
        from lionagi.adapters.governed_base import GovernedAdapter

        class AdapterH(GovernedAdapter):
            def _get_tool_name(self):
                return "test.log"

            async def _call_wrapped(self, *a, **kw):
                return "execution_proceeded"

        adapter = AdapterH(MagicMock(), on_deny="log")
        adapter._controller = self._mock_controller("deny")

        with patch.object(adapter, "_is_denied", return_value=True):
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                result, cert = await adapter.execute()
        # Execution must have continued — real result, not None
        assert result == "execution_proceeded"
        # A warning should have been emitted
        assert any("denied" in str(warning.message).lower() for warning in w)

    @pytest.mark.asyncio
    async def test_post_op_record_called(self):
        """post_op_record is called after successful execution."""
        from lionagi.adapters.governed_base import GovernedAdapter

        class AdapterJ(GovernedAdapter):
            def _get_tool_name(self):
                return "test.record"

            async def _call_wrapped(self, *a, **kw):
                return "result"

        adapter = AdapterJ(MagicMock())
        ctrl = self._mock_controller("allow")
        adapter._controller = ctrl

        await adapter.execute("arg1")
        ctrl.post_op_record.assert_called_once()


# ---------------------------------------------------------------------------
# GovernanceViolationError tests
# ---------------------------------------------------------------------------


class TestGovernanceViolationError:
    """Tests for GovernanceViolationError standalone behavior."""

    def test_raise_and_catch(self):
        from lionagi.adapters.governed_base import GovernanceViolationError

        with pytest.raises(GovernanceViolationError):
            raise GovernanceViolationError("gate denied")

    def test_message(self):
        from lionagi.adapters.governed_base import GovernanceViolationError

        err = GovernanceViolationError("my message")
        assert "my message" in str(err)

    def test_default_message(self):
        from lionagi.adapters.governed_base import GovernanceViolationError

        err = GovernanceViolationError()
        assert "governance" in str(err).lower() or "denied" in str(err).lower()


# ---------------------------------------------------------------------------
# Per-adapter tool name tests
# ---------------------------------------------------------------------------


class TestAdapterToolNames:
    """Each adapter's _get_tool_name() returns the expected value."""

    def test_openai_agents_tool_name(self):
        from lionagi.adapters.openai_agents import GovernedOpenAIAgent

        adapter = GovernedOpenAIAgent(MagicMock())
        assert adapter._get_tool_name() == "openai_agents.run"

    def test_anthropic_agents_tool_name(self):
        from lionagi.adapters.anthropic_agents import GovernedAnthropicAgent

        adapter = GovernedAnthropicAgent(MagicMock())
        assert adapter._get_tool_name() == "anthropic_agents.run"

    def test_langchain_tool_name(self):
        from lionagi.adapters.langchain import GovernedChain

        adapter = GovernedChain(MagicMock())
        assert adapter._get_tool_name() == "langchain.chain"

    def test_crewai_tool_name(self):
        from lionagi.adapters.crewai import GovernedCrew

        adapter = GovernedCrew(MagicMock())
        assert adapter._get_tool_name() == "crewai.crew"


# ---------------------------------------------------------------------------
# Lazy import / missing framework tests
# ---------------------------------------------------------------------------


class TestMissingFrameworks:
    """Adapters raise ImportError with clear messages when frameworks absent."""

    def test_openai_agents_missing(self):
        """_require_openai_agents raises ImportError when openai-agents absent."""
        import lionagi.adapters.openai_agents as mod

        with patch.dict(sys.modules, {"agents": None}):
            importlib.reload(mod)
            with pytest.raises(ImportError, match="openai-agents"):
                mod._require_openai_agents()

    def test_langchain_missing(self):
        """_require_langchain raises ImportError when langchain absent."""
        import lionagi.adapters.langchain as lc_mod

        with patch.dict(sys.modules, {"langchain_core": None, "langchain": None}):
            importlib.reload(lc_mod)
            with pytest.raises(ImportError, match="langchain|LangChain"):
                lc_mod._require_langchain()

    def test_crewai_missing(self):
        """_require_crewai raises ImportError when crewai absent."""
        import lionagi.adapters.crewai as crewai_mod

        with patch.dict(sys.modules, {"crewai": None}):
            importlib.reload(crewai_mod)
            with pytest.raises(ImportError, match="crewai|CrewAI"):
                crewai_mod._require_crewai()


# ---------------------------------------------------------------------------
# Mocked framework integration tests
# ---------------------------------------------------------------------------


class TestMockedFrameworkRuns:
    """Tests that actually call run() with mocked framework objects."""

    @pytest.mark.asyncio
    async def test_langchain_chain_ainvoke(self):
        """GovernedChain.run uses ainvoke when available."""
        from lionagi.adapters.langchain import GovernedChain

        chain = MagicMock()
        chain.ainvoke = AsyncMock(return_value={"text": "LangChain answer"})

        adapter = GovernedChain(chain)
        with patch("lionagi.adapters.langchain._require_langchain"):
            result, cert = await adapter.run({"question": "test"})
        assert result == {"text": "LangChain answer"}
        assert cert is None
        chain.ainvoke.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_langchain_chain_invoke_fallback(self):
        """GovernedChain.run falls back to sync invoke wrapped in executor."""
        from lionagi.adapters.langchain import GovernedChain

        chain = MagicMock(spec=["invoke"])
        chain.invoke = MagicMock(return_value="sync result")

        adapter = GovernedChain(chain)
        with patch("lionagi.adapters.langchain._require_langchain"):
            result, cert = await adapter.run("input text")
        assert result == "sync result"
        assert cert is None

    @pytest.mark.asyncio
    async def test_crewai_crew_kickoff(self):
        """GovernedCrew.run calls crew.kickoff() via executor."""
        from lionagi.adapters.crewai import GovernedCrew

        crew = MagicMock(spec=["kickoff"])
        crew.kickoff = MagicMock(return_value="crew output")

        adapter = GovernedCrew(crew)
        with patch("lionagi.adapters.crewai._require_crewai"):
            result, cert = await adapter.run(inputs={"topic": "AI"})
        assert result == "crew output"
        assert cert is None

    @pytest.mark.asyncio
    async def test_crewai_crew_akickoff_preferred(self):
        """GovernedCrew.run prefers akickoff when available."""
        from lionagi.adapters.crewai import GovernedCrew

        crew = MagicMock()
        crew.akickoff = AsyncMock(return_value="async crew output")

        adapter = GovernedCrew(crew)
        with patch("lionagi.adapters.crewai._require_crewai"):
            result, cert = await adapter.run()
        assert result == "async crew output"
        assert cert is None
        crew.akickoff.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_anthropic_agent_arun(self):
        """GovernedAnthropicAgent.run uses arun when available."""
        from lionagi.adapters.anthropic_agents import GovernedAnthropicAgent

        agent = MagicMock()
        agent.arun = AsyncMock(return_value="anthropic result")

        adapter = GovernedAnthropicAgent(agent)
        with patch("lionagi.adapters.anthropic_agents._require_anthropic_agents"):
            result, cert = await adapter.run("What is 2+2?")
        assert result == "anthropic result"
        assert cert is None

    @pytest.mark.asyncio
    async def test_anthropic_agent_run_fallback(self):
        """GovernedAnthropicAgent.run falls back to sync run via executor."""
        from lionagi.adapters.anthropic_agents import GovernedAnthropicAgent

        agent = MagicMock(spec=["run"])  # Only sync run, no arun
        agent.run = MagicMock(return_value="sync anthropic")

        adapter = GovernedAnthropicAgent(agent)
        with patch("lionagi.adapters.anthropic_agents._require_anthropic_agents"):
            result, cert = await adapter.run("question")
        assert result == "sync anthropic"

    @pytest.mark.asyncio
    async def test_openai_agents_runner(self):
        """GovernedOpenAIAgent.run calls Runner.run on an Agent."""
        from lionagi.adapters.openai_agents import GovernedOpenAIAgent

        # Build a minimal fake agents module
        fake_agents = types.ModuleType("agents")

        class FakeRunner:
            @staticmethod
            async def run(agent_or_runner, user_input, **kwargs):
                return f"ran: {user_input}"

        fake_agents.Runner = FakeRunner

        agent_mock = MagicMock()
        adapter = GovernedOpenAIAgent(agent_mock)

        with patch(
            "lionagi.adapters.openai_agents._require_openai_agents",
            return_value=fake_agents,
        ):
            result, cert = await adapter.run("hello world")
        assert result == "ran: hello world"
        assert cert is None


# ---------------------------------------------------------------------------
# __init__ lazy imports
# ---------------------------------------------------------------------------


class TestAdaptersPackageExports:
    """GovernedAdapter and GovernanceViolationError importable from package root."""

    def test_governed_adapter_importable(self):
        from lionagi.adapters import GovernedAdapter

        assert GovernedAdapter is not None

    def test_governance_violation_error_importable(self):
        from lionagi.adapters import GovernanceViolationError

        assert GovernanceViolationError is not None

    def test_lazy_governed_chain(self):
        from lionagi.adapters import GovernedChain  # noqa: F401

    def test_lazy_governed_crew(self):
        from lionagi.adapters import GovernedCrew  # noqa: F401

    def test_lazy_governed_openai_agent(self):
        from lionagi.adapters import GovernedOpenAIAgent  # noqa: F401

    def test_lazy_governed_anthropic_agent(self):
        from lionagi.adapters import GovernedAnthropicAgent  # noqa: F401

    def test_unknown_attr_raises(self):
        import lionagi.adapters as pkg

        with pytest.raises(AttributeError):
            _ = pkg.NonExistentAdapter
