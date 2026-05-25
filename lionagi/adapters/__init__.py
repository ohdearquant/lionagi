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
from .json_ import JsonAdapter
from .toml_ import TomlAdapter

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
)
