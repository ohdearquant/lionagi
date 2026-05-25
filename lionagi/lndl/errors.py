# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0


class LNDLError(Exception):
    """Base exception for LNDL parsing/validation errors."""


class MissingLvarError(LNDLError):
    """Referenced lvar does not exist."""


class MissingFieldError(LNDLError):
    """Required Spec field missing from OUT{} block."""


class TypeMismatchError(LNDLError):
    """Constructor class doesn't match Spec type."""


class InvalidConstructorError(LNDLError):
    """Cannot parse constructor syntax."""


class MissingOutBlockError(LNDLError):
    """No OUT{} block found in response."""


class AmbiguousMatchError(LNDLError):
    """Multiple fields match with similar similarity scores (tie)."""
