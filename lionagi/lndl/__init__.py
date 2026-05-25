# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""LNDL — Lion Notation Definition Language (Phase 1).

Structured-output format for LLM responses. This module ships the Phase 1
minimal core as an opt-in feature. It is intentionally excluded from any
default routing in ``operations/parse``; callers must explicitly pass an
LNDL schema to activate this path.

Phase 1 exports
---------------
- Types: :class:`LNDLOutput`, :class:`ActionCall`, :class:`LvarMetadata`,
  :class:`RLvarMetadata`, :class:`LactMetadata`, :class:`ParsedConstructor`
- Errors: :class:`LNDLError` and all subclasses
- Prompt: :func:`get_lndl_system_prompt`
- Extract: :func:`extract_lndl_blocks`
- Normalize: :func:`normalize_lndl_text`

Phase 2 (deferred — see issue #966)
------------------------------------
Lexer / Parser / AST / Resolver / Orchestrator / Symbolic-AST modules.
:func:`parse_lndl_fuzzy` is present but raises ``NotImplementedError``
until Phase 2 lands.
"""

from .errors import (
    AmbiguousMatchError,
    InvalidConstructorError,
    LNDLError,
    MissingFieldError,
    MissingLvarError,
    MissingOutBlockError,
    TypeMismatchError,
)
from .extract import extract_lndl_blocks
from .fuzzy import normalize_lndl_text, parse_lndl_fuzzy
from .prompt import LNDL_SYSTEM_PROMPT, get_lndl_system_prompt
from .types import (
    ActionCall,
    LactMetadata,
    LNDLOutput,
    LvarMetadata,
    ParsedConstructor,
    RLvarMetadata,
    Scalar,
    ensure_no_action_calls,
    has_action_calls,
    revalidate_with_action_results,
)

__all__ = (
    # Errors
    "LNDLError",
    "MissingLvarError",
    "MissingFieldError",
    "TypeMismatchError",
    "InvalidConstructorError",
    "MissingOutBlockError",
    "AmbiguousMatchError",
    # Types
    "ActionCall",
    "LactMetadata",
    "LNDLOutput",
    "LvarMetadata",
    "ParsedConstructor",
    "RLvarMetadata",
    "Scalar",
    "ensure_no_action_calls",
    "has_action_calls",
    "revalidate_with_action_results",
    # Prompt
    "LNDL_SYSTEM_PROMPT",
    "get_lndl_system_prompt",
    # Extract
    "extract_lndl_blocks",
    # Fuzzy (Phase 1: normalize only; parse_lndl_fuzzy raises until Phase 2)
    "normalize_lndl_text",
    "parse_lndl_fuzzy",
)
