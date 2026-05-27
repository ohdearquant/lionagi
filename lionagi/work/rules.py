# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

r"""Declarative validation rules for WorkForm fields.

Rules complement FieldSpec by expressing *cross-field* or *value-level*
constraints that cannot be expressed in a plain type declaration:

- **required**: field must be present (alias for FieldSpec.required, useful
  when you want rules separate from schema).
- **type**: re-checks the declared type (useful in rule-only pipelines).
- **range**: numeric value must fall within [min, max].
- **pattern**: string value must match a regex pattern.
- **custom**: arbitrary Python callable.

Usage::

    from lionagi.work.rules import Rule, RuleSet
    from lionagi.work.form import WorkForm, FieldSpec

    rs = RuleSet()
    rs.add(Rule(rule_id="r1", field="age", check="range", params={"min": 0, "max": 150}))
    rs.add(Rule(rule_id="r2", field="email", check="pattern", params={"pattern": r".+@.+\..+"}))

    errors = rs.apply_all(form)
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, Field

from .form import WorkForm

__all__ = (
    "Rule",
    "RuleSet",
)

CheckKind = Literal["required", "type", "range", "pattern", "custom"]


class Rule(BaseModel):
    """A single declarative validation rule.

    Attributes:
        rule_id: Unique identifier within a RuleSet.
        field: Name of the WorkForm field this rule targets.
        check: Kind of check to perform.
        params: Check-specific parameters (see class-level docs).
        message: Optional override for the generated error message.
        enabled: When False, this rule is skipped silently.
    """

    rule_id: str = Field(..., description="Unique rule identifier.")
    field: str = Field(..., description="WorkForm field name this rule applies to.")
    check: CheckKind = Field(..., description="Kind of validation check.")
    params: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Check-specific params.  "
            "range: {min, max} (either or both optional).  "
            "pattern: {pattern: str, flags: int (optional)}.  "
            "type: {type: str} (FieldType literal).  "
            "custom: {callable: Callable[[Any], bool], error: str}."
        ),
    )
    message: str | None = Field(
        None,
        description="Custom error message template (overrides auto-generated text).",
    )
    enabled: bool = Field(True, description="Skip rule when False.")

    model_config = {"arbitrary_types_allowed": True}

    def apply(self, form: WorkForm) -> str | None:
        """Apply this rule to *form*.

        Returns an error string on failure, ``None`` on pass.
        """
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

    # ------------------------------------------------------------------
    # Internal checkers
    # ------------------------------------------------------------------

    def _check_required(self, value: Any) -> str | None:
        if value is None:
            return self.message or f"Field {self.field!r} is required but missing or None."
        return None

    def _check_type(self, value: Any) -> str | None:
        if value is None:
            return None  # type check doesn't apply to absent values
        expected = self.params.get("type", "str")
        type_map = {
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
        # Allow int where float is expected.
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
            return (
                self.message
                or f"Field {self.field!r}: range check requires numeric type, "
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
        if value is None:
            return None
        if not isinstance(value, str):
            return (
                self.message
                or f"Field {self.field!r}: pattern check requires str, "
                f"got {type(value).__name__!r}."
            )
        pattern = self.params.get("pattern", "")
        flags = int(self.params.get("flags", 0))
        try:
            if not re.search(pattern, value, flags):
                return self.message or (
                    f"Field {self.field!r} value {value!r} does not match pattern {pattern!r}."
                )
        except re.error as exc:
            return f"Rule {self.rule_id!r}: invalid regex pattern — {exc}."
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
            return self.message or (
                self.params.get("error")
                or f"Field {self.field!r} failed custom check {self.rule_id!r}."
            )
        return None


class RuleSet:
    """An ordered collection of :class:`Rule` objects.

    Rules are applied in insertion order.  All rules are evaluated (no
    short-circuit), so the caller gets a complete list of errors.

    Usage::

        rs = RuleSet()
        rs.add(Rule(...))
        errors = rs.apply_all(form)
    """

    def __init__(self) -> None:
        self._rules: list[Rule] = []

    def add(self, rule: Rule) -> RuleSet:
        """Append *rule* and return ``self`` for chaining."""
        self._rules.append(rule)
        return self

    def remove(self, rule_id: str) -> bool:
        """Remove the rule with *rule_id*.  Returns True if found."""
        before = len(self._rules)
        self._rules = [r for r in self._rules if r.rule_id != rule_id]
        return len(self._rules) < before

    def get(self, rule_id: str) -> Rule | None:
        """Return the rule with *rule_id*, or None."""
        for r in self._rules:
            if r.rule_id == rule_id:
                return r
        return None

    def rules(self) -> list[Rule]:
        """Return a shallow copy of the rule list."""
        return list(self._rules)

    def apply_all(self, form: WorkForm) -> list[str]:
        """Apply every enabled rule to *form*.

        Returns a list of error messages.  An empty list means all rules passed.
        """
        errors: list[str] = []
        for rule in self._rules:
            err = rule.apply(form)
            if err is not None:
                errors.append(err)
        return errors
