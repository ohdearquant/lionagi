# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Declarative validation rules (required/type/range/pattern/custom) for WorkForm fields; see docs/reference/outcomes-work.md for security notes on pattern rules."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, Field

from .form import WorkForm

__all__ = (
    "Rule",
    "RuleSet",
    "CheckKind",
    "REGEX_MAX_INPUT_LENGTH",
)

# Guards the input dimension of worst-case regex backtracking; does not make pathological patterns safe.
REGEX_MAX_INPUT_LENGTH: int = 4096

CheckKind = Literal["required", "type", "range", "pattern", "custom"]


class Rule(BaseModel):
    """A single declarative validation rule targeting one WorkForm field; see docs/reference/outcomes-work.md for params contract."""

    rule_id: str = Field(..., description="Unique rule identifier.")
    field: str = Field(..., description="WorkForm field name this rule applies to.")
    check: CheckKind = Field(..., description="Kind of validation check.")
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="Check-specific parameters.",
    )
    message: str | None = Field(
        None,
        description="Custom error message (overrides auto-generated text).",
    )
    enabled: bool = Field(True, description="Skip rule when False.")

    model_config = {"arbitrary_types_allowed": True}

    def apply(self, form: WorkForm) -> str | None:
        """Apply this rule to *form*; returns an error string on failure, ``None`` on pass or when disabled."""
        if not self.enabled:
            return None

        value = form.values.get(self.field)

        if self.check == "required":
            return self._check_required(value)
        if self.check == "type":
            return self._check_type(value)
        if self.check == "range":
            return self._check_range(value)
        if self.check == "pattern":
            return self._check_pattern(value)
        if self.check == "custom":
            return self._check_custom(value)

        return f"Rule {self.rule_id!r}: unknown check kind {self.check!r}."

    # Internal checkers

    def _check_required(self, value: Any) -> str | None:
        if value is None:
            return self.message or f"Field {self.field!r} is required but missing or None."
        return None

    def _check_type(self, value: Any) -> str | None:
        if value is None:
            return None  # type check does not apply to absent values
        expected = self.params.get("type", "str")
        type_map: dict[str, type] = {
            "str": str,
            "int": int,
            "float": float,
            "bool": bool,
            "list": list,
            "dict": dict,
        }
        target = type_map.get(expected)
        if target is None:
            return f"Rule {self.rule_id!r}: unknown type {expected!r}."
        # Allow int where float is expected (numeric widening).
        if expected == "float" and isinstance(value, int):
            return None
        if not isinstance(value, target):
            return self.message or (
                f"Field {self.field!r} must be type {expected!r}, got {type(value).__name__!r}."
            )
        return None

    def _check_range(self, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, int | float):
            return self.message or (
                f"Field {self.field!r}: range check requires numeric type, "
                f"got {type(value).__name__!r}."
            )
        lo = self.params.get("min")
        hi = self.params.get("max")
        if lo is not None and value < lo:
            return self.message or f"Field {self.field!r} = {value} is below minimum {lo}."
        if hi is not None and value > hi:
            return self.message or f"Field {self.field!r} = {value} exceeds maximum {hi}."
        return None

    def _check_pattern(self, value: Any) -> str | None:
        """Check that *value* matches the declared pattern; trusted patterns only (stdlib re backtracking)."""
        if value is None:
            return None
        if not isinstance(value, str):
            return self.message or (
                f"Field {self.field!r}: pattern check requires str, got {type(value).__name__!r}."
            )

        # Bounds input dimension of worst-case backtracking; does NOT make pathological patterns safe.
        if len(value) > REGEX_MAX_INPUT_LENGTH:
            return self.message or (
                f"Field {self.field!r}: input length {len(value)} exceeds "
                f"the maximum allowed for pattern matching ({REGEX_MAX_INPUT_LENGTH})."
            )

        pattern = self.params.get("pattern", "")
        flags = int(self.params.get("flags", 0))
        try:
            re.compile(pattern, flags)
        except re.error as exc:
            return f"Rule {self.rule_id!r}: invalid regex pattern — {exc}."

        if not re.search(pattern, value, flags):
            return self.message or (
                f"Field {self.field!r} value {value!r} does not match pattern {pattern!r}."
            )
        return None

    def _check_custom(self, value: Any) -> str | None:
        fn: Callable[[Any], bool] | None = self.params.get("callable")
        if fn is None:
            return f"Rule {self.rule_id!r}: 'custom' check requires params['callable']."
        try:
            passed = fn(value)
        except Exception as exc:  # noqa: BLE001
            return f"Rule {self.rule_id!r}: custom check raised {type(exc).__name__}: {exc}."
        if not passed:
            return (
                self.message
                or self.params.get("error")
                or f"Field {self.field!r} failed custom check {self.rule_id!r}."
            )
        return None


class RuleSet:
    """Ordered collection of Rules applied in insertion order; all enabled rules run (no short-circuit)."""

    def __init__(self) -> None:
        self._rules: list[Rule] = []

    def add(self, rule: Rule) -> RuleSet:
        """Append *rule* and return ``self`` for chaining; raises ValueError on duplicate rule_id."""
        if any(r.rule_id == rule.rule_id for r in self._rules):
            raise ValueError(
                f"RuleSet already contains a rule with rule_id={rule.rule_id!r}. "
                "Use a unique rule_id or remove the existing rule first."
            )
        self._rules.append(rule)
        return self

    def remove(self, rule_id: str) -> bool:
        """Remove the rule with *rule_id*.  Returns True if found and removed."""
        before = len(self._rules)
        self._rules = [r for r in self._rules if r.rule_id != rule_id]
        return len(self._rules) < before

    def get(self, rule_id: str) -> Rule | None:
        """Return the rule with *rule_id*, or ``None`` if not found."""
        for r in self._rules:
            if r.rule_id == rule_id:
                return r
        return None

    def rules(self) -> list[Rule]:
        """Return a shallow copy of the rule list."""
        return list(self._rules)

    def apply_all(self, form: WorkForm) -> list[str]:
        """Apply every enabled rule to *form*; return list of error strings (empty = all pass)."""
        errors: list[str] = []
        for rule in self._rules:
            err = rule.apply(form)
            if err is not None:
                errors.append(err)
        return errors
