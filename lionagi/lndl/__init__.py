# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""LNDL — Lion Notation Definition Language Phase 1: types, errors, prompt, extract, normalize."""

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
