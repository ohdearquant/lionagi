# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""LNDL — Lion Notation Definition Language. Structured output format for
LLM responses; tags let models mix natural reasoning with structured data."""

from .assembler import (
    NOTE_NAMESPACE,
    assemble,
    assemble_spec_value,
    build_action_call,
    collect_actions,
    collect_notes,
    replace_actions,
)
from .ast import Identifier, Lact, Literal, Lvar, OutBlock, Program, RLvar
from .diagnostics import (
    LndlChunkHealth,
    LndlRoundRecord,
    LndlTrace,
    classify_chunk,
    classify_result,
    extract_lndl_chunks,
)
from .errors import (
    InvalidConstructorError,
    LNDLError,
    MissingFieldError,
    MissingLvarError,
    TypeMismatchError,
)
from .extract import extract_lndl_blocks
from .lexer import Lexer, Token, TokenType
from .normalize import normalize_lndl_text
from .parser import ParseError, Parser
from .prompt import LNDL_SYSTEM_PROMPT, get_lndl_system_prompt
from .round_outcome import Continue, Exhausted, Failed, Retry, RoundOutcome, Success
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
    "LNDL_SYSTEM_PROMPT",
    "NOTE_NAMESPACE",
    "ActionCall",
    "Continue",
    "Exhausted",
    "Failed",
    "Identifier",
    "InvalidConstructorError",
    "LNDLError",
    "LNDLOutput",
    "Lact",
    "LactMetadata",
    "Lexer",
    "Literal",
    "LndlChunkHealth",
    "LndlRoundRecord",
    "LndlTrace",
    "Lvar",
    "LvarMetadata",
    "MissingFieldError",
    "MissingLvarError",
    "OutBlock",
    "ParseError",
    "ParsedConstructor",
    "Parser",
    "Program",
    "RLvar",
    "RLvarMetadata",
    "Retry",
    "RoundOutcome",
    "Scalar",
    "Success",
    "Token",
    "TokenType",
    "TypeMismatchError",
    "assemble",
    "assemble_spec_value",
    "build_action_call",
    "classify_chunk",
    "classify_result",
    "collect_actions",
    "collect_notes",
    "ensure_no_action_calls",
    "extract_lndl_blocks",
    "extract_lndl_chunks",
    "get_lndl_system_prompt",
    "has_action_calls",
    "normalize_lndl_text",
    "replace_actions",
    "revalidate_with_action_results",
)
