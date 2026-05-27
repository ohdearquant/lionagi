# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""
lionagi.adapters — inlined adapter stack (no pydapter runtime dependency).

Core protocols and registries:
  Adaptable, AsyncAdaptable, Adapter, AsyncAdapter
  AdapterRegistry, AsyncAdapterRegistry

Built-in adapters:
  JsonAdapter   — JSON files, strings, bytes
  CsvAdapter    — CSV files, strings
  TomlAdapter   — TOML files, strings
  DataFrameAdapter — pandas DataFrames (optional; requires pandas)

Spec adapters (separate sub-package):
  lionagi.adapters.spec_adapters

Governed framework adapters (zero-rewrite wrappers):
  GovernedAdapter      — base class for all governed adapters
  GovernedChain        — LangChain Runnable/Chain/Agent  (requires langchain)
  GovernedCrew         — CrewAI Crew                     (requires crewai)
  GovernedOpenAIAgent  — openai-agents Runner/Agent       (requires openai-agents)
  GovernedAnthropicAgent — Anthropic Agent SDK Agent     (requires anthropic[agents])
  GovernanceViolationError — raised on hard governance denial
"""

from ._base import (
    Adaptable,
    Adapter,
    AdapterBase,
    AdapterConfigurationError,
    AdapterConnectionError,
    AdapterError,
    AdapterNotFoundError,
    AdapterParseError,
    AdapterQueryError,
    AdapterRegistry,
    AdapterResourceError,
    AdapterValidationError,
    AsyncAdaptable,
    AsyncAdapter,
    AsyncAdapterRegistry,
    dispatch_adapt_meth,
)
from .csv_ import CsvAdapter
from .governed_base import GovernanceViolationError, GovernedAdapter
from .json_ import JsonAdapter
from .toml_ import TomlAdapter


def __getattr__(name: str):  # noqa: N807
    """Lazy-load framework-specific governed adapters on first access."""
    if name == "GovernedChain":
        from .langchain import GovernedChain

        return GovernedChain
    if name == "GovernedCrew":
        from .crewai import GovernedCrew

        return GovernedCrew
    if name == "GovernedOpenAIAgent":
        from .openai_agents import GovernedOpenAIAgent

        return GovernedOpenAIAgent
    if name == "GovernedAnthropicAgent":
        from .anthropic_agents import GovernedAnthropicAgent

        return GovernedAnthropicAgent
    raise AttributeError(f"module 'lionagi.adapters' has no attribute {name!r}")


__all__ = (
    # protocols / mixins
    "Adaptable",
    "AsyncAdaptable",
    "Adapter",
    "AsyncAdapter",
    "AdapterBase",
    "AdapterRegistry",
    "AsyncAdapterRegistry",
    "dispatch_adapt_meth",
    # exceptions
    "AdapterError",
    "AdapterValidationError",
    "AdapterParseError",
    "AdapterNotFoundError",
    "AdapterConfigurationError",
    "AdapterResourceError",
    "AdapterConnectionError",
    "AdapterQueryError",
    # adapters
    "JsonAdapter",
    "CsvAdapter",
    "TomlAdapter",
    # governed adapters
    "GovernedAdapter",
    "GovernanceViolationError",
    "GovernedChain",
    "GovernedCrew",
    "GovernedOpenAIAgent",
    "GovernedAnthropicAgent",
)
