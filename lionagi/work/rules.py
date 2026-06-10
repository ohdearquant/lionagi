# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

r"""Declarative validation rules for WorkForm fields.

Rules complement FieldSpec by expressing *value-level* or *cross-field*
constraints that cannot be expressed in a plain type declaration:

- **required**: field must be present and not None.
- **type**: value must be an instance of the declared type.
- **range**: numeric value must fall within [min, max].
- **pattern**: string value must match a regex pattern.
- **custom**: arbitrary Python callable returning bool.

Usage::

    from lionagi.work.rules import Rule, RuleSet
    from lionagi.work.form import WorkForm, FieldSpec

    rs = RuleSet()
    rs.add(Rule(rule_id="r1", field="age", check="range", params={"min": 0, "max": 150}))
    rs.add(Rule(rule_id="r2", field="email", check="pattern",
                params={"pattern": r".+@.+\..+"}))

    errors = rs.apply_all(form)

.. warning::

    **Pattern rules are NOT safe for untrusted or adversarial input.**

    The stdlib ``re`` engine uses backtracking and can hold the GIL during
    catastrophic matches, making any thread-based timeout ineffective.
    Pattern rules are intended for **trusted patterns only** — for example,
    validating application-controlled fields (phone formats, zip codes, etc.)
    where the pattern is authored by the developer, not supplied by users.

    To mitigate worst-case performance: inputs exceeding
    :data:`REGEX_MAX_INPUT_LENGTH` characters are rejected outright before
    the regex engine is invoked.  This bounds the *input* dimension; it does
    not bound the *pattern* dimension.  Nested-quantifier patterns such as
    ``(a+)+`` remain pathological regardless of input length if that limit
    is not tight enough.

    If you need safe matching against untrusted patterns or very long
    inputs, use a non-backtracking engine (e.g., ``google-re2``) and
    provide a ``custom`` rule backed by that engine instead.
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
    "CheckKind",
    "REGEX_MAX_INPUT_LENGTH",
)

# Maximum input length for pattern checks.  Inputs longer than this are
# rejected before regex evaluation.  This limits the *input* dimension of
# worst-case backtracking but does NOT eliminate the risk for pathological
# patterns.  See module docstring.
REGEX_MAX_INPUT_LENGTH: int = 4096

CheckKind = Literal["required", "type", "range", "pattern", "custom"]


class Rule(BaseModel):
    """A single declarative validation rule.

    Attributes:
        rule_id: Unique identifier within a RuleSet.
        field: Name of the WorkForm field this rule targets.
        check: Kind of check to perform.
        params: Check-specific parameters:

            - ``range``: ``{"min": <number>, "max": <number>}`` — either
              or both bounds are optional.
            - ``pattern``: ``{"pattern": "<regex>", "flags": <int>}`` —
              ``flags`` defaults to 0.  See module-level warning about
              trusted-patterns-only.
            - ``type``: ``{"type": "<FieldType>"}`` — one of
              ``str|int|float|bool|list|dict``.
            - ``custom``: ``{"callable": Callable[[Any], bool],
              "error": "<msg>"}`` — ``error`` is the fallback message.

        message: Optional override for the generated error message.
        enabled: When False, this rule is skipped silently.
    """

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
        """Apply this rule to *form*.

        Returns an error string on failure, ``None`` on pass or when disabled.
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
        """Check that *value* matches the declared pattern.

        .. warning::
            Uses the stdlib ``re`` backtracking engine.  Suitable for
            **trusted patterns only**.  See module docstring for details.
        """
        if value is None:
            return None
        if not isinstance(value, str):
            return self.message or (
                f"Field {self.field!r}: pattern check requires str, got {type(value).__name__!r}."
            )

        # Reject inputs that exceed the configurable length limit.
        # This bounds the input dimension of worst-case backtracking; it
        # does NOT make arbitrary patterns safe (see module docstring).
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
    """An ordered collection of :class:`Rule` objects.

    Rules are applied in insertion order.  All rules are evaluated
    (no short-circuit), so the caller receives a complete list of errors.

    Each rule must have a unique ``rule_id`` within this set — :meth:`add`
    raises ``ValueError`` if a duplicate ``rule_id`` is supplied.

    Usage::

        rs = RuleSet()
        rs.add(Rule(...))
        errors = rs.apply_all(form)
    """

    def __init__(self) -> None:
        self._rules: list[Rule] = []

    def add(self, rule: Rule) -> RuleSet:
        """Append *rule* and return ``self`` for chaining.

        Raises:
            ValueError: If a rule with the same ``rule_id`` already exists
                in this set.
        """
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
        """Apply every enabled rule to *form*.

        Returns a list of error messages.  An empty list means all rules
        passed (or all were disabled).
        """
        errors: list[str] = []
        for rule in self._rules:
            err = rule.apply(form)
            if err is not None:
                errors.append(err)
        return errors
