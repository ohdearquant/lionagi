# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""WorkForm: structured input/output container for worker tasks.

A WorkForm captures a typed specification (FieldSpec) for every input
and output slot a worker needs, tracks live values, and records the
validation status of those values.  The lifecycle is:

    draft → filled → validated  (happy path)
    draft → filled → error      (validation failed)
    validated → submitted       (engine accepted it)
    submitted → completed       (worker finished)
    error → draft               (allow re-opening for correction)
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

__all__ = (
    "FieldSpec",
    "FieldType",
    "FormStatus",
    "VALID_TRANSITIONS",
    "WorkForm",
    "fill_form",
    "validate_form",
)

# Allowed value-type labels.  "list" and "dict" are JSON containers.
FieldType = Literal["str", "int", "float", "bool", "list", "dict"]

_PYTHON_TYPE_MAP: dict[str, type] = {
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "list": list,
    "dict": dict,
}

FormStatus = Literal["draft", "filled", "validated", "error", "submitted", "completed"]

# Allowed lifecycle transitions.  Any move not listed here is invalid.
VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"filled"}),
    "filled": frozenset({"validated", "error"}),
    "validated": frozenset({"submitted", "error"}),
    "error": frozenset({"draft"}),  # allow re-opening for correction
    "submitted": frozenset({"completed", "error"}),
    "completed": frozenset(),  # terminal — no outgoing transitions
}


class FieldSpec(BaseModel):
    """Declaration of a single field inside a WorkForm.

    Attributes:
        name: Machine-readable field name (alphanumeric + underscores,
            must start with a letter or underscore).
        type: Expected Python type expressed as a string literal.
        required: When True, the form cannot be validated with this
            field absent or None.
        default: Value used when the field is absent and not required.
        description: Human-readable explanation of this field's purpose.
    """

    name: str = Field(..., description="Field identifier (alphanumeric + underscores).")
    type: FieldType = Field("str", description="Expected value type.")
    required: bool = Field(True, description="Whether this field must be supplied.")
    default: Any = Field(None, description="Default value when field is absent.")
    description: str = Field("", description="Human-readable description.")

    @model_validator(mode="after")
    def _validate_name(self) -> FieldSpec:
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", self.name):
            raise ValueError(
                f"Field name {self.name!r} must start with a letter or underscore "
                "and contain only alphanumeric characters and underscores."
            )
        return self

    def coerce(self, value: Any) -> Any:
        """Attempt to coerce *value* to this field's declared type.

        Returns the coerced value on success, raises ``TypeError`` on failure.
        ``None`` is returned unchanged.
        """
        if value is None:
            return None
        target = _PYTHON_TYPE_MAP[self.type]
        if isinstance(value, target):
            return value
        # Numeric widening: int → float is allowed.
        if self.type == "float" and isinstance(value, int):
            return float(value)
        # str → bool special case.
        if self.type == "bool" and isinstance(value, str):
            if value.lower() in {"true", "1", "yes"}:
                return True
            if value.lower() in {"false", "0", "no"}:
                return False
        # str → int / float.
        if self.type in {"int", "float"} and isinstance(value, str):
            try:
                return target(value)
            except ValueError:
                pass
        raise TypeError(
            f"Field {self.name!r} expects type {self.type!r}, "
            f"got {type(value).__name__!r} with value {value!r}."
        )


class WorkForm(BaseModel):
    """A structured data container for a single worker invocation.

    WorkForm instances are *immutable by convention* — mutation helpers
    (:func:`fill_form`, :func:`validate_form`, :meth:`transition_to`)
    always return a *new* copy via ``model_copy``.

    Attributes:
        form_id: Unique string identifier (template name + optional suffix).
        title: Human-readable label shown in UI and logs.
        fields: Ordered mapping from field name to its FieldSpec.
        values: Mutable mapping from field name to its current value.
        status: Lifecycle status of this form instance.
        validation_errors: List of human-readable error messages from the
            last call to :func:`validate_form`.
    """

    form_id: str = Field(..., description="Unique form identifier.")
    title: str = Field("", description="Human-readable form title.")
    fields: dict[str, FieldSpec] = Field(
        default_factory=dict,
        description="Field name → FieldSpec mapping.",
    )
    values: dict[str, Any] = Field(
        default_factory=dict,
        description="Current field values.",
    )
    status: FormStatus = Field("draft", description="Form lifecycle status.")
    validation_errors: list[str] = Field(
        default_factory=list,
        description="Errors from the most recent validation pass.",
    )

    model_config = {"arbitrary_types_allowed": True}

    def get(self, name: str, default: Any = None) -> Any:
        """Return the value for *name*, falling back to *default*."""
        return self.values.get(name, default)

    def field_names(self) -> list[str]:
        """Return the list of declared field names."""
        return list(self.fields.keys())

    def is_complete(self) -> bool:
        """Return True when status is ``validated`` or ``completed``."""
        return self.status in {"validated", "completed"}

    def transition_to(self, new_status: FormStatus) -> WorkForm:
        """Return a *new* WorkForm after validating the status transition.

        Args:
            new_status: The desired next lifecycle status.

        Returns:
            A new WorkForm with ``status`` set to *new_status*.

        Raises:
            ValueError: If the transition from the current status to
                *new_status* is not permitted by :data:`VALID_TRANSITIONS`.
        """
        allowed = VALID_TRANSITIONS.get(self.status, frozenset())
        if new_status not in allowed:
            raise ValueError(
                f"Invalid transition {self.status!r} → {new_status!r}.  "
                f"Allowed from {self.status!r}: "
                f"{sorted(allowed) or '(none — terminal state)'}."
            )
        return self.model_copy(update={"status": new_status})


# ---------------------------------------------------------------------------
# Functional API
# ---------------------------------------------------------------------------


def fill_form(form: WorkForm, values: dict[str, Any]) -> WorkForm:
    """Return a *new* WorkForm with *values* merged into it.

    Missing fields whose FieldSpec declares a non-None ``default`` are
    pre-filled with that default.  After merging, :func:`validate_form` is
    called automatically — the returned form will have status ``validated``
    or ``error``.

    Args:
        form: Source form (not mutated).
        values: Key/value pairs to set on the form.

    Returns:
        A new WorkForm instance with merged values and updated status.
    """
    merged: dict[str, Any] = {}
    for name, spec in form.fields.items():
        if name in values:
            merged[name] = values[name]
        elif spec.default is not None:
            merged[name] = spec.default
        # Required with no value: leave absent so validate_form flags it.

    # Propagate extra keys that are not declared in spec (passed through as-is).
    for k, v in values.items():
        if k not in merged:
            merged[k] = v

    filled = form.model_copy(update={"values": merged, "status": "filled", "validation_errors": []})
    return validate_form(filled)


def validate_form(form: WorkForm) -> WorkForm:
    """Validate *form* values against its FieldSpec declarations.

    Returns a *new* WorkForm with status ``validated`` when all checks pass,
    or ``error`` with ``validation_errors`` populated when any check fails.

    Checks performed per declared field:

    1. Required fields must be present (key exists) and not ``None``.
    2. Present values must be coercible to the declared type; coerced
       values are stored in the returned form's ``values``.

    Args:
        form: Form to validate (not mutated).

    Returns:
        New WorkForm with updated ``status`` and ``validation_errors``.
    """
    errors: list[str] = []
    coerced_values: dict[str, Any] = dict(form.values)

    for name, spec in form.fields.items():
        value = form.values.get(name)

        # Required check.
        if spec.required and value is None:
            errors.append(f"Field {name!r} is required but missing or None.")
            continue

        # Type check / coercion (only when a value is present).
        if value is not None:
            try:
                coerced_values[name] = spec.coerce(value)
            except TypeError as exc:
                errors.append(str(exc))

    new_status: FormStatus = "error" if errors else "validated"
    return form.model_copy(
        update={
            "values": coerced_values,
            "status": new_status,
            "validation_errors": errors,
        }
    )
